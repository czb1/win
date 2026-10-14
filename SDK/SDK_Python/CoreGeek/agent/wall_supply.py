"""Day-four watch ownership and one shop visit for the whole wall order."""
from collections import Counter
from .commands import command
from .model import HEROES, WEAPONS, neighbours
from .economy_plan import via
from .wall_health import WALL_MAX_HEALTH

PACK = 'WallFixer'
V1 = 'WallUpgradeVoucher1'
V2 = 'WallUpgradeVoucher2'


def priority_wall(turn, mem, wall, sites):
    """Front, damaged and previously pressured walls keep upgrade priority."""
    from .economy_plan import front_sites
    return (wall.pos in front_sites(turn, sites) or mem.wall_hits.get(wall.pos, 0)
            or wall.health < WALL_MAX_HEALTH[wall.level - 1]
            or wall.pos in mem.wall_rebuild_levels)


def defer_side_upgrades(turn, mem):
    """Only the sixth-day defense-ready economy reserves an assault window."""
    from .robot_assault import enemy_base
    previous = mem.wall_watch.history[-1] if mem.wall_watch.history else None
    return bool(turn.day == 6 and mem.wall_watch_id is not None and enemy_base(turn)
                and turn.weapons and all(w.level == 3 for w in turn.weapons)
                and not (previous and (previous.unmet or previous.lost)))


def active(turn, mem):
    return bool(turn.day >= 4 and mem and mem.wall_watch_id is not None
                and any(h.id == mem.wall_watch_id for h in turn.workers)
                and (getattr(turn, 'planned_wall_sites', ())
                     or any(w.kind == 'wall' for w in turn.ours)))


def requirements(turn, mem, hero, sites):
    """Stock actual walls and priority front/side breaches, net of all stock."""
    from .economy_plan import front_sites, wall_level_limit
    from .navigation import wall_gaps
    walls = {w.pos: w for w in turn.ours if w.kind == 'wall'}
    structures = {p for b in (*turn.ours, *turn.enemies) if b.kind not in HEROES for p in b.cells}
    front = set(front_sites(turn, sites))
    rear_x = (min(p[0] for p in sites) if turn.station and turn.station.pos[0] < turn.width / 2
              else max(p[0] for p in sites)) if sites else None
    # Share the material builder's breach evidence. Untouched expansion,
    # especially the unfinished rear, is not a prepaid future L1 wall.
    breaches = wall_gaps(turn, sites, mem.wall_hits) | set(mem.wall_rebuild_levels)
    gaps = {p for p in set(sites) & breaches if p not in structures
            and p not in mem.build_failures and turn.zones.get(p, 'land') == 'land'
            and (p in front or p[0] != rear_x)}
    one = sum(w.level == 1 for w in walls.values()) + len(gaps)
    two = sum(w.level == 2 for w in walls.values())
    held = Counter(item for h in turn.heroes for item in h.backpack)
    key_walls = sum(1 for w in walls.values() if w.level < 3
                    and wall_level_limit(turn, w.pos, sites) == 3)
    front_three = max(0, key_walls + len(gaps & front) - held[V2])
    key_one = sum(w.level == 1 and priority_wall(turn, mem, w, sites)
                  for w in walls.values()) + len(gaps)
    target = min(hero.capacity, mem.wall_watch.stock_target(turn))
    previous = mem.wall_watch.history[-1] if mem.wall_watch.history else None
    safety = min(target, max(3, previous.used + 2) if previous and previous.unmet else 3)
    return dict(one=one, two=two, gaps=len(gaps),
                demand={V1: max(0, one - held[V1]),
                        V2: front_three,
                        PACK: max(0, target - hero.inventory[PACK])},
                safety=max(0, safety - hero.inventory[PACK]), target=target,
                key_three=front_three,
                key_one=max(0, key_one - held[V1]),
                stockout=bool(previous and previous.unmet))


