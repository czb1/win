from collections import Counter
from dataclasses import replace
from .commands import command
from .model import ORES, WEAPONS, HEROES, pos, distance, neighbours
from .navigation import wall_priority, wall_gaps
from .mining import mine, earn, sale_inventory, return_destination
from .economy_plan import planned_weapons, via, trade_available, preparation_start, front_sites, mining_only


DUSK_SPEND_TICK = 60
DUSK_USE_TICK = 65
BASE_MAX_HEALTH = 1500
RESOURCE_POLICY_DAY = 4


def dusk_stop(turn, nav, ledger, hero, destinations, actions=1, require_home=True):
    """A real delivery tile plus the assigned operator post must fit daylight.

    Keep one spare turn for travel congestion; an actor already at its post
    can spend the final turn on an adjacent upgrade without moving.
    """
    home, exact = return_destination(turn, nav, ledger, hero)
    if require_home and not home:
        return None
    options = []
    cells = {p for target in destinations for p in neighbours(target)} - set(destinations)
    for cell in sorted(cells):
        route = nav.search(hero, {cell}, ledger.reserved)
        if route is None or route[0] + actions > turn.day_left:
            continue
        original = turn.blocked
        try:
            turn.blocked = original - {hero.pos}
            proxy = replace(hero, pos=cell)
            if not require_home:
                back = (0, None)
            elif home:
                back = (nav.search(proxy, home, ledger.reserved) if exact
                        else nav.approach(proxy, home, ledger.reserved))
            else:
                back = None
        finally:
            turn.blocked = original
        margin = int(bool(route[0] or back and back[0] or actions > 2))
        if back and route[0] + actions + back[0] + margin <= turn.day_left:
            options.append((route[0], back[0], cell, route))
    return min(options) if options else None


def dusk_route(turn, nav, ledger, hero, destinations, actions=1, require_home=True):
    stop = dusk_stop(turn, nav, ledger, hero, destinations, actions, require_home)
    return stop[3] if stop else None


def dusk_batch(turn, nav, ledger, hero, candidates, count, elapsed, require_home=True):
    """Budget every use and the return trip, following dusk delivery order."""
    pending, delivered = list(candidates), 0
    while pending and delivered < count:
        options = []
        # Budget ordinary level-3 walls in the same center-out stages as use.
        # A nearby edge cannot stand in for an inner delivery that won't fit.
        stages = [priority[1:4] for priority, name, _ in pending
                  if name == "WallUpgradeVoucher2" and priority[0] >= 0]
        for index, (priority, name, cells) in enumerate(pending):
            if (name == "WallUpgradeVoucher2" and priority[0] >= 0
                    and priority[1:4] != min(stages)):
                continue
            stop = dusk_stop(turn, nav, ledger, hero, cells, actions=elapsed + 1,
                             require_home=require_home)
            if stop:
                options.append((min(0, priority[0]), stop[0], priority, index, stop))
        if not options:
            break
        _, _, _, index, stop = min(options)
        elapsed += stop[0] + 1
        hero = replace(hero, pos=stop[2])
        pending.pop(index)
        delivered += 1
    return delivered, elapsed


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


def rebuilding_wall(turn, building, mem):
    return (turn.is_day and mem is not None and building.kind == "wall"
            and building.level < mem.wall_rebuild_levels.get(building.pos, 1))


def wall_upgrade_allowed(turn, building, mem=None):
    """Replacements first; all level 2, front center outward, then flanks."""
    if building.kind != "wall" or building.level == 1 or rebuilding_wall(turn, building, mem):
        return True
    walls = [w for w in turn.ours if w.kind == "wall"]
    if any(w.level == 1 for w in walls):
        return False
    stage = wall_three_stage(turn, building)
    return not any(w.level < 3 and wall_three_stage(turn, w) < stage for w in walls)


def wall_three_stage(turn, building):
    """Equal distances from the full front's midpoint share a level-3 stage."""
    if wall_sector(turn, building) != "front":
        return (1, 0)
    # Include completed walls so the midpoint cannot drift during upgrades.
    ys = [w.pos[1] for w in turn.ours if w.kind == "wall" and w.pos[0] == building.pos[0]]
    return (0, abs(2 * building.pos[1] - min(ys) - max(ys)))


