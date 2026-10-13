"""Reach an open base firing position before coordinating unavoidable clearance."""
from collections import deque
from dataclasses import dataclass, field
from heapq import heappop, heappush
import re

from .commands import command
from .model import CHARACTERS, WEAPONS, distance, neighbours
from .navigation import check_time
from .projectiles import line_cells


FIRST_DAY_SUMMON_RADIUS = 2
ASSAULT_STALL_LIMIT = 3
OCCUPANCY_EVIDENCE_ROUNDS = 8
FAILED_BASE_LANE_ROUNDS = 4


_OCCUPIED_MOVE = re.compile(
    r"robot (\d+) wants move to \((-?\d+),\s*(-?\d+)\), "
    r"but target is occupied \(canMove=false\)")


def _occupied_move_errors(turn):
    failures = set()
    for error in turn.raw.get("errors") or []:
        if not isinstance(error, dict) or error.get("errorCode") != 4:
            continue
        match = _OCCUPIED_MOVE.fullmatch(str(error.get("description", "")).strip())
        if match:
            uid, x, y = map(int, match.groups())
            failures.add((uid, (x, y)))
    return failures


@dataclass(frozen=True)
class AssaultPlan:
    entry: tuple
    wall: object
    position: tuple


@dataclass
class RobotObjective:
    entry: tuple
    wall_id: int | None
    phase: str


@dataclass(frozen=True)
class AssaultRoute:
    issued: int
    target_id: int
    target_pos: tuple
    goal: tuple
    steps: int
    next_step: tuple


@dataclass
class RobotAssaultMemory:
    objectives: dict = field(default_factory=dict)
    buildings: dict = field(default_factory=dict)
    rejected_buildings: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    yield_waiters: dict = field(default_factory=dict)
    failed_shots: set = field(default_factory=set)
    shot_contexts: dict = field(default_factory=dict)
    accepted_shots: dict = field(default_factory=dict)
    failed_steps: dict = field(default_factory=dict)
    step_failures: dict = field(default_factory=dict)
    occupied_steps: dict = field(default_factory=dict)
    occupied_failures: dict = field(default_factory=dict)
    occupied_seen: dict = field(default_factory=dict)
    failed_base_lanes: dict = field(default_factory=dict)
    approach_best: dict = field(default_factory=dict)
    approach_stalls: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)
    recent_positions: dict = field(default_factory=dict)
    robot_ids: set = field(default_factory=set)
    entry_crossed: set = field(default_factory=set)
    day: int | None = None
    base_id: int | None = None
    base_pos: tuple | None = None
    focus_target_id: int | None = None
    clearance_entry: tuple | None = None

    def observe(self, turn, base):
        base_id = base.id if base else None
        base_pos = base.pos if base else None
        changed_base = self.base_id != base_id or self.base_pos != base_pos
        if changed_base:
            self.buildings.clear()
        if self.day != turn.day or changed_base:
            self.entry_crossed.clear()
            self.focus_target_id = None
            self.clearance_entry = None
            self.objectives.clear()
            self.pending.clear()
            self.yield_waiters.clear()
            self.failed_shots.clear()
            self.shot_contexts.clear()
            self.accepted_shots.clear()
            self.failed_steps.clear()
            self.step_failures.clear()
            self.occupied_steps.clear()
            self.occupied_failures.clear()
            self.occupied_seen.clear()
            self.failed_base_lanes.clear()
            self.approach_best.clear()
            self.approach_stalls.clear()
            self.routes.clear()
            self.recent_positions.clear()
            self.rejected_buildings.clear()
        self.day, self.base_id, self.base_pos = turn.day, base_id, base_pos
        alive = turn.summon_robot_ids
        self.robot_ids = alive.copy()
        self.entry_crossed.intersection_update(alive)
        self.objectives = {uid: goal for uid, goal in self.objectives.items() if uid in alive}
        self.failed_shots = {key for key in self.failed_shots if key[0] in alive}
        self.shot_contexts = {key: value for key, value in self.shot_contexts.items()
                              if key in self.failed_shots}
        self.accepted_shots = {key: value for key, value in self.accepted_shots.items()
                               if key[0] in alive}
        self.failed_steps = {key: until for key, until in self.failed_steps.items()
                             if key[0] in alive and until > turn.round}
        self.step_failures = {key: count for key, count in self.step_failures.items()
                              if key[0] in alive}
        self.occupied_steps = {point: until for point, until in self.occupied_steps.items()
                               if until > turn.round}
        self.occupied_seen = {point: seen for point, seen in self.occupied_seen.items()
                              if turn.round - seen <= OCCUPANCY_EVIDENCE_ROUNDS}
        self.failed_base_lanes = {point: until for point, until in self.failed_base_lanes.items()
                                  if until > turn.round}
        self.approach_best = {uid: best for uid, best in self.approach_best.items() if uid in alive}
        self.approach_stalls = {uid: count for uid, count in self.approach_stalls.items() if uid in alive}
        self.routes = {uid: route for uid, route in self.routes.items()
                       if uid in alive and turn.round - 1 <= route.issued <= turn.round}
        self.recent_positions = {uid: points for uid, points in self.recent_positions.items() if uid in alive}
        results = turn.raw.get("lastRoundRoleActionResults") or {}
        occupied_errors = _occupied_move_errors(turn)
        robots = {r.id: r for r in turn.summon_robots}
        visible_ids = {u.id for u in turn.enemies}
        # Gather all correlated occupied destinations before processing shots,
        # so evidence sharing does not depend on robot/command insertion order.
        for uid, (issued, origin, action, point, target_id, context) in self.pending.items():
            if (issued == turn.round - 1 and uid in alive and action == 'move'
                    and results.get(str(uid), results.get(uid)) is False
                    and robots[uid].pos != point and (uid, point) in occupied_errors):
                self.occupied_seen[point] = turn.round
        for uid, (issued, origin, action, point, target_id, context) in self.pending.items():
            if issued != turn.round - 1 or uid not in alive:
                continue
            result = results.get(str(uid), results.get(uid))
            if action == "attack" and result is False:
                key = uid, origin, target_id, point
                self.failed_shots.add(key)
                self.shot_contexts[key] = context
                self.accepted_shots.pop((uid, origin, target_id), None)
                if target_id == base_id:
                    self.approach_stalls[uid] = min(ASSAULT_STALL_LIMIT,
                        self.approach_stalls.get(uid, 0) + 1)
                    evidence = set(line_cells(origin, point)) & self.occupied_seen.keys()
                    if evidence:
                        # A collision alone says nothing about attack geometry.
                        # A failed base attack through that recently occupied
                        # cell additionally supports bounded route avoidance,
                        # without assigning an enemy identity or inventing HP.
                        self.failed_base_lanes.update((p, turn.round + FAILED_BASE_LANE_ROUNDS)
                                                      for p in evidence)
                        self.approach_stalls[uid] = ASSAULT_STALL_LIMIT
                if (target_id in self.buildings and target_id not in visible_ids
                        and distance(origin, point) <= 2
                        and any(key[2] == target_id and key[1] != origin for key in self.failed_shots)):
                    # Two failed stances, including one inside the range
                    # boundary, make this unseen target unreliable. Keep its
                    # cell as an obstacle and route around, without declaring
                    # it dead. A fresh sighting restores it as a target.
                    self.rejected_buildings.add(target_id)
            elif action == "attack" and result is True:
                # Legality is useful for aim selection, not proof of damage.
                self.accepted_shots[uid, origin, target_id] = point
                if target_id == base_id:
                    self.approach_stalls.pop(uid, None)
            elif action == "move":
                key = uid, point
                if base is not None:
                    best = self.approach_best.get(uid, base_distance(base, origin))
                    current = base_distance(base, robots[uid].pos)
                    route = self.routes.get(uid)
                    route_progress = (route is not None and route.issued == issued
                                      and route.next_step == point == robots[uid].pos
                                      and point not in self.recent_positions.get(uid, ()))
                    # A confirmed step on a firing route can go around a wall
                    # without reducing geometric base distance. Revisiting a
                    # recent cell is still backtracking, not fresh progress.
                    if current < best or route_progress:
                        self.approach_stalls.pop(uid, None)
                    else:
                        self.approach_stalls[uid] = min(ASSAULT_STALL_LIMIT,
                            self.approach_stalls.get(uid, 0) + 1)
                    self.approach_best[uid] = min(best, current)
                if robots[uid].pos != point:
                    # Repeated collisions need a longer avoidance window;
                    # cycling back every four turns wastes attack opportunities.
                    count = min(3, self.step_failures.get(key, 0) + 1)
                    self.step_failures[key] = count
                    self.failed_steps[key] = turn.round + 4 * 2 ** (count - 1)
                    if result is False and key in occupied_errors:
                        # The engine identified an occupied destination, even
                        # when its occupant is outside ordinary shared vision.
                        # Share movement avoidance, not an invented attack target
                        # or an assumed projectile blocker.
                        count = min(3, self.occupied_failures.get(point, 0) + 1)
                        self.occupied_failures[point] = count
                        self.occupied_steps[point] = turn.round + 4 * 2 ** (count - 1)
                else:
                    self.step_failures.pop(key, None)
                    self.failed_steps.pop(key, None)
                    self._clear_occupied_step(point)
                    # A confirmed cooperative yield changes the approach that
                    # stalled followers were waiting on. Rebase their progress
                    # only after observing the mover at its destination and its
                    # old cell empty; command legality alone proves neither.
                    if base is not None and origin not in turn.blocked:
                        for follower in self.yield_waiters.get(uid, ()):
                            if follower in robots:
                                self.approach_stalls.pop(follower, None)
                                self.approach_best[follower] = base_distance(base, robots[follower].pos)
        if base is not None:
            for robot in turn.summon_robots:
                current = base_distance(base, robot.pos)
                self.approach_best[robot.id] = min(self.approach_best.get(robot.id, current), current)
        for robot in turn.summon_robots:
            history = self.recent_positions.get(robot.id, ())
            if not history or history[-1] != robot.pos:
                self.recent_positions[robot.id] = (*history, robot.pos)[-8:]
            route = self.routes.get(robot.id)
            if route and route.issued < turn.round and robot.pos != route.next_step:
                self.routes.pop(robot.id, None)
        self.pending = {uid: value for uid, value in self.pending.items()
                        if uid in alive and value[0] == turn.round}
        self.yield_waiters = {uid: waiters & alive for uid, waiters in self.yield_waiters.items()
                             if uid in self.pending and waiters & alive}
        # Weapons do not move. Losing sight alone does not prove destruction;
        # a reported death or absence inside ordinary shared vision does.
        reported = {int(r["id"]) for r in turn.raw.get("teamEnemy", {}).get("roles", [])}
        seen = {u.id: u for u in turn.enemies if u.kind in WEAPONS}
        observers = [p for u in turn.ours for p in u.cells]
        self.buildings = {uid: u for uid, u in self.buildings.items()
                          if uid in seen or (uid not in reported and
                              not any(distance(u.pos, p) <= 4 for p in observers))}
        self.buildings.update(seen)
        self.rejected_buildings.intersection_update(self.buildings)
        self.rejected_buildings.difference_update(seen)
        turn.blocked.update(u.pos for u in self.buildings.values())
        for point in self.occupied_failures.keys() | self.occupied_seen.keys() | self.failed_base_lanes.keys():
            if point not in turn.blocked and any(distance(point, p) <= 4 for p in observers):
                self._clear_occupied_step(point)
        occupied = _occupants(turn, self)
        # A failed ray is evidence about that scene, not a permanent ban on
        # a firing position after a blocker moves or disappears.
        self.failed_shots = {key for key in self.failed_shots
                             if self.shot_contexts[key] == _shot_context(
                                 key[1], key[3], occupied, key[0], key[2])}
        self.shot_contexts = {key: value for key, value in self.shot_contexts.items()
                              if key in self.failed_shots}

    def rejected(self, uid, origin, target_id, point):
        return (uid, origin, target_id, point) in self.failed_shots

    def _clear_occupied_step(self, point):
        self.occupied_steps.pop(point, None)
        self.occupied_failures.pop(point, None)
        self.occupied_seen.pop(point, None)
        self.failed_base_lanes.pop(point, None)
        self.failed_steps = {key: until for key, until in self.failed_steps.items() if key[1] != point}
        self.step_failures = {key: count for key, count in self.step_failures.items() if key[1] != point}

    def avoided_steps(self, uid):
        return set(self.occupied_steps) | {p for robot_id, p in self.failed_steps if robot_id == uid}

    def remember(self, turn, robot, action, point, target_id=None, occupied=None):
        self.yield_waiters.pop(robot.id, None)
        self.routes.pop(robot.id, None)
        context = (_shot_context(robot.pos, point, occupied if occupied is not None else _occupants(turn, self),
                                 robot.id, target_id)
                   if action == "attack" else None)
        self.pending[robot.id] = turn.round, robot.pos, action, point, target_id, context