def shopping_list(turn, mem, hero, sites, gold, space):
    """Minimum packs, L2 prerequisites, then front-only L3/extra packs."""
    report = requirements(turn, mem, hero, sites)
    demand, order = report['demand'], Counter()
    gold, space = max(0, gold), max(0, space)

    def allocate(name, amount):
        nonlocal gold, space
        price = turn.shop.get(name, -1)
        if price < 0 or (hero.id, name) in mem.buy_failures:
            return
        count = min(max(0, amount), demand[name] - order[name], space,
                    gold // price if price else amount)
        if count > 0:
            order[name] += count
            gold -= count * price
            space -= count

    # Sixth-day offense defers only healthy, unpressured side/rear walls.
    deferred = defer_side_upgrades(turn, mem)
    one = report['key_one'] if deferred else demand[V1]
    two = report['key_three'] if deferred else demand[V2]
    allocate(PACK, report['safety'])
    allocate(V1, one)
    # Never fund L3 while an L2 prerequisite remains unpaid.
    if order[V1] == one and not report['stockout']:
        allocate(V2, report['key_three'])
    allocate(PACK, demand[PACK])
    if order[V1] == one:
        allocate(V2, max(0, two - order[V2]))
    report['deferred'] = {V1: demand[V1] - one, V2: demand[V2] - two} if deferred else {}
    report.update(order=dict(order), cost=sum(n * turn.shop[k] for k, n in order.items()),
                  unfunded={k: n - order[k] for k, n in demand.items() if n > order[k]})
    return report


def core_reserve(turn, cfg):
    """Keep unfinished firepower funded, as in the existing stock policy."""
    held = Counter(item for h in turn.heroes for item in h.backpack)
    prices = [turn.shop[name] for w in turn.weapons if w.level < 3
              if (name := f'WeaponUpgradeVoucher{w.level}') in turn.shop
              and not held[name] and turn.shop[name] > 0]
    return max(0, len(cfg.loadout) - len(turn.weapons)) * cfg.weapon_cost + min(prices, default=0)


def _home(turn, mem, nav, ledger, hero, sites):
    from .wall_watch import geometry, watch_route
    inside, _ = geometry(turn, sites)
    goals = ({p for wall in sites for p in neighbours(wall)} & inside
             - set(ledger.operator_posts.values()))
    post = getattr(ledger, 'daytime_gunner_post', mem.gunner_post)
    goals.discard(post)
    route = watch_route(turn, nav, ledger, mem, hero, sites)
    return goals, route


def request_wall_passage(turn, cfg, mem, nav, ledger, hero, sites):
    """Expose the supplier's return even when its ordinary work job was cleared."""
    if (not turn.is_day or not hero or hero.id in ledger.used
            or turn.tick < cfg.economy_rounds or not active(turn, mem)):
        return
    home, back = _home(turn, mem, nav, ledger, hero, sites)
    from .traffic import transit_cells
    safe_home = home - transit_cells(turn, nav, ledger)
    goals = safe_home or home
    if not goals or hero.pos in goals:
        return
    lease = mem.traffic_leases.get(hero.id, {})
    if back is not None and lease.get('job', (None,))[0] != 'wall_supply_home':
        return
    ledger.traffic_requests[hero.id] = dict(
        goals=goals, signature=('wall_supply_home', hero.id), urgency=0,
        local_work=any(w.kind == 'wall' and w.level < 3 and w.pos in sites
                       and turn.adjacent(hero.pos, w.pos)
                       and hero.inventory[V1 if w.level == 1 else V2] for w in turn.ours),
        advance=mem.wall_supply_trip.get('phase') in ('deliver', 'return'))


def _shops(turn, cfg, mem, nav, ledger, hero, home, actions, preferred=None):
    options = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
            continue
        route = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
        if route is None:
            continue
        trip = via(nav, hero, [[shop], home], ledger.reserved, final_exact=True)
        if trip is not None and trip + actions + cfg.return_margin + 1 <= turn.day_left:
            options.append((shop != preferred, route[0], shop, route))
    return sorted(options)


def prepare(turn, cfg, mem, nav, ledger, hero, sites):
    """Own the watcher's full day; reserve the unbought order exactly once."""
    from .mining import earn, sale_inventory
    from .economy import use_inventory, batch_sale_ready, wall_upgrade_allowed
    previous_job = mem.daytime_jobs.get(hero.id, {})
    state = mem.wall_supply_trip
    if state.get('key') != (turn.day, hero.id):
        state.clear()
        state.update(key=(turn.day, hero.id), phase='mine',
                     shop=previous_job.get('target') if previous_job.get('kind') == 'buy' else None,
                     pending=None)
    mem.wall_batches.pop(hero.id, None)
    if turn.tick >= cfg.economy_rounds or previous_job.get('kind') not in ('mine', 'sell', 'spare'):
        mem.daytime_jobs.pop(hero.id, None)
    mem.stone_reserves[hero.id] = 0
    if ledger.income_worker_id == hero.id:
        ledger.income_worker_id = None
    home, back = _home(turn, mem, nav, ledger, hero, sites)
    report = shopping_list(turn, mem, hero, sites,
                           ledger.gold - core_reserve(turn, cfg), hero.space)
    ledger.watch_pack_slots[hero.id] = report['order'].get(PACK, 0)

    def finish(reason, reserve=0):
        ledger.explain(hero.id, 'wall_supply_trip', phase=state['phase'], reason_detail=reason,
                       shop=state['shop'], reserved_gold=min(ledger.gold, reserve), **report)
        mem.wall_watch.decision(turn, 'wall_supply_trip', actor=hero.id,
                                phase=state['phase'], reason=reason,
                                action=ledger.commands.get(str(hero.id), {}).get('action'),
                                reserved_gold=min(ledger.gold, reserve), **report)
        return True, min(ledger.gold, reserve)

    if hero.health <= 165 and hero.inventory['Medicine']:
        ledger.add(hero.id, command('use', name='Medicine'))
        return finish('self_heal', report['cost'])
    if hero.health <= 110:
        from .economy import supplies, buy_supply
        if buy_supply(turn, ledger, hero, supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True)):
            return finish('emergency_medicine', min(report['cost'], ledger.gold))
    # No ordinary delivery or shopping can preempt the configured mining window.
    if turn.tick < cfg.economy_rounds:
        safety_count = min(report['safety'], report['order'].get(PACK, 0))
        feasible = bool(home and _shops(turn, cfg, mem, nav, ledger, hero, home, 1))
        ledger.watch_pack_slots[hero.id] = safety_count if feasible else 0
        if back is None:
            original = turn.blocked
            try:
                turn.blocked = original - {h.pos for h in turn.heroes if h.id != hero.id}
                _, future = _home(turn, mem, nav, ledger, hero, sites)
            finally:
                turn.blocked = original
            if future is None:
                ledger.daytime_waits[hero.id] = 'no_home_route'
                return finish('home_route_blocked')
        funds = safety_count * turn.shop.get(PACK, 0) if feasible else 0
        finish('mining_window', funds)
        return False, funds
    if state['phase'] == 'mine':
        state['phase'] = 'sell'
    pending = state.get('pending')
    if pending and pending[0] < turn.round:
        _, name, count, before = pending
        if hero.inventory[name] < before + count:
            # Keep the shopping phase: authoritative inventory confirms receipt.
            state['shop'] = None
        state['pending'] = None
    if back is None:
        # An adjacent paid delivery needs no travel and must not be vetoed by
        # an unrelated teammate currently occupying the inside return route.
        if use_inventory(turn, nav, ledger, hero, mem=mem, local_only=True,
                         upgrades_only=True, target_sites=set(sites)):
            action = ledger.commands[str(hero.id)]['targetPos'][0]
            state['anchor'] = action['x'], action['y']
            return finish('local_delivery_while_waiting_for_passage')
        ledger.daytime_waits[hero.id] = 'no_home_route'
        return finish('home_route_blocked')
    if turn.day_left <= back[0] + cfg.return_margin + 1:
        if back[0] == 0 and use_inventory(turn, nav, ledger, hero, mem=mem,
                                         local_only=True, upgrades_only=True):
            mem.wall_batches.pop(hero.id, None)
            return finish('local_paid_use_before_recall')
        state['phase'] = 'return'

    if state['phase'] == 'deliver' and state['shop'] is None and report['order']:
        # A late task reward may fund the first visit after an unfunded wait.
        # Completed shop visits remain closed until the next day.
        state['phase'] = 'shop'
    if state['phase'] == 'sell':
        ores = sale_inventory(turn, mem, hero)
        # Selling all saleable ore clears capacity and avoids a second shop trip.
        value = sum(n * turn.prices.get(k, 0) for k, n in ores.items())
        future = shopping_list(turn, mem, hero, sites,
                               ledger.gold + value - core_reserve(turn, cfg),
                               hero.space + sum(ores.values()))
        vendors = []
        for vendor, kind in turn.zones.items():
            if kind != 'vendor' or mem.movement.avoids(hero.id, vendor):
                continue
            route = nav.approach(hero, [vendor], ledger.reserved, with_endpoint=True)
            if route is None:
                continue
            for _, _, shop, _ in _shops(turn, cfg, mem, nav, ledger, hero, home,
                                        len(future['order'])):
                trip = via(nav, hero, [[vendor], [shop], home], ledger.reserved,
                           final_exact=True)
                if trip is not None and trip + len(ores) + len(future['order']) + cfg.return_margin + 1 <= turn.day_left:
                    vendors.append((vendor != mem.sale_targets.get(hero.id), route[0], vendor, route))
        need_sale = (batch_sale_ready(turn, cfg, mem, nav, ledger, hero)
                     or hero.space < sum(future['order'].values())
                     or future['cost'] > report['cost'])
        if ores and vendors and need_sale:
            _, _, vendor, route = min(vendors)
            kind = max(ores, key=lambda k: ores[k] * turn.prices.get(k, 0))
            action = command('sell', name=kind, num=ores[kind]) if route[1] is None else command('move', route[1])
            if ledger.add(hero.id, action):
                mem.sale_targets[hero.id] = vendor
                mem.sale_workers.add(hero.id)
                if route[1] is None:
                    mem.sold_workers.add(hero.id)
                ledger.remember_work(hero, 'sell', vendor)
                return finish('sell_before_shop', report['cost'])
        state['phase'] = 'shop'

    if state['phase'] == 'shop':
        options = _shops(turn, cfg, mem, nav, ledger, hero, home,
                         len(report['order']), state['shop']) if report['order'] else []
        if options:
            _, _, shop, route = options[0]
            state['shop'] = shop
            if route[1] is not None:
                if nav.move_along(hero, ledger, route, {route[2]}, targets={shop}, prefer_vehicle=False):
                    ledger.remember_work(hero, 'buy', shop, name=next(iter(report['order'])),
                                         quantity=report['order'][next(iter(report['order']))])
                    return finish('travel_to_shop', report['cost'])
            else:
                # Buy each type in bulk on consecutive turns; never deliver
                # the first voucher before the remaining shop order is bought.
                name = next(iter(report['order']))
                count = report['order'][name]
                if ledger.add(hero.id, command('buy', name=name, num=count)):
                    state['pending'] = turn.round, name, count, hero.inventory[name]
                    return finish('buy_batch', report['cost'] - count * turn.shop[name])
            ledger.daytime_waits[hero.id] = 'command_rejected'
            ledger.watch_pack_slots[hero.id] = 0
            return finish('shop_command_blocked')
        state['phase'] = 'deliver'

    if state['phase'] == 'deliver':
        walls = [w for w in turn.ours if w.kind == 'wall']
        from .economy import dusk_route, wall_sector
        from .wall_batches import face_sites
        feasible = []
        for wall in walls:
            name = V1 if wall.level == 1 else V2 if wall.level == 2 else None
            if (not name or not hero.inventory[name] or wall.id in ledger.upgrade_claims
                    or not wall_upgrade_allowed(turn, wall, mem, paid=True)):
                continue
            route = dusk_route(turn, nav, ledger, hero, wall.cells,
                               home_override=(home, True))
            if route is not None:
                same_face = wall.pos in face_sites(sites, state.get('anchor'))
                feasible.append((not same_face, wall.pos != state.get('anchor'),
                                 wall_sector(turn, wall) != 'front', route[0], wall.health, wall.id, wall, name))
        if feasible:
            *_, wall, name = min(feasible, key=lambda o: o[:6])
            if use_inventory(turn, nav, ledger, hero, mem=mem, target_only=wall.pos, name_only=name):
                state['anchor'] = wall.pos
                mem.wall_batches.pop(hero.id, None)
                return finish('deliver_wall_voucher')
        # Missing planned walls may be built later today. Stay inside and
        # recheck paid vouchers each turn without launching another shop trip.
    if back[1] is not None:
        # With no affordable errands, permit only bounded work inside the
        # perimeter. It cannot reopen an outward shop or ore-selling trip.
        if state['phase'] == 'deliver' and not report['order']:
            from .mining import spare_mine
            from .wall_watch import geometry
            inside, _ = geometry(turn, sites)
            reserved = ledger.reserved | {(x, y) for x in range(turn.width) for y in range(turn.height)
                                          if (x, y) not in inside}
            if hero.pos in inside and spare_mine(turn, cfg, mem, nav, ledger, hero,
                    home=home, exact=True, reserved=reserved, max_steps=2):
                return finish('inside_work_without_purchase')
        ledger.add(hero.id, command('move', back[1]))
    else:
        ledger.daytime_waits[hero.id] = 'watch_ready' if hero.inventory[PACK] else 'empty_watch'
    return finish('return_or_wait_for_wall')