def walls_ready_for_station(turn, mem):
    # Carried vouchers and same-turn upgrade claims are not completed walls.
    return (all(w.level == 3 for w in turn.ours if w.kind == "wall")
            and not mem.wall_rebuild_levels)


def critical_station(turn, building, mem=None):
    """A base upgrade is an emergency only below one quarter health."""
    # The protocol exposes current health only. Retain the observed peak for
    # each level; 1500 is the existing fixture fallback, not a verified cap.
    if turn.day < RESOURCE_POLICY_DAY:
        return building.kind == "station" and building.level < 3 and building.health < 750
    maximum = max(BASE_MAX_HEALTH, mem.station_health_peaks.get(building.level, 0) if mem else 0)
    return (building.kind == "station" and building.level < 3
            and 0 < 4 * building.health < maximum)


def late_wall_relief(turn):
    """After day four, maxed weapons may trade operator return time for walls."""
    return (turn.day >= RESOURCE_POLICY_DAY and turn.is_day
            and turn.tick >= DUSK_SPEND_TICK
            and turn.weapons and all(w.level >= 3 for w in turn.weapons))


def late_wall_phase(turn):
    return (turn.day >= RESOURCE_POLICY_DAY and turn.is_day and turn.tick >= 40
            and turn.weapons and all(w.level >= 3 for w in turn.weapons))


def staged_wall_purchase_allowed(turn, building, mem=None):
    """Stage wall purchases after maxing weapons, or for urgent rebuilding.

    Actual use still follows the observed levels and center-out wall stages;
    supplies budgets paid prerequisites before any additional deliveries.
    """
    return bool(building and building.kind == "wall" and building.level < 3
                and turn.is_day
                and (rebuilding_wall(turn, building, mem)
                     or turn.weapons and all(w.level >= 3 for w in turn.weapons)))


def wall_purchase_allowed(turn, building, mem=None):
    return wall_upgrade_allowed(turn, building, mem) or staged_wall_purchase_allowed(turn, building, mem)


def replacement_work_pending(turn, mem):
    levels = {w.pos: w.level for w in turn.ours if w.kind == "wall"}
    return bool(mem and any(levels.get(p, 0) < level
                            for p, level in mem.wall_rebuild_levels.items()))


def station_purchase_allowed(turn, building, mem):
    if turn.day < RESOURCE_POLICY_DAY:
        return walls_ready_for_station(turn, mem)
    return (not replacement_work_pending(turn, mem)
            and (critical_station(turn, building, mem) or walls_ready_for_station(turn, mem)))


def delivery_trip(turn, nav, ledger, hero, shop_cell, targets):
    """Walk through every delivery and back from the final use tile."""
    home, exact = return_destination(turn, nav, ledger, hero)
    if not home:
        return None
    original = turn.blocked
    try:
        turn.blocked = original - {hero.pos}
        return via(nav, replace(hero, pos=shop_cell), [*targets, home],
                   ledger.reserved, final_exact=exact)
    finally:
        turn.blocked = original


def upgrade_order(turn, building, mem=None):
    """Upgrade all weapons to level 2, then 3, before a healthy base.

    Rebuilt walls catch up first, then critical base healing. Ordinary walls
    follow level-3 weapons and precede healthy base upgrades.
    """
    if rebuilding_wall(turn, building, mem):
        return (-.75, building.level, building.id)
    if building.kind == "station":
        # Rebuilt walls must be restored first. Once walls are ready, a
        # critically damaged base outranks weapon and ordinary wall upgrades.
        emergency = -1 if turn.day < RESOURCE_POLICY_DAY else -.5
        return (emergency if critical_station(turn, building, mem) else 2 if building.level == 1 else 3, building.id)
    if building.kind in WEAPONS:
        return (0 if building.level == 1 else 1, building.id)
    xs = [u.pos[0] for u in turn.ours if u.kind == "wall"]
    front = (max(xs) if turn.station and turn.station.pos[0] < turn.width / 2 else min(xs)) if xs else 0
    if mem is not None and exposed_wall(turn, building, mem):
        return (1.25, -mem.wall_hits.get(building.pos, 0), building.health,
                turn.base_distance(building.pos), building.id)
    stage = wall_three_stage(turn, building) if building.level == 2 else (int(building.pos[0] != front), 0)
    return (1.75, building.level, *stage, building.health, building.id)


