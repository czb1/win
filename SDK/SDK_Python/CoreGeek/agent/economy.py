import logging
from .logging_system import update_context, emit_event
from .task_schedule import task_options, choose_task
from collections import Counter
from dataclasses import replace
from .commands import command
from .model import ORES, WEAPONS, HEROES, CHARACTERS, DEFENCE_RETURN_TICK, FIRST_DAY_PIONEER_SHOP_TICK, pos, distance, neighbours
from .navigation import wall_priority, wall_gaps
from .mining import mine, earn, spare_mine, sale_inventory, return_destination
from .economy_plan import planned_weapons, via, trade_available, preparation_start, front_sites, wall_level_limit, delivery_destination, development_pending, reserve_income_worker
from .economy_plan import maxed_weapon_mining_phase, late_economy_allowed
from .treasure_clues import treasure_active
from .wall_health import needs_night_repair


DUSK_SPEND_TICK = 60
DUSK_USE_TICK = 65
RESOURCE_POLICY_DAY = 4


def dusk_stop(turn, nav, ledger, hero, destinations, actions=1, require_home=True,
              home_override=None):
    """A real delivery tile plus the night destination must fit daylight.

    Keep one spare turn for travel congestion; an actor already at its post
    can spend the final turn on an adjacent upgrade without moving.
    """
    home, exact = (home_override if home_override is not None
                   else delivery_destination(turn, nav, ledger, hero))
    if require_home and not home:
        return None
    # All delivery endpoints share one reverse return search. Forward BFS
    # from every adjacent wall tile made early large batches exceed deadlines.
    return_reserved = (set(ledger.reserved) | (nav.memory.blocked(hero.id) if nav.memory else set())) - {hero.pos}
    back_distances = (nav.actor_distances_to(hero, home, reserved=return_reserved, exact=exact)
                      if require_home and home else {})
    options = []
    cells = {p for target in destinations for p in neighbours(target)} - set(destinations)
    for cell in sorted(cells):
        route = nav.search(hero, {cell}, ledger.reserved)
        if route is None or route[0] + actions > turn.day_left:
            continue
        back = ((0, None) if not require_home else
                (back_distances[cell], None) if cell in back_distances else None)
        travelling = bool(route[0] or back and back[0] or actions > 2)
        margin = (ledger.cfg.return_margin if hero.id in ledger.operator_posts and travelling
                  else int(travelling))
        if back and route[0] + actions + back[0] + margin <= turn.day_left:
            options.append((route[0], back[0], cell, route))
    return min(options) if options else None


def dusk_route(turn, nav, ledger, hero, destinations, actions=1, require_home=True,
               home_override=None):
    stop = dusk_stop(turn, nav, ledger, hero, destinations, actions, require_home, home_override)
    return stop[3] if stop else None


def dusk_batch(turn, nav, ledger, hero, candidates, count, elapsed, require_home=True):
    """Budget every use and the return trip, following dusk delivery order."""
    pending, delivered = list(candidates), 0
    while pending and delivered < count:
        options = []
        for index, (priority, name, cells) in enumerate(pending):
            stop = dusk_stop(turn, nav, ledger, hero, cells, actions=elapsed + 1,
                             require_home=require_home)
            if stop:
                options.append((min(0, priority[0]), stop[0], priority, index, stop))
        if not options:
            break
        # Prefer front/center stages among deliveries that actually fit.
        stages = [priority[1:4] for _, _, priority, index, _ in options
                  if pending[index][1].startswith("WallUpgradeVoucher") and priority[0] >= 0]
        if stages:
            options = [o for o in options if not pending[o[3]][1].startswith("WallUpgradeVoucher")
                       or o[2][0] < 0 or o[2][1:4] == min(stages)]
        _, _, _, index, stop = min(options)
        elapsed += stop[0] + 1
        hero = replace(hero, pos=stop[2])
        pending.pop(index)
        delivered += 1
    return delivered, elapsed


def walk(nav, ledger, hero, targets):
    targets = set(targets)
    route = nav.approach(hero, targets, ledger.reserved, with_endpoint=True)
    if route is None:
        ledger.explain(hero.id, "route_unavailable", targets=list(targets))
        return False
    if route[1] is not None:
        accepted = nav.move_along(hero, ledger, route, {route[2]}, targets=targets)
        if accepted:
            ledger.explain(hero.id, "approach_destination", targets=list(targets), route_steps=route[0])
        return accepted
    ledger.explain(hero.id, "already_at_destination", targets=list(targets), route_steps=0)
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
               if (route := nav.approach(hero, [p], ledger.reserved, with_endpoint=True)) is not None]
    if not options:
        return False
    _, target, route = min(options)
    if route[1] is None:
        return ledger.add(hero.id, action)
    return nav.move_along(hero, ledger, route, {route[2]}, targets={target})


def wall_sector(turn, wall):
    walls = [w for w in turn.ours if w.kind == "wall"]
    if not walls or not turn.station:
        return None
    sites = [*getattr(turn, "planned_wall_sites", ()), *(w.pos for w in walls)]
    xs = [p[0] for p in sites]
    ys = [p[1] for p in sites]
    front = max(xs) if turn.station.pos[0] < turn.width / 2 else min(xs)
    if wall.pos[0] == front:
        return "front"
    if wall.pos[1] == min(ys):
        return "top"
    if wall.pos[1] == max(ys):
        return "bottom"
    if wall.pos[0] == (min(xs) if front == max(xs) else max(xs)):
        return "rear"
    return None


def exposed_wall(turn, wall, mem):
    """Strengthen damaged walls and cover each side before the third night."""
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


def wall_upgrade_allowed(turn, building, mem=None, paid=False):
    """Purchase stages must not block an already paid, matching wall voucher."""
    if building.kind == "wall" and building.level >= wall_level_limit(turn, building.pos):
        return False
    if building.kind != "wall" or paid or rebuilding_wall(turn, building, mem):
        return True
    walls = [w for w in turn.ours if w.kind == "wall"]
    if building.level == 1:
        return (wall_sector(turn, building) == "front"
                or not any(w.level == 1 and wall_sector(turn, w) == "front" for w in walls))
    # A planned but unbuilt wall also needs its level-two prerequisite. A
    # missing front must never promote the surviving rear to the new front.
    if set(getattr(turn, "planned_wall_sites", ())) - {w.pos for w in walls}:
        return False
    if any(w.level == 1 for w in walls):
        return False
    stage = wall_three_stage(turn, building)
    return not any(w.level < 3 and wall_three_stage(turn, w) < stage for w in walls)


def wall_three_stage(turn, building):
    """Equal distances from the full front's midpoint share a level-3 stage."""
    if wall_sector(turn, building) != "front":
        return (1, 0)
    # Include completed walls so the midpoint cannot drift during upgrades.
    sites = [*getattr(turn, "planned_wall_sites", ()),
             *(w.pos for w in turn.ours if w.kind == "wall")]
    ys = [p[1] for p in sites if p[0] == building.pos[0]]
    return (0, abs(2 * building.pos[1] - min(ys) - max(ys)))


