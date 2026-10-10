"""One ordinary worker maintains shared sight around the night assault."""
from dataclasses import dataclass

from .commands import command
from .model import distance, neighbours
from .navigation import check_time
from .projectiles import line_cells
from .robot_assault import base_distance, enemy_base


@dataclass
class ScoutMemory:
    key: tuple | None = None
    worker_id: int | None = None
    post: tuple | None = None


def _observation_points(turn, base):
    # Observe the attack corridor and the construction ring, including cells
    # whose occupants are not yet visible. Never invent hidden attack targets.
    points = {p: 1 for x in range(max(0, base.pos[0] - 2), min(turn.width, base.pos[0] + 4))
              for y in range(max(0, base.pos[1] - 3), min(turn.height, base.pos[1] + 3))
              if base_distance(base, p := (x, y)) <= 2}
    for robot in turn.summon_robots:
        target = min(base.cells, key=lambda p: (distance(robot.pos, p), p))
        for point in line_cells(robot.pos, target):
            if base_distance(base, point) <= 4:
                points[point] = 4
    return points


def act_night_scout(turn, memory, nav, ledger, home_roles=()):
    base = enemy_base(turn)
    key = (turn.day, base.id, base.pos) if base and not turn.is_day else None
    if key != memory.key:
        memory.key, memory.worker_id, memory.post = key, None, None
    if key is None:
        return None
    workers = {h.id: h for h in turn.workers}
    scout = workers.get(memory.worker_id)
    if scout is None:
        candidates = [h for h in workers.values() if h.id not in ledger.used]
        scout = min(candidates, key=lambda h: (tuple(h.id == uid for uid in home_roles if uid is not None),
                    bool(h.inventory['WallFixer']), base_distance(base, h.pos), -h.health, h.id), default=None)
        memory.worker_id, memory.post = (scout.id if scout else None), None
    if scout is None or scout.id in ledger.used:
        return scout
    if scout.abnormal_state == 'dizzy':
        ledger.used.add(scout.id)
        ledger.reserved.add(scout.pos)
        ledger.explain(scout.id, 'night_scout_dizzy')
        return scout
    if scout.health <= 165 and scout.inventory['Medicine']:
        if ledger.add(scout.id, command('use', name='Medicine')):
            ledger.explain(scout.id, 'night_scout_heal')
            return scout

    # Public robots have unknown ownership. Own summoned robots are the only
    # exception; keep a movement-turn margin outside every other robot's reach.
    threats = {r.id: r for r in turn.robots if r.id not in turn.summon_robot_ids}
    danger = set()
    for robot in threats.values():
        check_time(nav.deadline)
        reach = robot.attack_range + 1
        danger.update((x, y)
                      for x in range(max(0, robot.pos[0] - reach), min(turn.width, robot.pos[0] + reach + 1))
                      for y in range(max(0, robot.pos[1] - reach), min(turn.height, robot.pos[1] + reach + 1)))
    lanes = set()
    for robot in turn.summon_robots:
        action = ledger.commands.get(str(robot.id), {})
        if action.get('action') == 'attack':
            aim = action['targetPos'][0]
            lanes.update(line_cells(robot.pos, (aim['x'], aim['y'])))
        else:
            target = min(base.cells, key=lambda p: (distance(robot.pos, p), p))
            lanes.update(line_cells(robot.pos, target))
    reserved = ledger.reserved | danger | lanes
    blocked = (turn.blocked - {scout.pos}) | reserved
    points = _observation_points(turn, base)
    goals = {(x, y) for x in range(max(0, base.pos[0] - 4), min(turn.width, base.pos[0] + 6))
             for y in range(max(0, base.pos[1] - 5), min(turn.height, base.pos[1] + 5))
             if (x, y) not in blocked and 3 <= base_distance(base, (x, y)) <= 4}
    coverage = {p: sum(weight for point, weight in points.items() if distance(p, point) <= 4)
                for p in goals}
    routes = []
    for goal in sorted(goals):
        route = nav.search(scout, {goal}, reserved)
        if route is not None:
            routes.append((coverage[goal], goal, route))
    if routes:
        # Establish useful sight soon instead of crossing the entire frontage
        # for a marginally larger footprint. Prefer full base coverage when
        # available, then discount corridor coverage by travel time. Four turns
        # keep nearby gains useful without abandoning an established post for
        # small improvements; the existing two-point retention bonus remains.
        base_coverage = {p: sum(distance(p, cell) <= 4 for cell in base.cells) for p in goals}
        choice = min(routes, key=lambda row: (-base_coverage[row[1]],
                     -(row[0] + (2 if row[1] == memory.post else 0)) / (4 + row[2][0]),
                     row[2][0], row[1]))
        score, goal, route = choice
        memory.post = goal
        if route[1] is None:
            ledger.used.add(scout.id)
            ledger.reserved.add(scout.pos)
            ledger.explain(scout.id, 'night_scout_hold', post=goal, coverage=score,
                           post_base_cells=base_coverage[goal])
            return scout
        if ledger.add(scout.id, command('move', route[1])):
            ledger.explain(scout.id, 'night_scout_approach', post=goal,
                           coverage=score, route_steps=route[0],
                           post_base_cells=base_coverage[goal])
            return scout

    # If a wave closes the safe route, retain this worker's role. A threatened
    # observer may take an improving escape step instead of walking into fire.
    def risk(point):
        return sum(r.power for r in threats.values() if distance(point, r.pos) <= r.attack_range + 1)
    current_risk = risk(scout.pos)
    choices = [p for p in neighbours(scout.pos) if turn.inside(p)
               and p not in turn.blocked and p not in ledger.reserved
               and p not in lanes and p not in (nav.memory.blocked(scout.id) if nav.memory else ())
               and (risk(p) < current_risk or scout.pos in lanes and risk(p) <= current_risk)]
    if choices:
        step = min(choices, key=lambda p: (risk(p), -min((distance(p, r.pos) for r in threats.values()), default=0), p))
        if ledger.add(scout.id, command('move', step)):
            memory.post = None
            ledger.explain(scout.id, 'night_scout_escape', observed_risk=current_risk, next_risk=risk(step))
            return scout
    ledger.used.add(scout.id)
    ledger.reserved.add(scout.pos)
    ledger.explain(scout.id, 'night_scout_route_blocked', post=memory.post, observed_risk=current_risk)
    return scout
