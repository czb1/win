"""Enemy-corner mining disruption, with survival taking precedence over channeling."""
from dataclasses import dataclass, field
from heapq import heappop, heappush
from .commands import command
from .model import CHARACTERS, WEAPONS, distance, neighbours
from .navigation import check_time, DeadlineExceeded

DESTROY_ROUNDS = 4
ENEMY_MEMORY_ROUNDS = 3


@dataclass
class ImpMemory:
    targets: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)
    enemies: dict = field(default_factory=dict)
    gate_states: dict = field(default_factory=dict)
    night_wave: dict = field(default_factory=dict)
    scout: dict = field(default_factory=dict)

    def observe(self, turn, mem):
        alive = {h.id for h in turn.imps}
        self.gate_states = {uid: state for uid, state in self.gate_states.items()
                            if uid in alive and state['day'] == turn.day}
        self.targets = {uid: target for uid, target in self.targets.items()
                        if uid in alive and turn.enemy_mine(target[0])
                        and turn.zones.get(target[0]) == target[1]}
        results = turn.raw.get("lastRoundRoleActionResults") or {}
        consecutive = mem.last_round == turn.round - 1
        confirmed = {}
        for hero in turn.imps:
            previous = mem.last_commands.get(str(hero.id), {}) if consecutive else {}
            target = self.targets.get(hero.id)
            if (target and previous.get("action") == "destroy"
                    and previous.get("targetPos") == [{"x": target[0][0], "y": target[0][1]}]
                    and self.positions.get(hero.id) == hero.pos
                    and results.get(str(hero.id), results.get(hero.id)) is True):
                confirmed[hero.id] = min(DESTROY_ROUNDS, self.progress.get(hero.id, 0) + 1)
        # An absent/illegal action, skipped round, changed ore, movement or death
        # cannot carry channel progress. Legality alone never proves destruction.
        self.progress = confirmed
        self.positions = {h.id: h.pos for h in turn.imps}
        dead = {int(r["id"]) for r in turn.raw.get("teamEnemy", {}).get("roles", [])
                if int(r["health"]) <= 0}
        if turn.is_day and turn.tick == 0:
            self.enemies.clear()  # Opponents may have respawned at their base.
        self.enemies = {uid: seen for uid, seen in self.enemies.items()
                        if uid not in dead and 0 <= turn.round - seen[1] <= ENEMY_MEMORY_ROUNDS}
        self.enemies.update({u.id: (u, turn.round) for u in turn.enemies if u.kind in CHARACTERS})


@dataclass(frozen=True)
class Threat:
    position: tuple
    reach: int
    speed: int
    damage: int
    catch: bool = False


def threats(turn, memory, friendly_robot_ids=()):
    result = []
    for enemy, seen_round in memory.enemies.values():
        speed = 2 if enemy.is_driving and turn.is_day else 1
        age = max(0, turn.round - seen_round)
        past_speed = 2 if enemy.is_driving else 1
        result.append(Threat(enemy.pos, 1 + age * past_speed, speed, 0, True))
    # RobotRole does not promise attackRange/attackPower fields. All four
    # documented robot types have range 3; targetTeam is NOT ownership and does
    # not make an opponent-bound robot safe for an imp blocking its route.
    for robot in turn.robots:
        if robot.id in friendly_robot_ids:
            continue
        power = {"smallRobot": 5, "middleRobot": 10, "largeRobot": 20, "bossRobot": 40}
        result.append(Threat(robot.pos, max(3, robot.attack_range), 1,
                             max(1, robot.power or power.get(robot.kind, 40))))
    return result


def danger_cells(turn, dangers, horizon, deadline):
    cells = set()
    for threat in dangers:
        check_time(deadline)
        radius = threat.reach + threat.speed * horizon
        cells.update((x, y)
                     for x in range(max(0, threat.position[0] - radius),
                                    min(turn.width, threat.position[0] + radius + 1))
                     for y in range(max(0, threat.position[1] - radius),
                                    min(turn.height, threat.position[1] + radius + 1)))
    return cells


def exposed(point, dangers, horizon=1):
    return any(distance(point, t.position) <= t.reach + t.speed * horizon for t in dangers)


