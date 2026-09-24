"""Bounded repair observations, daily stock planning and replay diagnostics."""
from collections import Counter, deque
from dataclasses import dataclass, field, asdict
import json
import logging
from .model import pos
from .wall_health import repair_risk

LOG = logging.getLogger(__name__)


def log_event(turn, event, **values):
    LOG.info("round=%s %s=%s", turn.round, event,
             json.dumps({"day": turn.day, "tick": turn.tick, **values},
                        ensure_ascii=False, separators=(",", ":")))


@dataclass
class NightRecord:
    day: int
    target: int
    opening_tick: int
    wave: dict
    used: int = 0
    failed: int = 0
    unconfirmed: int = 0
    remaining: int = 0
    empty_tick: int | None = None
    unmet: set = field(default_factory=set)
    unreachable: set = field(default_factory=set)
    lost: set = field(default_factory=set)

    def report(self):
        values = asdict(self)
        for key in ("unmet", "unreachable", "lost"):
            values[key] = sorted(values[key])
        return values


@dataclass
class WallWatchState:
    history: deque = field(default_factory=lambda: deque(maxlen=3))
    night: NightRecord | None = None
    plan_day: int = 0
    target: int = 3
    previous_round: int = -1
    previous_day: int = 0
    previous_tick: int = 0
    previous_walls: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=dict)
    damage: dict = field(default_factory=dict)
    signatures: dict = field(default_factory=dict)

    def stock_target(self, turn):
        if self.plan_day == turn.day:
            return self.target
        baseline = min(9, max(3, turn.day - 1))
        target, reasons = baseline, ["day_baseline"]
        last = self.history[-1] if self.history else None
        if last:
            target = max(target, last.used + 1)
            if last.unmet:
                # Exhaustion is censored demand; an early empty bag needs more
                # reserve, not just yesterday's observed consumption count.
                buffer = 2 + min(2, max(0, 130 - (last.empty_tick or 130)) // 20)
                # Project observed use over a 60-turn night. A 20-turn minimum
                # window avoids multiplying one opening repair into 60 packs.
                elapsed = max(20, (last.empty_tick or 130) - last.opening_tick)
                projected = (last.used * 60 + elapsed - 1) // elapsed
                target = max(target, last.used + buffer, projected + 1)
                reasons.append("early_stockout" if buffer > 2 else "stockout")
            elif last.remaining >= 2 and not last.lost:
                target = max(target, last.target - 1)
                reasons.append("surplus_decay")
            else:
                reasons.append("confirmed_consumption")
        # One repairer has at most 60 night actions. Budget/capacity usually
        # impose a much smaller limit; no fixed 12-pack ceiling traps feedback.
        self.plan_day, self.target = turn.day, min(60, target)
        if turn.day >= 4:
            log_event(turn, "wall_stock_plan", target=self.target, baseline=baseline,
                      cap=60, reasons=reasons, previous=last.report() if last else None)
        return self.target

    def recent_damage(self, wall, round_no):
        return max((loss for r, loss in self.damage.get((wall.id, wall.level), ())
                    if 0 <= round_no - r < 3), default=0)

    def observe(self, turn, mem):
        if turn.round <= self.previous_round:
            return
        consecutive = turn.round == self.previous_round + 1
        walls = {w.id: w for w in turn.ours if w.kind == "wall"}
        healing = {w.pos for uid, w in walls.items() if uid in self.previous_walls
                   and (w.level != self.previous_walls[uid].level
                        or w.health > self.previous_walls[uid].health)}
        if self.night and self.previous_tick >= 70:
            results = turn.raw.get("lastRoundRoleActionResults") or {}
            for actor, action in mem.last_commands.items():
                name = action.get("name", "")
                points = action.get("targetPos", [])
                if action.get("action") != "use" or name != "WallFixer":
                    continue
                uid = int(actor)
                hero = turn.units.get(uid)
                legal = results.get(actor, results.get(uid)) if consecutive else None
                consumed = (consecutive and hero is not None
                            and self.inventory.get(uid, 0) - hero.inventory['WallFixer'] == 1)
                outcome = "failed" if legal is False else "used" if consumed else "unconfirmed"
                if outcome == "used" and points:
                    healing.add(pos(points[0]))
                setattr(self.night, outcome, getattr(self.night, outcome) + 1)
                log_event(turn, "wall_repair_result", actor=uid, outcome=outcome,
                          issued_round=self.previous_round, legal=legal,
                          inventory_before=self.inventory.get(uid),
                          inventory_after=hero.inventory['WallFixer'] if hero else None)
        # Drop stale samples on gaps, new IDs/levels, dawn, or attempted healing.
        self.damage = {key: [(r, loss) for r, loss in samples if turn.round - r < 3]
                       for key, samples in self.damage.items()
                       if consecutive and not turn.is_day and key[0] in walls
                       and walls[key[0]].level == key[1] and walls[key[0]].pos not in healing}
        if consecutive and self.previous_tick >= 70 and self.night:
            removed = {pos(c['targetPos'][0]) for c in mem.last_commands.values()
                       if c.get('action') == 'remove' and c.get('targetPos')}
            for uid, old in self.previous_walls.items():
                wall = walls.get(uid)
                if wall is None and old.pos not in removed:
                    self.night.lost.add(uid)
                    log_event(turn, "wall_loss", wall=uid, pos=old.pos, level=old.level,
                              last_health=old.health, previous_round=self.previous_round,
                              watcher=mem.wall_watch_id, inventory_before=self.inventory,
                              previous_action=mem.last_commands.get(str(mem.wall_watch_id)))
                elif (wall and wall.level == old.level and wall.pos not in healing
                      and old.health > wall.health and not turn.is_day):
                    self.damage.setdefault((uid, wall.level), []).append((turn.round, old.health - wall.health))
        # Settle the last night action before the dawn summary and new plan.
        if self.night and turn.day != self.night.day:
            self.night.remaining = sum(h.inventory['WallFixer'] for h in turn.heroes)
            log_event(turn, "wall_night_summary", **self.night.report())
            self.history.append(self.night)
            self.night = None
        target = self.stock_target(turn)
        if not turn.is_day and self.night is None:
            wave = dict(Counter(r.kind for r in turn.robots if turn.threatens_us(r)))
            self.night = NightRecord(turn.day, target, turn.tick, wave)
        if self.night:
            self.night.remaining = sum(h.inventory['WallFixer'] for h in turn.heroes)
        self.previous_round, self.previous_day, self.previous_tick = turn.round, turn.day, turn.tick
        self.previous_walls = walls
        self.inventory = {h.id: h.inventory['WallFixer'] for h in turn.heroes}

    def shortage(self, turn, wall, reason):
        if self.night is None or self.night.day != turn.day:
            return
        if reason == "no_pack":
            self.night.unmet.add(wall.id)
            if self.night.empty_tick is None:
                self.night.empty_tick = turn.tick
        elif reason == "no_route":
            self.night.unreachable.add(wall.id)

    def decision(self, turn, event, **values):
        # Keep use/buy events; throttle repeated waits/movements to ten rounds.
        key = (event, values.get("actor"))
        signature = tuple((k, str(v)) for k, v in values.items()
                          if k not in ("health", "steps", "recent_damage", "gold"))
        if (values.get("action") in ("use", "buy") or self.signatures.get(key) != signature
                or turn.tick % 10 == 0):
            log_event(turn, event, **values)
            self.signatures[key] = signature

    def finish(self, turn, mem, response):
        for actor, action in response['roleCommandMap'].items():
            if action.get('action') != 'use' or action.get('name') != 'WallFixer':
                continue
            wall = next((w for w in turn.ours if w.pos == pos(action['targetPos'][0])), None)
            if wall:
                log_event(turn, "wall_repair_attempt", actor=int(actor), wall=wall.id,
                          health=wall.health, pos=wall.pos, **asdict(repair_risk(turn, wall, mem)))
        if not turn.is_day and (turn.tick % 10 == 0 or turn.tick == 129):
            watcher = turn.units.get(mem.wall_watch_id)
            log_event(turn, "wall_watch_status", watcher=mem.wall_watch_id, target=self.target,
                      held=watcher.inventory['WallFixer'] if watcher else 0,
                      action=response['roleCommandMap'].get(str(mem.wall_watch_id)),
                      night=self.night.report() if self.night else None,
                      final_tick=turn.tick == 129)
