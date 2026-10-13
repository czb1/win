"""Daylight deadlines and transport estimates; no permanent worker professions."""
from dataclasses import replace
from heapq import heappop, heappush
from .model import Unit, ORES, neighbours


def itinerary_field(nav, hero, groups, reserved=(), final_exact=False):
    """Reverse shortest paths through ordered stops, including every endpoint.

    Unlike independent leg estimates (or greedy nearest-stop selection), a
    trading tile is chosen for the cost of the whole remaining itinerary.
    Weighted seeds carry the cost of later stops into each reverse search.
    """
    from .navigation import check_time
    groups = tuple(frozenset(g) for g in groups)
    blocked = frozenset((nav.turn.blocked - nav.vacated_cells(hero)) | set(reserved)
                        | set(nav.traffic_forbidden.get(hero.id, ()))
                        | (nav.memory.blocked(hero.id) if nav.memory else set()))
    cache = getattr(nav, '_itineraries', None)
    if cache is None:
        cache = nav._itineraries = {}
    key = groups, blocked, final_exact
    if key in cache:
        return cache[key]
    tail = None
    for index in range(len(groups) - 1, -1, -1):
        check_time(nav.deadline)
        targets = groups[index]
        goals = (set(targets) if final_exact and index == len(groups) - 1 else
                 {p for t in targets for p in neighbours(t)} - targets)
        goals = {p for p in goals if nav.turn.inside(p) and p not in blocked}
        costs = {p: (tail[p] if tail is not None else 0) for p in goals
                 if tail is None or p in tail}
        queue = [(cost, p) for p, cost in costs.items()]
        from heapq import heapify
        heapify(queue)
        expanded = 0
        while queue:
            cost, p = heappop(queue)
            if costs[p] != cost:
                continue
            expanded += 1
            if expanded % 32 == 0:
                check_time(nav.deadline)
            for q in neighbours(p):
                if q not in blocked and nav.turn.inside(q) and cost + 1 < costs.get(q, float('inf')):
                    costs[q] = cost + 1
                    heappush(queue, (cost + 1, q))
        tail = costs
    cache[key] = tail or {}
    return cache[key]


def itinerary_route(nav, hero, groups, reserved=(), final_exact=False):
    """Return total walking cost and the actual route to the first stop."""
    groups = list(groups)
    if not groups:
        return None
    tail = itinerary_field(nav, hero, groups[1:], reserved, final_exact) if len(groups) > 1 else None
    targets = set(groups[0])
    goals = (targets if final_exact and len(groups) == 1 else
             {p for t in targets for p in neighbours(t)} - targets)
    options = []
    for p in sorted(goals):
        if tail is not None and p not in tail:
            continue
        route = nav.search(hero, {p}, reserved, with_endpoint=True)
        if route is not None:
            options.append((route[0] + (tail[p] if tail is not None else 0), route[0], p, route))
    best = min(options, default=None)
    return (best[0], best[3]) if best else None


def maxed_weapon_mining_phase(turn, cfg):
    """Completed firepower keeps the configured early-day mining window."""
    return (turn.is_day and turn.tick < cfg.economy_rounds
            and len(turn.weapons) >= len(cfg.loadout)
            and all(w.level >= 3 for w in turn.weapons))


def late_logistics_ready(turn, cfg):
    """New idle/traffic policies start after the opening firepower tasks.

    Day one keeps the dedicated offense and gun-construction chain, even if
    a fixture reports upgraded weapons. Later days still need every gun built
    and upgraded before ordinary logistics may use the new policy.
    """
    return (turn.is_day and turn.day > 1
            and len(turn.weapons) >= min(3, len(cfg.loadout))
            and bool(turn.weapons) and all(w.level >= 3 for w in turn.weapons))


def late_economy_allowed(turn, cfg, mem, ledger, hero):
    """Repeat income only for a worker without a reserved special assignment."""
    if (not late_logistics_ready(turn, cfg) or hero.id in mem.return_targets
            or hero.id in mem.wall_batches or hero.id == ledger.first_day_offense_worker_id):
        return False
    job = mem.daytime_jobs.get(hero.id, {})
    if (job.get('kind') in ('build', 'buy', 'use', 'robot_buy', 'recovery')
            or job.get('kind') == 'mine' and job.get('want_stone')):
        return False
    from .model import SUMMON_ORDERS
    return not any('UpgradeVoucher' in name or name in SUMMON_ORDERS for name in hero.backpack)


