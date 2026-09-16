"""Defence assignment and weapon-specific targeting. No simulated enemy moves."""
from itertools import permutations, product
from .model import distance, dump, neighbours
from .commands import command
from .navigation import check_time


def line_cells(start, end):
    """Supercover grid traversal, conservatively includes cells touched at corners."""
    x, y = start
    dx, dy = end[0]-x, end[1]-y
    nx, ny = abs(dx), abs(dy)
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    ix = iy = 0
    out = []
    while ix < nx or iy < ny:
        a, b = (1+2*ix)*ny, (1+2*iy)*nx
        if a == b:
            out.extend([(x+sx, y), (x, y+sy)])
            x, y, ix, iy = x+sx, y+sy, ix+1, iy+1
        elif a < b:
            x, ix = x+sx, ix+1
        else:
            y, iy = y+sy, iy+1
        out.append((x, y))
    return out


def assignments(turn, nav, ledger, excluded=(), fixed=None):
    towers = turn.weapons[:3]
    heroes = [h for h in turn.heroes if h.id not in ledger.used and h.id not in excluded]
    routes = {(h.id, w.id): nav.approach(h, [w.pos], ledger.reserved) for h in heroes for w in towers}
    # Preserve main's cooperative return: a teammate can yield, while a wall
    # enclosing a gun cannot. Only the latter should release its operator.
    reachable = {key for key, route in routes.items() if route is not None}
    original = turn.blocked
    try:
        turn.blocked = original - {h.pos for h in heroes}
        for h in heroes:
            for w in towers:
                if (h.id, w.id) not in reachable and nav.approach(h, [w.pos], ledger.reserved) is not None:
                    reachable.add((h.id, w.id))
    finally:
        turn.blocked = original
    fixed = fixed or {}
    committed = []
    assigned = set()
    for h in heroes:
        w = next((w for w in towers if w.id == fixed.get(h.id) and w.id not in assigned), None)
        if w and (h.id, w.id) in reachable:
            committed.append((h, w))
            assigned.add(w.id)
    towers = [w for w in towers if w.id not in assigned]
    heroes = [h for h in heroes if h.id not in {actor.id for actor, _ in committed}]
    count = min(len(towers), len(heroes))
    if not count:
        return committed
    # When an operator heals/dies, tower IDs must not decide which gun stays idle.
    firepower = {}
    for w in towers:
        damage = {}
        if not turn.is_day and not w.cooldown:
            select_targets(turn, w, damage, nav.deadline)
        firepower[w.id] = sum(damage.get(r.id, 0) * threat(turn, r) for r in turn.robots)
    best, result = None, []
    for selected in permutations(towers, count):
        for crew in permutations(heroes, count):
            check_time(nav.deadline)
            # An unreachable gun must not reserve a worker for an impossible
            # return trip. Keep the best reachable partial crew instead.
            pairs = [(h, w) for h, w in zip(crew, selected) if (h.id, w.id) in reachable]
            travel = sum(routes[h.id, w.id][0] if routes[h.id, w.id] else 10000 for h, w in pairs)
            immediate = sum(firepower[w.id] for h, w in pairs
                            if routes[h.id, w.id] and routes[h.id, w.id][0] == 0)
            cost = (-immediate, -len(pairs), travel)
            if best is None or cost < best:
                best, result = cost, pairs
    return committed + sorted(result, key=lambda pair: (-firepower[pair[1].id], pair[1].id))


def operator_posts(turn, nav, pairs, wall_sites=(), fixed=None):
    """Plan distinct control cells before recalling the crew, including detours.

    Teammates can move during the return trip; buildings cannot. Actual movement
    still checks current occupancy. Never park on a future wall or let two guns
    count the same control cell as their independently nearest destination.
    """
    if not pairs:
        return {}
    fixed = fixed or {}
    original = turn.blocked
    turn.blocked = original - {h.pos for h, _ in pairs}
    options = []
    try:
        for h, w in pairs:
            candidates = []
            for p in sorted(set(neighbours(w.pos)) - set(wall_sites)):
                route = nav.search(h, {p})
                if route is not None:
                    candidates.append((p, route[0]))
            if not candidates:
                return {}
            options.append(candidates)
        best, result = None, {}
        for choice in product(*options):
            check_time(nav.deadline)
            if len({p for p, _ in choice}) != len(choice):
                continue
            changes = sum(h.id in fixed and fixed[h.id] != p
                          for (h, _), (p, _) in zip(pairs, choice))
            # Controller deaths preceded both late-wave collapses.  Prefer the
            # base-side control ring before shaving a step off the daytime walk.
            exposure = sum(turn.base_distance(p) for p, _ in choice)
            hostile_distance = sum(-min((distance(p, r.pos) for r in turn.robots
                                         if turn.threatens_us(r)), default=turn.width + turn.height)
                                   for p, _ in choice)
            cost = (changes, exposure, hostile_distance,
                    sum(length for _, length in choice), max(length for _, length in choice))
            if best is None or cost < best:
                best = cost
                result = {h.id: option for (h, _), option in zip(pairs, choice)}
        return result
    finally:
        turn.blocked = original