def enemy_base(turn):
    return next((u for u in turn.enemies if u.kind == "station"), None)


def base_distance(base, point):
    return min(distance(point, cell) for cell in base.cells)


def enemy_entries(turn, base):
    """Full wall ring, including absent walls rather than our gate blueprint.

    Like the existing layout, distance two is inferred from the demo; the
    rulebook's construction-zone diagram provides no numerical coordinates.
    """
    xs, ys = zip(*base.cells)
    return [(x, y)
            for x in range(max(0, min(xs) - 2), min(turn.width, max(xs) + 3))
            for y in range(max(0, min(ys) - 2), min(turn.height, max(ys) + 3))
            if base_distance(base, (x, y)) == 2]


def wall_entry_axis(base, entry):
    """Normal axis of a wall face; corners retain their horizontal face."""
    ys = [p[1] for p in base.cells]
    return 1 if entry[1] in (min(ys) - 2, max(ys) + 2) else 0


def first_day_wall_entries(turn, base):
    """Both horizontal faces plus the vertical face toward our base."""
    xs = [p[0] for p in base.cells]
    home_x = (min(p[0] for p in turn.station.cells) + max(p[0] for p in turn.station.cells)
              if turn.station else turn.width - 1)
    centre_x = min(xs) + max(xs)
    return [p for p in enemy_entries(turn, base)
            if wall_entry_axis(base, p) == 1 or
            (2 * p[0] - centre_x) * (home_x - centre_x) > 0]


def first_day_clear_sides(turn, base, known_enemies=(), *, axis=1):
    """Wall rows (or columns) with no observed weapon in the inner face.

    Unseen weapons remain unknown. Remembered stationary weapons count until
    ordinary shared sight or an explicit death disproves their occupancy.
    """
    if turn.day != 1:
        return set()
    coordinates = [p[axis] for p in base.cells]
    lower, upper = min(coordinates) - 2, max(coordinates) + 2
    armed = set()
    for weapon in (*known_enemies, *turn.enemies):
        if weapon.kind not in WEAPONS or base_distance(base, weapon.pos) != 1:
            continue
        if weapon.pos[axis] < min(coordinates):
            armed.add(lower)
        elif weapon.pos[axis] > max(coordinates):
            armed.add(upper)
    return {lower, upper} - armed


