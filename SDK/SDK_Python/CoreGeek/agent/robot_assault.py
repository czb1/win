"""Deploy behind the enemy frontage and clear the path toward its base."""
from collections import deque
from dataclasses import dataclass, field
import re

from .commands import command
from .model import CHARACTERS, WEAPONS, distance, neighbours
from .navigation import check_time
from .projectiles import line_cells


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


@dataclass
class RobotAssaultMemory:
    objectives: dict = field(default_factory=dict)
    buildings: dict = field(default_factory=dict)
    rejected_buildings: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    failed_shots: set = field(default_factory=set)
    shot_contexts: dict = field(default_factory=dict)
    accepted_shots: dict = field(default_factory=dict)
    failed_steps: dict = field(default_factory=dict)
    step_failures: dict = field(default_factory=dict)
    occupied_steps: dict = field(default_factory=dict)
    occupied_failures: dict = field(default_factory=dict)
    robot_ids: set = field(default_factory=set)
    day: int | None = None
    base_id: int | None = None
    base_pos: tuple | None = None
    focus_target_id: int | None = None

    def observe(self, turn, base):
        base_id = base.id if base else None
        base_pos = base.pos if base else None
        changed_base = self.base_id != base_id or self.base_pos != base_pos
        if changed_base:
            self.buildings.clear()
        if self.day != turn.day or changed_base:
            self.focus_target_id = None
            self.objectives.clear()
            self.pending.clear()
            self.failed_shots.clear()
            self.shot_contexts.clear()
            self.accepted_shots.clear()
            self.failed_steps.clear()
            self.step_failures.clear()
            self.occupied_steps.clear()
            self.occupied_failures.clear()
            self.rejected_buildings.clear()
        self.day, self.base_id, self.base_pos = turn.day, base_id, base_pos
        alive = turn.summon_robot_ids
        self.robot_ids = alive.copy()
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
        results = turn.raw.get("lastRoundRoleActionResults") or {}
        occupied_errors = _occupied_move_errors(turn)
        robots = {r.id: r for r in turn.summon_robots}
        visible_ids = {u.id for u in turn.enemies}
        for uid, (issued, origin, action, point, target_id, context) in self.pending.items():
            if issued != turn.round - 1 or uid not in alive:
                continue
            result = results.get(str(uid), results.get(uid))
            if action == "attack" and result is False:
                key = uid, origin, target_id, point
                self.failed_shots.add(key)
                self.shot_contexts[key] = context
                self.accepted_shots.pop((uid, origin, target_id), None)
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
            elif action == "move":
                key = uid, point
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
        self.pending = {uid: value for uid, value in self.pending.items()
                        if uid in alive and value[0] == turn.round}
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
        for point in tuple(self.occupied_failures):
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
        if point not in self.occupied_failures:
            return
        self.occupied_steps.pop(point, None)
        self.occupied_failures.pop(point, None)
        self.failed_steps = {key: until for key, until in self.failed_steps.items() if key[1] != point}
        self.step_failures = {key: count for key, count in self.step_failures.items() if key[1] != point}

    def avoided_steps(self, uid):
        return set(self.occupied_steps) | {p for robot_id, p in self.failed_steps if robot_id == uid}

    def remember(self, turn, robot, action, point, target_id=None, occupied=None):
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


def _spawn_at_entry(turn, ledger, base, entry, wall, deadline, rear_only=False):
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
            if step not in seen and turn.inside(step) and step not in blocked:
                seen.add(step)
                queue.append((step, length + 1))
    return best


def weakest_approach(turn, ledger, deadline=None, *, prefer_rear=False):
    base = enemy_base(turn)
    if base is None:
        return None
    entries = sorted((_entry_rank(turn, base, point, wall, prefer_rear), point, wall)
                     for point, wall in _entry_walls(turn, base))
    chosen, chosen_key = None, None
    for rank, entry, wall in entries:
        if chosen_key is not None and rank > chosen_key[:len(rank)]:
            break
        spawn = _spawn_at_entry(turn, ledger, base, entry, wall, deadline,
                                rear_only=prefer_rear and rear_approach(turn, base, entry))
        if spawn is None:
            continue
        key = (*rank, *spawn[:3], entry, spawn[3])
        if chosen_key is None or key < chosen_key:
            chosen_key = key
            chosen = AssaultPlan(entry, wall, spawn[3])
    return chosen


def choose_summon_position(turn, ledger, deadline=None):
    plan = weakest_approach(turn, ledger, deadline, prefer_rear=True)
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


def _first_hit(origin, point, occupied, actor_id):
    for cell in line_cells(origin, point):
        hit = occupied.get(cell)
        if hit is not None and (hit[0] is None or hit[0].id != actor_id):
            return *hit, cell
    return None