def return_plan(turn, nav, pairs, wall_sites, fixed_targets, fixed_posts):
    """Choose the crew and their distinct posts together, before committing."""
    best, result = None, (pairs, {})
    for crew in permutations([h for h, _ in pairs]):
        candidate = list(zip(crew, [w for _, w in pairs]))
        if any(h.id in fixed_targets and fixed_targets[h.id] != w.id for h, w in candidate):
            continue
        posts = operator_posts(turn, nav, candidate, wall_sites, fixed_posts)
        if not posts:
            continue
        cost = (sum(h.id in fixed_posts and fixed_posts[h.id] != posts[h.id][0] for h, _ in candidate),
                sum(length for _, length in posts.values()), max(length for _, length in posts.values()))
        if best is None or cost < best:
            best, result = cost, (candidate, posts)
    return result


def threat(turn, robot):
    if not turn.threatens_us(robot):
        return 0.0
    base_distance = turn.base_distance(robot.pos)
    score = 10.0 / (1 + base_distance)
    if robot.target_team == turn.team:
        score *= 2
    kind = {"bossRobot": 8, "largeRobot": 4, "middleRobot": 1}.get(robot.kind, .2)
    if base_distance <= 2 and robot.kind in ("bossRobot", "largeRobot"):
        kind *= 2
    return score + kind


def select_targets(turn, tower, damage, deadline):
    if tower.attack_range <= 0:
        return []
    check_time(deadline)
    targets = [r for r in turn.robots if turn.threatens_us(r)
               and distance(tower.pos, r.pos) <= tower.attack_range]
    protected = {r.id for r in turn.robots if not turn.threatens_us(r)}
    # Shot origin follows upstream demo (weapon centre); confirm against official engine.
    barriers = {p for p, kind in turn.zones.items() if kind != "land"}
    for u in (*turn.ours, *turn.enemies):
        if u.id != tower.id:
            barriers.update(u.cells)
    if tower.kind != "rocket":
        targets = [r for r in targets if not (set(line_cells(tower.pos, r.pos)) & barriers)]
    remaining = {r.id: max(0, r.health - damage.get(r.id, 0)) for r in turn.robots}
    weights = {r.id: threat(turn, r) for r in turn.robots}
    if tower.kind == "railgun":
        options = []
        for r in targets:
            check_time(deadline)
            path = set(line_cells(tower.pos, r.pos))
            energy, score, hits, collateral = max(0, tower.power), 0, {}, False
            for hit in sorted((b for b in turn.robots if b.pos in path), key=lambda b: distance(tower.pos, b.pos)):
                # All damage settles at turn end: earlier planned shots do NOT
                # reduce the energy absorbed by a currently living blocker.
                actual = min(energy, hit.health)
                collateral |= actual > 0 and hit.id in protected
                energy -= actual
                amount = min(actual, remaining[hit.id])
                hits[hit.id] = amount
                score += amount * threat(turn, hit)
            # Opponent-bound robots still absorb railgun energy physically.
            if not collateral:
                options.append((score, -r.id, r.pos, hits))
        if not options:
            return []
        score, _, target, hits = max(options, key=lambda x: x[:2])
        if score <= 0:
            return []
        for uid, amount in hits.items():
            damage[uid] = damage.get(uid, 0) + amount
        return [target]
    # Cache physical hits, then rescore marginal damage after each projectile.
    # A rocket's best landing cell can be empty, including at the range edge.
    shots = {}
    if tower.kind == "rocket":
        for robot in turn.robots:
            check_time(deadline)
            for point in [robot.pos, *neighbours(robot.pos)]:
                if turn.inside(point) and distance(tower.pos, point) <= tower.attack_range:
                    shots.setdefault(point, {})[robot.id] = 20 if point == robot.pos else 10
    else:
        for robot in targets:
            check_time(deadline)
            path = set(line_cells(tower.pos, robot.pos))
            hit = min((b for b in turn.robots if b.pos in path),
                      key=lambda b: distance(tower.pos, b.pos), default=robot)
            shots[robot.pos] = {hit.id: 10}
    # Filter physical hits, not just aim points: splash and first-hit blocking
    # can otherwise help the opponent even when aiming at our own wave.
    shots = {point: hits for point, hits in shots.items() if not protected.intersection(hits)}
    result = []
    for _ in range(tower.level):
        check_time(deadline)
        candidates = shots
        if tower.kind == "gatling":
            candidates = [q for q in shots if all(
                (q[0]-tower.pos[0])*(p[0]-tower.pos[0]) + (q[1]-tower.pos[1])*(p[1]-tower.pos[1]) >= 0 for p in result)]
        ranked = []
        for point in candidates:
            hits = {uid: min(remaining[uid], amount) for uid, amount in shots[point].items()}
            score = sum(amount * weights[uid] for uid, amount in hits.items())
            ranked.append((score, point, hits))
        if not ranked:
            return []
        score, target, hits = min(ranked, key=lambda x: (-x[0], x[1]))
        if score <= 0 and not result:
            return []
        result.append(target)
        for uid, amount in hits.items():
            remaining[uid] -= amount
            damage[uid] = damage.get(uid, 0) + amount
    return result


