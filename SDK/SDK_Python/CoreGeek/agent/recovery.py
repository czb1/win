"""Bounded worker recovery: rules generate goals; a small model only ranks them."""
from dataclasses import dataclass, field, replace
import json
import logging

from .commands import command
from .mining import return_destination
from .model import ORES, distance, neighbours

LOG = logging.getLogger(__name__)


@dataclass
class Recovery:
    idle: dict = field(default_factory=dict)
    active: dict = field(default_factory=dict)
    offered: list = field(default_factory=list)
    ready: list = field(default_factory=list)
    next_call: int = 0

    def observe(self, turn, mem):
        self.ready = []
        self.idle = {h.id: (self.idle.get(h.id, 0) + 1
                           if mem.last_round == turn.round - 1
                           and str(h.id) not in mem.last_commands else 0)
                     for h in turn.workers}
        self.active = {uid: job for uid, job in self.active.items()
                       if uid in self.idle and turn.is_day and job[1] >= turn.round}

    def accept(self, parsed, turn):
        # No generated commands, coordinates, Python, or free-form plans.
        if (isinstance(parsed, dict) and set(parsed) == {"choice"}
                and type(parsed["choice"]) is int
                and 0 <= parsed["choice"] < len(self.offered)):
            goal = self.offered[parsed["choice"]]
            self.active[goal[0]] = (goal, turn.round + 8)
            LOG.info("round=%s worker=%s recovery=model goal=%s", turn.round, goal[0], goal)
        self.offered = []

    def action(self, goal, turn, cfg, mem, nav, ledger):
        uid, kind, target = goal
        hero = turn.units.get(uid)
        if (not turn.is_day or not hero or hero.kind != "worker" or uid in ledger.used
                or uid == mem.wall_repair_worker
                or mem.movement.avoids(uid, target)):
            return None
        # Recovery never competes with combat or moves through robot ranges.
        if turn.robots:
            return None
        if kind == "collect":
            if (not hero.space or turn.zones.get(target) not in ORES
                    or turn.prices.get(turn.zones.get(target), 0) <= 0
                    or target in mem.collect_failures or distance(hero.pos, target) > 3):
                return None
            action = command("collect", target)
        else:
            from .economy import voucher_for, wall_upgrade_allowed
            building = next((b for b in turn.ours if b.pos == target), None)
            name = voucher_for(building) if building else None
            if (not name or not hero.inventory[name] or building.id in ledger.upgrade_claims
                    or not wall_upgrade_allowed(turn, building, mem)):
                return None
            action = command("use", target, name=name)
        targets = building.cells if kind == "use" else [target]
        home, exact = return_destination(turn, nav, ledger, hero)
        if not home:
            return None
        options = []
        cells = {p for t in targets for p in neighbours(t)} - set(targets)
        for cell in sorted(cells):
            route = nav.search(hero, {cell}, ledger.reserved)
            if route is None:
                continue
            original = turn.blocked
            try:
                turn.blocked = original - {hero.pos}
                proxy = replace(hero, pos=cell)
                back = (nav.search(proxy, home, ledger.reserved) if exact
                        else nav.approach(proxy, home, ledger.reserved))
            finally:
                turn.blocked = original
            if back and route[0] + 1 + back[0] + cfg.return_margin < turn.day_left:
                options.append((route[0], back[0], cell, route))
        if not options:
            return None
        route = min(options)[3]
        return command("move", route[1]) if route[1] is not None else action

    def resume(self, turn, cfg, mem, nav, ledger, excluded):
        for uid, (goal, _) in list(self.active.items()):
            action = None if uid in excluded else self.action(goal, turn, cfg, mem, nav, ledger)
            if action and ledger.add(uid, action):
                if action["action"] != "move":
                    self.active.pop(uid, None)
            else:
                self.active.pop(uid, None)

    def recover(self, turn, cfg, mem, nav, ledger, excluded, loops_only=False):
        if not turn.is_day or turn.phase_task:
            return
        for hero in turn.workers:
            eligible = (hero.id in mem.movement.looped if loops_only
                        else self.idle.get(hero.id, 0) >= 2)
            if hero.id in ledger.used or hero.id in excluded or not eligible:
                continue
            goals = [(hero.id, "use", b.pos) for b in turn.ours]
            goals += [(hero.id, "collect", p) for p in sorted(turn.zones)
                      if distance(hero.pos, p) <= 3]
            options = []
            for goal in goals:
                action = self.action(goal, turn, cfg, mem, nav, ledger)
                if action:
                    options.append((goal, action))
                if len(options) == 3:
                    break
            if not options:
                continue
            goal, action = options[0]
            if ledger.add(hero.id, action):
                if action["action"] == "move":
                    self.active[hero.id] = (goal, turn.round + 8)
                # Always execute a deterministic fallback in the request round.
                if len(options) > 1 and not self.ready:
                    self.ready = [g for g, _ in options]
                LOG.info("round=%s worker=%s recovery=rules goal=%s", turn.round, hero.id, goal)

    def prompt(self, intel):
        turn = intel.turn
        if (not self.ready or turn.phase_task or turn.round < self.next_call
                or not intel.can_call()):
            return ""
        options = [{"choice": i, "worker": g[0], "action": g[1], "target": g[2],
                    "workerPos": turn.units[g[0]].pos,
                    "ore": turn.zones.get(g[2]) if g[1] == "collect" else None,
                    "unitValue": turn.prices.get(turn.zones.get(g[2]), 0)}
                   for i, g in enumerate(self.ready)]
        prompt = ("工人连续空闲或往返。只从候选中选一项；优先使用升级券，其次就近采矿。"
                  "只输出JSON，例如{\"choice\":0}。不要生成坐标或指令。\n"
                  + json.dumps({"dayLeft": turn.day_left, "gold": turn.gold,
                                "options": options}, ensure_ascii=False))
        result = intel.request("recovery", prompt)
        if result:
            self.offered = self.ready[:]
            self.next_call = turn.round + 12
        return result