def voucher_for(building):
    prefix = ("Weapon" if building.kind in WEAPONS else "Station" if building.kind == "station"
              else "Wall" if building.kind == "wall" else None)
    return f"{prefix}UpgradeVoucher{building.level}" if prefix and building.level < 3 else None


def use_inventory(turn, nav, ledger, hero, local_only=False, mem=None):
    if hero.health <= (165 if hero.kind == "worker" else 150) and hero.inventory["Medicine"]:
        return ledger.add(hero.id, command("use", name="Medicine"))
    upgrades = []
    for building in turn.ours:
        if building.kind == "station" and replacement_work_pending(turn, mem):
            continue
        name = voucher_for(building)
        if (name and hero.inventory[name] and wall_upgrade_allowed(turn, building, mem)
                and building.id not in ledger.upgrade_claims
                and not (mem and mem.movement.avoids(hero.id, building.pos))):
            route = (dusk_route(turn, nav, ledger, hero, building.cells,
                                require_home=not late_wall_relief(turn))
                     if turn.is_day and turn.tick >= DUSK_SPEND_TICK
                     else nav.approach(hero, building.cells, ledger.reserved))
            if route and (not local_only or route[0] == 0):
                upgrades.append((upgrade_order(turn, building, mem), route[0], name, building, route))
        if (building.kind == "wall" and hero.inventory["WallFixer"] and building.health < 500
                and building.id not in ledger.repair_claims):
            route = (dusk_route(turn, nav, ledger, hero, building.cells)
                     if turn.is_day and turn.tick >= DUSK_SPEND_TICK
                     else nav.approach(hero, building.cells, ledger.reserved))
            if route and (not local_only or route[0] == 0):
                upgrades.append(((1.5, building.health, building.id), route[0], "WallFixer", building, route))
    if upgrades:
        previous = mem.upgrade_targets.get(hero.id) if mem else None
        # Keep emergency base healing and replacement-wall upgrades ahead of
        # the current delivery; hold the target among ordinary deliveries.
        nearest_tick = DUSK_SPEND_TICK if turn.day >= RESOURCE_POLICY_DAY else DUSK_USE_TICK
        if turn.is_day and turn.tick >= nearest_tick:
            # Finish nearby paid upgrades instead of crossing the base to
            # preserve a stale target. Emergency healing still goes first.
            key = lambda o: (min(0, o[0][0]), o[1], o[0], o[3].id)
        else:
            key = lambda o: (min(0, o[0][0]), o[3].pos != previous, o[0], o[1], o[3].id)
        _, _, name, building, route = min(upgrades, key=key)
        if route[1] is None:
            used = ledger.add(hero.id, command("use", building.pos, name=name))
            if used and mem:
                mem.upgrade_targets.pop(hero.id, None)
            return used
        if ledger.add(hero.id, command("move", route[1])):
            if mem:
                mem.upgrade_targets[hero.id] = building.pos
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
    dusk = turn.is_day and turn.tick >= DUSK_SPEND_TICK
    # Ordinary dusk items remain single purchases. Walls may share one trip
    # only after the complete batch's delivery/return budget has been checked.
    bulk = bulk and not dusk
    shops = [p for p, k in turn.zones.items() if k == "weaponShop"]
    routes = [(r[0], p, r) for p in shops
              if not mem.movement.avoids(hero.id, p)
              and (r := nav.approach(hero, [p], ledger.reserved)) is not None]
    if not routes:
        return None
    candidates = []
    paid_walls = []
    wounded = hero.health <= (165 if hero.kind == "worker" else 150)
    if not hero.inventory["Medicine"] and wounded:
        candidates.append(((-2 if hero.health <= 110 else -.5,), "Medicine", hero.cells))
    if not urgent_only:
        carried = Counter(item for h in turn.heroes for item in h.backpack)
        # Team stock prevents duplicate purchases; only this courier's paid
        # walls belong in its delivery route. Other carriers deliver theirs.
        carried_here = hero.inventory
        buildings = list(turn.ours) + [w for w in planned if w.id < 0]
        first_level_gun = any(b.kind in WEAPONS and b.level == 1 for b in buildings)
        for building in sorted(buildings, key=lambda b: upgrade_order(turn, b, mem)):
            if building.kind == "station" and not station_purchase_allowed(turn, building, mem):
                continue
            name = voucher_for(building)
            if not name:
                continue
            if not wall_purchase_allowed(turn, building, mem):
                continue
            # Account for every voucher already carried, including vouchers
            # for the next level in a single shop trip. Never buy a duplicate.
            while name and carried[name]:
                carried[name] -= 1
                if building.kind == "wall" and carried_here[name]:
                    paid_walls.append((upgrade_order(turn, building, mem), name, building.cells))
                if carried_here[name]:
                    carried_here[name] -= 1
                building = replace(building, level=building.level + 1, health=max(1000, building.health))
                # A paid prerequisite can fund the next tier in the same trip.
                # Actual use still waits for the observed wall level.
                name = voucher_for(building) if bulk else None
            if not name or not wall_purchase_allowed(turn, building, mem):
                continue
            urgent_wall = (rebuilding_wall(turn, building, mem)
                           or not first_level_gun and exposed_wall(turn, building, mem))
            if (building.kind == "wall" and not urgent_wall and not dusk
                    and any(b.level < 3 and b.kind in WEAPONS for b in buildings)):
                continue
            candidates.append((upgrade_order(turn, building, mem), name, building.cells))
        damaged = [w for w in turn.ours if w.kind == "wall" and w.health < 500]
        if damaged and not any(h.inventory["WallFixer"] for h in turn.heroes):
            candidates.append(((1.5,), "WallFixer", damaged[0].cells))
    candidates.sort(key=lambda c: c[0])
    seen = set()
    for _, name, destinations in candidates:
        if name in seen:
            continue
        seen.add(name)
        price = turn.shop.get(name)
        if (price is None or price < 0 or name in ledger.purchases
                or (hero.id, name) in mem.buy_failures):
            continue
        if price > ledger.gold - reserve:
            # Do not divert scarce weapon funds to cheaper, lower-priority items.
            if turn.tick < cfg.economy_rounds and not dusk:
                return None
            # During preparation use affordable, deliverable alternatives;
            # never spend gold reserved for missing guns.
            continue
        matches = [c[2] for c in candidates if c[1] == name]
        prerequisites = [c[2] for c in sorted(paid_walls, key=lambda c: c[0])]
        # Do not prefetch level three while any level-one prerequisite is
        # still unfunded. This also avoids spending another buyer's reservation.
        if name == "WallUpgradeVoucher2" and any(c[1] == "WallUpgradeVoucher1" for c in candidates):
            continue
        wall_batch = dusk and turn.day >= RESOURCE_POLICY_DAY and name.startswith("WallUpgradeVoucher")
        count = min(len(matches) if bulk or wall_batch else 1, max(1, hero.space),
                    (ledger.gold - reserve) // price if price else len(matches))
        options = []
        for _, shop, _ in routes:
            if dusk and "UpgradeVoucher" in name:
                # Start from the exact shopping tile and include buy + use,
                # then return to this actor's actual post, not the nearest gun.
                for cell in neighbours(shop):
                    to_shop = nav.search(hero, {cell}, ledger.reserved)
                    if to_shop is None or to_shop[0] + 2 > turn.day_left:
                        continue
                    original = turn.blocked
                    try:
                        turn.blocked = original - {hero.pos}
                        if wall_batch:
                            batch = [c for c in candidates if c[1] == name]
                            num, cost = dusk_batch(
                                turn, nav, ledger, replace(hero, pos=cell), batch, count,
                                to_shop[0] + 1, require_home=not late_wall_relief(turn))
                            if num:
                                options.append((-num, cost, to_shop))
                            continue
                        # dusk_route reads day_left; charge shopping travel and
                        # the buy action through its action budget instead.
                        deliveries = [r for target in matches
                                      if (r := dusk_route(turn, nav, ledger, replace(hero, pos=cell),
                                                          target, actions=to_shop[0] + 2)) is not None]
                    finally:
                        turn.blocked = original
                    if deliveries:
                        options.append((-1, to_shop[0] + min(r[0] for r in deliveries), to_shop))
                continue
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
                    # Before dusk preserve the established bulk-trip estimate;
                    # the stricter full-post route is needed when a purchase
                    # could otherwise strand the worker at night.
                    after_delivery = (home if name == "Medicine" else min(
                        (distance(p, w.pos) for p in destinations for w in turn.weapons), default=0))
                    # Wall vouchers are always paid deliveries: a worker who
                    # buys a batch must still be able to use it and return to
                    # the assigned post before night.  The other preparation
                    # purchases keep their established, lighter estimate
                    # until dusk so opening construction is not delayed.
                    strict_delivery = name.startswith("WallUpgradeVoucher") or dusk
                    for num in range(count, 0, -1):
                        targets = prerequisites + matches[:num] if name.startswith("WallUpgradeVoucher") else matches[:num]
                        delivery_walk = (delivery_trip(turn, nav, ledger, hero, cell, targets)
                                         if strict_delivery and name != "Medicine" else
                                         delivery[0] if name != "Medicine" else delivery[0] + home)
                        if delivery_walk is None:
                            continue
                        held = sum(n for k, n in hero.inventory.items() if "UpgradeVoucher" in k)
                        cost = (to_shop[0] + 1 + delivery_walk + num
                                + (0 if strict_delivery else after_delivery)
                                + 2 * held + len(prerequisites)
                                + 2 * sum(w.id < 0 for w in planned) + cfg.return_margin)
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


def dusk_resources(turn, cfg, mem, nav, ledger, tower_sites):
    """Liquidate surplus ore early, then spend daylight before return locks."""
    if not turn.is_day or turn.tick < 40:
        return
    # This pass runs before workers(), so refresh stone reservations from the
    # current blueprint rather than selling against yesterday's allocation.
    structures = {p for b in (*turn.ours, *turn.enemies) if b.kind not in HEROES for p in b.cells}
    missing = [p for p in ledger.wall_cells if p not in structures
               and p not in mem.build_failures and turn.zones.get(p, "land") == "land"]
    stone_need = len(missing) * cfg.wall_stones
    mem.stone_reserves.clear()
    for h in sorted(turn.workers, key=lambda h: (h.id != mem.wall_repair_worker,
                                               -h.inventory["stone"], h.id)):
        mem.stone_reserves[h.id] = min(stone_need, h.inventory["stone"])
        stone_need -= mem.stone_reserves[h.id]
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    reserve = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    for hero in turn.heroes:
        if hero.id in ledger.used or hero.kind == "pioneer" and turn.phase_task:
            continue
        if hero.kind == "worker" and mining_only(turn, cfg):
            continue
        if hero.health <= 165 and hero.inventory["Medicine"]:
            if ledger.add(hero.id, command("use", name="Medicine")):
                continue
        # Begin well before tick 60 so selling several ore types and returning
        # can fit. Reopen a completed daily sale for newly collected surplus.
        carrying = any("UpgradeVoucher" in k or k == "WallFixer" for k in hero.backpack)
        if (hero.kind == "worker" and not carrying
                and hero.id != mem.wall_repair_worker and sale_inventory(turn, mem, hero)
                and earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)):
            continue
        if turn.tick < DUSK_SPEND_TICK:
            continue
        if use_inventory(turn, nav, ledger, hero, mem=mem):
            continue
        if (hero.kind == "worker" and hero.id != mem.wall_repair_worker
                and sale_inventory(turn, mem, hero)
                and earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)):
            continue
        if hero.kind != "worker" or hero.id == mem.wall_repair_worker:
            continue
        # Do not add a second delivery while a paid, applicable voucher waits.
        if any(hero.inventory[voucher_for(b)] for b in turn.ours if voucher_for(b)):
            continue
        plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve, planned=planned)
        buy_supply(turn, ledger, hero, plan)


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
    gaps = wall_gaps(turn, ledger.wall_cells, mem.wall_hits) | (
        set(mem.wall_rebuild_levels) - wall_chain)
    for index, target in enumerate(sites):
        if name_for(index) == "wall" and cfg.layout_mode != "explicit":
            # Shared edges, not diagonal contact: grow one continuous wall.
            # A move claim is not a built wall and cannot seed a second segment.
            if wall_chain and not any(abs(target[0]-p[0]) + abs(target[1]-p[1]) == 1
                                      for p in wall_chain):
                continue
            if not wall_chain and pending_wall:
                continue
            if missing_front and target not in front and target not in gaps:
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
                # Close gaps first, then retain the established expansion order.
                priority = (int(target not in gaps),) + priority[:3] + (0, target != mem.build_targets.get(hero.id))
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
    gaps = wall_gaps(turn, wall_sites, mem.wall_hits) | set(mem.wall_rebuild_levels)
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
                options.append((target not in gaps, target not in front, work[0],
                                work[0] + after[0], target, cell))
    for _, _, _, _, target, cell in sorted(options):
        if build(turn, cfg, mem, nav, ledger, hero, [target], lambda _: "wall", work_cell=cell):
            return True
    return False


