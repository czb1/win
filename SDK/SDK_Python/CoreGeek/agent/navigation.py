"""Eight-neighbour BFS; diagonal corner cutting is legal under the task rules."""
from collections import deque
from time import monotonic
from .model import neighbours


class DeadlineExceeded(Exception):
    pass


def check_time(deadline):
    if monotonic() >= deadline:
        raise DeadlineExceeded


class Navigator:
    def __init__(self, turn, deadline):
        self.turn, self.deadline = turn, deadline
        self._trees = {}

    def search(self, hero, goals, reserved=()):
        check_time(self.deadline)
        blocked = frozenset((self.turn.blocked | set(reserved)) - {hero.pos})
        goals = {g for g in goals if self.turn.inside(g) and g not in blocked}
        if not goals:
            return None
        if hero.pos in goals:
            return 0, None
        # Many mines/build sites share the same actor and occupancy snapshot.
        # Cache the entire BFS, including deterministic discovery order.
        key = hero.pos, blocked
        if key not in self._trees:
            queue = deque([hero.pos])
            routes = {hero.pos: (0, None, 0)}
            visited = 0
            while queue:
                current = queue.popleft()
                visited += 1
                if visited % 32 == 0:
                    check_time(self.deadline)
                length, first, _ = routes[current]
                for q in neighbours(current):
                    if q in routes or q in blocked or not self.turn.inside(q):
                        continue
                    routes[q] = (length + 1, q if current == hero.pos else first, len(routes))
                    queue.append(q)
            if len(self._trees) >= 16:
                self._trees.clear()
            self._trees[key] = routes
        routes = self._trees[key]
        route = min((routes[g] for g in goals if g in routes),
                    key=lambda r: (r[0], r[2]), default=None)
        return route[:2] if route else None

    def approach(self, hero, targets, reserved=()):
        targets = set(targets)
        goals = {p for target in targets for p in neighbours(target)} - targets
        return self.search(hero, goals, reserved)


def layout(turn, cfg):
    """One fixed rectangle anchored to the station's full 2x2 footprint.

    Default build regions remain demo-inferred. Gates and firing slots are
    deliberate omissions on that rectangle, never shifted replacement cells.
    """
    if cfg.layout_mode == "explicit":
        return (list(dict.fromkeys(tuple(p) for p in cfg.weapon_cells if turn.inside(tuple(p)))),
                list(dict.fromkeys(tuple(p) for p in cfg.wall_cells if turn.inside(tuple(p)))))
    if not turn.station:
        return [], []
    xs, ys = zip(*turn.station.cells)
    sx = 1 if min(xs) + max(xs) < turn.width - 1 else -1
    sy = 1 if min(ys) + max(ys) < turn.height - 1 else -1
    origin = (min(xs) if sx > 0 else max(xs), min(ys) if sy > 0 else max(ys))

    def world(p):
        return origin[0] + sx * p[0], origin[1] + sy * p[1]

    def local(p):
        return (p[0] - origin[0]) * sx, (p[1] - origin[1]) * sy

    # Spread weapons across both centre-facing sides; mirror on side switches.
    tower_order = [(2, 0), (0, 2), (2, 2)]
    towers = [world(p) for p in tower_order[:len(cfg.loadout)] if turn.inside(world(p))]
    perimeter = {(u, v) for u in range(-2, 4) for v in range(-2, 4)
                 if u in (-2, 3) or v in (-2, 3)}
    # Prefer a two-cell rear gate. At map edges use the first in-bounds side.
    gates = [((-2, 0), (-2, 1)), ((0, -2), (1, -2)),
             ((3, 0), (3, 1)), ((0, 3), (1, 3))]
    openings = set(next((g for g in gates if all(turn.inside(world(p)) for p in g)), ()))
    # Straight-fire weapons need a clear outward ray. Rockets can fire over walls.
    planned = [(world(p), name) for p, name in zip(tower_order, cfg.loadout)]
    existing = [(w.pos, w.kind) for w in turn.weapons]
    for point, kind in planned + existing:
        if kind not in ("gatling", "railgun"):
            continue
        u, v = local(point)
        du = -1 if u < 0 else 1 if u > 1 else 0
        dv = -1 if v < 0 else 1 if v > 1 else 0
        slot = u + du, v + dv
        if slot in perimeter:
            openings.add(slot)
    order = sorted(perimeter - openings, key=lambda p: (-max(p), -min(p), p))
    return towers, [world(p) for p in order if turn.inside(world(p))]
