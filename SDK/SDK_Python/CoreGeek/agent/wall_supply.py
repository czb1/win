"""Day-four split worker itineraries and one shared defence budget."""
from collections import Counter
from .commands import command
from .model import HEROES, neighbours
from .economy_plan import via, itinerary_field, itinerary_route
from .wall_health import WALL_MAX_HEALTH

PACK = 'WallFixer'
V1 = 'WallUpgradeVoucher1'
V2 = 'WallUpgradeVoucher2'


def active(turn, mem):
    return bool(turn.day >= 4 and mem and mem.wall_watch_id is not None
                and any(h.id == mem.wall_watch_id for h in turn.workers)
                and (getattr(turn, 'planned_wall_sites', ())
                     or any(w.kind == 'wall' for w in turn.ours)))


def requirements(turn, mem, hero, sites):
    """Unbuilt planned walls count as L1; stock is counted across all carriers."""
    walls = {w.pos: w for w in turn.ours if w.kind == 'wall'}
    structures = {p for b in (*turn.ours, *turn.enemies) if b.kind not in HEROES for p in b.cells}
    gaps = {p for p in sites if p not in structures and p not in mem.build_failures
            and turn.zones.get(p, 'land') == 'land'}
    one = sum(w.level == 1 for w in walls.values()) + len(gaps)
    two = sum(w.level == 2 for w in walls.values())
    held = Counter(item for h in turn.heroes for item in h.backpack)
    from .economy_plan import front_sites
    front = set(front_sites(turn, sites))
    key_walls = sum(1 for w in walls.values() if w.level < 3
                    and (w.pos in front or mem.wall_hits.get(w.pos, 0)
                         or w.health < WALL_MAX_HEALTH[w.level - 1]
                         or w.pos in mem.wall_rebuild_levels))
    key_gaps = sum(bool(p in front or mem.wall_hits.get(p, 0)) for p in gaps)
    target = min(hero.capacity, mem.wall_watch.stock_target(turn))
    previous = mem.wall_watch.history[-1] if mem.wall_watch.history else None
    safety = min(target, max(3, previous.used + 2) if previous and previous.unmet else 3)
    return dict(one=one, two=two, gaps=len(gaps),
                demand={V1: max(0, one - held[V1]),
                        V2: max(0, one + two - held[V2]),
                        PACK: max(0, target - hero.inventory[PACK])},
                safety=max(0, safety - hero.inventory[PACK]), target=target,
                key_three=max(0, key_walls + key_gaps - held[V2]),
                stockout=bool(previous and previous.unmet))


def shopping_list(turn, mem, hero, sites, gold, space):
    """Minimum packs, all affordable L2, then pressure-aware L3/extra packs."""
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

    allocate(PACK, report['safety'])
    allocate(V1, demand[V1])
    # Never fund L3 while an L2 prerequisite remains unpaid.
    if order[V1] == demand[V1] and not report['stockout']:
        allocate(V2, report['key_three'])
    allocate(PACK, demand[PACK])
    if order[V1] == demand[V1]:
        allocate(V2, demand[V2])
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


def builder_home(turn, mem, nav, ledger, hero, sites):
    """A real operator keeps its post; other builders use nearby safe shelter."""
    from .wall_watch import geometry
    from .traffic import transit_cells
    if hero.id in ledger.operator_posts:
        return {ledger.operator_posts[hero.id]}, True
    inside, _ = geometry(turn, sites)
    # Outside work/standby is permitted near our perimeter, away from the
    # enemy approach. Safety is rechecked from visible threats every turn.
    from .economy_plan import front_sites
    front = set(front_sites(turn, sites))
    outside = {p for wall in set(sites) - front for p in neighbours(wall)
               if turn.inside(p) and turn.base_distance(p) <= 5}
    danger = {p for p in inside | outside for r in turn.robots if turn.threatens_us(r)
              and max(abs(p[0]-r.pos[0]), abs(p[1]-r.pos[1])) <= r.attack_range + 2}
    goals = ((inside | outside) - danger - transit_cells(turn, nav, ledger)
             - set(ledger.operator_posts.values()) - ledger.wall_cells - ledger.tower_cells)
    goals.discard(getattr(ledger, 'daytime_gunner_post', mem.gunner_post))
    if goals and nav.search(hero, goals, ledger.reserved) is not None:
        return goals, True
    # Sparse layouts may have no perimeter landing; use the base approach.
    fallback = ({p for c in turn.station.cells for p in neighbours(c)} - turn.station.cells
                if turn.station else set())
    fallback -= (transit_cells(turn, nav, ledger) | ledger.wall_cells | ledger.tower_cells
                 | set(ledger.operator_posts.values()))
    fallback.discard(getattr(ledger, 'daytime_gunner_post', mem.gunner_post))
    return {p for p in fallback if turn.inside(p) and not any(turn.threatens_us(r)
            and max(abs(p[0]-r.pos[0]), abs(p[1]-r.pos[1])) <= r.attack_range + 2
            for r in turn.robots)}, True