def other_worker(turn, cfg, mem, nav, ledger, hero, towers, sites):
    """One material builder; after its batch, ordinary income and gun work."""
    from .economy import repair_walls, worker, refresh_stone_reserves
    from .mining import earn
    mem.wall_batches.pop(hero.id, None)
    job = mem.daytime_jobs.pop(hero.id, None)
    mem.wall_repair_worker = hero.id
    refresh_stone_reserves(turn, cfg, mem, ledger)
    if hero.health <= 165 and hero.inventory['Medicine']:
        ledger.add(hero.id, command('use', name='Medicine'))
        return
    if hero.health <= 110:
        from .economy import supplies, buy_supply
        if buy_supply(turn, ledger, hero, supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True)):
            return
    from .economy import use_inventory
    from .front_rebuild import act as rebuild_front
    if rebuild_front(turn, cfg, mem, nav, ledger, hero):
        return
    if job and job.get('kind') == 'sell':
        from .worker_jobs import _continue
        if _continue(turn, cfg, mem, nav, ledger, hero, job, towers, sites)[0]:
            return
    if repair_walls(turn, cfg, mem, nav, ledger, [hero], sites):
        mem.wall_batches.pop(hero.id, None)
        return
    # Inventory cannot be transferred. Deliver legacy wall vouchers from the
    # earlier allocator so globally deducted stock does not become stranded.
    if turn.tick >= cfg.economy_rounds and (hero.inventory[V1] or hero.inventory[V2]):
        if use_inventory(turn, nav, ledger, hero, mem=mem, target_sites=set(sites)):
            mem.wall_batches.pop(hero.id, None)
            return
    if len(turn.weapons) < len(cfg.loadout) or any(w.level < 3 for w in turn.weapons):
        worker(turn, cfg, mem, nav, ledger, hero, towers, sites, True,
               develop=True, shopping=True, deadline=cfg.economy_rounds)
        return
    if job and job.get('kind') in ('mine', 'sell', 'spare') and not job.get('want_stone'):
        from .worker_jobs import _continue
        if _continue(turn, cfg, mem, nav, ledger, hero, job, towers, sites)[0]:
            return
    earn(turn, cfg, mem, nav, ledger, hero, deadline=70)


