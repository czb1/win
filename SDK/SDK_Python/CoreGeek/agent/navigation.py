"""Eight-neighbour BFS; diagonal corner cutting is legal under the task rules."""
from collections import deque
from time import monotonic
from .model import neighbours, distance


class DeadlineExceeded(Exception):
    pass


def check_time(deadline):
    if monotonic() >= deadline:
        raise DeadlineExceeded


class Navigator:
    def __init__(self, turn, deadline):
        self.turn, self.deadline = turn, deadline

    def search(self, hero, goals, reserved=()):
        goals = set(goals)
        blocked = (self.turn.blocked | set(reserved)) - {hero.pos}
        goals = {g for g in goals if self.turn.inside(g) and g not in blocked}
        if not goals:
            return None
        if hero.pos in goals:
            return 0, None
        queue = deque([hero.pos])
        lengths, first = {hero.pos: 0}, {}
        while queue:
            current = queue.popleft()
            if len(lengths) % 32 == 0:
                check_time(self.deadline)
            for q in neighbours(current):
                if q in lengths or q in blocked or not self.turn.inside(q):
                    continue
                lengths[q] = lengths[current] + 1
                first[q] = q if current == hero.pos else first[current]
                if q in goals:
                    return lengths[q], first[q]
                queue.append(q)
        return None

    def approach(self, hero, targets, reserved=()):
        goals = {p for target in targets for p in neighbours(target)} - set(targets)
        return self.search(hero, goals, reserved)


def layout(turn, cfg):
    """Explicit whitelist preferred; demo rings are documented unverified defaults."""
    if cfg.layout_mode == "explicit":
        return [tuple(p) for p in cfg.weapon_cells if turn.inside(tuple(p))], [tuple(p) for p in cfg.wall_cells if turn.inside(tuple(p))]
    if not turn.station:
        return [], []
    cells = turn.station.cells
    xs, ys = [p[0] for p in cells], [p[1] for p in cells]
    def ring(n):
        return sorted((x, y) for x in range(min(xs)-n, max(xs)+n+1)
                      for y in range(min(ys)-n, max(ys)+n+1)
                      if turn.inside((x, y)) and min(distance((x, y), c) for c in cells) == n)
    # Face the map centre; reserve a two-cell opening for workers/operators.
    center = (turn.width // 2, turn.height // 2)
    towers = sorted(ring(1), key=lambda p: (distance(p, center), p))[:len(cfg.loadout)]
    walls = sorted(ring(2), key=lambda p: (distance(p, center), p))
    return towers, walls[2:]
