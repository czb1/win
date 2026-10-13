"""Reserve construction/upgrades and spend the remaining gold on robots."""
from .economy_plan import front_sites, planned_weapons, via
from .model import DEFENCE_RETURN_TICK

FIRST_DAY_ROBOT_PURCHASE_LIMIT = 3


def first_day_purchase_reserve(turn, mem, ledger, include_robots=True):
    """Protect the base heal and unbought opening orders, counting trips once."""
    if not turn.is_day or turn.day != 1:
        return 0
    from .brain import observe_robot_summons
    from .economy import voucher_for
    from .robot_assault import enemy_base
    state = observe_robot_summons(turn, mem)
    name = voucher_for(turn.station) if turn.station else None
    held = any(name in (role.get('backpack') or [])
               for role in turn.raw.get('teamOur', {}).get('roles', [])) if name else False
    reserve = (max(0, turn.shop.get(name, 0)) if name and not held
               and not state['station_purchased'] and name not in ledger.purchases else 0)
    if include_robots and enemy_base(turn) is not None:
        remaining = max(0, FIRST_DAY_ROBOT_PURCHASE_LIMIT - state['purchased'])
        planned = ledger.spending_plan.get('robot_purchase')
        if planned and ledger.commands.get(str(planned['actor']), {}).get('action') != 'buy':
            # Walking has already removed this batch's money from ledger.gold.
            remaining = max(0, remaining - planned['quantity'])
        reserve += remaining * max(0, turn.shop.get('BossRobotSummonOrder', 0))
    return reserve


def heal_station_with_pioneer(turn, mem, nav, ledger):
    """Keep the pioneer's base voucher until HP is strictly below 500."""
    from .economy import critical_station, use_inventory, voucher_for
    hero, station = turn.pioneer, turn.station
    if (not hero or hero.id in ledger.used or not station
            or not critical_station(turn, station, mem)):
        return False
    name = voucher_for(station)
    if not hero.inventory[name]:
        return False
    return use_inventory(turn, nav, ledger, hero, mem=mem,
                         target_only=station.pos, name_only=name, upgrades_only=True)


def prepare_station_reserve(turn, cfg, mem, nav, ledger):
    """Buy the opening base heal before allocating money to robots or guns."""
    from .brain import observe_robot_summons
    from .commands import command
    from .economy import voucher_for
    from .mining import return_destination
    hero, station = turn.pioneer, turn.station
    state = observe_robot_summons(turn, mem)
    if (not turn.is_day or turn.day != 1 or turn.tick >= DEFENCE_RETURN_TICK
            or not hero or hero.id in ledger.used or turn.phase_task or not hero.space
            or not station or state.get('station_purchased')):
        return False
    name = voucher_for(station)
    if (not name or name not in turn.shop or turn.shop[name] < 0
            or (hero.id, name) in mem.buy_failures or name in ledger.purchases
            or any(name in (role.get('backpack') or [])
                   for role in turn.raw.get('teamOur', {}).get('roles', []))):
        return False
    if ledger.gold < turn.shop[name]:
        return False
    home, exact = return_destination(turn, nav, ledger, hero, mem)
    if not home:
        return False
    options = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
            continue
        route = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
        if route is None:
            continue
        trip = via(nav, hero, [[shop], home], ledger.reserved,
                   final_exact=exact, future_return=True)
        if trip is not None and trip + 1 + cfg.return_margin <= turn.day_left:
            options.append((route[0], shop, route))
    if not options:
        return False
    _, shop, route = min(options)
    if route[1] is None:
        accepted = ledger.add(hero.id, command('buy', name=name, num=1))
        if accepted:
            state['station_purchased'] = True
            state['station_pending'] = turn.round, hero.id
    else:
        accepted = nav.move_along(hero, ledger, route, {route[2]})
        if accepted:
            ledger.gold -= turn.shop[name]
            ledger.purchases.add(name)
    if accepted:
        ledger.explain(hero.id, 'reserve_station_healing', item=name, quantity=1,
                       station_health=station.health, heal_below=500,
                       shop=shop, purchase_submitted=route[1] is None)
    return accepted


def prepare_paid_upgrades(turn, mem, nav, ledger, heroes, excluded=()):
    """Deliver feasible paid vouchers before spending an actor on robots."""
    if not turn.is_day or turn.day == 1:
        return
    from .economy import use_inventory
    for hero in sorted(heroes, key=lambda h: (h.id != mem.supply_worker, h.id)):
        if (hero.id in ledger.used or hero.id in excluded
                or hero.kind == 'pioneer' and turn.phase_task
                or mem.daytime_jobs.get(hero.id, {}).get('kind') in ('use', 'sell')
                or not any('UpgradeVoucher' in item for item in hero.backpack)):
            continue
        if use_inventory(turn, nav, ledger, hero, mem=mem, upgrades_only=True):
            mem.daytime_jobs.pop(hero.id, None)
            if hero.kind == 'worker':
                ledger.protected_supply_workers.add(hero.id)
                if mem.supply_worker is None:
                    mem.supply_worker = hero.id


def prepare_defense_purchase(turn, mem, ledger, reserve):
    """Submit the selected buyer before a robot can reserve its first step."""
    purchase, ledger.defense_purchase = ledger.defense_purchase, None
    if purchase is None:
        return reserve
    from .economy import buy_supply
    uid, plan = purchase
    before = ledger.gold
    if buy_supply(turn, ledger, turn.units[uid], plan):
        mem.daytime_jobs.pop(uid, None)
        mem.supply_worker = uid
        ledger.protected_supply_workers.add(uid)
        reserve = max(0, reserve - (before - ledger.gold))
        ledger.spending_plan.update(gold_available=ledger.gold, reserved_gold=reserve,
                                    surplus=max(0, ledger.gold - reserve))
    return reserve


