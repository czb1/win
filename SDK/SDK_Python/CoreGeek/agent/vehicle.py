"""Pioneer transport: board for a trip, work in place, park outside home."""
from collections import deque
from dataclasses import dataclass

from .model import neighbours, DEFENCE_RETURN_TICK
from .projectiles import wall_gates


def driving_segment(start, target):
    """Only unambiguous two-step moves in one of the eight directions."""
    dx, dy = target[0] - start[0], target[1] - start[1]
    if max(abs(dx), abs(dy)) != 2 or dx not in (-2, 0, 2) or dy not in (-2, 0, 2):
        return None
    return (start[0] + dx // 2, start[1] + dy // 2), target


@dataclass
class VehicleMemory:
    journey: object = None
    walking: bool = False
    pending_exit: tuple | None = None
    day: int = 0
    actor: int | None = None
    driving: bool = False

    def observe(self, turn, mem):
        hero = turn.pioneer
        observed = bool(hero and turn.is_day and hero.is_driving and hero.pos in turn.vehicle_cells)
        if observed != self.driving:
            from .logging_system import emit_event
            emit_event('vehicle_state', dict(actor=hero.id if hero else self.actor,
                       reason='boarded' if observed else 'dismounted_or_unavailable',
                       is_driving=observed), 'economy')
        self.driving = observed
        if (not turn.is_day or not hero or not turn.vehicle_cells
                or self.day != turn.day or self.actor != hero.id):
            self.journey, self.walking, self.pending_exit = None, False, None
        elif hero.is_driving:
            self.walking = False
        elif self.pending_exit and hero.pos == self.pending_exit:
            self.walking = True
        # A completed non-move action ends the last leg. Multi-item shopping
        # still runs first at the destination; pickup only precedes a new trip.
        previous = mem.last_commands.get(str(hero.id), {}) if hero else {}
        if mem.last_round == turn.round - 1 and previous.get('action') not in (None, 'move'):
            self.journey, self.walking = None, False
        self.pending_exit = None
        self.day, self.actor = turn.day, hero.id if hero else None


@dataclass(frozen=True)
class VehicleRoute:
    rounds: int
    target: tuple
    mode: str
    parking: tuple | None = None
    walking_tail: int = 0


def home_area(turn, ledger):
    """Conservative footprint of the actual and planned wall perimeter.

    Include the base for incomplete rings; never drive through a missing wall
    or gate just because construction has not closed the perimeter yet.
    """
    walls = ledger.wall_cells | {u.pos for u in turn.ours if u.kind == 'wall'}
    points = walls | (turn.station.cells if turn.station else set())
    if not points:
        return set()
    xs, ys = zip(*points)
    return {(x, y) for x in range(min(xs), max(xs) + 1)
            for y in range(min(ys), max(ys) + 1)}


def _field(nav, goals, blocked):
    """Reverse pedestrian distances, also used to price the dismount leg."""
    from .navigation import check_time
    distances = dict.fromkeys(sorted(goals - blocked), 0)
    queue = deque(distances)
    while queue:
        if len(distances) % 32 == 0:
            check_time(nav.deadline)
        cell = queue.popleft()
        for target in neighbours(cell):
            if nav.turn.inside(target) and target not in blocked and target not in distances:
                distances[target] = distances[cell] + 1
                queue.append(target)
    return distances


def plan_route(nav, ledger, hero, goals, walking, reserved=(), *, prefer=True, max_rounds=None):
    """Price a complete board/drive/exit trip; never select the business goal."""
    from .navigation import check_time
    turn = nav.turn
    if (hero.kind != 'pioneer' or not turn.is_day or not walking or walking[1] is None
            or hero.id in ledger.used):
        return None
    driving = hero.is_driving and hero.pos in turn.vehicle_cells
    cars = turn.free_vehicle_cells
    if not driving and not cars:
        return None
    forbidden = set(reserved) | ledger.reserved
    if nav.memory:
        forbidden |= nav.memory.blocked(hero.id)
    blocked = (turn.blocked - cars - {hero.pos}) | forbidden
    goals = set(goals) - blocked
    if not goals:
        return None
    interior = home_area(turn, ledger)
    # Return trips must not fetch a remote car, but may keep an existing one.
    if not driving and goals <= interior:
        return None
    walls = ledger.wall_cells | {u.pos for u in turn.ours if u.kind == 'wall'}
    gates = wall_gates(turn, walls)
    no_parking = (interior | ledger.tower_cells | set(ledger.operator_posts.values())
                  | gates | {p for gate in gates for p in neighbours(gate)})
    parking = {}

    def safe_parking(cell):
        if cell not in parking:
            around = {p for p in neighbours(cell) if turn.inside(p) and p not in blocked}
            seen, pending = set(), [min(around)] if around else []
            while pending:
                point = pending.pop()
                if point not in seen:
                    seen.add(point)
                    pending.extend((set(neighbours(point)) & around) - seen)
            parking[cell] = cell not in no_parking and bool(around) and seen == around
        return parking[cell]

    limit = min(turn.day_left, max_rounds if max_rounds is not None else turn.day_left)
    if not prefer:
        limit = min(limit, walking[0] - 1)
    # A pedestrian suffix only follows descending distances. Therefore it
    # cannot walk back through the parked car or trigger another boarding.
    foot = _field(nav, goals, blocked)
    initial = hero.pos, int(driving)
    queue = deque([initial])
    routes = {initial: (0, None)}
    candidates = []
    visited = 0
    while queue:
        visited += 1
        if visited % 32 == 0:
            check_time(nav.deadline)
        cell, stage = queue.popleft()
        length, first = routes[cell, stage]
        if length > limit:
            continue
        if stage == 1 and safe_parking(cell):
            if cell in goals and first is not None:
                candidates.append((length, 0, cell, first))
            for target in neighbours(cell):
                tail = foot.get(target)
                if (tail is not None and tail < foot.get(cell, float('inf'))
                        and length + 1 + tail <= limit):
                    candidates.append((length + 1 + tail, 1 + tail, cell,
                                       first if first is not None else target))
        if length >= limit:
            continue
        if stage == 1:
            edges = []
            for middle in neighbours(cell):
                target = 2 * middle[0] - cell[0], 2 * middle[1] - cell[1]
                if (turn.inside(middle) and turn.inside(target)
                        and middle not in blocked | interior
                        and target not in blocked | no_parking):
                    edges.append((target, 1))
        else:
            edges = [(p, int(p in cars)) for p in neighbours(cell)
                     if turn.inside(p) and p not in blocked
                     and (p not in cars or p not in interior)]
        for state in edges:
            if state not in routes:
                routes[state] = length + 1, first if first is not None else state[0]
                queue.append(state)
    if not candidates:
        return None
    # Same arrival cost: keep driving longer, instead of dropping the car early.
    length, tail, park, first = min(candidates)
    mode = ('drive' if driving_segment(hero.pos, first) else
            'park_outside' if goals <= interior else 'dismount') if driving else 'pickup'
    return VehicleRoute(length, first, mode, park if tail else None, tail)


def faster_route(nav, ledger, hero, goals, walking, reserved=()):
    """Compatibility helper for urgent trips which must improve arrival time."""
    route = plan_route(nav, ledger, hero, goals, walking, reserved, prefer=False)
    return (route.rounds, route.target) if route else None


def take_idle_vehicle(turn, cfg, mem, nav, ledger, hero):
    """Pickup when genuinely idle; never leave a return post to chase a car."""
    from .commands import command
    from .logging_system import emit_event
    from .navigation import DeadlineExceeded
    if (not turn.is_day or turn.phase_task or hero.id in ledger.used
            or hero.id in mem.return_targets or turn.tick >= DEFENCE_RETURN_TICK
            or not nav.vehicle_detours or hero.health <= 150):
        return False
    if hero.is_driving and hero.pos in turn.vehicle_cells:
        ledger.used.add(hero.id)
        ledger.explain(hero.id, 'vehicle_onboard_standby')
        return True
    try:
        blocked = (turn.blocked - turn.free_vehicle_cells - {hero.pos}) | ledger.reserved
        if nav.memory:
            blocked |= nav.memory.blocked(hero.id)
        interior = home_area(turn, ledger)
        cars = turn.free_vehicle_cells - blocked - interior
        if not cars:
            return False
        distances = _field(nav, cars, blocked)
        steps = distances.get(hero.pos)
        if steps is None or steps == 0:
            return False
        # Reserve a conservative walk home from the car as well as pickup.
        home = set(ledger.operator_posts.values())
        if not home and turn.station:
            home = {p for c in turn.station.cells for p in neighbours(c)} - turn.station.cells
        back = _field(nav, home, blocked) if home else {}
        cost = max((back.get(car, turn.day_left) for car in cars), default=0) if home else 0
        if steps + cost + cfg.return_margin >= min(turn.day_left, DEFENCE_RETURN_TICK - turn.tick):
            return False
        target = next((p for p in neighbours(hero.pos) if distances.get(p) == steps - 1), None)
        if target is not None and ledger.add(hero.id, command('move', target)):
            ledger.explain(hero.id, 'vehicle_idle_pickup', route_steps=steps)
            emit_event('vehicle_travel', dict(actor=hero.id, reason='idle_pickup', target=target,
                       is_driving=False, accepted=True), 'economy')
            return True
    except DeadlineExceeded:
        return False
    return False
