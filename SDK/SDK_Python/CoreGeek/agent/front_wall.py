"""One opening stone batch and a temporary front guard; release permanently."""
from .commands import command
from .economy import build
from .economy_plan import front_sites
from .mining import mine
from .model import distance, neighbours
from .sabotage import exposed, threats


def finish_front(mem, reason):
    # A terminal marker prevents timeout, death or a new gap restarting work.
    job = mem.front_wall_job
    if job.get('worker') is not None:
        # The ordinary deadline was estimated with the builder temporarily
        # absent. Recompute it with the returned crew instead of retaining a
        # solo-worker cutoff that can leave the empty carrier idle at home.
        # Existing sale/delivery/preparation ownership still wins as usual.
        mem.preparation_tick = 70
    mem.daytime_jobs.pop(job.get('worker'), None)
    if not job.get('guard_released'):
        mem.daytime_jobs.pop(job.get('guard'), None)
    mem.stone_reserves.pop(job.get('worker'), None)
    mem.front_wall_job = {'phase': 'done', 'reason': reason}


def guard_available(turn, hero):
    return (hero.health > 165 and (hero.kind != 'pioneer' or not (
        turn.phase_task or any(t.get('isValid') for t in turn.tasks))))


def start_front(turn, cfg, mem, nav, ledger, sites):
    center = min(sites, key=lambda p: (abs(2 * p[1] - min(q[1] for q in sites)
                                         - max(q[1] for q in sites)), sites.index(p)))
    needed = len(sites) * cfg.wall_stones
    options = []
    for worker in turn.workers:
        if worker.id in ledger.used or worker.health <= 165:
            continue
        if worker.inventory['stone'] + worker.space < needed:
            continue
        route = nav.approach(worker, sites, ledger.reserved)
        if route is None:
            continue
        material = (0 if worker.inventory['stone'] >= needed else min(
            (r[0] for p, kind in turn.zones.items() if kind == 'stone'
             and turn.mine_remain.get(p) != 0 and p not in mem.collect_failures
             and not mem.movement.avoids(worker.id, p)
             and (r := nav.approach(worker, [p], ledger.reserved)) is not None), default=None))
        if material is not None:
            options.append((worker.inventory['stone'] < needed, material, route[0], worker.id))
    if not options:
        finish_front(mem, 'no_material_route')
        return
    worker_id = min(options)[-1]
    kinds = ('imp', 'pioneer', 'worker') if cfg.opening_guard == 'auto' else (cfg.opening_guard,)
    dangers = threats(turn, mem.sabotage)
    guards = []
    for hero in turn.characters:
        if (hero.id == worker_id or hero.id in ledger.used or hero.kind not in kinds
                or not guard_available(turn, hero)):
            continue
        route = nav.search(hero, {center}, ledger.reserved)
        if route and (hero.kind != 'imp' or not exposed(center, dangers)
                      and not exposed(hero.pos, dangers)
                      and (route[1] is None or not exposed(route[1], dangers))):
            guards.append((kinds.index(hero.kind), route[0], hero.id))
    mem.front_wall_job = dict(phase='gather', worker=worker_id,
                              guard=min(guards)[-1] if guards else None,
                              sites=tuple(sites), center=center, started=turn.round,
                              stalled=0, guard_released=False)


def vacate_guard(turn, nav, ledger, guard):
    exits = set(neighbours(guard.pos)) - ledger.wall_cells - ledger.tower_cells
    route = nav.search(guard, exits, ledger.reserved)
    if route and route[1] is not None and ledger.add(guard.id, command('move', route[1])):
        ledger.explain(guard.id, 'opening_front_handoff', target=guard.pos)
        return True
    return False


def reserve_front(turn, mem, nav, ledger, remaining, builder):
    job = mem.front_wall_job
    if job['guard_released']:
        return
    guard = turn.units.get(job['guard'])
    if not guard or not guard_available(turn, guard) or guard.id in ledger.used:
        job['guard_released'] = True
        return
    dangers = threats(turn, mem.sabotage)
    if guard.kind == 'imp' and exposed(guard.pos, dangers):
        # act_imps() executes its normal escape during this same decision.
        job['guard_released'] = True
        return
    # Only supplied visible opponents are actionable. Never chase beyond the
    # front or try to move onto an occupied enemy square.
    enemies = [e for e in turn.enemies if e.kind == 'worker'
               and min(distance(e.pos, p) for p in remaining) <= 4]
    choices = [p for p in remaining if p == guard.pos or p not in turn.blocked]
    if not choices:
        job['guard_released'] = True
        return
    target = min(choices, key=lambda p: (
        min((distance(p, e.pos) for e in enemies), default=0),
        distance(p, job['center']), p != guard.pos, remaining.index(p)))
    # No approaching worker: release when the carrier returns. Under pressure,
    # cover this one square until its own handoff, without borrowing new actors.
    if job['phase'] == 'build' and (
            turn.adjacent(builder.pos, guard.pos) if enemies else
            min(distance(builder.pos, p) for p in remaining) <= 1):
        if guard.pos in remaining:
            vacate_guard(turn, nav, ledger, guard)
        job['guard_released'] = True
        return
    route = nav.search(guard, {target}, ledger.reserved)
    if route is None or guard.kind == 'imp' and (
            exposed(target, dangers) or route[1] is not None and exposed(route[1], dangers)):
        job['guard_released'] = True
        return
    if route[1] is None:
        ledger.used.add(guard.id)
        ledger.explain(guard.id, 'opening_front_hold', target=target,
                       visible_enemy_workers=[e.id for e in enemies])
    elif ledger.add(guard.id, command('move', route[1])):
        ledger.explain(guard.id, 'opening_front_reserve', target=target,
                       visible_enemy_workers=[e.id for e in enemies])