def walls_ready_for_station(turn, mem):
    # Carried vouchers and same-turn upgrade claims are not completed walls.
    walls = [w for w in turn.ours if w.kind == "wall"]
    return (set(getattr(turn, "planned_wall_sites", ())) <= {w.pos for w in walls}
            and all(w.level >= wall_level_limit(turn, w.pos) for w in walls)
            and not mem.wall_rebuild_levels)


def pioneer_needs_night_relief(turn):
    return bool(not turn.is_day and turn.pioneer and turn.pioneer.health <= 150)


def critical_station(turn, building, mem=None):
    """Reserve upgrades for healing strictly below the requested 500 HP."""
    return (building.kind == "station" and building.level < 3
            and 0 < building.health < 500)


def late_wall_phase(turn):
    return (turn.day >= RESOURCE_POLICY_DAY and turn.is_day
            and turn.weapons and all(w.level >= 3 for w in turn.weapons))


def staged_wall_purchase_allowed(turn, building, mem=None):
    """Stage wall purchases after maxing weapons, or for urgent rebuilding.

    Actual use follows observed levels and prefers feasible center-out stages;
    supplies budgets paid prerequisites before any additional deliveries.
    """
    return bool(building and building.kind == "wall"
                and building.level < wall_level_limit(turn, building.pos)
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
            and (critical_station(turn, building, mem) or walls_ready_for_station(turn, mem)
                 or late_wall_phase(turn) and set(getattr(turn, 'planned_wall_sites', ()))
                 <= {w.pos for w in turn.ours if w.kind == 'wall'}))


