"""Fourth-night wall watch, subordinate to the existing gunner assignment."""
from dataclasses import replace
import logging
from .commands import command
from .model import neighbours, ORES, distance
from .mining import earn, sale_inventory
from .economy_plan import via, maxed_weapon_mining_phase
from .wall_health import repair_risk


def select_watch(turn, mem, pairs, fixed_operator=None, excluded=()):
    previous = mem.wall_watch_id
    operators = {fixed_operator} if fixed_operator is not None else {h.id for h, _ in pairs}
    eligible = [h for h in turn.workers if h.id not in operators and h.id not in excluded]
    hero = min(eligible, key=lambda h: (h.id != mem.wall_watch_id,
                                       -h.inventory['WallFixer'], h.id), default=None)
    mem.wall_watch_id = hero.id if turn.day >= 4 and hero else None
    if previous != mem.wall_watch_id:
        logging.getLogger(__name__).info(
            'round=%s night_watch_owner=%s->%s fixed_gatling=%s',
            turn.round, previous, mem.wall_watch_id, fixed_operator)
    return hero if mem.wall_watch_id is not None else None


def geometry(turn, sites):
    if not sites or not turn.station:
        return set(), None
    right = sum(p[0] for p in turn.station.cells) / 4 < (turn.width - 1) / 2
    front = (max if right else min)(p[0] for p in sites)
    low, high = min(p[1] for p in sites), max(p[1] for p in sites)
    # Bound the rear as well when the blueprint encloses the base. Sparse
    # explicit front-only layouts retain their existing patrol area.
    base_x = [p[0] for p in turn.station.cells]
    rear = (min if right else max)(p[0] for p in sites)
    enclosed = rear < min(base_x) if right else rear > max(base_x)
    # Blueprint boundaries survive destruction; the gate is for transit,
    # not an outside patrol route through a breach or around the rear.
    inside = {(x, y) for x in range(turn.width) for y in range(low + 1, high)
              if (x < front if right else x > front)
              and (not enclosed or (x > rear if right else x < rear))}
    return inside, front


def watch_route(turn, nav, ledger, mem, hero, sites, target=None, *, standing=None,
                with_endpoint=False):
    inside, _ = geometry(turn, sites)
    goals = (set(neighbours(target.pos)) if target else
             {p for wall in sites for p in neighbours(wall)}) & inside
    if standing is not None:
        goals &= {standing}
    reserved = set(ledger.reserved)
    post = (getattr(ledger, 'daytime_gunner_post', mem.gunner_post)
            if turn.is_day else mem.gunner_post)
    if post:
        reserved.add(post)
    if hero.pos in inside:
        reserved.update((x, y) for x in range(turn.width) for y in range(turn.height)
                        if (x, y) not in inside)
    return nav.search(hero, goals, reserved, with_endpoint=with_endpoint)


def staging_wall(turn, sites):
    walls = [w for w in turn.ours if w.kind == 'wall' and w.pos in sites]
    if not walls:
        return None
    from .wall_health import WALL_MAX_HEALTH
    return min(walls, key=lambda w: (w.health / WALL_MAX_HEALTH[w.level - 1],
                                     turn.base_distance(w.pos), w.id))