def _state(turn, mem, hero):
    root = mem.wall_supply_trip
    if root.get('key') != (turn.day, mem.wall_watch_id):
        root.clear()
        job = mem.daytime_jobs.get(mem.wall_watch_id, {})
        root.update(key=(turn.day, mem.wall_watch_id), phase='mine',
                    shop=job.get('target') if job.get('kind') == 'buy' and job.get('name') == PACK else None,
                    pending=None)
    if hero.id == mem.wall_watch_id:
        return root
    state = root.setdefault('builder', dict(actor=hero.id, phase='mine', shop=None, pending=None))
    if state['actor'] != hero.id:
        state.clear()
        state.update(actor=hero.id, phase='mine', shop=None, pending=None)
    return state


def _receipt(turn, hero, state):
    pending = state.get('pending')
    if pending and pending[0] < turn.round:
        _, name, count, before = pending
        if hero.inventory[name] >= before + count:
            state.setdefault('bought', []).append(name)
        else:
            state['shop'] = None
        state['pending'] = None


def shared_orders(turn, cfg, mem, ledger, sites, forecast_gold=None):
    """Allocate once per turn against observed inventories and one gold pool."""
    if forecast_gold is None and hasattr(ledger, 'wall_orders'):
        return ledger.wall_orders
    watcher = next(h for h in turn.workers if h.id == mem.wall_watch_id)
    builder = next((h for h in turn.workers if h.id != watcher.id), None)
    report = requirements(turn, mem, watcher, sites)
    # Watcher-held legacy wall vouchers cannot be transferred. Only the
    # construction carrier's stock can discharge the construction demand.
    held = builder.inventory if builder else Counter()
    demand = {PACK: report['demand'][PACK], V1: max(0, report['one'] - held[V1]),
              V2: max(0, report['one'] + report['two'] - held[V2])}
    orders = {watcher.id: Counter()}
    if builder:
        orders[builder.id] = Counter()
    gold = max(0, ledger.gold if forecast_gold is None else forecast_gold)
    # Sale clears ore slots before buying, so forecast the cleared capacity.
    from .mining import sale_inventory
    spaces = {h.id: h.space + sum(sale_inventory(turn, mem, h).values()) for h in (watcher, builder) if h}

    def allocate(hero, name, count):
        nonlocal gold
        if not hero or (hero.id, name) in mem.buy_failures or turn.shop.get(name, -1) < 0:
            return
        state = _state(turn, mem, hero)
        _receipt(turn, hero, state)
        if state.get('closed') or name in state.get('bought', ()):
            return
        price = turn.shop[name]
        count = min(max(0, count), demand[name] - orders[hero.id][name], spaces[hero.id],
                    gold // price if price else count)
        if count > 0:
            orders[hero.id][name] += count
            spaces[hero.id] -= count
            gold -= price * count

    allocate(watcher, PACK, report['safety'])
    # Preserve unfinished firepower after minimum medical safety stock.
    gold = max(0, gold - core_reserve(turn, cfg))
    allocate(builder, V1, demand[V1])
    l2_funded = bool(builder and orders[builder.id][V1] == demand[V1])
    if l2_funded and not report['stockout']:
        allocate(builder, V2, max(0, report['key_three']))
    allocate(watcher, PACK, demand[PACK])
    if l2_funded:
        allocate(builder, V2, demand[V2])
    result = {uid: dict(order) for uid, order in orders.items()}
    if forecast_gold is not None:
        return result
    ledger.wall_orders = result
    ledger.wall_budget_gold = ledger.gold
    ledger.wall_budget_report = dict(demand=demand, safety=report['safety'], target=report['target'],
                                    orders=ledger.wall_orders,
                                    cost=sum(n * turn.shop[k] for o in orders.values() for k, n in o.items()))
    from .logging_system import emit_event
    emit_event('worker_itinerary_budget', dict(gold_available=ledger.gold,
               repair_actor=watcher.id, construction_actor=builder.id if builder else None,
               **ledger.wall_budget_report), 'economy')
    return ledger.wall_orders


def _shops(turn, cfg, mem, nav, ledger, hero, home, actions, preferred=None, work=()):
    options = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
            continue
        trip = itinerary_route(nav, hero, [[shop], *work, home], ledger.reserved, final_exact=True)
        if trip is not None and trip[0] + actions + len(work) + cfg.return_margin <= turn.day_left:
            options.append((trip[0], shop != preferred, shop, trip[1]))
    if options:
        best = min(o[0] for o in options)
        # Continue a reachable shop within two steps of the best complete
        # itinerary; a stale faraway/cooldown shop must not force a detour.
        options.sort(key=lambda o: (not (o[2] == preferred and o[0] <= best + 2), o[0], o[2]))
    return options


def _sale(turn, cfg, mem, nav, ledger, hero, home, order, work=()):
    """Sell if sale + safe landing fits, even when shop/construction does not."""
    from .mining import sale_inventory
    ores = sale_inventory(turn, mem, hero)
    if not ores:
        return False
    # A sale may fund the very purchase whose route we are choosing. Forecast
    # that route with its proceeds, but do not add unconfirmed gold to Ledger.
    income = sum(n * turn.prices[k] for k, n in ores.items())
    forecast = shared_orders(turn, cfg, mem, ledger, ledger.wall_cells,
        forecast_gold=_forecast_cash(turn, cfg, ledger) + income)
    order = forecast.get(hero.id, order)
    options = []
    for vendor, kind in turn.zones.items():
        if kind != 'vendor' or mem.movement.avoids(hero.id, vendor):
            continue
        basic = itinerary_route(nav, hero, [[vendor], home], ledger.reserved, final_exact=True)
        if basic is None or basic[0] + len(ores) + cfg.return_margin > turn.day_left:
            continue
        full = []
        if order:
            for shop, kind in turn.zones.items():
                if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
                    continue
                route = itinerary_route(nav, hero, [[vendor], [shop], *work, home],
                                        ledger.reserved, final_exact=True)
                if route and route[0] + len(ores) + len(order) + len(work) + cfg.return_margin <= turn.day_left:
                    full.append(route)
        chosen = min(full, key=lambda r: r[0]) if full else basic
        options.append((not bool(full), chosen[0], vendor != mem.sale_targets.get(hero.id), vendor, chosen[1]))
    if not options:
        return False
    fallback, total, _, vendor, route = min(options)
    kind = max(ores, key=lambda k: ores[k] * turn.prices.get(k, 0))
    action = (command('sell', name=kind, num=ores[kind]) if route[1] is None
              else command('move', route[1]))
    if not ledger.add(hero.id, action):
        return False
    mem.sale_targets[hero.id] = vendor
    mem.sale_workers.add(hero.id)
    mem.mine_targets.pop(hero.id, None)
    if route[1] is None:
        mem.sold_workers.add(hero.id)
    ledger.remember_work(hero, 'sell', vendor)
    ledger.explain(hero.id, 'sell_before_worker_supply', vendor=vendor, quantity=ores[kind],
                   kind=kind, itinerary_steps=total, sale_only=fallback, procurement_fits=not fallback)
    return True


def _forecast_cash(turn, cfg, ledger):
    # Restore reservations to the common pool, but subtract accepted real
    # purchases/construction. Sale proceeds remain forecasts until observed.
    paid = sum(turn.shop.get(cmd.get('name'), 0) * cmd.get('num', 1)
               for cmd in ledger.commands.values() if cmd.get('action') == 'buy')
    paid += ledger.new_towers * cfg.weapon_cost
    return max(0, getattr(ledger, 'wall_budget_gold', turn.gold) - paid)


def _buy(turn, cfg, mem, nav, ledger, hero, state, home, order, work=()):
    _receipt(turn, hero, state)
    # A successful batch is final for that item; late rewards must not launch
    # another outward visit after the worker has departed this shop.
    order = {k: n for k, n in order.items() if k not in state.get('bought', ())}
    order = dict(order)
    space, gold = hero.space, ledger.gold
    for name in list(order):
        order[name] = min(order[name], space, gold // turn.shop[name] if turn.shop[name] else order[name])
        space -= order[name]
        gold -= order[name] * turn.shop[name]
        if not order[name]:
            del order[name]
    if not order:
        return False, 0
    options = _shops(turn, cfg, mem, nav, ledger, hero, home, len(order), state['shop'], work)
    if not options and work:
        # Buy a smaller deliverable order rather than requiring a whole ring.
        options = _shops(turn, cfg, mem, nav, ledger, hero, home, len(order), state['shop'], work[:1])
    if not options:
        return False, 0
    total, _, shop, route = options[0]
    state['shop'] = shop
    cost = sum(n * turn.shop[k] for k, n in order.items())
    if route[1] is not None:
        accepted = ledger.add(hero.id, command('move', route[1]))
        if accepted:
            name = next(iter(order))
            ledger.remember_work(hero, 'buy', shop, name=name, quantity=order[name])
    else:
        name = next(iter(order))
        accepted = ledger.add(hero.id, command('buy', name=name, num=order[name]))
        if accepted:
            state['pending'] = turn.round, name, order[name], hero.inventory[name]
            cost -= order[name] * turn.shop[name]
    if accepted:
        ledger.explain(hero.id, 'worker_supply_batch', order=order, shop=shop, itinerary_steps=total,
                       worker_duty='repair' if hero.id == mem.wall_watch_id else 'construction')
        return True, min(ledger.gold, cost)
    return False, 0


def wall_work(turn, cfg, mem, nav, ledger, hero, sites, order=()):
    """A continuous face with L1 prerequisites before its L3 deliveries."""
    from .economy import wall_sector
    from .wall_batches import face_sites
    state = _state(turn, mem, hero)
    home, _ = builder_home(turn, mem, nav, ledger, hero, sites)
    stock = hero.inventory + Counter(order)
    candidates = []
    for wall in turn.ours:
        name = V1 if wall.kind == 'wall' and wall.level == 1 else V2 if wall.kind == 'wall' and wall.level == 2 else None
        if (not name or not stock[name] or wall.id in ledger.upgrade_claims
                or mem.movement.avoids(hero.id, wall.pos)):
            continue
        trip = itinerary_route(nav, hero, [wall.cells, home], ledger.reserved, final_exact=True)
        if trip and trip[0] + 1 + cfg.return_margin <= turn.day_left:
            route = trip[1]
            candidates.append((wall.pos not in face_sites(sites, state.get('anchor')),
                               wall_sector(turn, wall) != 'front', trip[0], wall.level, wall.id, wall, name, route))
    return sorted(candidates, key=lambda o: o[:5])


def material_stops(turn, cfg, mem, nav, ledger, hero, sites, home):
    """Forecast one reachable missing-wall face, including its stone pickup."""
    from .economy import build_options
    from .wall_batches import face_sites
    from .mining import remaining_ore
    missing = [p for p in sites if p not in turn.blocked and p not in mem.build_failures
               and turn.zones.get(p, 'land') == 'land']
    options = build_options(turn, cfg, mem, nav, ledger, hero, missing, lambda _: 'wall')
    if not options:
        return [], 0
    face = face_sites(sites, options[0][4])
    gaps = [o[4] for o in options if o[4] in face]
    held = hero.inventory['stone']
    if held >= len(gaps) * cfg.wall_stones:
        return [[p] for p in gaps], len(gaps)
    choices = []
    for stone, kind in turn.zones.items():
        if kind != 'stone' or stone in mem.collect_failures or mem.movement.avoids(hero.id, stone):
            continue
        count = min(len(gaps), (held + min(hero.space, remaining_ore(turn, mem, stone))) // cfg.wall_stones)
        if not count:
            continue
        groups = [[stone], *[[p] for p in gaps[:count]]]
        route = itinerary_route(nav, hero, [*groups, home], ledger.reserved, final_exact=True)
        if route:
            needed = max(0, count * cfg.wall_stones - held)
            choices.append((route[0] + needed + count, stone, groups, needed + count))
    best = min(choices, key=lambda o: o[:2], default=None)
    return (best[2], best[3]) if best else ([], 0)


def mining_tail(turn, cfg, mem, nav, ledger, hero, cell, kind, expected_units=0):
    """Forecast sale, own shop, a construction batch and role-specific landing."""
    watcher = hero.id == mem.wall_watch_id
    home = (_home(turn, mem, nav, ledger, hero, ledger.wall_cells)[0] if watcher else
            builder_home(turn, mem, nav, ledger, hero, ledger.wall_cells)[0])
    if not home:
        return None
    orders = shared_orders(turn, cfg, mem, ledger, ledger.wall_cells)
    from .mining import sale_inventory
    income = (sum(n * turn.prices[k] for k, n in sale_inventory(turn, mem, hero).items())
              + expected_units * turn.prices.get(kind, 0))
    if income:
        orders = shared_orders(turn, cfg, mem, ledger, ledger.wall_cells,
            forecast_gold=_forecast_cash(turn, cfg, ledger) + income)
    order = orders.get(hero.id, {})
    work = []
    material, material_actions = [], 0
    if not watcher:
        # The suffix field is shared by all collection cells. Choose a funded
        # face once from today's actual actor rather than BFS every wall per mine.
        cache = getattr(ledger, 'wall_mining_work', None)
        if cache is None:
            cache = ledger.wall_mining_work = {}
        work_key = hero.id, tuple(sorted(order.items()))
        candidates = cache.setdefault(work_key, None)
        if candidates is None:
            candidates = cache[work_key] = wall_work(turn, cfg, mem, nav, ledger,
                turn.units[hero.id], ledger.wall_cells, order)
        materials = getattr(ledger, 'wall_mining_material', None)
        if materials is None:
            materials = ledger.wall_mining_material = {}
        if hero.id not in materials:
            materials[hero.id] = material_stops(turn, cfg, mem, nav, ledger,
                turn.units[hero.id], ledger.wall_cells, home)
        material, material_actions = materials[hero.id]
        if candidates:
            from .wall_batches import face_sites
            face = face_sites(ledger.wall_cells, candidates[0][-3].pos)
            # Budget the currently funded face, including both tiers on L1.
            stock = hero.inventory + Counter(order)
            for option in candidates:
                wall, name = option[-3:-1]
                if wall.pos in face and stock[name]:
                    stock[name] -= 1
                    work.append([wall.pos])
                    if name == V1 and stock[V2]:
                        stock[V2] -= 1
                        work.append([wall.pos])
    ores = {k for k, n in hero.inventory.items() if k in ('stone', 'iron', 'copper') and n and turn.prices.get(k, 0) > 0} | {kind}
    options = []
    for vendor, vkind in turn.zones.items():
        if vkind != 'vendor' or mem.movement.avoids(hero.id, vendor):
            continue
        shops = [p for p, k in turn.zones.items() if k == 'weaponShop' and not mem.movement.avoids(hero.id, p)] if order else [None]
        for shop in shops:
            groups = [[vendor], *material] + ([[shop]] if shop else []) + work + [home]
            field = itinerary_field(nav, hero, groups, ledger.reserved, final_exact=True)
            if cell in field:
                options.append((field[cell] + len(ores) + len(order) + len(work) + material_actions,
                                vendor, shop, field[cell], len(work) + material_actions))
    return min(options) if options else None


def prepare(turn, cfg, mem, nav, ledger, hero, sites):
    """The repair worker sells, buys only medical packs, then returns inside."""
    from .mining import spare_mine
    state = _state(turn, mem, hero)
    mem.wall_batches.pop(hero.id, None)
    mem.stone_reserves[hero.id] = 0
    if ledger.income_worker_id == hero.id:
        ledger.income_worker_id = None
    order = shared_orders(turn, cfg, mem, ledger, sites).get(hero.id, {})
    home, back = _home(turn, mem, nav, ledger, hero, sites)
    cost = sum(n * turn.shop[k] for k, n in order.items())
    ledger.watch_pack_slots[hero.id] = order.get(PACK, 0)
    feasible = bool(home and _shops(turn, cfg, mem, nav, ledger, hero, home, len(order)))
    if not feasible:
        cost = 0
        ledger.watch_pack_slots[hero.id] = 0
    if hero.health <= 165 and hero.inventory['Medicine']:
        ledger.add(hero.id, command('use', name='Medicine'))
        return True, min(ledger.gold, cost)
    if hero.health <= 110:
        from .economy import supplies, buy_supply
        if buy_supply(turn, ledger, hero, supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True)):
            return True, min(ledger.gold, cost)
    if turn.tick < cfg.economy_rounds:
        safety = min(order.get(PACK, 0), ledger.wall_budget_report['safety']) if feasible else 0
        ledger.watch_pack_slots[hero.id] = safety
        cost = safety * turn.shop.get(PACK, 0)
        if back is None:
            original = turn.blocked
            try:
                turn.blocked = original - {h.pos for h in turn.heroes if h.id != hero.id}
                _, future = _home(turn, mem, nav, ledger, hero, sites)
            finally:
                turn.blocked = original
            if future is None:
                ledger.daytime_waits[hero.id] = 'no_home_route'
                return True, 0
        ledger.explain(hero.id, 'wall_supply_trip', phase='mine', duty='repair',
                       reserved_gold=cost, **ledger.wall_budget_report)
        return False, min(ledger.gold, cost)
    mem.daytime_jobs.pop(hero.id, None)
    if state['phase'] == 'mine':
        state['phase'] = 'sell'
    # Selling is independent of any future procurement. Keep all ore types
    # in one visit before moving to the shop, even for small loads after 35.
    if state['phase'] == 'sell':
        if _sale(turn, cfg, mem, nav, ledger, hero, home, order):
            return True, min(ledger.gold, cost)
        state['phase'] = 'shop'
    if state['phase'] in ('shop', 'deliver') and not state.get('closed'):
        ok, reserve = _buy(turn, cfg, mem, nav, ledger, hero, state, home, order)
        if ok:
            state['phase'] = 'shop'
            return True, reserve
        state['phase'] = 'deliver'
        ledger.watch_pack_slots[hero.id] = 0
        if state.get('bought') or state.get('shop') and not state.get('pending'):
            state['closed'] = True
    if back is None:
        ledger.daytime_waits[hero.id] = 'no_home_route'
        return True, 0
    if turn.day_left <= back[0] + cfg.return_margin + 1:
        state['phase'] = 'return'
    else:
        from .wall_watch import geometry
        inside, _ = geometry(turn, sites)
        reserved = ledger.reserved | {(x, y) for x in range(turn.width) for y in range(turn.height)
                                      if (x, y) not in inside}
        if spare_mine(turn, cfg, mem, nav, ledger, hero, home=home, exact=True,
                      reserved=reserved if hero.pos in inside else (), max_steps=1):
            return True, 0
    if back[1] is not None:
        ledger.add(hero.id, command('move', back[1]))
    else:
        ledger.daytime_waits[hero.id] = 'watch_ready' if hero.inventory[PACK] else 'empty_watch'
    ledger.explain(hero.id, 'wall_supply_trip', phase=state['phase'], duty='repair',
                   return_steps=back[0], **ledger.wall_budget_report)
    return True, 0


def other_worker(turn, cfg, mem, nav, ledger, hero, towers, sites):
    """Own sale, missing-wall material, one voucher visit and continuous work."""
    from .economy import repair_walls, refresh_stone_reserves
    from .mining import earn, spare_mine
    state = _state(turn, mem, hero)
    mem.wall_batches.pop(hero.id, None)
    mem.wall_repair_worker = hero.id
    refresh_stone_reserves(turn, cfg, mem, ledger)
    order = shared_orders(turn, cfg, mem, ledger, sites).get(hero.id, {})
    cost = sum(n * turn.shop[k] for k, n in order.items())
    home, _ = builder_home(turn, mem, nav, ledger, hero, sites)
    if hero.health <= 165 and hero.inventory['Medicine']:
        ledger.add(hero.id, command('use', name='Medicine'))
        return min(ledger.gold, cost)
    if hero.health <= 110:
        from .economy import supplies, buy_supply
        if buy_supply(turn, ledger, hero, supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True)):
            return min(ledger.gold, cost)
    if turn.tick < cfg.economy_rounds:
        # Both workers produce early; the mining planner charges their
        # different post-35 obligations from the actual collection tile.
        earn(turn, cfg, mem, nav, ledger, hero, deadline=cfg.economy_rounds, allow_spare=False)
        return min(ledger.gold, cost)
    mem.daytime_jobs.pop(hero.id, None)
    if state['phase'] == 'mine':
        state['phase'] = 'sell'
    work = wall_work(turn, cfg, mem, nav, ledger, hero, sites, order)
    stops = [[work[0][-3].pos]] if work else []
    if state['phase'] == 'sell':
        if _sale(turn, cfg, mem, nav, ledger, hero, home, order, stops):
            return min(ledger.gold, cost)
        state['phase'] = 'build'
    if len(turn.weapons) < len(cfg.loadout) or any(w.level < 3 for w in turn.weapons):
        # Keep the inherited unfinished-firepower path alive without spending
        # either carrier's already allocated wall/medical gold.
        from .economy import worker
        reserved = min(ledger.gold, cost)
        ledger.gold -= reserved
        try:
            worker(turn, cfg, mem, nav, ledger, hero, towers, sites, True,
                   develop=True, shopping=True, deadline=cfg.economy_rounds)
        finally:
            ledger.gold += reserved
        if hero.id in ledger.used:
            return reserved
    if state['phase'] == 'build':
        if repair_walls(turn, cfg, mem, nav, ledger, [hero], sites):
            # Preserve builder material state, but never delegate its work to
            # the ordinary face planner or give the watcher a voucher job.
            mem.wall_batches.pop(hero.id, None)
            return min(ledger.gold, cost)
        state['phase'] = 'shop'
    if state['phase'] == 'shop':
        ok, reserve = _buy(turn, cfg, mem, nav, ledger, hero, state, home, order, stops)
        if ok:
            return reserve
        if state.get('bought') or state.get('shop') and not state.get('pending'):
            state['closed'] = True
        state['phase'] = 'deliver'
    # An unfunded initial visit may start after a later sale/task reward.
    if state['phase'] == 'deliver' and not state.get('closed') and order:
        ok, reserve = _buy(turn, cfg, mem, nav, ledger, hero, state, home, order, stops)
        if ok:
            state['phase'] = 'shop'
            return reserve
    for option in wall_work(turn, cfg, mem, nav, ledger, hero, sites):
        wall, name, route = option[-3:]
        action = command('use', wall.pos, name=name) if route[1] is None else command('move', route[1])
        if ledger.add(hero.id, action):
            state['anchor'] = wall.pos
            ledger.upgrade_claims.add(wall.id)
            ledger.remember_work(hero, 'use', wall.pos, name=name)
            ledger.explain(hero.id, 'construction_face_delivery', wall=wall.pos,
                           work_cell=route[2], route_steps=route[0], anchor=state['anchor'])
            return 0
    # Newly destroyed/missing walls remain eligible after voucher delivery.
    if repair_walls(turn, cfg, mem, nav, ledger, [hero], sites):
        mem.wall_batches.pop(hero.id, None)
        return 0
    if _sale(turn, cfg, mem, nav, ledger, hero, home, {}):
        return 0
    if earn(turn, cfg, mem, nav, ledger, hero, deadline=70, allow_spare=False):
        return 0
    if spare_mine(turn, cfg, mem, nav, ledger, hero, home=home, exact=True, max_steps=1):
        return 0
    back = nav.search(hero, home, ledger.reserved)
    if back and back[1] is not None:
        ledger.add(hero.id, command('move', back[1]))
    else:
        ledger.daytime_waits[hero.id] = 'construction_ready' if back else 'no_safe_landing'
    return 0


def station_plan(turn, cfg, mem, nav, ledger, hero):
    """Only the pioneer buys base vouchers; protect an active task in place."""
    from .economy import station_purchase_allowed, voucher_for
    from .mining import return_destination
    station = turn.station
    if (turn.day <= 1 or not turn.is_day or not hero or hero.id in ledger.used
            or hero.kind != 'pioneer' or turn.phase_task or not hero.space or not station
            or not station_purchase_allowed(turn, station, mem)):
        return None
    name = voucher_for(station)
    if (not name or name in ledger.purchases or (hero.id, name) in mem.buy_failures
            or turn.shop.get(name, -1) < 0 or ledger.gold < turn.shop[name]
            or hero.inventory[name]):
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