def weakness(wall):
    # Absolute observed HP breaks level ties; missing walls always win.
    return (1, wall.level, wall.health) if wall else (0, 0, 0)


def rear_approach(turn, base, point):
    """Behind the enemy base relative to our base, mirrored with the map.

    This is a geometry preference, not knowledge of unseen enemy weapons.
    Without our base, use the map centre as the incoming-wave direction.
    Doubled centres avoid rounding the 2x2 station footprint.
    """
    xs, ys = zip(*base.cells)
    centre = min(xs) + max(xs), min(ys) + max(ys)
    if turn.station:
        hx, hy = zip(*turn.station.cells)
        front = min(hx) + max(hx), min(hy) + max(hy)
    else:
        front = turn.width - 1, turn.height - 1
    return sum((2 * point[i] - centre[i]) * (front[i] - centre[i])
               for i in (0, 1)) < 0


def _entry_rank(turn, base, entry, wall, prefer_rear):
    rank = weakness(wall)
    return (int(not rear_approach(turn, base, entry)), *rank) if prefer_rear else rank


def legal_summon_position(turn, point):
    return turn.summon_position_legal(point)


def _entry_walls(turn, base):
    walls = {u.pos: u for u in turn.enemies if u.kind == "wall"}
    fixed = {p for u in (*turn.ours, *turn.enemies)
             if u.kind in (*WEAPONS, "station") for p in u.cells}
    fixed.update(u.pos for u in turn.ours if u.kind == "wall")
    fixed.update(p for p, kind in turn.zones.items()
                 if kind not in ("land", "stone", "iron", "copper"))
    return [(point, walls.get(point)) for point in enemy_entries(turn, base) if point not in fixed]


def _on_entry_side(base, point, side_row, axis=1):
    return (side_row is None or
            (point[axis] >= side_row if side_row > base.pos[axis] else point[axis] <= side_row))


def _spawn_at_entry(turn, ledger, base, entry, wall, deadline, rear_only=False, side_row=None):
    blocked = turn.blocked | ledger.reserved
    pending = ledger.summon_pending_positions
    if wall:
        starts = {p for p in neighbours(entry)
                  if turn.inside(p) and p not in blocked and base_distance(base, p) >= 3}
        if not starts:
            # Adjacent dynamic occupants should not suppress an otherwise
            # usable weak wall: a free square in robot range can still breach.
            starts = {p for p in _attack_goals(turn, wall.cells, 3)
                      if p not in blocked and base_distance(base, p) >= 3}
    elif entry not in blocked:
        starts = {entry}
    else:
        # A visible hero temporarily occupying an open gate does not turn it
        # into a wall. Prefer a free adjacent placement, avoiding spawn shift.
        starts = {p for p in neighbours(entry)
                  if turn.inside(p) and p not in blocked and base_distance(base, p) >= 2}
    starts = {p for p in starts if _on_entry_side(base, p, side_row)}
    if not starts:
        return None
    queue = deque((point, 0) for point in sorted(starts))
    seen = set(starts)
    best = None
    visited = 0
    while queue:
        point, length = queue.popleft()
        if best is not None and length > best[0]:
            break
        if deadline is not None and visited % 32 == 0:
            check_time(deadline)
        visited += 1
        if (legal_summon_position(turn, point) and point not in pending
                and (not rear_only or rear_approach(turn, base, point))):
            candidate = length, base_distance(base, point), distance(point, entry), point
            if best is None or candidate < best:
                best = candidate
            continue
        for step in neighbours(point):
            if (step not in seen and turn.inside(step) and step not in blocked
                    and _on_entry_side(base, step, side_row)):
                seen.add(step)
                queue.append((step, length + 1))
    return best


def weakest_approach(turn, ledger, deadline=None, *, prefer_rear=False, preferred_sides=()):
    base = enemy_base(turn)
    if base is None:
        return None
    entries = sorted(((int(point[1] not in preferred_sides),
                       *_entry_rank(turn, base, point, wall, prefer_rear)), point, wall)
                     for point, wall in _entry_walls(turn, base))
    chosen, chosen_key = None, None
    for rank, entry, wall in entries:
        if chosen_key is not None and rank > chosen_key[:len(rank)]:
            break
        preferred = entry[1] in preferred_sides
        spawn = _spawn_at_entry(turn, ledger, base, entry, wall, deadline,
                                rear_only=prefer_rear and rear_approach(turn, base, entry),
                                side_row=entry[1] if preferred else None)
        if spawn is None:
            continue
        key = (*rank, *spawn[:3], entry, spawn[3])
        if chosen_key is None or key < chosen_key:
            chosen_key = key
            chosen = AssaultPlan(entry, wall, spawn[3])
    return chosen


def open_assault_entry(turn, base, entry):
    """An unbuilt land wall cell stays open despite temporary occupants."""
    if entry not in enemy_entries(turn, base):
        return None
    if any(entry in u.cells for u in (*turn.ours, *turn.enemies)
           if u.kind in (*WEAPONS, 'wall', 'station')) or turn.zones.get(entry, 'land') != 'land':
        return None
    return entry


def in_summon_cluster(ledger, point):
    # Rear robots do not enter through the scout. Keep its existing wall gap
    # available as a fallback instead of dragging it to the new spawn cluster.
    if ledger.robot_summon_mode == 'rear_direct':
        return True
    anchor = ledger.robot_summon_anchor
    return anchor is None or distance(anchor, point) <= FIRST_DAY_SUMMON_RADIUS


def _summon_in_cluster(turn, ledger, base, entry, deadline):
    """Keep later day-one orders near the first still-reserved placement."""
    if deadline is not None:
        check_time(deadline)
    anchor = ledger.robot_summon_anchor
    if entry is not None and distance(entry, anchor) > FIRST_DAY_SUMMON_RADIUS + 1:
        entry = None
    axis = wall_entry_axis(base, entry) if entry else 1
    side_row = entry[axis] if entry else None
    blocked = turn.blocked | ledger.reserved | ledger.summon_pending_positions
    options = [(x, y)
               for x in range(anchor[0] - FIRST_DAY_SUMMON_RADIUS, anchor[0] + FIRST_DAY_SUMMON_RADIUS + 1)
               for y in range(anchor[1] - FIRST_DAY_SUMMON_RADIUS, anchor[1] + FIRST_DAY_SUMMON_RADIUS + 1)
               if (x, y) not in blocked and turn.summon_position_legal((x, y))
               and _on_entry_side(base, (x, y), side_row, axis)]
    return min(options, key=lambda p: (distance(p, entry or anchor),
               abs(p[1 - axis] - entry[1 - axis]) if entry else 0,
               distance(p, anchor), p)) if options else None


