"""Persistent mining, safe night runs and one daily sale visit per worker."""
from .commands import command
from .model import ORES, distance, neighbours
from .economy_plan import via


def sale_inventory(turn, mem, hero):
    """Only surplus stone can be sold while the wall blueprint needs material."""
    counts = hero.inventory
    counts["stone"] = max(0, counts["stone"] - mem.stone_reserves.get(hero.id, 0))
    return {k: counts[k] for k in ORES if counts[k] and turn.prices.get(k, 0) > 0}


def mine(turn, cfg, mem, nav, ledger, hero, want_stone=False, local_only=False,
         deadline=None, stockpile=False):
    if not hero.space:
        return False
    vendors = [] if stockpile else [p for p, kind in turn.zones.items() if kind == "vendor"]
    home = [w.pos for w in turn.weapons] or (list(turn.station.cells) if turn.station else [])
    # Pick one reachable sale-and-return corridor. Using the worst home walk
    # over every vendor would let a distant enemy-side vendor stop local mining.
    home_dist = (nav.distances_to(home, {hero.pos}, ledger.reserved)
                 if home and not want_stone else {})
    wave_budget = min((turn.base_distance(r.pos) - r.attack_range - cfg.return_margin
                       for r in turn.robots if turn.threatens_us(r)), default=None)
    vendor_home = 0
    if vendors and home and not want_stone:
        corridors = []
        for vendor in vendors:
            home_legs = [home_dist[q] for q in neighbours(vendor) if q in home_dist]
            route = nav.approach(hero, [vendor], ledger.reserved)
            if route and home_legs:
                corridors.append((route[0] + max(home_legs), vendor, max(home_legs)))
        if not corridors:
            return False
        _, vendor, vendor_home = min(corridors)
        vendors = [vendor]
    sale_dist = nav.distances_to(vendors, {hero.pos}, ledger.reserved) if vendors and not want_stone else {}
    end = min(70 - cfg.return_margin - vendor_home,
              deadline if deadline is not None and turn.tick < deadline else 70)
    options = []
    for p, kind in turn.zones.items():
        if (kind not in ORES or p in mem.collect_failures or want_stone and kind != "stone"
                or mem.movement.avoids(hero.id, p)):
            continue
        if local_only and not turn.adjacent(hero.pos, p):
            continue
        value = 1 if want_stone else max(1 if stockpile else 0, turn.prices.get(kind, 0))
        if not value:
            continue
        route = nav.approach(hero, [p], ledger.reserved)
        if route is None:
            continue
        cells = [q for q in neighbours(p) if q in sale_dist]
        # A distant worker must not chase the last units another worker can
        # exhaust before arrival. Sharing a nearby deposit remains legal.
        remaining = max(1, 10 - mem.mine_collected.get(p, 0))
        others = [h for h in turn.workers if h.id != hero.id
                  and (ledger.mine_claims.get(h.id, mem.mine_targets.get(h.id)) == p)
                  and h.id not in mem.sale_workers]
        for other in others:
            arrival = max(0, distance(other.pos, p) - 1)
            remaining -= max(0, route[0] - arrival)
        if remaining <= 0:
            continue
        share = max(1, (remaining + len(others)) // (1 + len(others)))
        amount = min(hero.space, share)
        sale_walk = min((sale_dist[q] for q in cells), default=None)
        if stockpile and home and (turn.is_day or wave_budget is not None):
            home_walk = min((home_dist[q] for q in neighbours(p) if q in home_dist), default=None)
            if home_walk is None:
                continue
            budget = turn.day_left - cfg.return_margin if turn.is_day else wave_budget
            amount = min(amount, budget - route[0] - home_walk)
            if amount <= 0:
                continue
        if not want_stone and vendors:
            if sale_walk is None:
                continue
            sale_actions = len({k for k in ORES if hero.inventory[k] and turn.prices.get(k, 0) > 0} | {kind})
            time_left = end - turn.tick - route[0] - sale_walk - sale_actions
            amount = min(amount, time_left)
            if amount <= 0:
                continue
        # Amortize one sale over a whole backpack run, not over every ten-unit
        # deposit. Walking to the next mine is charged in full to its yield.
        batch = min(hero.capacity, cfg.sell_batch_max)
        score = value * amount / (amount + route[0] +
                                 (amount * (sale_walk + 1) / max(1, batch) if sale_walk is not None else 0))
        options.append((score, route[0], turn.base_distance(p), p, route))
    if not options:
        mem.mine_targets.pop(hero.id, None)
        return False
    best = min(options, key=lambda o: (-o[0], o[1], o[2], o[3]))
    previous = next((o for o in options if o[3] == mem.mine_targets.get(hero.id)), None)
    # Finish a productive deposit, including its last few units. Replan only
    # when it vanishes, becomes inaccessible/unprofitable, or cannot meet dusk.
    chosen = previous if previous and previous[0] >= .65 * best[0] else best
    _, _, _, target, route = chosen
    action = command("collect", target) if route[1] is None else command("move", route[1])
    if ledger.add(hero.id, action):
        mem.mine_targets[hero.id] = target
        ledger.mine_claims[hero.id] = target
        return True
    return False


def earn(turn, cfg, mem, nav, ledger, hero, deadline=None, force_sale=False):
    counts = sale_inventory(turn, mem, hero)
    ores = list(counts)
    total = sum(counts[k] for k in ores)
    if not total:
        mem.sale_workers.discard(hero.id)
        mem.sale_targets.pop(hero.id, None)
    if not turn.is_day or (hero.id in mem.sold_workers and hero.id not in mem.sale_workers):
        return mine(turn, cfg, mem, nav, ledger, hero, stockpile=True)
    vendors = [p for p, k in turn.zones.items() if k == "vendor"]
    options = [(r[0] != 0, p != mem.sale_targets.get(hero.id), r[0], p, r) for p in vendors
               if (hero.id not in mem.sold_workers or p == mem.sale_targets.get(hero.id))
               and not mem.movement.avoids(hero.id, p)
               and (r := nav.approach(hero, [p], ledger.reserved)) is not None]
    choice = min(options, default=None)
    vendor, route = (choice[3], choice[4]) if choice else (None, None)
    due = False
    if route:
        home = [w.pos for w in turn.weapons] or (list(turn.station.cells) if turn.station else [])
        trip = via(nav, hero, [[vendor], home], ledger.reserved) if home else route[0]
        due = (deadline is not None and turn.tick <= deadline
               and deadline - turn.tick <= route[0] + len(ores)
               or trip is not None and turn.day_left <= trip + len(ores) + cfg.return_margin)

    def sell():
        if not ores or route is None:
            return False
        kind = max(ores, key=lambda k: counts[k] * turn.prices[k])
        action = (command("sell", name=kind, num=counts[kind]) if route[1] is None
                  else command("move", route[1]))
        if ledger.add(hero.id, action):
            mem.sale_workers.add(hero.id)
            if route[1] is None:
                mem.sold_workers.add(hero.id)
            mem.sale_targets[hero.id] = vendor
            mem.mine_targets.pop(hero.id, None)
            return True
        return False

    if total and (not hero.space or due or force_sale or hero.id in mem.sale_workers):
        if sell():
            return True
    if mine(turn, cfg, mem, nav, ledger, hero, deadline=deadline):
        return True
    return sell()


def night_mine(turn, cfg, mem, nav, ledger, hero):
    """Stockpile until daylight; exclude danger cells from the entire route."""
    threats = [r for r in turn.robots if turn.threatens_us(r)]
    if any(turn.base_distance(r.pos) <= max(cfg.task_danger_radius, r.attack_range + 2)
           for r in threats):
        if turn.station:
            route = nav.approach(hero, turn.station.cells, ledger.reserved)
            if route and route[1] is not None:
                return ledger.add(hero.id, command("move", route[1]))
        return False
    original = turn.blocked
    try:
        danger = set()
        # Even an opponent-bound robot makes a poor mining neighbour. Do not
        # approach or route through its range merely because our base is safe.
        for robot in turn.robots:
            radius = robot.attack_range + 2
            danger.update((x, y)
                          for x in range(max(0, robot.pos[0] - radius), min(turn.width, robot.pos[0] + radius + 1))
                          for y in range(max(0, robot.pos[1] - radius), min(turn.height, robot.pos[1] + radius + 1)))
        turn.blocked = original | danger
        # A threatened unassigned worker retreats instead of starting a new run.
        if hero.pos in danger:
            turn.blocked = original
            if turn.station:
                route = nav.approach(hero, turn.station.cells, ledger.reserved)
                if route and route[1] is not None:
                    return ledger.add(hero.id, command("move", route[1]))
            return False
        return mine(turn, cfg, mem, nav, ledger, hero, stockpile=True)
    finally:
        turn.blocked = original
