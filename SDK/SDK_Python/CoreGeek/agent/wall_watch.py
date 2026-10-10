"""Fourth-night wall watch, subordinate to the existing gunner assignment."""
from dataclasses import replace
import logging
from .commands import command
from .model import neighbours, ORES
from .mining import earn, sale_inventory
from .economy_plan import via
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


def watch_route(turn, nav, ledger, mem, hero, sites, target=None):
    inside, _ = geometry(turn, sites)
    goals = (set(neighbours(target.pos)) if target else
             {p for wall in sites for p in neighbours(wall)}) & inside
    reserved = set(ledger.reserved)
    post = (getattr(ledger, 'daytime_gunner_post', mem.gunner_post)
            if turn.is_day else mem.gunner_post)
    if post:
        reserved.add(post)
    if hero.pos in inside:
        reserved.update((x, y) for x in range(turn.width) for y in range(turn.height)
                        if (x, y) not in inside)
    return nav.search(hero, goals, reserved)


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
    from .wall_health import needs_day_repair
    # Buy planned daylight repairs in addition to the adaptive night reserve.
    repairs = [w for w in walls if needs_day_repair(w, turn) and w.health >= 500
               and w.level == 3 and watch_route(turn, nav, ledger, mem, hero, sites, w)]
    target = min(mem.wall_watch.stock_target(turn) + len(repairs), hero.capacity)
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
    if price is not None and price > 0:
        minimum = max(0, min(safety_stock, target) - held)
        quota = min(need, ledger.gold // price,
                    max(minimum, max(0, ledger.gold - core_budget - wall_budget) // price))
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
    budget = (shop[0] if shop else home[0]) + cfg.return_margin + len(ORES)
    sale_trip = min((trip for trip in trips if trip is not None), default=None)
    if sale_trip is not None:
        budget = max(budget, sale_trip + len(ores) + int(bool(quota)) + cfg.return_margin + 1)
    # A funded stock trip starts immediately once firepower is ready. Before
    # that, recall is determined by this complete trip, including sale, not a
    # fixed tick 60 switch. Keep two turns before the last feasible departure.
    firepower_ready = bool(turn.weapons and all(w.level >= 3 for w in turn.weapons))
    if (turn.tick < cfg.economy_rounds and turn.day_left > budget + 2
            and not (quota and feasible_stock_trip and firepower_ready)):
        report('reserve', 'daytime_production')
        return False, funds
    if hero.health <= 165 and hero.inventory['Medicine']:
        added = ledger.add(hero.id, command('use', name='Medicine'))
        if not added:
            ledger.daytime_waits[hero.id] = 'command_rejected'
            ledger.watch_pack_slots[hero.id] = 0
        return True, funds if added else 0
    if held and any(w.health < 500 for w in walls):
        from .economy import use_inventory
        if use_inventory(turn, nav, ledger, hero, mem=mem):
            report('repair', 'daytime_low_wall')
            return True, 0
    if held > mem.wall_watch.stock_target(turn) and repairs:
        from .economy import use_inventory
        if use_inventory(turn, nav, ledger, hero, mem=mem, name_only='WallFixer'):
            report('repair', 'pre_wave_repair')
            return True, 0
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
    if not held and hero.pos in inside and turn.zones.get(mem.mine_targets.get(hero.id)) in ORES:
        # Continue a short inside fallback instead of alternating a mining
        # move with an unconditional empty-handed recall. The final daytime
        # pass rechecks the return budget and recalls when work no longer fits.
        ledger.daytime_waits[hero.id] = 'empty_watch'
        report('work', 'resume_local_production', steps=home[0])
        return True, 0
    if (held >= mem.wall_watch.stock_target(turn)
            and turn.day_left > home[0] + 2 * cfg.return_margin):
        report('work', 'stock_ready_use_remaining_daylight', steps=home[0])
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
    _, front = geometry(turn, sites)
    options, waiting, critical = [], [], []
    # Stunned robots may wake next turn: stop spending, but retain late watch.
    hostile = [r for r in turn.robots if turn.threatens_us(r)]
    held = hero.inventory['WallFixer']
    for wall in turn.ours:
        if wall.kind != 'wall' or wall.pos not in sites or wall.id in ledger.repair_claims:
            continue
        risk = repair_risk(turn, wall, mem)
        if risk.needed:
            critical.append(wall)
        if not held:
            if risk.needed:
                mem.wall_watch.shortage(turn, wall, 'no_pack')
            continue
        route = watch_route(turn, nav, ledger, mem, hero, sites, wall)
        if route is None:
            if risk.needed:
                mem.wall_watch.shortage(turn, wall, 'no_route')
            continue
        rate = max(risk.recent_damage, risk.nearby_damage)
        slack = wall.health / rate - route[0] - 1 if rate else float('inf')
        # A threatened flank that cannot survive another turn outranks frontage.
        rank = (not (risk.emergency or slack <= 0),
                slack if slack <= 0 else 0, wall.pos[0] != front,
                wall.health / risk.maximum, route[0], wall.id)
        item = (rank, wall, route, risk)
        if risk.needed:
            options.append(item)
        elif ((wall.health * 10 <= risk.maximum * 4)
              or hostile and (turn.day >= 8 or risk.nearby and slack <= 2)):
            waiting.append(item)
    selected = min(options or waiting, key=lambda o: o[0], default=None)
    if selected:
        _, wall, route, risk = selected
        action = 'move' if route[1] else 'use' if risk.needed else 'hold'
        accepted = (action == 'hold' or ledger.add(hero.id, command('move', route[1]) if route[1]
                                                 else command('use', wall.pos, name='WallFixer')))
        mem.wall_watch.decision(turn, 'wall_watch_decision', actor=hero.id, wall=wall.id,
                                health=wall.health, threshold=risk.threshold, held=held,
                                recent_damage=risk.recent_damage, nearby=risk.nearby,
                                steps=route[0], action=action, accepted=accepted,
                                reason=risk.reason if risk.needed else 'preposition')
        return accepted
    stage = staging_wall(turn, sites)
    home = watch_route(turn, nav, ledger, mem, hero, sites, stage) if stage else None
    action = 'move' if home and home[1] is not None else 'hold'
    if action == 'move':
        ledger.add(hero.id, command('move', home[1]))
    reason = 'no_pack' if not held else 'no_route' if critical else 'inside_watch'
    mem.wall_watch.decision(turn, 'wall_watch_decision', actor=hero.id, action=action,
                            reason=reason, held=held, critical=[w.id for w in critical])
    return True

