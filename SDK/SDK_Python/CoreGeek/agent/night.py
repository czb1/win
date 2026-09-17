"""Shared rocket operators and threat-limited night mining."""
from dataclasses import replace
from itertools import combinations

from .commands import command
from .model import ORES, distance, neighbours
from .mining import record_target, sale_inventory


def crew_pairs(turn, crew, shared=False):
    guns = {w.id: w for w in turn.weapons}
    heroes = {h.id: h for h in turn.heroes}
    pioneer, operator, miner = (heroes[crew[k]] for k in ("pioneer", "operator", "miner"))
    if shared:
        pairs = [(pioneer, guns[uid]) for uid in crew["pair"]]
        pairs.append((operator, guns[crew["solo"]]))
        posts = {pioneer.id: crew["post"], operator.id: crew["solo_post"]}
    else:
        anchor = next(uid for uid in crew["pair"] if uid != crew["miner_gun"])
        pairs = [(pioneer, guns[anchor]), (operator, guns[crew["solo"]]),
                 (miner, guns[crew["miner_gun"]])]
        posts = {pioneer.id: crew["post"], operator.id: crew["solo_post"],
                 miner.id: crew["miner_post"]}
    return pairs, posts


def shared_crew(turn, cfg, mem, nav, ledger, wall_sites, excluded=()):
    """Reserve a shared pioneer post and a miner post with an open exit.

    Daylight planning includes the pioneer even while it finishes a task; the
    caller excludes it from actual return commands until its task ends.
    """
    heroes = turn.heroes
    if (not cfg.night_economy_enabled or len(turn.weapons) != 3 or len(heroes) != 3
            or not turn.pioneer or any(w.kind != "rocket" for w in turn.weapons)
            or any(h.id in ledger.used or h.id in excluded or h.health <= (
                165 if h.kind == "worker" else 150) for h in heroes)):
        return None
    original = turn.blocked
    static = original - {h.pos for h in heroes}
    forbidden = static | set(wall_sites) | ledger.reserved
    guns = {w.id: w for w in turn.weapons}
    ids = {h.id for h in heroes}

    def valid(crew):
        if (not crew or {crew[k] for k in ("pioneer", "operator", "miner")} != ids
                or crew["pioneer"] != turn.pioneer.id
                or set(crew["pair"]) | {crew["solo"]} != set(guns)):
            return False
        pairs, posts = crew_pairs(turn, crew)
        if len(set(posts.values())) != 3 or any(p in forbidden for p in posts.values()):
            return False
        if not all(turn.adjacent(crew["post"], guns[uid].pos) for uid in crew["pair"]):
            return False
        return all(turn.adjacent(posts[h.id], w.pos)
                   and nav.search(h, {posts[h.id]}, ledger.reserved) is not None for h, w in pairs)

    try:
        turn.blocked = static
        if valid(mem.night_crew):
            return mem.night_crew
        mem.night_crew = None
        options = []
        pioneer = turn.pioneer
        for pair in combinations(turn.weapons, 2):
            shared_posts = (set(neighbours(pair[0].pos)) & set(neighbours(pair[1].pos))) - forbidden
            solo = next(w for w in turn.weapons if w not in pair)
            for post in sorted(shared_posts):
                pr = nav.search(pioneer, {post}, ledger.reserved)
                if pr is None:
                    continue
                for operator in turn.workers:
                    miner = next(h for h in turn.workers if h.id != operator.id)
                    solo_options = sorted((r[0], p) for p in set(neighbours(solo.pos)) - forbidden - {post}
                                          if (r := nav.search(operator, {p}, ledger.reserved)) is not None)
                    for _, solo_post in solo_options[:3]:
                        for miner_gun in pair:
                            for miner_post in sorted(set(neighbours(miner_gun.pos)) - forbidden - {post, solo_post}):
                                mr = nav.search(miner, {miner_post}, ledger.reserved)
                                sr = nav.search(operator, {solo_post}, ledger.reserved)
                                if mr is None or sr is None:
                                    continue
                                crew = {"pioneer": pioneer.id, "operator": operator.id, "miner": miner.id,
                                        "pair": tuple(w.id for w in pair), "solo": solo.id, "post": post,
                                        "solo_post": solo_post, "miner_gun": miner_gun.id,
                                        "miner_post": miner_post}
                                options.append((pr[0] + sr[0] + mr[0], pr[0], operator.id,
                                                post, solo_post, miner_post, miner_gun.id, crew))
        # Check the completed wall ring, not today's unfinished perimeter.
        exits = {p for p, k in turn.zones.items() if k in ORES}
        if not exits:
            exits = {p for p, k in turn.zones.items() if k in ("vendor", "weaponShop")}
        for *_, crew in sorted(options, key=lambda o: o[:-1]):
            turn.blocked = static | set(wall_sites) | {crew["post"], crew["solo_post"]}
            miner = next(h for h in heroes if h.id == crew["miner"])
            proxy = replace(miner, pos=crew["miner_post"])
            route = nav.approach(proxy, exits, ledger.reserved) if exits else None
            if route is not None:
                mem.night_crew = crew
                return crew
    finally:
        turn.blocked = original
    return None


