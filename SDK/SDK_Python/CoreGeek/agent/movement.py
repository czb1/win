"""Observed movement feedback and stationary obstacles outside shared vision."""
from collections import deque
from dataclasses import dataclass, field
from .model import WEAPONS, distance, pos


@dataclass
class MovementMemory:
    failures: dict = field(default_factory=dict)
    failure_counts: dict = field(default_factory=dict)
    trails: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    buildings: dict = field(default_factory=dict)
    looped: set = field(default_factory=set)

    def observe(self, turn, mem):
        self.looped.clear()
        self.failures = {k: r for k, r in self.failures.items()
                         if r > turn.round and k[0] in turn.units}
        self.targets = {k: r for k, r in self.targets.items()
                        if r > turn.round and k[0] in turn.units}
        self.trails = {uid: trail for uid, trail in self.trails.items() if uid in turn.units}
        self.failure_counts = {k: value for k, value in self.failure_counts.items()
                               if k[0] in turn.units and turn.round - value[1] < 130}
        consecutive = mem.last_round == turn.round - 1
        for hero in turn.heroes:
            cmd = mem.last_commands.get(str(hero.id), {}) if consecutive else {}
            if cmd.get("action") != "move":
                self.trails.pop(hero.id, None)
                continue
            target = pos(cmd["targetPos"][0])
            if hero.pos != target:
                # A collision is local and temporary, never a permanent wall.
                key = hero.id, target
                count = self.failure_counts.get(key, (0, 0))[0] + 1
                self.failure_counts[key] = count, turn.round
                self.failures[key] = turn.round + 4 * 2 ** min(count - 1, 3)
            else:
                self.failure_counts.pop((hero.id, target), None)
                self.failures.pop((hero.id, target), None)
            trail = self.trails.setdefault(hero.id, deque(maxlen=8))
            trail.append(hero.pos)
            if len(trail) == 8 and len(set(trail)) <= 4:
                self.looped.add(hero.id)
                # Release a repeatedly unproductive destination for this actor.
                for p in (mem.mine_targets.get(hero.id), mem.build_targets.get(hero.id),
                          mem.sale_targets.get(hero.id), mem.upgrade_targets.get(hero.id)):
                    if p is not None:
                        self.targets[hero.id, p] = turn.round + 8
                mem.mine_targets.pop(hero.id, None)
                mem.build_targets.pop(hero.id, None)
                mem.sale_targets.pop(hero.id, None)
                mem.upgrade_targets.pop(hero.id, None)
                trail.clear()
        # Neutral zones are supplied globally by v1.0; never retain exhausted
        # mines as obstacles. Enemy weapons, however, disappear outside vision.
        seen = {r.pos: r for r in turn.enemies if r.kind in WEAPONS}
        observers = [p for r in turn.ours for p in r.cells]
        self.buildings = {p: r for p, r in self.buildings.items()
                          if p in seen or not any(distance(p, q) <= 4 for q in observers)}
        self.buildings.update(seen)
        turn.blocked.update(self.buildings)

    def blocked(self, uid):
        return {p for actor, p in self.failures if actor == uid}

    def avoids(self, uid, target):
        return (uid, target) in self.targets
