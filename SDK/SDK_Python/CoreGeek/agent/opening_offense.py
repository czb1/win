"""A fixed first-day worker cashes out, buys orders and denies a wall cell."""
from collections import Counter
from dataclasses import replace

from .commands import command
from .economy import (build, build_options, buy_supply, delivery_trip, upgrade_order,
                      use_inventory, voucher_for, weapon_upgrade_allowed)
from .economy_plan import via
from .logging_system import emit_event
from .model import DEFENCE_RETURN_TICK, ORES, SUMMON_ORDERS, neighbours
from .robot_assault import enemy_base, first_day_wall_entries
from .scouting import FIRST_DAY_SCOUT_TICK, prepare_first_day_wall_blocker
from .spending import (FIRST_DAY_ROBOT_PURCHASE_LIMIT, first_day_guns_built,
                       first_day_purchase_reserve, first_day_pioneer_plan_active,
                       first_day_robot_batch_allowed)


FIRST_DAY_OFFENSE_TICK = FIRST_DAY_SCOUT_TICK
BOSS_ORDER = 'BossRobotSummonOrder'


def cancel_first_day_offense(turn, mem, reason, *, phase='mining', ledger=None, **details):
    """Release the assault role and keep the cancellation for this match."""
    scout = mem.night_scout
    if scout.cancelled:
        return
    worker_id = mem.first_day_offense.get('worker_id') or scout.worker_id
    scout.cancelled = True
    scout.cancel_reason = reason
    scout.key, scout.worker_id, scout.post, scout.wall_entry = None, None, None, None
    mem.first_day_offense.clear()
    mem.first_day_offense.update(worker_id=worker_id, phase=phase, transition=reason)
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
    if ledger is not None:
        # The pioneer shops after the blocker is planned. Withdraw only that
        # unsubmitted movement/hold; keep useful sales, construction and heals.
        if ledger.first_day_offense_worker_id == worker_id and worker_id is not None:
            action = ledger.commands.get(str(worker_id), {})
            if action.get('action') in (None, 'move'):
                from .model import pos
                ledger.commands.pop(str(worker_id), None)
                ledger.used.discard(worker_id)
                ledger.reserved.difference_update(pos(p) for p in action.get('targetPos', []))
                worker = next((w for w in turn.workers if w.id == worker_id), None)
                if worker:
                    ledger.reserved.discard(worker.pos)
                ledger.notes.pop(worker_id, None)
            ledger.work_jobs.pop(worker_id, None)
        ledger.first_day_offense_worker_id = None
        ledger.robot_assault_entry = None
        ledger.spending_plan['robot_blocked'] = reason
        ledger.spending_plan['robot_use_blocked'] = reason
    if getattr(mem, 'robot_summon_state', None):
        mem.robot_summon_state['buyer'] = None
    emit_event('first_day_offense_cancelled', dict(reason=reason, worker=worker_id,
                                                  **details), 'decision')


