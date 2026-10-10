"""Continue accepted daytime work before ordinary per-turn role allocation."""
import logging

from .mining import mine, spare_mine, earn, sale_inventory, return_destination
from .economy_plan import planned_weapons, via, development_pending
from .market import cashout_ores, preferred_stock

LOG = logging.getLogger(__name__)


def save_daytime_jobs(turn, mem, ledger):
    # Submitted work, including a collection paused by one bounded catch,
    # survives. Emergencies, recall, failed continuation and night release it.
    mem.daytime_jobs = dict(ledger.work_jobs) if turn.is_day else {}
    # A remembered mining preference must not look like an active mine claim
    # after this worker actually starts shopping, delivering or returning.
    if turn.is_day:
        mem.mine_targets = {uid: p for uid, p in mem.mine_targets.items()
                            if uid in ledger.mine_claims}


def _fits_return(turn, cfg, mem, nav, ledger, hero, target):
    home, exact = return_destination(turn, nav, ledger, hero)
    if turn.day >= 4 and hero.id == mem.wall_watch_id:
        from .wall_watch import geometry
        inside, _ = geometry(turn, ledger.wall_cells)
        post = getattr(ledger, 'daytime_gunner_post', mem.gunner_post)
        home, exact = inside - set(ledger.operator_posts.values()) - {post}, True
    if not home:
        return False
    trip = via(nav, hero, [[target], home], ledger.reserved, final_exact=exact,
               future_return=turn.tick < cfg.economy_rounds)
    return trip is not None and trip + 1 + cfg.return_margin <= turn.day_left


def _continue(turn, cfg, mem, nav, ledger, hero, job, towers, walls):
    from .economy import buy_supply, supplies, use_inventory, build, batch_sale_ready

    kind, target = job['kind'], job['target']
    if kind in ('mine', 'spare') and not job.get('want_stone'):
        if (turn.tick >= min(cfg.economy_rounds, mem.preparation_tick)
                and development_pending(turn, cfg, mem, towers, walls)):
            return False, 'development_phase_started'
        if cashout_ores(turn, cfg, mem, hero, sale_inventory(turn, mem, hero)):
            ok = earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)
            return ok, 'news_cashout_or_return_deadline'
        preferred = preferred_stock(turn, cfg, mem, ledger, hero)
        if preferred and turn.zones.get(target) not in preferred:
            return False, 'news_stock_replan'
    if target is not None and mem.movement.avoids(hero.id, target):
        return False, 'movement_retry_cooldown'
    if kind == 'sell':
        if not sale_inventory(turn, mem, hero):
            return False, 'sale_complete'
        ok = earn(turn, cfg, mem, nav, ledger, hero, force_sale=True,
                  allow_spare=False, vendor_only=target)
    elif kind == 'buy':
        planned = planned_weapons(turn, cfg, mem, towers)
        plan = supplies(turn, cfg, mem, nav, ledger, hero,
                        sum(w.id < 0 for w in planned) * cfg.weapon_cost,
                        planned=planned, bulk=True, item_only=job['name'],
                        shop_only=target, quantity=job['quantity'])
        ok = buy_supply(turn, ledger, hero, plan)
        if ok:
            mem.supply_worker = hero.id
    elif kind == 'robot_buy':
        from .brain import summon_best_robot
        from .spending import plan_day_spending
        _, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, towers)
        ok = summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls,
                               excluded={h.id for h in turn.heroes if h.id != hero.id},
                               reserve=reserve, item_only=job['name'], shop_only=target,
                               quantity_limit=job['quantity'])
    elif kind == 'use':
        ok = use_inventory(turn, nav, ledger, hero, mem=mem,
                           target_only=target, name_only=job['name'])
    elif kind == 'build':
        name = job['name']
        if target not in (walls if name == 'wall' else towers) or not _fits_return(
                turn, cfg, mem, nav, ledger, hero, target):
            return False, 'build_site_or_return_deadline'
        if name == 'wall' and hero.inventory['stone'] < cfg.wall_stones:
            return False, 'build_materials_changed'
        ok = build(turn, cfg, mem, nav, ledger, hero, [target], lambda _: name,
                   work_cell=job.get('work_cell'))
    elif kind == 'mine':
        if turn.zones.get(target) != job['ore'] or target in mem.collect_failures:
            return False, 'mine_changed_or_failed'
        if turn.mine_remain.get(target) is not None and turn.mine_remain[target] <= 0:
            return False, 'mine_exhausted'
        deadline = job['deadline']
        if not job['want_stone'] and not job['stockpile']:
            deadline = min(deadline if deadline is not None else 70, mem.preparation_tick)
            if turn.tick >= deadline or batch_sale_ready(turn, cfg, mem, nav, ledger, hero):
                return False, 'farming_batch_or_phase_complete'
        if job['stockpile'] and turn.tick >= cfg.economy_rounds and batch_sale_ready(turn, cfg, mem, nav, ledger, hero):
            return False, 'stockpile_batch_complete'
        if job['want_stone']:
            goal = job.get('stone_goal') or cfg.stone_batch
            missing = [p for p in walls if p not in turn.blocked and p not in mem.build_failures]
            if not missing or hero.inventory['stone'] >= min(goal, len(missing) * cfg.wall_stones):
                return False, 'material_batch_complete'
        if not _fits_return(turn, cfg, mem, nav, ledger, hero, target):
            return False, 'mining_return_deadline'
        ok = mine(turn, cfg, mem, nav, ledger, hero, want_stone=job['want_stone'],
                  stockpile=job['stockpile'], deadline=deadline,
                  target_only=target, stone_goal=job.get('stone_goal'))
    elif kind == 'spare':
        ok = spare_mine(turn, cfg, mem, nav, ledger, hero, target_only=target)
    elif kind == 'recovery':
        if turn.round > job['until']:
            return False, 'recovery_expired'
        goal = hero.id, job['action'], target
        action = mem.recovery.action(goal, turn, cfg, mem, nav, ledger)
        ok = bool(action and ledger.add(hero.id, action))
        if ok and action['action'] == 'move':
            ledger.remember_work(hero, 'recovery', target, action=job['action'], until=job['until'])
        elif ok:
            mem.recovery.active.pop(hero.id, None)
    else:
        return False, 'unknown_job'
    return ok, 'target_or_route_no_longer_feasible'


