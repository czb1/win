"""Fourth-night wall watch, subordinate to the existing gunner assignment."""
from dataclasses import replace
from .commands import command
from .model import neighbours, ORES
from .mining import earn
from .wall_health import repair_risk


def select_watch(turn, mem, pairs):
    operators = {h.id for h, _ in pairs}
    eligible = [h for h in turn.workers if h.id not in operators]
    hero = min(eligible, key=lambda h: (h.id != mem.wall_watch_id,
                                       -h.inventory['WallFixer'], h.id), default=None)
    mem.wall_watch_id = hero.id if turn.day >= 4 and hero else None
    return hero if mem.wall_watch_id is not None else None


def geometry(turn, sites):
    if not sites or not turn.station:
        return set(), None
    right = sum(p[0] for p in turn.station.cells) / 4 < (turn.width - 1) / 2
    front = (max if right else min)(p[0] for p in sites)
    low, high = min(p[1] for p in sites), max(p[1] for p in sites)
    # Rear remains open; side/front boundaries come from the blueprint even
    # when a wall is destroyed. Never cross a breach to patrol outside.
    inside = {(x, y) for x in range(turn.width) for y in range(low + 1, high)
              if (x < front if right else x > front)}
    return inside, front


def watch_route(turn, nav, ledger, mem, hero, sites, target=None):
    inside, _ = geometry(turn, sites)
    goals = (set(neighbours(target.pos)) if target else
             {p for wall in sites for p in neighbours(wall)}) & inside
    reserved = set(ledger.reserved)
    if mem.gunner_post:
        reserved.add(mem.gunner_post)
    if hero.pos in inside:
        reserved.update((x, y) for x in range(turn.width) for y in range(turn.height)
                        if (x, y) not in inside)
    return nav.search(hero, goals, reserved)


