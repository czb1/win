"""A fixed first-day worker cashes out, buys orders and denies a wall cell."""
from dataclasses import replace

from .commands import command
from .economy import build, build_options, buy_supply, supplies, use_inventory, voucher_for
from .economy_plan import via
from .logging_system import emit_event
from .model import DEFENCE_RETURN_TICK, ORES, SUMMON_ORDERS
from .robot_assault import enemy_base, first_day_wall_entries, scouted_rear_summon_position
from .scouting import FIRST_DAY_SCOUT_TICK, prepare_first_day_wall_blocker
from .spending import (FIRST_DAY_ROBOT_PURCHASE_LIMIT, first_day_guns_built,
                       first_day_purchase_reserve, first_day_pioneer_plan_active)


FIRST_DAY_OFFENSE_TICK = FIRST_DAY_SCOUT_TICK
BOSS_ORDER = 'BossRobotSummonOrder'


def observe_first_day_gatling(turn, mem):
    """Cancel from shared, live enemy sight before reserving any new actions."""
    scout = mem.night_scout
    if scout.cancelled or turn.day != 1 or not turn.is_day:
        return
    gatling = next((unit for unit in turn.enemies if unit.kind == 'gatling'), None)
    if gatling is None:
        return
    worker_id = mem.first_day_offense.get('worker_id') or scout.worker_id
    scout.cancelled = True
    scout.key, scout.worker_id, scout.post, scout.wall_entry = None, None, None, None
    mem.first_day_offense.clear()
    mem.first_day_offense.update(worker_id=worker_id, phase='upgrade_buy',
                                 transition='enemy_gatling_spotted')
    # Release both the worker's old task chain and any other robot buyer.
    mem.daytime_jobs = {uid: job for uid, job in mem.daytime_jobs.items()
                       if uid != worker_id and job.get('kind') != 'robot_buy'}
    for targets in (mem.build_targets, mem.wall_batches, mem.sale_targets, mem.mine_targets,
                    mem.upgrade_targets,
                    mem.return_targets, mem.return_posts):
        targets.pop(worker_id, None)
    for workers in (mem.preparation_workers, mem.sold_workers, mem.sale_workers):
        workers.discard(worker_id)
    if mem.supply_worker == worker_id:
        mem.supply_worker = None
    emit_event('first_day_offense_cancelled', dict(reason='enemy_gatling_spotted',
               worker=worker_id, enemy=gatling.id, position=gatling.pos), 'decision')


def first_day_offense_worker(turn, cfg, mem):
    """Identify the future wall blocker before its tick-25 task chain starts."""
    base = enemy_base(turn)
    if mem.night_scout.cancelled or not turn.is_day or turn.day != 1 or base is None:
        return None
    if len(turn.workers) < 2 and not first_day_guns_built(turn, cfg):
        return None
    state = mem.first_day_offense
    worker_id = (state.get('worker_id')
                 if state.get('key') == (turn.day, base.id, base.pos) else None)
    return (next((w for w in turn.workers if w.id == worker_id), None)
            or min(turn.workers, key=lambda w: w.id, default=None))


def _entries(turn, base):
    return {p for p in first_day_wall_entries(turn, base)
            if p not in turn.blocked and turn.zones.get(p, 'land') == 'land'}


def _tail_steps(turn, mem, nav, ledger, hero, point, base, sell=True, buy=True):
    """Reserve travel and actions for the remaining stops, ending at the gap."""
    counts = {k: hero.inventory[k] for k in ORES if k != 'stone'
              and hero.inventory[k] and turn.prices.get(k, 0) > 0} if sell else {}
    held = sum(hero.inventory[n] for n in SUMMON_ORDERS)
    vendors = {p for p, k in turn.zones.items() if k == 'vendor'}
    shops = {p for p, k in turn.zones.items() if k == 'weaponShop'}
    counts = counts if vendors else {}
    buying = (buy and not first_day_pioneer_plan_active(turn)
              and not held and shops and turn.shop.get(BOSS_ORDER, -1) >= 0
              and mem.robot_summon_state.get('purchased', 0) < FIRST_DAY_ROBOT_PURCHASE_LIMIT)
    groups, actions = [], len(counts) + held
    if counts:
        groups.append(vendors)
    if buying:
        groups.append(shops)
        actions += 2  # At least one purchase and one daylight use.
    entries = _entries(turn, base)
    if entries:
        groups.append(entries)
    if not groups:
        return actions
    original = turn.blocked
    try:
        # Only this worker's present tile is vacated in the route estimate.
        turn.blocked = original - {hero.pos}
        trip = via(nav, replace(hero, pos=point), groups, ledger.reserved,
                   final_exact=bool(entries))
    finally:
        turn.blocked = original
    return None if trip is None else trip + actions


