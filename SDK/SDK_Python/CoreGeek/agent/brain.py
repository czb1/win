import hashlib
import json
import logging
from collections import OrderedDict
from time import monotonic
from .logging_system import turn_context, update_context, request_context, emit_event
from . import diagnostics
from .config import Config
from .daytime import finish_daytime_work, park_idle_pioneer
from .worker_jobs import resume_daytime_jobs, save_daytime_jobs
from .model import Turn, distance, SUMMON_ORDERS
from .navigation import Navigator, layout, DeadlineExceeded
from .commands import Ledger, command
from .combat import (assignments, fixed_gatling_crew, operator_posts, return_plan, defend, emergency_items,
                     shared_crew, shared_defend, clear_gunner_route, yield_gate_operators,
                     finish_weapon_reports, catch_nearby_imp)
from .projectiles import wall_gates
from .economy import workers, pioneer, walk, vacate_site, use_inventory, finish_preparation, wall_sector, dusk_resources
from .economy import reserve_treasure_gold
from .economy_plan import via
from .intelligence import Memory, Intelligence
from .mining import night_mine
from .wall_watch import select_watch, prepare_watch, repair_watch
from .sabotage import act_imps
from .gate_guard import prepare_gate_guard
from .front_wall import prepare_front
from .robot_assault import RobotAssaultMemory, act_robots, choose_summon_position
from .spending import first_day_boss_phase, plan_day_spending, robot_purchase_plan

LOG = logging.getLogger(__name__)

DAILY_SUMMON_LIMIT = 10


def defenses_maxed(turn, cfg, towers, walls):
    """Report strict observed completion independently of surplus readiness."""
    built_walls = [unit for unit in turn.ours if unit.kind == "wall"]
    return bool(turn.station and built_walls and turn.weapons
                and set(walls) <= {wall.pos for wall in built_walls}
                and len(turn.weapons) >= min(len(towers), len(cfg.loadout), 3)
                and set(towers[:min(len(cfg.loadout), 3)]) <=
                    {tower.pos for tower in turn.weapons}
                and all(wall.level == 3 for wall in built_walls)
                and all(tower.level == 3 for tower in turn.weapons))


def observe_robot_summons(turn, mem):
    """Reserve submitted uses until explicit rejection; never invent success."""
    state = getattr(mem, "robot_summon_state", None)
    if state is None or state["day"] != turn.day:
        state = {"day": turn.day, "count": 0, "positions": set(),
                 "pending": None, "observed_round": -1, "buyer": None}
        mem.robot_summon_state = state
    if state["observed_round"] == turn.round:
        return state
    pending = state["pending"]
    if pending and pending[0] < turn.round:
        previous_round, actor, position = pending
        results = turn.raw.get("lastRoundRoleActionResults") or {}
        if (previous_round == turn.round - 1
                and results.get(str(actor), results.get(actor)) is False):
            state["count"] -= 1
            state["positions"].discard(position)
        state["pending"] = None
    state["observed_round"] = turn.round
    return state


def summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls, excluded=(), reserve=0,
                      item_only=None, shop_only=None, quantity_limit=None):
    """Use carried orders or prepare the strongest affordable surplus order."""
    state = observe_robot_summons(turn, mem)
    if turn.day == 1 and not first_day_boss_phase(turn, cfg):
        ledger.spending_plan['robot_blocked'] = 'first_day_tower_construction'
        return False
    ledger.summon_pending_positions = state["positions"].copy()
    ledger.summon_daily_count = state["count"]
    excluded = set(excluded)
    if turn.phase_task and turn.pioneer:
        excluded.add(turn.pioneer.id)
    free = [hero for hero in turn.heroes
            if hero.id not in ledger.used and hero.id not in excluded]
    carriers = sorted(((-rank, hero.kind != 'worker', hero.id, hero, name)
                       for hero in free for rank, name in enumerate(SUMMON_ORDERS)
                       if hero.inventory[name]), key=lambda row: row[:3])
    can_use = (turn.is_day and state["count"] < DAILY_SUMMON_LIMIT
               and not (state["pending"] and state["pending"][0] == turn.round))
    if carriers and can_use:
        position = choose_summon_position(turn, ledger, nav.deadline)
        ledger.robot_summon_target = position
        if position is not None:
            for _, _, _, hero, name in carriers:
                if ledger.add(hero.id, command("use", position, name=name)):
                    state["count"] += 1
                    state["positions"].add(position)
                    state["pending"] = turn.round, hero.id, position
                    state["buyer"] = None
                    ledger.explain(hero.id, "summon_highest_robot", item=name, summon_position=position,
                                   daily_reserved_uses=state["count"], daily_limit=DAILY_SUMMON_LIMIT)
                    return True
    available = max(0, ledger.gold - reserve)
    names = [name for name in reversed(SUMMON_ORDERS)
             if (item_only is None or name == item_only)
             and (turn.day > 1 or name == 'BossRobotSummonOrder')
             and name in turn.shop and 0 <= turn.shop[name] <= available]
    if not names:
        ledger.spending_plan['robot_blocked'] = 'insufficient_surplus'
        return False
    if any(item in ledger.purchases for item in SUMMON_ORDERS):
        ledger.spending_plan['robot_blocked'] = 'planned_order'
        return False
    buyers = []
    for name in names:
        price = turn.shop[name]
        quantity = 1
        if name == "BossRobotSummonOrder":
            quantity = available // price if price else DAILY_SUMMON_LIMIT
        if quantity_limit is not None:
            quantity = min(quantity, quantity_limit)
        for hero in free:
            plan = robot_purchase_plan(turn, cfg, mem, nav, ledger, hero, name, shop_only, quantity)
            if plan:
                count, shop, route = plan
                buyers.append((hero.id != state.get("buyer"), -count, route[0], hero.kind != "worker",
                               hero.id, hero, route, shop))
        if buyers:
            break
    if not buyers:
        ledger.spending_plan['robot_blocked'] = 'no_free_buyer_or_shop_route'
        return False
    _, negative_count, _, _, _, hero, route, shop = min(buyers)
    quantity = -negative_count
    price = turn.shop[name]
    if route[1] is None:
        accepted = ledger.add(hero.id, command("buy", name=name, num=quantity))
    else:
        accepted = ledger.add(hero.id, command("move", route[1]))
        if accepted:
            ledger.gold -= price * quantity
            ledger.purchases.add(name)
            ledger.remember_work(hero, 'robot_buy', shop, name=name, quantity=quantity)
    if accepted:
        state["buyer"] = hero.id
        ledger.explain(hero.id, "prepare_highest_robot", item=name,
                       price=price, quantity=quantity, total_cost=price * quantity,
                       route_steps=route[0])
        ledger.spending_plan['robot_purchase'] = dict(actor=hero.id, item=name, quantity=quantity,
                                                     unit_price=price, total_cost=price * quantity)
    return accepted


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
        with request_context(data):
            try:
                turn = Turn(data, self.cfg)
            except Exception:
                LOG.exception("invalid turn", extra={"event": "turn_invalid", "category": "protocol", "data": {"input": data}})
                raise
            with turn_context(turn):
                try:
                    return self._decide(data, turn, started)
                except Exception:
                    LOG.exception("decision failed", extra={"event": "decision_failed"})
                    raise

    def _decide(self, data, turn, started):
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = (*turn.key, turn.station.pos if turn.station else None)
        mem = self.sessions.get(key)
        if mem and mem.last_round == turn.round and mem.last_digest == digest:
            update_context(session=mem.log_session, task_id=mem.log_task_id, task_type=mem.log_task_type)
            diagnostics.observe(turn, mem)
            emit_event("cache_hit", {"input_sha256": digest}, "runtime")
            diagnostics.finish(turn, mem, None, mem.last_response, (monotonic() - started) * 1000, cached=True)
            return json.loads(json.dumps(mem.last_response))
        session_reason = "round_rewind" if mem is not None else "new_team_or_base"
        if mem is None or turn.round < mem.last_round:
            mem = Memory()
            self.sessions[key] = mem
        self.sessions.move_to_end(key)
        while len(self.sessions) > 8:
            self.sessions.popitem(last=False)
        update_context(session=mem.log_session, task_id=mem.log_task_id, task_type=mem.log_task_type)
        if not hasattr(mem, "log_history"):
            diagnostics.start_session(mem, self.cfg, session_reason)
        emit_event("turn_started", {"input_sha256": digest}, "runtime")
        diagnostics.observe(turn, mem)
        diagnostics.incoming_payloads(turn, mem)
        previous_day, previous_tick = divmod(mem.last_round - self.cfg.round_origin, 130)
        if (mem.last_round < 0 or previous_day != turn.day - 1
                or (previous_tick < 70) != turn.is_day):
            LOG.info("phase_start round=%s day=%s phase=%s", turn.round, turn.day,
                     "day" if turn.is_day else "night", extra={"event": "phase_start"})
        previous_mines = mem.mine_kinds.copy()
        mem.observe(turn, self.cfg)
        observe_robot_summons(turn, mem)
        diagnostics.task_state(turn, mem)
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
        if getattr(turn, "weapon_layout", None):
            emit_event("weapon_layout", turn.weapon_layout, "decision")
        ledger = Ledger(turn, self.cfg, towers, walls)
        nav = Navigator(turn, started + self.cfg.decision_seconds, mem.movement)
        intel = Intelligence(turn, self.cfg, mem)
        prompt, execute = "", ""
        pairs = []
        shared = None
        gatling_pairs = []
        budget_reached = False
        gate_guard = None
        try:
            # Opening offense gets an action before ordinary job continuation,
            # recall, or voucher budgets can take every available buyer.
            opening_boss = turn.is_day and first_day_boss_phase(turn, self.cfg)
            if opening_boss:
                _, opening_reserve = plan_day_spending(turn, self.cfg, mem, nav, ledger, towers)
                summon_best_robot(turn, self.cfg, mem, nav, ledger, towers, walls,
                                  reserve=opening_reserve)
            prepare_front(turn, self.cfg, mem, nav, ledger, walls)
            gate_guard = prepare_gate_guard(turn, self.cfg, mem, nav, ledger, towers, walls)
            nav.gate_guard = gate_guard
            act_imps(turn, mem.sabotage, nav, ledger)
            if not hasattr(mem, "robot_assault"):
                mem.robot_assault = RobotAssaultMemory()
            act_robots(turn, mem.robot_assault, nav, ledger)
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
            if turn.phase_task or mem.log_task_id:
                emit_event("task_schedule", {"active": bool(turn.phase_task), "within_timeout": within_timeout,
                           "return_steps": task_return, "return_margin": self.cfg.return_margin,
                           "day_left": turn.day_left, "danger": danger, "first_watch": first_watch,
                           "hold_task": hold_task, "stop_reason": mem.stop_reason}, "evolution")
            if not turn.is_day:
                emergency_items(turn, ledger)
            pioneer_gunner = bool(h and turn.weapons and (not turn.is_day or not hold_task))
            mixed = (self.cfg.loadout.count("rocket") == 2 and self.cfg.loadout.count("gatling") == 1
                     and (any(w.kind == "gatling" for w in turn.weapons)
                          or mem.gatling_operator_id is not None))
            if not turn.is_day and pioneer_gunner:
                hold_task = False
                if turn.phase_task and not mem.stop_reason:
                    mem.stop_reason = "defence_threat" if danger else "night_role"
                    LOG.info("round=%s task_stop=%s", turn.round, mem.stop_reason)
            excluded = ({h.id} if h and hold_task else set())
            alive = {hero.id for hero in turn.heroes}
            mem.operator_yields = {uid: state for uid, state in mem.operator_yields.items()
                                  if not turn.is_day and uid in alive and state["until"] >= turn.round}
            unavailable = {uid: {state["gate"]} for uid, state in mem.operator_yields.items()}
            if pioneer_gunner and not mixed:
                excluded.update(worker.id for worker in turn.workers)
            mem.return_targets = {uid: wid for uid, wid in mem.return_targets.items() if uid not in excluded}
            mem.return_posts = {uid: post for uid, post in mem.return_posts.items() if uid not in excluded}
            shared = shared_crew(turn, self.cfg, mem, nav, towers, walls, excluded=excluded)
            if mixed:
                gatling_pairs = fixed_gatling_crew(turn, mem, nav, ledger)
            if shared is not None:
                pairs, posts = shared
                if mixed:
                    # Use a separate worker/post, keeping the pioneer's common
                    # control cell and the other worker available for wall watch.
                    original = turn.blocked
                    try:
                        # Posts are future destinations; friendly congestion
                        # must not swap the two workers' night responsibilities.
                        turn.blocked = original - {h.pos for h in turn.heroes}
                        gatling_posts = operator_posts(turn, nav, gatling_pairs,
                            walls + ([mem.gunner_post] if mem.gunner_post else []), mem.return_posts,
                            unavailable=unavailable, gates=wall_gates(turn, walls),
                            reports=ledger.operator_post_reports)
                    finally:
                        turn.blocked = original
                    if gatling_pairs and not gatling_posts and gatling_pairs[0][0].id in mem.operator_yields:
                        actor = gatling_pairs[0][0]
                        gatling_posts = {actor.id: (actor.pos, 0)}
                    pairs += gatling_pairs
                    posts.update(gatling_posts)
                # Release stale assignments while preserving both active posts.
                crew_ids = {hero.id for hero, _ in pairs}
                mem.return_targets = {uid: wid for uid, wid in mem.return_targets.items()
                                      if uid in crew_ids}
                mem.return_posts = {uid: p for uid, p in mem.return_posts.items()
                                    if uid in crew_ids}
            else:
                if mixed:
                    pairs = assignments(turn, nav, ledger,
                        excluded={worker.id for worker in turn.workers} | excluded,
                        towers=sorted((w for w in turn.weapons if w.kind == "rocket"),
                                      key=lambda w: (bool(w.cooldown), w.id)))
                    pairs += gatling_pairs
                else:
                    pairs = assignments(turn, nav, ledger, excluded=excluded,
                                        fixed=mem.return_targets if turn.is_day else None)
                fixed = ({hero.id: weapon.id for hero, weapon in pairs} if mixed else mem.return_targets)
                pairs, posts = (return_plan(turn, nav, pairs, walls, fixed, mem.return_posts)
                                if turn.is_day else (pairs, operator_posts(turn, nav, pairs, walls,
                                    mem.return_posts, unavailable=unavailable,
                                    reports=ledger.operator_post_reports)))
                if not posts and mem.operator_yields:
                    posts = {hero.id: (hero.pos, 0) for hero, _ in pairs if hero.id in mem.operator_yields}
            if not turn.is_day:
                # Recall from the current position early enough for an approaching
                # wave, but release workers immediately when local danger ends.
                def needs_defence(hero, tower):
                    if hero.kind != "worker":
                        return True
                    route = (nav.search(hero, {posts[hero.id][0]}) if hero.id in posts
                             else nav.approach(hero, [tower.pos]))
                    lead = (route[0] if route else 130) + self.cfg.return_margin
                    return any(turn.threatens_us(r) and (
                        turn.base_distance(r.pos) <= max(self.cfg.task_danger_radius, lead + r.attack_range)
                        or distance(tower.pos, r.pos) <= tower.attack_range + 1)
                        or distance(hero.pos, r.pos) <= r.attack_range + 2 for r in turn.robots)
                pairs = [(hero, tower) for hero, tower in pairs
                         if (shared is not None and tower.kind == "rocket")
                         or hero.id in mem.operator_yields or needs_defence(hero, tower)]
                posts = {uid: p for uid, p in posts.items() if uid in {hero.id for hero, _ in pairs}}
                mem.return_targets.clear()
                mem.return_posts.clear()
            # A released gatling operator goes mining, rather than inheriting
            # the other worker's wall-watch role when the local wave clears.
            watcher = select_watch(turn, mem, pairs + gatling_pairs,
                                   fixed_operator=mem.gatling_operator_id if mixed else None)
            ledger.return_pairs = pairs
            ledger.operator_posts = {uid: p for uid, (p, _) in posts.items()}
            for actor, weapon in pairs:
                current = ledger.operator_post_reports.get((actor.id, weapon.id, actor.pos))
                if current:
                    ledger.weapon_diagnostics[weapon.id] = current.copy()
            if turn.is_day:
                ledger.daytime_gunner_post = mem.gunner_post if shared is not None else None
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
                    ledger.plans[hero.id] = {"reason": "defence_recall", "return_steps": length,
                                            "return_margin": self.cfg.return_margin, "day_left": turn.day_left,
                                            "post": posts.get(hero.id), "tower": tower.id}
                    if hero.id in posts:
                        evaluation = ledger.operator_post_reports.get((hero.id, tower.id, posts[hero.id][0]))
                        if evaluation:
                            ledger.plans[hero.id]["post_targeting"] = evaluation
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
            if h and turn.phase_task and not (not turn.is_day and pioneer_gunner):
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
                    if str(h.id) not in ledger.commands:
                        ledger.explain(h.id, "task_work_pending", pending=mem.pending,
                                       return_steps=task_return, day_left=turn.day_left,
                                       return_margin=self.cfg.return_margin)
                elif not mem.stop_reason:
                    mem.stop_reason = ("defence_threat" if danger else
                                       "first_wave_deadline" if first_watch else "task_deadline")
                    LOG.info("round=%s task_stop=%s", turn.round, mem.stop_reason)
            if not turn.is_day:
                yield_gate_operators(turn, nav, ledger, mem, walls, pairs)
                if shared is not None:
                    corridor = clear_gunner_route(turn, nav, ledger, mem, pairs)
                    shared_defend(turn, nav, ledger, mem, pairs, towers,
                                  siege_radius=self.cfg.task_danger_radius, corridor=corridor or ())
                    ledger.reserved.update(corridor or ())
                else:
                    defend(turn, nav, ledger, pairs, ledger.operator_posts)
                if shared is not None and mem.gunner_post:
                    ledger.reserved.add(mem.gunner_post)
                if not opening_boss:
                    summon_best_robot(turn, self.cfg, mem, nav, ledger, towers, walls)
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
                            night_mine(turn, self.cfg, mem, nav, ledger, hero, dedicated=pioneer_gunner or shared is not None)
                        elif turn.station:
                            walk(nav, ledger, hero, turn.station.cells)
            else:
                # Reserve the watcher's repair budget before other dusk buyers
                # spend it. The watcher may sell its own ore first.
                watch_locked, watch_gold = prepare_watch(turn, self.cfg, mem, nav, ledger, watcher, walls)
                if watch_locked:
                    returning.add(watcher.id)
                ledger.gold -= min(watch_gold, ledger.gold)
                resume_daytime_jobs(turn, self.cfg, mem, nav, ledger, towers, walls, returning)
                if not opening_boss:
                    _, priority_gold = plan_day_spending(
                        turn, self.cfg, mem, nav, ledger, towers, returning)
                    summon_best_robot(turn, self.cfg, mem, nav, ledger, towers, walls,
                                      reserve=priority_gold)
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
                                    if turn.day > 1 and h and h.id not in ledger.used
                                    and h.id not in returning and not danger else 0)
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
                        pioneer(turn, self.cfg, mem, nav, ledger, h, shopping=turn.day > 1)
                        if h.id not in ledger.used:
                            park_idle_pioneer(turn, self.cfg, mem, nav, ledger, h)
                finish_daytime_work(turn, self.cfg, mem, nav, ledger, returning)
                emit_event('spending_plan', dict(ledger.spending_plan,
                           gold_after_reservations=ledger.gold,
                           purchased_items=sorted(ledger.purchases), supply_reports=ledger.supply_reports), 'economy')
                if not prompt and not execute:
                    prompt = intel.news()
                    if not prompt:
                        prompt = mem.recovery.prompt(intel)
        except DeadlineExceeded:
            budget_reached = True
            emit_event("budget_reached", {"validated_actions": len(ledger.commands)}, "runtime", level=logging.WARNING)
            LOG.warning("round=%s budget reached; returning %s validated actions", turn.round, len(ledger.commands))
        # Workers plan first. Their failed gate routes and accepted movements
        # decide whether the imp holds, yields, or returns to the opening.
        nav.gate_guard = None
        if gate_guard is not None:
            gate_guard.finish()
        catch_nearby_imp(turn, mem, ledger)
        save_daytime_jobs(turn, mem, ledger)
        response = ledger.response(prompt, execute)
        finish_weapon_reports(turn, ledger)
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
            diagnostic_pairs = ([(hero, w) for hero, tower in pairs for w in turn.weapons
                                 if (w.kind == "rocket" and hero.id == mem.gunner_id) or w.id == tower.id]
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
        diagnostics.finish(turn, mem, ledger, response, (monotonic() - started) * 1000, budget_reached, navigation=nav)
        if turn.is_day:
            for worker in turn.workers:
                LOG.debug("round=%s worker=%s position=%s free_space=%s mining_target=%s command=%s",
                          turn.round, worker.id, worker.pos, worker.space, mem.mine_targets.get(worker.id),
                          response["roleCommandMap"].get(str(worker.id)))
        LOG.debug("round=%s day=%s phase=%s commands=%s latency_ms=%.2f", turn.round, turn.day,
                 "day" if turn.is_day else "night", len(ledger.commands), (monotonic()-started)*1000)
        return json.loads(json.dumps(response))
