from collections import Counter
from dataclasses import replace
from .commands import command
from .model import ORES, WEAPONS, HEROES, pos, distance, neighbours
from .navigation import wall_priority
from .mining import mine, earn, sale_inventory
from .economy_plan import planned_weapons, via, trade_available, preparation_start, front_sites


def walk(nav, ledger, hero, targets):
    route = nav.approach(hero, targets, ledger.reserved)
    if route is None:
        return False
    if route[1] is not None:
        return ledger.add(hero.id, command("move", route[1]))
    return True


def vacate_site(turn, nav, ledger, hero, sites):
    """An idle actor on a blueprint cell must not create a permanent hole."""
    if hero.pos not in sites:
        return False
    route = nav.search(hero, set(neighbours(hero.pos)) - set(sites), ledger.reserved)
    return bool(route and route[1] is not None
                and ledger.add(hero.id, command("move", route[1])))


def visit(turn, nav, ledger, hero, kind, action):
    targets = [p for p, k in turn.zones.items() if k == kind]
    options = [(route[0], p, route) for p in targets
               if (route := nav.approach(hero, [p], ledger.reserved)) is not None]
    if not options:
        return False
    _, _, route = min(options)
    if route[1] is None:
        return ledger.add(hero.id, action)
    return ledger.add(hero.id, command("move", route[1]))


def wall_sector(turn, wall):
    walls = [w for w in turn.ours if w.kind == "wall"]
    if not walls or not turn.station:
        return None
    xs = [w.pos[0] for w in walls]
    ys = [w.pos[1] for w in walls]
    front = max(xs) if turn.station.pos[0] < turn.width / 2 else min(xs)
    if wall.pos[0] == front:
        return "front"
    if wall.pos[1] == min(ys):
        return "top"
    if wall.pos[1] == max(ys):
        return "bottom"
    return None


def exposed_wall(turn, wall, mem):
    """Strengthen damaged walls and cover the three sides before the third night."""
    if turn.day < 2 or wall.level != 1:
        return False
    sector = wall_sector(turn, wall)
    if sector is None:
        return False
    # A destroyed cell keeps its hit history after rebuilding. Do not turn a
    # fresh full-health replacement into an immediate shopping detour.
    if wall.health < 800 or (wall.health < 1000 and mem.wall_hits.get(wall.pos, 0)):
        return True
    return (turn.day >= 3 and not any(w.kind == "wall" and w.level > 1
                                     and wall_sector(turn, w) == sector for w in turn.ours))


def upgrade_order(turn, building, mem=None):
    """Upgrade all weapons to level 2, then 3, before a healthy base.

    A damaged base gets the full-heal benefit immediately. Wall upgrades are
    deliberately last: upgrading every wall first would starve the guns.
    """
    if building.kind == "station":
        return (-1 if building.health < 750 else 2 if building.level == 1 else 3, building.id)
    if building.kind in WEAPONS:
        return (0 if building.level == 1 else 1, building.id)
    xs = [u.pos[0] for u in turn.ours if u.kind == "wall"]
    front = (max(xs) if turn.station and turn.station.pos[0] < turn.width / 2 else min(xs)) if xs else 0
    if mem is not None and exposed_wall(turn, building, mem):
        return (1.25, -mem.wall_hits.get(building.pos, 0), building.health,
                turn.base_distance(building.pos), building.id)
    return (4, int(building.pos[0] != front), building.health, building.id)


def voucher_for(building):
    prefix = ("Weapon" if building.kind in WEAPONS else "Station" if building.kind == "station"
              else "Wall" if building.kind == "wall" else None)
    return f"{prefix}UpgradeVoucher{building.level}" if prefix and building.level < 3 else None


def use_inventory(turn, nav, ledger, hero, local_only=False, mem=None, repair_only=False):
    if hero.health <= (165 if hero.kind == "worker" else 150) and hero.inventory["Medicine"]:
        return ledger.add(hero.id, command("use", name="Medicine"))
    upgrades = []
    for building in turn.ours:
        name = voucher_for(building)
        if not repair_only and name and hero.inventory[name] and building.id not in ledger.upgrade_claims:
            route = nav.approach(hero, building.cells, ledger.reserved)
            if route and (not local_only or route[0] == 0):
                upgrades.append((upgrade_order(turn, building, mem), route[0], name, building, route))
        if (building.kind == "wall" and hero.inventory["WallFixer"] and building.health < 500
                and building.id not in ledger.repair_claims):
            route = nav.approach(hero, building.cells, ledger.reserved)
            if route and (not local_only or route[0] == 0):
                upgrades.append(((1.5, building.health, building.id), route[0], "WallFixer", building, route))
    if upgrades:
        _, _, name, building, route = min(upgrades, key=lambda o: (o[0], o[1], o[3].id))
        if route[1] is None:
            if ledger.add(hero.id, command("use", building.pos, name=name)):
                (ledger.repair_claims if name == "WallFixer" else ledger.upgrade_claims).add(building.id)
                return True
            return False
        if ledger.add(hero.id, command("move", route[1])):
            (ledger.repair_claims if name == "WallFixer" else ledger.upgrade_claims).add(building.id)
            return True
    return False


