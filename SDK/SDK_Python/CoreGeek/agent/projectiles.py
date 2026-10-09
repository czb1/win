"""Projectile geometry, separate from movement occupancy and robot collision.

Defaults preserve the inherited weapon-centre/character-blocking assumptions.
The official engine must settle those assumptions before configuring alternatives.
"""
from .model import CHARACTERS, WEAPONS, neighbours


def line_cells(start, end):
    """Supercover traversal, conservatively including touched corner cells."""
    x, y = start
    dx, dy = end[0] - x, end[1] - y
    nx, ny = abs(dx), abs(dy)
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    ix = iy = 0
    out = []
    while ix < nx or iy < ny:
        a, b = (1 + 2 * ix) * ny, (1 + 2 * iy) * nx
        if a == b:
            out.extend([(x + sx, y), (x, y + sy)])
            x, y, ix, iy = x + sx, y + sy, ix + 1, iy + 1
        elif a < b:
            x, ix = x + sx, ix + 1
        else:
            y, iy = y + sy, iy + 1
        out.append((x, y))
    return out


def projectile_origin(turn, tower, controller=None, actor_positions=None):
    if turn.projectile_origin == "controller":
        if controller is None:
            return None
        return (actor_positions or {}).get(controller.id, controller.pos)
    return tower.pos


def projectile_obstacles(turn, tower, actor_positions=None, static=False):
    """Map physical obstacles; robots are resolved by weapon hit logic instead."""
    positions = actor_positions or {}
    blocked = {}
    for point, kind in turn.zones.items():
        if kind != "land":
            blocked.setdefault(point, []).append({"type": "neutral", "kind": kind})
    for side, units in (("our", turn.ours), ("enemy", turn.enemies)):
        for unit in units:
            if unit.id == tower.id:
                continue
            character = unit.kind in CHARACTERS
            if character and (static or not turn.projectile_characters_block):
                continue
            category = "character" if character else "building"
            occupied = {positions[unit.id]} if character and unit.id in positions else unit.cells
            for point in occupied:
                blocked.setdefault(point, []).append({"type": category, "kind": unit.kind,
                                                     "unit_id": unit.id, "side": side})
    return blocked


def trajectory_blockers(origin, target, obstacles):
    return [{"pos": point, **blocker} for point in line_cells(origin, target)
            for blocker in obstacles.get(point, ())]


def wall_gates(turn, walls):
    """Recognize a single missing perimeter cell, never invent explicit gates."""
    walls = set(walls)
    if not walls:
        return set()
    xs, ys = zip(*walls)
    left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
    if right - left < 2 or top - bottom < 2:
        return set()
    perimeter = {(x, y) for x in range(left, right + 1) for y in range(bottom, top + 1)
                 if (x in (left, right) or y in (bottom, top)) and turn.inside((x, y))}
    gaps = perimeter - walls
    return gaps if len(gaps) == 1 and walls <= perimeter else set()


def mixed_layout(turn, cfg, sites, walls):
    """Give the gatling a gate-facing site within the inherited three-cell ring.

    Built weapons constrain the permutation. Explicit layouts never call this.
    A closed wall ring makes the gatling an entrance guard, not a front sniper.
    """
    if (len(sites) != 3 or cfg.loadout.count("rocket") != 2
            or cfg.loadout.count("gatling") != 1):
        return sites
    from itertools import permutations
    gates = wall_gates(turn, walls)
    static = set(walls) | turn.station.cells
    static.update(point for point, kind in turn.zones.items() if kind != "land")
    for unit in (*turn.ours, *turn.enemies):
        if unit.kind in (*WEAPONS, "station", "wall") and unit.pos not in sites:
            static.update(unit.cells)
    index = cfg.loadout.index("gatling")
    choices = []
    for order in permutations(sites):
        if any(unit.pos not in order or cfg.loadout[order.index(unit.pos)] != unit.kind
               for unit in turn.weapons):
            continue
        point = order[index]
        obstacles = static | (set(order) - {point})
        rockets = [p for i, p in enumerate(order) if cfg.loadout[i] == "rocket"]
        common = set(neighbours(rockets[0])) & set(neighbours(rockets[1]))
        posts = {p for p in neighbours(point) if turn.inside(p) and p not in obstacles | common}
        # A clear entrance must have an independent post that does not cut its ray.
        lanes = 0
        for gate in gates:
            for post in posts:
                if not set(line_cells(point, gate)) & (obstacles | {post}):
                    lanes += 1
                    break
        changes = sum(a != b for a, b in zip(order, sites))
        choices.append(((-lanes, -len(posts), changes, order), list(order)))
    selected = min(choices)[1] if choices else sites
    turn.weapon_layout = {"mode": "gate_guard", "sites": selected, "gates": sorted(gates),
                          "preserved_existing": bool(turn.weapons),
                          "scope": "inherited_ring_only; construction_area_unverified"}
    return selected