def prepare_watch(turn, cfg, mem, nav, ledger, hero, sites):
    """Return (worker locked, gold reserved); retain early daytime production."""
    if not hero or hero.id in ledger.used:
        return False, 0
    ledger.watch_pack_slots[hero.id] = 0
    walls = [w for w in turn.ours if w.kind == 'wall']
    if not walls:
        return False, 0
    held = hero.inventory['WallFixer']
    price = turn.shop.get('WallFixer')
    # Daylight shopping only prepares the adaptive night reserve.
    target = min(mem.wall_watch.stock_target(turn), hero.capacity)
    need = max(0, target - held)
    # Guarantee up to three packs first. Optional stock must leave money for
    # missing weapons, the next firepower upgrade and a needed wall voucher.
    upgrades = [turn.shop[name] for w in turn.weapons if w.level < 3
                if (name := f'WeaponUpgradeVoucher{w.level}') in turn.shop and turn.shop[name] > 0]
    core_budget = (max(0, len(cfg.loadout) - len(turn.weapons)) * cfg.weapon_cost
                   + min(upgrades, default=0))
    from .economy import wall_purchase_allowed, voucher_for, refresh_stone_reserves, batch_sale_ready
    wall_prices = [turn.shop[name] for wall in walls
                   if wall_purchase_allowed(turn, wall, mem)
                   and (name := voucher_for(wall)) in turn.shop and turn.shop[name] > 0]
    wall_budget = min(wall_prices, default=0)
    previous = mem.wall_watch.history[-1] if mem.wall_watch.history else None
    safety_stock = max(3, previous.used + 2) if previous and previous.unmet else 3
    from .spending import first_day_purchase_reserve
    available = max(0, ledger.gold - first_day_purchase_reserve(turn, mem, ledger))
    if price is not None and price > 0:
        minimum = max(0, min(safety_stock, target) - held)
        quota = min(need, available // price,
                    max(minimum, max(0, available - core_budget - wall_budget) // price))
    else:
        quota = need if price == 0 else 0
    funds = quota * price if price is not None and price > 0 else 0

    def report(action, reason, **extra):
        mem.wall_watch.decision(turn, 'wall_supply', actor=hero.id, action=action, reason=reason,
                                target=target, held=held, need=need, affordable=quota,
                                safety_stock=min(safety_stock, target),
                                reserved_gold=funds, core_budget=core_budget,
                                wall_budget=wall_budget,
                                gold=ledger.gold, price=price, **extra)

    stage = staging_wall(turn, sites)
    home = watch_route(turn, nav, ledger, mem, hero, sites, stage)
    if home is None:
        home = watch_route(turn, nav, ledger, mem, hero, sites)
    if home is None:
        # A friendly body in the gate is not a daytime change of profession.
        # Estimate the recall deadline with allies vacated, but never issue a
        # movement using this optimistic route. Actual jobs still check occupancy.
        original = turn.blocked
        try:
            turn.blocked = original - {h.pos for h in turn.heroes if h.id != hero.id}
            future_home = watch_route(turn, nav, ledger, mem, hero, sites)
        finally:
            turn.blocked = original
        if (future_home is not None and turn.tick < cfg.economy_rounds
                and turn.day_left > future_home[0] + 2 * cfg.return_margin):
            report('work', 'temporary_home_blocked', steps=future_home[0])
            return False, 0
        report('wait', 'no_home_route')
        # Keep ownership of the watch assignment while another actor briefly
        # blocks the corridor. Releasing it to the ordinary worker planner
        # alternates between a mining/home move and another attempted recall.
        ledger.daytime_waits[hero.id] = 'no_home_route'
        return True, 0
    shops = []
    for point, kind in turn.zones.items():
        if (kind != 'weaponShop' or (hero.id, 'WallFixer') in mem.buy_failures
                or mem.movement.avoids(hero.id, point)):
            continue
        for cell in neighbours(point):
            route = nav.search(hero, {cell}, ledger.reserved)
            if route is None:
                continue
            back = watch_route(turn, nav, ledger, mem, replace(hero, pos=cell), sites, stage)
            if back is None:
                back = watch_route(turn, nav, ledger, mem, replace(hero, pos=cell), sites)
            if back:
                shops.append((route[0] + back[0] + 1, route, point))
    previous_job = mem.daytime_jobs.get(hero.id, {})
    previous_shop = (previous_job.get('target') if previous_job.get('kind') == 'buy'
                     and previous_job.get('name') == 'WallFixer' else None)
    feasible_shops = [o for o in shops if o[0] + cfg.return_margin < turn.day_left]
    shop = min(feasible_shops, key=lambda o: (o[2] != previous_shop, o[0]), default=None)
    if shop is None:
        shop = min(shops, key=lambda o: o[0], default=None)
    feasible_stock_trip = bool(shop and shop[0] + cfg.return_margin < turn.day_left)
    if not feasible_stock_trip:
        funds = 0
    else:
        ledger.watch_pack_slots[hero.id] = quota
    refresh_stone_reserves(turn, cfg, mem, ledger)
    ores = sale_inventory(turn, mem, hero)
    trips = []
    if ores and stage:
        vendors = [[p] for p, kind in turn.zones.items() if kind == 'vendor']
        shops_for_sale = [[p] for p, kind in turn.zones.items() if kind == 'weaponShop'] if quota else []
        inside, _ = geometry(turn, sites)
        landing = {p for wall in sites for p in neighbours(wall)} & inside
        trips = [via(nav, hero, [vendor, *([stop] if stop else []), landing],
                     ledger.reserved, final_exact=True)
                 for vendor in vendors for stop in (shops_for_sale or [None])]
    buying = bool(quota and feasible_stock_trip)
    from .economy import wall_upgrade_allowed, upgrade_order, critical_station
    carried = hero.inventory.copy()
    deliveries = []
    for building in sorted((w for w in turn.ours if voucher_for(w)),
                           key=lambda w: (upgrade_order(turn, w, mem), w.id)):
        name = voucher_for(building)
        if (not name or not carried[name] or not wall_upgrade_allowed(turn, building, mem, paid=True)
                or building.kind == 'station' and not critical_station(turn, building, mem)):
            continue
        carried[name] -= 1
        deliveries.append(building.pos)
    if deliveries:
        # Finish one shop visit before departure, then protect its paid
        # delivery from later increases in the team's gold/stock quota.
        minimum = max(0, min(safety_stock, target) - held)
        count = min(quota, minimum, hero.space)
        landing, _ = geometry(turn, sites)
        trip = via(nav, hero, [[p] for p in deliveries] + [landing],
                   ledger.reserved, final_exact=True)
        can_top_up = bool(count and buying and shop[1][0] == 0
                          and trip is not None
                          and trip + len(deliveries) + 1 + cfg.return_margin < turn.day_left
                          and not (hero.health <= 165 and hero.inventory['Medicine']))
        ledger.watch_pack_slots[hero.id] = count if can_top_up else 0
        if can_top_up and ledger.add(hero.id, command('buy', name='WallFixer', num=count)):
            ledger.explain(hero.id, 'finish_shared_shop_purchase', shop=shop[2],
                           item='WallFixer', quantity=count, pending_deliveries=deliveries)
            report('buy', 'same_shop_safety_stock', count=count, pending_deliveries=deliveries)
            return True, 0
        from .economy import use_inventory
        target_only = previous_job.get('target') if previous_job.get('kind') == 'use' else None
        if use_inventory(turn, nav, ledger, hero, mem=mem, target_only=target_only):
            report('use', 'continue_paid_delivery', pending_deliveries=deliveries)
            return True, 0
        report('work', 'continue_paid_delivery', pending_deliveries=deliveries)
        return False, 0
    if (buying and previous_job.get('kind') == 'buy'
            and 'UpgradeVoucher' in previous_job.get('name', '')):
        # The voucher and its safety packs share a visit. Reserve the packs
        # once, let the existing voucher order arrive/buy, then top up in place.
        count = min(quota, max(0, min(safety_stock, target) - held))
        ledger.watch_pack_slots[hero.id] = count
        funds = count * price if price is not None and price > 0 else 0
        report('reserve', 'shared_shop_order', pending_item=previous_job['name'], count=count)
        return False, funds
    budget = (shop[0] if buying else home[0]) + cfg.return_margin + (len(ORES) if buying else 1)
    sale_trip = min((trip for trip in trips if trip is not None), default=None)
    if sale_trip is not None:
        budget = max(budget, sale_trip + len(ores) + int(bool(quota)) + cfg.return_margin + 1)
    # A distant single-pack order is not a reason to give up a whole workday.
    # Finish an accepted order, buy a useful nearby batch, or leave two turns
    # before the last feasible full trip. Unfunded trips charge only the return.
    firepower_ready = bool(turn.weapons and all(w.level >= 3 for w in turn.weapons))
    near_batch = bool(buying and shop[1][0] <= 2 and (quota >= 2 or shop[1][0] == 0))
    continuing_order = bool(buying and previous_shop is not None)
    paid_work = (held >= mem.wall_watch.stock_target(turn)
                 and (hero.inventory['stone'] >= cfg.wall_stones
                      or any('UpgradeVoucher' in k for k in hero.backpack)))
    if (hero.id == ledger.income_worker_id and held >= min(safety_stock, target)
            and turn.day_left > home[0] + cfg.return_margin + 2):
        ledger.watch_pack_slots[hero.id] = 0
        funds = 0
        report('work', 'income_worker_reserved', steps=home[0])
        return False, 0
    if (not paid_work
            and (maxed_weapon_mining_phase(turn, cfg)
                 or turn.day_left > budget + 2 and not continuing_order
                 and not (near_batch and (firepower_ready or turn.tick >= cfg.economy_rounds)))):
        # Reserve mandatory safety packs while producing; optional stock is
        # funded when the shopping journey is actually dispatched.
        safety_quota = min(quota, max(0, min(safety_stock, target) - held)) if buying else 0
        funds = safety_quota * price if price is not None and price > 0 else 0
        ledger.watch_pack_slots[hero.id] = safety_quota
        report('reserve', 'daytime_production', trip_steps=budget,
               deferred_quantity=quota, return_steps=home[0])
        return False, funds
    if hero.health <= 165 and hero.inventory['Medicine']:
        added = ledger.add(hero.id, command('use', name='Medicine'))
        if not added:
            ledger.daytime_waits[hero.id] = 'command_rejected'
            ledger.watch_pack_slots[hero.id] = 0
        return True, funds if added else 0
    if held >= mem.wall_watch.stock_target(turn):
        from .economy import use_inventory
        from .worker_jobs import finish_wall_work
        if use_inventory(turn, nav, ledger, hero, mem=mem):
            report('use', 'paid_delivery_before_optional_sale')
            return True, 0
        if finish_wall_work(turn, cfg, mem, nav, ledger, hero, sites):
            report('build', 'wall_work_before_optional_sale')
            return True, 0
    # Sell the watcher's own ore before shopping, including while carrying
    # packs. Keep enough daylight for the vendor, shop and inside return.
    sale_value = sum(count * turn.prices[kind] for kind, count in ores.items())
    funding_pack = (price is not None and price > 0
                    and quota < max(0, min(safety_stock, target) - held)
                    and (ledger.gold + sale_value) // price > ledger.gold // price)
    # A tiny new load must not restart a distant sale after every fallback
    # pickup. Selling to unlock another safety pack or clear its slots remains
    # a defence need; otherwise use the ordinary batch/deadline contract.
    sale_ready = (batch_sale_ready(turn, cfg, mem, nav, ledger, hero)
                  or funding_pack or hero.space < quota
                  or hero.id not in mem.sold_workers and turn.day_left <= budget)
    if ores and stage and sale_ready:
        if any(trip is not None and trip + len(ores) + int(bool(quota))
               + cfg.return_margin < turn.day_left for trip in trips):
            if earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False,
                    vendor_only=mem.sale_targets.get(hero.id) if hero.id in mem.sale_workers else None):
                report('sell', 'liquidate_before_stock')
                return True, funds
    if quota and shop:
        if shop[0] + cfg.return_margin < turn.day_left:
            if hero.space < quota and any(hero.inventory[k] for k in ORES):
                # Clear ore through the existing once-daily sale contract.
                if earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False,
                        vendor_only=mem.sale_targets.get(hero.id) if hero.id in mem.sale_workers else None):
                    report('sell', 'clear_pack_slots')
                    return True, funds
            count = min(quota, hero.space)
            if count:
                route = shop[1]
                added = ledger.add(hero.id, command('move', route[1]) if route[1] else
                                   command('buy', name='WallFixer', num=count))
                if not added:
                    funds = 0
                    ledger.daytime_waits[hero.id] = 'command_rejected'
                    ledger.watch_pack_slots[hero.id] = 0
                elif route[1] is not None:
                    ledger.remember_work(hero, 'buy', shop[2], name='WallFixer', quantity=count)
                    ledger.purchases.add('WallFixer')
                    funds = count * price if price is not None and price > 0 else 0
                if added:
                    ledger.watch_pack_slots[hero.id] = count
                report('move' if route[1] else 'buy', 'restock' if added else 'command_rejected', count=count)
                return True, funds if route[1] else 0
    ledger.watch_pack_slots[hero.id] = 0
    inside, _ = geometry(turn, sites)
    if (not held and hero.pos in inside and turn.zones.get(mem.mine_targets.get(hero.id)) in ORES
            and turn.day_left > home[0] + cfg.return_margin + 1):
        # Continue a short inside fallback instead of alternating a mining
        # move with an unconditional empty-handed recall. The final daytime
        # pass rechecks the return budget and recalls when work no longer fits.
        ledger.daytime_waits[hero.id] = 'empty_watch'
        report('work', 'resume_local_production', steps=home[0])
        return True, 0
    if turn.day_left > home[0] + cfg.return_margin + 2:
        report('work', 'use_remaining_daylight', steps=home[0])
        return False, 0
    if home[1] is not None:
        if not ledger.add(hero.id, command('move', home[1])):
            ledger.daytime_waits[hero.id] = 'command_rejected'
    else:
        ledger.daytime_waits[hero.id] = 'watch_ready' if held else 'empty_watch'
    report('move' if home[1] else 'hold',
           'return_with_stock' if held else 'return_without_stock', steps=home[0])
    return True, 0


def repair_watch(turn, mem, nav, ledger, hero, sites):
    if turn.is_day:
        return False
    inside, front = geometry(turn, sites)
    state = mem.wall_watch
    key = turn.day, hero.id
    if state.patrol_key != key:
        state.patrol_key, state.patrol_target, state.patrol_post = key, None, None
    options, waiting, critical = [], [], []
    # Stunned robots may wake next turn: stop spending, but retain late watch.
    hostile = [r for r in turn.robots if turn.threatens_us(r)]
    held = hero.inventory['WallFixer']
    pressure = False
    for wall in turn.ours:
        if wall.kind != 'wall' or wall.pos not in sites or wall.id in ledger.repair_claims:
            continue
        risk = repair_risk(turn, wall, mem)
        pressure |= bool(risk.nearby)
        if risk.needed:
            critical.append(wall)
        if not held:
            if risk.needed:
                mem.wall_watch.shortage(turn, wall, 'no_pack')
            continue
        standing = state.patrol_post if wall.id == state.patrol_target else None
        route = watch_route(turn, nav, ledger, mem, hero, sites, wall,
                            standing=standing, with_endpoint=True)
        if route is None and standing is not None:
            route = watch_route(turn, nav, ledger, mem, hero, sites, wall, with_endpoint=True)
        if route is None:
            if risk.needed:
                mem.wall_watch.shortage(turn, wall, 'no_route')
            continue
        rate = max(risk.recent_damage, risk.nearby_damage)
        lifetime = wall.health / rate if rate else float('inf')
        # Urgency is a property of the wall. Subtracting the worker's walk
        # promotes the farther wall after every step, even with unchanged HP.
        urgent = risk.emergency or bool(risk.nearby and lifetime <= 1)
        rank = (0 if risk.emergency else 1 if urgent else 2, wall.pos[0] != front,
                wall.health / risk.maximum, route[0], wall.id)
        item = (rank, wall, route, risk)
        if risk.needed:
            options.append(item)
        elif ((wall.health * 10 <= risk.maximum * 4)
              or hostile and (turn.day >= 8 or risk.nearby and lifetime <= 3)):
            waiting.append(item)
    selected = min(options or waiting, key=lambda o: o[0], default=None)
    previous = next((o for o in (options or waiting) if o[1].id == state.patrol_target), None)
    if previous and selected and previous[0][0] <= selected[0][0]:
        selected = previous
    if selected:
        _, wall, route, risk = selected
        action = 'move' if route[1] else 'use' if risk.needed else 'hold'
        accepted = (action == 'hold' or ledger.add(hero.id, command('move', route[1]) if route[1]
                                                 else command('use', wall.pos, name='WallFixer')))
        if accepted:
            state.patrol_target, state.patrol_post = wall.id, route[2]
            if action == 'hold':
                ledger.used.add(hero.id)
                ledger.reserved.add(hero.pos)
            ledger.explain(hero.id, 'wall_watch_' + ('repair' if risk.needed else 'preposition'),
                           wall=wall.id, target=wall.pos, post=route[2], steps=route[0],
                           health=wall.health, threshold=risk.threshold, held=held,
                           retained_target=selected is previous, emergency=selected[0][0] < 2)
        mem.wall_watch.decision(turn, 'wall_watch_decision', actor=hero.id, wall=wall.id,
                                health=wall.health, threshold=risk.threshold, held=held,
                                recent_damage=risk.recent_damage, nearby=risk.nearby,
                                steps=route[0], action=action, accepted=accepted,
                                reason=risk.reason if risk.needed else 'preposition')
        return accepted
    state.patrol_target, state.patrol_post = None, None
    threatened = bool(critical or pressure or any(distance(hero.pos, r.pos) <= r.attack_range + 2
                                                 for r in hostile))
    if not held and not threatened:
        ledger.explain(hero.id, 'wall_watch_empty_released', held=0)
        mem.wall_watch.decision(turn, 'wall_watch_decision', actor=hero.id, action='release',
                                reason='no_pack_safe_work', held=0)
        return False
    # Stay at a valid inside post instead of chasing the weakest wall without
    # a pack or a repair target. An outside watcher still returns to shelter.
    stage = staging_wall(turn, sites)
    home = (watch_route(turn, nav, ledger, mem, hero, sites, stage)
            if hero.pos not in inside and stage else None)
    action = 'move' if home and home[1] is not None else 'hold'
    if action == 'move':
        ledger.add(hero.id, command('move', home[1]))
    elif hero.pos in inside:
        ledger.used.add(hero.id)
        ledger.reserved.add(hero.pos)
    reason = 'no_pack' if not held else 'no_route' if critical else 'inside_watch'
    ledger.explain(hero.id, 'wall_watch_' + reason, held=held, critical=[w.id for w in critical])
    mem.wall_watch.decision(turn, 'wall_watch_decision', actor=hero.id, action=action,
                            reason=reason, held=held, critical=[w.id for w in critical])
    return True