def _summon_at_gap(turn, ledger, base, entry, deadline):
    # The held wall cell is in the no-summon construction ring. Search outward
    # from it along the face normal, keeping every reserved spawn unique.
    blocked = turn.blocked | ledger.reserved
    axis = wall_entry_axis(base, entry)
    side_row = entry[axis]
    starts = {p for p in neighbours(entry) if turn.inside(p) and p not in blocked
              and base_distance(base, p) >= 3 and _on_entry_side(base, p, side_row, axis)}
    queue = deque((p, 0) for p in sorted(starts))
    seen, options = set(starts), []
    while queue:
        point, steps = queue.popleft()
        if options and steps > options[0][0]:
            break
        if deadline is not None:
            check_time(deadline)
        if (turn.summon_position_legal(point) and point not in ledger.summon_pending_positions):
            options.append((steps, distance(point, entry),
                            abs(point[1 - axis] - entry[1 - axis]), point))
            continue
        for step in neighbours(point):
            if (step not in seen and turn.inside(step) and step not in blocked
                    and base_distance(base, step) >= 3 and _on_entry_side(base, step, side_row, axis)):
                seen.add(step)
                queue.append((step, steps + 1))
    return min(options)[-1] if options else None


def scouted_rear_summon_position(turn, ledger, deadline=None):
    """Use an observed turret's open rear to shoot the base from legal land.

    Walls are globally reported. Seen/remembered weapons occupy movement and
    spawn cells, but robot attacks can pass through them.
    The rear is relative to the two bases, not a hard-coded map direction.
    """
    base = enemy_base(turn)
    if turn.day != 1 or not turn.is_day or base is None:
        return None
    weapons = {u.id: u for u in (*ledger.robot_known_enemies, *turn.enemies)
               if u.kind in WEAPONS and base_distance(base, u.pos) == 1}
    if not weapons:
        return None
    blocked = (turn.blocked | ledger.reserved | ledger.summon_pending_positions
               | {p for u in weapons.values() for p in u.cells})
    weapon_cells = {p for u in (*turn.ours, *turn.enemies, *ledger.robot_known_enemies)
                    if u.kind in WEAPONS for p in u.cells}
    obstacles = ((turn.blocked - base.cells - weapon_cells)
                 | ledger.reserved | ledger.summon_pending_positions)
    candidates = []
    # Every summon type has range three. Check the same conservative rays as
    # night attacks, including characters/NPCs, while shooting through towers.
    for point in sorted(_attack_goals(turn, base.cells, 3)):
        if deadline is not None:
            check_time(deadline)
        if (point in blocked or not rear_approach(turn, base, point)
                or not turn.summon_position_legal(point)):
            continue
        if any(distance(point, cell) <= 3
               and not obstacles.intersection(line_cells(point, cell)) for cell in base.cells):
            candidates.append(point)
    anchor = ledger.robot_summon_anchor
    clear_sides = first_day_clear_sides(turn, base, weapons.values())
    # Keep a rear group together when it still has an open lane. A newly
    # scouted rear or a newly closed lane may require a different deployment.
    return min(candidates, key=lambda p: (
        bool(anchor and distance(p, anchor) > FIRST_DAY_SUMMON_RADIUS),
        distance(p, anchor) if anchor else 0,
        not any(_on_entry_side(base, p, row) for row in clear_sides),
        -min(distance(p, w.pos) for w in weapons.values()), p)) if candidates else None


def choose_summon_position(turn, ledger, deadline=None):
    base = enemy_base(turn)
    if base is None:
        return None
    entry = open_assault_entry(turn, base, ledger.robot_assault_entry)
    rear = scouted_rear_summon_position(turn, ledger, deadline)
    if rear is not None:
        ledger.robot_summon_mode = 'rear_direct'
        return rear
    # If the rear is now walled/blocked, release its grouping preference and
    # return to the worker's gap. Already submitted positions stay reserved.
    if (turn.day == 1 and ledger.robot_summon_anchor is not None
            and ledger.robot_summon_mode != 'rear_direct'):
        return _summon_in_cluster(turn, ledger, base, entry, deadline)
    if entry is not None:
        position = _summon_at_gap(turn, ledger, base, entry, deadline)
        if position is not None:
            ledger.robot_summon_mode = 'scout_gap'
            return position
    clear_sides = first_day_clear_sides(turn, base, ledger.robot_known_enemies) if base else set()
    # Without a direct rear shot or a usable gap, retain the old weak-entry
    # fallback, preferring an unarmed horizontal face when one is known.
    plan = weakest_approach(turn, ledger, deadline, prefer_rear=True,
                            preferred_sides=clear_sides if len(clear_sides) == 1 else ())
    ledger.robot_summon_mode = 'weakest_entry'
    return plan.position if plan else None


def _attack_goals(turn, target_cells, reach):
    return {point for cell in target_cells
            for x in range(max(0, cell[0] - reach), min(turn.width, cell[0] + reach + 1))
            for y in range(max(0, cell[1] - reach), min(turn.height, cell[1] + reach + 1))
            if (point := (x, y)) not in target_cells}


def _new_objective(turn, base, robot, nav, ledger, avoided=()):
    # A rear deployment must not immediately walk around to a weaker front
    # wall. Preserve legacy weak-entry selection for a robot shifted in front.
    prefer_rear = rear_approach(turn, base, robot.pos)
    entries = sorted((_entry_rank(turn, base, point, wall, prefer_rear),
                      distance(robot.pos, point), point, wall)
                     for point, wall in _entry_walls(turn, base))
    for _, _, entry, wall in entries:
        check_time(nav.deadline)
        # With no wall to breach, the next objective is immediately the base.
        if wall is None:
            return RobotObjective(entry, None, "base")
        if distance(robot.pos, wall.pos) <= robot.attack_range:
            return RobotObjective(entry, wall.id, "breach")
        route = nav.search(robot, _attack_goals(turn, wall.cells, robot.attack_range),
                           ledger.reserved | set(avoided))
        if route is not None:
            return RobotObjective(entry, wall.id, "breach")
    return None


def _target_points(origin, target):
    return sorted(target.cells, key=lambda point: (distance(origin, point), point != target.pos,
                  (origin[0] - point[0]) ** 2 + (origin[1] - point[1]) ** 2, point))


def _target_point(origin, target):
    return _target_points(origin, target)[0]


def _occupants(turn, memory):
    enemies = {u.id: u for u in memory.buildings.values()}
    enemies.update((u.id, u) for u in turn.enemies)
    occupied = {p: (u, u.id not in memory.rejected_buildings) for u in enemies.values() for p in u.cells}
    # Public robots have unknown ownership and are never attack targets.
    occupied.update((p, (u, False)) for u in (*turn.ours, *turn.robots, *turn.summon_robots)
                    for p in u.cells)
    for p, kind in turn.zones.items():
        if kind != "land":
            occupied.setdefault(p, (None, False))
    return occupied


def _ray_hits(origin, point, occupied, actor_id):
    """Robot attacks pass intermediate turrets; an aimed turret is still a target.

    This is independent of weapon projectiles and of movement occupancy.
    """
    for cell in line_cells(origin, point):
        hit = occupied.get(cell)
        if hit is not None and (hit[0] is None or hit[0].id != actor_id):
            if hit[0] is not None and hit[0].kind in WEAPONS and cell != point:
                continue
            yield (*hit, cell)


def _first_hit(origin, point, occupied, actor_id):
    return next(_ray_hits(origin, point, occupied, actor_id), None)


def _clear_shot(origin, target, point, occupied, actor_id):
    hit = _first_hit(origin, point, occupied, actor_id)
    return bool(hit and hit[1] and hit[0].id == target.id)


def _shot_context(origin, point, occupied, actor_id, target_id):
    return tuple((cell, unit.id if unit else None)
                 for unit, _, cell in _ray_hits(origin, point, occupied, actor_id)
                 if unit is None or unit.id != target_id)


