"""Defence assignment and weapon-specific targeting. No simulated enemy moves."""
from itertools import permutations
from .model import distance, dump
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


def assignments(turn, nav, ledger):
    towers = turn.weapons[:3]
    heroes = [h for h in turn.heroes if h.id not in ledger.used]
    count = min(len(towers), len(heroes))
    if not count:
        return []
    routes = {(h.id, w.id): nav.approach(h, [w.pos], ledger.reserved) for h in heroes for w in towers}
    best, result = float("inf"), []
    for selected in permutations(towers, count):
        for crew in permutations(heroes, count):
            check_time(nav.deadline)
            pairs = list(zip(crew, selected))
            cost = sum(routes[h.id, w.id][0] if routes[h.id, w.id] else 10000 for h, w in pairs)
            if cost < best:
                best, result = cost, pairs
    return result


def threat(turn, robot):
    score = 10.0 / (1 + turn.base_distance(robot.pos))
    if robot.target_team == turn.team:
        score *= 2
    return score + {"bossRobot": 2, "largeRobot": 1, "middleRobot": .5}.get(robot.kind, .2)


def select_targets(turn, tower, damage, deadline):
    targets = [r for r in turn.robots if distance(tower.pos, r.pos) <= tower.attack_range]
    if not targets or tower.attack_range <= 0:
        return []
    # Shot origin follows upstream demo (weapon centre); confirm against official engine.
    barriers = {p for p, kind in turn.zones.items() if kind != "land"}
    for u in (*turn.ours, *turn.enemies):
        if u.id != tower.id:
            barriers.update(u.cells)
    if tower.kind != "rocket":
        targets = [r for r in targets if not (set(line_cells(tower.pos, r.pos)) & barriers)]
    if not targets:
        return []
    remaining = {r.id: max(0, r.health - damage.get(r.id, 0)) for r in turn.robots}
    if tower.kind == "railgun":
        options = []
        for r in targets:
            check_time(deadline)
            path = set(line_cells(tower.pos, r.pos))
            energy, score, hits = max(0, tower.power), 0, {}
            for hit in sorted((b for b in turn.robots if b.pos in path), key=lambda b: distance(tower.pos, b.pos)):
                amount = min(energy, remaining[hit.id])
                energy -= amount
                hits[hit.id] = amount
                score += amount * threat(turn, hit)
            options.append((score, -r.id, r.pos, hits))
        score, _, target, hits = max(options, key=lambda x: x[:2])
        if score <= 0:
            return []
        for uid, amount in hits.items():
            damage[uid] = damage.get(uid, 0) + amount
        return [target]
    result = []
    for _ in range(tower.level):
        check_time(deadline)
        candidates = targets
        if tower.kind == "gatling":
            candidates = [r for r in targets if all(
                (r.pos[0]-tower.pos[0])*(p[0]-tower.pos[0]) + (r.pos[1]-tower.pos[1])*(p[1]-tower.pos[1]) >= 0 for p in result)]
        ranked = []
        for r in candidates:
            if tower.kind == "rocket":
                hits = {b.id: min(remaining[b.id], 20 if b.pos == r.pos else 10)
                        for b in turn.robots if distance(b.pos, r.pos) <= 1}
            else:
                path = set(line_cells(tower.pos, r.pos))
                # Bullets stop at the nearest living robot, even if earlier planned shots kill it at end of turn.
                hit = min((b for b in turn.robots if b.pos in path), key=lambda b: distance(tower.pos, b.pos), default=r)
                hits = {hit.id: min(remaining[hit.id], 10)}
            score = sum(amount * threat(turn, turn_robot) for turn_robot in turn.robots
                        if (amount := hits.get(turn_robot.id, 0)))
            ranked.append((score, -r.id, r.pos, hits))
        if not ranked:
            return []
        _, _, target, hits = max(ranked, key=lambda x: x[:2])
        result.append(target)
        for uid, amount in hits.items():
            remaining[uid] -= amount
            damage[uid] = damage.get(uid, 0) + amount
    return result


def defend(turn, nav, ledger, pairs=None):
    damage = {}
    for hero, tower in (pairs if pairs is not None else assignments(turn, nav, ledger)):
        if hero.id in ledger.used:
            continue
        route = nav.approach(hero, [tower.pos], ledger.reserved)
        if route and route[1] is not None:
            ledger.add(hero.id, command("move", route[1]))
        elif route and not turn.is_day and not tower.cooldown:
            targets = select_targets(turn, tower, damage, nav.deadline)
            if targets:
                ledger.add(tower.id, {"action": "attack", "controllerId": str(hero.id),
                                      "targetPos": [dump(p) for p in targets]})


def emergency_items(turn, ledger):
    """Use carried emergency supplies before assigning operators; no speculative shopping."""
    area_used = False
    for hero in turn.heroes:
        if hero.health <= (70 if hero.kind == "worker" else 65) and hero.inventory["Medicine"]:
            ledger.add(hero.id, command("use", name="Medicine"))
            continue
        if area_used:
            continue
        threats = [r for r in turn.robots if turn.base_distance(r.pos) <= 4]
        if not threats:
            continue
        target = max(threats, key=lambda r: sum(min(b.health,100) for b in threats if distance(r.pos,b.pos)<=1))
        nearby = [r for r in threats if distance(r.pos,target.pos)<=1]
        if hero.inventory["Bomb"] and sum(min(r.health,100) for r in nearby) >= 100:
            area_used = ledger.add(hero.id, command("use", target.pos, name="Bomb"))
        elif hero.inventory["DizzyWeapon"] and len(nearby) >= 2:
            area_used = ledger.add(hero.id, command("use", target.pos, name="DizzyWeapon"))
