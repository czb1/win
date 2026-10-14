"""Reserve construction/upgrades and spend the remaining gold on robots."""
from .economy_plan import front_sites, planned_weapons, via
from .model import (DEFENCE_RETURN_TICK, FIRST_DAY_PIONEER_SHOP_TICK,
                    FIRST_DAY_PIONEER_TASK_DEADLINE, SUMMON_ORDERS)

FIRST_DAY_ROBOT_PURCHASE_LIMIT = 3
FIRST_DAY_ROBOT_PURCHASE_MINIMUM = 2
FIRST_DAY_ROBOT_TASK_STOP = 'first_day_robot_deadline'


def first_day_robot_batch_allowed(turn, mem, ledger, quantity):
    """Commit to the opening assault only with at least two purchased orders."""
    if turn.day != 1 or not turn.is_day:
        return True
    if mem.night_scout.cancelled:
        return False
    purchased = mem.robot_summon_state.get('purchased', 0)
    if purchased + quantity >= FIRST_DAY_ROBOT_PURCHASE_MINIMUM:
        return True
    from .opening_offense import cancel_first_day_offense
    # Use the same day-one weapon upgrade fallback as an enemy Gatling sighting.
    cancel_first_day_offense(turn, mem, 'insufficient_first_day_robots',
                             phase='upgrade_buy', ledger=ledger,
                             purchased=purchased, available_quantity=quantity,
                             minimum=FIRST_DAY_ROBOT_PURCHASE_MINIMUM, gold=ledger.gold)
    return False


def first_day_pioneer_work_pending(turn, mem):
    """Opening work owns daylight actions; unfinished orders yield to night guns."""
    from .daily_offense import daily_pioneer_active
    if daily_pioneer_active(turn, mem):
        return False
    return bool(turn.is_day and turn.pioneer and mem.first_day_pioneer.get('phase') in
                ('shop', 'station', 'robots'))


def first_day_pioneer_plan_active(turn, mem=None):
    """Task-first opening, with a time-based fallback for exhausted points."""
    return bool((mem is not None and first_day_pioneer_work_pending(turn, mem))
                or (turn.is_day and turn.day == 1 and turn.pioneer
                    and (turn.tasks or turn.tick >= FIRST_DAY_PIONEER_SHOP_TICK)))


def first_day_tasks_finished(turn):
    """A cooling or temporarily unreachable point is not an exhausted point."""
    return bool(turn.tasks and not turn.phase_task and all(
        task.get('isValid') is False and int(task.get('coldDownRounds', 0)) <= 0
        for task in turn.tasks))


def first_day_purchase_reserve(turn, mem, ledger, include_robots=True):
    """Protect the base heal and unbought opening orders, counting trips once."""
    if not turn.is_day or turn.day != 1:
        return 0
    if first_day_pioneer_plan_active(turn, mem) and mem.first_day_pioneer.get('phase') == 'return':
        return 0
    from .brain import observe_robot_summons
    from .economy import station_voucher_needed, voucher_for
    from .robot_assault import enemy_base
    state = observe_robot_summons(turn, mem)
    name = voucher_for(turn.station) if turn.station else None
    held = bool(turn.pioneer and not station_voucher_needed(turn, turn.pioneer, name)) if name else False
    reserve = (max(0, turn.shop.get(name, 0)) if name and not held
               and not state['station_purchased'] and name not in ledger.purchases else 0)
    if include_robots and not mem.night_scout.cancelled and enemy_base(turn) is not None:
        remaining = max(0, FIRST_DAY_ROBOT_PURCHASE_LIMIT - state['purchased'])
        planned = ledger.spending_plan.get('robot_purchase')
        if planned and ledger.commands.get(str(planned['actor']), {}).get('action') != 'buy':
            # Walking has already removed this batch's money from ledger.gold.
            remaining = max(0, remaining - planned['quantity'])
        reserve += remaining * max(0, turn.shop.get('BossRobotSummonOrder', 0))
    return reserve


