"""One ordinary worker maintains shared sight around the night assault."""
from dataclasses import dataclass

from .commands import command
from .model import distance, neighbours
from .navigation import check_time
from .projectiles import line_cells
from .robot_assault import base_distance, enemy_base, rear_approach


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


def _routes(turn, memory, nav, ledger, scout, base, reserved):
    blocked = (turn.blocked - {scout.pos}) | reserved
    points = _observation_points(turn, base)
    goals = {(x, y) for x in range(max(0, base.pos[0] - 4), min(turn.width, base.pos[0] + 6))
             for y in range(max(0, base.pos[1] - 5), min(turn.height, base.pos[1] + 5))
             if (x, y) not in blocked and 3 <= base_distance(base, (x, y)) <= 4}
    routes = []
    for goal in sorted(goals):
        route = nav.search(scout, {goal}, reserved)
        if route is not None:
            coverage = sum(weight for point, weight in points.items() if distance(goal, point) <= 4)
            base_cells = sum(distance(goal, cell) <= 4 for cell in base.cells)
            routes.append((goal, route, coverage, base_cells))
    # Keep full base sight where possible. A far-side post is preferable only
    # when its detour is small; don't turn a six-step arrival into eleven.
    if routes:
        most_base = max(row[3] for row in routes)
        routes = [row for row in routes if row[3] == most_base]
        shortest = min(row[1][0] for row in routes)
        routes = [row for row in routes if row[1][0] <= shortest + 2]
    return sorted(routes, key=lambda row: (-row[3], int(not rear_approach(turn, base, row[0])),
                  -(row[2] + (2 if row[0] == memory.post else 0)) / (4 + row[1][0]),
                  row[1][0], row[0]))


def prepare_night_scout(turn, memory, nav, ledger, excluded=(), jobs=None):
    """Leave near dusk, as late as the route permits, without taking paid work."""
    base = enemy_base(turn)
    if not turn.is_day or base is None or turn.tick < 50:
        return None
    key = turn.day, base.id, base.pos
    if memory.key != key:
        memory.key, memory.worker_id, memory.post = key, None, None
    jobs = jobs or {}
    candidates = []
    for scout in turn.workers:
        job = jobs.get(scout.id, {})
        if (scout.id in ledger.used or scout.id in excluded or scout.health <= 165
                or any('UpgradeVoucher' in item or item == 'WallFixer' for item in scout.backpack)
                or job.get('kind') in ('sell', 'buy', 'use', 'robot_buy', 'build')):
            continue
        routes = _routes(turn, memory, nav, ledger, scout, base, ledger.reserved)
        if routes:
            goal, route, coverage, base_cells = routes[0]
            candidates.append((scout.id != memory.worker_id, route[0], scout.id,
                               scout, goal, route, coverage, base_cells))
    if not candidates:
        return None
    _, _, _, scout, goal, route, coverage, base_cells = min(candidates)
    # No more than the final twenty daylight moves are spent scouting. A
    # distant worker may arrive after nightfall; ordinary income comes first.
    if route[0] + 2 < turn.day_left and turn.tick < 68:
        return None
    memory.worker_id, memory.post = scout.id, goal
    if route[1] is None:
        ledger.used.add(scout.id)
        ledger.reserved.add(scout.pos)
        ledger.explain(scout.id, 'dusk_scout_hold', post=goal, post_base_cells=base_cells)
    elif ledger.add(scout.id, command('move', route[1])):
        ledger.explain(scout.id, 'dusk_scout_approach', post=goal, route_steps=route[0],
                       coverage=coverage, post_base_cells=base_cells)
    else:
        return None
    return scout


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
    routes = _routes(turn, memory, nav, ledger, scout, base, reserved)
    if routes:
        goal, route, score, base_cells = routes[0]
        memory.post = goal
        if route[1] is None:
            ledger.used.add(scout.id)
            ledger.reserved.add(scout.pos)
            ledger.explain(scout.id, 'night_scout_hold', post=goal, coverage=score,
                           post_base_cells=base_cells)
            return scout
        if ledger.add(scout.id, command('move', route[1])):
            ledger.explain(scout.id, 'night_scout_approach', post=goal,
                           coverage=score, route_steps=route[0],
                           post_base_cells=base_cells)
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