def supplies(turn, cfg, mem, nav, ledger, hero, reserve=0, urgent_only=False,
             planned=(), bulk=False, repair_only=False, wall_upgrades=None):
    """Return an affordable, applicable purchase and its shopping route.

    Routes are checked before committing to shopping. A full backpack can be
    emptied by the worker before buying, and duplicate walking buyers reserve
    both their item and the shared gold for this decision.
    """
    shops = [p for p, k in turn.zones.items() if k == "weaponShop"]
    routes = [(r[0], p, r) for p in shops
              if (r := nav.approach(hero, [p], ledger.reserved)) is not None]
    if not routes:
        return None
    candidates = []
    wounded = hero.health <= (165 if hero.kind == "worker" else 150)
    if not hero.inventory["Medicine"] and wounded:
        candidates.append(((-2 if hero.health <= 110 else -.5,), "Medicine", hero.cells))
    if not urgent_only:
        carried = Counter(item for h in turn.heroes for item in h.backpack)
        buildings = list(turn.ours) + [w for w in planned if w.id < 0]
        first_level_gun = any(b.kind in WEAPONS and b.level == 1 for b in buildings)
        for building in ([] if repair_only else sorted(buildings, key=lambda b: upgrade_order(turn, b, mem))):
            name = voucher_for(building)
            if not name:
                continue
            # Account for every voucher already carried, including vouchers
            # for the next level in a single shop trip. Never buy a duplicate.
            while name and carried[name]:
                carried[name] -= 1
                building = replace(building, level=building.level + 1, health=max(1000, building.health))
                name = voucher_for(building) if bulk else None
            if not name:
                continue
            urgent_wall = (not first_level_gun and exposed_wall(turn, building, mem))
            if building.kind == "wall" and not urgent_wall and (
                    any(b.level < 3 and b.kind in (*WEAPONS, "station") for b in buildings)
                    or bulk and building.health >= 500):
                continue
            candidates.append((upgrade_order(turn, building, mem), name, building.cells))
        damaged = [w for w in turn.ours if w.kind == "wall" and w.health < 500]
        if damaged and not any(h.inventory["WallFixer"] for h in turn.heroes):
            candidates.extend(((1.5,), "WallFixer", w.cells) for w in damaged)
        if turn.is_day and turn.day >= 2 and not repair_only:
            front = set(front_sites(turn, ledger.wall_cells))
            upgrades = sorted((w for w in turn.ours if w.kind == "wall" and w.level < 3
                               and w.pos in ledger.wall_cells
                               and (w.pos in front or mem.wall_hits.get(w.pos, 0))),
                              key=lambda w: (w.level, w.health, w.id))
            stock = Counter(item for h in turn.heroes for item in h.backpack)
            covered, next_cost = set(), 0
            for wall in upgrades:
                name = voucher_for(wall)
                if stock[name]:
                    stock[name] -= 1
                    covered.add(wall.id)
                elif not next_cost and 0 <= turn.shop.get(name, -1) <= ledger.gold - reserve:
                    next_cost = turn.shop[name]
                    covered.add(wall.id)
            held = sum(h.inventory["WallFixer"] for h in turn.heroes)
            reserve += next_cost + max(0, sum(w.id not in covered for w in damaged) - held) * turn.shop.get("WallFixer", 0)
    if wall_upgrades is not None:
        # Dedicated wall phase: never let general weapon/base ordering intervene.
        candidates = [c for c in candidates if c[1] == "Medicine"]
        carried = Counter(item for h in turn.heroes for item in h.backpack)
        for wall in wall_upgrades:
            name = voucher_for(wall)
            if not name:
                continue
            if carried[name]:
                carried[name] -= 1
                continue
            candidates.append(((wall.level, wall.health, wall.id), name, wall.cells))
    seen = set()
    for _, name, destinations in sorted(candidates, key=lambda c: c[0]):
        if name in seen:
            continue
        seen.add(name)
        price = turn.shop.get(name)
        if (price is None or price < 0 or name in ledger.purchases
                or (hero.id, name) in mem.buy_failures):
            continue
        if price > ledger.gold - reserve:
            # Do not divert scarce weapon funds to cheaper, lower-priority items.
            return None
        matches = [c[2] for c in candidates if c[1] == name]
        count = min(len(matches) if bulk else 1, max(1, hero.space),
                    (ledger.gold - reserve) // price if price else len(matches))
        options = []
        for _, shop, _ in routes:
            arrival = nav.approach(hero, destinations, ledger.reserved)
            if arrival is None:
                continue
            # Evaluate from an actual reachable shop-adjacent tile, with the
            # original actor position vacated, rather than a straight-line guess.
            for cell in neighbours(shop):
                if (not turn.inside(cell) or cell in turn.blocked and cell != hero.pos
                        or cell in ledger.reserved):
                    continue
                to_shop = nav.search(hero, {cell}, ledger.reserved)
                original = turn.blocked
                turn.blocked = original - {hero.pos}
                try:
                    proxy = replace(hero, pos=cell)
                    delivery = nav.approach(proxy, destinations, ledger.reserved) if name != "Medicine" else (0, None)
                    home = min((r[0] for w in turn.weapons
                                if (r := nav.approach(proxy, w.cells, ledger.reserved)) is not None), default=0)
                finally:
                    turn.blocked = original
                if to_shop and delivery:
                    # Building supplies end at the base; Medicine ends at the
                    # shop. Include the remaining walk back to an operator spot.
                    after_delivery = (home if name == "Medicine" else min(
                        (distance(p, w.pos) for p in destinations for w in turn.weapons), default=0))
                    for num in range(count, 0, -1):
                        delivery_walk = (via(nav, replace(hero, pos=cell), matches[:num], ledger.reserved)
                                         if bulk and name != "Medicine" else delivery[0])
                        if delivery_walk is None:
                            continue
                        held = sum(n for k, n in hero.inventory.items() if "UpgradeVoucher" in k)
                        cost = (to_shop[0] + 1 + delivery_walk + num + after_delivery
                                + 2 * held + 2 * sum(w.id < 0 for w in planned) + cfg.return_margin)
                        if cost < turn.day_left:
                            options.append((-num, cost, to_shop))
                            break
        if options:
            neg_num, _, route = min(options, key=lambda o: o[:2])
            return name, route, -neg_num
    return None


def buy_supply(turn, ledger, hero, plan):
    if not plan or not hero.space:
        return False
    name, route, num = plan
    if route[1] is None:
        return ledger.add(hero.id, command("buy", name=name, num=num))
    if ledger.add(hero.id, command("move", route[1])):
        ledger.purchases.add(name)
        ledger.gold -= turn.shop[name] * num
        return True
    return False


def wall_keeps_access(turn, nav, ledger, target):
    destinations = [turn.station.cells] if turn.station else []
    for kind in ("vendor", "weaponShop", *ORES):
        cells = {p for p, k in turn.zones.items() if k == kind}
        if cells:
            destinations.append(cells)
    destinations.extend(w.cells for w in turn.weapons)
    destinations.extend({p} for p, (_, name) in ledger.build_claims.items() if name in WEAPONS)
    # Connectivity is structural: a passing actor must not turn a planned gate
    # into a permanent extra hole. Check actors at their proposed end positions.
    original = turn.blocked
    mobile = {r.pos for r in (*turn.ours, *turn.enemies, *turn.robots)
              if r.kind in HEROES or r in turn.robots}
    built = {pos(c["targetPos"][0]) for c in ledger.commands.values() if c["action"] == "build"}
    turn.blocked = (original - mobile) | built
    heroes = []
    for hero in turn.heroes:
        cmd = ledger.commands.get(str(hero.id), {})
        heroes.append(replace(hero, pos=pos(cmd["targetPos"][0])) if cmd.get("action") == "move" else hero)
    try:
        reachable = [(h, ds) for h in heroes for ds in destinations if nav.approach(h, ds)]
        unfinished_guns = ledger.tower_cells - {w.pos for w in turn.weapons} - built
        gun_routes = [(h, p, r[0]) for h in heroes for p in unfinished_guns
                      if (r := nav.approach(h, {p})) is not None]
        turn.blocked = turn.blocked | {target}
        # A wall must not lengthen access to a gun still under construction.
        # Reachability alone allows huge detours and courier/builder oscillation.
        if any((r := nav.approach(h, {p})) is None or r[0] > length
               for h, p, length in gun_routes):
            return False
        if not all(nav.approach(h, ds) is not None for h, ds in reachable):
            return False
        positions = {h.id: h for h in heroes}
        # A late wall must leave every assigned operator time to get inside,
        # including operators whose movement was already planned this turn.
        return all((route := (nav.search(positions[h.id], {ledger.operator_posts[h.id]})
                             if h.id in ledger.operator_posts else nav.approach(positions[h.id], w.cells))) is not None
                   and turn.day_left > route[0] + 2 for h, w in ledger.return_pairs)
    finally:
        turn.blocked = original


def build(turn, cfg, mem, nav, ledger, hero, sites, name_for, work_cell=None):
    options = []
    wall_chain = {w.pos for w in turn.ours if w.kind == "wall" and w.pos in ledger.wall_cells}
    wall_chain.update(pos(c["targetPos"][0]) for c in ledger.commands.values()
                      if c.get("action") == "build" and c.get("name") == "wall")
    pending_wall = any(name == "wall" for _, name in ledger.build_claims.values())
    front = set(front_sites(turn, ledger.wall_cells))
    missing_front = front - wall_chain
    for index, target in enumerate(sites):
        if name_for(index) == "wall" and cfg.layout_mode != "explicit":
            # Shared edges, not diagonal contact: grow one continuous wall.
            # A move claim is not a built wall and cannot seed a second segment.
            if wall_chain and not any(abs(target[0]-p[0]) + abs(target[1]-p[1]) == 1
                                      for p in wall_chain):
                continue
            if not wall_chain and pending_wall:
                continue
            if missing_front and target not in front:
                continue
        if (target in turn.blocked or target in ledger.reserved
                or target in ledger.build_claims or target in mem.build_failures
                or mem.movement.avoids(hero.id, target)):
            continue
        route = (nav.search(hero, {work_cell}, ledger.reserved) if work_cell is not None
                 else nav.approach(hero, [target], ledger.reserved))
        if route:
            priority = (wall_priority(turn, cfg, sites, index, mem.wall_hits)
                        if name_for(index) == "wall" else (0, 0, route[0]))
            if name_for(index) == "wall":
                # Retain the established front/breach/continuity ordering when
                # no attacked flank is urgent.
                priority = priority[:3] + (0, target != mem.build_targets.get(hero.id))
            previous = mem.build_targets.get(hero.id)
            continuity = distance(target, previous) if name_for(index) == "wall" and previous else 0
            options.append((priority, route[0], continuity, index, target, route))
    for _, _, _, index, target, route in sorted(options):
        name = name_for(index)
        if name == "wall" and not wall_keeps_access(turn, nav, ledger, target):
            continue
        action = command("move", route[1]) if route[1] is not None else command("build", target, name=name)
        if ledger.add(hero.id, action):
            ledger.build_claims[target] = (hero.id, name)
            mem.build_targets[hero.id] = target
            return True
    return False


def finish_preparation(turn, cfg, mem, nav, ledger, hero, tower, wall_sites):
    """Spend one local preparation action only with a checked route to a gun."""
    route = (nav.search(hero, {ledger.operator_posts[hero.id]}, ledger.reserved)
             if hero.id in ledger.operator_posts else nav.approach(hero, tower.cells, ledger.reserved))
    if route is None or turn.day_left <= route[0] + 2:
        return False
    if use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem):
        return True
    if hero.kind != "worker" or hero.inventory["stone"] < cfg.wall_stones:
        return False
    front = set(front_sites(turn, wall_sites))
    options = []
    for target in wall_sites:
        if target in turn.blocked:
            continue
        for cell in neighbours(target):
            work = nav.search(hero, {cell}, ledger.reserved)
            if work is None or work[0] > (4 if target in front else 0):
                continue
            original = turn.blocked
            try:
                turn.blocked = (original - {hero.pos}) | {target}
                proxy = replace(hero, pos=cell)
                after = (nav.search(proxy, {ledger.operator_posts[hero.id]}, ledger.reserved)
                         if hero.id in ledger.operator_posts else nav.approach(proxy, tower.cells, ledger.reserved))
            finally:
                turn.blocked = original
            margin = 2 if target in front else cfg.return_margin
            if after and turn.day_left > work[0] + after[0] + margin:
                options.append((target not in front, work[0], work[0] + after[0], target, cell))
    for _, _, _, target, cell in sorted(options):
        if build(turn, cfg, mem, nav, ledger, hero, [target], lambda _: "wall", work_cell=cell):
            return True
    return False


