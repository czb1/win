"""Place controllable robots at a weak entrance, breach it, then attack base."""
from collections import deque
from dataclasses import dataclass, field

from .commands import command
from .model import CHARACTERS, WEAPONS, distance, neighbours
from .navigation import check_time


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
    day: int | None = None
    base_id: int | None = None

    def observe(self, turn, base):
        if self.day != turn.day or self.base_id != (base.id if base else None):
            self.objectives.clear()
        self.day, self.base_id = turn.day, base.id if base else None
        alive = turn.summon_robot_ids
        self.objectives = {uid: goal for uid, goal in self.objectives.items() if uid in alive}


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


def _spawn_at_entry(turn, ledger, base, entry, wall, deadline):
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
        if legal_summon_position(turn, point) and point not in pending:
            candidate = length, base_distance(base, point), distance(point, entry), point
            if best is None or candidate < best:
                best = candidate
            continue
        for step in neighbours(point):
            if step not in seen and turn.inside(step) and step not in blocked:
                seen.add(step)
                queue.append((step, length + 1))
    return best


def weakest_approach(turn, ledger, deadline=None):
    base = enemy_base(turn)
    if base is None:
        return None
    entries = sorted((weakness(wall), point, wall) for point, wall in _entry_walls(turn, base))
    chosen, chosen_key = None, None
    for rank, entry, wall in entries:
        if chosen_key is not None and rank > chosen_key[:3]:
            break
        spawn = _spawn_at_entry(turn, ledger, base, entry, wall, deadline)
        if spawn is None:
            continue
        key = (*rank, *spawn[:3], entry, spawn[3])
        if chosen_key is None or key < chosen_key:
            chosen_key = key
            chosen = AssaultPlan(entry, wall, spawn[3])
    return chosen


def choose_summon_position(turn, ledger, deadline=None):
    plan = weakest_approach(turn, ledger, deadline)
    return plan.position if plan else None


def _attack_goals(turn, target_cells, reach):
    return {point for cell in target_cells
            for x in range(max(0, cell[0] - reach), min(turn.width, cell[0] + reach + 1))
            for y in range(max(0, cell[1] - reach), min(turn.height, cell[1] + reach + 1))
            if (point := (x, y)) not in target_cells}


def _new_objective(turn, base, robot, nav, ledger):
    entries = sorted((weakness(wall), distance(robot.pos, point), point, wall)
                     for point, wall in _entry_walls(turn, base))
    for _, _, entry, wall in entries:
        check_time(nav.deadline)
        # With no wall to breach, the next objective is immediately the base.
        if wall is None:
            return RobotObjective(entry, None, "base")
        if distance(robot.pos, wall.pos) <= robot.attack_range:
            return RobotObjective(entry, wall.id, "breach")
        route = nav.search(robot, _attack_goals(turn, wall.cells, robot.attack_range), ledger.reserved)
        if route is not None:
            return RobotObjective(entry, wall.id, "breach")
    return None


def _target_point(robot, target):
    return min(target.cells, key=lambda point: (distance(robot.pos, point), point != target.pos, point))


def act_robots(turn, memory, nav, ledger):
    base = enemy_base(turn)
    memory.observe(turn, base)
    if not turn.summon_robots:
        return
    by_id = {u.id: u for u in turn.enemies if u.kind == "wall"}
    by_pos = {u.pos: u for u in by_id.values()}
    for robot in sorted(turn.summon_robots, key=lambda unit: unit.id):
        check_time(nav.deadline)
        if robot.id in ledger.used:
            continue
        if turn.is_day or robot.abnormal_state == "dizzy" or base is None:
            ledger.used.add(robot.id)
            ledger.explain(robot.id, "robot_wait_day" if turn.is_day else "robot_dizzy" if robot.abnormal_state == "dizzy"
                           else "robot_enemy_base_missing")
            continue
        goal = memory.objectives.get(robot.id)
        if goal is None:
            goal = _new_objective(turn, base, robot, nav, ledger)
            if goal is not None:
                memory.objectives[robot.id] = goal
        if goal is None:
            ledger.used.add(robot.id)
            ledger.explain(robot.id, "robot_no_reachable_entry")
            continue
        # Walls are globally visible: absence of this exact live wall proves
        # the breach. An estimated lethal shot never changes the phase early.
        if goal.phase == "breach" and goal.wall_id not in by_id:
            goal.phase, goal.wall_id = "base", None
        rebuilt = by_pos.get(goal.entry)
        if goal.phase == "base" and rebuilt and base_distance(base, robot.pos) >= 2:
            goal.phase, goal.wall_id = "breach", rebuilt.id
        target = by_id[goal.wall_id] if goal.phase == "breach" else base
        point = _target_point(robot, target)
        if distance(robot.pos, point) <= robot.attack_range:
            if ledger.add(robot.id, command("attack", point)):
                ledger.explain(robot.id, "robot_breach_wall" if goal.phase == "breach" else "robot_attack_base",
                               phase=goal.phase, entry=goal.entry, target_id=target.id,
                               wall_level=target.level if goal.phase == "breach" else None,
                               observed_health=target.health)
                continue
        route = nav.search(robot, _attack_goals(turn, target.cells, robot.attack_range), ledger.reserved)
        if route is not None and route[1] is not None and ledger.add(robot.id, command("move", route[1])):
            ledger.explain(robot.id, "robot_approach_weak_wall" if goal.phase == "breach" else "robot_approach_base",
                           phase=goal.phase, entry=goal.entry, target_id=target.id, route_steps=route[0])
            continue
        # Clear an observed enemy character only when it prevents advancing;
        # own units, NPCs and public robots never become attack targets.
        blockers = [u for u in turn.enemies if u.kind in CHARACTERS
                    and distance(robot.pos, u.pos) <= robot.attack_range]
        blocker = min(blockers, key=lambda unit: (distance(unit.pos, point), unit.health, unit.id), default=None)
        if blocker and ledger.add(robot.id, command("attack", blocker.pos)):
            ledger.explain(robot.id, "robot_clear_enemy_blocker", phase=goal.phase, target_id=blocker.id)
            continue
        ledger.used.add(robot.id)
        ledger.reserved.add(robot.pos)
        ledger.explain(robot.id, "robot_assault_path_blocked", phase=goal.phase,
                       entry=goal.entry, target_id=target.id)
