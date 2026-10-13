"""Isolated first-night robot screen experiment; geometry is not engine proof."""
from .commands import command
from .model import distance, neighbours, SUMMON_ORDERS
from .navigation import check_time
from .projectiles import line_cells
from .spending import robot_purchase_plan
from .mining import earn
from .economy import pioneer, walk
from .logging_system import emit_event
from .sabotage import act_imps
from .robot_assault import RobotAssaultMemory, act_robots, choose_summon_position

ORDER = 'SmallRobotSummonOrder'
VOUCHER = 'StationUpgradeVoucher1'
LIMIT = 8


def screen_cells(turn):
    return {p for cell in turn.station.cells for p in neighbours(cell)
            if turn.inside(p) and p not in turn.station.cells}


def ray_cells(origin, target, base_cells):
    """Count positive-length ray intersections, not unverified corner touches.

    Stop at the first base cell: shooting a rear base cell cannot bypass the
    front footprint. Excluding corner-only contacts deliberately underclaims
    protection relative to the assault module's supercover model.
    """
    result = set()
    for cell in line_cells(origin, target):
        if cell in base_cells:
            break
        lo, hi = 0.0, 1.0
        for axis in (0, 1):
            delta = target[axis] - origin[axis]
            if delta:
                a = (cell[axis] - .5 - origin[axis]) / delta
                b = (cell[axis] + .5 - origin[axis]) / delta
                lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
            elif abs(cell[axis] - origin[axis]) > .5:
                hi = -1
        if hi - lo > 1e-9:
            result.add(cell)
    return result


def threat_rays(turn):
    rays = []
    for enemy in turn.robots:
        if enemy.id in turn.summon_robot_ids or turn.base_distance(enemy.pos) > 10:
            continue
        # Do not infer ownership/safety from targetTeam. Nearby public robots
        # can be controlled opponents, and can change their next destination.
        for origin in [enemy.pos] + sorted(p for p in neighbours(enemy.pos)
                if turn.inside(p) and p not in turn.blocked):
            for target in sorted(turn.station.cells):
                gap = distance(origin, target)
                immediate = origin == enemy.pos and gap <= enemy.attack_range
                weight = max(1, enemy.power) / (1 + max(0, gap - enemy.attack_range))
                if origin != enemy.pos:
                    weight *= .35
                rays.append((enemy.id, ray_cells(origin, target, turn.station.cells),
                             weight, immediate))
    return rays


def defend_screen(turn, mem, nav, ledger):
    if not turn.station or turn.is_day:
        return
    # The home screen is exclusively the cheap small-robot layer. Larger
    # summoned robots remain assault units and must never be recalled home.
    robots = sorted((r for r in turn.summon_robots if r.kind == 'smallRobot'),
                    key=lambda r: (turn.base_distance(r.pos), r.id))
    rays = threat_rays(turn)
    actual = {r.pos for r in robots}
    goals = screen_cells(turn)
    assigned = set()
    for robot in robots:
        check_time(nav.deadline)
        others = actual - {robot.pos}
        # Never surrender a currently protected shot on the assumption another
        # robot's same-turn movement succeeds or resolves before enemy attack.
        critical = [cells for _, cells, _, immediate in rays
                    if immediate and robot.pos in cells and not cells & others]
        uncovered = [(cells, weight) for _, cells, weight, _ in rays if not cells & others]
        def coverage(p):
            return sum(weight for cells, weight in uncovered if p in cells)
        choices = []
        for goal in sorted(goals | {robot.pos}):
            if goal in assigned or (goal in turn.blocked and goal != robot.pos):
                continue
            route = nav.search(robot, {goal}, ledger.reserved)
            if route is None:
                continue
            steps, step = route[:2]
            next_pos = step if step is not None else robot.pos
            if critical and next_pos != robot.pos:
                continue
            # Current position gets a tie preference; no periodic patrol.
            spread = min((distance(goal, p) for p in assigned | others), default=3)
            score = (coverage(next_pos) * 10 + coverage(goal) / (steps + 1)
                     + min(spread, 3) * .1 - steps * .15
                     - turn.base_distance(goal) * .3)
            choices.append((-score, next_pos != robot.pos, steps, goal, step))
        if robot.abnormal_state == 'dizzy' or not choices:
            goal, step = robot.pos, None
        else:
            _, _, _, goal, step = min(choices)
        assigned.add(goal)
        if step is not None and ledger.add(robot.id, command('move', step)):
            ledger.explain(robot.id, 'robot_screen_intercept', goal=goal,
                           ray_coverage=coverage(step), predicted=True)
        else:
            ledger.used.add(robot.id)
            ledger.reserved.add(robot.pos)
            ledger.explain(robot.id, 'robot_screen_hold', goal=goal,
                           protected_current_rays=len(critical))
    exposed = {uid for uid, cells, _, immediate in rays if immediate and not cells & actual}
    emit_event('robot_screen', dict(robots=len(robots), threat_ids=sorted({r[0] for r in rays}),
               exposed_enemy_ids=sorted(exposed), current_rays=sum(r[3] for r in rays),
               geometry='positive_length_ray_assumption', positions=sorted(actual)), 'decision')