def day_return(turn, mem, nav, ledger, crew, excluded=()):
    """Reach three separate posts that permit a later two-operator handoff."""
    pairs, posts = crew_pairs(turn, crew)
    if any(mem.return_targets.get(h.id, w.id) != w.id
           or mem.return_posts.get(h.id, posts[h.id]) != posts[h.id] for h, w in pairs):
        return None
    pairs = [(h, w) for h, w in pairs if h.id not in excluded]
    original = turn.blocked
    try:
        turn.blocked = original - {h.pos for h in turn.heroes}
        routes = {h.id: nav.search(h, {posts[h.id]}, ledger.reserved) for h, _ in pairs}
        if any(r is None for r in routes.values()):
            return None
        return pairs, {uid: (posts[uid], r[0]) for uid, r in routes.items()}
    finally:
        turn.blocked = original


def safe_trip(turn, cfg, mem, nav, hero, post, length=0, endpoint=None, reserved=()):
    """Budget outbound work plus the actual return before a threat can arrive."""
    if mem.night_alarm_until > turn.round:
        return False
    original = turn.blocked
    try:
        turn.blocked = original - {hero.pos}
        proxy = replace(hero, pos=endpoint or hero.pos)
        back = nav.search(proxy, {post}, reserved)
    finally:
        turn.blocked = original
    if back is None:
        return False
    horizon = length + back[0] + cfg.return_margin
    threats = [r for r in turn.robots if turn.threatens_us(r)]
    threats.extend(r for r in turn.enemies if r.kind not in ("wall", "station"))
    if threats and turn.station and turn.station.health < 750:
        return False
    if threats and any(w.kind == "wall" and w.health < 400 for w in turn.ours):
        return False
    if any(turn.base_distance(r.pos) <= max(cfg.task_danger_radius, horizon + r.attack_range)
           for r in threats):
        return False
    # Even an opponent-bound robot can block or endanger an outbound miner.
    return not any(distance(proxy.pos, r.pos) <= length + r.attack_range + 2
                   for r in (*turn.robots, *turn.enemies) if r.kind not in ("wall", "station"))


def night_plan(turn, cfg, mem, nav, ledger, wall_sites, excluded=(), defence_guns=None):
    crew = shared_crew(turn, cfg, mem, nav, ledger, wall_sites, excluded)
    mem.night_mode, mem.night_reason, mem.night_miner = "full", "no_shared_crew", None
    if crew is None:
        return None
    miner = next(h for h in turn.workers if h.id == crew["miner"])
    if not safe_trip(turn, cfg, mem, nav, miner, crew["miner_post"], reserved=ledger.reserved):
        if mem.night_alarm_until <= turn.round:
            mem.night_alarm_until = turn.round + cfg.return_margin
        mem.night_reason = "threat_recall"
        return crew_pairs(turn, crew)
    pairs, posts = crew_pairs(turn, crew, shared=True)
    if defence_guns is not None and crew["solo"] not in defence_guns:
        # Preserve main's clear-night release: a gun with no local defence
        # demand does not keep its worker stationary for the shared handoff.
        pairs = [(h, w) for h, w in pairs if h.id != crew["operator"]]
        posts.pop(crew["operator"])
    if any(h.pos != posts[h.id] for h, _ in pairs):
        mem.night_reason = "handoff_not_ready"
        return None
    mem.night_mode, mem.night_reason, mem.night_miner = "shared", "safe", miner.id
    return pairs, posts