def delivery_trip(turn, nav, ledger, hero, shop_cell, targets):
    """Walk through every delivery and back from the final use tile."""
    home, exact = delivery_destination(turn, nav, ledger, hero)
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

    Critical base healing leads feasible upgrades, then rebuilt walls catch
    up. Ordinary walls follow level-3 weapons and precede healthy base upgrades.
    """
    if rebuilding_wall(turn, building, mem):
        return (-.75, building.level, building.id)
    if building.kind == "station":
        return (-1 if critical_station(turn, building, mem) else 2 if building.level == 1 else 3, building.id)
    if building.kind in WEAPONS:
        return (0 if building.level == 1 else 1, building.id)
    stage = wall_three_stage(turn, building) if building.level == 2 else (
        int(wall_sector(turn, building) != "front"), 0)
    if mem is not None and exposed_wall(turn, building, mem):
        return (1.25, building.level, *stage, -mem.wall_hits.get(building.pos, 0), building.health,
                turn.base_distance(building.pos), building.id)
    return (1.75, building.level, *stage, building.health, building.id)


def voucher_for(building):
    prefix = ("Weapon" if building.kind in WEAPONS else "Station" if building.kind == "station"
              else "Wall" if building.kind == "wall" else None)
    return f"{prefix}UpgradeVoucher{building.level}" if prefix and building.level < 3 else None


def station_voucher_needed(turn, hero, name):
    """Buy one usable current-tier base voucher for its actual carrier."""
    return bool(name and turn.station and hero and hero.kind == 'pioneer'
                and name == voucher_for(turn.station) and not hero.inventory[name])


def first_day_scout_upgrades(turn, mem, hero):
    """Only the recalled opening scout may spend day-one weapon vouchers."""
    return bool(mem and turn.day == 1 and turn.is_day and mem.night_scout.cancelled
                and hero.kind == 'worker' and mem.first_day_offense.get('worker_id') == hero.id
                and mem.first_day_offense.get('phase') in ('upgrade_buy', 'upgrade_use'))


def use_inventory(turn, nav, ledger, hero, local_only=False, mem=None,
                  target_only=None, name_only=None, urgent_only=False, upgrades_only=False,
                  target_sites=None):
    if (hero.health <= (165 if hero.kind == "worker" else 150) and hero.inventory["Medicine"]
            and (name_only is None or name_only == 'Medicine')):
        return ledger.add(hero.id, command("use", name="Medicine"))
    upgrades = []
    pioneer_emergency = hero.kind == 'pioneer' and pioneer_needs_night_relief(turn)
    for building in turn.ours:
        if building.kind == 'station' and hero.kind != 'pioneer':
            continue
        station_heal = (building.kind == 'station' and building.level < 3
                        and (critical_station(turn, building, mem) or pioneer_emergency))
        if target_sites is not None and building.pos not in target_sites:
            continue
        if building.kind == 'station' and not station_heal:
            continue
        if (turn.day == 1 and building.kind in WEAPONS
                and not first_day_scout_upgrades(turn, mem, hero)):
            continue
        if target_only is not None and building.pos != target_only:
            continue
        if urgent_only and not (station_heal
                                or rebuilding_wall(turn, building, mem)
                                or building.kind == 'wall' and building.health < 500):
            continue
        # Paid emergency healing competes with other feasible deliveries.
        if (building.kind == "station" and replacement_work_pending(turn, mem)
                and not station_heal):
            continue
        name = voucher_for(building)
        if (name and (name_only is None or name == name_only)
                and hero.inventory[name] and wall_upgrade_allowed(turn, building, mem, paid=True)
                and building.id not in ledger.upgrade_claims
                and not (mem and mem.movement.avoids(hero.id, building.pos))):
            if building.kind == 'station' and hero.kind == 'pioneer':
                # Emergency healing must not depend on a post-use dusk return.
                route = nav.approach(hero, building.cells, ledger.reserved)
            elif (building.kind == 'wall' and mem and turn.is_day and turn.day >= 4
                    and hero.id == mem.wall_watch_id):
                # Daylight delivery may use either side of the wall. Verify
                # the subsequent inside/post return separately, as at dusk.
                route = (nav.approach(hero, building.cells, ledger.reserved) if local_only
                         else dusk_route(turn, nav, ledger, hero, building.cells,
                                   home_override=return_destination(turn, nav, ledger, hero, mem)))
            else:
                route = (dusk_route(turn, nav, ledger, hero, building.cells)
                         if turn.is_day and (upgrades_only or turn.tick >= DUSK_SPEND_TICK or late_wall_phase(turn))
                         else nav.approach(hero, building.cells, ledger.reserved))
            if route and (not local_only or route[0] == 0):
                priority = (-1, building.id) if station_heal else upgrade_order(turn, building, mem)
                upgrades.append((priority, route[0], name, building, route))
        if (not turn.is_day and not upgrades_only and building.kind == "wall"
                and (name_only is None or name_only == 'WallFixer')
                and hero.inventory["WallFixer"]
                and needs_night_repair(building, turn, mem)
                and building.id not in ledger.repair_claims
                and not (mem and turn.day >= 4
                         and hero.id == mem.wall_watch_id)):
            route = nav.approach(hero, building.cells, ledger.reserved)
            if route and (not local_only or route[0] == 0):
                upgrades.append(((1.5 if building.health < 500 else 1.8, building.health, building.id), route[0], "WallFixer", building, route))
    if upgrades:
        # Stages are preferences among reachable, paid targets. A level-one
        # wall without its voucher, or an unreachable center, cannot stall V2.
        stages = [o[0][1:4] for o in upgrades
                  if o[2].startswith('WallUpgradeVoucher') and o[0][0] >= 0]
        if stages:
            upgrades = [o for o in upgrades if not o[2].startswith('WallUpgradeVoucher')
                        or o[0][0] < 0 or o[0][1:4] == min(stages)]
        previous = mem.upgrade_targets.get(hero.id) if mem else None
        # Keep emergency base healing and replacement-wall upgrades ahead of
        # the current delivery; hold the target among ordinary deliveries.
        nearest_tick = 0 if late_wall_phase(turn) else DUSK_SPEND_TICK if turn.day >= RESOURCE_POLICY_DAY else DUSK_USE_TICK
        if turn.is_day and turn.tick >= nearest_tick:
            # Finish nearby paid upgrades instead of crossing the base to
            # preserve a stale target. Emergency healing still goes first.
            key = lambda o: (min(0, o[0][0]), o[1], o[0], o[3].id)
        else:
            key = lambda o: (min(0, o[0][0]), o[3].pos != previous, o[0], o[1], o[3].id)
        _, _, name, building, route = min(upgrades, key=key)
        if route[1] is None:
            used = ledger.add(hero.id, command("use", building.nearest_cell(hero.pos), name=name))
            if used and mem:
                if building.kind == "wall":
                    from .wall_batches import start_batch
                    start_batch(turn, mem, ledger, hero, building.pos)
                mem.upgrade_targets.pop(hero.id, None)
            return used
        if ledger.add(hero.id, command("move", route[1])):
            if mem:
                if building.kind == "wall":
                    from .wall_batches import start_batch
                    start_batch(turn, mem, ledger, hero, building.pos)
                mem.upgrade_targets[hero.id] = building.pos
            (ledger.repair_claims if name == "WallFixer" else ledger.upgrade_claims).add(building.id)
            ledger.remember_work(hero, 'use', building.pos, name=name)
            return True
    return False


def supplies(turn, cfg, mem, nav, ledger, hero, reserve=0, urgent_only=False,
             planned=(), bulk=False, item_only=None, shop_only=None, quantity=None,
             target_sites=None):
    """Return an affordable, applicable purchase and its shopping route.

    Routes are checked before committing to shopping. A full backpack can be
    emptied by the worker before buying, and duplicate walking buyers reserve
    both their item and the shared gold for this decision.
    """
    opening_upgrades = first_day_scout_upgrades(turn, mem, hero)
    # The recalled scout can replace the cancelled robot trip with gun upgrades.
    if turn.day == 1:
        from .spending import first_day_purchase_reserve
        reserve = max(reserve, first_day_purchase_reserve(turn, mem, ledger))
        if (item_only and item_only != 'Medicine'
                and not (opening_upgrades and item_only.startswith('WeaponUpgradeVoucher'))):
            ledger.supply_reports.setdefault(hero.id, {'reasons': []})['reasons'].append(
                {'reason': 'first_day_boss_priority'})
            return None
    dusk = turn.is_day and turn.tick >= DUSK_SPEND_TICK
    # Ordinary dusk items remain single purchases. Walls may share one trip
    # only after the complete batch's delivery/return budget has been checked.
    bulk = bulk and not dusk
    shops = [p for p, k in turn.zones.items() if k == "weaponShop"
             and (shop_only is None or p == shop_only)]
    routes = [(r[0], p, r) for p in shops
              if not mem.movement.avoids(hero.id, p)
              and (r := nav.approach(hero, [p], ledger.reserved)) is not None]
    report = ledger.supply_reports.setdefault(hero.id, {'reasons': []})
    def reject(reason, **facts):
        if len(report['reasons']) < 8:
            report['reasons'].append(dict(reason=reason, **facts))
    if not routes:
        reject('no_shop_route')
        return None
    candidates = []
    from .wall_supply import active
    dedicated = active(turn, mem)
    paid_walls = []
    wounded = hero.health <= (165 if hero.kind == "worker" else 150)
    if not hero.inventory["Medicine"] and wounded:
        candidates.append(((-2 if hero.health <= 110 else -.5,), "Medicine", hero.cells))
    if not urgent_only and (turn.day > 1 or opening_upgrades):
        carried = Counter(item for h in turn.heroes for item in h.backpack)
        own_carried = hero.inventory.copy()
        buildings = list(turn.ours) + [w for w in planned if w.id < 0]
        first_level_gun = any(b.kind in WEAPONS and b.level == 1 for b in buildings)
        for building in sorted(buildings, key=lambda b: upgrade_order(turn, b, mem)):
            if opening_upgrades and building.kind not in WEAPONS:
                continue
            if (building.kind == 'station' and hero.kind != 'pioneer'
                    or dedicated and building.kind == 'wall' and hero.id != mem.wall_watch_id):
                continue
            if building.kind == 'station':
                if not station_voucher_needed(turn, hero, voucher_for(building)):
                    continue
                for level in (1, 2):
                    carried[f'StationUpgradeVoucher{level}'] = hero.inventory[f'StationUpgradeVoucher{level}']
            if target_sites is not None and building.pos not in target_sites:
                continue
            if building.kind == "station" and not station_purchase_allowed(turn, building, mem):
                continue
            name = voucher_for(building)
            if not name:
                continue
            if not wall_purchase_allowed(turn, building, mem):
                continue
            if building.kind == 'wall':
                from .wall_supply import priority_wall, defer_side_upgrades
                if defer_side_upgrades(turn, mem) and not priority_wall(turn, mem, building, ledger.wall_cells):
                    continue
            # Account for every voucher already carried, including vouchers
            # for the next level in a single shop trip. Never buy a duplicate.
            while name and carried[name] and wall_purchase_allowed(turn, building, mem):
                carried[name] -= 1
                own = own_carried[name] > 0
                if own:
                    own_carried[name] -= 1
                if building.kind == "wall" and own:
                    paid_walls.append((upgrade_order(turn, building, mem), name, building.cells))
                building = replace(building, level=building.level + 1, health=max(1000, building.health))
                # A paid prerequisite can fund the next tier in the same trip.
                # Actual use still waits for the observed wall level.
                name = voucher_for(building) if bulk and own else None
            if not name or not wall_purchase_allowed(turn, building, mem):
                continue
            urgent_wall = (rebuilding_wall(turn, building, mem)
                           or not first_level_gun and exposed_wall(turn, building, mem))
            if (building.kind == "wall" and not urgent_wall and not dusk
                    and any(b.level < 3 and b.kind in WEAPONS for b in buildings)):
                continue
            candidates.append((upgrade_order(turn, building, mem), name, building.cells))
    if target_sites is not None:
        candidates = [c for c in candidates if set(c[2]) & set(target_sites)]
        paid_walls = [c for c in paid_walls if set(c[2]) & set(target_sites)]
    if maxed_weapon_mining_phase(turn, cfg):
        # Shared by new allocation, the preemptive spending planner and old
        # buy jobs. Preserve medicine, critical base healing and real repairs.
        urgent_cells = {p for b in turn.ours for p in b.cells
                        if critical_station(turn, b, mem) or rebuilding_wall(turn, b, mem)
                        or b.kind == 'wall' and (b.health < 500 or exposed_wall(turn, b, mem))}
        allowed = [c for c in candidates if c[0][0] < 0 or set(c[2]) & urgent_cells]
        if len(allowed) < len(candidates):
            reject('mining_phase', until_tick=cfg.economy_rounds)
        candidates = allowed
    candidates.sort(key=lambda c: c[0])
    if not candidates:
        reject('no_applicable_item')
    seen = set()
    for _, name, destinations in candidates:
        if item_only is not None and name != item_only:
            continue
        if name in seen:
            continue
        seen.add(name)
        price = turn.shop.get(name)
        if (price is None or price < 0 or name in ledger.purchases
                or (hero.id, name) in mem.buy_failures):
            reject('item_unavailable_or_claimed', item=name)
            continue
        if price > ledger.gold - reserve:
            reject('insufficient_unreserved_gold', item=name, price=price, gold=ledger.gold, reserve=reserve)
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
        wall_batch = turn.day >= RESOURCE_POLICY_DAY and (dusk or late_wall_phase(turn)) and name.startswith("WallUpgradeVoucher")
        count = min(len(matches) if bulk or wall_batch else 1, max(1, hero.space),
                    (ledger.gold - reserve) // price if price else len(matches))
        if quantity is not None:
            count = min(count, quantity)
        options = []
        for _, shop, _ in routes:
            if ((dusk or late_wall_phase(turn)) and "UpgradeVoucher" in name
                    and not prerequisites):
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
                                to_shop[0] + 1)
                            if num:
                                options.append((-num, cost, to_shop, shop))
                            continue
                        # dusk_route reads day_left; charge shopping travel and
                        # the buy action through its action budget instead.
                        deliveries = [r for target in matches
                                      if (r := dusk_route(turn, nav, ledger, replace(hero, pos=cell),
                                                          target, actions=to_shop[0] + 2)) is not None]
                    finally:
                        turn.blocked = original
                    if deliveries:
                        options.append((-1, to_shop[0] + min(r[0] for r in deliveries), to_shop, shop))
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
                    strict_delivery = (opening_upgrades or name.startswith("WallUpgradeVoucher")
                                       or name == "WallFixer" or dusk)
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
                            options.append((-num, cost, to_shop, shop))
                            break
        if options:
            neg_num, _, route, shop = min(options, key=lambda o: o[:2])
            ledger.supply_targets[hero.id] = shop
            report['selected'] = dict(item=name, quantity=-neg_num, route_steps=route[0])
            return name, route, -neg_num
        reject('delivery_or_return_deadline', item=name, day_left=turn.day_left)
    return None


def buy_supply(turn, ledger, hero, plan):
    if not plan or not hero.space:
        return False
    if plan[0].startswith('StationUpgradeVoucher') and (
            plan[2] != 1 or not station_voucher_needed(turn, hero, plan[0])):
        return False
    name, route, num = plan
    if route[1] is None:
        return ledger.add(hero.id, command("buy", name=name, num=num))
    if ledger.add(hero.id, command("move", route[1])):
        ledger.purchases.add(name)
        ledger.gold -= turn.shop[name] * num
        ledger.remember_work(hero, 'buy', ledger.supply_targets.get(hero.id), name=name, quantity=num)
        return True
    return False


def refresh_stone_reserves(turn, cfg, mem, ledger):
    """Protect current blueprint stone before any daytime sale."""
    structures = {p for b in (*turn.ours, *turn.enemies) if b.kind not in HEROES for p in b.cells}
    missing = [p for p in ledger.wall_cells if p not in structures
               and p not in mem.build_failures and turn.zones.get(p, "land") == "land"]
    stone_need = len(missing) * cfg.wall_stones
    mem.stone_reserves.clear()
    from .wall_supply import active
    for h in sorted(turn.workers, key=lambda h: (h.id != mem.wall_repair_worker,
                                               -h.inventory["stone"], h.id)):
        if active(turn, mem) and h.id == mem.wall_watch_id:
            mem.stone_reserves[h.id] = 0
            continue
        if h.id == ledger.income_worker_id and h.id != mem.wall_repair_worker:
            mem.stone_reserves[h.id] = 0
            continue
        mem.stone_reserves[h.id] = min(stone_need, h.inventory["stone"])
        stone_need -= mem.stone_reserves[h.id]


def batch_sale_ready(turn, cfg, mem, nav, ledger, hero):
    """Small late loads justify travel only for a phase/return deadline."""
    counts = sale_inventory(turn, mem, hero)
    total = sum(counts.values())
    if not total:
        return False
    if (hero.id in mem.sale_workers or not hero.space
            or total >= min(hero.capacity, cfg.sell_batch)):
        return True
    home, exact = return_destination(turn, nav, ledger, hero)
    for vendor, kind in turn.zones.items():
        if kind != 'vendor':
            continue
        route = nav.approach(hero, [vendor], ledger.reserved)
        if route is None:
            continue
        # An adjacent sale has no outward journey to amortize.
        if route[0] == 0:
            return True
        trip = via(nav, hero, [[vendor], home], ledger.reserved, final_exact=exact,
                   future_return=turn.tick < cfg.economy_rounds) if home else route[0]
        if trip is None or trip + len(counts) + cfg.return_margin > turn.day_left:
            continue
        cutoff = 70 if hero.id == ledger.income_worker_id else min(60, mem.preparation_tick)
        phase_due = (hero.id not in mem.sold_workers
                     and cutoff - turn.tick <= route[0] + len(counts))
        return_due = turn.day_left <= trip + len(counts) + cfg.return_margin + 2
        if phase_due or return_due:
            return True
    return False


def dusk_resources(turn, cfg, mem, nav, ledger, tower_sites):
    """Sell complete batches or due loads, then spend feasible daylight."""
    if (not turn.is_day or maxed_weapon_mining_phase(turn, cfg)
            or turn.tick < cfg.economy_rounds and not late_wall_phase(turn)):
        return
    refresh_stone_reserves(turn, cfg, mem, ledger)
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    reserve = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    from .wall_supply import active
    for hero in turn.heroes:
        if active(turn, mem) and hero.kind == 'worker':
            continue
        if hero.id in ledger.used or hero.kind == "pioneer" and turn.phase_task:
            continue
        if hero.health <= 165 and hero.inventory["Medicine"]:
            if ledger.add(hero.id, command("use", name="Medicine")):
                continue
        if hero.id == ledger.income_worker_id:
            if batch_sale_ready(turn, cfg, mem, nav, ledger, hero):
                earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)
            continue
        # Do not reopen a vendor journey for each newly collected unit.
        sale_ready = hero.kind == 'worker' and batch_sale_ready(turn, cfg, mem, nav, ledger, hero)
        carrying = any("UpgradeVoucher" in k or k == "WallFixer" for k in hero.backpack)
        if (hero.kind == "worker" and not carrying
                and hero.id != mem.wall_repair_worker and sale_ready
                and earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)):
            continue
        # After firepower is ready, use any remaining daylight for a
        # deliverable purchase even when the vendor route is unavailable; the
        # earlier sale attempt has already taken priority when it was viable.
        if use_inventory(turn, nav, ledger, hero, mem=mem):
            continue
        if (hero.kind == "worker" and hero.id != mem.wall_repair_worker
                and sale_ready
                and earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)):
            continue
        if hero.kind != "worker" or hero.id == mem.wall_repair_worker:
            continue
        # Do not add a second delivery while a paid, applicable voucher waits.
        if any(hero.inventory[voucher_for(b)] for b in turn.ours if voucher_for(b)
               and (b.kind != "wall" or b.level < wall_level_limit(turn, b.pos))):
            continue
        plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve, planned=planned)
        buy_supply(turn, ledger, hero, plan)


def wall_keeps_access(turn, nav, ledger, target):
    if not turn.is_day or turn.tick >= DEFENCE_RETURN_TICK:
        return False
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
              if r.kind in CHARACTERS or r in turn.robots}
    built = {pos(c["targetPos"][0]) for c in ledger.commands.values() if c["action"] == "build"}
    turn.blocked = (original - mobile) | built
    heroes = []
    for hero in turn.heroes:
        cmd = ledger.commands.get(str(hero.id), {})
        heroes.append(replace(hero, pos=pos(cmd["targetPos"][-1])) if cmd.get("action") == "move" else hero)
    try:
        reachable = [(h, ds) for h in heroes for ds in destinations if nav.approach(h, ds)]
        unfinished_guns = ledger.tower_cells - {w.pos for w in turn.weapons} - built
        gun_routes = [(h, p, r[0]) for h in heroes for p in unfinished_guns
                      if (r := nav.approach(h, {p})) is not None]
        turn.blocked = turn.blocked | {target}
        # Day-one front closure takes priority over shortcuts to future guns.
        # Otherwise the builder can approach a gap, make its route through the
        # gap shorter, then reject that same wall and turn back toward a flank.
        # All future guns must still be reachable; retain the stricter detour
        # guard for other walls and later days.
        front_priority = (turn.day == 1 and turn.station is not None
                          and target in front_sites(turn, ledger.wall_cells))
        if any((r := nav.approach(h, {p})) is None or (not front_priority and r[0] > length)
               for h, p, length in gun_routes):
            return False
        if not all(nav.approach(h, ds) is not None for h, ds in reachable):
            return False
        positions = {h.id: h for h in heroes}
        # Preserve operator routes, including moves already planned this turn.
        # The shared tick cutoff above owns timing; do not add another margin.
        return all((route := (nav.search(positions[h.id], {ledger.operator_posts[h.id]})
                             if h.id in ledger.operator_posts else nav.approach(positions[h.id], w.cells))) is not None
                   for h, w in ledger.return_pairs)
    finally:
        turn.blocked = original


def build_options(turn, cfg, mem, nav, ledger, hero, sites, name_for, work_cell=None):
    """Share construction order with the material-trip forecast."""
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
        if name_for(index) == "wall" and cfg.layout_mode != "explicit" and turn.day != 1:
            # Shared edges, not diagonal contact: grow one continuous wall.
            # A move claim is not a built wall and cannot seed a second segment.
            if wall_chain and not any(abs(target[0]-p[0]) + abs(target[1]-p[1]) == 1
                                      for p in wall_chain):
                continue
            if not wall_chain and pending_wall:
                continue
            if missing_front and target not in front and target not in gaps:
                continue
        if (target == hero.pos or target in turn.blocked or target in ledger.reserved
                or target in ledger.build_claims or target in mem.build_failures
                or mem.movement.avoids(hero.id, target)):
            continue
        nearest = nav.approach(hero, [target], ledger.reserved, with_endpoint=True)
        pinned = (nav.search(hero, {work_cell}, ledger.reserved, with_endpoint=True)
                  if work_cell is not None and work_cell in neighbours(target) else None)
        route = (pinned if pinned and (nearest is None or pinned[0] <= nearest[0])
                 else nearest)
        if route:
            priority = (wall_priority(turn, cfg, sites, index, mem.wall_hits)[:3]
                        if name_for(index) == "wall" else
                        (0, int(turn.day == 1 and name_for(index) != "gatling"), route[0]))
            if name_for(index) == "wall":
                # Close gaps first, then retain the established expansion order.
                priority = (int(target not in gaps),) + priority[:3]
                if turn.day == 1:
                    # Prefer a connected segment, but never require it: an
                    # occupied extension must not suppress every other site.
                    connected = not wall_chain or any(
                        abs(target[0]-p[0]) + abs(target[1]-p[1]) == 1 for p in wall_chain)
                    priority += (int(not connected),)
            previous = mem.build_targets.get(hero.id)
            continuity = distance(target, previous) if name_for(index) == "wall" and previous else 0
            # Keep front/breach order, then choose the nearest reachable work
            # tile instead of the first blueprint index or a stale target.
            options.append((priority, route[0], continuity, index, target, route))
    return sorted(options)


def build(turn, cfg, mem, nav, ledger, hero, sites, name_for, work_cell=None):
    for _, _, _, index, target, route in build_options(
            turn, cfg, mem, nav, ledger, hero, sites, name_for, work_cell):
        name = name_for(index)
        if name in WEAPONS:
            # A worker walking to the first gatling has claimed its site but
            # has not spent its gold yet. Do not let another worker build a
            # rocket first or consume that budget before the gatling is built.
            if turn.day == 1 and name != "gatling" and any(
                    kind == "gatling" and ledger.commands.get(str(uid), {}).get("action") != "build"
                    for uid, kind in ledger.build_claims.values()):
                continue
            from .spending import first_day_purchase_reserve
            if ledger.gold - first_day_purchase_reserve(turn, mem, ledger) < cfg.weapon_cost:
                continue
        if name == "wall" and not wall_keeps_access(turn, nav, ledger, target):
            continue
        action = command("move", route[1]) if route[1] is not None else command("build", target, name=name)
        if ledger.add(hero.id, action):
            ledger.build_claims[target] = (hero.id, name)
            mem.build_targets[hero.id] = target
            if name == "wall":
                from .wall_batches import start_batch
                start_batch(turn, mem, ledger, hero, target)
            if route[1] is not None:
                ledger.remember_work(hero, 'build', target, name=name, work_cell=route[2])
            return True
    return False


def first_day_front(turn, cfg, mem, nav, ledger, wall_sites, excluded=()):
    """One active builder, no guards or waiting locks, ordinary work on failure."""
    if not turn.is_day or turn.day != 1 or not turn.station or turn.tick >= DEFENCE_RETURN_TICK:
        mem.day1_wall_worker = None
        mem.day1_wall_delivering = False
        return
    material_sites = [p for p in wall_sites
                      if p not in turn.blocked and p not in ledger.reserved
                      and p not in mem.build_failures and p not in ledger.build_claims]
    front = [p for p in front_sites(turn, wall_sites) if p in material_sites]
    if not front:
        mem.day1_wall_worker = None
        mem.day1_wall_delivering = False
        return
    options = []
    for hero in turn.workers:
        job = mem.daytime_jobs.get(hero.id, {})
        if (hero.id in ledger.used or hero.id in excluded or hero.health <= 165
                or any('UpgradeVoucher' in k or k == 'WallFixer' for k in hero.backpack)
                or job.get('kind') in ('sell', 'buy', 'use', 'robot_buy')
                or job.get('kind') == 'build' and job.get('name') != 'wall'):
            continue
        routes = [r[0] for p in front if not mem.movement.avoids(hero.id, p)
                  and (r := nav.approach(hero, [p], ledger.reserved)) is not None]
        if not routes or min(routes) + cfg.return_margin + 2 >= turn.day_left:
            continue
        held = hero.inventory['stone']
        # Carry a modest flank/rear reserve with the first front-wall batch,
        # capped by the existing batch size and actual perimeter demand.
        goal = min(cfg.stone_batch, len(material_sites) * cfg.wall_stones)
        front_goal = min(goal, len(front) * cfg.wall_stones)
        delivering = hero.id == mem.day1_wall_worker and mem.day1_wall_delivering and held > 0
        # A full/partial carried batch is spent before gathering again. Only
        # gather while starting the job empty or continuing its material trip.
        gathering = (held < cfg.wall_stones or hero.id == mem.day1_wall_worker and not delivering)
        mines = []
        if gathering and held < goal and hero.space:
            for p, kind in turn.zones.items():
                if (kind != 'stone' or turn.mine_remain.get(p, 1) == 0
                        or p in mem.collect_failures or mem.movement.avoids(hero.id, p)):
                    continue
                trip = via(nav, hero, [[p], front], ledger.reserved)
                # Let the complete material forecast shrink the reserve near
                # dusk; extra flank stone must not prevent a viable front trip.
                if trip is not None and trip + max(0, front_goal - held) + len(front) + cfg.return_margin < turn.day_left:
                    mines.append((trip, p))
        if not mines and held < cfg.wall_stones:
            continue
        options.append((hero.id != mem.day1_wall_worker, held < cfg.wall_stones,
                        min(mines)[0] if mines else min(routes), hero.id, hero, mines, goal))
    for _, _, _, _, hero, mines, goal in sorted(options):
        if mines:
            accepted = mine(turn, cfg, mem, nav, ledger, hero, want_stone=True,
                            target_only=min(mines)[1], stone_goal=goal, stone_sites=material_sites)
            delivering = False
        else:
            accepted = build(turn, cfg, mem, nav, ledger, hero, front, lambda _: 'wall')
            delivering = True
        if accepted:
            mem.day1_wall_worker = hero.id
            mem.day1_wall_delivering = delivering
            mem.daytime_jobs.pop(hero.id, None)
            # Reevaluate every turn: no stale material/build job after the
            # front fills or becomes blocked. Other workers' jobs stay intact.
            ledger.work_jobs.pop(hero.id, None)
            return
    mem.day1_wall_worker = None
    mem.day1_wall_delivering = False


def finish_preparation(turn, cfg, mem, nav, ledger, hero, tower, wall_sites):
    """Spend one local preparation action only with a checked route to a gun."""
    if turn.tick >= DEFENCE_RETURN_TICK:
        return False
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

    def accepted(hero):
        if hero.id == ledger.income_worker_id:
            emit_event('income_preempted', dict(actor=hero.id, reason='wall_breach'), 'economy')
            ledger.income_worker_id = None
        return gaps

    options = []
    for hero in free:
        if hero.health <= 110 or mem.daytime_jobs.get(hero.id, {}).get("kind") == "sell":
            continue
        reachable = [p for p in sorted(gaps) if not mem.movement.avoids(hero.id, p)
                     and (r := nav.approach(hero, [p], ledger.reserved)) is not None
                     and r[0] + 1 + cfg.return_margin < turn.day_left
                     and wall_keeps_access(turn, nav, ledger, p)]
        if not reachable:
            continue
        if turn.day > 1:
            from .wall_batches import face_sites
            anchor = min(reachable, key=lambda p: (nav.approach(hero, [p], ledger.reserved)[0], p))
            face = face_sites(wall_sites, anchor)
            reachable = [p for p in reachable if p in face]
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
            options.append((hero.id == ledger.income_worker_id,
                            hero.id != mem.wall_repair_worker, -min(held, goal), trip,
                            hero.id, hero, reachable, goal))
    for _, _, _, _, _, hero, sites, goal in sorted(options, key=lambda o: o[:5]):
        if hero.id != mem.wall_repair_worker:
            mem.wall_repair_delivering = False
        mem.wall_repair_worker = hero.id
        if hero.health <= 165 and hero.inventory["Medicine"]:
            if ledger.add(hero.id, command("use", name="Medicine")):
                return accepted(hero)
        held = hero.inventory["stone"]
        if held < cfg.wall_stones:
            mem.wall_repair_delivering = False
        # Once delivery starts, finish the carried batch rather than refilling
        # after each wall or switching to a shopping trip.
        if not mem.wall_repair_delivering and held < goal:
            if mine(turn, cfg, mem, nav, ledger, hero, want_stone=True,
                    stone_goal=goal, stone_sites=sites):
                from .wall_batches import start_batch
                start_batch(turn, mem, ledger, hero, sites[0])
                return accepted(hero)
        if held >= cfg.wall_stones:
            mem.wall_repair_delivering = True
            if build(turn, cfg, mem, nav, ledger, hero, sites, lambda _: "wall"):
                mem.mine_targets.pop(hero.id, None)
                return accepted(hero)
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
    if hero.id == ledger.income_worker_id:
        if not use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem, urgent_only=True):
            earn(turn, cfg, mem, nav, ledger, hero, deadline=70)
        return
    if hero.inventory["WallFixer"] and use_inventory(turn, nav, ledger, hero, mem=mem):
        return
    if not develop:
        if use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem):
            return
        earn(turn, cfg, mem, nav, ledger, hero, deadline)
        return
    # Deliver paid vouchers before sales or ordinary shopping.
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
            if mine(turn, cfg, mem, nav, ledger, hero, want_stone=True,
                    stone_goal=min(cfg.stone_batch, missing * cfg.wall_stones), stone_sites=stone_sites):
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
                local_only=hero.inventory["stone"] >= cfg.wall_stones, stone_goal=stone_goal,
                stone_sites=stone_sites):
            return
    if need_walls and hero.inventory["stone"] >= cfg.wall_stones:
        if build(turn, cfg, mem, nav, ledger, hero, wall_sites, lambda _: "wall"):
            return
    if earn(turn, cfg, mem, nav, ledger, hero, deadline):
        return
    # Failed construction/shopping is not a reason to stop working at the
    # preparation cutoff. A short stockpile action must fit the return route;
    # try it only after paid delivery and executable development work.
    if deadline is not None:
        max_steps = 0 if turn.tick >= cfg.economy_rounds and sale_inventory(turn, mem, hero) else 2
        if spare_mine(turn, cfg, mem, nav, ledger, hero, max_steps=max_steps):
            return
    if turn.station and not late_economy_allowed(turn, cfg, mem, ledger, hero):
        from .traffic import park_idle_worker, transit_cells
        home = nav.approach(hero, turn.station.cells, ledger.reserved, with_endpoint=True)
        critical = transit_cells(turn, nav, ledger) | set(ledger.operator_posts.values())
        if (hero.pos in critical or home and home[2] in critical
                or ledger.traffic_forbidden.get(hero.id)):
            if park_idle_worker(turn, nav, ledger, hero):
                return
        walk(nav, ledger, hero, turn.station.cells)



def workers(turn, cfg, mem, nav, ledger, tower_sites, wall_sites, excluded=()):
    """Allocate only today's outstanding jobs; all other worker time earns gold."""
    from .wall_supply import active, other_worker
    if active(turn, mem):
        for hero in turn.workers:
            if hero.id in ledger.used or hero.id in excluded:
                continue
            if hero.id == mem.wall_watch_id:
                if turn.tick < cfg.economy_rounds:
                    job = mem.daytime_jobs.get(hero.id, {})
                    if job.get('kind') in ('mine', 'sell', 'spare') and not job.get('want_stone'):
                        from .worker_jobs import _continue
                        if _continue(turn, cfg, mem, nav, ledger, hero, job, tower_sites, wall_sites)[0]:
                            continue
                    earn(turn, cfg, mem, nav, ledger, hero, deadline=cfg.economy_rounds)
            else:
                other_worker(turn, cfg, mem, nav, ledger, hero, tower_sites, wall_sites)
        return
    if ledger.income_worker_id is None:
        reserve_income_worker(turn, mem, nav, ledger, excluded)
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
    trading = {h.id for h in free if trade_available(turn, mem, nav, h)}
    planned = planned_weapons(turn, cfg, mem, tower_sites)
    work_remains = development_pending(turn, cfg, mem, tower_sites, wall_sites)
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
    if maxed_weapon_mining_phase(turn, cfg):
        # Stale sale/supply flags and spare packs cannot start ordinary
        # development before the cutoff. Paid voucher delivery remains useful.
        developing = {h.id for h in free if any('UpgradeVoucher' in k for k in h.backpack)}
    developing.discard(ledger.income_worker_id)
    mem.preparation_workers.discard(ledger.income_worker_id)
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
                repair_buyers.append((h.id == ledger.income_worker_id,
                                      h.id != mem.supply_worker, plan[1][0], h.id))
    repair_buyer = min(repair_buyers)[3] if repair_buyers else None
    if repair_buyer is not None:
        developing.add(repair_buyer)
        if repair_buyer == ledger.income_worker_id:
            emit_event('income_preempted', dict(actor=repair_buyer, reason='wall_rebuild'), 'economy')
            ledger.income_worker_id = None
    wall_sites = [p for p in wall_sites if p not in repair_sites]
    # Only the assigned carrier's stone funds its repair batch. Everyone else
    # keeps the original construction, shopping and income allocation.
    stone_need = sum(p not in repair_sites for p in missing) * cfg.wall_stones
    mem.stone_reserves.clear()
    for h in sorted(turn.workers, key=lambda h: (-h.inventory["stone"], h.id)):
        if h.id == mem.wall_repair_worker:
            mem.stone_reserves[h.id] = min(len(repair_sites) * cfg.wall_stones, h.inventory["stone"])
        elif h.id == ledger.income_worker_id:
            mem.stone_reserves[h.id] = 0
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
        if any(h.inventory[voucher_for(b)] for b in turn.ours
                                          if voucher_for(b) and wall_upgrade_allowed(turn, b, mem, paid=True)):
            continue
        p = supplies(turn, cfg, mem, nav, ledger, h, reserve, planned=planned, bulk=True)
        if p:
            buyers.append((p[1][0], h.id))
    # Keep the same courier through buying and delivery. Reassign only when
    # its work finishes, becomes infeasible, or the worker disappears.
    carrying = next((h for h in free if h.id == mem.supply_worker
                     and any("UpgradeVoucher" in k for k in h.backpack)), None)
    eligible = {uid for _, uid in buyers}
    planned_buyer = ledger.spending_plan.get('purchase_actor')
    if planned_buyer in eligible and not carrying:
        buyer = planned_buyer
    elif mem.supply_worker in eligible and not carrying:
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
    if mem.supply_worker not in ledger.protected_supply_workers:
        mem.supply_worker = buyer
    # Complete the configured wall blueprint; the default surrounds the base except for its side gate.
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
        if hero.id not in ledger.used:
            vacate_site(turn, nav, ledger, hero, sites)