def act_offensive_robots(turn, mem, nav, ledger):
    """Run the normal assault controller only for non-small summoned robots."""
    offensive = tuple(r for r in turn.summon_robots if r.kind != 'smallRobot')
    if not offensive:
        return
    if not hasattr(mem, 'robot_assault'):
        mem.robot_assault = RobotAssaultMemory()
    original_robots, original_ids = turn.summon_robots, turn.summon_robot_ids
    try:
        turn.summon_robots = offensive
        turn.summon_robot_ids = {r.id for r in offensive}
        act_robots(turn, mem.robot_assault, nav, ledger)
    finally:
        turn.summon_robots, turn.summon_robot_ids = original_robots, original_ids


def summon_offensive_order(turn, mem, nav, ledger, state):
    """Keep any carried larger order on the original enemy-base summon path."""
    if state['count'] >= 10:
        return False
    carriers = [(hero, name) for hero in turn.heroes if hero.id not in ledger.used
                for name in SUMMON_ORDERS if name != ORDER and hero.inventory[name]]
    if not carriers:
        return False
    position = choose_summon_position(turn, ledger, nav.deadline)
    if position is None:
        return False
    hero, name = carriers[0]
    if not ledger.add(hero.id, command('use', position, name=name)):
        return False
    state['count'] += 1
    state['positions'].add(position)
    state['pending'] = turn.round, hero.id, position
    ledger.summon_pending_positions.add(position)
    ledger.summon_daily_count = state['count']
    ledger.explain(hero.id, 'robot_assault_summon', item=name, position=position,
                   count=state['count'])
    return True


def summon_position(turn, state):
    goals = screen_cells(turn)
    candidates = [p for x in range(turn.width) for y in range(turn.height)
                  if (p := (x, y)) not in turn.blocked and p not in state['positions']
                  and turn.summon_position_legal(p)]
    # Small robots alone spread around our own base. Larger orders use the
    # normal enemy-base assault summon selector above.
    return min(candidates, key=lambda p: (min(distance(p, q) for q in goals),
               -min((distance(p, q) for q in state['positions']), default=0), p), default=None)