def escape_route(turn, nav, ledger, hero, dangers, avoided):
    """Find a way around obstacles when one move cannot leave the danger area."""
    blocked = (turn.blocked | ledger.reserved | avoided) - {hero.pos}
    queue = [((0, 0, 0), hero.pos, None)]
    costs = {hero.pos: (0, 0, 0)}
    visited = 0
    while queue:
        cost, point, first = heappop(queue)
        if costs.get(point) != cost:
            continue
        if visited % 32 == 0:
            check_time(nav.deadline)
        visited += 1
        if first is not None and not exposed(point, dangers):
            return first
        for step in neighbours(point):
            if not turn.inside(step) or step in blocked:
                continue
            nearby = [t for t in dangers if distance(step, t.position) <= t.reach + t.speed]
            next_cost = (cost[0] + sum(t.catch for t in nearby),
                         cost[1] + sum(t.damage for t in nearby), cost[2] + 1)
            if step not in costs or next_cost < costs[step]:
                costs[step] = next_cost
                heappush(queue, (next_cost, step, first or step))
    return None


def escape(turn, nav, ledger, hero, dangers, forbidden=()):
    """Always decide a legal local escape before any expensive route search."""
    avoided = (nav.memory.blocked(hero.id) if nav.memory else set()) | set(forbidden)
    options = [hero.pos] + [p for p in neighbours(hero.pos)
                            if turn.inside(p) and p not in turn.blocked
                            and p not in ledger.reserved and p not in avoided]

    def risk(point):
        nearby = [t for t in dangers
                  if distance(point, t.position) <= t.reach + t.speed]
        clearance = min((distance(point, t.position) - t.reach - t.speed for t in dangers),
                        default=0)
        # Catch is instant death. After eliminating it, minimize potential robot
        # damage, then maximize distance from the closest threat envelope.
        return (sum(t.catch for t in nearby), sum(t.damage for t in nearby),
                -clearance, turn.base_distance(point), point != hero.pos, point)

    destination = min(options, key=risk)
    if exposed(destination, dangers):
        try:
            destination = escape_route(turn, nav, ledger, hero, dangers, avoided) or destination
        except DeadlineExceeded:
            pass  # Keep the cheap local decision when no search time remains.
    safe = not exposed(destination, dangers)
    if destination != hero.pos:
        ledger.add(hero.id, command("move", destination))
    else:
        ledger.used.add(hero.id)
    ledger.explain(hero.id, "imp_escape" if destination != hero.pos else "imp_no_safe_escape",
                   destination=destination, safe_exit=safe, threats=len(dangers))


def sync_imp_scout(turn, mem):
    """Keep one imp on a confirmed daily offense until that night's end."""
    from .robot_assault import enemy_base
    base = enemy_base(turn)
    plan = mem.daily_pioneer_offense
    summons = getattr(mem, 'robot_summon_state', {}) or {}
    committed = (summons.get('day') == turn.day and summons.get('count', 0) > 0
                 or bool(turn.summon_robots) and not turn.is_day)
    planned = (plan.get('attack_day') == turn.day and turn.pioneer is not None
               and plan.get('phase') in ('shop', 'robots', 'return'))
    scout = mem.sabotage.scout
    if turn.day < 2 or not turn.imps or base is None or not (planned or committed):
        scout.clear()
        return False
    hero = turn.imps[0]  # The same imp selected for the existing gate duty.
    identity = dict(day=turn.day, hero_id=hero.id, base=(base.id, base.pos))
    if any(scout.get(key) != value for key, value in identity.items()):
        scout.clear()
        scout.update(identity, post=None)
    mem.sabotage.gate_states.pop(hero.id, None)
    mem.sabotage.targets.pop(hero.id, None)
    mem.sabotage.progress.pop(hero.id, None)
    return True