def treasure_purchase_plan(turn, cfg, mem, nav, ledger, hero):
    """One material target for both reservation and execution; no clue guessing."""
    if (not cfg.llm_enabled or not hero or not turn.is_day or turn.day == 1
            or turn.phase_task or not treasure_active(turn, mem)):
        return None
    t = mem.treasure
    required = (Counter(t["items"]) if t else
                Counter({i["name"]: i["quantity"] for i in mem.treasure_materials["items"]}))
    missing = required - hero.inventory
    if not missing:
        return None
    emergency = (any(h.health <= (150 if h.kind == "pioneer" else 110) for h in turn.heroes)
                 or any(w.kind == "wall" and w.health < 500 for w in turn.ours)
                 or bool(turn.station and turn.station.health < 500)
                 or any(turn.threatens_us(r) and (turn.base_distance(r.pos) <= max(6, r.attack_range)
                        or distance(hero.pos, r.pos) <= max(6, r.attack_range + 2)) for r in turn.robots))
    reason = ("emergency" if emergency else "backpack_full" if hero.space < sum(missing.values())
              else "item_unavailable" if any(k not in turn.shop or turn.shop[k] < 0 for k in missing)
              else "buy_retry_cooldown" if any((hero.id, k) in mem.buy_failures for k in missing)
              else "no_feasible_trip")
    cost = sum(turn.shop.get(k, 0) * n for k, n in missing.items())
    best = None
    if reason == "no_feasible_trip":
        home, exact = return_destination(turn, nav, ledger, hero, mem)
        for shop, kind in turn.zones.items():
            if kind != "weaponShop" or mem.movement.avoids(hero.id, shop):
                continue
            route = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
            if route is None:
                continue
            groups = [[shop]] + ([[tuple(t["position"])]] if t else [])
            travel = via(nav, hero, groups, ledger.reserved)
            if travel is None:
                continue
            wait = max(0, t["startRound"] - (turn.round + travel + len(missing))) if t else 0
            if t and turn.round + travel + len(missing) + wait > t["endRound"]:
                continue
            # Buying can happen days before opening; today only the shopping
            # and return trip must fit. The full trip must still meet expiry.
            total = via(nav, hero, [[shop]] + ([home] if home else []), ledger.reserved,
                        final_exact=exact if home else False, future_return=bool(home))
            if total is None or total + len(missing) + cfg.return_margin > turn.day_left:
                continue
            slack = turn.day_left - total - len(missing) - cfg.return_margin
            if t:
                slack = min(slack, t['endRound'] - turn.round - travel - len(missing) - wait)
            nav.vehicle_budgets[('treasure_shop', route[2])] = (route[0] + slack, shop)
            candidate = (travel, shop, route)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best:
            reason = "shopping" if ledger.gold >= cost else "saving_gold"
    mem.trace_treasure(turn, "treasure_prepare", dedupe=True, reason=reason,
                       missing=dict(missing), missing_cost=cost, gold=ledger.gold,
                       day_left=turn.day_left)
    return (missing, best[2], cost) if best else None


