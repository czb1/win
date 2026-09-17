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


def via(nav, hero, groups, reserved=(), final_exact=False):
    """Shortest reachable first stop, then a feasible walk through later stops.

    The chosen endpoint matters: distances from the original actor to every
    stop independently would undercount the actual vendor/shop/base trip.
    final_exact treats the last group as standing cells, e.g. an operator post.
    """
    original = nav.turn.blocked
    proxy, total = hero, 0
    try:
        nav.turn.blocked = original - {hero.pos}
        for index, targets in enumerate(groups):
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


def preparation_start(turn, cfg, mem, nav, heroes, sites):
    weapons = planned_weapons(turn, cfg, mem, sites)
    home = {p for w in weapons for p in w.cells}
    if not home and turn.station:
        home = turn.station.cells
    vendors = [p for p, k in turn.zones.items() if k == "vendor"]
    shops = [p for p, k in turn.zones.items() if k == "weaponShop"]
    missing = sum(w.id < 0 for w in weapons)
    upgrades = sum(w.level < 3 for w in weapons)
    # Night income can fund healthy-base vouchers in the same bulk shop trip.
    # Reserve their real purchase/use actions instead of crowding out walls.
    base_work = 0
    if mem.night_crew and turn.station and turn.station.health >= 750:
        funds = turn.gold + sum(max(0, h.inventory[k] - (mem.stone_reserves.get(h.id, 0) if k == "stone" else 0))
                                * turn.prices.get(k, 0) for h in turn.heroes for k in ORES)
        funds -= missing * cfg.weapon_cost
        funds -= sum(turn.shop.get(f"WeaponUpgradeVoucher{w.level}", 0)
                     for w in weapons if w.level < 3)
        for level in range(turn.station.level, 3):
            price = turn.shop.get(f"StationUpgradeVoucher{level}")
            if price is None or funds < price:
                break
            funds -= price
            base_work += 2
    budgets = []
    for hero in heroes:
        if not home:
            continue
        # One batch sale, up to two voucher purchases, delivery/use, and a
        # congestion margin. Building and shopping can proceed in parallel.
        groups = ([vendors] if vendors else []) + ([shops] if shops and upgrades else []) + [home]
        route = via(nav, hero, groups)
        if route is not None:
            build_route = nav.approach(hero, home)
            shopping = route + (1 if vendors else 0) + (2 + 2 * upgrades + base_work if shops else 0)
            construction = (build_route[0] if build_route else 0) + 2 * missing
            work = max(shopping, construction) if len(heroes) > 1 else shopping + 2 * missing
            budgets.append(work + cfg.return_margin)
    # Re-evaluate as mines move, but do not oscillate back into an earlier phase.
    cutoff = min(max(0, cfg.economy_rounds - base_work),
                 max(0, 70 - max(budgets, default=cfg.return_margin)))
    mem.preparation_tick = min(mem.preparation_tick, cutoff)
    return mem.preparation_tick


def front_sites(turn, sites):
    if not sites or not turn.station:
        return list(sites)
    x = max(p[0] for p in sites) if turn.station.pos[0] < turn.width / 2 else min(p[0] for p in sites)
    return [p for p in sites if p[0] == x]