def _firing_point(origin, robot, target, memory, occupied):
    points = _target_points(origin, target)
    accepted = memory.accepted_shots.get((robot.id, origin, target.id))
    if accepted in points:
        points.remove(accepted)
        points.insert(0, accepted)
    return next((point for point in points
                 if distance(origin, point) <= robot.attack_range
                 and not memory.rejected(robot.id, origin, target.id, point)
                 and (target.kind != 'station' or accepted == point or not memory.failed_base_lanes
                      or not memory.failed_base_lanes.keys() & set(line_cells(origin, point)))
                 and _clear_shot(origin, target, point, occupied, robot.id)), None)


def _assault_reserved(turn, memory, ledger, robot):
    avoided = memory.avoided_steps(robot.id)
    reserved = ledger.reserved | avoided
    for uid, lane in ledger.robot_firing_lanes.items():
        if uid != robot.id:
            reserved.update(lane)
    # Attacks settle before movement, but stopping in a teammate's established
    # firing lane would interrupt its next shot. Preserve those lanes while
    # finding a new stance; current occupancy still governs every attack.
    robots = {r.id: r for r in turn.summon_robots}
    for uid, action in ledger.commands.items():
        shooter = robots.get(int(uid))
        if shooter is not None and action.get('action') == 'attack':
            aim = action['targetPos'][0]
            reserved.update(line_cells(shooter.pos, (aim['x'], aim['y'])))
    return reserved


def _planned_occupants(turn, ledger, occupied):
    # Add destinations without removing origins: a submitted move does not
    # prove that its old cell will be vacant. Only future stances use this view;
    # actual attacks continue to use the current observation.
    planned = occupied.copy()
    for robot in turn.summon_robots:
        action = ledger.commands.get(str(robot.id), {})
        if action.get('action') == 'move':
            point = action['targetPos'][0]
            planned[point['x'], point['y']] = robot, False
    return planned


def _act_toward_target(turn, memory, nav, ledger, robot, target, occupied, *, attack_only=False,
                       move_within=None, continuation=None):
    phase = "base" if target.kind == "station" else "breach" if target.kind == "wall" else "clear"
    facts = dict(phase=phase, target_id=target.id, target_kind=target.kind,
                 target_source="visible" if target in turn.enemies else "last_seen_building",
                 observed_health=target.health, focus_target_id=memory.focus_target_id,
                 clearance_entry=memory.clearance_entry if target.id == memory.focus_target_id else None,
                 approach_stalls=memory.approach_stalls.get(robot.id, 0),
                 failed_base_lane_count=len(memory.failed_base_lanes),
                 assault_choice="base_route" if phase == "base" else
                 "shared_clearance" if target.id == memory.focus_target_id else "local_clearance",
                 focus_unreachable=memory.focus_target_id is not None and target.id != memory.focus_target_id)
    facts['route_continued'] = continuation is not None
    point = _firing_point(robot.pos, robot, target, memory, occupied)
    if point is not None and ledger.add(robot.id, command("attack", point)):
        memory.objectives[robot.id] = RobotObjective(point, target.id if target.kind == "wall" else None, phase)
        memory.remember(turn, robot, "attack", point, target.id, occupied)
        reason = ("robot_attack_base" if phase == "base" else "robot_breach_wall"
                  if phase == "breach" else "robot_clear_path_blocker")
        ledger.explain(robot.id, reason, aim=point,
                       accepted_aim=memory.accepted_shots.get((robot.id, robot.pos, target.id)) == point,
                       **facts)
        return True
    if attack_only:
        return False
    reserved = _assault_reserved(turn, memory, ledger, robot)
    if move_within is not None:
        # Block the boundary as well as filtering goals: a nearby endpoint
        # must not authorize a route that exits and re-enters the inner ring.
        reserved |= {p for cell in move_within for p in neighbours(cell)} - move_within
    blocked = (turn.blocked | reserved) - {robot.pos}
    goals = set()
    planned = _planned_occupants(turn, ledger, occupied)
    reach = robot.attack_range
    nearest = _target_point(robot.pos, target)
    if memory.rejected(robot.id, robot.pos, target.id, nearest):
        # After exhausting alternate aims, prefer a closer stance for an
        # unexplained range/path rejection rather than another boundary shot.
        reach = min(reach, max(1, distance(robot.pos, nearest) - 1))
    candidates = {continuation.goal} if continuation else _attack_goals(turn, target.cells, reach)
    for i, origin in enumerate(sorted(candidates)):
        if i % 16 == 0:
            check_time(nav.deadline)
        if origin in blocked or move_within is not None and origin not in move_within:
            continue
        if (distance(origin, _target_point(origin, target)) <= reach
                and _firing_point(origin, robot, target, memory, planned) is not None):
            goals.add(origin)
    route = nav.search(robot, goals, reserved, with_endpoint=True)
    if continuation and route is not None and route[0] >= continuation.steps:
        return False
    if route is not None and route[1] is not None and ledger.add(robot.id, command("move", route[1])):
        memory.objectives[robot.id] = RobotObjective(_target_point(robot.pos, target),
                                                   target.id if target.kind == "wall" else None, phase)
        memory.remember(turn, robot, "move", route[1], target.id)
        memory.routes[robot.id] = AssaultRoute(turn.round, target.id, target.pos, route[2],
                                              route[0], route[1])
        if route[0] == 1:
            aim = _firing_point(route[2], robot, target, memory, planned)
            ledger.robot_firing_lanes[robot.id] = set(line_cells(route[2], aim))
        reason = ("robot_approach_base" if phase == "base" else "robot_approach_weak_wall"
                  if phase == "breach" else "robot_approach_path_blocker")
        ledger.explain(robot.id, reason, route_steps=route[0],
                       firing_goal=route[2], occupied_avoidance_count=len(memory.occupied_steps), **facts)
        return True
    return False


def _continue_assault_route(turn, memory, nav, ledger, robot, occupied):
    route = memory.routes.get(robot.id)
    if route is None or route.issued != turn.round - 1:
        return False
    targets = {u.id: u for u in memory.buildings.values() if u.id not in memory.rejected_buildings}
    targets.update((u.id, u) for u in turn.enemies)
    target = targets.get(route.target_id)
    if (target is not None and target.pos == route.target_pos
            and memory.approach_stalls.get(robot.id, 0) < ASSAULT_STALL_LIMIT
            and _act_toward_target(turn, memory, nav, ledger, robot, target, occupied,
                                   continuation=route)):
        return True
    # Disappearance, movement, a blocked stance or a non-shrinking path releases
    # the intent. Never chase a remembered character or wait on a stale route.
    memory.routes.pop(robot.id, None)
    return False


def _act_inside_base(turn, memory, nav, ledger, robot, base, occupied):
    """Keep an entered robot working inside before considering outer detours."""
    if base_distance(base, robot.pos) != 1:
        return False
    inner = {p for cell in base.cells for p in neighbours(cell)
             if turn.inside(p) and p not in base.cells}
    if _act_toward_target(turn, memory, nav, ledger, robot, base, occupied, move_within=inner):
        return True
    # A public robot may block one ray while a visible inner worker blocks the
    # others. Clear that local obstacle instead of retreating to the shared
    # outer wall. Do not invent targets or assume any occupant will move.
    targets = {}
    for point in _target_points(robot.pos, base):
        hit = _first_hit(robot.pos, point, occupied, robot.id)
        if hit and hit[1] and hit[2] in inner and hit[0].kind in (*CHARACTERS, *WEAPONS, 'wall'):
            targets[hit[0].id] = hit[0]
    for attack_only in (True, False):
        for target in targets.values():
            check_time(nav.deadline)
            if _act_toward_target(turn, memory, nav, ledger, robot, target, occupied,
                                  attack_only=attack_only, move_within=inner):
                ledger.notes[robot.id]['conditions']['assault_choice'] = 'inner_clearance'
                return True
    # If neither an inner firing route nor an actionable inner blocker exists,
    # retain the ordinary fallback; this is a preference, not an indefinite hold.
    return False