def _clear_shot(origin, target, point, occupied, actor_id):
    hit = _first_hit(origin, point, occupied, actor_id)
    return bool(hit and hit[1] and hit[0].id == target.id)


def _shot_context(origin, point, occupied, actor_id, target_id):
    return tuple((cell, hit[0].id if hit[0] else None)
                 for cell in line_cells(origin, point)
                 if (hit := occupied.get(cell)) is not None
                 and (hit[0] is None or hit[0].id not in (actor_id, target_id)))


def _firing_point(origin, robot, target, memory, occupied):
    points = _target_points(origin, target)
    accepted = memory.accepted_shots.get((robot.id, origin, target.id))
    if accepted in points:
        points.remove(accepted)
        points.insert(0, accepted)
    return next((point for point in points
                 if distance(origin, point) <= robot.attack_range
                 and not memory.rejected(robot.id, origin, target.id, point)
                 and _clear_shot(origin, target, point, occupied, robot.id)), None)


def _act_toward_target(turn, memory, nav, ledger, robot, target, occupied, *, attack_only=False):
    phase = "base" if target.kind == "station" else "breach" if target.kind == "wall" else "clear"
    facts = dict(phase=phase, target_id=target.id, target_kind=target.kind,
                 target_source="visible" if target in turn.enemies else "last_seen_building",
                 observed_health=target.health, focus_target_id=memory.focus_target_id)
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
    avoided = memory.avoided_steps(robot.id)
    reserved = ledger.reserved | avoided
    blocked = (turn.blocked | reserved) - {robot.pos}
    goals = set()
    reach = robot.attack_range
    nearest = _target_point(robot.pos, target)
    if memory.rejected(robot.id, robot.pos, target.id, nearest):
        # After exhausting alternate aims, prefer a closer stance for an
        # unexplained range/path rejection rather than another boundary shot.
        reach = min(reach, max(1, distance(robot.pos, nearest) - 1))
    for i, origin in enumerate(sorted(_attack_goals(turn, target.cells, reach))):
        if i % 16 == 0:
            check_time(nav.deadline)
        if origin in blocked:
            continue
        if _firing_point(origin, robot, target, memory, occupied) is not None:
            goals.add(origin)
    route = nav.search(robot, goals, reserved)
    if route is not None and route[1] is not None and ledger.add(robot.id, command("move", route[1])):
        memory.objectives[robot.id] = RobotObjective(_target_point(robot.pos, target),
                                                   target.id if target.kind == "wall" else None, phase)
        memory.remember(turn, robot, "move", route[1])
        reason = ("robot_approach_base" if phase == "base" else "robot_approach_weak_wall"
                  if phase == "breach" else "robot_approach_path_blocker")
        ledger.explain(robot.id, reason, route_steps=route[0],
                       occupied_avoidance_count=len(memory.occupied_steps), **facts)
        return True
    return False


def _focus_blocker(turn, memory, ledger, base, occupied):
    """Coordinate exposed obstacles on base-directed rays, never hunt gunners."""
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


def act_robots(turn, memory, nav, ledger):
    base = enemy_base(turn)
    memory.observe(turn, base)
    if not turn.summon_robots:
        memory.focus_target_id = None
        return
    ledger.robot_known_enemies = tuple(u for uid, u in memory.buildings.items()
                                       if uid not in memory.rejected_buildings)
    occupied = _occupants(turn, memory)
    focus = _focus_blocker(turn, memory, ledger, base, occupied) if base and not turn.is_day else None
    for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
        check_time(nav.deadline)
        if robot.id in ledger.used:
            continue
        if turn.is_day or robot.abnormal_state == "dizzy" or base is None:
            ledger.used.add(robot.id)
            ledger.explain(robot.id, "robot_wait_day" if turn.is_day else "robot_dizzy" if robot.abnormal_state == "dizzy"
                           else "robot_enemy_base_missing")
            continue
        # Check all four base cells before giving up an attack to reposition
        # or clear a blocker on just one ray. Successful aims remain preferred
        # only while their current occupancy and range still permit firing.
        if _act_toward_target(turn, memory, nav, ledger, robot, base, occupied, attack_only=True):
            continue
        hits = [_first_hit(robot.pos, point, occupied, robot.id)
                for point in _target_points(robot.pos, base)]
        hit = hits[0]
        if (focus is not None and any(h and h[1] and h[0].id != base.id for h in hits)
                and _act_toward_target(turn, memory, nav, ledger, robot, focus, occupied, attack_only=True)):
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
        ledger.used.add(robot.id)
        ledger.reserved.add(robot.pos)
        ledger.explain(robot.id, "robot_assault_path_blocked", target_id=target.id,
                       first_blocker=hit[2] if hit else None,
                       rejected_stance=memory.rejected(robot.id, robot.pos, target.id, _target_point(robot.pos, target)))
