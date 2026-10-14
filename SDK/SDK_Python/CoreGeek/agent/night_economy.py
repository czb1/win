"""Release a completed night assignment, then finish income across dawn.

This is separate from daytime construction and the wall supplier's shopping
policy. Inventories, positions and prices always come from the current turn.
"""
from collections import Counter, deque
from dataclasses import replace

from .commands import command
from .model import ORES, WEAPONS, distance, neighbours
from .navigation import Navigator, check_time
from .logging_system import emit_event


def hostiles(turn):
    # Missing destination/ownership is uncertainty, not proof of an enemy wave.
    return [r for r in turn.robots if r.id not in turn.summon_robot_ids
            and (r.target_team not in ('challenger', 'defender') or r.target_team == turn.team)]


def rockets_sufficient(turn, pairs):
    """A conservative single-controller budget, independent of target ordering.

    Every robot must die before it could reach any defended structure. Count
    stunned robots normally and require a healthy pioneer already at both guns.
    """
    threats = hostiles(turn)
    if not threats:
        return True
    pioneer = turn.pioneer
    controls_rockets = pioneer and any(h.id == pioneer.id and w.kind == 'rocket'
                                       for h, w in pairs)
    guns = [w for w in turn.weapons if controls_rockets and w.kind == 'rocket'
            and distance(pioneer.pos, w.pos) == 1]
    if (not pioneer or pioneer.health <= 150 or len(guns) != 2
            or len({w.level for w in guns}) != 1 or not turn.station):
        return False
    if any(u.kind in WEAPONS and turn.base_distance(u.pos) <= u.attack_range + 1
           for u in turn.enemies):
        return False
    defended = [p for u in turn.ours if u.kind in (*WEAPONS, 'wall', 'station')
                or u.id == pioneer.id for p in u.cells]
    deadlines = {r.id: min(129 - turn.tick,
                          min(distance(r.pos, p) for p in defended) - r.attack_range - 1)
                 for r in threats}
    if min(deadlines.values()) < 0:
        return False
    if any(not turn.threatens_us(r) for r in threats):
        # The current combat selector would protect this uncertain robot.
        return False
    limit = min(deadlines.values())
    if any(distance(r.pos, w.pos) + limit > w.attack_range for r in threats for w in guns):
        return False
    protected = [r for r in turn.robots if r not in threats]
    if any(distance(r.pos, ally.pos) <= 2 * limit + 2 for r in threats for ally in protected):
        # Protected-robot splash filtering could suppress all valid aim points.
        return False
    # A selected rocket projectile makes at least one positive hit. Charge
    # only its 10-HP edge damage, not ideal centre hits or any splash benefit.
    # Rounding each robot upward also pays for the final partial hit.
    remaining = sum((r.health + 9) // 10 for r in threats)
    ready = {w.id: w.cooldown for w in guns}
    for tick in range(limit + 1):
        available = [w for w in guns if ready[w.id] <= tick]
        if available:
            weapon = min(available, key=lambda w: (w.level, w.id))
            remaining -= weapon.level
            ready[weapon.id] = tick + 3
        if remaining <= 0:
            return True
    return False


def release_gatling(turn, mem, ledger, pairs):
    """Keep the assignment until it can be released for the rest of this night."""
    if turn.is_day or turn.day < 2:
        return False
    actor = mem.gatling_operator_id
    if actor is None:
        return False
    state = mem.night_release
    from .combat import night_guard_reason
    tower = next((w for w in turn.weapons if w.kind == 'gatling'), None)
    nearby_enemy = bool(tower and night_guard_reason(turn, tower, ledger.cfg.task_danger_radius) == 'nearby_enemy')
    threats = {r.id for r in hostiles(turn)}
    walls = {w.id for w in turn.ours if w.kind == 'wall'}
    if state.get('day') == turn.day and state.get('actor') == actor:
        if state.get('revoked'):
            return False
        emergency = (nearby_enemy or not turn.station or turn.station.health < state['base_health']
                     or not walls >= state['walls'] or not threats <= state['robots']
                     or any(w.kind == 'wall' and w.health < state['wall_health'].get(w.id, w.health)
                            for w in turn.ours)
                     or threats and (not turn.pioneer or turn.pioneer.health <= 150
                         or turn.pioneer.id in ledger.used
                         or not any(h.id == turn.pioneer.id and w.kind == 'rocket' for h, w in pairs)
                         or len([w for w in turn.weapons if w.kind == 'rocket'
                                 and distance(turn.pioneer.pos, w.pos) == 1]) != 2))
        if not emergency:
            return True
        state['revoked'] = True
        mem.night_work.pop(actor, None)
        emit_event('night_work_recalled', dict(actor=actor, reason='survival_emergency'), 'economy')
        return False
    if (nearby_enemy or actor in mem.operator_yields or not rockets_sufficient(turn, pairs)
            or threats and turn.pioneer and turn.pioneer.id in ledger.used):
        return False
    state.clear()
    state.update(day=turn.day, actor=actor, robots=threats, walls=walls,
                 wall_health={w.id: w.health for w in turn.ours if w.kind == 'wall'},
                 base_health=turn.station.health if turn.station else 0)
    emit_event('night_operator_released', dict(actor=actor,
               reason='wave_cleared' if not threats else 'rockets_sufficient'), 'economy')
    return True


class WorkNavigator(Navigator):
    """Ordinary BFS legality plus conservative robot reach at arrival time."""
    def __init__(self, nav, ledger):
        super().__init__(nav.turn, nav.deadline, nav.memory)
        self.gate_guard = nav.gate_guard
        self.traffic_forbidden = nav.traffic_forbidden
        self.ledger = ledger
        self.elapsed = 0
        self.night_left = 0 if self.turn.is_day else 130 - self.turn.tick
        self.robots = [r for r in self.turn.robots if r.id not in self.turn.summon_robot_ids]
        self.enemy_guns = [w for w in (*self.turn.enemies, *ledger.robot_known_enemies)
                           if w.kind in WEAPONS]

    def safe(self, cell, elapsed=0, stay=0):
        if elapsed >= self.night_left:
            return True
        reach = min(elapsed + stay, self.night_left - 1)
        return (all(distance(cell, r.pos) > r.attack_range + reach + 1 for r in self.robots)
                and all(distance(cell, w.pos) > w.attack_range + (w.kind == 'rocket')
                        for w in self.enemy_guns))

    def _bfs(self, hero, goals, blocked):
        key = hero.pos, blocked, self.elapsed
        if key not in self._trees:
            queue = deque([hero.pos])
            routes = {hero.pos: (0, None, 0)}
            while queue:
                check_time(self.deadline)
                cell = queue.popleft()
                length, first, _ = routes[cell]
                for p in neighbours(cell):
                    if (p in blocked or p in routes or not self.turn.inside(p)
                            or not self.safe(p, self.elapsed + length + 1)):
                        continue
                    routes[p] = length + 1, p if length == 0 else first, len(routes)
                    queue.append(p)
            if len(self._trees) >= 16:
                self._trees.clear()
            self._trees[key] = routes
        routes = self._trees[key]
        route = min(((*routes[p], p) for p in goals if p in routes),
                    key=lambda row: (row[0], row[2]), default=None)
        return route, len(routes)

    def leg(self, hero, targets, elapsed=0, exact=False, inside=None):
        reserved = set(self.ledger.reserved)
        if inside is not None and hero.pos in inside:
            reserved.update((x, y) for x in range(self.turn.width)
                            for y in range(self.turn.height) if (x, y) not in inside)
        self.elapsed = elapsed
        try:
            route = (self.search(hero, set(targets), reserved, with_endpoint=True) if exact
                     else self.approach(hero, targets, reserved, with_endpoint=True))
            return route if route and self.safe(route[2], elapsed + route[0], 1) else None
        finally:
            self.elapsed = 0


def _deliver(turn, mem, nav, ledger, hero, state, watcher=False):
    from .economy import voucher_for, wall_upgrade_allowed, upgrade_order, weapon_upgrade_allowed
    from .wall_watch import geometry
    from .wall_batches import face_sites
    from .wall_health import WALL_MAX_HEALTH
    inside = geometry(turn, ledger.wall_cells)[0] if watcher else None
    options = []
    for wall in turn.ours:
        if wall.kind not in (('wall',) if watcher else WEAPONS):
            continue
        name = voucher_for(wall)
        if (not name or not hero.inventory[name] or wall.id in ledger.upgrade_claims
                or not weapon_upgrade_allowed(turn, wall)
                or not wall_upgrade_allowed(turn, wall, mem, paid=True)
                or mem.movement.avoids(hero.id, wall.pos)):
            continue
        goals = (set(neighbours(wall.pos)) & inside) if watcher else wall.cells
        route = nav.leg(hero, goals, exact=watcher, inside=inside)
        if not route or route[0] + 1 > state['end_round'] - turn.round:
            continue
        wounded = watcher and wall.health < WALL_MAX_HEALTH[wall.level - 1]
        same_face = wall.pos in face_sites(ledger.wall_cells, state.get('anchor'))
        priority = ((not wounded, not same_face, route[0], upgrade_order(turn, wall, mem), wall.id)
                    if watcher else (upgrade_order(turn, wall, mem), route[0], wall.id))
        options.append((priority, wall, name, route))
    if not options:
        return False
    _, wall, name, route = min(options, key=lambda row: row[0])
    action = (command('use', wall.nearest_cell(hero.pos), name=name) if route[1] is None
              else command('move', route[1]))
    if ledger.add(hero.id, action):
        state['anchor'] = wall.pos
        state['delivery_pending'] = action['action'] == 'move'
        ledger.upgrade_claims.add(wall.id)
        ledger.explain(hero.id, 'night_wall_delivery' if watcher else 'night_weapon_delivery',
                       target=wall.pos, item=name, steps=route[0], inside_only=watcher)
        return True
    return False


def _sale_counts(turn, cfg, mem, hero, ledger):
    # Carry enough stone for the existing blueprint gaps into the next day.
    from .mining import sale_inventory
    counts = sale_inventory(turn, mem, hero)
    occupied = {p for u in (*turn.ours, *turn.enemies) if u.kind not in ('worker', 'pioneer', 'imp')
                for p in u.cells}
    need = len(ledger.wall_cells - occupied) * cfg.wall_stones
    counts['stone'] = min(counts.get('stone', 0), max(0, hero.inventory['stone'] - need))
    return {name: n for name, n in counts.items() if n and turn.prices.get(name, 0) > 0}


def _sell(turn, cfg, mem, nav, ledger, hero, state):
    counts = _sale_counts(turn, cfg, mem, hero, ledger)
    options = []
    for vendor, kind in turn.zones.items():
        if kind != 'vendor' or mem.movement.avoids(hero.id, vendor):
            continue
        route = nav.leg(hero, [vendor])
        if (route and route[0] + len(counts) <= state['end_round'] - turn.round
                and nav.safe(route[2], route[0], len(counts))):
            options.append((vendor != state.get('vendor'), route[0], vendor, route))
    if not counts or not options:
        return False
    _, _, vendor, route = min(options)
    name = max(counts, key=lambda k: counts[k] * turn.prices[k])
    action = (command('sell', name=name, num=counts[name]) if route[1] is None
              else command('move', route[1]))
    if ledger.add(hero.id, action):
        state['vendor'] = vendor
        ledger.explain(hero.id, 'night_income_sale', target=vendor, steps=route[0],
                       item=name, quantity=counts[name], end_round=state['end_round'])
        return True
    return False


def _purchase(turn, cfg, mem, nav, ledger, hero, state):
    """Gatling courier buys its own applicable weapon vouchers, never watch packs."""
    from .economy import voucher_for, upgrade_order, weapon_upgrade_allowed
    from .wall_supply import shopping_list, core_reserve
    if hero.id != mem.gatling_operator_id or not hero.space or state.get('shopping_done'):
        return False
    held = Counter(item for h in turn.heroes for item in h.backpack)
    missing_guns = max(0, len(cfg.loadout) - len(turn.weapons)) * cfg.weapon_cost
    reserve = missing_guns
    watcher = turn.units.get(mem.wall_watch_id)
    if watcher:
        # Use the unchanged supplier policy; its own planned order keeps its gold.
        reserve += shopping_list(turn, mem, watcher, ledger.wall_cells,
                                 ledger.gold - core_reserve(turn, cfg), watcher.space)['cost']
    demand = {}
    for weapon in sorted(turn.weapons, key=lambda w: upgrade_order(turn, w, mem)):
        if not weapon_upgrade_allowed(turn, weapon):
            continue
        name = voucher_for(weapon)
        if not name:
            continue
        if held[name]:
            held[name] -= 1
            continue
        price = turn.shop.get(name, -1)
        if (price < 0 or name in ledger.purchases or (hero.id, name) in mem.buy_failures
                or price > ledger.gold - reserve):
            continue
        demand.setdefault(name, []).append(weapon)
    candidates = []
    for name, weapons in demand.items():
        price = turn.shop[name]
        maximum = min(len(weapons), hero.space,
                      (ledger.gold - reserve) // price if price else len(weapons))
        for shop, kind in turn.zones.items():
            if kind != 'weaponShop' or mem.movement.avoids(hero.id, shop):
                continue
            route = nav.leg(hero, [shop])
            if not route:
                continue
            proxy, total, quantity = replace(hero, pos=route[2]), route[0] + 1, 0
            for weapon in weapons[:maximum]:
                delivery = nav.leg(proxy, weapon.cells, total)
                if not delivery or total + delivery[0] + 1 > state['end_round'] - turn.round:
                    break
                total += delivery[0] + 1
                quantity += 1
                proxy = replace(hero, pos=delivery[2])
            if quantity:
                candidates.append((upgrade_order(turn, weapons[0], mem), shop != state.get('shop'),
                                   total, shop, name, route, price, quantity))
    if not candidates:
        return False
    _, _, total, shop, name, route, price, quantity = min(candidates, key=lambda row: row[:4])
    action = command('buy', name=name, num=quantity) if route[1] is None else command('move', route[1])
    if ledger.add(hero.id, action):
        if route[1] is not None:
            ledger.gold -= price * quantity
        else:
            state['pending_buy'] = turn.round, name, quantity, hero.inventory[name]
        ledger.purchases.add(name)
        state['shop'] = shop
        ledger.explain(hero.id, 'night_income_purchase', target=shop, item=name,
                       quantity=quantity, steps=route[0], delivery_steps=total, reserved_gold=reserve)
        return True
    return False


def _mine(turn, cfg, mem, nav, ledger, hero, state):
    from .mining import (MineOption, mining_yield, prefer_separate_sector,
                         remaining_ore, reserve_watch_space)
    hero = reserve_watch_space(turn, mem, hero, ledger)
    vendors = [p for p, kind in turn.zones.items() if kind == 'vendor'
               and not mem.movement.avoids(hero.id, p)]
    options = []
    for target, kind in turn.zones.items():
        if (kind not in ORES or turn.prices.get(kind, 0) <= 0 or not hero.space
                or remaining_ore(turn, mem, target) <= 0 or target in mem.collect_failures
                or mem.movement.avoids(hero.id, target)):
            continue
        arrival = nav.leg(hero, [target])
        if not arrival:
            continue
        amount, claimants = mining_yield(turn, mem, nav, ledger, hero, target, arrival[0], hero.space)
        if target == state.get('mine'):
            amount = min(amount, max(0, state['collect_goal'] - hero.inventory[kind]))
        # Every selected deposit must have a real subsequent sale endpoint.
        count = len(set(_sale_counts(turn, cfg, mem, hero, ledger)) | {kind})
        for quantity in range(amount, 0, -1):
            if not nav.safe(arrival[2], arrival[0], quantity):
                continue
            sale = nav.leg(replace(hero, pos=arrival[2]), vendors, arrival[0] + quantity)
            if not sale or not nav.safe(sale[2], arrival[0] + quantity + sale[0], count):
                continue
            # Preserve a feasible next-day preparation leg, without travelling
            # home at dawn. The existing daytime allocator owns work after 35.
            home = turn.station.cells if turn.station else []
            prep = nav.leg(replace(hero, pos=sale[2]), home,
                           arrival[0] + quantity + sale[0] + count) if home else None
            preparation = prep[0] if prep else 0
            if (home and prep is None
                    or preparation + cfg.return_margin > 70 - cfg.economy_rounds):
                continue
            total = arrival[0] + quantity + sale[0] + count
            if total <= state['end_round'] - turn.round:
                score = turn.prices[kind] * quantity / total
                options.append(MineOption(target, arrival[:2], score, quantity, claimants,
                                          cell=arrival[2], run={'vendor_cell': sale[2],
                                                              'preparation_steps': preparation}))
                break
    if not options:
        return False
    order = lambda o: (-o.score, o.route[0], o.target)
    best = min(options, key=order)
    previous = next((o for o in options if o.target == state.get('mine')), None)
    chosen = (previous if previous and previous.score >= .65 * best.score else
              min(prefer_separate_sector(turn, hero,
                  [o for o in options if o.score >= .8 * best.score]), key=order))
    name = turn.zones[chosen.target]
    action = (command('collect', chosen.target) if chosen.route[1] is None
              else command('move', chosen.route[1]))
    if ledger.add(hero.id, action):
        if state.get('mine') != chosen.target:
            state.update(mine=chosen.target, collect_goal=hero.inventory[name] + chosen.amount)
        state['ore'] = name
        ledger.mine_claims[hero.id] = chosen.target
        ledger.mine_plans[hero.id] = chosen.route[0], chosen.amount
        mem.mine_targets[hero.id] = chosen.target
        ledger.explain(hero.id, 'night_income_mine', target=chosen.target,
                       steps=chosen.route[0], expected_units=chosen.amount,
                       end_round=state['end_round'], claimed_by=chosen.claimants, **chosen.run)
        return True
    return False


def work(turn, cfg, mem, base_nav, ledger, hero, *, start=False):
    """Return ownership even on a bounded wait; no idle-home fallback can recall it."""
    if hero.kind != 'worker' or hero.id in ledger.used or turn.day < 2:
        return False
    state = mem.night_work.get(hero.id)
    if not state and not start:
        return False
    if state is None:
        end = cfg.round_origin + min(turn.day * 130 + cfg.economy_rounds, 1300)
        state = mem.night_work[hero.id] = dict(day=turn.day, end_round=end, phase='sell')
    if turn.round >= state['end_round'] or turn.day > state['day'] + 1:
        mem.night_work.pop(hero.id, None)
        return False
    nav = WorkNavigator(base_nav, ledger)
    watcher = hero.id == mem.wall_watch_id
    pending = state.get('pending_buy')
    if pending and pending[0] < turn.round:
        _, name, quantity, before = pending
        if hero.inventory[name] >= before + quantity:
            state['shop_bought'] = True
        state.pop('pending_buy', None)
    if hero.health <= 165 and hero.inventory['Medicine']:
        ledger.add(hero.id, command('use', name='Medicine'))
        return True
    if hero.health <= 110:
        # Let existing emergency medicine/repair handling take over.
        mem.night_work.pop(hero.id, None)
        return False
    if ((not turn.is_day or not watcher or state.get('delivery_pending'))
            and not (not watcher and state['phase'] == 'shop')
            and _deliver(turn, mem, nav, ledger, hero, state, watcher)):
        return True
    phase = state['phase']
    if phase == 'mine':
        target = state.get('mine')
        if (target is not None and turn.zones.get(target) == state.get('ore')
                and hero.inventory[state['ore']] < state['collect_goal']
                and _mine(turn, cfg, mem, nav, ledger, hero, state)):
            return True
        state['phase'] = 'sell'
    if state['phase'] == 'sell':
        if _sell(turn, cfg, mem, nav, ledger, hero, state):
            return True
        state['phase'] = 'shop'
    if state['phase'] == 'shop':
        # Paid weapon deliveries also continue across dawn, unlike the watcher's
        # daytime walls, which remain owned by its existing supply trip.
        if _purchase(turn, cfg, mem, nav, ledger, hero, state):
            return True
        if state.get('shop_bought'):
            state['shopping_done'] = True
        if not watcher and _deliver(turn, mem, nav, ledger, hero, state):
            return True
        state['phase'] = 'mine'
        state.pop('mine', None)
    if _mine(turn, cfg, mem, nav, ledger, hero, state):
        return True
    if (not turn.is_day and (not turn.prices or not any(k == 'vendor' for k in turn.zones.values()))):
        # Incomplete/offline payloads retain bounded safe collection. Live
        # income plans with vendors must pass the full sale forecast above.
        from .mining import night_mine
        if night_mine(turn, cfg, mem, nav, ledger, hero, dedicated=True):
            return True
    if not nav.safe(hero.pos, stay=1):
        safe = {p for p in neighbours(hero.pos) if nav.safe(p, 1, 1)}
        route = nav.leg(hero, safe, exact=True) if safe else None
        if route and route[1] is not None:
            ledger.add(hero.id, command('move', route[1]))
    if hero.id not in ledger.used:
        from .traffic import transit_cells, parking_goals
        if hero.pos in transit_cells(turn, nav, ledger) | set(ledger.operator_posts.values()) | {mem.gunner_post}:
            goals = {p for p in parking_goals(turn, nav, ledger, hero)
                     if distance(p, hero.pos) <= 2 and nav.safe(p, stay=2)}
            route = nav.leg(hero, goals, exact=True) if goals else None
            if route and route[1] is not None:
                ledger.add(hero.id, command('move', route[1]))
    ledger.used.add(hero.id)
    ledger.reserved.add(hero.pos)
    ledger.explain(hero.id, 'night_income_wait', phase=state['phase'],
                   reason_detail='no_safe_profitable_route', end_round=state['end_round'])
    return True


def resume_morning(turn, cfg, mem, nav, ledger, excluded=()):
    if not turn.is_day:
        return
    for hero in turn.workers:
        if hero.id in excluded or hero.id in ledger.traffic_holds:
            continue
        state = mem.night_work.get(hero.id)
        if state and turn.day == state['day'] + 1:
            work(turn, cfg, mem, nav, ledger, hero)
