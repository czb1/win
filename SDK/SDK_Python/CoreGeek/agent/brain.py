import hashlib
import json
import logging
from collections import OrderedDict
from time import monotonic
from .config import Config
from .model import Turn, distance
from .navigation import Navigator, layout, DeadlineExceeded
from .commands import Ledger
from .combat import assignments, return_plan, defend, emergency_items, shared_crew, shared_defend, clear_gunner_route
from .combat import block_enemy_controls, block_enemy_workers
from .economy import workers, pioneer, walk, vacate_site, use_inventory, finish_preparation, wall_sector, dusk_resources
from .economy import reserve_treasure_gold
from .intelligence import Memory, Intelligence
from .mining import night_mine
from .wall_watch import select_watch, prepare_watch, repair_watch

LOG = logging.getLogger(__name__)


def battle_diagnostics(turn, mem, pairs, response):
    """One compact line per night/transition with actual actions and obstacles."""
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
            reason = ("operator_en_route" if commands.get(str(hero.id), {}).get("action") == "move"
                      else "operator_waiting")
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
                      for uid, action in mem.last_commands.items() if action.get("action") == "attack"]
    walls = [{"pos": wall.pos, "sector": wall_sector(turn, wall), "level": wall.level,
              "health": wall.health, "hits": mem.wall_hits.get(wall.pos, 0)}
             for wall in turn.ours if wall.kind == "wall"]
    LOG.info("round=%s battle_state=%s", turn.round, json.dumps({
        "baseHealth": turn.station.health, "heroes": len(turn.heroes), "gold": turn.gold,
        "hostileTotal": len(hostile), "hostileWithin6": len(near),
        "hostileNear": [{"id": r.id, "kind": r.kind, "pos": r.pos, "health": r.health}
                        for r in near[:12]], "walls": walls, "towers": towers,
        "previousShots": previous_shots,
        "wallWatch": mem.wall_watch_id,
        "gunner": {"id": mem.gunner_id, "post": mem.gunner_post, "stalled": mem.gunner_stalled},
        "crew": [{"id": h.id, "pos": h.pos, "action": commands.get(str(h.id))}
                 for h in turn.heroes]}, ensure_ascii=False, separators=(",", ":")))


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
        mem.observe(turn, self.cfg)
        if mem.last_round < 0 or previous_mines != mem.mine_kinds:
            LOG.info("round=%s source=mapInfo.zones mines_received=%s mines=%s", turn.round,
                     len(mem.mine_kinds), json.dumps(
                         [{"type": kind, "pos": list(p)} for p, kind in sorted(mem.mine_kinds.items())],
                         ensure_ascii=False))
        results = turn.raw.get("lastRoundRoleActionResults") or {}
        if mem.last_round == turn.round - 1:
            for uid, action in mem.last_commands.items():
                if action["action"] == "build" or (action["action"] in ("buy", "use")
                                                   and "UpgradeVoucher" in action.get("name", "")):
                    LOG.info("round=%s previous_defence_result=%s", turn.round,
                             json.dumps({"actor": uid, "action": action,
                                         "result": results.get(uid, results.get(int(uid)))}, ensure_ascii=False))
        if turn.station and mem.station_health is not None and turn.station.health < mem.station_health:
            LOG.warning("round=%s base_damage=%s health=%s", turn.round,
                        mem.station_health - turn.station.health, turn.station.health)
        mem.station_health = turn.station.health if turn.station else None
        towers, walls = layout(turn, self.cfg)
        ledger = Ledger(turn, self.cfg, towers, walls)
        nav = Navigator(turn, started + self.cfg.decision_seconds, mem.movement)
        intel = Intelligence(turn, self.cfg, mem)
        prompt, execute = "", ""
        pairs = []
        shared = None
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
            early_gunner = bool(h and turn.day <= 2 and turn.weapons
                                and (not turn.is_day or not hold_task))
            night_raid = bool(h and turn.day >= 3 and not turn.is_day)
            if not turn.is_day and (early_gunner or night_raid):
                hold_task = False
                if turn.phase_task and not mem.stop_reason:
                    mem.stop_reason = "defence_threat" if danger else "night_role"
                    LOG.info("round=%s task_stop=%s", turn.round, mem.stop_reason)
            excluded = ({h.id} if h and (hold_task or turn.day >= 3) else set())
            if early_gunner:
                excluded.update(worker.id for worker in turn.workers)
            mem.return_targets = {uid: wid for uid, wid in mem.return_targets.items() if uid not in excluded}
            mem.return_posts = {uid: post for uid, post in mem.return_posts.items() if uid not in excluded}
            shared = shared_crew(turn, self.cfg, mem, nav, towers, walls, excluded=excluded)
            if shared is not None:
                pairs, posts = shared
                # Previous multi-operator assignments must not recall the miner.
                mem.return_targets = {uid: wid for uid, wid in mem.return_targets.items()
                                      if uid == mem.gunner_id}
                mem.return_posts = {uid: p for uid, p in mem.return_posts.items()
                                    if uid == mem.gunner_id}
            else:
                pairs = assignments(turn, nav, ledger, excluded=excluded,
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
                if shared is None:
                    pairs = [(hero, tower) for hero, tower in pairs if needs_defence(hero, tower)]
                mem.return_targets.clear()
                mem.return_posts.clear()
            watcher = select_watch(turn, mem, pairs)
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
            if h and turn.phase_task and not (not turn.is_day and (early_gunner or night_raid)):
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
                if shared is not None:
                    corridor = clear_gunner_route(turn, nav, ledger, mem, pairs)
                    shared_defend(turn, nav, ledger, mem, pairs, towers,
                                  siege_radius=self.cfg.task_danger_radius)
                    ledger.reserved.update(corridor or ())
                else:
                    defend(turn, nav, ledger, pairs)
                if shared is not None and mem.gunner_post:
                    ledger.reserved.add(mem.gunner_post)
                for hero in turn.heroes:
                    if watcher and hero.id == watcher.id and hero.id not in ledger.used:
                        if hero.health <= 165 and hero.inventory["Medicine"]:
                            use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem)
                        else:
                            repair_watch(turn, mem, nav, ledger, hero, walls)
                        continue
                    if hero.id not in ledger.used and use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem):
                        continue
                    if hero.id not in ledger.used and hero.id not in {h.id for h, _ in pairs}:
                        if shared is not None and hero.pos == mem.gunner_post:
                            vacate_site(turn, nav, ledger, hero, towers + walls + [mem.gunner_post])
                        if hero.id in ledger.used:
                            continue
                        if hero.kind == "worker":
                            night_mine(turn, self.cfg, mem, nav, ledger, hero, dedicated=early_gunner or shared is not None)
                        elif block_enemy_controls(turn, self.cfg, nav, ledger, hero):
                            continue
                        elif night_raid:
                            block_enemy_workers(turn, nav, ledger, hero)
                        elif turn.station:
                            walk(nav, ledger, hero, turn.station.cells)
            else:
                # Reserve the watcher's repair budget before other dusk buyers
                # spend it. The watcher may sell its own ore first.
                watch_locked, watch_gold = prepare_watch(turn, self.cfg, mem, nav, ledger, watcher, walls)
                if watch_locked:
                    returning.add(watcher.id)
                ledger.gold -= min(watch_gold, ledger.gold)
                dusk_resources(turn, self.cfg, mem, nav, ledger, towers)
                for hero, tower in pairs:
                    if hero.id in returning and hero.id not in ledger.used:
                        finish_preparation(turn, self.cfg, mem, nav, ledger, hero, tower, walls)
                corridor = (clear_gunner_route(turn, nav, ledger, mem, pairs)
                            if shared is not None and returning else None)
                defend(turn, nav, ledger, [(h, w) for h, w in pairs if h.id in returning], ledger.operator_posts)
                ledger.reserved.update(corridor or ())
                if not turn.phase_task:
                    mem.recovery.resume(turn, self.cfg, mem, nav, ledger, returning)
                    mem.recovery.recover(turn, self.cfg, mem, nav, ledger, returning, loops_only=True)
                treasure_reserve = (reserve_treasure_gold(turn, self.cfg, mem, nav, ledger, h)
                                    if h and h.id not in ledger.used and h.id not in returning and not danger else 0)
                ledger.gold -= treasure_reserve
                try:
                    workers(turn, self.cfg, mem, nav, ledger, towers, walls, returning)
                finally:
                    ledger.gold += treasure_reserve
                mem.recovery.recover(turn, self.cfg, mem, nav, ledger, returning)
                if shared is not None and mem.gunner_post:
                    for idle in turn.workers:
                        if idle.id != mem.gunner_id and idle.id not in ledger.used:
                            vacate_site(turn, nav, ledger, idle, towers + walls + [mem.gunner_post])
                if h and h.id not in ledger.used and h.id not in returning:
                    if turn.phase_task:
                        if not hold_task and turn.station:
                            walk(nav, ledger, h, turn.station.cells)
                    else:
                        pioneer(turn, self.cfg, mem, nav, ledger, h)
                        if h.id not in ledger.used:
                            block_enemy_controls(turn, self.cfg, nav, ledger, h)
                        if h.id not in ledger.used:
                            vacate_site(turn, nav, ledger, h, towers + walls +
                                        ([mem.gunner_post] if shared is not None and mem.gunner_post else []))
                if not prompt and not execute:
                    prompt = intel.news()
                    if not prompt:
                        prompt = mem.recovery.prompt(intel)
        except DeadlineExceeded:
            LOG.warning("round=%s budget reached; returning %s validated actions", turn.round, len(ledger.commands))
        response = ledger.response(prompt, execute)
        if mem.news or mem.treasure:
            hero = turn.pioneer
            reason = ("no_pioneer" if not hero else "night" if not turn.is_day
                      else "active_task" if turn.phase_task else "returning" if hero.id in mem.return_targets
                      else "available")
            mem.trace_treasure(turn, "treasure_schedule", dedupe=True, reason=reason)
            if not turn.is_day or turn.phase_task:
                mem.trace_treasure(turn, "news_gate", dedupe=True,
                                   reason="night" if not turn.is_day else "active_task")
            action = response["roleCommandMap"].get(str(hero.id)) if hero else None
            if mem.treasure and action and action.get("action") in ("buy", "summonTreasure", "acceptTask"):
                mem.trace_treasure(turn, "treasure_actor_action", actor=hero.id,
                                   position=hero.pos, action=action)
        mem.wall_watch.finish(turn, mem, response)
        if not turn.is_day or turn.tick in (0, 69):
            diagnostic_pairs = ([(hero, w) for hero, _ in pairs for w in turn.weapons]
                                if shared is not None else pairs)
            battle_diagnostics(turn, mem, diagnostic_pairs, response)
        for uid, action in response["roleCommandMap"].items():
            if action["action"] == "build" or (action["action"] in ("buy", "use")
                                                and ("UpgradeVoucher" in action.get("name", "")
                                                     or action.get("name") in ("WallFixer", "Bomb", "DizzyWeapon"))):
                LOG.info("round=%s defence_action=%s", turn.round,
                         json.dumps({"actor": uid, **action}, ensure_ascii=False))
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
