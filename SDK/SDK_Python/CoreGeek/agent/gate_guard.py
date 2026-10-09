"""Dusk staging and a night gatekeeper that yields to actual friendly traffic."""
from .commands import command
from .model import HEROES, distance, neighbours, pos
from .navigation import DeadlineExceeded
from .projectiles import wall_gates
from .sabotage import escape, exposed, threats


class GateGuard:
    def __init__(self, turn, nav, ledger, hero, gate, inward, posts, state):
        self.turn, self.nav, self.ledger = turn, nav, ledger
        self.hero, self.gate, self.inward = hero, gate, inward
        self.posts, self.state = posts, state
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
                        or point in original and point != self.hero.pos):
                    continue
                route = self.nav._search(self.hero, {point}, self.ledger.reserved | forbidden)
                if route is None:
                    continue
                # A side step must leave the waiting worker's route open, too.
                landing = route[1] or self.hero.pos
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
                    or self.gate in self.ledger.reserved)
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
    """Recall early enough to be beside the sole tower-facing gap at dusk."""
    gates = wall_gates(turn, walls)
    if len(gates) != 1 or not turn.imps:
        return None
    gate = next(iter(gates))
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
    hero = turn.imps[0]
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
            return None
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
                 yield_until=state.get('yield_until', -1))
    mem.sabotage.gate_states[hero.id] = state
    if returning or not turn.is_day:
        mem.sabotage.targets.pop(hero.id, None)
    mem.sabotage.progress.pop(hero.id, None)
    ledger.used.add(hero.id)  # Suppress the daytime sabotage action for this imp.
    return GateGuard(turn, nav, ledger, hero, gate, inward, posts, state)