def defend(turn, nav, ledger, pairs=None, posts=None):
    damage = {}
    pairs = pairs if pairs is not None else assignments(turn, nav, ledger)
    for hero, tower in pairs:
        if hero.id in ledger.used:
            continue
        route = (nav.search(hero, {posts[hero.id]}, ledger.reserved) if posts and hero.id in posts
                 else nav.approach(hero, [tower.pos], ledger.reserved))
        if turn.is_day and posts and hero.id in posts:
            if yield_operator(turn, nav, ledger, hero, pairs, posts, route):
                continue
        if route and route[1] is not None:
            ledger.add(hero.id, command("move", route[1]))
        elif route and not turn.is_day and not tower.cooldown:
            planned = damage.copy()
            targets = select_targets(turn, tower, planned, nav.deadline)
            if targets:
                if ledger.add(tower.id, {"action": "attack", "controllerId": str(hero.id),
                                        "targetPos": [dump(p) for p in targets]}):
                    damage = planned


def yield_operator(turn, nav, ledger, hero, pairs, posts, route):
    """Occupy a narrow control cell only after the other operators pass it."""
    target = posts[hero.id]
    if hero.pos != target and (route is None or route[1] != target):
        return False
    waiting = [h for h, _ in pairs if h.id != hero.id and h.id in posts and h.pos != posts[h.id]]
    if not waiting:
        return False
    original = turn.blocked
    static = original - {h.pos for h, _ in pairs}
    try:
        turn.blocked = static
        reachable = [h for h in waiting if nav.search(h, {posts[h.id]}) is not None]
        turn.blocked = static | {target}
        obstructed = [h for h in reachable if nav.search(h, {posts[h.id]}) is None]
        if not obstructed:
            return False
        choices = []
        for p in [hero.pos, *neighbours(hero.pos)]:
            if (not turn.inside(p) or p == target or p in original and p != hero.pos
                    or p in ledger.reserved or nav.memory and p in nav.memory.blocked(hero.id)):
                continue
            turn.blocked = static | {p}
            lengths = [r[0] if (r := nav.search(h, {posts[h.id]})) else 10000 for h in obstructed]
            choices.append((sum(lengths), p != hero.pos, distance(p, target), p))
    finally:
        turn.blocked = original
    if choices:
        p = min(choices)[-1]
        if p != hero.pos:
            ledger.add(hero.id, command("move", p))
        return True
    return False


def emergency_items(turn, ledger):
    """Use carried emergency supplies before assigning operators; no speculative shopping."""
    area_used = False
    for hero in turn.heroes:
        if hero.health <= (70 if hero.kind == "worker" else 65) and hero.inventory["Medicine"]:
            ledger.add(hero.id, command("use", name="Medicine"))
            continue
        if area_used:
            continue
        threats = [r for r in turn.robots if turn.threatens_us(r) and turn.base_distance(r.pos) <= 4]
        threats = [r for r in threats if not any(not turn.threatens_us(b)
                   and distance(r.pos, b.pos) <= 1 for b in turn.robots)]
        if not threats:
            continue
        target = max(threats, key=lambda r: sum(min(b.health,100) for b in threats if distance(r.pos,b.pos)<=1))
        nearby = [r for r in threats if distance(r.pos,target.pos)<=1]
        if hero.inventory["Bomb"] and sum(min(r.health,100) for r in nearby) >= 100:
            area_used = ledger.add(hero.id, command("use", target.pos, name="Bomb"))
        elif hero.inventory["DizzyWeapon"] and len(nearby) >= 2:
            area_used = ledger.add(hero.id, command("use", target.pos, name="DizzyWeapon"))
