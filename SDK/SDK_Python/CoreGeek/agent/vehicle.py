"""Optional pioneer travel acceleration after the strategy has chosen a goal.

Costs are actions, not geometric distance. A leg may board once, drive straight
two-cell steps, then walk after leaving the vehicle. Reboarding a parked car
would revisit the same state and cannot improve a static shortest route.
"""
from collections import deque

from .model import neighbours
from .projectiles import wall_gates


def driving_segment(start, target):
    """Only unambiguous two-step moves in one of the eight directions."""
    dx, dy = target[0] - start[0], target[1] - start[1]
    if max(abs(dx), abs(dy)) != 2 or dx not in (-2, 0, 2) or dy not in (-2, 0, 2):
        return None
    return (start[0] + dx // 2, start[1] + dy // 2), target


def faster_route(nav, ledger, hero, goals, walking, reserved=()):
    """Return a strictly faster route, or None; never select a business goal."""
    turn = nav.turn
    if (hero.kind != 'pioneer' or not turn.is_day or not walking or walking[1] is None
            or hero.id in ledger.used
            or (nav.memory and hero.id in getattr(nav.memory, 'driving_disabled', ()))):
        return None
    driving = hero.is_driving and hero.pos in turn.vehicle_cells
    cars = turn.free_vehicle_cells
    if not driving and not cars:
        return None
    # Keep all actor occupancy, movement feedback and caller safety masks.
    forbidden = set(reserved) | ledger.reserved
    if nav.memory:
        forbidden |= nav.memory.blocked(hero.id)
    blocked = ((turn.blocked - cars - {hero.pos}) | forbidden)
    goals = set(goals) - blocked
    if not goals:
        return None
    no_parking = (ledger.wall_cells | ledger.tower_cells | set(ledger.operator_posts.values())
                  | wall_gates(turn, ledger.wall_cells))
    parking = {}

    def safe_parking(cell):
        if cell not in parking:
            # A parked car remains an obstacle for other roles. Reject narrow
            # corridors using a sufficient, conservative local connectivity test.
            around = {p for p in neighbours(cell) if turn.inside(p) and p not in blocked}
            seen = set()
            pending = [min(around)] if around else []
            while pending:
                p = pending.pop()
                if p not in seen:
                    seen.add(p)
                    pending.extend((set(neighbours(p)) & around) - seen)
            parking[cell] = cell not in no_parking and seen == around
        return parking[cell]

    # Stage 0: walking to the observed car; 1: driving; 2: walking after exit.
    initial = hero.pos, 1 if driving else 0
    queue = deque([initial])
    routes = {initial: (0, None)}
    limit = min(walking[0] - 1, turn.day_left)
    visited = 0
    while queue:
        visited += 1
        if visited % 32 == 0:
            # Use the same deadline as ordinary navigation.
            from .navigation import check_time
            check_time(nav.deadline)
        cell, stage = queue.popleft()
        length, first = routes[cell, stage]
        if cell in goals and stage != 0 and (stage != 1 or safe_parking(cell)):
            return length, first
        if length >= limit:
            continue
        edges = []
        if stage == 1:
            for middle in neighbours(cell):
                target = 2 * middle[0] - cell[0], 2 * middle[1] - cell[1]
                if (turn.inside(middle) and middle not in blocked
                        and turn.inside(target) and target not in blocked and target not in no_parking):
                    edges.append((target, 1))
        if stage != 1 or safe_parking(cell):
            for target in neighbours(cell):
                if turn.inside(target) and target not in blocked:
                    following = (1 if stage == 0 and target in cars else
                                 2 if stage == 1 else stage)
                    edges.append((target, following))
        for state in edges:
            if state not in routes:
                routes[state] = length + 1, first if first is not None else state[0]
                queue.append(state)
    return None