def act_imp_scout(turn, memory, nav, ledger):
    """Observe the assault from a safe post without taking a worker's role."""
    from .model import pos
    from .projectiles import line_cells
    from .robot_assault import base_distance, enemy_base
    from .scouting import _observation_points

    state = memory.scout
    hero = next((h for h in turn.imps if h.id == state.get('hero_id')), None)
    base = enemy_base(turn)
    if (hero is None or hero.id in ledger.used or state.get('day') != turn.day
            or base is None):
        return
    dangers = threats(turn, memory, turn.summon_robot_ids)
    lanes = set(ledger.summon_pending_positions)
    for robot in turn.summon_robots:
        action = ledger.commands.get(str(robot.id), {})
        targets = action.get('targetPos', [])
        aim = pos(targets[0]) if action.get('action') == 'attack' and targets else min(
            base.cells, key=lambda p: (distance(robot.pos, p), p))
        lanes.update(line_cells(robot.pos, aim))
        if action.get('action') == 'move':
            lanes.update(pos(p) for p in targets)
    forbidden = lanes | ledger.tower_cells | set(ledger.operator_posts.values())
    if hero.abnormal_state == 'dizzy':
        ledger.used.add(hero.id)
        ledger.reserved.add(hero.pos)
        ledger.explain(hero.id, 'imp_scout_dizzy')
        return
    if exposed(hero.pos, dangers):
        catches = {p for p in neighbours(hero.pos)
                   if any(t.catch and distance(p, t.position) <= t.reach for t in dangers)}
        escape(turn, nav, ledger, hero, dangers, forbidden | catches)
        return
    routes = []
    try:
        reserved = ledger.reserved | forbidden | danger_cells(turn, dangers, 1, nav.deadline)
        weapons = {u.id: u for u in (*ledger.robot_known_enemies, *turn.enemies)
                   if u.kind in WEAPONS}
        points = _observation_points(turn, base)
        for point in ledger.summon_pending_positions:
            target = min(base.cells, key=lambda p: (distance(point, p), p))
            points.update({p: 4 for p in line_cells(point, target) if base_distance(base, p) <= 4})
        xs, ys = zip(*base.cells)
        goals = {(x, y) for x in range(max(0, min(xs)-4), min(turn.width, max(xs)+5))
                 for y in range(max(0, min(ys)-4), min(turn.height, max(ys)+5))
                 if 3 <= base_distance(base, (x, y)) <= 4
                 and (x, y) not in (turn.blocked - {hero.pos}) | reserved}
        for goal in sorted(goals):
            route = nav.search(hero, {goal}, reserved)
            if route is not None:
                coverage = sum(weight for p, weight in points.items() if distance(goal, p) <= 4)
                base_cells = sum(distance(goal, p) <= 4 for p in base.cells)
                routes.append((goal, route, coverage, base_cells))
        routes.sort(key=lambda row: (
            -row[3],
            -sum(distance(row[0], u.pos) <= 4 for u in weapons.values()),
            -(row[2] + (2 if row[0] == state.get('post') else 0)) / (4 + row[1][0]),
            row[1][0], row[0]))
    except DeadlineExceeded:
        pass  # Still retain the role and a cheap legal lane-clearance step.
    if routes:
        goal, route, coverage, base_cells = routes[0]
        state['post'] = goal
        if route[1] is not None and ledger.add(hero.id, command('move', route[1])):
            ledger.explain(hero.id, 'imp_scout_approach', post=goal, coverage=coverage,
                           post_base_cells=base_cells, route_steps=route[0])
            return
        if route[1] is None:
            ledger.used.add(hero.id)
            ledger.reserved.add(hero.pos)
            ledger.explain(hero.id, 'imp_scout_hold', post=goal, coverage=coverage,
                           post_base_cells=base_cells)
            return
    if hero.pos in lanes:
        avoided = nav.memory.blocked(hero.id) if nav.memory else set()
        options = [p for p in neighbours(hero.pos) if turn.inside(p)
                   and p not in turn.blocked | ledger.reserved | forbidden | avoided
                   and not exposed(p, dangers)]
        if options:
            step = min(options, key=lambda p: (abs(base_distance(base, p) - 3), p))
            if ledger.add(hero.id, command('move', step)):
                state['post'] = None
                ledger.explain(hero.id, 'imp_scout_yield', destination=step)
                return
    ledger.used.add(hero.id)
    ledger.reserved.add(hero.pos)
    ledger.explain(hero.id, 'imp_scout_route_blocked', post=state.get('post'))


def rear_waiting_posts(turn, walls=(), gate=None):
    """Wait behind the base with an empty tile between the imp and the wall."""
    if not turn.station:
        return []
    walls = set(walls) | {w.pos for w in turn.ours if w.kind == 'wall'}
    xs, ys = zip(*turn.station.cells)
    outward = -1 if min(xs) + max(xs) < turn.width - 1 else 1
    rear = (min if outward < 0 else max)([*xs, *(p[0] for p in walls)])
    middle = gate[1] if gate else (min(ys) + max(ys)) // 2
    points = [(rear + outward * back, middle + side)
              for back in (2, 3) for side in (-2, 2, -3, 3, -4, 4)]
    legal = [p for p in points if turn.inside(p)
             and all(distance(p, w) >= 2 for w in walls)
             and p not in turn.station.cells]
    if not legal:
        # Near a map edge, stand beyond a rear corner rather than against the
        # wall. The same two-cell clearance still applies in both directions.
        edge_ys = [*ys, *(p[1] for p in walls)]
        legal = [(rear + outward, y) for y in (min(edge_ys) - 2, max(edge_ys) + 2)
                 if turn.inside((rear + outward, y))
                 and all(distance((rear + outward, y), w) >= 2 for w in walls)]
    return legal


