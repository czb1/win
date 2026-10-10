"""Early front construction and observed-occupancy handoff; never build at night."""
from .commands import command
from .economy import build, wall_keeps_access
from .economy_plan import front_sites
from .mining import mine
from .model import CHARACTERS, distance, neighbours
from .sabotage import exposed, threats


def missing_front(turn, mem, walls):
    structures = {p for u in (*turn.ours, *turn.enemies)
                  if u.kind not in CHARACTERS for p in u.cells}
    return [p for p in front_sites(turn, walls)
            if p not in structures and p not in mem.build_failures
            and turn.zones.get(p, 'land') == 'land']


def prepare_front(turn, cfg, mem, nav, ledger, walls):
    """Borrow one builder early, leaving the other worker to guns/economy.

    A guard moves away only with a stone-carrying worker already beside the
    site. Construction waits for next turn's observed empty cell; simultaneous
    movement/build ordering is not specified by the competition protocol.
    """
    if not turn.is_day or not turn.station or turn.day_left <= 20:
        mem.front_wall_job.clear()
        return
    missing = missing_front(turn, mem, walls)
    if turn.day > 1:
        missing = [p for p in missing if p in mem.wall_rebuild_levels or mem.wall_hits.get(p)]
    enemies = {u.pos for u in turn.enemies if u.kind in CHARACTERS}
    candidates = [p for p in missing if p not in enemies and not any(
        distance(p, r.pos) <= r.attack_range + 1 for r in turn.robots if turn.threatens_us(r))]
    if not candidates:
        mem.front_wall_job.clear()
        return
    old = mem.front_wall_job
    # Expire an unproductive reservation instead of suppressing work forever.
    if old and turn.round - old.get('started', turn.round) >= 18:
        mem.front_wall_job.clear()
        return
    workers = [h for h in turn.workers if h.id not in ledger.used and h.health > 165]
    options = []
    for h in workers:
        material_steps = 0
        if h.inventory['stone'] < cfg.wall_stones:
            material_steps = min((r[0] for p, kind in turn.zones.items()
                                  if kind == 'stone' and turn.mine_remain.get(p, 1) != 0
                                  and (r := nav.approach(h, [p], ledger.reserved)) is not None),
                                 default=None)
        if material_steps is None:
            continue
        for p in candidates:
            if turn.phase_task and turn.pioneer and p == turn.pioneer.pos:
                continue
            route = nav.approach(h, [p], ledger.reserved)
            if route is None or route[0] + cfg.return_margin + 2 >= turn.day_left:
                continue
            enemy_steps = min((distance(p, e.pos) for e in turn.enemies
                               if e.kind in CHARACTERS), default=100)
            options.append(((p != old.get('target'), h.inventory['stone'] < cfg.wall_stones,
                             enemy_steps, h.id != old.get('worker'), material_steps, route[0],
                             candidates.index(p), h.id), h, p))
    if not options:
        mem.front_wall_job.clear()
        return
    selected = next((item for item in sorted(options, key=lambda item: item[0])
                     if wall_keeps_access(turn, nav, ledger, item[2])), None)
    if selected is None:
        mem.front_wall_job.clear()
        return
    _, worker, target = selected
    if target != old.get('target') or worker.id != old.get('worker'):
        mem.front_wall_job = dict(target=target, worker=worker.id, started=turn.round)
    ready = worker.inventory['stone'] >= cfg.wall_stones
    occupant = next((h for h in turn.characters if h.pos == target), None)
    if occupant and occupant.id != worker.id:
        # Never cancel a running evolution task to clear a blueprint cell.
        if (occupant.id not in ledger.used
                and not (occupant.kind == 'pioneer' and turn.phase_task)):
            if ready and turn.adjacent(worker.pos, target):
                exits = set(neighbours(target)) - ledger.wall_cells - ledger.tower_cells
                route = nav.search(occupant, exits, ledger.reserved)
                if route and route[1] is not None and ledger.add(
                        occupant.id, command('move', route[1])):
                    ledger.explain(occupant.id, 'front_wall_handoff_vacate', target=target,
                                   builder=worker.id)
            elif occupant.kind in ('imp', 'worker'):
                dangers = [t for t in threats(turn, mem.sabotage) if t.catch]
                if occupant.kind != 'imp' or not exposed(occupant.pos, dangers):
                    ledger.used.add(occupant.id)
                    ledger.explain(occupant.id, 'front_wall_hold', target=target, builder=worker.id)
    elif occupant is None and not (ready and turn.adjacent(worker.pos, target)):
        guards = sorted((h for h in turn.characters
                         if h.kind in ('imp', 'worker') and h.id != worker.id
                         and h.id not in ledger.used and h.health > 165),
                        key=lambda h: (h.kind != 'imp', distance(h.pos, target), h.id))
        dangers = [t for t in threats(turn, mem.sabotage) if t.catch]
        for guard in guards:
            route = nav.search(guard, {target}, ledger.reserved)
            if (route and route[1] is not None and route[0] <= (6 if guard.kind == 'imp' else 3)
                    and (guard.kind != 'imp' or not exposed(target, dangers)
                         and not exposed(route[1], dangers))
                    and ledger.add(guard.id, command('move', route[1]))):
                ledger.explain(guard.id, 'front_wall_reserve', target=target, builder=worker.id)
                break
    if ready:
        if occupant and occupant.id == worker.id:
            route = nav.search(worker, set(neighbours(target)) - ledger.wall_cells
                               - ledger.tower_cells, ledger.reserved)
            if route and route[1] is not None:
                ledger.add(worker.id, command('move', route[1]))
        elif target in turn.blocked:
            route = nav.approach(worker, [target], ledger.reserved)
            if route and route[1] is not None:
                ledger.add(worker.id, command('move', route[1]))
            elif route:
                ledger.used.add(worker.id)
                ledger.explain(worker.id, 'front_wall_wait_for_clear', target=target)
        else:
            build(turn, cfg, mem, nav, ledger, worker, [target], lambda _: 'wall', front_claim=True)
    else:
        # Deliver the first stone immediately instead of waiting for a full batch.
        mine(turn, cfg, mem, nav, ledger, worker, want_stone=True,
             stone_goal=cfg.wall_stones)
    if worker.id in ledger.used:
        mem.daytime_jobs.pop(worker.id, None)
        mem.stone_reserves[worker.id] = min(worker.inventory['stone'], len(missing) * cfg.wall_stones)