def prepare_screen(turn, cfg, mem, nav, ledger, state):
    free = [h for h in turn.workers if h.id not in ledger.used]
    for hero in turn.heroes:
        if hero.id in ledger.used or (hero.kind == "pioneer" and turn.phase_task):
            continue
        if hero.inventory[ORDER] and state['count'] < 10:
            point = summon_position(turn, state)
            if point is not None and ledger.add(hero.id, command('use', point, name=ORDER)):
                state['count'] += 1
                state['positions'].add(point)
                state['pending'] = turn.round, hero.id, point
                ledger.summon_pending_positions.add(point)
                ledger.summon_daily_count = state['count']
                ledger.explain(hero.id, 'robot_screen_summon', position=point, count=state['count'])
                return
    # At least four cheap blockers before spending 100 on the base upgrade.
    voucher_held = any(h.inventory[VOUCHER] for h in turn.heroes)
    name = (VOUCHER if state['purchased'] >= 4 and turn.station.level == 1
            and not voucher_held and not state['station_purchased']
            and VOUCHER in turn.shop and ledger.gold >= turn.shop[VOUCHER] else ORDER)
    if name == ORDER and state['purchased'] >= LIMIT:
        return
    price = turn.shop.get(name)
    if price is None or price < 0 or ledger.gold < price:
        return
    held = sum(h.inventory[n] for h in turn.heroes for n in SUMMON_ORDERS)
    capacity = min(LIMIT - state['purchased'], 10 - state['count'] - held)
    quantity = (min(capacity, ledger.gold // price if price else capacity) if name == ORDER else 1)
    if quantity <= 0:
        return
    plans = []
    for hero in free:
        plan = robot_purchase_plan(turn, cfg, mem, nav, ledger, hero, name,
                                   quantity=quantity, reserved_uses=held)
        if plan:
            count, shop, route = plan
            plans.append((route[0], hero.id != state['buyer'], hero.id, hero, count, route))
    if not plans:
        return
    _, _, _, hero, count, route = min(plans)
    cmd = (command('buy', name=name, num=count) if route[1] is None else command('move', route[1]))
    if ledger.add(hero.id, cmd):
        state['buyer'] = hero.id
        if route[1] is None:
            if name == ORDER:
                state['purchased'] += count
                state['pending_purchase'] = turn.round, hero.id, count
            else:
                state['station_purchased'] = True
                state['station_pending'] = turn.round, hero.id
        ledger.explain(hero.id, 'robot_screen_purchase', item=name, quantity=count,
                       total_cost=count * price, route_steps=route[0])


def run_experiment(turn, cfg, mem, nav, ledger, intel, state):
    """No construction/scouting in this isolated experiment."""
    prompt, execute = '', ''
    if not turn.station:
        return prompt, execute
    # Larger robots keep the normal assault lifecycle on both day and night.
    # Only small robots are diverted into the home screen experiment.
    act_offensive_robots(turn, mem, nav, ledger)
    if not turn.is_day:
        defend_screen(turn, mem, nav, ledger)
        # Personnel stay out of the eight edge/interception cells. Do not send
        # undefended workers mining among the incoming wave.
        safe = {p for x in range(turn.width) for y in range(turn.height)
                if turn.inside(p := (x, y)) and turn.base_distance(p) in (1, 2)
                and p not in screen_cells(turn) and p not in turn.blocked
                and all(distance(p, r.pos) > r.attack_range + 1 for r in turn.robots
                        if r.id not in turn.summon_robot_ids)}
        for hero in turn.heroes:
            route = nav.search(hero, safe - ledger.reserved) if safe else None
            if route and route[1] is not None:
                ledger.add(hero.id, command('move', route[1]))
        return prompt, execute
    # Deliver any paid level-one base upgrade before ordinary work.
    for hero in turn.heroes:
        if hero.inventory[VOUCHER] and turn.station.level == 1:
            if turn.base_distance(hero.pos) <= 1:
                ledger.add(hero.id, command('use', turn.station.nearest_cell(hero.pos), name=VOUCHER))
            else:
                walk(nav, ledger, hero, turn.station.cells)
    # A larger order, if already carried, retains the original enemy-base spawn
    # behavior. Small orders then fill the local screen with remaining actors.
    summon_offensive_order(turn, mem, nav, ledger, state)
    prepare_screen(turn, cfg, mem, nav, ledger, state)
    act_imps(turn, mem.sabotage, nav, ledger)
    # No stone demand or wall material reservation in this experiment.
    zones = turn.zones
    turn.zones = {p: k for p, k in zones.items() if k != 'stone'}
    try:
        for hero in turn.workers:
            if hero.id not in ledger.used:
                amount = sum(hero.inventory[k] for k in ('stone', 'iron', 'copper'))
                earn(turn, cfg, mem, nav, ledger, hero, force_sale=amount >= 8)
    finally:
        turn.zones = zones
    h = turn.pioneer
    if h and h.id not in ledger.used:
        if turn.phase_task:
            prompt, execute = intel.task(ledger, available_rounds=turn.day_left)
        else:
            pioneer(turn, cfg, mem, nav, ledger, h, shopping=False)
    if not prompt and not execute:
        prompt = intel.news()
    emit_event('robot_screen_budget', dict(gold=ledger.gold, purchased=state['purchased'],
               summoned=state['count'], cap=LIMIT, base_level=turn.station.level), 'economy')
    return prompt, execute