def worker(turn, cfg, mem, nav, ledger, hero, tower_sites, wall_sites, builder,
           develop=True, shopping=True, deadline=None, gather_first=False):
    if hero.health <= 165 and hero.inventory["Medicine"]:
        ledger.add(hero.id, command("use", name="Medicine"))
        return
    if hero.inventory["WallFixer"] and use_inventory(turn, nav, ledger, hero, mem=mem):
        return
    if not develop:
        if use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem):
            return
        earn(turn, cfg, mem, nav, ledger, hero, deadline)
        return
    # Close a nearby front breach before a courier leaves to deliver upgrades.
    # This spends reserved stone in-place and avoids a later repair round trip.
    if hero.inventory["stone"] >= cfg.wall_stones and (builder or len(turn.weapons) >= len(cfg.loadout)):
        local_front = [p for p in front_sites(turn, wall_sites) if turn.adjacent(hero.pos, p)]
        if build(turn, cfg, mem, nav, ledger, hero, local_front, lambda _: "wall"):
            return
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    reserve = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve, planned=planned, bulk=True) if shopping else None
    # Finish a batch at the shop before delivering its first voucher.
    if plan and plan[1][0] == 0 and hero.space and buy_supply(turn, ledger, hero, plan):
        return
    if use_inventory(turn, nav, ledger, hero, mem=mem):
        return
    built_walls = {w.pos for w in turn.ours if w.kind == "wall"}
    stone_sites = wall_sites
    unfinished_front = [p for p in front_sites(turn, wall_sites) if p not in built_walls]
    if cfg.layout_mode != "explicit" and unfinished_front and turn.day_left <= 35:
        # Deliver enough for the front now; do not chase a second deposit for
        # optional flank material while the first night's defence is still open.
        stone_sites = unfinished_front
    if gather_first:
        missing = sum(p not in built_walls and p not in mem.build_failures for p in stone_sites)
        # The courier's stone is not an assured delivery to this wall chain.
        # Keep the builder's front batch self-contained while shopping runs.
        held = hero.inventory["stone"]
        # The courier can construct guns while the other worker collects the
        # front-wall batch on its way home, avoiding a second base -> mine trip.
        if held < min(cfg.stone_batch, missing * cfg.wall_stones):
            if mine(turn, cfg, mem, nav, ledger, hero, want_stone=True):
                return
    available_towers = [p for p in tower_sites if p not in turn.blocked
                        and p not in ledger.reserved and p not in ledger.build_claims
                        and p not in mem.build_failures]
    missing_towers = max(0, min(len(available_towers),
                               len(cfg.loadout) - len(turn.weapons)
                               - sum(name in WEAPONS for _, name in ledger.build_claims.values())))
    if plan and hero.space and buy_supply(turn, ledger, hero, plan):
        return
    if missing_towers and ledger.gold >= cfg.weapon_cost and not gather_first:
        if build(turn, cfg, mem, nav, ledger, hero, tower_sites, lambda i: cfg.loadout[i % len(cfg.loadout)]):
            return
    if plan:
        if not hero.space:
            sale = sale_inventory(turn, mem, hero)
            if sale and earn(turn, cfg, mem, nav, ledger, hero, deadline, force_sale=True):
                return
        elif buy_supply(turn, ledger, hero, plan):
            return
    available_walls = [p for p in wall_sites if p not in turn.blocked
                       and p not in ledger.reserved and p not in ledger.build_claims
                       and p not in mem.build_failures]
    # Count missing blueprint cells, not all walls or an artificial daily quota.
    missing_walls = len(available_walls)
    other_stones = 0
    for other in turn.workers:
        if other.id == hero.id:
            continue
        spent = ledger.commands.get(str(other.id), {})
        stones = other.inventory["stone"]
        if spent.get("action") == "build" and spent.get("name") == "wall":
            stones -= cfg.wall_stones
        other_stones += max(0, stones)
    need_walls = builder and missing_walls > 0 and (
        hero.inventory["stone"] >= cfg.wall_stones or missing_walls * cfg.wall_stones > other_stones)
    material_gaps = sum(p in stone_sites for p in available_walls)
    stone_goal = min(max(cfg.wall_stones, cfg.stone_batch),
                     max(cfg.wall_stones, material_gaps * cfg.wall_stones - other_stones))
    # As dusk approaches, spend an existing partial batch instead of returning
    # with unused stone. Include travel, construction, and the operator margin.
    if need_walls and hero.inventory["stone"] >= cfg.wall_stones:
        routes = [r[0] for p in available_walls
                  if (r := nav.approach(hero, [p], ledger.reserved)) is not None]
        if routes:
            # Keep the largest batch that still fits: remaining collection,
            # delivery, roughly two actions per wall, and the operator margin.
            # Reducing every late trip to one stone wastes the entire dusk.
            budget = turn.day_left - min(routes) - cfg.return_margin + hero.inventory["stone"]
            feasible = max(1, budget // (cfg.wall_stones + 2)) * cfg.wall_stones
            stone_goal = min(stone_goal, feasible)
    if need_walls and hero.inventory["stone"] < stone_goal:
        if mine(turn, cfg, mem, nav, ledger, hero, want_stone=True,
                local_only=hero.inventory["stone"] >= cfg.wall_stones):
            return
    if need_walls and hero.inventory["stone"] >= cfg.wall_stones:
        if build(turn, cfg, mem, nav, ledger, hero, wall_sites, lambda _: "wall"):
            return
    earn(turn, cfg, mem, nav, ledger, hero, deadline)



def maintain_walls(turn, cfg, mem, nav, ledger, free, wall_sites):
    """Give one worker daylight breach/critical-health work before earning gold."""
    if not turn.is_day or turn.day < 2 or not free:
        return
    front = set(front_sites(turn, wall_sites))
    structures = {p for u in (*turn.ours, *turn.enemies)
                  if u.kind not in HEROES for p in u.cells}
    gaps = [p for p in wall_sites if p not in structures
            and p not in mem.build_failures and turn.zones.get(p, "land") == "land"
            and (p in front or mem.wall_hits.get(p, 0))]
    damaged = [w for w in turn.ours if w.kind == "wall" and w.health < 500]
    upgrade_walls = sorted((w for w in turn.ours if w.kind == "wall" and w.level < 3
                            and w.pos in wall_sites and (w.pos in front or mem.wall_hits.get(w.pos, 0))),
                           key=lambda w: (w.level, w.health, w.id))
    if not gaps and not damaged and not upgrade_walls:
        return
    def near(hero, targets):
        route = nav.approach(hero, targets, ledger.reserved) if targets else None
        return route[0] if route else 999
    # Finish the breach phase across all available workers before any item use.
    if gaps:
        for hero in sorted(free, key=lambda h: (h.inventory["stone"] < cfg.wall_stones,
                                               near(h, gaps), h.id)):
            if hero.inventory["stone"] >= cfg.wall_stones:
                if build(turn, cfg, mem, nav, ledger, hero, gaps, lambda _: "wall"):
                    return
            elif mine(turn, cfg, mem, nav, ledger, hero, want_stone=True):
                return
            if not hero.space and earn(turn, cfg, mem, nav, ledger, hero, force_sale=True):
                return
    # Actual observed level/health advances the phase; no model or optimistic state.
    for wall in upgrade_walls:
        name = voucher_for(wall)
        for hero in sorted(free, key=lambda h: (near(h, wall.cells), h.id)):
            if not hero.inventory[name] or wall.id in ledger.upgrade_claims:
                continue
            route = nav.approach(hero, wall.cells, ledger.reserved)
            if route and route[0] + 1 + cfg.return_margin < turn.day_left:
                action = (command("use", wall.pos, name=name) if route[1] is None
                          else command("move", route[1]))
                if ledger.add(hero.id, action):
                    ledger.upgrade_claims.add(wall.id)
                    return
        # Purchase lower-level upgrades before delivering higher-level vouchers.
        for hero in sorted(free, key=lambda h: h.id):
            plan = supplies(turn, cfg, mem, nav, ledger, hero, bulk=True, repair_only=True,
                            wall_upgrades=[w for w in upgrade_walls if w.level == wall.level])
            if buy_supply(turn, ledger, hero, plan):
                return
    # Full-level walls, or upgrades unavailable/unaffordable/too late: heal danger.
    for hero in sorted(free, key=lambda h: (not h.inventory["WallFixer"], h.id)):
        if damaged and hero.inventory["WallFixer"]:
            if use_inventory(turn, nav, ledger, hero, mem=mem, repair_only=True):
                return
        if damaged:
            plan = supplies(turn, cfg, mem, nav, ledger, hero, bulk=True, repair_only=True)
            if buy_supply(turn, ledger, hero, plan):
                return
        if not hero.space or damaged and ledger.gold < turn.shop.get("WallFixer", 0):
            if earn(turn, cfg, mem, nav, ledger, hero, force_sale=True):
                return


def workers(turn, cfg, mem, nav, ledger, tower_sites, wall_sites, excluded=()):
    """Allocate only today's outstanding jobs; all other worker time earns gold."""
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in excluded]
    for h in free:
        if h.health <= 110:
            if h.inventory["Medicine"]:
                ledger.add(h.id, command("use", name="Medicine"))
            else:
                buy_supply(turn, ledger, h, supplies(turn, cfg, mem, nav, ledger, h, urgent_only=True))
    free = [h for h in free if h.id not in ledger.used]
    if not free:
        return
    structures = {p for r in (*turn.ours, *turn.enemies) if r.kind not in HEROES for p in r.cells}
    missing = [p for p in wall_sites if p not in structures and p not in mem.build_failures
               and turn.zones.get(p, "land") == "land"]
    stone_need = len(missing) * cfg.wall_stones
    mem.stone_reserves.clear()
    for h in sorted(turn.workers, key=lambda h: (-h.inventory["stone"], h.id)):
        mem.stone_reserves[h.id] = min(stone_need, h.inventory["stone"])
        stone_need -= mem.stone_reserves[h.id]
    maintain_walls(turn, cfg, mem, nav, ledger, free, wall_sites)
    free = [h for h in free if h.id not in ledger.used]
    if not free:
        return
    trading = {h.id for h in free if trade_available(turn, mem, nav, h)}
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    built_walls = {w.pos for w in turn.ours if w.kind == "wall"}
    work_remains = (any(w.kind == "wall" and w.health < 500 for w in turn.ours)
                    or any(w.id < 0 or w.level < 3 for w in planned)
                    or turn.station and turn.station.level < 3 and bool(turn.shop)
                    or any(p not in built_walls and p not in mem.build_failures for p in wall_sites))
    cutoff = (preparation_start(turn, cfg, mem, nav, free, tower_sites) if work_remains else 70) if trading else 0
    developing = {h.id for h in free if h.id not in trading or turn.tick >= cutoff
                  or h.id in mem.sold_workers
                  or any("UpgradeVoucher" in k or k == "WallFixer" for k in h.backpack)}
    # Liquidate the last farming load before either actor leaves for the base
    # or the shop. Otherwise unspent ore can split one bulk order into two trips.
    for h in free:
        if h.id in developing and h.id not in mem.preparation_workers:
            mem.preparation_workers.add(h.id)
            if h.id in trading and h.id not in mem.sold_workers and sale_inventory(turn, mem, h):
                mem.sale_workers.add(h.id)
        if h.id in developing and h.id in mem.sale_workers:
            if sale_inventory(turn, mem, h):
                earn(turn, cfg, mem, nav, ledger, h, cutoff, allow_spare=False)
            else:
                mem.sale_workers.discard(h.id)
    free = [h for h in free if h.id not in ledger.used]
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    reserve = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    buyers = []
    for h in free:
        if h.id not in developing:
            continue
        at_shop = any(k == "weaponShop" and turn.adjacent(h.pos, p) for p, k in turn.zones.items())
        if h.inventory["WallFixer"] or any("UpgradeVoucher" in k for k in h.backpack) and not at_shop:
            continue
        p = supplies(turn, cfg, mem, nav, ledger, h, reserve, planned=planned, bulk=True)
        if p:
            buyers.append((p[1][0], h.id))
    # Keep the same courier through buying and delivery. Reassign only when
    # its work finishes, becomes infeasible, or the worker disappears.
    carrying = next((h for h in free if h.id == mem.supply_worker
                     and any("UpgradeVoucher" in k for k in h.backpack)), None)
    eligible = {uid for _, uid in buyers}
    buyer = (mem.supply_worker if mem.supply_worker in eligible or carrying
             else min(buyers)[1] if buyers else None)
    mem.supply_worker = buyer
    # Complete the configured wall blueprint; the default contains the front and short connected flanks.
    selected_walls = wall_sites
    builders = [h for h in free if h.id in developing and h.id != buyer]
    if trading:
        builders.sort(key=lambda h: (-h.inventory["stone"],
                      min((r[0] for p in selected_walls
                           if (r := nav.approach(h, [p], ledger.reserved)) is not None), default=999), h.id))
        built_walls = {w.pos for w in turn.ours if w.kind == "wall"}
        missing_front = [p for p in front_sites(turn, selected_walls)
                         if p not in built_walls and p not in mem.build_failures]
        # Near dusk, parallelize the remaining front segment instead of sending
        # the second worker on another mining trip that cannot fund an upgrade.
        wall_work = 2 * len(missing_front) + cfg.stone_batch + cfg.return_margin
        if not missing_front or turn.day_left > wall_work:
            builders = builders[:1]
    builder_ids = {h.id for h in builders}
    # The buyer reserves its complete batch before a second actor spends gold.
    for hero in sorted(free, key=lambda h: (h.id != buyer, h.id)):
        sites = tower_sites + wall_sites
        last_site = all(p in turn.blocked or p in ledger.reserved for p in sites)
        if last_site and vacate_site(turn, nav, ledger, hero, sites):
            continue
        worker(turn, cfg, mem, nav, ledger, hero, tower_sites, selected_walls, hero.id in builder_ids,
               develop=hero.id in developing, shopping=hero.id == buyer, deadline=cutoff,
               gather_first=bool(trading) and hero.id in builder_ids and buyer is not None)
        if hero.id not in ledger.mine_claims:
            mem.mine_targets.pop(hero.id, None)
        if hero.id not in ledger.used:
            vacate_site(turn, nav, ledger, hero, sites)