def stage_front_breach(turn, cfg, mem, nav, ledger, walls, excluded=()):
    """After all hostile robots clear, stage a spare carrier inside a lost wall."""
    if turn.is_day or not turn.station or any(turn.threatens_us(r) for r in turn.robots):
        return
    lost = [p for p in missing_front(turn, mem, walls) if p in mem.wall_rebuild_levels]
    if not lost:
        return
    forbidden = ledger.wall_cells | ledger.tower_cells | set(ledger.operator_posts.values())
    options = []
    for worker in turn.workers:
        if worker.id in ledger.used or worker.id in excluded or worker.health <= 165:
            continue
        if worker.inventory['stone'] < cfg.wall_stones:
            continue
        for target in lost:
            posts = {p for p in neighbours(target) if turn.base_distance(p) < turn.base_distance(target)}
            route = nav.search(worker, posts - forbidden, ledger.reserved)
            if route:
                options.append((route[0], worker.id, target, worker, route))
    if not options:
        return
    _, _, target, worker, route = min(options, key=lambda item: item[:3])
    if route[1] is not None:
        if not ledger.add(worker.id, command('move', route[1])):
            return
    else:
        ledger.used.add(worker.id)
    ledger.explain(worker.id, 'front_breach_stage_until_daylight', target=target)
    mem.front_wall_job = dict(target=target, worker=worker.id, started=turn.round)
