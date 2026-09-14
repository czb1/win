from collections import Counter
from .commands import command
from .model import ORES, WEAPONS, pos, distance


def walk(nav, ledger, hero, targets):
    route = nav.approach(hero, targets, ledger.reserved)
    if route is None:
        return False
    if route[1] is not None:
        return ledger.add(hero.id, command("move", route[1]))
    return True


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


def use_inventory(turn, nav, ledger, hero):
    if hero.health < (110 if hero.kind == "worker" else 100) and "Medicine" in hero.inventory:
        return ledger.add(hero.id, command("use", name="Medicine"))
    upgrades = []
    for building in turn.ours:
        prefix = "Weapon" if building.kind in WEAPONS else "Station" if building.kind == "station" else "Wall" if building.kind == "wall" else None
        if not prefix:
            continue
        name = f"{prefix}UpgradeVoucher{building.level}"
        if building.level < 3 and hero.inventory[name]:
            route = nav.approach(hero, building.cells, ledger.reserved)
            if route:
                upgrades.append((route[0], building.id, name, building, route))
        if building.kind == "wall" and hero.inventory["WallFixer"] and building.health < 500:
            if turn.adjacent(hero.pos, building.pos):
                return ledger.add(hero.id, command("use", building.pos, name="WallFixer"))
    if upgrades:
        _, _, name, building, route = min(upgrades)
        if route[1] is None:
            return ledger.add(hero.id, command("use", building.pos, name=name))
        return ledger.add(hero.id, command("move", route[1]))
    return False


def wall_keeps_access(turn, nav, ledger, target):
    destinations = [turn.station.cells] if turn.station else []
    vendors = {p for p, k in turn.zones.items() if k in ("vendor", "weaponShop")}
    if vendors:
        destinations.append(vendors)
    # Preserve existing reachable routes; existing isolated actors must not block every wall build.
    reachable = [(h, ds) for h in turn.heroes for ds in destinations
                 if nav.approach(h, ds, ledger.reserved)]
    turn.blocked.add(target)
    try:
        return all(nav.approach(h, ds, ledger.reserved) is not None for h, ds in reachable)
    finally:
        turn.blocked.discard(target)


def build(turn, cfg, mem, nav, ledger, hero, sites, name_for):
    options = []
    for index, target in enumerate(sites):
        if target in turn.blocked or target in ledger.reserved or target in mem.build_failures:
            continue
        route = nav.approach(hero, [target], ledger.reserved)
        if route:
            options.append((route[0], index, target, route))
    for _, index, target, route in sorted(options):
        name = name_for(index)
        if route[1] is not None:
            return ledger.add(hero.id, command("move", route[1]))
        if name == "wall" and not wall_keeps_access(turn, nav, ledger, target):
            continue
        if ledger.add(hero.id, command("build", target, name=name)):
            return True
    return False


def mine(turn, cfg, mem, nav, ledger, hero, want_stone=False):
    if not hero.space:
        return False
    options = []
    for p, kind in turn.zones.items():
        if kind not in ORES or p in mem.collect_failures:
            continue
        if any(o["name"] == kind and o["startDay"] <= turn.day <= o["endDay"] for o in mem.outages):
            continue
        if want_stone and kind != "stone":
            continue
        route = nav.approach(hero, [p], ledger.reserved)
        if route:
            value = 1 if want_stone else max(0, turn.prices.get(kind, 0))
            # Amortize walking over a batch; use observed market prices, no fixed copper preference.
            score = value / (1 + route[0] / max(1, cfg.sell_batch))
            options.append((-score, route[0], p, route))
    if not options:
        return False
    _, _, target, route = min(options)
    if route[1] is None:
        return ledger.add(hero.id, command("collect", target))
    return ledger.add(hero.id, command("move", route[1]))


def worker(turn, cfg, mem, nav, ledger, hero, tower_sites, wall_sites, builder):
    if use_inventory(turn, nav, ledger, hero):
        return
    if len(turn.weapons) + ledger.new_towers < len(cfg.loadout) and ledger.gold >= cfg.weapon_cost:
        if build(turn, cfg, mem, nav, ledger, hero, tower_sites, lambda i: cfg.loadout[i % len(cfg.loadout)]):
            return
    walls = [u for u in turn.ours if u.kind == "wall"]
    need_walls = builder and len(walls) < min(len(wall_sites), 4 + turn.day * 2)
    if need_walls and hero.inventory["stone"] >= cfg.wall_stones:
        if build(turn, cfg, mem, nav, ledger, hero, wall_sites, lambda _: "wall"):
            return
    if need_walls and hero.inventory["stone"] < cfg.stone_batch:
        if mine(turn, cfg, mem, nav, ledger, hero, want_stone=True):
            return
    reserve = max(0, len(cfg.loadout)-len(turn.weapons)-ledger.new_towers)*cfg.weapon_cost
    # Buy only a currently applicable voucher, without duplicating one already carried by the team.
    if not any("UpgradeVoucher" in item for h in turn.heroes for item in h.backpack):
        upgrade = next((f"WeaponUpgradeVoucher{w.level}" for w in sorted(turn.weapons, key=lambda w: (w.level, w.id)) if w.level < 3), None)
        if not upgrade and turn.station and turn.station.level < 3:
            upgrade = f"StationUpgradeVoucher{turn.station.level}"
        if upgrade in turn.shop and upgrade not in ledger.purchases and hero.space and ledger.gold - reserve >= turn.shop[upgrade]:
            if visit(turn, nav, ledger, hero, "weaponShop", command("buy", name=upgrade, num=1)):
                return
    counts = hero.inventory
    ore_count = sum(counts[k] for k in ORES)
    if ore_count and (ore_count >= cfg.sell_batch or not hero.space or ledger.gold < cfg.weapon_cost):
        sellable = [k for k in ORES if counts[k] and k in turn.prices and not (need_walls and k == "stone")]
        if sellable:
            kind = max(sellable, key=lambda k: counts[k] * turn.prices[k])
            if visit(turn, nav, ledger, hero, "vendor", command("sell", name=kind, num=counts[kind])):
                return
    mine(turn, cfg, mem, nav, ledger, hero)


def pioneer(turn, cfg, mem, nav, ledger, hero):
    if use_inventory(turn, nav, ledger, hero):
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
        return_estimate = min((distance(p, w.pos) for p in cells for w in turn.weapons), default=0)
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