def planned_weapons(turn, cfg, mem, sites):
    weapons = list(turn.weapons)
    occupied = {p for u in (*turn.ours, *turn.enemies) if u.kind not in ("worker", "pioneer")
                for p in u.cells}
    for i, p in enumerate(sites):
        if len(weapons) >= len(cfg.loadout):
            break
        if p in occupied or p in mem.build_failures:
            continue
        weapons.append(Unit(-i-1, p, cfg.loadout[i % len(cfg.loadout)], 1000))
    return weapons


def via(nav, hero, groups, reserved=(), final_exact=False, future_return=False):
    """Shortest reachable first stop, then a feasible walk through later stops.

    The chosen endpoint matters: distances from the original actor to every
    stop independently would undercount the actual vendor/shop/base trip.
    final_exact treats the last group as standing cells, e.g. an operator post.
    Early daytime return estimates may vacate friendly heroes on the last leg;
    the outward leg and every submitted movement still use current occupancy.
    """
    original = nav.turn.blocked
    proxy, total = hero, 0
    try:
        nav.turn.blocked = original - {hero.pos}
        for index, targets in enumerate(groups):
            if future_return and index > 0 and index == len(groups) - 1:
                nav.turn.blocked -= {h.pos for h in nav.turn.heroes}
            targets = set(targets)
            goals = (targets if final_exact and index == len(groups) - 1
                     else {p for t in targets for p in neighbours(t)} - targets)
            options = [(r[0], p) for p in goals
                       if (r := nav.search(proxy, {p}, reserved)) is not None]
            if not options:
                return None
            length, endpoint = min(options)
            total += length
            proxy = replace(proxy, pos=endpoint)
        return total
    finally:
        nav.turn.blocked = original


def trade_available(turn, mem, nav, hero):
    vendors = [p for p, k in turn.zones.items() if k == "vendor"]
    mines = [p for p, k in turn.zones.items() if k in ORES and turn.prices.get(k, 0) > 0
             and p not in mem.collect_failures]
    return bool(vendors and mines and nav.approach(hero, vendors) and nav.approach(hero, mines))


def reserve_income_worker(turn, mem, nav, ledger, excluded=()):
    """After firepower is ready, keep one daytime worker out of optional jobs.

    Paid deliveries and actual recalls are ineligible. Keep the same earner
    while it remains available; a night-watch identity alone is not a recall.
    """
    from .model import SUMMON_ORDERS
    from .wall_supply import active
    if active(turn, mem):
        excluded = set(excluded) | {mem.wall_watch_id}
    ledger.income_worker_id = None
    if (not turn.is_day or turn.day < 4 or not turn.weapons
            or not all(w.level >= 3 for w in turn.weapons)):
        mem.income_worker_id = None
        return
    vendors = [p for p, k in turn.zones.items() if k == 'vendor']
    eligible = [h for h in turn.workers if h.id not in excluded and h.id not in ledger.used
                and h.health > 110 and h.abnormal_state != 'dizzy'
                and not any('UpgradeVoucher' in k or k in SUMMON_ORDERS for k in h.backpack)
                and (h.space and trade_available(turn, mem, nav, h)
                     or any(h.inventory[k] and turn.prices.get(k, 0) > 0 for k in ORES)
                     and vendors and nav.approach(h, vendors))]
    hero = min(eligible, key=lambda h: (h.id == mem.wall_repair_worker,
               h.id != mem.income_worker_id, h.id == mem.wall_watch_id,
               h.id == mem.supply_worker, h.id), default=None)
    mem.income_worker_id = ledger.income_worker_id = hero.id if hero else None
    if hero:
        # An unpaid optional face batch must not take the earner back before
        # ordinary job continuation sees its protected allocation.
        mem.wall_batches.pop(hero.id, None)
    return hero


def development_pending(turn, cfg, mem, tower_sites, wall_sites):
    """Shared phase condition for new allocation and accepted income jobs."""
    built_walls = {w.pos for w in turn.ours if w.kind == 'wall'}
    return bool(any(w.id < 0 or w.level < 3 for w in planned_weapons(turn, cfg, mem, tower_sites))
                or turn.station and turn.station.level < 3 and (
                    turn.shop or any(h.inventory[f'StationUpgradeVoucher{turn.station.level}']
                                     for h in turn.heroes))
                or any(w.kind == 'wall' and w.level < wall_level_limit(turn, w.pos)
                       and (w.level < mem.wall_rebuild_levels.get(w.pos, 1)
                            or f'WallUpgradeVoucher{w.level}' in turn.shop
                            or any(h.inventory[f'WallUpgradeVoucher{w.level}'] for h in turn.heroes))
                       for w in turn.ours)
                or any(p not in built_walls and p not in mem.build_failures for p in wall_sites))