def _advance(mem, ledger, hero, state, phase, reason):
    state['phase'] = phase
    state['transition'] = reason
    mem.daytime_jobs.pop(hero.id, None)
    ledger.work_jobs.pop(hero.id, None)
    mem.build_targets.pop(hero.id, None)
    mem.wall_batches.pop(hero.id, None)


def _walls(turn, cfg, mem, nav, ledger, hero, base, walls):
    if hero.inventory['stone'] < cfg.wall_stones:
        return False
    for _, _, _, _, target, route in build_options(
            turn, cfg, mem, nav, ledger, hero, walls, lambda _: 'wall'):
        tail = _tail_steps(turn, mem, nav, ledger, hero, route[2], base)
        if tail is None or route[0] + 1 + tail > turn.day_left:
            continue
        if build(turn, cfg, mem, nav, ledger, hero, [target], lambda _: 'wall',
                 work_cell=route[2]):
            return True
    return False


def _sell(turn, mem, nav, ledger, hero, base):
    # Held stone has already had its construction chance. Sell any unusable
    # remainder as well, without news stockpiling or another collection trip.
    counts = {k: hero.inventory[k] for k in ORES
              if hero.inventory[k] and turn.prices.get(k, 0) > 0}
    if not counts:
        return False
    options = []
    for vendor, kind in turn.zones.items():
        if kind != 'vendor' or mem.movement.avoids(hero.id, vendor):
            continue
        route = nav.approach(hero, {vendor}, ledger.reserved, with_endpoint=True)
        if route is None:
            continue
        tail = _tail_steps(turn, mem, nav, ledger, hero, route[2], base, sell=False)
        if tail is not None and route[0] + len(counts) + tail <= turn.day_left:
            options.append((vendor != mem.sale_targets.get(hero.id), route[0], vendor, route))
    if not options:
        return False
    _, _, vendor, route = min(options)
    ore = max(counts, key=lambda k: (counts[k] * turn.prices[k], k))
    action = (command('move', route[1]) if route[1] is not None
              else command('sell', name=ore, num=counts[ore]))
    if not ledger.add(hero.id, action):
        return False
    mem.sale_targets[hero.id] = vendor
    if route[1] is None:
        mem.sold_workers.add(hero.id)
    ledger.remember_work(hero, 'sell', vendor)
    ledger.explain(hero.id, 'first_day_offense_sell', vendor=vendor, ore=ore,
                   quantity=counts[ore], route_steps=route[0])
    return True


