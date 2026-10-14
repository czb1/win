"""The fixed Gatling worker funds a complete front before ordinary income."""
from copy import copy
from dataclasses import replace

from .commands import command
from .economy_plan import front_sites
from .model import CHARACTERS, DEFENCE_RETURN_TICK, Unit
from .mining import mining_yield, return_destination, sale_inventory


def sale_counts(turn, cfg, mem, ledger, hero):
    counts = sale_inventory(turn, mem, hero)
    counts['stone'] = min(counts.get('stone', 0),
                          max(0, hero.inventory['stone'] - len(missing_front(turn, mem, ledger)) * cfg.wall_stones))
    return {k: n for k, n in counts.items() if n}


def missing_front(turn, mem, ledger):
    occupied = {p for u in (*turn.ours, *turn.enemies) if u.kind not in CHARACTERS
                for p in u.cells}
    return sorted(p for p in front_sites(turn, ledger.wall_cells)
            if p not in occupied and turn.zones.get(p, 'land') == 'land'
            and p not in mem.build_failures)


def finish_plan(turn, cfg, mem, nav, ledger, hero, sites, elapsed=0, vendor=None):
    """Forecast sale, every build with updated obstacles, then the operator trip."""
    from .economy import build_options, wall_keeps_access
    shadow_turn, shadow = copy(turn), copy(ledger)
    shadow.commands, shadow.build_claims = dict(ledger.commands), dict(ledger.build_claims)
    shadow_mem = copy(mem)
    shadow_mem.build_targets = dict(mem.build_targets)
    from .night_economy import WorkNavigator
    planned_nav = WorkNavigator(nav, shadow)
    planned_nav.turn = shadow_turn
    shadow.turn = shadow_turn
    dawn = 0 if turn.is_day else 130 - turn.tick
    cutoff = DEFENCE_RETURN_TICK - turn.tick if turn.is_day else dawn + DEFENCE_RETURN_TICK
    limit = turn.day_left if turn.is_day else dawn + 70
    proxy, total, sale_route = hero, elapsed, None
    shadow_turn.blocked = turn.blocked - {hero.pos, turn.units[hero.id].pos}
    if vendor is not None:
        sale_route = planned_nav.leg(proxy, [vendor], total)
        counts = sale_counts(turn, cfg, mem, ledger, hero)
        if not sale_route or not planned_nav.safe(sale_route[2], total + sale_route[0], len(counts)):
            return None
        total += sale_route[0] + len(counts)
        proxy = replace(proxy, pos=sale_route[2])
    # Construction is daylight-only; never emit/forecast night build actions.
    total = max(total, dawn)
    if total:
        # Friendly traffic can vacate before a future delivery. The actual
        # collection/movement/build still uses this turn's occupied cells.
        shadow_turn.blocked -= {h.pos for h in turn.characters}
    shadow_turn.is_day, shadow_turn.tick = True, 0 if dawn else turn.tick
    for index in range(len(sites)):
        shadow_turn.ours = tuple(proxy if u.id == hero.id else u for u in shadow_turn.ours)
        shadow.commands[str(hero.id)] = command('move', proxy.pos)
        planned_nav.elapsed = total
        options = build_options(shadow_turn, cfg, shadow_mem, planned_nav, shadow,
                                proxy, sites, lambda _: 'wall')
        target = next((p for _, _, _, _, p, _ in options
                       if wall_keeps_access(shadow_turn, planned_nav, shadow, p)), None)
        if target is None:
            return None
        route = planned_nav.leg(proxy, [target], total)
        if not route:
            return None
        total += route[0] + 1
        if total > cutoff:
            return None
        proxy = replace(proxy, pos=route[2])
        shadow_turn.blocked |= {target}
        shadow_turn.ours += (Unit(-200000-index, target, 'wall', 1000),)
        shadow.build_claims[target] = (hero.id, 'wall')
        shadow_mem.build_targets[hero.id] = target
    home, exact = return_destination(turn, nav, ledger, hero, mem)
    back = planned_nav.leg(proxy, home, total, exact=exact) if home else None
    if not back or total + back[0] + cfg.return_margin > limit:
        return None
    return dict(total_steps=total + back[0] + cfg.return_margin,
                construction_end=total, return_steps=back[0], sale_route=sale_route)


def _material_plan(turn, cfg, mem, nav, ledger, hero, sites, first):
    """Account for multiple depleted/contested deposits, not a fictitious batch."""
    need = len(sites) * cfg.wall_stones - hero.inventory['stone']
    if need <= 0 or need > hero.space:
        return None
    proxy, elapsed, remaining, visited = hero, 0, need, set()
    first_route, first_amount = None, 0
    while remaining > 0:
        options = []
        for target, kind in turn.zones.items():
            if (kind != 'stone' or target in visited or target in mem.collect_failures
                    or mem.movement.avoids(hero.id, target)
                    or not visited and target != first):
                continue
            route = nav.leg(proxy, [target], elapsed)
            if not route:
                continue
            amount, _ = mining_yield(turn, mem, nav, ledger, hero, target,
                                     elapsed + route[0], remaining)
            if amount and nav.safe(route[2], elapsed + route[0], amount):
                options.append((route[0], -amount, target, route, amount))
        if not options:
            return None
        _, _, target, route, amount = min(options)
        if first_route is None:
            first_route, first_amount = route, amount
        elapsed += route[0] + amount
        proxy = replace(proxy, pos=route[2], backpack=proxy.backpack + ('stone',) * amount)
        remaining -= amount
        visited.add(target)
    plan = _sale_or_build_plan(turn, cfg, mem, nav, ledger, proxy, sites, elapsed)
    if not plan:
        return None
    return dict(plan, mine=first, route=first_route, amount=first_amount,
                stone_goal=len(sites) * cfg.wall_stones)