def prepare_watch(turn, cfg, mem, nav, ledger, hero, sites):
    """Return (worker locked, gold reserved); retain early daytime production."""
    if not hero or hero.id in ledger.used:
        return False, 0
    walls = [w for w in turn.ours if w.kind == 'wall']
    if not walls:
        return False, 0
    held = hero.inventory['WallFixer']
    price = turn.shop.get('WallFixer')
    target = min(mem.wall_watch.stock_target(turn), hero.capacity)
    need = max(0, target - held)
    # Guarantee up to three packs first. Additional stock must leave money for
    # missing weapons and the next available firepower upgrade.
    upgrades = [turn.shop[name] for w in turn.weapons if w.level < 3
                if (name := f'WeaponUpgradeVoucher{w.level}') in turn.shop and turn.shop[name] > 0]
    core_budget = (max(0, len(cfg.loadout) - len(turn.weapons)) * cfg.weapon_cost
                   + min(upgrades, default=0))
    previous = mem.wall_watch.history[-1] if mem.wall_watch.history else None
    safety_stock = max(3, previous.used + 2) if previous and previous.unmet else 3
    if price is not None and price > 0:
        minimum = max(0, min(safety_stock, target) - held)
        quota = min(need, ledger.gold // price,
                    max(minimum, max(0, ledger.gold - core_budget) // price))
    else:
        quota = need if price == 0 else 0
    funds = quota * price if price is not None and price > 0 else 0

    def report(action, reason, **extra):
        mem.wall_watch.decision(turn, 'wall_supply', actor=hero.id, action=action, reason=reason,
                                target=target, held=held, need=need, affordable=quota,
                                safety_stock=min(safety_stock, target),
                                reserved_gold=funds, core_budget=core_budget,
                                gold=ledger.gold, price=price, **extra)

    home = watch_route(turn, nav, ledger, mem, hero, sites)
    if home is None:
        report('wait', 'no_home_route')
        return False, 0
    shops = []
    for point, kind in turn.zones.items():
        if kind != 'weaponShop' or (hero.id, 'WallFixer') in mem.buy_failures:
            continue
        for cell in neighbours(point):
            route = nav.search(hero, {cell}, ledger.reserved)
            if route is None:
                continue
            back = watch_route(turn, nav, ledger, mem, replace(hero, pos=cell), sites)
            if back:
                shops.append((route[0] + back[0] + 1, route))
    shop = min(shops, key=lambda o: o[0], default=None)
    if shop is None:
        funds = 0
    budget = (shop[0] if shop else home[0]) + cfg.return_margin + len(ORES)
    if turn.tick < cfg.economy_rounds and turn.day_left > budget:
        report('reserve', 'daytime_production')
        return False, funds
    if hero.health <= 165 and hero.inventory['Medicine']:
        ledger.add(hero.id, command('use', name='Medicine'))
        return True, funds
    if quota and shop:
        if shop[0] + cfg.return_margin < turn.day_left:
            if hero.space < quota and any(hero.inventory[k] for k in ORES):
                # Clear ore through the existing once-daily sale contract.
                earn(turn, cfg, mem, nav, ledger, hero, force_sale=True, allow_spare=False)
                report('sell', 'clear_pack_slots')
                return True, funds
            count = min(quota, hero.space)
            if count:
                route = shop[1]
                added = ledger.add(hero.id, command('move', route[1]) if route[1] else
                                   command('buy', name='WallFixer', num=count))
                report('move' if route[1] else 'buy', 'restock' if added else 'command_rejected', count=count)
                return True, funds if route[1] else 0
    if held:
        if home[1] is not None:
            ledger.add(hero.id, command('move', home[1]))
        report('move' if home[1] else 'hold', 'return_with_stock', steps=home[0])
        return True, 0
    report('release', 'no_funds' if not quota else 'shop_or_deadline_unavailable')
    return False, 0


def repair_watch(turn, mem, nav, ledger, hero, sites):
    if turn.is_day:
        return False
    _, front = geometry(turn, sites)
    options, waiting, critical = [], [], []
    # Stunned robots may wake next turn: stop spending, but retain late watch.
    hostile = [r for r in turn.robots if turn.threatens_us(r)]
    held = hero.inventory['WallFixer']
    for wall in turn.ours:
        if wall.kind != 'wall' or wall.pos not in sites or wall.id in ledger.repair_claims:
            continue
        risk = repair_risk(turn, wall, mem)
        if risk.needed:
            critical.append(wall)
        if not held:
            if risk.needed:
                mem.wall_watch.shortage(turn, wall, 'no_pack')
            continue
        route = watch_route(turn, nav, ledger, mem, hero, sites, wall)
        if route is None:
            if risk.needed:
                mem.wall_watch.shortage(turn, wall, 'no_route')
            continue
        rate = max(risk.recent_damage, risk.nearby_damage)
        slack = wall.health / rate - route[0] - 1 if rate else float('inf')
        # A threatened flank that cannot survive another turn outranks frontage.
        rank = (not (risk.emergency or slack <= 0),
                slack if slack <= 0 else 0, wall.pos[0] != front,
                wall.health / risk.maximum, route[0], wall.id)
        item = (rank, wall, route, risk)
        if risk.needed:
            options.append(item)
        elif ((turn.day >= 4 and wall.health * 10 <= risk.maximum * 3)
              or hostile and (turn.day >= 8 or risk.nearby and slack <= 2)):
            # A damaged wall can draw fire later in the wave. Stay beside it
            # even before enemies arrive instead of mining outside the ring.
            waiting.append(item)
    selected = min(options or waiting, key=lambda o: o[0], default=None)
    if selected:
        _, wall, route, risk = selected
        action = 'move' if route[1] else 'use' if risk.needed else 'hold'
        accepted = (action == 'hold' or ledger.add(hero.id, command('move', route[1]) if route[1]
                                                 else command('use', wall.pos, name='WallFixer')))
        mem.wall_watch.decision(turn, 'wall_watch_decision', actor=hero.id, wall=wall.id,
                                health=wall.health, threshold=risk.threshold, held=held,
                                recent_damage=risk.recent_damage, nearby=risk.nearby,
                                steps=route[0], action=action, accepted=accepted,
                                reason=risk.reason if risk.needed else 'preposition')
        return accepted
    reason = ('no_pack' if not held else 'no_route') if critical else 'no_urgent_wall'
    mem.wall_watch.decision(turn, 'wall_watch_decision', actor=hero.id, action='hold' if critical and held else 'release',
                            reason=reason, held=held, critical=[w.id for w in critical])
    return bool(held and (critical or any(w.kind == 'wall' and w.pos in sites
                                      and w.health * 10 <= 3 * repair_risk(turn, w, mem).maximum
                                      for w in turn.ours)))