def night_earn(turn, cfg, mem, nav, ledger, hero):
    """Collect and sell at night without daylight sale/return deadlines."""
    crew = mem.night_crew
    post = crew["miner_post"]
    counts = sale_inventory(turn, mem, hero)
    total = sum(counts.values())
    if not total:
        mem.sale_workers.discard(hero.id)
        mem.sale_targets.pop(hero.id, None)
    vendors = [p for p, k in turn.zones.items() if k == "vendor"]
    vendor_options = []
    for vendor in vendors:
        if mem.movement.avoids(hero.id, vendor):
            continue
        for cell in neighbours(vendor):
            route = nav.search(hero, {cell}, ledger.reserved)
            if route is not None and safe_trip(turn, cfg, mem, nav, hero, post,
                                               route[0] + len(counts), cell, ledger.reserved):
                vendor_options.append((vendor != mem.sale_targets.get(hero.id), route[0], vendor, cell, route))
    vendor_choice = min(vendor_options, default=None)

    def sell():
        if not counts or vendor_choice is None:
            return False
        _, _, vendor, _, route = vendor_choice
        kind = max(counts, key=lambda k: counts[k] * turn.prices[k])
        action = command("move", route[1]) if route[1] is not None else command("sell", name=kind, num=counts[kind])
        if not ledger.add(hero.id, action):
            return False
        mem.sale_workers.add(hero.id)
        mem.sale_targets[hero.id] = vendor
        mem.mine_targets.pop(hero.id, None)
        return True

    price = turn.shop.get("WeaponUpgradeVoucher1", 100)
    funds = ledger.gold < price <= ledger.gold + sum(counts[k] * turn.prices[k] for k in counts)
    if total and (not hero.space or total >= min(cfg.sell_batch, hero.capacity)
                  or funds or hero.id in mem.sale_workers):
        if sell():
            return True
    options = []
    if hero.space:
        for target, kind in turn.zones.items():
            if (kind not in ORES or turn.prices.get(kind, 0) <= 0 or target in mem.collect_failures
                    or mem.movement.avoids(hero.id, target)):
                continue
            cells = [(r[0], cell, r) for cell in neighbours(target)
                     if (r := nav.search(hero, {cell}, ledger.reserved)) is not None]
            for _, cell, route in sorted(cells):
                if not safe_trip(turn, cfg, mem, nav, hero, post, route[0] + 1, cell, ledger.reserved):
                    continue
                amount = min(hero.space, max(1, 10 - mem.mine_collected.get(target, 0)))
                score = turn.prices[kind] * amount / (amount + route[0])
                options.append((score, route[0], target, route))
                break
    if options:
        best = min(options, key=lambda o: (-o[0], o[1], o[2]))
        previous = next((o for o in options if o[2] == mem.mine_targets.get(hero.id)), None)
        _, _, target, route = previous if previous and previous[0] >= .65 * best[0] else best
        action = command("collect", target) if route[1] is None else command("move", route[1])
        if ledger.add(hero.id, action):
            changed = mem.mine_targets.get(hero.id) != target
            mem.mine_targets[hero.id] = target
            ledger.mine_claims[hero.id] = target
            record_target(turn, hero, target, route, "night_income", changed)
            return True
    if sell():
        return True
    # No safe productive trip remains; recover the third operator now.
    mem.night_alarm_until = turn.round + cfg.return_margin
    mem.night_mode, mem.night_reason, mem.night_miner = "full", "no_safe_income_route", None
    route = nav.search(hero, {post}, ledger.reserved)
    return bool(route and route[1] is not None and ledger.add(hero.id, command("move", route[1])))