def _buy(turn, cfg, mem, nav, ledger, hero, base, towers):
    from .brain import summon_best_robot

    if first_day_pioneer_plan_active(turn):
        return False, 'pioneer_owns_post_task_purchase'
    if any(hero.inventory[n] for n in SUMMON_ORDERS):
        return False, 'carried_order_ready'
    state = mem.robot_summon_state
    remaining = FIRST_DAY_ROBOT_PURCHASE_LIMIT - state.get('purchased', 0)
    if remaining <= 0 or BOSS_ORDER not in turn.shop or turn.shop[BOSS_ORDER] < 0 or not hero.space:
        return False, 'purchase_limit_item_or_capacity'
    options = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
            continue
        route = nav.approach(hero, {shop}, ledger.reserved, with_endpoint=True)
        if route is None:
            continue
        tail = _tail_steps(turn, mem, nav, ledger, hero, route[2], base, sell=False, buy=False)
        if tail is None:
            continue
        quantity = min(remaining, hero.space, turn.day_left - route[0] - 1 - tail)
        if quantity > 0:
            options.append((-quantity, route[0], shop, route))
    if not options:
        return False, 'shop_or_wall_daylight_deadline'
    negative_count, _, shop, route = min(options)
    if (hero.id, BOSS_ORDER) in mem.buy_failures:
        ledger.explain(hero.id, 'first_day_offense_wait_buy_retry', shop=shop,
                       retry_round=mem.buy_failures[hero.id, BOSS_ORDER])
        return True, 'buy_retry_cooldown'
    reserve = first_day_purchase_reserve(turn, mem, ledger, include_robots=False)
    if summon_best_robot(turn, cfg, mem, nav, ledger, towers, ledger.wall_cells,
                         excluded={h.id for h in turn.heroes if h.id != hero.id},
                         reserve=reserve, item_only=BOSS_ORDER, shop_only=shop,
                         quantity_limit=-negative_count):
        return True, 'purchase_or_approach'
    if ledger.spending_plan.get('robot_blocked') == 'insufficient_surplus':
        # Stage while the other worker/pioneer can still earn the price. Stop
        # waiting when the next turn would no longer leave time to occupy/use.
        if route[1] is not None and ledger.add(hero.id, command('move', route[1])):
            ledger.remember_work(hero, 'robot_buy', shop, name=BOSS_ORDER,
                                 quantity=-negative_count)
        ledger.explain(hero.id, 'first_day_offense_wait_gold', shop=shop,
                       reserved_station_gold=reserve, gold=ledger.gold,
                       price=turn.shop[BOSS_ORDER], route_steps=route[0])
        return True, 'waiting_for_gold'
    return False, ledger.spending_plan.get('robot_blocked', 'purchase_unavailable')


def _upgrade_after_abort(turn, cfg, mem, nav, ledger):
    """Keep the scout for one affordable shop trip and its observed deliveries."""
    state = mem.first_day_offense
    if state.get('worker_id') is None and (len(turn.workers) >= 2 or first_day_guns_built(turn, cfg)):
        hero = min(turn.workers, key=lambda w: w.id, default=None)
        state['worker_id'] = hero.id if hero else None
    hero = next((w for w in turn.workers if w.id == state.get('worker_id')), None)
    if hero is None or hero.id in ledger.used:
        return None
    if state.get('phase') in ('upgrade_buy', 'upgrade_use'):
        targets = {w.pos for w in turn.weapons}
        held = any(hero.inventory[voucher_for(w)] for w in turn.weapons if voucher_for(w))
        if held:
            state['phase'] = 'upgrade_use'
        elif state['phase'] == 'upgrade_use':
            _advance(mem, ledger, hero, state, 'mining', 'weapon_vouchers_delivered')
        if state['phase'] != 'mining':
            acted = use_inventory(turn, nav, ledger, hero, mem=mem,
                                  upgrades_only=True, target_sites=targets)
            if not acted and not held and hero.health > 165:
                # Preserve the base heal and remaining opening construction.
                reserve = (first_day_purchase_reserve(turn, mem, ledger, include_robots=False)
                           + max(0, min(3, len(cfg.loadout)) - len(turn.weapons)) * cfg.weapon_cost)
                plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve,
                                bulk=True, shop_only=state.get('upgrade_shop'), target_sites=targets)
                if plan is None and state.get('upgrade_shop') is not None:
                    plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve,
                                    bulk=True, target_sites=targets)
                acted = buy_supply(turn, ledger, hero, plan)
                if acted:
                    state['upgrade_shop'] = ledger.supply_targets.get(hero.id)
            if acted:
                ledger.first_day_offense_worker_id = hero.id
                mem.daytime_jobs.pop(hero.id, None)
                ledger.notes[hero.id]['conditions'].update(
                    first_day_offense_phase=state['phase'], first_day_offense_worker=hero.id,
                    transition=state.get('transition'))
                return hero
            # A blocked paid delivery yields to recovery/recall, never mining.
            if held:
                return None
    if hero.health > 165 and turn.tick < DEFENCE_RETURN_TICK:
        from .mining import earn
        earn(turn, cfg, mem, nav, ledger, hero, deadline=DEFENCE_RETURN_TICK)
    return None


