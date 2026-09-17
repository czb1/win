"""Defence assignment and weapon-specific targeting. No simulated enemy moves."""
from itertools import product
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


def control_cells(turn, towers, wall_sites=()):
    """Cells from which one stationary hero can control every given tower."""
    towers = list(towers)
    if not towers:
        return set()
    cells = set(neighbours(towers[0].pos))
    for tower in towers[1:]:
        cells.intersection_update(neighbours(tower.pos))
    return {p for p in cells if turn.inside(p) and p not in set(wall_sites)}


def assignments(turn, nav, ledger, excluded=(), fixed=None):
    """Cover up to three towers while using as few reachable operators as possible."""
    towers = turn.weapons[:3]
    heroes = [h for h in turn.heroes if h.id not in ledger.used and h.id not in excluded]
    if not towers or not heroes:
        return []
    fixed = fixed or {}
    firepower = {}
    for tower in towers:
        damage = {}
        if not turn.is_day and not tower.cooldown:
            select_targets(turn, tower, damage, nav.deadline)
        firepower[tower.id] = sum(damage.get(r.id, 0) * threat(turn, r) for r in turn.robots)

    # A teammate may move away during recall, so only buildings and terrain
    # determine whether a shared control post is structurally reachable.
    original = turn.blocked
    turn.blocked = original - {h.pos for h in heroes}
    best, result = None, []
    try:
        # -1 deliberately leaves an unreachable tower unassigned. All complete
        # mappings are considered, including one hero controlling all towers.
        for owners in product(range(-1, len(heroes)), repeat=len(towers)):
            check_time(nav.deadline)
            groups = {index: [] for index in range(len(heroes))}
            for tower, owner in zip(towers, owners):
                if owner >= 0:
                    groups[owner].append(tower)
            routes = {}
            valid = True
            for index, group in groups.items():
                if not group:
                    continue
                hero = heroes[index]
                route = nav.search(hero, control_cells(turn, group), ledger.reserved)
                if route is None:
                    valid = False
                    break
                routes[index] = route
            if not valid:
                continue
            # Preserve an established assignment as an anchor while allowing
            # the same operator to pick up additional towers.
            changes = sum(fixed.get(hero.id) is not None
                          and all(tower.id != fixed[hero.id] for tower in groups[index])
                          for index, hero in enumerate(heroes) if groups[index])
            covered = sum(bool(group) and len(group) for group in groups.values())
            operators = sum(bool(group) for group in groups.values())
            travel = sum(route[0] for route in routes.values())
            immediate = sum(firepower[tower.id] for index, group in groups.items()
                            if group and routes[index][0] == 0 for tower in group)
            cost = ((-covered, operators, changes, travel) if turn.is_day else
                    (-immediate, -covered, operators, changes, travel))
            if best is None or cost < best:
                best = cost
                result = [(heroes[index], tower) for index, group in groups.items() for tower in group]
    finally:
        turn.blocked = original
    return sorted(result, key=lambda pair: (-firepower[pair[1].id], pair[1].id))


def operator_posts(turn, nav, pairs, wall_sites=(), fixed=None):
    """Plan one distinct shared control cell per operator, including detours.

    Teammates can move during the return trip; buildings cannot. Actual movement
    still checks current occupancy. Never park on a future wall or let two guns
    count the same control cell as their independently nearest destination.
    """
    if not pairs:
        return {}
    fixed = fixed or {}
    original = turn.blocked
    groups = {}
    for hero, tower in pairs:
        groups.setdefault(hero.id, (hero, []))[1].append(tower)
    turn.blocked = original - {hero.pos for hero, _ in groups.values()}
    options = []
    try:
        for hero, towers in groups.values():
            candidates = []
            for p in sorted(control_cells(turn, towers, wall_sites)):
                route = nav.search(hero, {p})
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
            heroes = [hero for hero, _ in groups.values()]
            changes = sum(hero.id in fixed and fixed[hero.id] != p
                          for hero, (p, _) in zip(heroes, choice))
            cost = (changes, sum(length for _, length in choice), max(length for _, length in choice))
            if best is None or cost < best:
                best = cost
                result = {hero.id: option for hero, option in zip(heroes, choice)}
        return result
    finally:
        turn.blocked = original


def return_plan(turn, nav, pairs, wall_sites, fixed_targets, fixed_posts):
    """Validate shared posts before committing the assignment."""
    posts = operator_posts(turn, nav, pairs, wall_sites, fixed_posts)
    return (pairs, posts) if posts else ([], {})


def threat(turn, robot):
    if not turn.threatens_us(robot):
        return 0.0
    score = 10.0 / (1 + turn.base_distance(robot.pos))
    if robot.target_team == turn.team:
        score *= 2
    return score + {"bossRobot": 2, "largeRobot": 1, "middleRobot": .5}.get(robot.kind, .2)


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
    if posts is None:
        planned = operator_posts(turn, nav, pairs)
        posts = {uid: point for uid, (point, _) in planned.items()}
    for hero, tower in pairs:
        if hero.id in ledger.used and hero.id not in ledger.operators:
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