def observe_first_day_gatling(turn, mem):
    """Cancel from shared, live enemy sight before reserving any new actions."""
    if mem.night_scout.cancelled or turn.day != 1 or not turn.is_day:
        return
    gatling = next((unit for unit in turn.enemies if unit.kind == 'gatling'), None)
    if gatling is not None:
        cancel_first_day_offense(turn, mem, 'enemy_gatling_spotted', phase='upgrade_buy',
                                 enemy=gatling.id, position=gatling.pos)


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
        first_day_robot_batch_allowed(turn, mem, ledger, 0)
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
        first_day_robot_batch_allowed(turn, mem, ledger, 0)
        return False, 'shop_or_wall_daylight_deadline'
    negative_count, _, shop, route = min(options)
    reserve = first_day_purchase_reserve(turn, mem, ledger, include_robots=False)
    price = turn.shop[BOSS_ORDER]
    funded = min(-negative_count, max(0, ledger.gold - reserve) // price) if price else -negative_count
    if not first_day_robot_batch_allowed(turn, mem, ledger, funded):
        return False, 'insufficient_first_day_robots'
    if (hero.id, BOSS_ORDER) in mem.buy_failures:
        ledger.explain(hero.id, 'first_day_offense_wait_buy_retry', shop=shop,
                       retry_round=mem.buy_failures[hero.id, BOSS_ORDER])
        return True, 'buy_retry_cooldown'
    if summon_best_robot(turn, cfg, mem, nav, ledger, towers, ledger.wall_cells,
                         excluded={h.id for h in turn.heroes if h.id != hero.id},
                         reserve=reserve, item_only=BOSS_ORDER, shop_only=shop,
                         quantity_limit=-negative_count):
        return True, 'purchase_or_approach'
    return False, ledger.spending_plan.get('robot_blocked', 'purchase_unavailable')


def _upgrade_cart(turn, ledger, hero, weapons, limit):
    """Fund useful upgrades through level three, including their prerequisites."""
    own = hero.inventory.copy()
    others = Counter(item for h in turn.heroes if h.id != hero.id for item in h.backpack)
    gold, space = ledger.gold, min(hero.space, limit)
    cart, deliveries = Counter(), []
    for weapon in weapons:
        for level in range(weapon.level, 3):
            name = f'WeaponUpgradeVoucher{level}'
            if own[name]:
                own[name] -= 1
            elif others[name]:
                others[name] -= 1
                # Another carrier's prerequisite is not ours to deliver.
                break
            else:
                price = turn.shop.get(name, -1)
                if price < 0 or price > gold or not space or name in ledger.purchases:
                    break
                cart[name] += 1
                gold -= price
                space -= 1
            deliveries.append(weapon.cells)
    return cart, deliveries


def _buy_upgrade_cart(turn, cfg, mem, nav, ledger, hero, state):
    """Finish both voucher tiers at one shop before starting the return trip."""
    weapons = [w for w in sorted(turn.weapons, key=lambda w: upgrade_order(turn, w, mem))
               if w.level < 3 and weapon_upgrade_allowed(turn, w)
               and not mem.movement.avoids(hero.id, w.pos)
               and nav.approach(hero, w.cells, ledger.reserved) is not None]
    limit = min(hero.space, sum(3 - w.level for w in weapons))
    options = []
    for maximum in range(limit, 0, -1):
        cart, deliveries = _upgrade_cart(turn, ledger, hero, weapons, maximum)
        if not cart:
            continue
        for shop, kind in turn.zones.items():
            if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
                continue
            for cell in neighbours(shop):
                route = nav.search(hero, {cell}, ledger.reserved)
                if route is None:
                    continue
                walk = delivery_trip(turn, nav, ledger, hero, cell, deliveries)
                if walk is None:
                    continue
                retry = max(0, mem.buy_failures.get((hero.id, next(iter(cart))), turn.round) - turn.round)
                cost = route[0] + len(cart) + retry + walk + len(deliveries) + cfg.return_margin
                if cost < turn.day_left:
                    options.append((shop != state.get('upgrade_shop', shop), cost,
                                    shop, route, cart))
        if options:
            break
    if not options:
        return False
    _, _, shop, route, cart = min(options, key=lambda row: (row[0], row[1], row[3][0]))
    name = next(iter(cart))
    cost = sum(turn.shop[item] * num for item, num in cart.items())
    ledger.supply_targets[hero.id] = shop
    if route[1] is None and (hero.id, name) in mem.buy_failures:
        ledger.used.add(hero.id)
        ledger.reserved.add(hero.pos)
        ledger.gold -= cost
        ledger.explain(hero.id, 'first_day_upgrade_wait_buy_retry', shop=shop,
                       retry_round=mem.buy_failures[hero.id, name])
    elif buy_supply(turn, ledger, hero, (name, route, cart[name])):
        # Reserve the later category too, without counting this purchase twice.
        ledger.gold -= cost - turn.shop[name] * cart[name]
        ledger.explain(hero.id, 'first_day_upgrade_shop', shop=shop, item=name,
                       quantity=cart[name], remaining_cart=dict(cart), reserved_gold=cost)
    else:
        return False
    ledger.purchases.update(cart)
    state['upgrade_shop'] = shop
    return True


def _upgrade_after_abort(turn, cfg, mem, nav, ledger):
    """Spend the scout's full useful-upgrade budget, then deliver observed stock."""
    state = mem.first_day_offense
    if state.get('worker_id') is None and (len(turn.workers) >= 2 or first_day_guns_built(turn, cfg)):
        hero = min(turn.workers, key=lambda w: w.id, default=None)
        state['worker_id'] = hero.id if hero else None
    hero = next((w for w in turn.workers if w.id == state.get('worker_id')), None)
    if hero is None or hero.id in ledger.used:
        return None
    if state.get('phase') in ('upgrade_buy', 'upgrade_use'):
        weapons = [w for w in turn.weapons if weapon_upgrade_allowed(turn, w)]
        targets = {w.pos for w in weapons}
        held = any(hero.inventory[voucher_for(w)] for w in weapons if voucher_for(w))
        # Already-carried vouchers away from an active shopping trip still
        # take the direct delivery path. Newly bought stock must not end a
        # two-category shopping visit after only its first purchase.
        at_shop = any(kind == 'weaponShop' and turn.adjacent(hero.pos, shop)
                      for shop, kind in turn.zones.items())
        if held and 'upgrade_shop' not in state and not at_shop:
            state['phase'] = 'upgrade_use'
        elif not held and state['phase'] == 'upgrade_use':
            _advance(mem, ledger, hero, state, 'mining', 'weapon_vouchers_delivered')
        if state['phase'] != 'mining':
            acted = (state['phase'] == 'upgrade_buy' and hero.health > 165
                     and _buy_upgrade_cart(turn, cfg, mem, nav, ledger, hero, state))
            if not acted:
                if held:
                    state['phase'] = 'upgrade_use'
                acted = use_inventory(turn, nav, ledger, hero, mem=mem,
                                      upgrades_only=True, target_sites=targets)
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
            if mem.night_scout.cancelled:
                return _upgrade_after_abort(turn, cfg, mem, nav, ledger)
            if acted:
                return hero
            _advance(mem, ledger, hero, state, 'block', reason)
        scout = mem.night_scout
        if scout.key != key or scout.worker_id != hero.id:
            scout.key, scout.worker_id, scout.post, scout.wall_entry = key, hero.id, None, None
        from .brain import summon_best_robot
        # Preserve final daylight uses; earlier, establish the worker's gap
        # before considering a rear fallback, including on the first visit.
        if turn.tick >= DEFENCE_RETURN_TICK and summon_best_robot(
                turn, cfg, mem, nav, ledger, towers, walls,
                excluded={h.id for h in turn.heroes if h.id != hero.id}, use_only=True):
            return hero
        blocker = prepare_first_day_wall_blocker(
            turn, scout, nav, ledger, worker_id=hero.id,
            departure_tick=FIRST_DAY_OFFENSE_TICK, reserve_hold=False)
        if hero.id not in ledger.used and (blocker is None or hero.pos == scout.wall_entry):
            summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls,
                              excluded={h.id for h in turn.heroes if h.id != hero.id}, use_only=True)
        if blocker is None:
            ledger.explain(hero.id, 'first_day_offense_gap_unavailable', entry=scout.wall_entry)
        return hero
    finally:
        # The role stays assigned even when no legal action is available. No
        # later construction, shopping, recall, recovery or mining may take it.
        if not mem.night_scout.cancelled:
            ledger.used.add(hero.id)
            ledger.reserved.add(hero.pos)
            note = ledger.notes.setdefault(hero.id, {'reason': 'first_day_offense_wait', 'conditions': {}})
            note['conditions'].update(first_day_offense_phase=state['phase'],
                                      first_day_offense_worker=hero.id, stop_mining_tick=FIRST_DAY_OFFENSE_TICK,
                                      transition=state.get('transition'))