def _sale_or_build_plan(turn, cfg, mem, nav, ledger, hero, sites, elapsed=0):
    plans = []
    if sale_counts(turn, cfg, mem, ledger, hero):
        for vendor, kind in turn.zones.items():
            if kind == 'vendor' and not mem.movement.avoids(hero.id, vendor):
                plan = finish_plan(turn, cfg, mem, nav, ledger, hero, sites, elapsed, vendor)
                if plan:
                    plans.append(dict(plan, vendor=vendor))
    if plans:
        return min(plans, key=lambda p: (p['total_steps'], p['vendor']))
    # Sale is optional when it would cost the last viable construction window.
    return finish_plan(turn, cfg, mem, nav, ledger, hero, sites, elapsed)


def act(turn, cfg, mem, base_nav, ledger, hero):
    """Preempt income only for an executable front-material/delivery action."""
    if (turn.day < 2 or hero.id != mem.gatling_operator_id or hero.kind != 'worker'
            or hero.id in ledger.used or hero.health <= 110
            or not turn.is_day and turn.day == 10):
        return False
    sites = missing_front(turn, mem, ledger)
    if not sites:
        mem.front_rebuild.pop(hero.id, None)
        return False
    if hero.health <= 165 and hero.inventory['Medicine']:
        return ledger.add(hero.id, command('use', name='Medicine'))
    goal = len(sites) * cfg.wall_stones
    mem.stone_reserves[hero.id] = max(mem.stone_reserves.get(hero.id, 0),
                                     min(goal, hero.inventory['stone']))
    from .night_economy import WorkNavigator
    nav = WorkNavigator(base_nav, ledger)
    state = mem.front_rebuild.setdefault(hero.id, dict(phase='collect'))
    state['sites'] = tuple(sites)
    # A daylight/material-limited batch is spent before another collection trip.
    if state['phase'] in ('collect', 'sell') and state.get('stone_goal'):
        goal = min(goal, state['stone_goal'])
    if hero.inventory['stone'] < cfg.wall_stones or state['phase'] != 'build':
        if hero.inventory['stone'] < goal:
            candidates = [_material_plan(turn, cfg, mem, nav, ledger, hero, sites, target)
                          for target, kind in turn.zones.items() if kind == 'stone']
            plans = [p for p in candidates if p]
            if not plans:
                # Depleted deposits or dusk can make a full face impossible.
                # Prefer a useful smaller material batch to profitable ore.
                ordered = sorted(sites, key=lambda p: (
                    (r[0] if (r := nav.leg(hero, [p])) else 10000), p))
                for count in range(len(sites)-1, 0, -1):
                    partial = [_material_plan(turn, cfg, mem, nav, ledger, hero, ordered[:count], target)
                               for target, kind in turn.zones.items() if kind == 'stone']
                    plans = [p for p in partial if p]
                    if plans:
                        break
            if plans:
                best = min(plans, key=lambda p: (p['total_steps'], p['mine']))
                previous = next((p for p in plans if p['mine'] == state.get('mine')), None)
                plan = previous if previous and previous['total_steps'] <= best['total_steps'] + 2 else best
                route, target = plan['route'], plan['mine']
                action = command('collect', target) if route[1] is None else command('move', route[1])
                if ledger.add(hero.id, action):
                    goal = plan['stone_goal']
                    state.update(phase='collect', mine=target, stone_goal=goal)
                    ledger.mine_claims[hero.id] = target
                    ledger.mine_plans[hero.id] = route[0], plan['amount']
                    mem.mine_targets[hero.id] = target
                    ledger.remember_work(hero, 'mine', target, ore='stone', want_stone=True,
                                         stockpile=False, deadline=None, stone_goal=goal, stone_sites=tuple(sites))
                    ledger.explain(hero.id, 'front_rebuild_material', stone_goal=goal,
                                   held_stone=hero.inventory['stone'], target=target,
                                   expected_units=plan['amount'], total_steps=plan['total_steps'])
                    return True
            # A short day/depleted mine must still allow a useful carried batch.
            if not turn.is_day or hero.inventory['stone'] < cfg.wall_stones:
                return False
            state['phase'] = 'build'
        else:
            state['phase'] = 'sell'
    if state['phase'] == 'sell':
        plan = _sale_or_build_plan(turn, cfg, mem, nav, ledger, hero, sites)
        if plan and plan.get('vendor') is not None:
            route = plan['sale_route']
            counts = sale_counts(turn, cfg, mem, ledger, hero)
            name = max(counts, key=lambda k: counts[k] * turn.prices[k])
            action = (command('sell', name=name, num=counts[name]) if route[1] is None
                      else command('move', route[1]))
            if ledger.add(hero.id, action):
                ledger.remember_work(hero, 'sell', plan['vendor'])
                ledger.explain(hero.id, 'front_rebuild_sale', stone_goal=goal,
                               total_steps=plan['total_steps'], item=name)
                return True
        state['phase'] = 'build'
    if turn.is_day and hero.inventory['stone'] >= cfg.wall_stones:
        from .economy import build
        feasible = [p for p in sites if finish_plan(turn, cfg, mem, nav, ledger, hero, [p])]
        if build(turn, cfg, mem, base_nav, ledger, hero, feasible, lambda _: 'wall'):
            state['phase'] = 'build'
            mem.wall_batches.pop(hero.id, None)
            mem.daytime_jobs.pop(hero.id, None)
            ledger.income_worker_id = None if ledger.income_worker_id == hero.id else ledger.income_worker_id
            ledger.explain(hero.id, 'front_rebuild_delivery', stone_goal=goal,
                           remaining_sites=sites)
            return True
    return False