def _gap_clearance_target(turn, memory, nav, ledger, base, occupied):
    """Find the first enemy on a useful route through a gap, not on a base ray.

    Projected removal only chooses a target. Actual attacks and moves still
    require the current observation, so a live blocker is never walked through.
    """
    robots = [r for r in turn.summon_robots
              if r.id not in ledger.used and r.abnormal_state != 'dizzy']
    if not robots:
        return None
    robot_ids = {r.id for r in robots}
    ring = set(enemy_entries(turn, base))
    preferred = (ledger.robot_assault_entry if ledger.robot_assault_entry in ring
                 else memory.clearance_entry)
    entries = [p for p in ring if p == preferred or not (
        (hit := occupied.get(p)) and hit[0] is not None and hit[0].kind == 'wall')]
    inner = {p for cell in base.cells for p in neighbours(cell)
             if turn.inside(p) and p not in base.cells}
    for entry in sorted(entries, key=lambda p: (p != preferred,
                        min(distance(r.pos, p) for r in robots), p)):
        check_time(nav.deadline)
        region = inner | {entry}
        removable = {p: hit[0] for p, hit in occupied.items()
                     if p in region and hit[1] and hit[0] is not None
                     and hit[0].kind in (*CHARACTERS, *WEAPONS, 'wall')}
        holder = occupied.get(entry)
        vacated = {entry} if holder and holder[0] is not None and holder[0].id in robot_ids else set()
        fixed = ((turn.blocked - removable.keys() - vacated) | ledger.reserved
                 | set(memory.occupied_steps))
        if entry in fixed:
            continue
        projected = {p: hit for p, hit in occupied.items() if p not in removable}
        goals = {p for p in inner - fixed
                 if any(_clear_shot(p, base, cell, projected, -1) for cell in base.cells)}
        if not goals:
            continue
        first = removable.get(entry)
        initial = (int(first is not None), first.health if first else 0, 0)
        queue, costs, parents = [(*initial, entry)], {entry: initial}, {entry: None}
        target = None
        while queue:
            check_time(nav.deadline)
            count, hp, steps, point = heappop(queue)
            if (count, hp, steps) != costs[point]:
                continue
            if point in goals:
                path = []
                while point is not None:
                    path.append(point)
                    point = parents[point]
                target = next((removable[p] for p in reversed(path) if p in removable), None)
                break
            for step in neighbours(point):
                if step not in region or step in fixed:
                    continue
                obstacle = removable.get(step)
                cost = (count + int(obstacle is not None), hp + (obstacle.health if obstacle else 0),
                        steps + 1)
                if step not in costs or cost < costs[step]:
                    costs[step], parents[step] = cost, point
                    heappush(queue, (*cost, step))
        if target is None:
            continue
        # Do not lock onto an entry that no remaining robot can support.
        for robot in robots:
            check_time(nav.deadline)
            if _firing_point(robot.pos, robot, target, memory, occupied) is None:
                reserved = _assault_reserved(turn, memory, ledger, robot)
                goals = {p for p in _attack_goals(turn, target.cells, robot.attack_range)
                         - (turn.blocked - {robot.pos}) - reserved
                         if _firing_point(p, robot, target, memory, occupied) is not None}
                if nav.search(robot, goals, reserved) is None:
                    continue
            memory.clearance_entry = entry
            return target
    return None


def _focus_blocker(turn, memory, nav, ledger, base, occupied):
    """Clear a usable gap first, then fall back to base-directed ray blockers."""
    gap_target = _gap_clearance_target(turn, memory, nav, ledger, base, occupied)
    if gap_target is not None:
        memory.focus_target_id = gap_target.id
        return gap_target
    memory.clearance_entry = None
    blocked = []
    candidates = {}
    for robot in turn.summon_robots:
        if robot.id in ledger.used or robot.abnormal_state == 'dizzy':
            continue
        if _firing_point(robot.pos, robot, base, memory, occupied) is not None:
            continue
        blocked.append(robot)
        for point in _target_points(robot.pos, base):
            hit = _first_hit(robot.pos, point, occupied, robot.id)
            if hit and hit[1] and hit[0].kind in (*CHARACTERS, *WEAPONS, 'wall'):
                candidates[hit[0].id] = hit[0]
    viable = []
    for target in candidates.values():
        shooters = sum(_firing_point(r.pos, r, target, memory, occupied) is not None for r in blocked)
        if shooters:
            viable.append((target.id != memory.focus_target_id, -shooters,
                           min(distance(r.pos, target.pos) for r in blocked), target.id, target))
    target = min(viable, key=lambda row: row[:-1])[-1] if viable else None
    memory.focus_target_id = target.id if target else None
    return target


def _advance_toward_base(turn, memory, ledger, robot, base):
    """Use a legal forward step if every firing route and clearance failed."""
    current = base_distance(base, robot.pos)
    firing_lanes = set()
    shooters = {r.id: r for r in turn.summon_robots}
    for uid, action in ledger.commands.items():
        shooter = shooters.get(int(uid))
        if shooter and action.get('action') == 'attack':
            aim = action['targetPos'][0]
            firing_lanes.update(line_cells(shooter.pos, (aim['x'], aim['y'])))
    options = [p for p in neighbours(robot.pos) if turn.inside(p)
               and base_distance(base, p) < current and p not in turn.blocked
               and p not in ledger.reserved and p not in memory.avoided_steps(robot.id)
               and p not in firing_lanes]
    for step in sorted(options, key=lambda p: (base_distance(base, p), p)):
        if ledger.add(robot.id, command('move', step)):
            memory.remember(turn, robot, 'move', step)
            ledger.explain(robot.id, 'robot_advance_when_routes_blocked',
                           target_id=base.id, assault_choice='base_progress',
                           remaining_distance=base_distance(base, step))
            return True
    return False


