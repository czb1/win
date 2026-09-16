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
    """Establish level-2 firepower, then double the base health budget.

    A damaged base gets the full-heal benefit immediately.  A healthy level-1
    station waits until all three guns reach level 2, but no longer waits for
    every gun to reach level 3.  Wall upgrades remain behind core progression.
    """
    if building.kind == "station":
        if building.health < 750:
            return (-1, building.id)
        guns_ready = (len(turn.weapons) >= 3 and all(w.level >= 2 for w in turn.weapons[:3]))
        if building.level == 1:
            return (.5 if guns_ready else 2, building.id)
        return (3, building.id)
    if building.kind in WEAPONS:
        return (0 if building.level == 1 else 1, building.id)
    xs = [u.pos[0] for u in turn.ours if u.kind == "wall"]
    front = (max(xs) if turn.station and turn.station.pos[0] < turn.width / 2 else min(xs)) if xs else 0
    if mem is not None and exposed_wall(turn, building, mem):
        return (1.5, -mem.wall_hits.get(building.pos, 0), building.health,
                turn.base_distance(building.pos), building.id)
    return (4, int(building.pos[0] != front), building.health, building.id)


def voucher_for(building):
    prefix = ("Weapon" if building.kind in WEAPONS else "Station" if building.kind == "station"
              else "Wall" if building.kind == "wall" else None)
    return f"{prefix}UpgradeVoucher{building.level}" if prefix and building.level < 3 else None


def use_inventory(turn, nav, ledger, hero, local_only=False, mem=None):
    if hero.health <= (165 if hero.kind == "worker" else 150) and hero.inventory["Medicine"]:
        return ledger.add(hero.id, command("use", name="Medicine"))
    upgrades = []
    for building in turn.ours:
        name = voucher_for(building)
        if name and hero.inventory[name] and building.id not in ledger.upgrade_claims:
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
            return ledger.add(hero.id, command("use", building.pos, name=name))
        if ledger.add(hero.id, command("move", route[1])):
            (ledger.repair_claims if name == "WallFixer" else ledger.upgrade_claims).add(building.id)
            return True
    return False


def supplies(turn, cfg, mem, nav, ledger, hero, reserve=0, urgent_only=False,
             planned=(), bulk=False):
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
        for building in sorted(buildings, key=lambda b: upgrade_order(turn, b, mem)):
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
            candidates.append(((1.5,), "WallFixer", damaged[0].cells))
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
        turn.blocked = turn.blocked | {target}
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