def repair_walls(turn, cfg, mem, nav, ledger, free, wall_sites):
    """Borrow one worker for a material batch; leave normal day phases alone."""
    walls = {w.pos for w in turn.ours if w.kind == "wall"}
    gaps = (wall_gaps(turn, wall_sites, mem.wall_hits) | set(mem.wall_rebuild_levels)) - walls
    gaps &= set(wall_sites) - set(mem.build_failures)
    structures = {p for u in (*turn.ours, *turn.enemies) if u.kind not in HEROES for p in u.cells}
    gaps = {p for p in gaps if p not in structures and turn.zones.get(p, "land") == "land"}
    options = []
    for hero in free:
        if hero.health <= 110:
            continue
        reachable = [p for p in sorted(gaps) if not mem.movement.avoids(hero.id, p)
                     and (r := nav.approach(hero, [p], ledger.reserved)) is not None
                     and r[0] + 1 + cfg.return_margin < turn.day_left
                     and wall_keeps_access(turn, nav, ledger, p)]
        if not reachable:
            continue
        held = hero.inventory["stone"]
        capacity = (held + hero.space) // cfg.wall_stones
        direct = nav.approach(hero, reachable, ledger.reserved)[0]
        batches = []
        for amount in range(min(len(reachable), capacity), 0, -1):
            needed = max(0, amount * cfg.wall_stones - held)
            if needed:
                trips = [length for p, kind in turn.zones.items()
                         if kind == "stone" and p not in mem.collect_failures
                         and not mem.movement.avoids(hero.id, p)
                         and (length := via(nav, hero, [[p], reachable], ledger.reserved)) is not None]
                trip = min(trips, default=None)
            else:
                trip = direct
            if trip is not None and trip + needed + 2 * amount + cfg.return_margin < turn.day_left:
                batches.append((amount * cfg.wall_stones, trip))
                break
        if batches:
            goal, trip = batches[0]
            options.append((hero.id != mem.wall_repair_worker, -min(held, goal), trip,
                            hero.id, hero, reachable, goal))
    for _, _, _, _, hero, sites, goal in sorted(options, key=lambda o: o[:4]):
        if hero.id != mem.wall_repair_worker:
            mem.wall_repair_delivering = False
        mem.wall_repair_worker = hero.id
        if hero.health <= 165 and hero.inventory["Medicine"]:
            if ledger.add(hero.id, command("use", name="Medicine")):
                return gaps
        held = hero.inventory["stone"]
        if held < cfg.wall_stones:
            mem.wall_repair_delivering = False
        # Once delivery starts, finish the carried batch rather than refilling
        # after each wall or switching to a shopping trip.
        if not mem.wall_repair_delivering and held < goal:
            if mine(turn, cfg, mem, nav, ledger, hero, want_stone=True):
                return gaps
        if held >= cfg.wall_stones:
            mem.wall_repair_delivering = True
            if build(turn, cfg, mem, nav, ledger, hero, sites, lambda _: "wall"):
                mem.mine_targets.pop(hero.id, None)
                return gaps
        mem.wall_repair_worker = None
        mem.wall_repair_delivering = False
    # No feasible action, no exclusive job. The caller immediately resumes
    # ordinary work, including after the last gap closes (even if upgrades remain).
    mem.wall_repair_worker = None
    mem.wall_repair_delivering = False
    return set()


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
    # Complete a mixed-tier wall load before leaving the shop. Only top up
    # when this carrier has the prerequisite, and never detour back to shop.
    if (turn.is_day and turn.tick < DUSK_SPEND_TICK and hero.space
            and hero.inventory["WallUpgradeVoucher1"]):
        planned = planned_weapons(turn, cfg, mem, tower_sites)
        top_up = supplies(turn, cfg, mem, nav, ledger, hero,
                          sum(w.id < 0 for w in planned) * cfg.weapon_cost,
                          planned=planned, bulk=True)
        if (top_up and top_up[0] == "WallUpgradeVoucher2" and top_up[1][0] == 0
                and buy_supply(turn, ledger, hero, top_up)):
            return
    # Otherwise deliver paid vouchers before sales or ordinary shopping.
    if use_inventory(turn, nav, ledger, hero, mem=mem):
        return
    # Close a nearby front breach before a courier leaves to deliver upgrades.
    # This spends reserved stone in-place and avoids a later repair round trip.
    if (hero.inventory["stone"] >= cfg.wall_stones and (builder or len(turn.weapons) >= len(cfg.loadout))
            and not any(rebuilding_wall(turn, w, mem) for w in turn.ours)):
        local_front = [p for p in front_sites(turn, wall_sites) if turn.adjacent(hero.pos, p)]
        if build(turn, cfg, mem, nav, ledger, hero, local_front, lambda _: "wall"):
            return
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    reserve = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve, planned=planned, bulk=True) if shopping else None
    # A single purchase may still cover several targets of the same tier.
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
    if earn(turn, cfg, mem, nav, ledger, hero, deadline):
        return
    # A preparation deadline can make the normal income planner reject every
    # mine because there is no time to sell and return before the cutoff. Do
    # not leave the worker beside a wall with no command: stockpile a nearby
    # load for the next sale, or at least move back toward the base.
    if deadline is not None and turn.tick < min(deadline, 40):
        if mine(turn, cfg, mem, nav, ledger, hero, stockpile=True):
            return
    if turn.station:
        walk(nav, ledger, hero, turn.station.cells)



