"""One ordinary worker maintains shared sight around the night assault."""
from dataclasses import dataclass

from .commands import command
from .model import distance, neighbours, SUMMON_ORDERS, WEAPONS
from .navigation import check_time
from .projectiles import line_cells
from .robot_assault import (base_distance, enemy_base, enemy_entries,
                            first_day_clear_sides, in_summon_cluster,
                            open_assault_entry, rear_approach)


FIRST_DAY_SCOUT_TICK = 25


@dataclass
class ScoutMemory:
    key: tuple | None = None
    worker_id: int | None = None
    post: tuple | None = None
    wall_entry: tuple | None = None


def _sync_scout(turn, memory, base):
    key = (turn.day, base.id, base.pos) if base and turn.day == 1 else None
    if memory.key != key or key is None:
        memory.key, memory.worker_id, memory.post, memory.wall_entry = key, None, None, None


def prepare_first_day_wall_blocker(turn, memory, nav, ledger, excluded=(), jobs=None,
                                   worker_id=None, departure_tick=FIRST_DAY_SCOUT_TICK,
                                   reserve_hold=True):
    """Occupy an unbuilt upper/lower enemy wall cell after the departure tick."""
    base = enemy_base(turn)
    _sync_scout(turn, memory, base)
    if not turn.is_day or turn.day != 1 or turn.tick < departure_tick or base is None:
        return None
    if memory.worker_id is not None and not any(w.id == memory.worker_id for w in turn.workers):
        memory.worker_id, memory.post, memory.wall_entry = None, None, None
    xs, ys = zip(*base.cells)
    entries = [p for p in enemy_entries(turn, base)
               if p[1] in (min(ys) - 2, max(ys) + 2)]
    clear_sides = first_day_clear_sides(turn, base, ledger.robot_known_enemies)
    weapon_cells = {u.pos for u in (*ledger.robot_known_enemies, *turn.enemies)
                    if u.kind in WEAPONS}
    # Compare the nearest known turret, then avoid an immediately adjacent
    # turret in the entry's inward column when Chebyshev distances tie.
    entry_clearance = {entry: (
        -min((distance(entry, p) for p in weapon_cells), default=0),
        (entry[0], entry[1] + (1 if entry[1] < min(ys) else -1)) in weapon_cells,
    ) for entry in entries}
    jobs = jobs or {}
    candidates = []
    for scout in turn.workers:
        if worker_id is not None and scout.id != worker_id:
            continue
        incumbent = scout.id == memory.worker_id
        if scout.id in ledger.used or scout.id in excluded:
            continue
        if worker_id is None and not incumbent and (scout.health <= 165
                or any('UpgradeVoucher' in item or item == 'WallFixer' or item in SUMMON_ORDERS
                       for item in scout.backpack)
                or jobs.get(scout.id, {}).get('kind') in ('sell', 'buy', 'use', 'robot_buy')):
            continue
        for entry in entries:
            check_time(nav.deadline)
            if entry in (turn.blocked - {scout.pos}) | ledger.reserved:
                continue
            route = nav.search(scout, {entry}, ledger.reserved)
            # Leave at least the last daylight action for a carried order.
            if route is None or route[0] >= turn.day_left:
                continue
            if not any(turn.summon_position_legal(p) and base_distance(base, p) >= 3
                       and p not in (turn.blocked - {scout.pos}) | ledger.reserved
                        and in_summon_cluster(ledger, p)
                       and (p[1] >= entry[1] if entry[1] > max(ys) else p[1] <= entry[1])
                       for p in neighbours(entry)):
                continue
            # Reconsider an armed face before committing any summons. Once
            # spawns are reserved, keep a still-usable gap for those robots.
            candidates.append((bool(ledger.summon_pending_positions) and entry != memory.wall_entry,
                               entry[1] not in clear_sides,
                               *entry_clearance[entry],
                               not incumbent, entry != memory.wall_entry,
                               jobs.get(scout.id, {}).get('kind') == 'build',
                               entry[0] not in xs, route[0], scout.id, entry, scout, route))
    if not candidates:
        # A passing character/robot does not invalidate the unbuilt wall cell.
        entry = open_assault_entry(turn, base, memory.wall_entry)
        if entry is None:
            memory.wall_entry = None
        elif entry in turn.blocked | ledger.reserved:
            scout = next((w for w in turn.workers if w.id == memory.worker_id), None)
            if scout and scout.id not in ledger.used and scout.id not in excluded:
                ledger.used.add(scout.id)
                ledger.reserved.add(scout.pos)
                ledger.explain(scout.id, 'first_day_wall_block_wait', entry=entry,
                               waiting_reason='entry_occupied')
                return scout
        return None
    *_, entry, scout, route = min(candidates, key=lambda row: row[:-2])
    memory.worker_id, memory.post, memory.wall_entry = scout.id, entry, entry
    clearance, turret_in_front = entry_clearance[entry]
    selection = dict(entry=entry, nearest_turret_distance=-clearance if weapon_cells else None,
                     turret_in_front=turret_in_front)
    if route[1] is None:
        if reserve_hold:
            ledger.used.add(scout.id)
        ledger.reserved.add(scout.pos)
        ledger.explain(scout.id, 'first_day_wall_block_hold', **selection)
    elif ledger.add(scout.id, command('move', route[1])):
        ledger.explain(scout.id, 'first_day_wall_block_approach', **selection,
                       route_steps=route[0], departure_tick=departure_tick)
    else:
        return None
    return scout