def station_plan(turn, cfg, mem, nav, ledger, hero):
    """Only the pioneer buys base vouchers; protect an active task in place."""
    from .economy import station_purchase_allowed, station_voucher_needed, voucher_for
    from .mining import return_destination
    station = turn.station
    if (turn.day <= 1 or not turn.is_day or not hero or hero.id in ledger.used
            or hero.kind != 'pioneer' or turn.phase_task or not hero.space or not station
            or not station_purchase_allowed(turn, station, mem)):
        return None
    name = voucher_for(station)
    if (not name or name in ledger.purchases or (hero.id, name) in mem.buy_failures
            or turn.shop.get(name, -1) < 0 or ledger.gold < turn.shop[name]
            or not station_voucher_needed(turn, hero, name)):
        return None
    home, exact = return_destination(turn, nav, ledger, hero, mem)
    if not home:
        return None
    choices = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
            continue
        route = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
        trip = via(nav, hero, [[shop], home], ledger.reserved, final_exact=exact)
        if route is not None and trip is not None and trip + 1 + cfg.return_margin <= turn.day_left:
            choices.append((route[0], shop, route))
    if not choices:
        return None
    _, shop, route = min(choices)
    return name, shop, route


def prepare_station(turn, cfg, mem, nav, ledger, hero):
    plan = station_plan(turn, cfg, mem, nav, ledger, hero)
    if not plan:
        return False
    name, shop, route = plan
    accepted = (ledger.add(hero.id, command('buy', name=name, num=1)) if route[1] is None
                else nav.move_along(hero, ledger, route, {route[2]}, targets={shop}, prefer_vehicle=False))
    if accepted:
        if route[1] is not None:
            ledger.gold -= turn.shop[name]
            ledger.purchases.add(name)
        ledger.explain(hero.id, 'pioneer_station_supply', item=name, shop=shop,
                       route_steps=route[0])
    return accepted
