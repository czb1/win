"""Daylight deadlines and transport estimates; no permanent worker professions."""
from dataclasses import replace
from .model import Unit, ORES, neighbours


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