def retreat(turn, nav, ledger, hero, avoided):
    if turn.station:
        goals = set(rear_waiting_posts(turn, ledger.wall_cells))
        route = nav.search(hero, goals - avoided, ledger.reserved | avoided)
        if route and route[1] is not None and ledger.add(hero.id, command("move", route[1])):
            ledger.explain(hero.id, "imp_safe_retreat", threats_avoided=len(avoided))
            return True
    return False


def act_imps(turn, memory, nav, ledger):
    if not turn.imps:
        return
    dangers = threats(turn, memory)
    # Apply the cheap survival decision first, including when the shared search
    # budget is about to expire. Do not finish a channel in an unsafe position.
    for hero in turn.imps:
        if hero.id not in ledger.used and exposed(hero.pos, dangers):
            memory.targets.pop(hero.id, None)
            memory.progress.pop(hero.id, None)
            escape(turn, nav, ledger, hero, dangers)
    if all(h.id in ledger.used for h in turn.imps):
        return
    immediate = danger_cells(turn, dangers, 1, nav.deadline)
    # Broken wall blueprints are passageways at night, when construction is
    # unavailable. Keep actual buildings blocked through turn.blocked.
    forbidden = (ledger.tower_cells | (ledger.wall_cells if turn.is_day else set())
                 | set(ledger.operator_posts.values()))
    avoided = immediate | forbidden
    channel_danger = {DESTROY_ROUNDS: danger_cells(turn, dangers, DESTROY_ROUNDS, nav.deadline)}
    mines = sorted(p for p in turn.zones if turn.enemy_mine(p))
    for hero in turn.imps:
        if hero.id in ledger.used:
            continue
        check_time(nav.deadline)
        target = memory.targets.get(hero.id)
        remaining = max(1, DESTROY_ROUNDS - memory.progress.get(hero.id, 0))

        def route_to(mine, horizon):
            if horizon not in channel_danger:
                channel_danger[horizon] = danger_cells(turn, dangers, horizon, nav.deadline)
            goals = set(neighbours(mine)) - avoided - channel_danger[horizon]
            return nav.search(hero, goals, ledger.reserved | avoided)

        choice = None
        if (target and turn.enemy_mine(target[0])
                and not (nav.memory and nav.memory.avoids(hero.id, target[0]))):
            route = route_to(target[0], remaining if turn.adjacent(hero.pos, target[0]) else DESTROY_ROUNDS)
            if route is not None:
                choice = target[0], route
        if choice is None:
            memory.targets.pop(hero.id, None)
            memory.progress.pop(hero.id, None)
            remaining = DESTROY_ROUNDS
            candidates = []
            for mine in mines:
                if nav.memory and nav.memory.avoids(hero.id, mine):
                    continue
                route = route_to(mine, DESTROY_ROUNDS)
                if route is not None:
                    # Prefer a short safe trip, breaking ties with more ore denied.
                    remain = turn.mine_remain.get(mine)
                    candidates.append((route[0], -(remain if remain is not None else 10), mine, route))
            if candidates:
                _, _, mine, route = min(candidates)
                choice = mine, route
        if choice:
            mine, route = choice
            if route[1] is not None:
                if ledger.add(hero.id, command("move", route[1])):
                    memory.targets[hero.id] = mine, turn.zones[mine]
                    ledger.explain(hero.id, "imp_approach_enemy_mine", mine=mine, steps=route[0])
                    continue
            elif not turn.is_day or turn.day_left >= remaining:
                if ledger.add(hero.id, command("destroy", mine)):
                    memory.targets[hero.id] = mine, turn.zones[mine]
                    ledger.explain(hero.id, "imp_destroy_enemy_mine", mine=mine,
                                   confirmed_rounds=memory.progress.get(hero.id, 0), remaining=remaining)
                    continue
        # Revalidate each turn rather than forcing an unsafe or illegal action.
        near_home = bool(turn.station and (turn.base_distance(hero.pos) <= 2
                         or any(distance(hero.pos, p) <= 1 for p in ledger.wall_cells)))
        if (not turn.is_day or turn.day_left < DESTROY_ROUNDS or near_home) and retreat(
                turn, nav, ledger, hero, avoided):
            memory.progress.pop(hero.id, None)
            continue
        ledger.used.add(hero.id)
        ledger.reserved.add(hero.pos)
        ledger.explain(hero.id, "imp_wait_for_safe_enemy_mine", enemy_mines=len(mines),
                       day_left=turn.day_left)

