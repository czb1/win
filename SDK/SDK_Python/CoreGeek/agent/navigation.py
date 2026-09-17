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
    def __init__(self, turn, deadline, memory=None):
        self.turn, self.deadline = turn, deadline
        self.memory = memory
        self._trees = {}
        self._distances = {}

    def distances_to(self, targets, vacated=(), reserved=()):
        """One reverse BFS serves every mine; no BFS per deposit endpoint."""
        check_time(self.deadline)
        targets = frozenset(targets)
        blocked = frozenset((self.turn.blocked - set(vacated)) | set(reserved))
        key = targets, blocked
        if key not in self._distances:
            goals = {p for t in targets for p in neighbours(t)
                     if self.turn.inside(p) and p not in blocked and p not in targets}
            distances = dict.fromkeys(goals, 0)
            queue = deque(sorted(goals))
            visited = 0
            while queue:
                visited += 1
                if visited % 32 == 0:
                    check_time(self.deadline)
                p = queue.popleft()
                for q in neighbours(p):
                    if q not in distances and q not in blocked and self.turn.inside(q):
                        distances[q] = distances[p] + 1
                        queue.append(q)
            self._distances[key] = distances
        return self._distances[key]

    def search(self, hero, goals, reserved=()):
        check_time(self.deadline)
        avoided = self.memory.blocked(hero.id) if self.memory else set()
        blocked = frozenset((self.turn.blocked | set(reserved) | avoided) - {hero.pos})
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
    """Shared front cluster with legacy fallback; explicit layouts take precedence."""
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

    if cfg.shared_operators:
        # Forward cluster with a protected inner post. Try two offsets, then
        # retain the old geometry for existing guns or obstructed new sites.
        for middle in (1, 0):
            cluster = [world((3, v)) for v in (middle, middle-1, middle+1)][:len(cfg.loadout)]
            post = world((2, middle))
            outer = [world((4, v)) for v in (0, 1, -1, 2, -2, 3)]
            outer += [world((u, v)) for u in (3, 2, 1, 0) for v in (-2, 3)]
            occupied = {p for u in (*turn.ours, *turn.enemies)
                        if u.kind not in ("worker", "pioneer", "wall", "rocket", "gatling", "railgun")
                        for p in u.cells}
            occupied |= {p for p, kind in turn.zones.items() if kind != "land"}
            occupied |= {p for u in turn.enemies if u.kind not in ("worker", "pioneer") for p in u.cells}
            occupied |= {w.pos for w in turn.ours if w.kind == "wall" and w.pos not in outer}
            if (all(turn.inside(p) for p in cluster + outer + [post])
                    and not occupied.intersection(cluster + outer + [post])
                    and {w.pos for w in turn.weapons} <= set(cluster)):
                blocked = occupied | set(cluster) | set(outer)
                rear = world((-1, middle))
                def connected(start, goal, extra=()):
                    seen, queue = {start}, deque([start])
                    while queue:
                        p = queue.popleft()
                        if p == goal:
                            return True
                        for q in neighbours(p):
                            if turn.inside(q) and q not in blocked and q not in extra and q not in seen:
                                seen.add(q)
                                queue.append(q)
                    return False
                if turn.inside(rear) and connected(rear, post) and connected(rear, world((2, 1-middle)), {post}):
                    return cluster, outer

    # Group towers along the front. Spreading them around the 2x2 base cuts
    # the inner walking ring into pockets, forcing connectivity checks to
    # leave extra wall holes. Keep both flanks connected to the rear gate.
    # Leave (2, 1) as an operator/circulation cell; three consecutive towers
    # would leave their middle tower without a usable controller position.
    tower_order = [(2, 0), (2, -1), (2, 2)]
    towers = [world(p) for p in tower_order[:len(cfg.loadout)] if turn.inside(world(p))]
    # Close the six-cell front first, then add three cells on each flank.
    # Leave the rear open for mining, deliveries and operator circulation.
    order = [(3, v) for v in (0, 1, -1, 2, -2, 3)]
    order += [(u, v) for u in (2, 1, 0) for v in (-2, 3)]
    return towers, [world(p) for p in order if turn.inside(world(p))]



def wall_priority(turn, cfg, sites, index, hits=None):
    """Strategic side and existing breaches precede walking distance."""
    target = sites[index]
    if not turn.station:
        return (0, 0, index)
    xs = [p[0] for p in turn.station.cells]
    right = min(xs) + max(xs) < turn.width - 1
    front_x = max(p[0] for p in sites) if right else min(p[0] for p in sites)
    walls = {u.pos for u in turn.ours if u.kind == "wall"}
    x, y = target
    breach = ((x - 1, y) in walls and (x + 1, y) in walls or
              (x, y - 1) in walls and (x, y + 1) in walls)
    hits = hits or {}
    # Reclose a damaged flank before extending untouched wall segments.
    # Keep the urgent set narrow: rebuilding the observed destroyed cell comes
    # first, while neighbouring expansion keeps its normal front/breach order.
    urgent = turn.day >= 2 and x != front_x and hits.get(target, 0)
    return (int(not urgent), int(x != front_x), int(not breach), index)
