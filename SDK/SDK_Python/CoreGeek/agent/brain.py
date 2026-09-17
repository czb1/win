import hashlib
import json
import logging
from collections import Counter, OrderedDict
from time import monotonic
from .config import Config
from .model import Turn, distance, ORES
from .navigation import Navigator, layout, DeadlineExceeded
from .commands import Ledger
from .combat import assignments, return_plan, defend, emergency_items
from .economy import (workers, pioneer, walk, vacate_site, use_inventory, finish_preparation,
                      upgrade_order, voucher_for)
from .economy_plan import via
from .intelligence import Memory, Intelligence
from .mining import night_mine

LOG = logging.getLogger(__name__)


def battle_diagnostics(turn, mem, pairs, response):
    """One bounded summary at transitions, intervals, damage, or shot failure."""
    if not turn.station:
        return
    commands = response["roleCommandMap"]
    crew = {tower.id: hero for hero, tower in pairs}
    hostile = [r for r in turn.robots if turn.threatens_us(r)]
    near = [r for r in hostile if turn.base_distance(r.pos) <= 6]
    towers = []
    for tower in turn.weapons:
        hero = crew.get(tower.id)
        action = commands.get(str(tower.id))
        if action and action["action"] == "attack":
            reason = "fired"
        elif tower.cooldown:
            reason = "cooldown"
        elif hero is None:
            reason = "no_operator"
        elif distance(hero.pos, tower.pos) > 1:
            reason = "operator_en_route"
        elif commands.get(str(hero.id)):
            reason = "operator_other_action"
        elif tower.attack_range <= 0:
            reason = "unknown_range"
        elif not any(distance(tower.pos, r.pos) <= tower.attack_range for r in hostile):
            reason = "out_of_range"
        else:
            reason = "no_safe_target_or_command"
        towers.append({"id": tower.id, "pos": tower.pos, "kind": tower.kind,
                       "level": tower.level, "attackPower": tower.power,
                       "attackRange": tower.attack_range, "cooldown": tower.cooldown,
                       "operator": hero.id if hero else None,
                       "operatorPos": hero.pos if hero else None, "reason": reason,
                       "targetPos": action.get("targetPos") if action else None})
    results = turn.raw.get("lastRoundRoleActionResults") or {}
    previous_shots = [{"tower": uid, "result": results.get(uid, results.get(int(uid)))}
                      for uid, action in mem.last_commands.items()
                      if action.get("action") == "attack"
                      and results.get(uid, results.get(int(uid))) is not True]
    walls = [wall for wall in turn.ours if wall.kind == "wall"]
    levels = Counter(wall.level for wall in walls)
    LOG.info("round=%s battle_state=%s", turn.round, json.dumps({
        "baseHealth": turn.station.health, "heroes": len(turn.heroes), "gold": turn.gold,
        "hostileTotal": len(hostile), "hostileWithin6": len(near),
        "hostileNear": [{"id": r.id, "kind": r.kind, "pos": r.pos, "health": r.health}
                        for r in near[:4]],
        "walls": {"count": len(walls), "damaged": sum(w.health < 1000 for w in walls),
                  "levels": dict(sorted(levels.items()))}, "towers": towers,
        "previousShots": previous_shots}, ensure_ascii=False, separators=(",", ":")))