def act_first_day_offense(turn, cfg, mem, nav, ledger, towers, walls):
    """From tick 25, one stable worker owns walls -> sell -> buy -> wall denial."""
    base = enemy_base(turn)
    if turn.day != 1:
        mem.first_day_offense.clear()
    if not turn.is_day or turn.day != 1:
        return None
    from .brain import observe_robot_summons
    observe_robot_summons(turn, mem)
    if mem.night_scout.cancelled:
        return _upgrade_after_abort(turn, cfg, mem, nav, ledger)
    if turn.tick < FIRST_DAY_OFFENSE_TICK or base is None:
        return None
    # With no second worker there is no way to split offense and construction.
    if len(turn.workers) < 2 and not first_day_guns_built(turn, cfg):
        return None
    state = mem.first_day_offense
    key = (turn.day, base.id, base.pos)
    if state.get('key') != key:
        state.clear()
        state.update(key=key, worker_id=None, phase='walls')
    hero = first_day_offense_worker(turn, cfg, mem)
    if hero is None:
        return None
    if hero.id != state['worker_id']:
        state.update(worker_id=hero.id, phase='walls')
        mem.mine_targets.pop(hero.id, None)
        mem.build_targets.pop(hero.id, None)
        mem.wall_batches.pop(hero.id, None)
    ledger.first_day_offense_worker_id = hero.id
    mem.daytime_jobs.pop(hero.id, None)
    mem.preparation_workers.discard(hero.id)
    mem.return_targets.pop(hero.id, None)
    mem.return_posts.pop(hero.id, None)
    if mem.supply_worker == hero.id:
        mem.supply_worker = None
    try:
        if hero.health <= 165 and hero.inventory['Medicine']:
            ledger.add(hero.id, command('use', name='Medicine'))
            return hero
        if state['phase'] == 'walls':
            if _walls(turn, cfg, mem, nav, ledger, hero, base, walls):
                return hero
            _advance(mem, ledger, hero, state, 'sell', 'stone_finished_or_wall_unavailable')
        if state['phase'] == 'sell':
            if _sell(turn, mem, nav, ledger, hero, base):
                return hero
            _advance(mem, ledger, hero, state, 'buy', 'ore_finished_or_sale_unavailable')
        if state['phase'] == 'buy':
            acted, reason = _buy(turn, cfg, mem, nav, ledger, hero, base, towers)
            if acted:
                return hero
            _advance(mem, ledger, hero, state, 'block', reason)
        scout = mem.night_scout
        if scout.key != key or scout.worker_id != hero.id:
            scout.key, scout.worker_id, scout.post, scout.wall_entry = key, hero.id, None, None
        from .brain import summon_best_robot
        # An observed open rear needs no held gap. Leave the final daylight
        # uses available even when neither route could be secured in time.
        rear_open = scouted_rear_summon_position(turn, ledger, nav.deadline) is not None
        if (turn.tick >= DEFENCE_RETURN_TICK or rear_open) and summon_best_robot(
                turn, cfg, mem, nav, ledger, towers, walls,
                excluded={h.id for h in turn.heroes if h.id != hero.id}, use_only=True):
            return hero
        blocker = prepare_first_day_wall_blocker(
            turn, scout, nav, ledger, worker_id=hero.id,
            departure_tick=FIRST_DAY_OFFENSE_TICK, reserve_hold=False)
        if blocker and hero.pos == scout.wall_entry and hero.id not in ledger.used:
            summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls,
                              excluded={h.id for h in turn.heroes if h.id != hero.id}, use_only=True)
        if blocker is None:
            ledger.explain(hero.id, 'first_day_offense_gap_unavailable', entry=scout.wall_entry)
        return hero
    finally:
        # The role stays assigned even when no legal action is available. No
        # later construction, shopping, recall, recovery or mining may take it.
        ledger.used.add(hero.id)
        ledger.reserved.add(hero.pos)
        note = ledger.notes.setdefault(hero.id, {'reason': 'first_day_offense_wait', 'conditions': {}})
        note['conditions'].update(first_day_offense_phase=state['phase'],
                                  first_day_offense_worker=hero.id, stop_mining_tick=FIRST_DAY_OFFENSE_TICK,
                                  transition=state.get('transition'))
