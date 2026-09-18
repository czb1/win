"""Defence assignment and weapon-specific targeting. No simulated enemy moves."""
from itertools import permutations, product
from .model import distance, dump, neighbours
from .commands import command
from .navigation import check_time


def crew_groups(pairs):
    """Economy adapters use pairs, but planning and movement are per person."""
    groups = {}
    for h, w in pairs:
        groups.setdefault(h.id, (h, []))[1].append(w)
    return groups


def post_damage(turn, point, horizon=1):
    """Conservative exposure, not a prediction of robot targeting or wall damage.

    Allow one cell of approach per round; unknown attack power is not zero risk.
    Include all nearby robots because incidental attacks need not target our base.
    """
    return sum((r.power if r.power > 0 else 40) * max(0, horizon - max(0, distance(point, r.pos)
               - (r.attack_range if r.attack_range > 0 else 3) - 1)) for r in turn.robots)


def retreat_endangered(turn, ledger, guarded):
    """A lethal control cell must not trap the released worker in economy code."""
    for hero in turn.workers:
        if (hero.id in ledger.used or hero.id not in guarded
                and not any(turn.adjacent(hero.pos, w.pos) for w in turn.weapons)):
            continue
        risk = post_damage(turn, hero.pos)
        if risk < hero.health:
            continue
        exits = [p for p in neighbours(hero.pos) if turn.inside(p)
                 and p not in turn.blocked and p not in ledger.reserved
                 and post_damage(turn, p) < risk]
        if exits:
            target = min(exits, key=lambda p: (post_damage(turn, p), turn.base_distance(p), p))
            ledger.add(hero.id, command("move", target))