def _make_room_inside(turn, memory, nav, ledger, base, occupied):
    """Open inner firing stances when our own frontage blocks the queue.

    Vacating controlled robots is a planning comparison only. Every submitted
    move still uses observed occupancy, and followers wait for the next view.
    """
    movable = [r for r in turn.summon_robots
               if r.id not in ledger.used and r.abnormal_state != 'dizzy']
    if len(movable) < 2:
        return False
    aims = {r.id: _firing_point(r.pos, r, base, memory, occupied)
            for r in turn.summon_robots if r.abnormal_state != 'dizzy'}
    waiting = [r for r in movable if aims[r.id] is None]
    if not waiting:
        return False
    inner = {p for cell in base.cells for p in neighbours(cell)
             if turn.inside(p) and p not in base.cells}
    positions = {r.pos for r in movable}
    movable_ids = {r.id for r in movable}
    fixed = (turn.blocked - positions) | ledger.reserved | set(memory.occupied_steps)
    future_occupied = {p: hit for p, hit in occupied.items()
                       if hit[0] is None or hit[0].id not in movable_ids}
    rays = {r.id: set(line_cells(r.pos, aims[r.id]))
            for r in turn.summon_robots if aims.get(r.id) is not None
            and (r.id in movable_ids or ledger.commands.get(str(r.id), {}).get('action') == 'attack')}
    firing_lanes = set().union(*rays.values()) if rays else set()
    queue = []
    for robot in waiting:
        check_time(nav.deadline)
        avoided = ledger.reserved | memory.avoided_steps(robot.id)
        if nav.memory:
            avoided |= nav.memory.blocked(robot.id)
        future_goals = {p for p in inner - fixed - avoided
                        if _firing_point(p, robot, base, memory, future_occupied) is not None}
        future = nav.distances_to(future_goals, vacated=positions, reserved=avoided,
                                  exact=True).get(robot.pos)
        if future is None:
            # An enemy, public robot or NPC still seals the passage; moving a
            # shooter cannot open it, so retain the ordinary clearance attack.
            continue
        current_goals = {p for p in _attack_goals(turn, base.cells, robot.attack_range)
                         - (turn.blocked - {robot.pos}) - avoided
                         if _firing_point(p, robot, base, memory, occupied) is not None}
        current = nav.search(robot, current_goals, avoided | firing_lanes)
        if current is None or current[0] >= future + 2:
            queue.append(robot)
    if not queue:
        return False

    # Measure depth from the waiting queue's doorway. An opening on the far
    # side must not turn its useful next step into another entrance at depth 0.
    open_entries = set(enemy_entries(turn, base)) - fixed
    free_inner = inner - fixed
    front = min(queue, key=lambda r: (base_distance(base, r.pos), r.id))
    entry_depths = {}
    if front.pos in free_inner:
        entrances = {front.pos}
    else:
        avoided = ledger.reserved | memory.avoided_steps(front.id)
        if nav.memory:
            avoided |= nav.memory.blocked(front.id)
        approaches = nav.distances_to({front.pos}, vacated=positions, reserved=avoided,
                                      exact=True)
        entries = {e: approaches[e] for e in open_entries if e in approaches
                   and any(distance(p, e) == 1 for p in free_inner)}
        if not entries:
            return False
        nearest = min(entries.values())
        # A scout's old doorway is only a tie-breaker. A nearer usable entry
        # must not send the queue to wait for a shooter on the opposite side.
        if (entries.get(ledger.robot_assault_entry) == nearest
                and front.id not in memory.entry_crossed):
            entries = {ledger.robot_assault_entry: entries[ledger.robot_assault_entry]}
        entry_depths = {p: -1 for p, length in entries.items() if length == nearest}
        entrances = {p for p in free_inner
                     if any(length == nearest and distance(p, e) == 1
                            for e, length in entries.items())}
    depth = dict.fromkeys(entrances, 0)
    pending = deque(sorted(entrances))
    while pending:
        point = pending.popleft()
        for step in neighbours(point):
            if step in free_inner and step not in depth:
                depth[step] = depth[point] + 1
                pending.append(step)

    # A shooter on the doorway can obstruct the same queue as an inner
    # shooter. Apply the same measurable route/space improvement checks to it.
    depth.update(entry_depths)

    def queue_routes(view, vacated, lanes, destinations=()):
        # Compare one projected move, without removing any other occupant.
        # These paths rank yields only; actual followers still wait for a view.
        routes = {}
        for follower in queue:
            check_time(nav.deadline)
            avoided = (ledger.reserved | memory.avoided_steps(follower.id)
                       | set(lanes) | set(destinations))
            if nav.memory:
                avoided |= nav.memory.blocked(follower.id)
            cleared = set(vacated) | {follower.pos}
            blocked = (turn.blocked - cleared) | avoided
            goals = {p for p in inner - blocked
                     if _firing_point(p, follower, base, memory, view) is not None}
            routes[follower.id] = nav.distances_to(
                goals, vacated=cleared, reserved=avoided, exact=True).get(follower.pos)
        return routes

    current_routes = queue_routes(occupied, (), firing_lanes)
    current_footprint = (positions | firing_lanes) & free_inner
    choices = []
    for robot in movable:
        check_time(nav.deadline)
        if robot.pos not in depth:
            continue
        protected = set().union(*(ray for uid, ray in rays.items() if uid != robot.id))
        blocked = turn.blocked | ledger.reserved | memory.avoided_steps(robot.id) | protected
        if nav.memory:
            blocked |= nav.memory.blocked(robot.id)
        for step in neighbours(robot.pos):
            if step in blocked or step not in depth or depth[step] < depth[robot.pos]:
                continue
            moved = occupied.copy()
            moved.pop(robot.pos, None)
            moved[step] = robot, False
            aim = _firing_point(step, robot, base, memory, moved)
            if aim is None:
                continue
            next_lanes = protected | set(line_cells(step, aim))
            next_footprint = ((positions - {robot.pos}) | {step} | next_lanes) & free_inner
            space_gain = len(current_footprint) - len(next_footprint)
            next_routes = queue_routes(moved, {robot.pos}, next_lanes, {step})
            relief = sum(length is not None and (current_routes[uid] is None
                         or length < current_routes[uid]) for uid, length in next_routes.items())
            worse_route = any(length is not None and (next_routes[uid] is None
                              or next_routes[uid] > length)
                              for uid, length in current_routes.items())
            # A corner-to-straight shot can release the aisle at equal depth.
            # Neither route access nor space may worsen, and one must improve
            # there, so neutral sideways moves cannot oscillate. Prefer relief over packing a
            # shooter farther away while the doorway remains blocked.
            if depth[step] == depth[robot.pos] and (worse_route or space_gain < 0
                                                  or relief == 0 and space_gain == 0):
                continue
            waiters = [follower for follower in queue
                       if (memory.routes.get(follower.id) is None
                           or memory.routes[follower.id].target_id == base.id
                           or (next_routes[follower.id] is not None
                               and (current_routes[follower.id] is None
                                    or next_routes[follower.id] < current_routes[follower.id])))]
            if not waiters:
                # Do not interrupt an already confirmed clearance approach
                # for packing that leaves its own ingress route unchanged.
                continue
            rank = (-relief, -space_gain, -depth[step], -depth[robot.pos], robot.id, step)
            choices.append((rank, step, robot, relief, space_gain, waiters))
    for _, step, robot, relief, space_gain, waiters in sorted(choices, key=lambda row: row[0]):
        if ledger.add(robot.id, command('move', step)):
            memory.remember(turn, robot, 'move', step)
            ledger.explain(robot.id, 'robot_make_room_inside', target_id=base.id,
                           from_pos=robot.pos, next_pos=step, inner_depth=depth[robot.pos],
                           next_depth=depth[step], waiting_robots=[r.id for r in waiters],
                           ingress_routes_improved=relief, inner_space_gain=space_gain)
            for follower in waiters:
                if follower.id not in ledger.used:
                    ledger.used.add(follower.id)
                    ledger.reserved.add(follower.pos)
                    memory.yield_waiters.setdefault(robot.id, set()).add(follower.id)
                    ledger.explain(follower.id, 'robot_wait_assault_lane',
                                   target_id=base.id, yielding_robot=robot.id,
                                   occupied_pos=robot.pos, next_pos=step)
            return True
    return False


def _wait_for_scout_gap(turn, memory, nav, ledger, robot, base, entry, occupied):
    """Wait only when an observed scout blocks an otherwise usable firing route.

    This hypothetical view just explains waiting; it never authorizes a move
    into a cell that has not actually been vacated.
    """
    holder = next((u for u in turn.workers if u.pos == entry), None)
    if holder is None:
        return False
    vacated = {holder.pos, robot.pos}
    reserved = _assault_reserved(turn, memory, ledger, robot)
    if nav.memory:
        reserved |= nav.memory.blocked(robot.id)
    blocked = (turn.blocked - vacated) | reserved
    future = {p: hit for p, hit in occupied.items()
              if hit[0] is None or hit[0].id != holder.id}
    goals = {p for p in _attack_goals(turn, base.cells, robot.attack_range) - blocked
             if _firing_point(p, robot, base, memory, future) is not None}
    route = nav.distances_to(goals, vacated=vacated, reserved=reserved,
                             exact=True).get(robot.pos)
    if route is None:
        return False
    ledger.used.add(robot.id)
    ledger.reserved.add(robot.pos)
    ledger.explain(robot.id, 'robot_wait_scout_gap', entry=entry,
                   worker_id=holder.id, route_steps_after_yield=route)
    return True