def heal_station_with_pioneer(turn, mem, nav, ledger):
    """Heal a critical base, or spend the voucher before its night carrier dies."""
    from .economy import critical_station, pioneer_needs_night_relief, use_inventory, voucher_for
    hero, station = turn.pioneer, turn.station
    if (not hero or hero.id in ledger.used or not station
            or not (critical_station(turn, station, mem) or pioneer_needs_night_relief(turn))):
        return False
    name = voucher_for(station)
    if not name or not hero.inventory[name]:
        return False
    accepted = use_inventory(turn, nav, ledger, hero, mem=mem,
                             target_only=station.pos, name_only=name, upgrades_only=True)
    if accepted and pioneer_needs_night_relief(turn):
        ledger.explain(hero.id, 'pioneer_night_emergency_heal', health=hero.health,
                       station_health=station.health, item=name)
    return accepted


def prepare_station_reserve(turn, cfg, mem, nav, ledger):
    """Buy the opening base heal before allocating money to robots or guns."""
    from .brain import observe_robot_summons
    from .commands import command
    from .economy import station_voucher_needed, voucher_for
    from .mining import return_destination
    hero, station = turn.pioneer, turn.station
    state = observe_robot_summons(turn, mem)
    if (first_day_pioneer_plan_active(turn, mem)
            or not turn.is_day or turn.day != 1 or turn.tick >= DEFENCE_RETURN_TICK
            or not hero or hero.id in ledger.used or turn.phase_task or not hero.space
            or not station or state.get('station_purchased')):
        return False
    name = voucher_for(station)
    if (not name or name not in turn.shop or turn.shop[name] < 0
            or (hero.id, name) in mem.buy_failures or name in ledger.purchases
            or not station_voucher_needed(turn, hero, name)):
        return False
    if ledger.gold < turn.shop[name]:
        return False
    home, exact = return_destination(turn, nav, ledger, hero, mem)
    if not home:
        return False
    options = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
            continue
        route = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
        if route is None:
            continue
        trip = via(nav, hero, [[shop], home], ledger.reserved,
                   final_exact=exact, future_return=True)
        if trip is not None and trip + 1 + cfg.return_margin <= turn.day_left:
            options.append((route[0], shop, route))
    if not options:
        return False
    _, shop, route = min(options)
    if route[1] is None:
        accepted = ledger.add(hero.id, command('buy', name=name, num=1))
        if accepted:
            state['station_purchased'] = True
            state['station_pending'] = turn.round, hero.id
    else:
        accepted = nav.move_along(hero, ledger, route, {route[2]}, targets={shop},
                                  prefer_vehicle=False)
        if accepted:
            ledger.gold -= turn.shop[name]
            ledger.purchases.add(name)
    if accepted:
        ledger.explain(hero.id, 'reserve_station_healing', item=name, quantity=1,
                       station_health=station.health, heal_below=500,
                       shop=shop, purchase_submitted=route[1] is None)
    return accepted