def build(turn, cfg, mem, nav, ledger, hero, sites, name_for):
    options = []
    for index, target in enumerate(sites):
        if (target in turn.blocked or target in ledger.reserved
                or target in ledger.build_claims or target in mem.build_failures
                or mem.movement.avoids(hero.id, target)):
            continue
        route = nav.approach(hero, [target], ledger.reserved)
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
    for target in wall_sites:
        if not turn.adjacent(hero.pos, target) or target in turn.blocked:
            continue
        margin = 2 if target in front else cfg.return_margin
        if turn.day_left <= route[0] + margin:
            continue
        original = turn.blocked
        try:
            turn.blocked = original | {target}
            after = (nav.search(hero, {ledger.operator_posts[hero.id]}, ledger.reserved)
                     if hero.id in ledger.operator_posts else nav.approach(hero, tower.cells, ledger.reserved))
        finally:
            turn.blocked = original
        if after and turn.day_left > after[0] + margin:
            if build(turn, cfg, mem, nav, ledger, hero, [target], lambda _: "wall"):
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
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    reserve = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve, planned=planned, bulk=True) if shopping else None
    # Finish a batch at the shop before delivering its first voucher.
    if plan and plan[1][0] == 0 and hero.space and buy_supply(turn, ledger, hero, plan):
        return
    if use_inventory(turn, nav, ledger, hero, mem=mem):
        return
    if gather_first:
        built_walls = {w.pos for w in turn.ours if w.kind == "wall"}
        missing = sum(p not in built_walls and p not in mem.build_failures for p in wall_sites)
        held = sum(h.inventory["stone"] for h in turn.workers)
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
    if missing_towers and ledger.gold >= cfg.weapon_cost:
        if build(turn, cfg, mem, nav, ledger, hero, tower_sites, lambda i: cfg.loadout[i % len(cfg.loadout)]):
            return
    if plan:
        if not hero.space:
            sale = sale_inventory(turn, mem, hero)
            if sale:
                kind = max(sale, key=lambda k: sale[k] * turn.prices[k])
                if visit(turn, nav, ledger, hero, "vendor", command("sell", name=kind, num=sale[kind])):
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
    stone_goal = min(max(cfg.wall_stones, cfg.stone_batch),
                     max(cfg.wall_stones, missing_walls * cfg.wall_stones - other_stones))
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
    next_price = next((turn.shop.get(voucher_for(w), 0) for w in sorted(planned, key=lambda w: w.level)
                       if w.level < 3), 0)
    earn(turn, cfg, mem, nav, ledger, hero, deadline,
         missing_towers * cfg.weapon_cost + next_price)


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
    trading = {h.id for h in free if trade_available(turn, mem, nav, h)}
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    built_walls = {w.pos for w in turn.ours if w.kind == "wall"}
    work_remains = (any(w.id < 0 or w.level < 3 for w in planned)
                    or turn.station and turn.station.level < 3 and bool(turn.shop)
                    or any(p not in built_walls and p not in mem.build_failures for p in wall_sites))
    cutoff = (preparation_start(turn, cfg, mem, nav, free, tower_sites) if work_remains else 70) if trading else 0
    developing = {h.id for h in free if h.id not in trading or turn.tick >= cutoff
                  or any("UpgradeVoucher" in k or k == "WallFixer" for k in h.backpack)}
    # Liquidate the last farming load before either actor leaves for the base
    # or the shop. Otherwise unspent ore can split one bulk order into two trips.
    for h in free:
        if h.id in developing and h.id not in mem.preparation_workers:
            mem.preparation_workers.add(h.id)
            if h.id in trading and sale_inventory(turn, mem, h):
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
    # Close the front first, then keep building the flanks and rear regardless
    # of weapon level. Batch size limits transport, never the daily wall count.
    selected_walls = wall_sites
    builders = [h for h in free if h.id in developing and h.id != buyer]
    if trading:
        builders.sort(key=lambda h: (-h.inventory["stone"],
                      min((r[0] for p in selected_walls
                           if (r := nav.approach(h, [p], ledger.reserved)) is not None), default=999), h.id))
        built_walls = {w.pos for w in turn.ours if w.kind == "wall"}
        missing_front = [p for p in front_sites(turn, selected_walls)
                         if p not in built_walls and p not in mem.build_failures]
        urgent_breaches = [p for p in selected_walls if p not in built_walls
                           and p not in mem.build_failures and mem.wall_hits.get(p, 0)]
        core_ready = (len(turn.weapons) >= len(cfg.loadout)
                      and all(w.level >= 2 for w in turn.weapons[:len(cfg.loadout)])
                      and bool(turn.station and turn.station.level >= 2))
        # More level-1 perimeter did not protect the live match after the
        # enemy-facing segment closed.  Until firepower and base health catch
        # up, build only the front or an observed breach.
        if not missing_front and not urgent_breaches and not core_ready:
            # Spend stone already transported home, but do not launch another
            # side-wall mining trip ahead of the core upgrade milestones.
            builders = [h for h in builders if h.inventory["stone"] >= cfg.wall_stones]
        # Near dusk, parallelize the remaining front segment instead of sending
        # the second worker on another mining trip that cannot fund an upgrade.
        wall_work = 2 * len(missing_front) + cfg.stone_batch + cfg.return_margin
        if builders and (not missing_front or turn.day_left > wall_work):
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
