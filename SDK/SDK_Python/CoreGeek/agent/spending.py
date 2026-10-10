"""Reserve construction/upgrades and spend the remaining gold on robots."""
from .economy_plan import front_sites, planned_weapons
from .model import SUMMON_ORDERS


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
    from .economy import supplies
    planned = planned_weapons(turn, cfg, mem, towers)
    construction = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    actors, purchases = set(), {}
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in excluded]
    # Opening surplus belongs to BOSS orders, not vouchers or night stock.
    for hero in (free if turn.day > 1 else ()):
        if not hero.space:
            continue
        options = [supplies(turn, cfg, mem, nav, ledger, hero, construction,
                            planned=planned, bulk=True)]
        # A healthy base can be planned alongside a wall batch, rather than
        # losing its money to a robot while the courier is delivering walls.
        if turn.station and turn.station.level < 3:
            options.append(supplies(turn, cfg, mem, nav, ledger, hero, construction,
                                    planned=planned, item_only=f'StationUpgradeVoucher{turn.station.level}'))
        for plan in options:
            if plan:
                name, _, quantity = plan
                purchases[name] = max(purchases.get(name, 0), turn.shop[name] * quantity)
                actors.add(hero.id)
    if construction or mem.wall_repair_worker is not None:
        actors.update(h.id for h in free)
    reserve = min(ledger.gold, construction + sum(purchases.values()))
    # Paid summon orders use a single action before taking a new ordinary
    # purchase job. Emergency recall/repair has already locked its actors.
    actors = {uid for uid in actors if not any(turn.units[uid].inventory[item] for item in SUMMON_ORDERS)}
    ledger.spending_plan = dict(gold_available=ledger.gold, reserved_gold=reserve,
                               surplus=max(0, ledger.gold-reserve), purchases=purchases,
                               medical_gold_reserved=purchases.get('Medicine', 0),
                               priority_actors=sorted(actors), day_left=turn.day_left)
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
