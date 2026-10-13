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
    if not turn.is_day:
        mem.wall_batches.clear()
    if turn.is_day:
        mem.mine_targets = {uid: p for uid, p in mem.mine_targets.items()
                            if uid in ledger.mine_claims}


def _fits_return(turn, cfg, mem, nav, ledger, hero, target):
    home, exact = return_destination(turn, nav, ledger, hero, mem)
    if not home:
        return False
    trip = via(nav, hero, [[target], home], ledger.reserved, final_exact=exact,
               future_return=turn.tick < cfg.economy_rounds)
    return trip is not None and trip + 1 + cfg.return_margin <= turn.day_left


def _continue(turn, cfg, mem, nav, ledger, hero, job, towers, walls, excluded=()):
    from .economy import buy_supply, supplies, use_inventory, build, batch_sale_ready

    kind, target = job['kind'], job['target']
    income = hero.id == ledger.income_worker_id
    if income and (kind == 'mine' and job.get('want_stone')
                   or kind == 'build' and job.get('name') == 'wall'
                   or kind == 'buy' and job.get('name', '').startswith('Wall')):
        return False, 'income_worker_reserved'
    if kind in ('mine', 'spare') and not job.get('want_stone'):
        if (not job.get('run') and not income and turn.tick >= min(cfg.economy_rounds, mem.preparation_tick)
                and development_pending(turn, cfg, mem, towers, walls)):
            return False, 'development_phase_started'
        if cashout_ores(turn, cfg, mem, hero, sale_inventory(turn, mem, hero)):
            ok = earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)
            return ok, 'news_cashout_or_return_deadline'
        preferred = preferred_stock(turn, cfg, mem, ledger, hero)
        if preferred and turn.zones.get(target) not in preferred:
            return False, 'news_stock_replan'
    if target is not None and mem.movement.avoids(hero.id, target):
        if kind in ('buy', 'sell', 'robot_buy'):
            target = None  # Replan the same item/load through another shop.
        else:
            return False, 'movement_retry_cooldown'
    if kind == 'sell':
        if not sale_inventory(turn, mem, hero):
            return False, 'sale_complete'
        ok = earn(turn, cfg, mem, nav, ledger, hero, force_sale=True,
                  allow_spare=False, vendor_only=target)
        if not ok:
            # The original vendor may be blocked while another is reachable.
            ok = earn(turn, cfg, mem, nav, ledger, hero, force_sale=True,
                      allow_spare=False)
    elif kind == 'buy':
        planned = planned_weapons(turn, cfg, mem, towers)
        plan = supplies(turn, cfg, mem, nav, ledger, hero,
                        sum(w.id < 0 for w in planned) * cfg.weapon_cost,
                        planned=planned, bulk=True, item_only=job['name'],
                        shop_only=target, quantity=job['quantity'])
        if plan is None:
            plan = supplies(turn, cfg, mem, nav, ledger, hero,
                            sum(w.id < 0 for w in planned) * cfg.weapon_cost,
                            planned=planned, bulk=True, item_only=job['name'],
                            quantity=job['quantity'])
        ok = buy_supply(turn, ledger, hero, plan)
        if ok:
            mem.supply_worker = hero.id
    elif kind == 'robot_buy':
        if turn.day > cfg.robot_purchase_last_day:
            return False, 'purchase_days_finished'
        from .brain import summon_best_robot
        from .spending import plan_day_spending, prepare_defense_purchase
        priority_actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, towers, excluded)
        reserve = prepare_defense_purchase(turn, mem, ledger, reserve)
        ok = summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls,
                               excluded=priority_actors | {h.id for h in turn.heroes if h.id != hero.id},
                               reserve=reserve, item_only=job['name'], shop_only=target,
                               quantity_limit=job['quantity'])
        if not ok and target is not None:
            ok = summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls,
                                   excluded=priority_actors | {h.id for h in turn.heroes if h.id != hero.id},
                                   reserve=reserve, item_only=job['name'],
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
        if not ok and job.get('work_cell') is not None:
            # Persist the building target, not one stale adjacent work tile.
            ok = build(turn, cfg, mem, nav, ledger, hero, [target], lambda _: name)
    elif kind == 'mine':
        if turn.zones.get(target) != job['ore'] or target in mem.collect_failures:
            return False, 'mine_changed_or_failed'
        if turn.mine_remain.get(target) is not None and turn.mine_remain[target] <= 0:
            return False, 'mine_exhausted'
        deadline = job['deadline']
        run = job.get('run')
        if run:
            if run['day'] != turn.day:
                return False, 'mining_run_expired'
            if hero.inventory[job['ore']] >= run['collect_goal']:
                if run['mode'] == 'sell_today':
                    ok = earn(turn, cfg, mem, nav, ledger, hero, force_sale=True,
                              allow_spare=False, vendor_only=run['vendor'])
                    if ok:
                        return True, 'planned_collection_complete'
                return False, 'planned_collection_complete'
            if turn.tick >= run['end_tick']:
                return False, 'mining_run_deadline'
        if not run and not job['want_stone'] and not job['stockpile']:
            deadline = 70 if income else min(deadline if deadline is not None else 70, mem.preparation_tick)
            if turn.tick >= deadline or batch_sale_ready(turn, cfg, mem, nav, ledger, hero):
                return False, 'farming_batch_or_phase_complete'
        if not run and job['stockpile'] and turn.tick >= cfg.economy_rounds and batch_sale_ready(turn, cfg, mem, nav, ledger, hero):
            return False, 'stockpile_batch_complete'
        if job['want_stone']:
            goal = job.get('stone_goal') or cfg.stone_batch
            missing = [p for p in walls if p not in turn.blocked and p not in mem.build_failures]
            if not missing or hero.inventory['stone'] >= min(goal, len(missing) * cfg.wall_stones):
                return False, 'material_batch_complete'
        if not run and not job['want_stone'] and not _fits_return(turn, cfg, mem, nav, ledger, hero, target):
            return False, 'mining_return_deadline'
        ok = mine(turn, cfg, mem, nav, ledger, hero, want_stone=job['want_stone'],
                  stockpile=job['stockpile'], deadline=deadline,
                  target_only=target, stone_goal=job.get('stone_goal'),
                  stone_sites=job.get('stone_sites'), run=run)
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
    from .wall_supply import active
    if active(turn, mem):
        # The watch trip and material builder resume their own jobs. Old
        # face-maintenance/buy jobs cannot steal either actor's new assignment.
        return
    from .commands import command
    from .economy import (supplies, buy_supply, use_inventory, repair_walls,
                          refresh_stone_reserves, critical_station)

    refresh_stone_reserves(turn, cfg, mem, ledger)
    for uid in list(mem.wall_batches):
        if uid in returning or uid not in turn.units or uid in ledger.used:
            mem.wall_batches.pop(uid, None)
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in returning]
    for hero in free:
        job = mem.daytime_jobs.get(hero.id)
        if hero.health <= 165 and hero.inventory['Medicine']:
            ledger.add(hero.id, command('use', name='Medicine'))
        elif hero.health <= 110:
            buy_supply(turn, ledger, hero,
                       supplies(turn, cfg, mem, nav, ledger, hero, urgent_only=True))
            mem.daytime_jobs.pop(hero.id, None)
            mem.wall_batches.pop(hero.id, None)
        elif turn.station and critical_station(turn, turn.station, mem):
            use_inventory(turn, nav, ledger, hero, mem=mem, urgent_only=True,
                          target_only=turn.station.pos)
        if hero.id not in ledger.used and hero.id in mem.wall_batches:
            from .wall_batches import continue_batch
            continue_batch(turn, cfg, mem, nav, ledger, hero, towers, walls)
            job = mem.daytime_jobs.get(hero.id)
        if hero.id not in ledger.used and (not job or job['kind'] not in ('use', 'sell')):
            use_inventory(turn, nav, ledger, hero, mem=mem, urgent_only=True)
        if (hero.id not in ledger.used and job
                and (hero.inventory['WallUpgradeVoucher1'] or hero.inventory['WallUpgradeVoucher2'])
                and hero.id not in mem.wall_batches
                and (job['kind'] in ('mine', 'spare') and not job.get('want_stone')
                     or job['kind'] == 'buy' and job['name'].startswith('WallUpgradeVoucher'))):
            use_inventory(turn, nav, ledger, hero, mem=mem)
    # A real breach may borrow one worker, as before; it must not borrow both
    # simply because they have active income jobs. The normal allocator handles
    # first-time jobs; this early call only protects emergency preemption.
    repair_job = mem.daytime_jobs.get(mem.wall_repair_worker, {})
    if mem.daytime_jobs and repair_job.get('kind') not in ('mine', 'build', 'use', 'sell'):
        repair_walls(turn, cfg, mem, nav, ledger,
                     [h for h in free if h.id not in ledger.used], walls)
    from .spending import prepare_paid_upgrades
    prepare_paid_upgrades(turn, mem, nav, ledger, free, returning)
    # Paid robot orders need their daylight action before ordinary income jobs
    # resume. Keep the existing defense buyer/builder and delivery priorities.
    from .model import SUMMON_ORDERS
    if any(h.id not in ledger.used and any(h.inventory[n] for n in SUMMON_ORDERS) for h in free):
        from .brain import summon_best_robot
        from .spending import plan_day_spending, prepare_defense_purchase
        actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, towers, returning)
        reserve = prepare_defense_purchase(turn, mem, ledger, reserve)
        summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls,
                          excluded=returning | actors, reserve=reserve, use_only=True)
    # Keep the existing purchase-before-construction gold reservation order.
    # A builder spending first would make the buyer reserve the same missing
    # gun again and needlessly cancel an already accepted shopping journey.
    for hero in sorted(turn.workers, key=lambda h: (
            {'buy': 0, 'robot_buy': 2}.get(mem.daytime_jobs.get(h.id, {}).get('kind'), 1), h.id)):
        job = mem.daytime_jobs.get(hero.id)
        if hero.id in ledger.traffic_holds:
            continue
        if not job:
            continue
        if hero.id in ledger.used or hero.id in returning:
            reason = 'emergency_or_recall'
        else:
            ok, reason = _continue(turn, cfg, mem, nav, ledger, hero, job, towers, walls, returning)
            if ok:
                if job['kind'] == 'use' and 'UpgradeVoucher' in job['name']:
                    ledger.protected_supply_workers.add(hero.id)
                    if mem.supply_worker is None:
                        mem.supply_worker = hero.id
                LOG.info('round=%s worker=%s daytime_job=continue kind=%s target=%s',
                         turn.round, hero.id, job['kind'], job['target'])
                continue
        mem.daytime_jobs.pop(hero.id, None)
        ledger.daytime_waits[hero.id] = reason
        if not any('UpgradeVoucher' in k for k in hero.backpack):
            mem.preparation_workers.discard(hero.id)
            if mem.supply_worker == hero.id:
                mem.supply_worker = None
        if mem.build_targets.get(hero.id) == job['target']:
            mem.build_targets.pop(hero.id, None)
        LOG.info('round=%s worker=%s daytime_job=release kind=%s target=%s reason=%s',
                 turn.round, hero.id, job['kind'], job['target'], reason)