def pioneer(turn, cfg, mem, nav, ledger, hero):
    if use_inventory(turn, nav, ledger, hero, mem=mem):
        return
    # The pioneer can carry its own medicine; there is no transfer action.
    if not hero.inventory["Medicine"] and hero.health <= 150:
        plan = supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True)
        if buy_supply(turn, ledger, hero, plan):
            return
    t = mem.treasure
    if t and not mem.treasure_done and not mem.treasure_attempted and turn.round <= t["endRound"]:
        required = Counter(t["items"])
        missing = required - hero.inventory
        route = nav.approach(hero, [tuple(t["position"])], ledger.reserved)
        if route and turn.round + route[0] <= t["endRound"]:
            if missing:
                name = next(iter(missing))
                num = missing[name]
                if name in turn.shop and hero.space >= num and ledger.gold >= turn.shop[name]*num:
                    if visit(turn, nav, ledger, hero, "weaponShop", command("buy", name=name, num=num)):
                        return
            elif turn.round + route[0] >= t["startRound"]:
                if route[1] is not None:
                    ledger.add(hero.id, command("move", route[1]))
                elif turn.round >= t["startRound"]:
                    if ledger.add(hero.id, command("summonTreasure", tuple(t["position"]), item=t["items"])):
                        mem.treasure_attempted = True
                return
    if not cfg.llm_enabled:
        return
    options = []
    for task in turn.tasks:
        if not task.get("isValid") or int(task.get("coldDownRounds", 0)) > 0:
            continue
        cells = turn.task_cells(task)
        route = nav.approach(hero, cells, ledger.reserved)
        # Include a conservative return-distance estimate when accepting distant tasks.
        home = [w.pos for w in turn.weapons] or (list(turn.station.cells) if turn.station else [])
        return_estimate = min((distance(p, q) for p in cells for q in home), default=0)
        if route and turn.day_left > route[0] + cfg.task_min_rounds + return_estimate + cfg.return_margin:
            duration = min(int(task.get("timeoutRounds", cfg.task_max_rounds)), cfg.task_max_rounds)
            observed = [s["rounds"] for s in mem.skills if s.get("point") == pos(task["taskPosition"])
                        and s.get("workflow") == "check_token" and not s.get("disabled")
                        and type(s.get("rounds")) is int]
            if observed:
                # A learned fast SOP should not be priced at its official worst
                # case timeout. This estimate only ranks tasks, never deadlines.
                duration = min(duration, max(observed[-3:]) + 2)
            value = (int(task.get("scoreReward", 0)) + .5*int(task.get("goldReward", 0))) / max(1, route[0]+duration)
            options.append((-value, route[0], pos(task["taskPosition"]), task, route))
    if options:
        _, _, point, task, route = min(options, key=lambda x: x[:3])
        if route[1] is not None:
            ledger.add(hero.id, command("move", route[1]))
        elif ledger.add(hero.id, command("acceptTask")):
            mem.task_point = point
            mem.task_timeout = int(task.get("timeoutRounds", cfg.task_max_rounds))
            mem.accepted_round = turn.round
