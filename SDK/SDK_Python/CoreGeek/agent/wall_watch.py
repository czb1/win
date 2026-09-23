"""Fourth-night wall watch, subordinate to the existing gunner assignment."""
from dataclasses import replace
from .commands import command
from .model import neighbours, ORES
from .mining import earn
from .wall_health import needs_night_repair, WALL_MAX_HEALTH


def select_watch(turn, mem, pairs):
    operators = {h.id for h, _ in pairs}
    eligible = [h for h in turn.workers if h.id not in operators]
    hero = min(eligible, key=lambda h: (h.id != mem.wall_watch_id,
                                       -h.inventory['WallFixer'], h.id), default=None)
    mem.wall_watch_id = hero.id if turn.day >= 4 and hero else None
    return hero if mem.wall_watch_id is not None else None


def geometry(turn, sites):
    if not sites or not turn.station:
        return set(), None
    right = sum(p[0] for p in turn.station.cells) / 4 < (turn.width - 1) / 2
    front = (max if right else min)(p[0] for p in sites)
    low, high = min(p[1] for p in sites), max(p[1] for p in sites)
    # Rear remains open; side/front boundaries come from the blueprint even
    # when a wall is destroyed. Never cross a breach to patrol outside.
    inside = {(x, y) for x in range(turn.width) for y in range(low + 1, high)
              if (x < front if right else x > front)}
    return inside, front


def watch_route(turn, nav, ledger, mem, hero, sites, target=None):
    inside, _ = geometry(turn, sites)
    goals = (set(neighbours(target.pos)) if target else
             {p for wall in sites for p in neighbours(wall)}) & inside
    reserved = set(ledger.reserved)
    if mem.gunner_post:
        reserved.add(mem.gunner_post)
    if hero.pos in inside:
        reserved.update((x, y) for x in range(turn.width) for y in range(turn.height)
                        if (x, y) not in inside)
    return nav.search(hero, goals, reserved)


def prepare_watch(turn, cfg, mem, nav, ledger, hero, sites):
    """Return (worker locked, gold reserved); retain early daytime production."""
    if not hero or hero.id in ledger.used:
        return False, 0
    walls = [w for w in turn.ours if w.kind == 'wall']
    if not walls:
        return False, 0
    held = hero.inventory['WallFixer']
    price = turn.shop.get('WallFixer')
    need = max(0, min(3, hero.capacity) - held)
    funds = min(ledger.gold, need * price) if price is not None and price > 0 else 0
    home = watch_route(turn, nav, ledger, mem, hero, sites)
    if home is None:
        return False, 0
    shops = []
    for point, kind in turn.zones.items():
        if kind != 'weaponShop' or (hero.id, 'WallFixer') in mem.buy_failures:
            continue
        for cell in neighbours(point):
            route = nav.search(hero, {cell}, ledger.reserved)
            if route is None:
                continue
            back = watch_route(turn, nav, ledger, mem, replace(hero, pos=cell), sites)
            if back:
                shops.append((route[0] + back[0] + 1, route))
    shop = min(shops, key=lambda o: o[0], default=None)
    if shop is None:
        funds = 0
    budget = (shop[0] if shop else home[0]) + cfg.return_margin + len(ORES)
    if turn.tick < cfg.economy_rounds and turn.day_left > budget:
        return False, funds
    if hero.health <= 165 and hero.inventory['Medicine']:
        ledger.add(hero.id, command('use', name='Medicine'))
        return True, funds
    if need and price is not None and price >= 0 and (price == 0 or ledger.gold >= price) and shop:
        if shop[0] + cfg.return_margin < turn.day_left:
            if hero.space < need and any(hero.inventory[k] for k in ORES):
                # Clear ore through the existing once-daily sale contract.
                earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)
                return True, funds
            count = min(need, hero.space, ledger.gold // price if price else need)
            if count:
                route = shop[1]
                ledger.add(hero.id, command('move', route[1]) if route[1] else
                           command('buy', name='WallFixer', num=count))
                return True, funds if route[1] else 0
    if held:
        if home[1] is not None:
            ledger.add(hero.id, command('move', home[1]))
        return True, 0
    return False, 0


def repair_watch(turn, mem, nav, ledger, hero, sites):
    if turn.is_day or not hero.inventory['WallFixer']:
        return False
    _, front = geometry(turn, sites)
    options = []
    for wall in turn.ours:
        if (wall.kind != 'wall' or wall.pos not in sites or wall.id in ledger.repair_claims
                or not needs_night_repair(wall)):
            continue
        route = watch_route(turn, nav, ledger, mem, hero, sites, wall)
        if route:
            maximum = WALL_MAX_HEALTH[wall.level - 1]
            options.append((wall.pos[0] != front, wall.health / maximum, route[0], wall.id, wall, route))
    if not options:
        return False
    *_, wall, route = min(options, key=lambda o: o[:4])
    return ledger.add(hero.id, command('move', route[1]) if route[1] else
                      command('use', wall.pos, name='WallFixer'))
