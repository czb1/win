"""A task-first, five-BOSS shopping trip from day two's tick 50 onward."""
from .commands import command
from .model import SUMMON_ORDERS
from .robot_assault import choose_summon_position, enemy_base

PIONEER_OFFENSE_TICK = 50
BATCH_SIZE = 5
BOSS = 'BossRobotSummonOrder'


def observe_daily_pioneer(turn, mem):
    state = mem.daily_pioneer_offense
    carrying_over = state.get('phase') in ('check', 'shop', 'robots')
    if (turn.day >= 2 and turn.is_day and turn.tick >= PIONEER_OFFENSE_TICK
            and state.get('day') != turn.day and not carrying_over):
        state.clear()
        state.update(day=turn.day, phase='task' if turn.phase_task else 'check',
                     remaining=BATCH_SIZE, pending_buy=None, pending_use=None, anchor=None)
    if state.get('day') == turn.day and state.get('phase') == 'task' and not turn.phase_task:
        state['phase'] = 'check'
    return state


def daily_pioneer_active(turn, mem):
    state = mem.daily_pioneer_offense
    return bool(state and state.get('phase') != 'skipped' and
                (state['day'] == turn.day or
                 state['day'] < turn.day and state.get('phase') in ('check', 'shop', 'robots')))


def daily_pioneer_work_pending(turn, mem):
    return (turn.is_day and daily_pioneer_active(turn, mem)
            and mem.daily_pioneer_offense.get('phase') in ('check', 'shop', 'robots'))


def _placements(turn, ledger, nav, state, count):
    """Preview the same rear cluster used by subsequent serialized summons."""
    old = (ledger.robot_summon_mode, ledger.robot_summon_anchor,
           ledger.robot_assault_entry, ledger.summon_pending_positions)
    try:
        ledger.robot_summon_mode = 'daily_rear'
        ledger.robot_summon_anchor = state['anchor']
        ledger.robot_assault_entry = None
        ledger.summon_pending_positions = ledger.summon_pending_positions.copy()
        positions = []
        for _ in range(count):
            point = choose_summon_position(turn, ledger, nav.deadline)
            if point is None:
                break
            positions.append(point)
            ledger.summon_pending_positions.add(point)
            if ledger.robot_summon_anchor is None:
                ledger.robot_summon_anchor = point
        return positions
    finally:
        (ledger.robot_summon_mode, ledger.robot_summon_anchor,
         ledger.robot_assault_entry, ledger.summon_pending_positions) = old