def finish_wall_work(turn, cfg, mem, nav, ledger, hero, sites):
    """Spend held stone on feasible wall work before an optional vendor trip."""
    from .economy import build
    if hero.inventory['stone'] < cfg.wall_stones:
        return False
    job = mem.daytime_jobs.get(hero.id, {})
    if job.get('kind') == 'build' and job.get('name') == 'wall':
        ok, _ = _continue(turn, cfg, mem, nav, ledger, hero, job, ledger.tower_cells, sites)
        if ok:
            return True
    feasible = [p for p in sites if p not in turn.blocked and p not in mem.build_failures
                and _fits_return(turn, cfg, mem, nav, ledger, hero, p)]
    return build(turn, cfg, mem, nav, ledger, hero, feasible, lambda _: 'wall')


def resume_daytime_jobs(turn, cfg, mem, nav, ledger, towers, walls, returning):
    if not turn.is_day:
        return
    from .commands import command
    from .economy import (supplies, buy_supply, use_inventory, repair_walls,
                          refresh_stone_reserves, critical_station)

    refresh_stone_reserves(turn, cfg, mem, ledger)
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in returning]
    for hero in free:
        job = mem.daytime_jobs.get(hero.id)
        if hero.health <= 165 and hero.inventory['Medicine']:
            ledger.add(hero.id, command('use', name='Medicine'))
        elif hero.health <= 110:
            buy_supply(turn, ledger, hero,
                       supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True))
            mem.daytime_jobs.pop(hero.id, None)
        elif (turn.station and critical_station(turn, turn.station, mem)
              and (not job or job['target'] != turn.station.pos)):
            use_inventory(turn, nav, ledger, hero, mem=mem, urgent_only=True,
                          target_only=turn.station.pos)
        if hero.id not in ledger.used and (not job or job['kind'] != 'use'):
            use_inventory(turn, nav, ledger, hero, mem=mem, urgent_only=True)
        if (hero.id not in ledger.used and job
                and (hero.inventory['WallUpgradeVoucher1'] or hero.inventory['WallUpgradeVoucher2'])
                and (job['kind'] in ('mine', 'spare') and not job.get('want_stone')
                     or job['kind'] == 'buy' and job['name'].startswith('WallUpgradeVoucher'))):
            use_inventory(turn, nav, ledger, hero, mem=mem)
    # A real breach may borrow one worker, as before; it must not borrow both
    # simply because they have active income jobs. The normal allocator handles
    # first-time jobs; this early call only protects emergency preemption.
    repair_job = mem.daytime_jobs.get(mem.wall_repair_worker, {})
    if mem.daytime_jobs and repair_job.get('kind') not in ('mine', 'build'):
        repair_walls(turn, cfg, mem, nav, ledger,
                     [h for h in free if h.id not in ledger.used], walls)
    # Keep the existing purchase-before-construction gold reservation order.
    # A builder spending first would make the buyer reserve the same missing
    # gun again and needlessly cancel an already accepted shopping journey.
    for hero in sorted(turn.workers, key=lambda h: (
            {'buy': 0, 'robot_buy': 2}.get(mem.daytime_jobs.get(h.id, {}).get('kind'), 1), h.id)):
        job = mem.daytime_jobs.get(hero.id)
        if not job:
            continue
        if hero.id in ledger.used or hero.id in returning:
            reason = 'emergency_or_recall'
        else:
            ok, reason = _continue(turn, cfg, mem, nav, ledger, hero, job, towers, walls)
            if ok:
                LOG.info('round=%s worker=%s daytime_job=continue kind=%s target=%s',
                         turn.round, hero.id, job['kind'], job['target'])
                continue
        mem.daytime_jobs.pop(hero.id, None)
        LOG.info('round=%s worker=%s daytime_job=release kind=%s target=%s reason=%s',
                 turn.round, hero.id, job['kind'], job['target'], reason)

