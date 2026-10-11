"""Reserve construction/upgrades and spend the remaining gold on robots."""
from .economy_plan import front_sites, planned_weapons


def prepare_paid_upgrades(turn, mem, nav, ledger, heroes, excluded=()):
    """Deliver feasible paid vouchers before spending an actor on robots."""
    if not turn.is_day or turn.day == 1:
        return
    from .economy import use_inventory
    for hero in sorted(heroes, key=lambda h: (h.id != mem.supply_worker, h.id)):
        if (hero.id in ledger.used or hero.id in excluded
                or hero.kind == 'pioneer' and turn.phase_task
                or mem.daytime_jobs.get(hero.id, {}).get('kind') == 'use'
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


def first_day_boss_phase(turn, cfg):
    """Opening gold funds the configured guns first, then only BOSS orders."""
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
    from .economy import supplies, build_options
    planned = planned_weapons(turn, cfg, mem, towers)
    construction = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    actors, purchases, buyers = set(), {}, []
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in excluded]
    # Opening surplus belongs to BOSS orders, not vouchers or night stock.
    for hero in (free if turn.day > 1 else ()):
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


def robot_purchase_plan(turn, cfg, mem, nav, ledger, hero, name, shop_only=None, quantity=1):
    """Buy any affordable batch with space and a reachable shop."""
    if not hero.space or (hero.id, name) in mem.buy_failures:
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
        count = min(quantity, hero.space)
        if count > 0:
            options.append((-count, route[0], shop, route))
    if not options:
        return None
    negative_count, _, shop, route = min(options)
    return -negative_count, shop, route