class Agent:
    """A long-lived process serves isolated team memories. HTTP serializes decide()."""
    def __init__(self, config=None):
        self.cfg = config or Config()
        self.sessions = OrderedDict()

    def decide(self, data):
        started = monotonic()
        turn = Turn(data, self.cfg)
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = (*turn.key, turn.station.pos if turn.station else None)
        if not turn.station:
            previous = next(((k, m) for k, m in reversed(self.sessions.items())
                             if k[:2] == turn.key and m.last_round == turn.round - 1
                             and m.station_health is not None), None)
            if previous:
                key = previous[0]
        mem = self.sessions.get(key)
        if mem and mem.last_round == turn.round and mem.last_digest == digest:
            return json.loads(json.dumps(mem.last_response))
        if mem is None or turn.round < mem.last_round:
            mem = Memory()
            self.sessions[key] = mem
        self.sessions.move_to_end(key)
        while len(self.sessions) > 8:
            self.sessions.popitem(last=False)
        previous_mines = mem.mine_kinds.copy()
        previous_walls = mem.wall_health.copy()
        previous_heroes = mem.hero_count
        mem.observe(turn, self.cfg)
        if mem.last_round < 0:
            LOG.info("round=%s mine_map=%s", turn.round, json.dumps({
                "count": len(mem.mine_kinds),
                "byType": dict(sorted(Counter(mem.mine_kinds.values()).items())),
                "mines": [{"type": kind, "pos": list(p)}
                          for p, kind in sorted(mem.mine_kinds.items())]},
                ensure_ascii=False, separators=(",", ":")))
        elif previous_mines != mem.mine_kinds:
            added = [{"type": kind, "pos": list(p)} for p, kind in sorted(mem.mine_kinds.items())
                     if p not in previous_mines]
            removed = [{"type": kind, "pos": list(p)} for p, kind in sorted(previous_mines.items())
                       if p not in mem.mine_kinds]
            changed = [{"pos": list(p), "from": previous_mines[p], "to": mem.mine_kinds[p]}
                       for p in sorted(previous_mines.keys() & mem.mine_kinds.keys())
                       if previous_mines[p] != mem.mine_kinds[p]]
            LOG.info("round=%s mine_map_delta=%s", turn.round, json.dumps({
                "count": len(mem.mine_kinds), "added": added, "removed": removed, "changed": changed},
                ensure_ascii=False, separators=(",", ":")))
        results = turn.raw.get("lastRoundRoleActionResults") or {}
        if mem.last_round == turn.round - 1:
            for uid, action in mem.last_commands.items():
                important_item = (action["action"] in ("buy", "use") and (
                    "UpgradeVoucher" in action.get("name", "")
                    or action.get("name") in ("WallFixer", "Bomb", "DizzyWeapon")))
                if action["action"] == "build" or important_item:
                    result = results.get(uid, results.get(int(uid)))
                    target = action.get("targetPos", [None])[0]
                    record = {"actor": uid, "action": action["action"],
                              "name": action.get("name"), "target": target, "result": result}
                    log = LOG.warning if result is False else LOG.info
                    log("round=%s defence_result=%s", turn.round,
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        if turn.station and mem.station_health is not None and turn.station.health < mem.station_health:
            LOG.warning("round=%s base_damage=%s health=%s", turn.round,
                        mem.station_health - turn.station.health, turn.station.health)
        if not turn.station and mem.station_health is not None:
            team = turn.raw.get("teamOur", {})
            LOG.warning("round=%s match_end=%s", turn.round, json.dumps({
                "result": "eliminated", "lastBaseHealth": mem.station_health,
                "gold": turn.gold, "score": team.get("score", team.get("scoreNum")),
                "heroes": len(turn.heroes)}, ensure_ascii=False, separators=(",", ":")))
        mem.station_health = turn.station.health if turn.station else None
        if previous_heroes is not None and previous_heroes != len(turn.heroes):
            LOG.warning("round=%s hero_delta=%s", turn.round, json.dumps({
                "before": previous_heroes, "after": len(turn.heroes)}, separators=(",", ":")))
        mem.hero_count = len(turn.heroes)
        if previous_walls != mem.wall_health and mem.last_round >= 0:
            added = [list(p) for p in sorted(mem.wall_health.keys() - previous_walls.keys())]
            removed = [list(p) for p in sorted(previous_walls.keys() - mem.wall_health.keys())]
            damaged = [{"pos": list(p), "from": previous_walls[p][1], "to": mem.wall_health[p][1]}
                       for p in sorted(previous_walls.keys() & mem.wall_health.keys())
                       if mem.wall_health[p][1] < previous_walls[p][1]]
            LOG.info("round=%s wall_delta=%s", turn.round, json.dumps({
                "count": len(mem.wall_health), "added": added[:8], "removed": removed[:8],
                "damaged": damaged[:8]}, ensure_ascii=False, separators=(",", ":")))
        towers, walls = layout(turn, self.cfg)
        ledger = Ledger(turn, self.cfg, towers, walls)
        nav = Navigator(turn, started + self.cfg.decision_seconds, mem.movement)
        intel = Intelligence(turn, self.cfg, mem)
        prompt, execute = "", ""
        pairs = []
        try:
            h = turn.pioneer
            within_timeout = bool(h and turn.phase_task and turn.round - mem.task_started <
                                  min(mem.task_timeout, self.cfg.task_max_rounds))
            home = [w.cells for w in turn.weapons] or ([turn.station.cells] if turn.station else [])
            task_return = min((route[0] for cells in home
                               if (route := nav.approach(h, cells)) is not None), default=130) if within_timeout else 0
            danger = bool(h and any(turn.threatens_us(r) and (
                turn.base_distance(r.pos) <= max(self.cfg.task_danger_radius,
                                                 task_return + self.cfg.return_margin + r.attack_range)
                or distance(h.pos, r.pos) <= max(self.cfg.task_danger_radius, r.attack_range + 2))
                for r in turn.robots))
            # First-wave readiness has a hard return deadline. On later nights
            # a task may continue while no wave threatens the pioneer or base.
            first_watch = (turn.day == 1 and bool(home)
                           and turn.day_left <= task_return + self.cfg.return_margin)
            hold_task = within_timeout and not danger and not first_watch and not mem.stop_reason
            if not turn.is_day:
                emergency_items(turn, ledger)
            pairs = assignments(turn, nav, ledger, excluded={h.id} if hold_task else (),
                                fixed=mem.return_targets if turn.is_day else None)
            pairs, posts = (return_plan(turn, nav, pairs, walls, mem.return_targets, mem.return_posts)
                            if turn.is_day else (pairs, {}))
            if not turn.is_day:
                # Recall from the current position early enough for an approaching
                # wave, but release workers immediately when local danger ends.
                def needs_defence(hero, tower):
                    if hero.kind != "worker":
                        return True
                    route = nav.approach(hero, [tower.pos])
                    lead = (route[0] if route else 130) + self.cfg.return_margin
                    return any(turn.threatens_us(r) and (
                        turn.base_distance(r.pos) <= max(self.cfg.task_danger_radius, lead + r.attack_range)
                        or distance(tower.pos, r.pos) <= tower.attack_range + 1)
                        or distance(hero.pos, r.pos) <= r.attack_range + 2 for r in turn.robots)
                pairs = [(hero, tower) for hero, tower in pairs if needs_defence(hero, tower)]
                mem.return_targets.clear()
                mem.return_posts.clear()
            ledger.return_pairs = pairs
            ledger.operator_posts = {uid: p for uid, (p, _) in posts.items()}
            returning = set()
            for hero, tower in pairs:
                route = (nav.search(hero, {posts[hero.id][0]}, ledger.reserved) if hero.id in posts
                         else nav.approach(hero, [tower.pos], ledger.reserved))
                # Include today's congestion, not only the route with teammates
                # removed. A blocked post needs time for its gatekeeper to yield.
                if route is None and hero.id not in posts:
                    continue
                length = route[0] if route else posts[hero.id][1] + self.cfg.return_margin
                if hero.id in mem.return_targets or not turn.is_day or turn.day_left <= length + self.cfg.return_margin:
                    sellable = [kind for kind in ORES
                                if hero.inventory[kind] and turn.prices.get(kind, 0) > 0]
                    vendors = [p for p, kind in turn.zones.items() if kind == "vendor"]
                    post = {posts[hero.id][0]} if hero.id in posts else set(tower.cells)
                    liquidation = (via(nav, hero, [vendors, post], ledger.reserved,
                                       final_exact=hero.id in posts) if sellable and vendors else None)
                    # preparation_start normally begins this trip well before
                    # recall.  This guard closes the one-round ordering gap in
                    # which recall used to strand a small profitable load.
                    if (turn.is_day and hero.kind == "worker" and liquidation is not None
                            and turn.day_left > liquidation + len(sellable) + self.cfg.return_margin):
                        mem.sale_workers.add(hero.id)
                        continue
                    returning.add(hero.id)
                    mem.return_targets[hero.id] = tower.id
                    if hero.id in posts:
                        mem.return_posts[hero.id] = posts[hero.id][0]
                    LOG.debug("round=%s worker_or_pioneer=%s return_to_tower=%s steps=%s day_left=%s",
                              turn.round, hero.id, tower.id, length, turn.day_left)
            # A nearby worker can block a distant operator's only entrance long
            # before its own return deadline. Recall that helper now, so it can
            # move to its assigned post or yield instead of idling in the gate.
            if turn.is_day and posts and returning:
                for hero, _ in pairs:
                    if hero.id not in returning or nav.search(hero, {posts[hero.id][0]}) is not None:
                        continue
                    helpers = [(h, w) for h, w in pairs if h.id not in returning]
                    original = turn.blocked
                    try:
                        turn.blocked = original - {h.pos for h, _ in helpers}
                        can_clear = nav.search(hero, {posts[hero.id][0]}) is not None
                    finally:
                        turn.blocked = original
                    if can_clear:
                        for helper, weapon in helpers:
                            returning.add(helper.id)
                            mem.return_targets[helper.id] = weapon.id
                            mem.return_posts[helper.id] = posts[helper.id][0]
            if h and turn.phase_task:
                # Submit a ready answer before a return movement can cancel it.
                # LLM/sandbox work holds the pioneer at the task point and gets
                # its own chance before expensive worker connectivity searches.
                if within_timeout and (mem.answer is not None or hold_task):
                    available = min(mem.task_timeout, self.cfg.task_max_rounds) - (turn.round - mem.task_started)
                    if turn.day == 1 and home:
                        available = min(available, max(0, turn.day_left - task_return - self.cfg.return_margin))
                    prompt, execute = intel.task(ledger, available_rounds=available)
                if hold_task:
                    ledger.used.add(h.id)
                elif not mem.stop_reason:
                    mem.stop_reason = ("defence_threat" if danger else
                                       "first_wave_deadline" if first_watch else "task_deadline")
                    LOG.info("round=%s task_stop=%s", turn.round, mem.stop_reason)
            if not turn.is_day:
                defend(turn, nav, ledger, pairs)
                for hero in turn.heroes:
                    if hero.id not in ledger.used and use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem):
                        continue
                    if hero.id not in ledger.used and hero.id not in {h.id for h, _ in pairs}:
                        if hero.kind == "worker":
                            night_mine(turn, self.cfg, mem, nav, ledger, hero)
                        elif turn.station:
                            walk(nav, ledger, hero, turn.station.cells)
            else:
                for hero, tower in pairs:
                    if hero.id in returning and hero.id not in ledger.used:
                        finish_preparation(turn, self.cfg, mem, nav, ledger, hero, tower, walls)
                defend(turn, nav, ledger, [(h, w) for h, w in pairs if h.id in returning], ledger.operator_posts)
                workers(turn, self.cfg, mem, nav, ledger, towers, walls, returning)
                if h and h.id not in ledger.used and h.id not in returning:
                    if turn.phase_task:
                        if not hold_task and turn.station:
                            walk(nav, ledger, h, turn.station.cells)
                    else:
                        pioneer(turn, self.cfg, mem, nav, ledger, h)
                        if h.id not in ledger.used:
                            vacate_site(turn, nav, ledger, h, towers + walls)
                if not prompt and not execute:
                    prompt = intel.news()
        except DeadlineExceeded:
            LOG.warning("round=%s budget reached; returning %s validated actions", turn.round, len(ledger.commands))
        response = ledger.response(prompt, execute)
        if turn.tick == 69 and turn.station:
            inventory = Counter(item for hero in turn.heroes for item in hero.backpack)
            candidates = []
            for building in turn.ours:
                name = voucher_for(building)
                if name:
                    candidates.append((upgrade_order(turn, building, mem), name))
            next_upgrade = min(candidates, default=(None, None), key=lambda item: item[0])[1]
            price = turn.shop.get(next_upgrade) if next_upgrade else None
            blocked = ("unavailable" if next_upgrade and price is None else
                       "insufficient_gold" if price is not None and price > turn.gold else None)
            LOG.info("round=%s day_summary=%s", turn.round, json.dumps({
                "day": turn.day, "baseHealth": turn.station.health, "stationLevel": turn.station.level,
                "gold": turn.gold, "inventory": dict(sorted(inventory.items())),
                "towerLevels": [w.level for w in turn.weapons],
                "walls": len(mem.wall_health),
                "damagedWalls": sum(health < 1000 for _, health in mem.wall_health.values()),
                "nextUpgrade": next_upgrade, "nextPrice": price, "upgradeBlocked": blocked},
                ensure_ascii=False, separators=(",", ":")))
        previous_shot_failed = any(
            action.get("action") == "attack"
            and results.get(uid, results.get(int(uid))) is not True
            for uid, action in mem.last_commands.items())
        battle_due = (turn.tick in (0, 69, 70)
                      or not turn.is_day and ((turn.tick - 70) % 10 == 0
                                              or previous_shot_failed
                                              or mem.last_round < 0))
        if battle_due:
            battle_diagnostics(turn, mem, pairs, response)
        for uid, action in response["roleCommandMap"].items():
            if action["action"] == "build" or (action["action"] in ("buy", "use")
                                                and ("UpgradeVoucher" in action.get("name", "")
                                                     or action.get("name") in ("WallFixer", "Bomb", "DizzyWeapon"))):
                LOG.debug("round=%s defence_action=%s", turn.round,
                          json.dumps({"actor": uid, **action}, ensure_ascii=False,
                                     separators=(",", ":")))
        mem.last_round, mem.last_digest, mem.last_response = turn.round, digest, response
        mem.last_commands = response["roleCommandMap"]
        if turn.is_day:
            for worker in turn.workers:
                LOG.debug("round=%s worker=%s position=%s free_space=%s mining_target=%s command=%s",
                          turn.round, worker.id, worker.pos, worker.space, mem.mine_targets.get(worker.id),
                          response["roleCommandMap"].get(str(worker.id)))
        LOG.debug("round=%s day=%s phase=%s commands=%s latency_ms=%.2f", turn.round, turn.day,
                 "day" if turn.is_day else "night", len(ledger.commands), (monotonic()-started)*1000)
        return json.loads(json.dumps(response))