def crew_plan(turn, nav, ledger, wall_sites=(), excluded=(), previous=None):
    """Jointly choose a reachable post and a tower group for each operator."""
    towers = turn.weapons[:3]
    heroes = [h for h in turn.heroes if h.id not in ledger.used and h.id not in excluded]
    if not towers or not heroes:
        return [], {}
    previous = previous or {}
    firepower = {}
    for w in towers:
        damage = {}
        if not turn.is_day and not w.cooldown:
            select_targets(turn, w, damage, nav.deadline)
        firepower[w.id] = sum(damage.get(r.id, 0) * threat(turn, r) for r in turn.robots)
    # In the compact main layout, the stone carrier must not be committed to
    # the opposite end of the front while a nearby gap still needs closing.
    xs = [p[0] for p in turn.station.cells] if turn.station else []
    front_x = (max(p[0] for p in wall_sites) if min(xs) + max(xs) < turn.width - 1
               else min(p[0] for p in wall_sites)) if wall_sites and xs else None
    gaps = [p for p in wall_sites if p[0] == front_x and p not in turn.blocked]
    builder = max((h for h in heroes if h.kind == "worker" and h.inventory["stone"]),
                  key=lambda h: (h.inventory["stone"], -h.id), default=None)
    original = turn.blocked
    options = {}
    try:
        turn.blocked = original - {h.pos for h in heroes}
        for h in heroes:
            for mask in range(1, 1 << len(towers)):
                group = [w for i, w in enumerate(towers) if mask & (1 << i)]
                cells = set.intersection(*(set(neighbours(w.pos)) for w in group)) - set(wall_sites)
                choices = []
                for p in sorted(cells):
                    route = nav.search(h, {p}, ledger.reserved)
                    if route is not None:
                        exposure = post_damage(turn, p)
                        if not turn.is_day and exposure >= h.health:
                            continue
                        # Broad bands avoid churn for insignificant risk changes.
                        danger = min(3, exposure * 4 // max(1, h.health)) if not turn.is_day else 0
                        choices.append((p, route[0], danger))
                options[h.id, mask] = choices
    finally:
        turn.blocked = original
    best, result = None, ([], {})
    # Three towers bound this search: enumerate owners, then common posts.
    for owners in product(range(-1, len(heroes)), repeat=len(towers)):
        check_time(nav.deadline)
        masks = {j: sum(1 << i for i, owner in enumerate(owners) if owner == j)
                 for j in set(owners) if j >= 0}
        if not masks:
            continue
        crew = [(heroes[j], mask) for j, mask in sorted(masks.items())]
        for choices in product(*(options[h.id, mask] for h, mask in crew)):
            check_time(nav.deadline)
            if len({p for p, _, _ in choices}) != len(choices):
                continue
            stationary = {h.id for (h, _), (p, _, _) in zip(crew, choices) if h.pos == p}
            immediate = sum(firepower[w.id] for i, w in enumerate(towers)
                            if owners[i] >= 0 and heroes[owners[i]].id in stationary)
            changes = sum(previous.get(h.id) != (p, tuple(w.id for i, w in enumerate(towers)
                                                       if mask & (1 << i)))
                          for (h, mask), (p, _, _) in zip(crew, choices))
            late = sum(length > 0 and length + ledger.cfg.return_margin > turn.day_left
                       for _, length, _ in choices) if turn.is_day else 0
            held = sum(h.pos == p and h.id in previous and previous[h.id][0] == p
                       for (h, _), (p, _, _) in zip(crew, choices)) if not turn.is_day else 0
            preparation = min((max(0, distance(h.pos, gap) - 1) + max(0, distance(p, gap) - 1)
                               for (h, _), (p, _, _) in zip(crew, choices)
                               if h == builder
                               for gap in gaps), default=0) if turn.is_day else 0
            cost = (max(d for _, _, d in choices), -immediate,
                    -sum(o >= 0 for o in owners),
                    (sum(length >= turn.day_left for _, length, _ in choices)
                     if turn.is_day and builder and gaps else late),
                    sum(h.health <= 165 for h, _ in crew),
                    sum(h.kind != "worker" for h, _ in crew), len(crew),
                    -held, preparation, late, changes, sum(d for _, _, d in choices),
                    sum(length for _, length, _ in choices),
                    sum(turn.base_distance(p) for p, _, _ in choices) if turn.station else 0)
            if best is None or cost < best:
                best = cost
                result = ([(heroes[o], w) for o, w in zip(owners, towers) if o >= 0],
                          {h.id: (p, length) for (h, _), (p, length, _) in zip(crew, choices)})
    return result


def relief_excluded(turn, mem):
    """Keep an outgoing worker out of the assignment until relief completes."""
    live = {h.id for h in turn.heroes}
    static = turn.blocked - {h.pos for h in turn.heroes}
    static.update(p for u in (*turn.ours, *turn.enemies)
                  if u.kind not in ("worker", "pioneer") for p in u.cells)
    mem.relief = {old: entry for old, entry in mem.relief.items()
                  if old in live and entry[0] in live and entry[1] not in static
                  and turn.round - mem.relief_started.get(old, turn.round) <= 20
                  and any(turn.adjacent(entry[1], w.pos) for w in turn.weapons)}
    mem.relief_started = {old: started for old, started in mem.relief_started.items() if old in mem.relief}
    return {old for old, (_, _, vacated) in mem.relief.items() if vacated}


def prepare_relief(turn, nav, ledger, pairs, posts, mem):
    """Stage a healthy replacement before the incumbent vacates a unique post.

    Occupied cells cannot be entered this turn. Movement during the exchange
    is a real fire gap, not simulated instantaneous handoff.
    """
    if turn.is_day or not ledger.cfg.shared_operators:
        return
    groups = crew_groups(pairs)
    claimed = set(groups)
    for old, (replacement, post, vacated) in list(mem.relief.items()):
        if (vacated and replacement in groups and groups[replacement][0].pos == post
                or not vacated and (old not in groups or posts.get(old) != post)):
            del mem.relief[old]
    for uid, (hero, guns) in groups.items():
        post = posts.get(uid)
        if (hero.kind != "worker" or hero.pos != post
                or hero.id in ledger.used):
            continue
        candidates = []
        for helper in turn.workers:
            if (helper.id in claimed or helper.id in ledger.used or helper.health <= 165
                    or helper.health <= hero.health
                    or helper.health <= post_damage(turn, post)):
                continue
            route = nav.approach(helper, {post}, ledger.reserved)
            if route is not None and post_damage(turn, route[1] or helper.pos) < helper.health:
                horizon = route[0] + 2 + ledger.cfg.return_margin
                loss = max(post_damage(turn, post, horizon),
                           mem.worker_damage.get(uid, 0) * horizon)
                if hero.health > 165 and hero.health > loss:
                    continue
                incumbent = mem.relief.get(uid, (None,))[0]
                candidates.append((helper.id != incumbent, route[0], helper.id, helper, route))
        if not candidates:
            mem.relief.pop(uid, None)
            mem.relief_started.pop(uid, None)
            continue
        _, _, _, helper, route = min(candidates, key=lambda x: x[:3])
        claimed.add(helper.id)
        mem.relief_started.setdefault(uid, turn.round)
        mem.relief[uid] = (helper.id, post, False)
        if route[1] is not None:
            ledger.add(helper.id, command("move", route[1]))
            continue
        if all(turn.adjacent(helper.pos, gun.pos) for gun in guns):
            # Already in range: transfer the actual attack plan this round.
            pairs[:] = [(helper if h.id == uid else h, w) for h, w in pairs]
            posts.pop(uid, None)
            posts[helper.id] = helper.pos
            mem.relief[uid] = (helper.id, helper.pos, True)
            ledger.used.add(uid)
            continue
        exits = [p for p in neighbours(post) if turn.inside(p) and p not in turn.blocked
                 and p not in ledger.reserved and p != helper.pos
                 and post_damage(turn, p) < hero.health]
        if exits:
            exit_cell = min(exits, key=lambda p: (
                post_damage(turn, p),
                turn.base_distance(p) if turn.station else 0, p))
            if ledger.add(uid, command("move", exit_cell)):
                mem.relief[uid] = (helper.id, post, True)
                ledger.used.add(helper.id)
        else:
            ledger.used.add(helper.id)


def yield_spare_worker(turn, nav, ledger, pairs, posts):
    """Clear a spare worker from a blocked operator route before normal jobs."""
    crew = crew_groups(pairs)
    original = turn.blocked
    for uid, (hero, _) in crew.items():
        if uid in ledger.used or uid not in posts or nav.search(hero, {posts[uid]}, ledger.reserved) is not None:
            continue
        for helper in turn.workers:
            if helper.id in crew or helper.id in ledger.used:
                continue
            try:
                turn.blocked = original - {helper.pos}
                if nav.search(hero, {posts[uid]}, ledger.reserved) is None:
                    continue
                exits = []
                current_danger = sum(distance(helper.pos, r.pos) <= r.attack_range + 2 for r in turn.robots)
                for p in neighbours(helper.pos):
                    if (not turn.inside(p) or p in original or p in ledger.reserved
                            or p in posts.values()):
                        continue
                    danger = sum(distance(p, r.pos) <= r.attack_range + 2 for r in turn.robots)
                    turn.blocked = (original - {helper.pos}) | {p}
                    route = nav.search(hero, {posts[uid]}, ledger.reserved)
                    if route is not None and danger <= current_danger:
                        exits.append((danger, route[0], p))
            finally:
                turn.blocked = original
            if exits and ledger.add(helper.id, command("move", min(exits)[2])):
                break


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
            cost = (changes, sum(length for _, length in choice), max(length for _, length in choice))
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
    score = 10.0 / (1 + turn.base_distance(robot.pos))
    if robot.target_team == turn.team:
        score *= 2
    if any(distance(p, robot.pos) <= robot.attack_range + 2
           for p in getattr(turn, "control_posts", ())):
        score += 3
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
    if ledger.cfg.shared_operators:
        if pairs is None:
            pairs, planned = crew_plan(turn, nav, ledger)
            posts = {uid: p for uid, (p, _) in planned.items()}
        if posts:
            yield_spare_worker(turn, nav, ledger, pairs, posts)
        turn.control_posts = {h.pos for h, w in pairs if turn.adjacent(h.pos, w.pos)
                              and h.id not in ledger.used}
        for hero, towers in crew_groups(pairs).values():
            if hero.id in ledger.used:
                continue
            goals = ({posts[hero.id]} if posts and hero.id in posts else
                     set.intersection(*(set(neighbours(w.pos)) for w in towers)))
            route = nav.search(hero, goals, ledger.reserved)
            if turn.is_day and posts and hero.id in posts:
                if yield_operator(turn, nav, ledger, hero, pairs, posts, route):
                    continue
            if route and route[1] is not None:
                ledger.add(hero.id, command("move", route[1]))
            elif route and not turn.is_day:
                for tower in sorted(towers, key=lambda w: (-w.level, w.id)):
                    if tower.cooldown:
                        continue
                    planned = damage.copy()
                    targets = select_targets(turn, tower, planned, nav.deadline)
                    if targets and ledger.add(tower.id, {"action": "attack", "controllerId": str(hero.id),
                                                       "targetPos": [dump(p) for p in targets]}):
                        damage = planned
                ledger.used.add(hero.id)
        return
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


def emergency_items(turn, ledger, guarded=()):
    """Use carried emergency supplies before assigning operators; no speculative shopping."""
    area_used = False
    for hero in turn.heroes:
        if hero.health <= (70 if hero.kind == "worker" else 65) and hero.inventory["Medicine"]:
            ledger.add(hero.id, command("use", name="Medicine"))
            continue
        if hero.id in guarded:
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
