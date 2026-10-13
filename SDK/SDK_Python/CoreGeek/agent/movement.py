"""Observed movement feedback and stationary obstacles outside shared vision."""
from collections import deque
from dataclasses import dataclass, field
from .model import WEAPONS, distance, pos
from .vehicle import VehicleMemory


@dataclass
class MovementMemory:
    vehicle: VehicleMemory = field(default_factory=VehicleMemory)
    failures: dict = field(default_factory=dict)
    failure_counts: dict = field(default_factory=dict)
    trails: dict = field(default_factory=dict)
    trail_jobs: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    buildings: dict = field(default_factory=dict)
    looped: set = field(default_factory=set)
    positions: dict = field(default_factory=dict)
    driving_disabled: set = field(default_factory=set)
    driving_day: int = 0

    def observe(self, turn, mem):
        self.vehicle.observe(turn, mem)
        self.looped.clear()
        if self.driving_day != turn.day:
            self.driving_disabled.clear()
            self.driving_day = turn.day
        self.driving_disabled.intersection_update(turn.units)
        self.failures = {k: r for k, r in self.failures.items()
                         if r > turn.round and k[0] in turn.units}
        self.targets = {k: r for k, r in self.targets.items()
                        if r > turn.round and k[0] in turn.units}
        self.trails = {uid: trail for uid, trail in self.trails.items() if uid in turn.units}
        self.trail_jobs = {uid: job for uid, job in self.trail_jobs.items() if uid in turn.units}
        self.failure_counts = {k: value for k, value in self.failure_counts.items()
                               if k[0] in turn.units and turn.round - value[1] < 130}
        consecutive = mem.last_round == turn.round - 1
        for hero in turn.characters:
            cmd = mem.last_commands.get(str(hero.id), {}) if consecutive else {}
            if cmd.get("action") != "move":
                self.trails.pop(hero.id, None)
                self.trail_jobs.pop(hero.id, None)
                continue
            job = mem.daytime_jobs.get(hero.id, {})
            job_key = tuple(job.get(k) for k in ('kind', 'target', 'name', 'ore'))
            if hero.id in self.trail_jobs and self.trail_jobs[hero.id] != job_key:
                # A buy/build reassignment can reverse valid movement. Measure
                # loops within one destination task, without losing collisions.
                self.trails.pop(hero.id, None)
            self.trail_jobs[hero.id] = job_key
            target = pos(cmd["targetPos"][-1])
            if hero.pos != target:
                origin = self.positions.get(hero.id, hero.pos)
                if hero.kind == 'pioneer' and (len(cmd['targetPos']) == 2 or distance(origin, target) == 2):
                    # Repeatedly trying another two-cell landing can strand a
                    # driver. Finish this day's authorized trips one cell at a time.
                    self.driving_disabled.add(hero.id)
                # A collision is local and temporary, never a permanent wall.
                key = hero.id, target
                count = self.failure_counts.get(key, (0, 0))[0] + 1
                self.failure_counts[key] = count, turn.round
                # A teammate may vacate the cell on the very next turn.
                # Escalate only after repeated failures at the same step.
                self.failures[key] = turn.round + 2 ** min(count - 1, 4)
            else:
                self.failure_counts.pop((hero.id, target), None)
                self.failures.pop((hero.id, target), None)
            trail = self.trails.setdefault(hero.id, deque(maxlen=8))
            trail.append(hero.pos)
            if len(trail) == 8 and len(set(trail)) <= 4:
                self.looped.add(hero.id)
                # Release a repeatedly unproductive destination for this actor.
                for p in (mem.mine_targets.get(hero.id), mem.build_targets.get(hero.id),
                          mem.sale_targets.get(hero.id), mem.upgrade_targets.get(hero.id),
                          mem.daytime_jobs.get(hero.id, {}).get('target'),
                          mem.sabotage.targets.get(hero.id, (None, None))[0]):
                    if p is not None:
                        self.targets[hero.id, p] = turn.round + 8
                mem.mine_targets.pop(hero.id, None)
                mem.build_targets.pop(hero.id, None)
                mem.sale_targets.pop(hero.id, None)
                mem.upgrade_targets.pop(hero.id, None)
                mem.sabotage.targets.pop(hero.id, None)
                mem.sabotage.progress.pop(hero.id, None)
                trail.clear()
        self.positions = {hero.id: hero.pos for hero in turn.characters}
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