def act_robots(turn, memory, nav, ledger):
    base = enemy_base(turn)
    memory.observe(turn, base)
    ledger.robot_known_enemies = tuple(u for uid, u in memory.buildings.items()
                                       if uid not in memory.rejected_buildings)
    if not turn.summon_robots:
        memory.focus_target_id = None
        memory.clearance_entry = None
        return
    occupied = _occupants(turn, memory)
    if base is not None and not turn.is_day:
        _make_room_inside(turn, memory, nav, ledger, base, occupied)
    entry = open_assault_entry(turn, base, ledger.robot_assault_entry) if base else None
    if entry is not None and not turn.is_day:
        # Preserve observed crossing evidence for cooperative aisle selection;
        # it no longer forces robots to visit an entry before they may shoot.
        memory.entry_crossed.update(r.id for r in turn.summon_robots
                                    if r.pos == entry or base_distance(base, r.pos) < 2)
    # Give every robot its direct shot or obstacle-free route first. Only the
    # remaining robots participate in clearance; moving/attacking the base must
    # not supply imaginary firepower to the shared obstacle objective.
    previous_focus = memory.focus_target_id
    memory.focus_target_id = None
    for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
        check_time(nav.deadline)
        if robot.id in ledger.used:
            continue
        if turn.is_day or robot.abnormal_state == "dizzy" or base is None:
            ledger.used.add(robot.id)
            ledger.explain(robot.id, "robot_wait_day" if turn.is_day else "robot_dizzy" if robot.abnormal_state == "dizzy"
                           else "robot_enemy_base_missing")
            continue
        # An exposed wall/tower is not necessarily an unavoidable obstacle.
        # Search all reachable clear firing stances before spending attacks on
        # it. This retains current legal base shots without moving closer.
        _act_toward_target(turn, memory, nav, ledger, robot, base, occupied, attack_only=True)

    # Preserve observed ingress independently of a teammate's stalled approach.
    # All current base shots were allocated first, so their lanes remain protected.
    for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
        check_time(nav.deadline)
        if robot.id not in ledger.used:
            _act_inside_base(turn, memory, nav, ledger, robot, base, occupied)

    # Keep a confirmed, shrinking route (including a clearance approach) before
    # another speculative base detour or a teammate's shared clearance choice.
    for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
        check_time(nav.deadline)
        if robot.id not in ledger.used:
            _continue_assault_route(turn, memory, nav, ledger, robot, occupied)

    # A found route is not evidence that repeatedly following it is useful.
    # After correlated failures/backtracking, jointly clear an observed enemy
    # on base-directed rays before allocating another speculative detour.
    # Direct base shooters were allocated first and cannot be pulled away.
    early_focus = None
    if base and not turn.is_day and any(
            memory.approach_stalls.get(r.id, 0) >= ASSAULT_STALL_LIMIT
            for r in turn.summon_robots if r.id not in ledger.used):
        memory.focus_target_id = previous_focus
        early_focus = _focus_blocker(turn, memory, nav, ledger, base, occupied)
        if early_focus is not None:
            for attack_only in (True, False):
                for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
                    check_time(nav.deadline)
                    if robot.id not in ledger.used:
                        # Immediate supporting shots remain useful, but another
                        # robot's stall must not pull a progressing follower off
                        # its own reachable base route to approach the shared wall.
                        if (not attack_only and memory.approach_stalls.get(robot.id, 0) < ASSAULT_STALL_LIMIT
                                and _act_toward_target(turn, memory, nav, ledger, robot, base, occupied)):
                            continue
                        _act_toward_target(turn, memory, nav, ledger, robot, early_focus,
                                           occupied, attack_only=attack_only)

    # Allocate all existing shots before moves so even a higher-ID shooter's
    # lane is protected from a lower-ID robot's new stance.
    for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
        check_time(nav.deadline)
        if robot.id not in ledger.used:
            _act_toward_target(turn, memory, nav, ledger, robot, base, occupied)

    # An entry is a possible route, never a mandatory waypoint. Ordinary
    # firing-position navigation already traverses open gaps when useful.
    # Only a necessary, still-occupied scout gap can hold the remaining robots.
    if entry is not None and not turn.is_day:
        for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
            check_time(nav.deadline)
            if robot.id not in ledger.used:
                _wait_for_scout_gap(turn, memory, nav, ledger, robot, base, entry, occupied)

    memory.focus_target_id = early_focus.id if early_focus else previous_focus
    focus = early_focus or (_focus_blocker(turn, memory, nav, ledger, base, occupied)
                            if base and not turn.is_day else None)
    if focus is None:
        memory.focus_target_id = None
    else:
        for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
            check_time(nav.deadline)
            if robot.id not in ledger.used:
                _act_toward_target(turn, memory, nav, ledger, robot, focus, occupied, attack_only=True)
    for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
        check_time(nav.deadline)
        if robot.id in ledger.used:
            continue
        hits = [_first_hit(robot.pos, point, occupied, robot.id)
                for point in _target_points(robot.pos, base)]
        hit = hits[0]
        # Join the shared target by moving when necessary, rather than silently
        # reverting to a different wall just because today's ray is blocked.
        if (focus is not None
                and _act_toward_target(turn, memory, nav, ledger, robot, focus, occupied)):
            continue
        target = hit[0] if hit and hit[1] and hit[0].kind in (*CHARACTERS, *WEAPONS, "wall", "station") else base
        # With no base shot, a reachable enemy on another base-directed ray
        # can be cleared now instead of idling behind an unattackable obstacle.
        tried = set()
        for obstruction in hits:
            if not obstruction or not obstruction[1] or obstruction[0].id in tried:
                continue
            blocker = obstruction[0]
            tried.add(blocker.id)
            if blocker.kind in (*CHARACTERS, *WEAPONS, "wall") and _act_toward_target(
                    turn, memory, nav, ledger, robot, blocker, occupied, attack_only=True):
                break
        else:
            blocker = None
        if blocker is not None:
            continue
        if _act_toward_target(turn, memory, nav, ledger, robot, target, occupied):
            continue
        if hit and not hit[1]:
            # An unattackable NPC/friendly obstruction can require a detour
            # through a wall when no free firing position is reachable.
            detour = _new_objective(turn, base, robot, nav, ledger, memory.avoided_steps(robot.id))
            wall = next((u for u in turn.enemies if detour and u.id == detour.wall_id), None)
            if wall and _act_toward_target(turn, memory, nav, ledger, robot, wall, occupied):
                continue
        if _advance_toward_base(turn, memory, ledger, robot, base):
            continue
        ledger.used.add(robot.id)
        ledger.reserved.add(robot.pos)
        ledger.explain(robot.id, "robot_assault_path_blocked", target_id=target.id,
                       first_blocker=hit[2] if hit else None,
                       focus_target_id=memory.focus_target_id,
                       rejected_stance=memory.rejected(robot.id, robot.pos, target.id, _target_point(robot.pos, target)))