def preparation_start(turn, cfg, mem, nav, heroes, sites):
    if maxed_weapon_mining_phase(turn, cfg):
        # Optional wall/base work must not shorten the mining window once
        # firepower is ready. Each later purchase still checks its actual trip.
        mem.preparation_tick = cfg.economy_rounds
        return mem.preparation_tick
    weapons = planned_weapons(turn, cfg, mem, sites)
    home = {p for w in weapons for p in w.cells}
    if not home and turn.station:
        home = turn.station.cells
    vendors = [p for p, k in turn.zones.items() if k == "vendor"]
    shops = [p for p, k in turn.zones.items() if k == "weaponShop"]
    missing = sum(w.id < 0 for w in weapons)
    upgrades = sum(w.level < 3 for w in weapons) if turn.day > 1 else 0
    walls = [w for w in turn.ours if w.kind == "wall" and w.level < wall_level_limit(turn, w.pos)] if not upgrades else []
    front = set(front_sites(turn, [w.pos for w in turn.ours if w.kind == "wall"]))
    walls.sort(key=lambda w: (w.level, w.pos not in front, w.health, w.id))
    # Base vouchers also need a shop visit and delivery, even after all guns
    # are maxed. Reserve the actual station leg rather than only weapon cells.
    station = (turn.station if turn.station and turn.station.level < 3
               and not upgrades and not walls and not mem.wall_rebuild_levels else None)
    upgrades += len(walls) + int(station is not None)
    budgets = []
    for hero in heroes:
        if not home:
            continue
        # One batch sale, up to two voucher purchases, delivery/use, and a
        # congestion margin. Building and shopping can proceed in parallel.
        groups = (([vendors] if vendors else []) + ([shops] if shops and upgrades else [])
                  + [w.cells for w in walls] + ([station.cells] if station else []) + [home])
        route = via(nav, hero, groups)
        if route is not None:
            build_route = nav.approach(hero, home)
            shopping = route + (1 if vendors else 0) + (2 + 2 * upgrades if shops else 0)
            construction = (build_route[0] if build_route else 0) + 2 * missing
            work = max(shopping, construction) if len(heroes) > 1 else shopping + 2 * missing
            budgets.append(work + cfg.return_margin)
    # Re-evaluate as mines move, but do not oscillate back into an earlier phase.
    cutoff = min(cfg.economy_rounds, max(0, 70 - max(budgets, default=cfg.return_margin)))
    mem.preparation_tick = min(mem.preparation_tick, cutoff)
    return mem.preparation_tick


def front_sites(turn, sites):
    if not sites or not turn.station:
        return list(sites)
    x = max(p[0] for p in sites) if turn.station.pos[0] < turn.width / 2 else min(p[0] for p in sites)
    return [p for p in sites if p[0] == x]


def wall_level_limit(turn, position, sites=None):
    """Every side can reach level three; upgrade order controls the stages."""
    return 3


def delivery_destination(turn, nav, ledger, hero):
    """Return to an actual night job, or a safe inner-wall work position.

    Maxed firepower does not release an assigned operator from its post.
    Unassigned couriers need shelter, not a trip to an arbitrary turret.
    Sparse layouts keep the existing weapon/base return destination.
    """
    from .mining import return_destination
    if hero.id in ledger.operator_posts:
        return [ledger.operator_posts[hero.id]], True
    sites = ledger.wall_cells
    if turn.day >= 4 and turn.is_day and turn.weapons and all(w.level >= 3 for w in turn.weapons):
        from .wall_watch import geometry
        if sites and turn.station:
            xs, ys = zip(*sites)
            bx, by = zip(*turn.station.cells)
            enclosed = (min(xs) < min(bx) and max(xs) > max(bx)
                        and min(ys) < min(by) and max(ys) > max(by))
            built = {w.pos for w in turn.ours if w.kind == 'wall'}
            if enclosed and set(sites) <= built:
                inside, _ = geometry(turn, sites)
                danger = {p for p in inside for r in turn.robots
                          if turn.threatens_us(r) and max(abs(p[0]-r.pos[0]), abs(p[1]-r.pos[1]))
                          <= r.attack_range + 1}
                goals = inside - danger - set(ledger.operator_posts.values())
                if nav.search(hero, goals, ledger.reserved):
                    return goals, True
    return return_destination(turn, nav, ledger, hero)

