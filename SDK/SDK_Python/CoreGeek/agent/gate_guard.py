"""Dusk staging and a night gatekeeper that yields to actual friendly traffic."""
from .commands import command
from .model import HEROES, distance, neighbours, pos
from .navigation import DeadlineExceeded
from .projectiles import wall_gates
from .sabotage import escape, exposed, threats


class GateGuard:
    def __init__(self, turn, nav, ledger, hero, gate, inward, posts, state, catches=()):
        self.turn, self.nav, self.ledger = turn, nav, ledger
        self.hero, self.gate, self.inward = hero, gate, inward
        self.posts, self.state = posts, state
        self.catches = catches
        self.initial_blocked = set(turn.blocked)
        self.requests = {}

    def observe_search(self, helper, goals, reserved, route):
        """Record a blocked real search, without changing its result or legality.

        Retry only heroes and only the imp's current occupied cell. Synthetic
        danger masks and other occupied cells must never be removed to make
        an unsafe mining trip appear reachable.
        """
        if (route is not None
                or helper.kind not in HEROES or helper.id in self.ledger.used
                or not self.turn.blocked <= self.initial_blocked
                or self.hero.pos in reserved):
            return
        original = self.turn.blocked
        try:
            self.turn.blocked = original - {self.hero.pos}
            freed = self.nav._search(helper, goals, reserved)
        finally:
            self.turn.blocked = original
        if freed is not None and freed[0] > 0:
            self.requests.setdefault(helper.id, set()).update(goals)

    def _traffic(self):
        traffic = {}
        for helper in self.turn.heroes:
            action = self.ledger.commands.get(str(helper.id), {})
            target = (pos(action['targetPos'][0]) if action.get('action') == 'move' else None)
            # An accepted collection/use/attack is not a request to pass.
            if helper.id in self.ledger.used and target is None:
                continue
            if helper.pos == self.gate or target == self.gate:
                traffic[helper.id] = self.requests.get(helper.id, set())
            elif helper.id in self.requests and (target is None
                    or distance(target, self.gate) < distance(helper.pos, self.gate)):
                traffic[helper.id] = self.requests[helper.id]
        return traffic

    def _stage(self, traffic):
        options = []
        original = self.turn.blocked
        forbidden = (self.ledger.tower_cells | self.ledger.wall_cells
                     | set(self.ledger.operator_posts.values()))
        points = list(self.posts)
        if self.state.get('post') is not None:
            points.append(self.state['post'])
        if traffic:
            # A normal waiting post can itself become a narrow aisle when
            # mines/NPCs constrain the outside route. Clear that aisle too.
            points.extend(p for p in neighbours(self.hero.pos)
                          if ((p[0] - self.gate[0]) * self.inward[0]
                              + (p[1] - self.gate[1]) * self.inward[1]) < 0)
        try:
            for point in dict.fromkeys(points):
                if (not self.turn.inside(point) or point in forbidden or point in self.ledger.reserved
                        or point in original and point != self.hero.pos
                        or self.state.get('traffic_only') and exposed(point, self.catches)):
                    continue
                route = self.nav._search(self.hero, {point}, self.ledger.reserved | forbidden)
                if route is None:
                    continue
                # A side step must leave the waiting worker's route open, too.
                landing = route[1] or self.hero.pos
                if self.state.get('traffic_only') and exposed(landing, self.catches):
                    continue
                self.turn.blocked = (original - {self.hero.pos}) | {landing}
                if any(goals and self.nav._search(self.turn.units[uid], goals,
                                                self.ledger.reserved) is None
                       for uid, goals in traffic.items()):
                    self.turn.blocked = original
                    continue
                self.turn.blocked = original
                risk = sum(r.power for r in self.turn.robots
                           if distance(point, r.pos) <= r.attack_range)
                preferred = point in self.posts or point == self.state.get('post')
                options.append((route[0], not preferred, risk,
                                point != self.state.get('post'), point, route))
        finally:
            self.turn.blocked = original
        if not options:
            return None
        _, _, _, _, point, route = min(options)
        self.state['post'] = point
        return route

    def _hold(self, reason, **facts):
        self.ledger.used.add(self.hero.id)
        self.ledger.explain(self.hero.id, reason, gate=self.gate, **facts)

    def finish(self):
        self.ledger.used.discard(self.hero.id)
        traffic = self._traffic()
        if traffic:
            self.state['yield_until'] = self.turn.round + 2
        yielding = (bool(traffic) or self.state.get('yield_until', -1) >= self.turn.round
                    or self.gate in self.ledger.reserved or self.state.get('traffic_only'))
        reason = ('imp_daytime_gate_yield' if self.turn.is_day and not self.state.get('recalling') else
                  'imp_dusk_staging' if self.turn.is_day else
                  'imp_yielding_gate' if yielding else 'imp_guarding_gate')
        try:
            route = (self._stage(traffic) if self.turn.is_day or yielding else
                     self.nav._search(self.hero, {self.gate}, self.ledger.reserved
                                      | set(self.ledger.operator_posts.values())))
            # An occupied gate is never entered or exchanged with its occupant.
            if route is None and not self.turn.is_day and not yielding:
                route = self._stage(traffic)
            if route is not None and route[1] is not None:
                if self.ledger.add(self.hero.id, command('move', route[1])):
                    self.ledger.explain(self.hero.id, reason, gate=self.gate,
                                        teammates=sorted(traffic), return_steps=route[0],
                                        yield_until=self.state.get('yield_until'))
                    return
        except DeadlineExceeded:
            # Traffic clearance still gets a cheap legal side step if the
            # global search budget expires after the worker requests arrived.
            if self.hero.pos == self.gate and (self.turn.is_day or yielding):
                for point in self.posts:
                    if self.state.get('traffic_only') and exposed(point, self.catches):
                        continue
                    if point not in self.ledger.operator_posts.values() and self.ledger.add(
                            self.hero.id, command('move', point)):
                        self.state['post'] = point
                        self.ledger.explain(self.hero.id, reason, gate=self.gate,
                                            teammates=sorted(traffic), budget_fallback=True)
                        return
        self._hold(reason, teammates=sorted(traffic),
                   at_gate=self.hero.pos == self.gate,
                   yield_until=self.state.get('yield_until'))


def prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls):
    """Stage for a completed perimeter; guard only while a local wave remains."""
    gates = wall_gates(turn, walls)
    if not turn.imps:
        return None
    hero = turn.imps[0]
    built = {u.pos for u in turn.ours if u.kind == 'wall'}
    # The blueprint describes the intended doorway, not the observed holes.
    # Dead/missing walls and a wall filling the doorway invalidate this job.
    if len(gates) != 1 or not set(walls) <= built or gates & built:
        mem.sabotage.gate_states.pop(hero.id, None)
        return None
    gate = next(iter(gates))
    quiet_night = not turn.is_day and not any(
            robot.id not in turn.summon_robot_ids
            and min(distance(gate, robot.pos), turn.base_distance(robot.pos))
            <= max(cfg.task_danger_radius, robot.attack_range + 2)
            for robot in turn.robots)
    xs, ys = zip(*walls)
    if gate[0] == min(xs):
        inward = (1, 0)
    elif gate[0] == max(xs):
        inward = (-1, 0)
    elif gate[1] == min(ys):
        inward = (0, 1)
    else:
        inward = (0, -1)
    facing = gate[0] + inward[0], gate[1] + inward[1]
    if facing not in towers:
        return None  # Explicit layouts are authoritative; never invent a gate.
    posts = [(gate[0] - inward[0] + sign * inward[1],
              gate[1] - inward[1] + sign * inward[0]) for sign in (-1, 1)]
    posts = [p for p in posts if turn.inside(p)]
    if not posts:
        return None
    # With a smaller sabotage region there may be no target. An idle imp at
    # the doorway must still use the shared traffic clearance, without being
    # recalled from elsewhere or holding the gate on a quiet night.
    traffic_only = ((turn.is_day or quiet_night) and hero.pos in {gate, *posts}
                    and not any(turn.enemy_mine(p) for p in turn.zones))
    if quiet_night and not traffic_only:
        # Keep any valid mine target and channel progress when releasing guard.
        mem.sabotage.gate_states.pop(hero.id, None)
        return None
    state = mem.sabotage.gate_states.get(hero.id, {})
    returning = state.get('day') == turn.day and state.get('recalling', False)
    yielding = state.get('day') == turn.day and state.get('yield_until', -1) >= turn.round
    if turn.is_day and not returning and not yielding:
        original = turn.blocked
        try:
            # Estimate future friendly congestion only. Actual moves below
            # continue to respect the observed occupancy and reservations.
            turn.blocked = original - {h.pos for h in turn.characters}
            route = nav.search(hero, set(posts), ledger.tower_cells | ledger.wall_cells)
        finally:
            turn.blocked = original
        # Standing in the gate is still daytime work, not an implicit recall.
        # Otherwise staging and the mine route can alternate across the gap.
        if turn.day_left > (route[0] if route is not None else 0) + cfg.return_margin:
            if not traffic_only:
                if state.get('traffic_only'):
                    mem.sabotage.gate_states.pop(hero.id, None)
                return None
        else:
            returning = True
    catches = [t for t in threats(turn, mem.sabotage) if t.catch]
    if exposed(hero.pos, catches):
        # Robot damage is intentional while guarding. Catching is instant
        # death and grants the opponent gold, so retain the existing escape.
        mem.sabotage.targets.pop(hero.id, None)
        mem.sabotage.progress.pop(hero.id, None)
        escape(turn, nav, ledger, hero, catches)
        return None
    state.update(day=turn.day, gate=gate, recalling=returning,
                 traffic_only=traffic_only,
                 yield_until=state.get('yield_until', -1))
    mem.sabotage.gate_states[hero.id] = state
    if returning or not turn.is_day:
        mem.sabotage.targets.pop(hero.id, None)
    mem.sabotage.progress.pop(hero.id, None)
    ledger.used.add(hero.id)  # Suppress the daytime sabotage action for this imp.
    return GateGuard(turn, nav, ledger, hero, gate, inward, posts, state, catches)