def _first_day_pioneer_cart(turn, mem, ledger, summons, purchased):
    """Share the actual base reserve, stock and robot budget with task handoff."""
    from .brain import DAILY_SUMMON_LIMIT
    from .economy import station_voucher_needed, voucher_for
    from .robot_assault import enemy_base
    hero = turn.pioneer
    name = voucher_for(turn.station)
    base_missing = bool(name and not summons['station_purchased']
                        and station_voucher_needed(turn, hero, name))
    base_price = turn.shop.get(name, -1) if base_missing else 0
    robot_price = turn.shop.get('BossRobotSummonOrder', -1)
    remaining = 0 if mem.night_scout.cancelled else max(0, FIRST_DAY_ROBOT_PURCHASE_LIMIT - purchased)
    stock = sum(h.inventory[item] for h in turn.heroes for item in SUMMON_ORDERS)
    quantity = min(remaining, max(0, DAILY_SUMMON_LIMIT - summons['count'] - stock),
                   max(0, hero.space - int(base_missing)))
    if enemy_base(turn) is None or robot_price < 0:
        quantity = 0
    elif robot_price:
        quantity = min(quantity, max(0, ledger.gold - max(0, base_price)) // robot_price)
    return base_missing, base_price, remaining, quantity


def first_day_pioneer_task_due(turn, mem, ledger):
    """Stop solving at tick 57 only when the opening robot plan can proceed."""
    if (turn.day != 1 or not turn.is_day or turn.tick < FIRST_DAY_PIONEER_TASK_DEADLINE
            or not turn.phase_task or not turn.pioneer or not turn.station):
        return False
    # A blocked move or delayed task-clear report must not restart task work.
    if mem.stop_reason == FIRST_DAY_ROBOT_TASK_STOP:
        return True
    from .brain import DAILY_SUMMON_LIMIT, observe_robot_summons
    from .robot_assault import enemy_base
    summons = observe_robot_summons(turn, mem)
    if (mem.night_scout.cancelled or enemy_base(turn) is None
            or not any(kind == 'weaponShop' for kind in turn.zones.values())):
        return False
    purchased = mem.first_day_pioneer.get('robot_purchased', summons['purchased'])
    base_missing, base_price, _, quantity = _first_day_pioneer_cart(
        turn, mem, ledger, summons, purchased)
    if base_missing and (base_price < 0 or ledger.gold < base_price or not turn.pioneer.space):
        return False
    held = sum(turn.pioneer.inventory[item] for item in SUMMON_ORDERS)
    if held and summons['count'] < DAILY_SUMMON_LIMIT:
        return True
    return bool(quantity and summons['purchased'] + quantity >= FIRST_DAY_ROBOT_PURCHASE_MINIMUM)


def prepare_first_day_pioneer(turn, cfg, mem, nav, ledger):
    """Continue opening work in daylight, preserving it during night recall."""
    if (not first_day_pioneer_plan_active(turn, mem)
            or (turn.phase_task and mem.stop_reason != FIRST_DAY_ROBOT_TASK_STOP)
            or not (mem.first_day_pioneer or first_day_tasks_finished(turn)
                    or turn.tick >= FIRST_DAY_PIONEER_SHOP_TICK)):
        return False
    from .brain import DAILY_SUMMON_LIMIT, observe_robot_summons, summon_best_robot
    from .commands import command
    from .economy import voucher_for

    hero, station = turn.pioneer, turn.station
    if hero.id in ledger.used or not station:
        return False
    summons = observe_robot_summons(turn, mem)
    state = mem.first_day_pioneer
    if not state:
        state.update(phase='shop', station_item=voucher_for(station), pending_buy=None,
                     trigger=(f'tick_{FIRST_DAY_PIONEER_TASK_DEADLINE}'
                              if mem.stop_reason == FIRST_DAY_ROBOT_TASK_STOP else
                              'tick_51' if turn.tick >= FIRST_DAY_PIONEER_SHOP_TICK else 'tasks_finished'))
        mem.task_target = mem.task_wait_cell = None
        # The shopping chain takes precedence over the ordinary tick-67 recall.
        mem.return_targets.pop(hero.id, None)
        mem.return_posts.pop(hero.id, None)
    state.setdefault('robot_purchased', summons['purchased'] if turn.day == 1 else 0)
    pending = state['pending_buy']
    if pending and pending[0] < turn.round:
        previous, item, *details = pending
        count, before = details if details else (1, 0)
        results = turn.raw.get('lastRoundRoleActionResults') or {}
        rejected = (previous == turn.round - 1
                    and results.get(str(hero.id), results.get(hero.id)) is False)
        if rejected or hero.inventory[item] < before + count:
            # Legal submission is not proof of delivery. Require the next
            # authoritative backpack before advancing either purchase.
            state['phase'] = 'shop'
            if item == 'BossRobotSummonOrder':
                state['robot_purchased'] = max(0, state['robot_purchased'] - count)
                if not rejected:
                    summons['purchased'] = max(0, summons['purchased'] - count)
            else:
                summons['station_purchased'] = False
        state['pending_buy'] = None
    # Emergency use may have advanced the base since this shopping chain
    # started. Never retry a stored, obsolete tier after it was consumed.
    name = state['station_item'] = voucher_for(station)
    held = sum(hero.inventory[item] for item in SUMMON_ORDERS)

    if state['phase'] == 'shop':
        base_missing, base_price, remaining, quantity = _first_day_pioneer_cart(
            turn, mem, ledger, summons, state['robot_purchased'])
        robot_name = 'BossRobotSummonOrder'
        robot_price = turn.shop.get(robot_name, -1)
        if not held and remaining and not first_day_robot_batch_allowed(turn, mem, ledger, quantity):
            remaining = quantity = 0
        if not base_missing and (held or not remaining):
            state['phase'] = 'robots'
        elif base_missing and (base_price < 0 or not hero.space):
            ledger.used.add(hero.id)
            ledger.explain(hero.id, 'first_day_pioneer_shop_waiting', item=name,
                           price_available=base_price >= 0, free_space=hero.space)
            return True
        else:
            options = []
            for shop, kind in turn.zones.items():
                if kind != 'weaponShop':
                    continue
                if mem.movement.avoids(hero.id, shop):
                    continue
                route = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
                if route is None:
                    continue
                count = quantity
                if turn.is_day and count:
                    # Reserve the buy and each summon, but no healthy-base use:
                    # the base voucher stays in the backpack for emergency HP.
                    slots = turn.day_left - route[0] - int(base_missing) - 1 - held
                    minimum = (max(1, FIRST_DAY_ROBOT_PURCHASE_MINIMUM - summons['purchased'])
                               if turn.day == 1 else 1)
                    count = min(count, max(minimum, slots))
                options.append((-count, route[0], shop, route))
            if not options:
                ledger.used.add(hero.id)
                ledger.explain(hero.id, 'first_day_pioneer_shop_blocked')
                return True
            else:
                negative_count, _, shop, route = min(options)
                quantity = -negative_count
                item, count = (name, 1) if base_missing else (robot_name, quantity)
                price = base_price if base_missing else robot_price * count
                retry = (hero.id, item) in mem.buy_failures
                waiting = not count or ledger.gold < price
                if route[1] is not None:
                    # Spend only spare daylight on pickup. If the original trip
                    # is already late, preserve its progress rather than cancel it.
                    travel_limit = max(route[0], turn.day_left - int(base_missing)
                                       - int(quantity > 0) - held - quantity)
                    accepted = nav.move_along(hero, ledger, route, {route[2]},
                                              targets={shop}, max_rounds=travel_limit)
                elif count and not retry and ledger.gold >= price:
                    accepted = ledger.add(hero.id, command('buy', name=item, num=count))
                    if accepted:
                        state['pending_buy'] = turn.round, item, count, hero.inventory[item]
                        if base_missing:
                            summons['station_purchased'] = True
                            summons['station_pending'] = turn.round, hero.id
                        else:
                            state['robot_purchased'] += count
                            summons['purchased'] += count
                            summons['pending_purchase'] = turn.round, hero.id, count
                            state['phase'] = 'robots'
                else:
                    accepted = True
                    ledger.used.add(hero.id)
                if accepted:
                    if route[1] is not None or not count or retry:
                        reserved = min(ledger.gold, max(0, base_price) + max(0, robot_price) * quantity)
                        ledger.gold -= reserved
                        if base_missing:
                            ledger.purchases.add(name)
                        if quantity:
                            ledger.spending_plan['robot_purchase'] = dict(
                                actor=hero.id, item=robot_name, quantity=quantity,
                                unit_price=robot_price, total_cost=robot_price * quantity)
                    ledger.explain(hero.id, 'first_day_pioneer_shop', item=item, quantity=count,
                                   shop=shop, route_steps=route[0], waiting_for_gold=waiting,
                                   buy_retry_cooldown=retry, trigger=state.get('trigger'),
                                   tasks_finished=first_day_tasks_finished(turn))
                    return True

    # Resume old in-flight states without consuming the healing reserve.
    # heal_station_with_pioneer owns all emergency base uses before this flow.
    if state['phase'] == 'station':
        state['phase'] = 'robots'
    if state['phase'] == 'robots':
        if held and not mem.night_scout.cancelled:
            if (turn.is_day and summons['count'] < DAILY_SUMMON_LIMIT
                    and summon_best_robot(turn, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells,
                                         excluded={h.id for h in turn.heroes if h.id != hero.id}, use_only=True)):
                return True
            ledger.used.add(hero.id)
            ledger.explain(hero.id, 'first_day_pioneer_summon_blocked', held=held,
                           waiting_for_daylight=not turn.is_day,
                           daily_limit_reached=summons['count'] >= DAILY_SUMMON_LIMIT)
            return True
        state['robots_done'] = not mem.night_scout.cancelled
        if mem.night_scout.cancelled:
            state['robots_cancelled'] = True
        state['phase'] = 'return'
    if state['phase'] == 'return':
        return return_pioneer_to_post(turn, mem, nav, ledger, hero)
    return False


def return_pioneer_to_post(turn, mem, nav, ledger, hero):
    """Use the existing cannon assignment immediately after a shopping chain."""
    from .mining import return_destination
    from .model import neighbours
    if not turn.weapons:
        from .daytime import guard_station_with_pioneer
        return guard_station_with_pioneer(turn, mem, nav, ledger)
    tower = next((w for h, w in ledger.return_pairs if h.id == hero.id), None)
    if tower:
        mem.return_targets[hero.id] = tower.id
        if hero.id in ledger.operator_posts:
            mem.return_posts[hero.id] = ledger.operator_posts[hero.id]
    else:
        home, exact = return_destination(turn, nav, ledger, hero, mem)
        if not home:
            return False
        route = nav.search(hero, home, ledger.reserved) if exact else nav.approach(hero, home, ledger.reserved)
        if route and route[1] is not None:
            goals = set(home) if exact else {p for cell in home for p in neighbours(cell)} - set(home)
            return nav.move_along(hero, ledger, route, goals, returning=True)
    return False


def prepare_paid_upgrades(turn, mem, nav, ledger, heroes, excluded=()):
    """Deliver feasible paid vouchers before spending an actor on robots."""
    if not turn.is_day or turn.day == 1:
        return
    from .economy import use_inventory
    from .wall_supply import active
    for hero in sorted(heroes, key=lambda h: (h.id != mem.supply_worker, h.id)):
        if active(turn, mem) and hero.kind == 'worker':
            continue
        if (hero.id in ledger.used or hero.id in excluded
                or hero.kind == 'pioneer' and turn.phase_task
                or mem.daytime_jobs.get(hero.id, {}).get('kind') in ('use', 'sell')
                or not any('UpgradeVoucher' in item for item in hero.backpack)):
            continue
        if use_inventory(turn, nav, ledger, hero, mem=mem, upgrades_only=True):
            mem.daytime_jobs.pop(hero.id, None)
            if hero.kind == 'worker':
                ledger.protected_supply_workers.add(hero.id)
                if mem.supply_worker is None:
                    mem.supply_worker = hero.id


def prepare_defense_purchase(turn, mem, ledger, reserve):
    """Submit the selected buyer before a robot can reserve its first step."""
    purchase, ledger.defense_purchase = ledger.defense_purchase, None
    if purchase is None:
        return reserve
    from .economy import buy_supply
    uid, plan = purchase
    before = ledger.gold
    if buy_supply(turn, ledger, turn.units[uid], plan):
        mem.daytime_jobs.pop(uid, None)
        mem.supply_worker = uid
        ledger.protected_supply_workers.add(uid)
        reserve = max(0, reserve - (before - ledger.gold))
        ledger.spending_plan.update(gold_available=ledger.gold, reserved_gold=reserve,
                                    surplus=max(0, ledger.gold - reserve))
    return reserve


def first_day_guns_built(turn, cfg):
    """Observed gun completion for scouting, independent of order purchases."""
    return turn.day == 1 and len(turn.weapons) >= min(3, len(cfg.loadout))


def defenses_ready(turn, cfg, towers, walls, mem=None, ledger=None):
    """Observed readiness diagnostic; robot shopping does not require it."""
    built = {w.pos: w for w in turn.ours if w.kind == 'wall'}
    count = min(len(towers), len(cfg.loadout), 3)
    if not (turn.station and turn.weapons and built
            and set(walls) <= set(built)
            and set(towers[:count]) <= {w.pos for w in turn.weapons}
            and len(turn.weapons) >= count
            and all(w.level >= 3 for w in turn.weapons)
            and all(built[p].level >= 2 for p in walls)
            and all(built[p].level >= 3 for p in front_sites(turn, walls))):
        return False
    if mem and mem.wall_rebuild_levels:
        return False
    watcher = next((h for h in turn.workers if mem and h.id == mem.wall_watch_id), None)
    if watcher:
        missing = max(0, mem.wall_watch.stock_target(turn) - watcher.inventory['WallFixer'])
        funded = ledger.watch_pack_slots.get(watcher.id, 0) if ledger else 0
        if missing > funded:
            return False
    return True


def plan_day_spending(turn, cfg, mem, nav, ledger, towers, excluded=()):
    """Reserve only next purchases with a real actor and feasible delivery.

    Same-item alternatives are one batch, not duplicated obligations. Current
    stock-trips and continued purchases have already reserved ledger gold.
    """
    if turn.day == 1:
        reserve = min(ledger.gold, first_day_purchase_reserve(turn, mem, ledger,
                                                             include_robots=False))
        ledger.defense_purchase = None
        ledger.spending_plan.update(gold_available=ledger.gold, reserved_gold=reserve,
                                   surplus=max(0, ledger.gold - reserve), purchases={},
                                   medical_gold_reserved=0, priority_actors=[], purchase_actor=None,
                                   day_left=turn.day_left)
        return set(), reserve
    from .wall_supply import active, station_plan
    if active(turn, mem):
        from .economy import supplies
        purchases = {}
        ledger.defense_purchase = None
        # The watch and material/sale journeys precede optional robot errands.
        # Keep surplus robot shopping available once these duties are done.
        # Watch money was already removed by prepare_watch.
        actors = {mem.wall_watch_id}
        built = {w.pos for w in turn.ours if w.kind == 'wall'}
        missing = set(ledger.wall_cells) - built - set(mem.build_failures)
        for hero in turn.workers:
            job = mem.daytime_jobs.get(hero.id, {})
            if (turn.tick < cfg.economy_rounds or missing
                    or job.get('kind') in ('sell', 'build')
                    or job.get('kind') == 'mine' and job.get('want_stone')):
                actors.add(hero.id)
        for hero in turn.workers:
            if hero.id == mem.wall_watch_id or hero.id in excluded or hero.id in ledger.used:
                continue
            planned = planned_weapons(turn, cfg, mem, towers)
            plan = supplies(turn, cfg, mem, nav, ledger, hero,
                            sum(w.id < 0 for w in planned) * cfg.weapon_cost,
                            planned=planned, bulk=True)
            if plan:
                purchases[plan[0]] = turn.shop[plan[0]] * plan[2]
                actors.add(hero.id)
        station = station_plan(turn, cfg, mem, nav, ledger, turn.pioneer)
        if station:
            purchases[station[0]] = turn.shop[station[0]]
            actors.add(turn.pioneer.id)
        construction = max(0, len(cfg.loadout) - len(turn.weapons)) * cfg.weapon_cost
        reserve = min(ledger.gold, construction + sum(purchases.values()))
        ledger.spending_plan.update(gold_available=ledger.gold, reserved_gold=reserve,
                                   surplus=max(0, ledger.gold-reserve), purchases=purchases,
                                   priority_actors=sorted(actors), purchase_actor=None,
                                   day_left=turn.day_left, wall_supply_owner=mem.wall_watch_id)
        return actors, reserve
    from .economy import supplies, build_options
    planned = planned_weapons(turn, cfg, mem, towers)
    construction = sum(w.id < 0 for w in planned) * cfg.weapon_cost
    actors, purchases, buyers = set(), {}, []
    free = [h for h in turn.workers if h.id not in ledger.used and h.id not in excluded]
    for hero in free:
        if not hero.space:
            continue
        next_purchase = supplies(turn, cfg, mem, nav, ledger, hero, construction,
                                 planned=planned, bulk=True)
        options = [next_purchase]
        if next_purchase:
            buyers.append((hero.id != mem.supply_worker, next_purchase[1][0], hero.id, next_purchase))
        # A healthy base can be planned alongside a wall batch, rather than
        # losing its money to a robot while the courier is delivering walls.
        if turn.station and turn.station.level < 3:
            options.append(supplies(turn, cfg, mem, nav, ledger, hero, construction,
                                    planned=planned, item_only=f'StationUpgradeVoucher{turn.station.level}'))
        for plan in options:
            if plan:
                name, _, quantity = plan
                purchases[name] = max(purchases.get(name, 0), turn.shop[name] * quantity)
    base_plan = station_plan(turn, cfg, mem, nav, ledger, turn.pioneer)
    if base_plan:
        purchases[base_plan[0]] = turn.shop[base_plan[0]]
        actors.add(turn.pioneer.id)
    selected = min(buyers, key=lambda row: row[:3]) if buyers else None
    buyer = selected[2] if selected else None
    ledger.defense_purchase = (buyer, selected[3]) if selected else None
    if buyer is not None:
        actors.add(buyer)
    # Protect real construction assignments, rather than every possible helper.
    claimed = set()
    if construction and ledger.gold >= cfg.weapon_cost:
        missing_weapons = [w for w in planned if w.id < 0]
        for hero in sorted(free, key=lambda h: h.id):
            if len(claimed) >= ledger.gold // cfg.weapon_cost:
                break
            if hero.id == buyer:
                continue
            for _, _, _, _, target, _ in build_options(
                    turn, cfg, mem, nav, ledger, hero, [w.pos for w in missing_weapons],
                    lambda index: missing_weapons[index].kind):
                if target not in claimed:
                    actors.add(hero.id)
                    claimed.add(target)
                    break
    if any(h.id == mem.wall_repair_worker for h in free):
        actors.add(mem.wall_repair_worker)
    reserve = min(ledger.gold, construction + sum(purchases.values()))
    ledger.spending_plan = dict(gold_available=ledger.gold, reserved_gold=reserve,
                               surplus=max(0, ledger.gold-reserve), purchases=purchases,
                               medical_gold_reserved=purchases.get('Medicine', 0),
                               priority_actors=sorted(actors), purchase_actor=buyer,
                               day_left=turn.day_left)
    return actors, reserve


def robot_purchase_plan(turn, cfg, mem, nav, ledger, hero, name, shop_only=None,
                        quantity=1, reserved_uses=0):
    """Leave a daylight action for the purchase and for every carried/new use."""
    if not turn.is_day or not hero.space or (hero.id, name) in mem.buy_failures:
        return None
    options = []
    for shop, kind in turn.zones.items():
        if kind != 'weaponShop' or shop_only is not None and shop != shop_only:
            continue
        if mem.movement.avoids(hero.id, shop):
            continue
        route = nav.approach(hero, [shop], ledger.reserved)
        if route is None:
            continue
        count = min(quantity, hero.space, turn.day_left - route[0] - 1 - reserved_uses)
        if count > 0:
            options.append((-count, route[0], shop, route))
        else:
            ledger.spending_plan['robot_blocked'] = 'daylight_summon_deadline'
    if not options:
        return None
    negative_count, _, shop, route = min(options)
    return -negative_count, shop, route