def workers(turn, cfg, mem, nav, ledger, tower_sites, wall_sites, excluded=()):
    """Allocate only today's outstanding jobs; all other worker time earns gold."""
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in excluded]
    if mining_only(turn, cfg):
        # This phase is a hard dispatch boundary, including repair couriers,
        # paid vouchers, full backpacks and the inclusive final mining tick.
        # No sale/return deadline may turn early mining into a different job.
        for hero in free:
            mine(turn, cfg, mem, nav, ledger, hero, stockpile=True, dedicated=True)
        return
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
    trading = {h.id for h in free if trade_available(turn, mem, nav, h)}
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    built_walls = {w.pos for w in turn.ours if w.kind == "wall"}
    work_remains = (any(w.id < 0 or w.level < 3 for w in planned)
                    or turn.station and turn.station.level < 3 and bool(turn.shop)
                    or any(rebuilding_wall(turn, w, mem)
                           or w.kind == "wall" and voucher_for(w) in turn.shop for w in turn.ours)
                    or any(p not in built_walls and p not in mem.build_failures for p in wall_sites))
    cutoff = (preparation_start(turn, cfg, mem, nav, free, tower_sites) if work_remains else 70) if trading else 0
    # Keep the selected supply worker in the preparation phase after a
    # voucher batch is delivered.  The old condition only kept a carrier
    # active while it still had a voucher in its backpack; after the last
    # use, the worker fell back to mining before dusk and could never buy the
    # next batch, leaving the shared gold untouched for the rest of the day.
    developing = {h.id for h in free if h.id not in trading or turn.tick >= cutoff
                  or h.id in mem.preparation_workers
                  or h.id in mem.sold_workers
                  or h.id == mem.supply_worker
                  or late_wall_phase(turn)
                  or any("UpgradeVoucher" in k or k == "WallFixer" for k in h.backpack)}
    repair_sites = repair_walls(turn, cfg, mem, nav, ledger, free, wall_sites)
    free = [h for h in free if h.id not in ledger.used]
    # A rebuilt wall must not wait for the ordinary farming cutoff. Borrow
    # just one courier; other workers keep their existing day allocation.
    repair_buyers = []
    if any(rebuilding_wall(turn, w, mem) for w in turn.ours):
        for h in free:
            plan = supplies(turn, cfg, mem, nav, ledger, h,
                            sum(w.id < 0 for w in planned) * cfg.weapon_cost,
                            planned=planned, bulk=True)
            if plan and plan[0].startswith("WallUpgradeVoucher"):
                repair_buyers.append((h.id != mem.supply_worker, plan[1][0], h.id))
    repair_buyer = min(repair_buyers)[2] if repair_buyers else None
    if repair_buyer is not None:
        developing.add(repair_buyer)
    wall_sites = [p for p in wall_sites if p not in repair_sites]
    # Only the assigned carrier's stone funds its repair batch. Everyone else
    # keeps the original construction, shopping and income allocation.
    stone_need = sum(p not in repair_sites for p in missing) * cfg.wall_stones
    mem.stone_reserves.clear()
    for h in sorted(turn.workers, key=lambda h: (-h.inventory["stone"], h.id)):
        if h.id == mem.wall_repair_worker:
            mem.stone_reserves[h.id] = min(len(repair_sites) * cfg.wall_stones, h.inventory["stone"])
        else:
            mem.stone_reserves[h.id] = min(stone_need, h.inventory["stone"])
            stone_need -= mem.stone_reserves[h.id]
    # Liquidate the last farming load before either actor leaves for the base
    # or the shop. Otherwise unspent ore can split one bulk order into two trips.
    for h in free:
        # Keep a voucher carrier in the worker ordering. Its action is issued
        # by worker() below so the persistent supply-worker lock is retained;
        # spending the carrier here would let the other worker claim the build
        # slot and strand the remaining delivery behind it.
        if any("UpgradeVoucher" in k or k == "WallFixer" for k in h.backpack):
            continue
        if h.id == repair_buyer:
            continue
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
        if h.inventory["WallFixer"] or any(h.inventory[voucher_for(b)] for b in turn.ours
                                          if voucher_for(b) and wall_upgrade_allowed(turn, b, mem)):
            continue
        p = supplies(turn, cfg, mem, nav, ledger, h, reserve, planned=planned, bulk=True)
        if p:
            buyers.append((p[1][0], h.id))
    # Keep the same courier through buying and delivery. Reassign only when
    # its work finishes, becomes infeasible, or the worker disappears.
    carrying = next((h for h in free if h.id == mem.supply_worker
                     and any("UpgradeVoucher" in k for k in h.backpack)), None)
    eligible = {uid for _, uid in buyers}
    if mem.supply_worker in eligible and not carrying:
        buyer = mem.supply_worker
    elif eligible:
        # While the current carrier is walking a delivery, let another
        # worker purchase the next late-wall batch.  This is what prevents a
        # two-voucher backpack from becoming a hard throughput limit.
        buyer = min(buyers)[1]
    elif carrying:
        buyer = mem.supply_worker
    else:
        buyer = None
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