def reserve_treasure_gold(turn, cfg, mem, nav, ledger, hero):
    plan = treasure_purchase_plan(turn, cfg, mem, nav, ledger, hero)
    amount = min(plan[2], max(0, ledger.gold)) if plan else 0
    mem.trace_treasure(turn, "treasure_reserve", dedupe=True,
                       reason="reserved" if amount else "not_actionable", reserved=amount)
    return amount


def prepare_treasure(turn, cfg, mem, nav, ledger, hero):
    plan = treasure_purchase_plan(turn, cfg, mem, nav, ledger, hero)
    if not plan or ledger.gold < plan[2]:
        return False
    missing, route, _ = plan
    name = next(iter(missing))
    if route[1] is not None:
        limit, shop = nav.vehicle_budgets.get(('treasure_shop', route[2]), (route[0], None))
        return nav.move_along(hero, ledger, route, {route[2]}, max_rounds=limit,
                              targets={shop} if shop else ())
    return ledger.add(hero.id, command("buy", name=name, num=missing[name]))


def pioneer_task_options(turn, cfg, mem, nav, ledger, hero):
    return task_options(turn, cfg, mem, nav, ledger, hero)


def pioneer(turn, cfg, mem, nav, ledger, hero, shopping=True):
    if use_inventory(turn, nav, ledger, hero, mem=mem):
        if mem.treasure:
            mem.trace_treasure(turn, "treasure_progress", dedupe=True, reason="use_inventory")
        return
    # The pioneer can carry its own medicine; there is no transfer action.
    if shopping and not hero.inventory["Medicine"] and hero.health <= 150:
        plan = supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True)
        if buy_supply(turn, ledger, hero, plan):
            if mem.treasure:
                mem.trace_treasure(turn, "treasure_progress", dedupe=True, reason="medicine_supply")
            return
    t = mem.treasure
    if t and treasure_active(turn, mem):
        required = Counter(t["items"])
        missing = required - hero.inventory
        route = nav.approach(hero, [tuple(t["position"])], ledger.reserved, with_endpoint=True)
        reason = ("no_route" if not route else "cannot_arrive_in_time"
                  if turn.round + route[0] > t["endRound"] else "missing_items" if missing
                  else "too_early" if turn.round + route[0] < t["startRound"] else "approach_or_summon")
        if missing and reason == "missing_items":
            name = next(iter(missing))
            num = missing[name]
            reason = ("item_unavailable" if name not in turn.shop else "backpack_full" if hero.space < num
                      else "insufficient_gold" if ledger.gold < turn.shop[name] * num else "shopping")
        state = (reason, tuple(sorted(missing.items())), tuple(t["position"]), t["startRound"], t["endRound"])
        # Snapshot changing counters only when the stage changes, not every movement round.
        if mem.treasure_trace_state.get("progress_snapshot") != state:
            mem.treasure_trace_state["progress_snapshot"] = state
            mem.trace_treasure(turn, "treasure_progress", reason=reason, position=hero.pos,
                               missing=dict(missing), inventory=dict(hero.inventory), gold=ledger.gold,
                               space=hero.space, route_steps=route[0] if route else None,
                               window=[t["startRound"], t["endRound"]],
                               remaining=t["endRound"] - turn.round)
        if route and turn.round + route[0] <= t["endRound"]:
            if missing:
                name = next(iter(missing))
                num = missing[name]
                if shopping and prepare_treasure(turn, cfg, mem, nav, ledger, hero):
                    return
            elif turn.round + route[0] + cfg.return_margin >= t["startRound"]:
                if route[1] is not None:
                    nav.move_along(hero, ledger, route, {route[2]},
                                   targets={tuple(t['position'])},
                                   max_rounds=t['endRound'] - turn.round)
                elif turn.round >= t["startRound"]:
                    if ledger.add(hero.id, command("summonTreasure", tuple(t["position"]), item=t["items"])):
                        mem.treasure_attempted = True
                        mem.trace_treasure(turn, "treasure_summon", actor=hero.id,
                                           position=hero.pos, target=t["position"], items=t["items"])
                if hero.id not in ledger.used:
                    ledger.used.add(hero.id)
                    ledger.explain(hero.id, "treasure_wait_opening", start=t["startRound"])
                return
    elif t:
        mem.trace_treasure(turn, "treasure_progress", dedupe=True,
                           reason="done" if mem.treasure_done else "already_attempted"
                           if mem.treasure_attempted else "expired")
    if cfg.llm_enabled and shopping and prepare_treasure(turn, cfg, mem, nav, ledger, hero):
        return
    from .wall_supply import prepare_station
    if shopping and prepare_station(turn, cfg, mem, nav, ledger, hero):
        return
    if not cfg.llm_enabled:
        return
    options = pioneer_task_options(turn, cfg, mem, nav, ledger, hero)
    previous = mem.task_target
    selected = choose_task(options, mem)
    if selected:
        point, task, route = selected['point'], selected['task'], selected['route']
        reason = ('task_travel' if route[1] is not None else
                  'task_cooldown_wait' if selected['cooldown'] else 'task_ready')
        details = {k: selected[k] for k in ('point', 'cell', 'cooldown', 'start', 'finish',
                   'return_steps', 'budget', 'count', 'horizon', 'next_point')}
        details.update(day_left=turn.day_left, route_steps=route[0], target_changed=previous != point)
        # One compact scheduling record; task contents never enter this planner.
        emit_event('task_plan', dict(reason=reason, **details), 'evolution')
        if route[1] is not None:
            limit = turn.day_left - selected['budget']['rounds'] - selected['return_steps'] - cfg.return_margin - 1
            cutoff = (FIRST_DAY_PIONEER_SHOP_TICK - turn.tick - 1 if turn.day == 1 else
                      DEFENCE_RETURN_TICK - turn.tick - selected['budget']['rounds'])
            limit = min(limit, cutoff)
            if t and treasure_active(turn, mem):
                # The task/altar sequence was checked with pedestrian costs.
                # Keep that arrival bound while a treasure window is active.
                limit = min(limit, route[0])
            if nav.move_along(hero, ledger, route, {selected['cell']}, selected['travel_forbidden'],
                              journey=('task', point), max_rounds=limit):
                ledger.explain(hero.id, reason, **details)
        elif selected['cooldown'] or not task.get('isValid'):
            ledger.used.add(hero.id)
            ledger.explain(hero.id, 'task_cooldown_wait', **details)
        elif (cfg.llm_enabled and mem.news and
              ((mem.pending and mem.pending[0] == "news")
               or (mem.news_dirty and mem.calls < cfg.daily_llm_limit))):
            ledger.used.add(hero.id)
            ledger.explain(hero.id, "news_before_evolution", pending=mem.pending)
        elif ledger.add(hero.id, command("acceptTask")):
            mem.task_point = point
            mem.task_timeout = int(task.get("timeoutRounds", cfg.task_max_rounds))
            mem.accepted_round = turn.round
            mem.log_task_id = f"{mem.log_session}/r{turn.round}"
            mem.log_task_type = task.get("taskType", "自进化类")
            update_context(task_id=mem.log_task_id, task_type=mem.log_task_type)
            logging.getLogger(__name__).info("task_accept point=%s", point)
    else:
        ledger.explain(hero.id, 'no_task_fits_daylight', day_left=turn.day_left,
                       previous_target=previous)
        if previous is not None:
            emit_event('task_plan', dict(reason='target_unavailable_or_budget',
                       previous_target=previous, day_left=turn.day_left), 'evolution')
