"""Finish one perimeter face, then give its carrier an economic checkpoint."""
import logging

LOG = logging.getLogger(__name__)


def face_sites(sites, anchor):
    """Partition a rectangular blueprint; corners belong to the vertical face.

    Use the full blueprint, never the changing set of missing walls. This also
    gives sparse explicit layouts a stable, disjoint partition.
    """
    sites = set(sites)
    if not sites or anchor not in sites:
        return set()
    left, right = min(x for x, y in sites), max(x for x, y in sites)
    bottom, top = min(y for x, y in sites), max(y for x, y in sites)

    def side(p):
        x, y = p
        return min(((abs(x-left), 'left'), (abs(x-right), 'right'),
                    (abs(y-bottom), 'bottom'), (abs(y-top), 'top')),
                   key=lambda v: (v[0], ('left', 'right', 'bottom', 'top').index(v[1])))[1]
    return {p for p in sites if side(p) == side(anchor)}


def start_batch(turn, mem, ledger, hero, anchor):
    # The day-one front-wall/opening-gun/BOSS allocator keeps its own policy.
    if not turn.is_day or turn.day == 1 or hero.kind != 'worker' or hero.id in mem.wall_batches:
        return
    sites = face_sites(ledger.wall_cells, anchor)
    if sites:
        mem.wall_batches[hero.id] = dict(sites=tuple(sorted(sites)), phase='build',
                                          day=turn.day)
        LOG.info('round=%s worker=%s wall_batch=start sites=%s phase=%s',
                 turn.round, hero.id, sorted(sites), 'build')


def continue_batch(turn, cfg, mem, nav, ledger, hero, towers, walls):
    from .economy import (build, build_options, use_inventory, supplies, buy_supply,
                          wall_keeps_access)
    from .economy_plan import planned_weapons
    from .mining import mine, earn, sale_inventory
    from .worker_jobs import _fits_return, _continue

    batch = mem.wall_batches[hero.id]
    sites = set(batch['sites']) & set(walls)
    if batch['day'] != turn.day or not sites:
        mem.wall_batches.pop(hero.id, None)
        return False

    def accepted():
        note = ledger.notes.setdefault(hero.id, {'reason': 'wall_face_batch', 'conditions': {}})
        note['conditions'].update(wall_face=sorted(sites), wall_phase=batch['phase'])
        return True

    # Real survival emergencies run before this function in resume_daytime_jobs.
    # A held use/buy target stays stable within maintenance, not across faces.
    if batch['phase'] == 'build':
        missing = [p for p in sorted(sites) if p not in turn.blocked
                   and p not in mem.build_failures and _fits_return(turn, cfg, mem, nav, ledger, hero, p)]
        feasible = [o[4] for o in build_options(turn, cfg, mem, nav, ledger, hero,
                                               missing, lambda _: 'wall')
                    if wall_keeps_access(turn, nav, ledger, o[4])]
        if feasible:
            goal = len(feasible) * cfg.wall_stones
            # Gather for this face once, then spend the carried batch before
            # gathering again. A short/daylight-limited batch may be smaller.
            gathering = not batch.get('delivering') or hero.inventory['stone'] < cfg.wall_stones
            if gathering and hero.inventory['stone'] < goal and hero.space:
                if mine(turn, cfg, mem, nav, ledger, hero, want_stone=True,
                        stone_goal=goal, stone_sites=feasible):
                    return accepted()
            if hero.inventory['stone'] >= cfg.wall_stones:
                batch['delivering'] = True
                if build(turn, cfg, mem, nav, ledger, hero, feasible, lambda _: 'wall'):
                    return accepted()
        batch['phase'] = 'maintain'
        mem.daytime_jobs.pop(hero.id, None)
        LOG.info('round=%s worker=%s wall_batch=maintain sites=%s', turn.round, hero.id, sorted(sites))

    if batch['phase'] == 'maintain':
        job = mem.daytime_jobs.get(hero.id, {})
        # Continue only the current paid use. Purchases are recomputed against
        # this face each turn so a completed/blocked target cannot strand a buyer.
        if job.get('kind') == 'use' and job.get('target') in sites and _fits_return(
                turn, cfg, mem, nav, ledger, hero, job['target']):
            ok, _ = _continue(turn, cfg, mem, nav, ledger, hero, job, towers, walls)
            if ok:
                return accepted()
        service_sites = {p for p in sites if _fits_return(turn, cfg, mem, nav, ledger, hero, p)}
        if use_inventory(turn, nav, ledger, hero, mem=mem, target_sites=service_sites):
            return accepted()
        # At most one shopping visit per face. Use what's bought, then earn;
        # reaching L3 everywhere is deliberately not a batch completion gate.
        if not batch.get('purchased'):
            planned = planned_weapons(turn, cfg, mem, towers)
            reserve = sum(w.id < 0 for w in planned) * cfg.weapon_cost
            plan = supplies(turn, cfg, mem, nav, ledger, hero, reserve,
                            planned=planned, bulk=True, target_sites=sites)
            if buy_supply(turn, ledger, hero, plan):
                batch['purchased'] = ledger.commands[str(hero.id)]['action'] == 'buy'
                return accepted()
        batch['phase'] = 'economy'
        mem.daytime_jobs.pop(hero.id, None)
        if mem.wall_repair_worker == hero.id:
            mem.wall_repair_worker = None
            mem.wall_repair_delivering = False
        LOG.info('round=%s worker=%s wall_batch=economy sites=%s', turn.round, hero.id, sorted(sites))

    # A real sale must remain continuous until all saleable ore is sold. The
    # existing sale planner checks travel, each ore's action and return time.
    counts = sale_inventory(turn, mem, hero)
    value = sum(n * turn.prices.get(k, 0) for k, n in counts.items())
    selling = mem.daytime_jobs.get(hero.id, {}).get('kind') == 'sell'
    should_sell = selling or value >= cfg.weapon_cost or ledger.gold < cfg.weapon_cost or not hero.space
    if counts and should_sell and earn(turn, cfg, mem, nav, ledger, hero,
                                      force_sale=True, allow_spare=False):
        return accepted()
    mem.wall_batches.pop(hero.id, None)
    mem.daytime_jobs.pop(hero.id, None)
    LOG.info('round=%s worker=%s wall_batch=release ore_value=%s gold=%s day_left=%s',
             turn.round, hero.id, value, ledger.gold, turn.day_left)
    # Ordinary profitable mining is time bounded; otherwise the normal
    # allocator may start another face or buy supplies in this same turn.
    if turn.tick < min(cfg.economy_rounds, mem.preparation_tick):
        return earn(turn, cfg, mem, nav, ledger, hero,
                    deadline=min(cfg.economy_rounds, mem.preparation_tick), allow_spare=False)
    return False