def yield_wall_entry(turn, memory, nav, ledger):
    """Vacate the occupied gap before robot planning; retain real occupancy."""
    base = enemy_base(turn)
    _sync_scout(turn, memory, base)
    scout = next((w for w in turn.workers if w.id == memory.worker_id), None)
    if (turn.is_day or turn.day != 1 or base is None or scout is None
            or scout.pos != memory.wall_entry or scout.id in ledger.used):
        return None
    if scout.abnormal_state == 'dizzy':
        ledger.used.add(scout.id)
        ledger.reserved.add(scout.pos)
        ledger.explain(scout.id, 'night_scout_dizzy')
        return scout
    lanes = set()
    for robot in turn.summon_robots:
        for cell in base.cells:
            lanes.update(line_cells(robot.pos, cell))
    for cell in base.cells:
        lanes.update(line_cells(scout.pos, cell))
    threats = [r for r in turn.robots if r.id not in turn.summon_robot_ids]
    def risk(point):
        return sum(r.power for r in threats if distance(point, r.pos) <= r.attack_range + 1)
    avoided = nav.memory.blocked(scout.id) if nav.memory else set()
    options = [p for p in neighbours(scout.pos) if turn.inside(p)
               and p not in turn.blocked | ledger.reserved | lanes | avoided]
    if options:
        step = min(options, key=lambda p: (risk(p), base_distance(base, p) < 3,
                   -sum(distance(p, cell) <= 4 for cell in base.cells), p))
        if ledger.add(scout.id, command('move', step)):
            memory.post = step
            ledger.explain(scout.id, 'night_scout_yield_wall_entry', entry=memory.wall_entry,
                           next_risk=risk(step))
            return scout
    ledger.used.add(scout.id)
    ledger.reserved.add(scout.pos)
    ledger.explain(scout.id, 'night_scout_wall_entry_blocked', entry=memory.wall_entry)
    return scout


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
    """Prepare the first night's observer; later days belong to normal work."""
    if turn.day != 1:
        _sync_scout(turn, memory, None)
        return None
    scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger, excluded, jobs)
    if scout:
        return scout
    base = enemy_base(turn)
    if not turn.is_day or base is None or turn.tick < 50:
        return None
    _sync_scout(turn, memory, base)
    jobs = jobs or {}
    candidates = []
    for scout in turn.workers:
        job = jobs.get(scout.id, {})
        if (scout.id in ledger.used or scout.id in excluded
                or scout.id == ledger.income_worker_id or scout.health <= 165
                or any('UpgradeVoucher' in item or item == 'WallFixer' or item in SUMMON_ORDERS
                       for item in scout.backpack)
                or job.get('kind') in ('sell', 'buy', 'use', 'robot_buy', 'build')):
            continue
        routes = [row for row in _routes(turn, memory, nav, ledger, scout, base, ledger.reserved)
                  if row[1][0] + 1 <= turn.day_left]
        if routes:
            goal, route, coverage, base_cells = routes[0]
            candidates.append((scout.id != memory.worker_id, route[0], scout.id,
                               scout, goal, route, coverage, base_cells))
    if not candidates:
        memory.worker_id, memory.post = None, None
        return None
    _, _, _, scout, goal, route, coverage, base_cells = min(candidates)
    # Leave one daylight action at the post; do not spend the entire dusk on
    # an approach that can only finish after nightfall.
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


def act_night_scout(turn, memory, nav, ledger, home_roles=(), excluded=()):
    base = enemy_base(turn)
    _sync_scout(turn, memory, base)
    if turn.is_day or turn.day != 1 or base is None:
        return None
    workers = {h.id: h for h in turn.workers if h.id not in excluded}
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