def build_front(turn, cfg, mem, nav, ledger, worker, sites):
    job = mem.front_wall_job
    guard = turn.units.get(job['guard'])
    for target in sites:
        if worker.pos == target:
            return vacate_guard(turn, nav, ledger, worker)
        if guard and guard.pos == target and guard_available(turn, guard):
            route = nav.approach(worker, [target], ledger.reserved)
            if route is None:
                continue
            if route[1] is not None:
                if ledger.add(worker.id, command('move', route[1])):
                    return True
            else:
                if guard.id not in ledger.used:
                    vacate_guard(turn, nav, ledger, guard)
                ledger.used.add(worker.id)
                ledger.explain(worker.id, 'opening_front_wait_for_clear', target=target)
                job['guard_released'] = True
                return False
        # An opening wall may add at most two steps to an unfinished gun's
        # route. Reachability and recall remain mandatory; ordinary work keeps
        # the original zero-detour rule through the default argument.
        elif build(turn, cfg, mem, nav, ledger, worker, [target], lambda _: 'wall',
                   max_gun_detour=2):
            return True
    return False


def prepare_front(turn, cfg, mem, nav, ledger, walls):
    job = mem.front_wall_job
    if job.get('phase') == 'done':
        return
    if turn.day != 1 or not turn.is_day or not turn.station or turn.tick >= 40:
        finish_front(mem, 'opening_window_closed')
        return
    if not job:
        # Both protocol origins and an initial observation numbered 1 work.
        if turn.tick > 1:
            finish_front(mem, 'opening_missed')
            return
        owned = {w.pos for w in turn.ours if w.kind == 'wall'}
        sites = [p for p in front_sites(turn, walls) if p not in owned]
        if not sites:
            finish_front(mem, 'front_complete')
            return
        start_front(turn, cfg, mem, nav, ledger, sites)
        job = mem.front_wall_job
        if job['phase'] == 'done':
            return
    owned = {w.pos for w in turn.ours if w.kind == 'wall'}
    remaining = [p for p in job['sites'] if p not in owned]
    if not remaining:
        finish_front(mem, 'front_complete')
        return
    worker = turn.units.get(job['worker'])
    if not worker or worker.health <= 165 or worker.id in ledger.used:
        finish_front(mem, 'builder_unavailable')
        return
    if any(turn.threatens_us(r) and min(distance(r.pos, p) for p in remaining)
           <= r.attack_range + 1 for r in turn.robots):
        finish_front(mem, 'front_unsafe')
        return
    goal = len(remaining) * cfg.wall_stones
    if worker.inventory['stone'] >= goal:
        job['phase'] = 'build'
    acted = False
    if job['phase'] == 'gather':
        acted = mine(turn, cfg, mem, nav, ledger, worker, want_stone=True, stone_goal=goal)
    else:
        if worker.inventory['stone'] < cfg.wall_stones:
            finish_front(mem, 'batch_spent')
            return
        # Use the normal connected-wall rule; do not seed isolated segments.
        sites = sorted(remaining, key=lambda p: (distance(p, job['center']), remaining.index(p)))
        acted = build_front(turn, cfg, mem, nav, ledger, worker, sites)
    job['stalled'] = 0 if acted else job['stalled'] + 1
    if (not acted and worker.id not in ledger.used) or job['stalled'] >= 3:
        if str(worker.id) not in ledger.commands:
            ledger.used.discard(worker.id)
        finish_front(mem, 'opening_blocked')
        return
    mem.daytime_jobs.pop(worker.id, None)
    mem.stone_reserves[worker.id] = min(worker.inventory['stone'], goal)
    if acted:
        reserve_front(turn, mem, nav, ledger, remaining, worker)