def prepare_daily_pioneer(turn, cfg, mem, nav, ledger):
    from .brain import DAILY_SUMMON_LIMIT, observe_robot_summons
    from .spending import return_pioneer_to_post

    state = observe_daily_pioneer(turn, mem)
    hero = turn.pioneer
    if not hero or not daily_pioneer_active(turn, mem) or hero.id in ledger.used or turn.phase_task:
        return False
    summons = observe_robot_summons(turn, mem)
    ledger.summon_pending_positions = summons['positions'].copy()
    ledger.summon_daily_count = summons['count']
    results = turn.raw.get('lastRoundRoleActionResults') or {}
    for kind in ('buy', 'use'):
        pending = state.get('pending_' + kind)
        if not pending or pending[0] >= turn.round:
            continue
        previous, before, *position = pending
        same_day = (previous - cfg.round_origin) // 130 + 1 == turn.day
        rejected = (previous == turn.round - 1
                    and results.get(str(hero.id), results.get(hero.id)) is False)
        delivered = (hero.inventory[BOSS] >= before + BATCH_SIZE if kind == 'buy'
                     else hero.inventory[BOSS] < before)
        if rejected or not delivered:
            # The shared observer already rolls back explicit rejections.
            if kind == 'buy':
                state['phase'] = 'shop'
                if not rejected and same_day:
                    summons['purchased'] = max(0, summons['purchased'] - BATCH_SIZE)
            elif not rejected and same_day:
                summons['count'] -= 1
                summons['positions'].discard(position[0])
                ledger.summon_daily_count = summons['count']
                ledger.summon_pending_positions.discard(position[0])
        elif kind == 'use':
            state['remaining'] -= 1
        state['pending_' + kind] = None

    def skip(reason):
        state.update(phase='skipped', reason=reason)
        ledger.spending_plan['daily_pioneer_offense'] = dict(day=turn.day, blocked=reason)
        return False

    if not turn.is_day:
        # Pause the trip for night guns and resume any paid/committed work in
        # daylight. Never shrink a funded batch solely because a task ran late.
        return False
    if state['phase'] in ('check', 'shop'):
        price = turn.shop.get(BOSS, -1)
        cost = BATCH_SIZE * price
        stock = sum(h.inventory[item] for h in turn.heroes for item in SUMMON_ORDERS)
        if enemy_base(turn) is None:
            return skip('enemy_base_missing')
        if price < 0 or ledger.gold < cost:
            return skip('insufficient_batch_budget')
        if hero.space < BATCH_SIZE or summons['count'] + stock + BATCH_SIZE > DAILY_SUMMON_LIMIT:
            return skip('insufficient_batch_capacity')
        options = []
        for shop, kind in turn.zones.items():
            if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
                continue
            route = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
            if route is not None:
                options.append((route[0], shop, route))
        if not options:
            return skip('no_shop_route')
        if len(_placements(turn, ledger, nav, state, BATCH_SIZE)) < BATCH_SIZE:
            return skip('insufficient_rear_positions')
        state['phase'] = 'shop'
        mem.task_target = mem.task_wait_cell = None
        mem.return_targets.pop(hero.id, None)
        mem.return_posts.pop(hero.id, None)
        _, shop, route = min(options)
        if route[1] is not None:
            nav.move_along(hero, ledger, route, {route[2]}, targets={shop}, prefer_vehicle=False)
            ledger.gold -= cost
        elif (hero.id, BOSS) not in mem.buy_failures:
            if ledger.add(hero.id, command('buy', name=BOSS, num=BATCH_SIZE)):
                state.update(phase='robots', pending_buy=(turn.round, hero.inventory[BOSS]))
                summons['purchased'] += BATCH_SIZE
                summons['pending_purchase'] = turn.round, hero.id, BATCH_SIZE
        else:
            ledger.gold -= cost
        ledger.used.add(hero.id)
        ledger.explain(hero.id, 'daily_pioneer_robot_shop', quantity=BATCH_SIZE,
                       unit_price=price, shop=shop, route_steps=route[0])
        return True
    if state['phase'] == 'robots':
        if state['remaining'] <= 0 or enemy_base(turn) is None:
            state['phase'] = 'return'
        else:
            pending = state['pending_buy'] or state['pending_use']
            positions = (_placements(turn, ledger, nav, state, 1)
                         if not pending and hero.inventory[BOSS]
                         and summons['count'] < DAILY_SUMMON_LIMIT else [])
            if positions and not (summons['pending'] and summons['pending'][0] == turn.round):
                point = positions[0]
                if ledger.add(hero.id, command('use', point, name=BOSS)):
                    state['pending_use'] = turn.round, hero.inventory[BOSS], point
                    state['anchor'] = state['anchor'] or point
                    summons['count'] += 1
                    summons['positions'].add(point)
                    summons['pending'] = turn.round, hero.id, point
                    summons['pending_plan'] = summons['anchor'], summons['mode']
                    summons.update(anchor=state['anchor'], mode='daily_rear')
            ledger.used.add(hero.id)
            ledger.explain(hero.id, 'daily_pioneer_robot_summon', remaining=state['remaining'],
                           anchor=state['anchor'], daily_reserved_uses=summons['count'])
            return True
    if state['phase'] == 'return':
        return return_pioneer_to_post(turn, mem, nav, ledger, hero)
    return False