def first_day_guns_built(turn, cfg):
    """Observed gun completion for scouting, independent of order purchases."""
    return turn.day == 1 and len(turn.weapons) >= min(3, len(cfg.loadout))


def defenses_ready(turn, cfg, towers, walls, mem=None, ledger=None):
    """Observed readiness diagnostic; robot shopping does not require it."""
    built = {w.pos: w for w in turn.ours if w.kind == 'wall'}
    count = min(len(towers), len(cfg.loadout), 3)
    if not (turn.station and turn.weapons and built
            and set(walls) <= set(built)
            and set(towers[:count]) <= {w.pos for w in turn.weapons}
            and len(turn.weapons) >= count
            and all(w.level >= 3 for w in turn.weapons)
            and all(built[p].level >= 2 for p in walls)
            and all(built[p].level >= 3 for p in front_sites(turn, walls))):
        return False
    if mem and mem.wall_rebuild_levels:
        return False
    watcher = next((h for h in turn.workers if mem and h.id == mem.wall_watch_id), None)
    if watcher:
        missing = max(0, mem.wall_watch.stock_target(turn) - watcher.inventory['WallFixer'])
        funded = ledger.watch_pack_slots.get(watcher.id, 0) if ledger else 0
        if missing > funded:
            return False
    return True


def plan_day_spending(turn, cfg, mem, nav, ledger, towers, excluded=()):
    """Reserve only next purchases with a real actor and feasible delivery.

    Same-item alternatives are one batch, not duplicated obligations. Current
    stock-trips and continued purchases have already reserved ledger gold.
    """
    if turn.day == 1:
        reserve = min(ledger.gold, first_day_purchase_reserve(turn, mem, ledger,
                                                             include_robots=False))
        ledger.defense_purchase = None
        ledger.spending_plan.update(gold_available=ledger.gold, reserved_gold=reserve,
                                   surplus=max(0, ledger.gold - reserve), purchases={},
                                   medical_gold_reserved=0, priority_actors=[], purchase_actor=None,
                                   day_left=turn.day_left)
        return set(), reserve
    from .economy import supplies, build_options
    planned = planned_weapons(turn, cfg, mem, towers)
    construction = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    actors, purchases, buyers = set(), {}, []
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in excluded]
    for hero in free:
        if not hero.space:
            continue
        next_purchase = supplies(turn, cfg, mem, nav, ledger, hero, construction,
                                 planned=planned, bulk=True)
        options = [next_purchase]
        if next_purchase:
            buyers.append((hero.id != mem.supply_worker, next_purchase[1][0], hero.id, next_purchase))
        # A healthy base can be planned alongside a wall batch, rather than
        # losing its money to a robot while the courier is delivering walls.
        if turn.station and turn.station.level < 3:
            options.append(supplies(turn, cfg, mem, nav, ledger, hero, construction,
                                    planned=planned, item_only=f'StationUpgradeVoucher{turn.station.level}'))
        for plan in options:
            if plan:
                name, _, quantity = plan
                purchases[name] = max(purchases.get(name, 0), turn.shop[name] * quantity)
    selected = min(buyers, key=lambda row: row[:3]) if buyers else None
    buyer = selected[2] if selected else None
    ledger.defense_purchase = (buyer, selected[3]) if selected else None
    if buyer is not None:
        actors.add(buyer)
    # Protect real construction assignments, rather than every possible helper.
    claimed = set()
    if construction and ledger.gold >= cfg.weapon_cost:
        missing_weapons = [w for w in planned if w.id < 0]
        for hero in sorted(free, key=lambda h: h.id):
            if len(claimed) >= ledger.gold // cfg.weapon_cost:
                break
            if hero.id == buyer:
                continue
            for _, _, _, _, target, _ in build_options(
                    turn, cfg, mem, nav, ledger, hero, [w.pos for w in missing_weapons],
                    lambda index: missing_weapons[index].kind):
                if target not in claimed:
                    actors.add(hero.id)
                    claimed.add(target)
                    break
    if any(h.id == mem.wall_repair_worker for h in free):
        actors.add(mem.wall_repair_worker)
    reserve = min(ledger.gold, construction + sum(purchases.values()))
    ledger.spending_plan = dict(gold_available=ledger.gold, reserved_gold=reserve,
                               surplus=max(0, ledger.gold-reserve), purchases=purchases,
                               medical_gold_reserved=purchases.get('Medicine', 0),
                               priority_actors=sorted(actors), purchase_actor=buyer,
                               day_left=turn.day_left)
    return actors, reserve


def robot_purchase_plan(turn, cfg, mem, nav, ledger, hero, name, shop_only=None,
                        quantity=1, reserved_uses=0):
    """Leave a daylight action for the purchase and for every carried/new use."""
    if not turn.is_day or not hero.space or (hero.id, name) in mem.buy_failures:
        return None
    options = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or shop_only is not None and shop != shop_only:
            continue
        if mem.movement.avoids(hero.id, shop):
            continue
        route = nav.approach(hero, [shop], ledger.reserved)
        if route is None:
            continue
        count = min(quantity, hero.space, turn.day_left - route[0] - 1 - reserved_uses)
        if count > 0:
            options.append((-count, route[0], shop, route))
        else:
            ledger.spending_plan['robot_blocked'] = 'daylight_summon_deadline'
    if not options:
        return None
    negative_count, _, shop, route = min(options)
    return -negative_count, shop, route
