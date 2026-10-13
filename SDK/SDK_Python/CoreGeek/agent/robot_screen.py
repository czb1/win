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
BOSS_ORDER = 'BossRobotSummonOrder'
VOUCHER = 'StationUpgradeVoucher1'
LIMIT = 8
DAILY_SUMMON_LIMIT = 10


def screen_cells(turn):
    return {p for cell in turn.station.cells for p in neighbours(cell)
            if turn.inside(p) and p not in turn.station.cells}


def ray_cells(origin, target, base_cells):
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
    robots = sorted((r for r in turn.summon_robots if r.kind == 'smallRobot'),
                    key=lambda r: (turn.base_distance(r.pos), r.id))
    rays = threat_rays(turn)
    actual = {r.pos for r in robots}
    goals = screen_cells(turn)
    assigned = set()
    for robot in robots:
        check_time(nav.deadline)
        others = actual - {robot.pos}
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
    if state['count'] >= DAILY_SUMMON_LIMIT:
        return False
    carriers = [(hero, name) for hero in turn.heroes if hero.id not in ledger.used
                for name in reversed(SUMMON_ORDERS) if name != ORDER and hero.inventory[name]]
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
    ledger.explain(hero.id, 'summon_highest_robot', item=name, summon_position=position,
                   daily_reserved_uses=state['count'], daily_limit=DAILY_SUMMON_LIMIT)
    return True


def buy_original_boss(turn, cfg, mem, nav, ledger, state):
    """Restore the normal day-one offense: spend surplus on strongest BOSS orders."""
    if not turn.is_day or state['count'] >= DAILY_SUMMON_LIMIT:
        return False
    price = turn.shop.get(BOSS_ORDER)
    if price is None or price < 0 or ledger.gold < price:
        return False
    held = sum(hero.inventory[name] for hero in turn.heroes for name in SUMMON_ORDERS)
    remaining = DAILY_SUMMON_LIMIT - state['count'] - held
    if remaining <= 0:
        return False
    quantity = min(remaining, ledger.gold // price if price else remaining)
    if quantity <= 0:
        return False
    buyers = []
    for hero in turn.heroes:
        if hero.id in ledger.used or (hero.kind == 'pioneer' and turn.phase_task):
            continue
        plan = robot_purchase_plan(turn, cfg, mem, nav, ledger, hero, BOSS_ORDER,
                                   quantity=quantity, reserved_uses=held)
        if plan:
            count, shop, route = plan
            buyers.append((hero.id != state.get('buyer'), -count, route[0],
                           hero.kind != 'worker', hero.id, hero, route))
    if not buyers:
        return False
    _, negative_count, _, _, _, hero, route = min(buyers)
    quantity = -negative_count
    if route[1] is None:
        accepted = ledger.add(hero.id, command('buy', name=BOSS_ORDER, num=quantity))
    else:
        accepted = ledger.add(hero.id, command('move', route[1]))
    if not accepted:
        return False
    state['buyer'] = hero.id
    if route[1] is None:
        state['purchased'] += quantity
        state['pending_purchase'] = turn.round, hero.id, quantity
    else:
        ledger.gold -= price * quantity
        ledger.purchases.add(BOSS_ORDER)
        ledger.remember_work(hero, 'robot_buy', route[2], name=BOSS_ORDER, quantity=quantity)
    ledger.explain(hero.id, 'prepare_highest_robot', item=BOSS_ORDER, price=price,
                   quantity=quantity, total_cost=price * quantity,
                   route_steps=route[0], day_left=turn.day_left,
                   required_daylight_actions=route[0] + 1 + held + quantity,
                   purchase_submitted=route[1] is None)
    ledger.spending_plan['robot_purchase'] = dict(actor=hero.id, item=BOSS_ORDER,
        quantity=quantity, unit_price=price, total_cost=price * quantity)
    return True


def summon_position(turn, state):
    goals = screen_cells(turn)
    candidates = [p for x in range(turn.width) for y in range(turn.height)
                  if (p := (x, y)) not in turn.blocked and p not in state['positions']
                  and turn.summon_position_legal(p)]
    return min(candidates, key=lambda p: (min(distance(p, q) for q in goals),
               -min((distance(p, q) for q in state['positions']), default=0), p), default=None)


def prepare_screen(turn, cfg, mem, nav, ledger, state):
    free = [h for h in turn.workers if h.id not in ledger.used]
    for hero in turn.heroes:
        if hero.id in ledger.used or (hero.kind == 'pioneer' and turn.phase_task):
            continue
        if hero.inventory[ORDER] and state['count'] < DAILY_SUMMON_LIMIT:
            point = summon_position(turn, state)
            if point is not None and ledger.add(hero.id, command('use', point, name=ORDER)):
                state['count'] += 1
                state['positions'].add(point)
                state['pending'] = turn.round, hero.id, point
                ledger.summon_pending_positions.add(point)
                ledger.summon_daily_count = state['count']
                ledger.explain(hero.id, 'robot_screen_summon', position=point, count=state['count'])
                return
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
    capacity = min(LIMIT - state['purchased'], DAILY_SUMMON_LIMIT - state['count'] - held)
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
    cmd = command('buy', name=name, num=count) if route[1] is None else command('move', route[1])
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
    prompt, execute = '', ''
    if not turn.station:
        return prompt, execute
    act_offensive_robots(turn, mem, nav, ledger)
    if not turn.is_day:
        defend_screen(turn, mem, nav, ledger)
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
    for hero in turn.heroes:
        if hero.inventory[VOUCHER] and turn.station.level == 1:
            if turn.base_distance(hero.pos) <= 1:
                ledger.add(hero.id, command('use', turn.station.nearest_cell(hero.pos), name=VOUCHER))
            else:
                walk(nav, ledger, hero, turn.station.cells)
    # Preserve the original offensive robot flow: use existing large/BOSS orders
    # at the enemy base, then spend remaining gold on BOSS orders exactly as the
    # standard day-one strategy does. The screen logic only owns small robots.
    summon_offensive_order(turn, mem, nav, ledger, state)
    prepare_screen(turn, cfg, mem, nav, ledger, state)
    buy_original_boss(turn, cfg, mem, nav, ledger, state)
    act_imps(turn, mem.sabotage, nav, ledger)
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
