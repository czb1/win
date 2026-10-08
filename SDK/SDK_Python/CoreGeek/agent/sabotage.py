"""Enemy-half mining disruption, with survival taking precedence over channeling."""
from dataclasses import dataclass, field
from heapq import heappop, heappush
from .commands import command
from .model import CHARACTERS, distance, neighbours
from .navigation import check_time, DeadlineExceeded

DESTROY_ROUNDS = 4
ENEMY_MEMORY_ROUNDS = 3


@dataclass
class ImpMemory:
    targets: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)
    enemies: dict = field(default_factory=dict)

    def observe(self, turn, mem):
        alive = {h.id for h in turn.imps}
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


def threats(turn, memory):
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


def escape(turn, nav, ledger, hero, dangers):
    """Always decide a legal local escape before any expensive route search."""
    avoided = nav.memory.blocked(hero.id) if nav.memory else set()
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


def retreat(turn, nav, ledger, hero, avoided):
    if turn.station:
        goals = {p for cell in turn.station.cells for p in neighbours(cell)} - turn.station.cells
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
    forbidden = ledger.tower_cells | ledger.wall_cells | set(ledger.operator_posts.values())
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
        if target and not (nav.memory and nav.memory.avoids(hero.id, target[0])):
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
        if (not turn.is_day or turn.day_left < DESTROY_ROUNDS) and retreat(turn, nav, ledger, hero, avoided):
            memory.progress.pop(hero.id, None)
            continue
        ledger.used.add(hero.id)
        ledger.reserved.add(hero.pos)
        ledger.explain(hero.id, "imp_wait_for_safe_enemy_mine", enemy_mines=len(mines),
                       day_left=turn.day_left)
