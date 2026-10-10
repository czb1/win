"""Persistent mining, safe night runs and budgeted daytime batch sales."""
import logging
from collections import Counter
from copy import copy
from dataclasses import dataclass, replace
from math import isclose
from .commands import command
from .market import hold_inventory, preferred_stock, cashout_ores
from .model import ORES, Unit, DEFENCE_RETURN_TICK, distance, neighbours
from .economy_plan import via

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaterialPlan:
    amount: int
    goal: int
    construction_steps: int
    return_steps: int
    targets: tuple


@dataclass(frozen=True)
class MineOption:
    target: tuple
    route: tuple
    score: float
    amount: int
    claimants: tuple
    return_steps: int = 0
    cell: tuple | None = None
    material: MaterialPlan | None = None


def remaining_ore(turn, mem, target):
    remaining = turn.mine_remain.get(target)
    if remaining is not None:
        return max(0, remaining)
    return max(1, 10 - mem.mine_collected.get(target, 0))


def mining_sector(turn, hero):
    """Stable worker IDs split the two wings around the actual base centre."""
    if not turn.station or len(turn.workers) != 2 or min(turn.width, turn.height) <= 1:
        return None
    home_side = sum(turn.mine_half(p) for p in turn.station.cells)
    if not home_side:
        return None
    index = sorted(h.id for h in turn.workers).index(hero.id)
    if home_side < 0:
        index = 1 - index
    return 'southwest' if index == 0 else 'northeast'


def target_sector(turn, target):
    if not turn.station:
        return None
    cells = turn.station.cells
    projection = ((len(cells) * target[0] - sum(p[0] for p in cells)) * (turn.height - 1)
                  + (len(cells) * target[1] - sum(p[1] for p in cells)) * (turn.width - 1))
    return 'southwest' if projection < 0 else 'northeast' if projection > 0 else 'boundary'


def prefer_separate_sector(turn, hero, options):
    # Nearby simultaneous collection remains useful. Only redirect a journey
    # when a comparable independent deposit exists; never walk to a bare corner.
    separate = [o for o in options if not o.claimants or o.route[0] == 0]
    options = separate or options
    sector = mining_sector(turn, hero)
    local = [o for o in options if target_sector(turn, o.target) in (sector, 'boundary')] if sector else []
    return local or options


def comparable_score(score, best):
    threshold = .8 * best
    return score >= threshold or isclose(score, threshold, rel_tol=1e-12)


def record_target(turn, hero, target, route, mode, changed=False):
    log = LOG.info if changed or route[1] is None else LOG.debug
    log("round=%s worker=%s mining_mode=%s ore=%s target=%s steps=%s action=%s",
        turn.round, hero.id, mode, turn.zones[target], target, route[0],
        "collect" if route[1] is None else "move")


def mining_home(turn, nav, hero, reserved=()):
    home = [w.pos for w in turn.weapons]
    if home and nav.approach(hero, home, reserved) is not None:
        return home
    # A blocked weapon ring does not make the base itself unreachable.
    return list(turn.station.cells) if turn.station else []


def return_destination(turn, nav, ledger, hero, mem=None):
    if mem is not None and turn.is_day and turn.day >= 4 and hero.id == mem.wall_watch_id:
        from .wall_watch import geometry
        inside, _ = geometry(turn, ledger.wall_cells)
        post = getattr(ledger, 'daytime_gunner_post', mem.gunner_post)
        return inside - set(ledger.operator_posts.values()) - {post}, True
    if hero.id in ledger.operator_posts:
        return [ledger.operator_posts[hero.id]], True
    tower = next((w for h, w in ledger.return_pairs if h.id == hero.id), None)
    return (tower.cells if tower else mining_home(turn, nav, hero, ledger.reserved)), False


def reserve_watch_space(turn, mem, hero, ledger=None):
    if turn.is_day and turn.day >= 4 and hero.id == mem.wall_watch_id:
        price = turn.shop.get("WallFixer")
        if price is not None and price >= 0:
            target = min(hero.capacity, mem.wall_watch.stock_target(turn))
            missing = max(0, target - hero.inventory["WallFixer"])
            gold = ledger.gold if ledger is not None else turn.gold
            affordable = missing if price == 0 else min(missing, gold // price)
            if ledger is not None:
                affordable = ledger.watch_pack_slots.get(hero.id, affordable)
            return replace(hero, capacity=max(0, hero.capacity - affordable))
    return hero


def mining_yield(turn, mem, nav, ledger, hero, target, steps, capacity):
    """Forecast this worker's yield using active claims and real arrival paths."""
    remaining = remaining_ore(turn, mem, target)
    partners = []
    for other in turn.workers:
        if other.id == hero.id or other.abnormal_state == 'dizzy':
            continue
        other = reserve_watch_space(turn, mem, other, ledger)
        if not other.space:
            continue
        job = ledger.work_jobs.get(other.id, mem.daytime_jobs.get(other.id, {}))
        if other.id in ledger.mine_claims:
            claim = ledger.mine_claims[other.id]
        elif other.id in ledger.used or other.id in mem.sale_workers:
            continue
        elif job:
            if job['kind'] not in ('mine', 'spare'):
                continue
            claim = job['target']
        else:
            claim = mem.mine_targets.get(other.id)
        if claim != target or mem.movement.avoids(other.id, target):
            continue
        plan = ledger.mine_plans.get(other.id)
        if plan is not None:
            arrival, limit = plan
        else:
            route = nav.approach(other, [target], ledger.reserved)
            if route is None:
                continue
            arrival, limit = route[0], other.space
            if job.get('kind') == 'spare':
                limit = min(limit, 1)
            elif job.get('want_stone') and job.get('stone_goal') is not None:
                limit = min(limit, max(0, job['stone_goal'] - other.inventory['stone']))
        before = min(limit, max(0, steps - arrival))
        remaining -= before
        partners.append([arrival, limit - before, other.id])
    claimants = tuple(p[2] for p in partners)
    if remaining <= 0:
        return 0, claimants
    amount = 0
    # A deposit holds at most ten units. On its last collection turn the rules
    # grant each simultaneous collector one unit, even if only one remains.
    for offset in range(remaining):
        if amount >= capacity:
            break
        collectors = 1
        for partner in partners:
            if partner[0] <= steps + offset and partner[1] > 0:
                partner[1] -= 1
                collectors += 1
        amount += 1
        remaining -= collectors
        if remaining <= 0:
            break
    return amount, claimants


def spare_mine(turn, cfg, mem, nav, ledger, hero, *, home=None, exact=False,
               reserved=(), max_steps=2, allow_blocked_home=False, target_only=None):
    """Use otherwise idle daylight near ore; carry it to a later day's sale."""
    hero = reserve_watch_space(turn, mem, hero, ledger)
    if not turn.is_day or not hero.space:
        LOG.debug("round=%s worker=%s spare_mining=unavailable day=%s free_space=%s",
                  turn.round, hero.id, turn.is_day, hero.space)
        return False
    if home is None:
        home, exact = return_destination(turn, nav, ledger, hero)
    if not home and not allow_blocked_home:
        LOG.debug("round=%s worker=%s spare_mining=no_return_destination", turn.round, hero.id)
        return False
    danger = {(x, y) for r in turn.robots if turn.threatens_us(r)
              for x in range(max(0, r.pos[0] - r.attack_range - 2),
                             min(turn.width, r.pos[0] + r.attack_range + 3))
              for y in range(max(0, r.pos[1] - r.attack_range - 2),
                             min(turn.height, r.pos[1] + r.attack_range + 3))}
    if hero.pos in danger:
        return False
    reserved = set(ledger.reserved) | set(reserved) | danger
    options = []
    for target, kind in turn.zones.items():
        if target_only is not None and target != target_only:
            continue
        if turn.mine_remain.get(target) is not None and turn.mine_remain[target] <= 0:
            continue
        if (kind not in ORES or turn.prices.get(kind, 0) <= 0 or target in mem.collect_failures
                or mem.movement.avoids(hero.id, target)):
            continue
        if distance(hero.pos, target) > 3:
            continue
        # Check the return from the actual collection tile, not from a
        # different side of the deposit. At most two steps start a spare trip.
        for cell in neighbours(target):
            route = nav.search(hero, {cell}, reserved)
            if route is None or route[0] > max_steps:
                continue
            amount, claimants = mining_yield(turn, mem, nav, ledger, hero, target, route[0], 1)
            if not amount:
                continue
            original = turn.blocked
            try:
                turn.blocked = original - {hero.pos}
                proxy = replace(hero, pos=cell)
                back = (nav.search(proxy, home, reserved) if exact
                        else nav.approach(proxy, home, reserved)) if home else None
            finally:
                turn.blocked = original
            if back is None:
                # A blocked recall may still use an adjacent deposit, but may
                # never start a walk without a verified way home.
                if not allow_blocked_home or route[0] or turn.day_left <= cfg.return_margin:
                    continue
                return_steps = 0
            else:
                return_steps = back[0]
                if route[0] + 1 + return_steps + cfg.return_margin > turn.day_left:
                    continue
            options.append(MineOption(target, route, turn.prices[kind] / (1 + route[0]),
                                      amount, claimants, return_steps, cell))
    if not options:
        LOG.debug("round=%s worker=%s spare_mining=no_safe_mine_within_two_steps day_left=%s",
                  turn.round, hero.id, turn.day_left)
        return False
    preferred = preferred_stock(turn, cfg, mem, ledger, hero)
    order = lambda o: (o.route[0], -o.score, o.return_steps, o.target, o.cell)
    previous = min((o for o in options if o.target == mem.mine_targets.get(hero.id)),
                   key=order, default=None)
    shortest = min(o.route[0] for o in options)
    nearby = [o for o in options if o.route[0] == shortest]
    best_score = max(o.score for o in nearby)
    comparable = [o for o in nearby if comparable_score(o.score, best_score)]
    if preferred:
        news = [o for o in comparable if turn.zones[o.target] in preferred]
        chosen = min(prefer_separate_sector(turn, hero, news or comparable), key=order)
    else:
        chosen = previous or min(prefer_separate_sector(turn, hero, comparable), key=order)
    target, route = chosen.target, chosen.route
    action = command("collect", target) if route[1] is None else command("move", route[1])
    if ledger.add(hero.id, action):
        ledger.explain(hero.id, "spare_mining_for_later", target=target, route_steps=route[0],
                       day_left=turn.day_left, return_margin=cfg.return_margin,
                       free_space=hero.space, mining_sector=mining_sector(turn, hero),
                       target_sector=target_sector(turn, target), remaining_ore=remaining_ore(turn, mem, target),
                       expected_units=chosen.amount, claimed_by=chosen.claimants)
        changed = mem.mine_targets.get(hero.id) != target
        mem.mine_targets[hero.id] = target
        ledger.mine_claims[hero.id] = target
        ledger.mine_plans[hero.id] = route[0], chosen.amount
        if route[1] is not None:
            ledger.remember_work(hero, 'spare', target)
        record_target(turn, hero, target, route, "carry_for_later", changed)
        return True
    return False


def sale_inventory(turn, mem, hero):
    """Only surplus stone can be sold while the wall blueprint needs material."""
    counts = hero.inventory
    counts["stone"] = max(0, counts["stone"] - mem.stone_reserves.get(hero.id, 0))
    return {k: counts[k] for k in ORES if counts[k] and turn.prices.get(k, 0) > 0}


def wall_material_plan(turn, cfg, mem, nav, ledger, hero, cell, steps, amount, goal, sites):
    """Forecast a complete, possibly smaller, collect/build/return batch."""
    if not turn.is_day or turn.tick >= DEFENCE_RETURN_TICK:
        return None
    home, exact = return_destination(turn, nav, ledger, hero, mem)
    if not home:
        return None
    from .economy import build_options, wall_keeps_access

    held = hero.inventory['stone']
    goal = min(goal if goal is not None else cfg.stone_batch, held + amount)
    shadow = copy(ledger)
    shadow.commands, shadow.build_claims = dict(ledger.commands), dict(ledger.build_claims)
    memory = copy(mem)
    memory.build_targets = dict(mem.build_targets)
    original, original_units = turn.blocked, turn.ours
    guard, nav.gate_guard = nav.gate_guard, None
    proxy, walk, targets, best = replace(hero, pos=cell), 0, [], None
    try:
        turn.blocked = original - {hero.pos}
        turn.ours = tuple(proxy if u.id == hero.id else u for u in original_units)
        for _ in range(goal // cfg.wall_stones):
            shadow.commands[str(hero.id)] = command('move', proxy.pos)
            options = build_options(turn, cfg, memory, nav, shadow, proxy, sites, lambda _: 'wall')
            target = next((p for _, _, _, _, p, _ in options
                           if wall_keeps_access(turn, nav, shadow, p)), None)
            if target is None:
                break
            leg = nav.approach(proxy, [target], shadow.reserved, with_endpoint=True)
            walk += leg[0]
            proxy = replace(proxy, pos=leg[2])
            targets.append(target)
            shadow.build_claims[target] = (hero.id, 'wall')
            memory.build_targets[hero.id] = target
            turn.blocked = turn.blocked | {target}
            turn.ours = tuple(proxy if u.id == hero.id else u for u in turn.ours) + (
                Unit(-100000 - len(targets), target, 'wall', 1000),)
            needed = len(targets) * cfg.wall_stones - held
            if needed <= 0:
                continue
            construction = walk + len(targets)
            work = steps + needed + construction
            if work > DEFENCE_RETURN_TICK - turn.tick:
                break
            occupied = turn.blocked
            try:
                if turn.tick < cfg.economy_rounds:
                    # A vacated actor cell may now contain a constructed wall.
                    turn.blocked = occupied - ({h.pos for h in turn.heroes} - set(targets))
                back = (nav.search(proxy, home, shadow.reserved) if exact
                        else nav.approach(proxy, home, shadow.reserved))
            finally:
                turn.blocked = occupied
            if back is not None and work + back[0] + cfg.return_margin <= turn.day_left:
                best = MaterialPlan(needed, len(targets) * cfg.wall_stones,
                                    construction, back[0], tuple(targets))
        return best
    finally:
        turn.blocked, turn.ours = original, original_units
        nav.gate_guard = guard


def mine(turn, cfg, mem, nav, ledger, hero, want_stone=False, local_only=False,
         deadline=None, stockpile=False, dedicated=False, target_only=None, stone_goal=None,
         stone_sites=None):
    hero = reserve_watch_space(turn, mem, hero, ledger)
    if not hero.space:
        LOG.debug("round=%s worker=%s mining=backpack_full", turn.round, hero.id)
        return False
    vendors = [] if stockpile else [p for p, kind in turn.zones.items() if kind == "vendor"]
    home = mining_home(turn, nav, hero, ledger.reserved)
    # Pick one reachable sale-and-return corridor. Using the worst home walk
    # over every vendor would let a distant enemy-side vendor stop local mining.
    home_dist = (nav.distances_to(home, {hero.pos}, ledger.reserved)
                 if home and not want_stone else {})
    wave_budget = min((turn.base_distance(r.pos) - r.attack_range - cfg.return_margin
                       for r in turn.robots if turn.threatens_us(r)), default=None)
    vendor_home = 0
    if vendors and home and not want_stone:
        corridors = []
        for vendor in vendors:
            home_legs = [home_dist[q] for q in neighbours(vendor) if q in home_dist]
            route = nav.approach(hero, [vendor], ledger.reserved)
            if route and home_legs:
                corridors.append((route[0] + max(home_legs), vendor, max(home_legs)))
        if not corridors:
            LOG.debug("round=%s worker=%s mining=no_vendor_return_route", turn.round, hero.id)
            return False
        _, vendor, vendor_home = min(corridors)
        vendors = [vendor]
    sale_dist = nav.distances_to(vendors, {hero.pos}, ledger.reserved) if vendors and not want_stone else {}
    end = min(70 - cfg.return_margin - vendor_home,
              deadline if deadline is not None and turn.tick < deadline else 70)
    options, skipped = [], Counter()
    for p, kind in turn.zones.items():
        if target_only is not None and p != target_only:
            continue
        if turn.mine_remain.get(p) is not None and turn.mine_remain[p] <= 0:
            skipped['mine_exhausted'] += 1
            continue
        if kind not in ORES:
            continue
        if p in mem.collect_failures:
            skipped["recent_collect_failure"] += 1
            continue
        if mem.movement.avoids(hero.id, p):
            skipped["movement_retry_cooldown"] += 1
            continue
        if want_stone and kind != "stone":
            skipped["needs_stone"] += 1
            continue
        if local_only and not turn.adjacent(hero.pos, p):
            skipped["not_adjacent"] += 1
            continue
        value = 1 if want_stone else max(1 if stockpile else 0, turn.prices.get(kind, 0))
        if not value:
            skipped["no_positive_price"] += 1
            continue
        arrival = nav.approach(hero, [p], ledger.reserved, with_endpoint=True)
        if arrival is None:
            skipped["unreachable"] += 1
            continue
        route, cell = arrival[:2], arrival[2]
        capacity = hero.space
        if want_stone and stone_goal is not None:
            capacity = min(capacity, max(0, stone_goal - hero.inventory['stone']))
        amount, claimants = mining_yield(turn, mem, nav, ledger, hero, p, route[0], capacity)
        if amount <= 0:
            skipped["other_worker_exhausts_mine"] += 1
            continue
        # The sale and home legs must start where this outbound walk ends.
        sale_walk = sale_dist.get(cell)
        material = None
        if want_stone:
            material = wall_material_plan(turn, cfg, mem, nav, ledger, hero, cell, route[0],
                                          amount, stone_goal,
                                          list(stone_sites) if stone_sites is not None else sorted(ledger.wall_cells))
            if material is None:
                skipped['wall_material_deadline'] += 1
                continue
            amount = material.amount
        if stockpile and home and not dedicated and (turn.is_day or wave_budget is not None):
            home_walk = home_dist.get(cell)
            if home_walk is None:
                continue
            budget = turn.day_left - cfg.return_margin if turn.is_day else wave_budget
            amount = min(amount, budget - route[0] - home_walk)
            if amount <= 0:
                continue
        if not want_stone and vendors:
            if sale_walk is None:
                skipped["no_sale_route"] += 1
                continue
            sale_actions = len({k for k in ORES if hero.inventory[k] and turn.prices.get(k, 0) > 0} | {kind})
            time_left = end - turn.tick - route[0] - sale_walk - sale_actions
            amount = min(amount, time_left)
            if amount <= 0:
                skipped["sale_deadline"] += 1
                continue
        # Amortize one sale over a whole backpack run, not over every ten-unit
        # deposit. Walking to the next mine is charged in full to its yield.
        batch = min(hero.capacity, cfg.sell_batch_max)
        score = value * amount / (amount + route[0] +
                                 (amount * (sale_walk + 1) / max(1, batch) if sale_walk is not None else 0))
        options.append(MineOption(p, route, score, amount, claimants, cell=cell, material=material))
    if not options:
        ledger.explain(hero.id, "no_mining_candidate", skipped=dict(skipped),
                       day_left=turn.day_left, deadline=deadline, free_space=hero.space)
        LOG.debug("round=%s worker=%s mining=no_candidate skipped=%s day_left=%s deadline=%s",
                  turn.round, hero.id, dict(skipped), turn.day_left, deadline)
        mem.mine_targets.pop(hero.id, None)
        return False
    order = lambda o: (-o.score, o.route[0], turn.base_distance(o.target), o.target)
    best = min(options, key=order)
    previous = next((o for o in options if o.target == mem.mine_targets.get(hero.id)), None)
    # Finish a productive deposit, including its last few units. Replan only
    # when it vanishes, becomes inaccessible/unprofitable, or cannot meet dusk.
    comparable = [o for o in options if comparable_score(o.score, best.score)]
    chosen = (previous if previous and previous.score >= .65 * best.score else
              min(prefer_separate_sector(turn, hero, comparable), key=order))
    preferred = preferred_stock(turn, cfg, mem, ledger, hero) if not want_stone else set()
    # Never chase a shortage deposit whose present yield is substantially worse.
    news_options = [o for o in comparable if turn.zones[o.target] in preferred]
    if news_options:
        chosen = min(prefer_separate_sector(turn, hero, news_options), key=order)
    target, route = chosen.target, chosen.route
    action = command("collect", target) if route[1] is None else command("move", route[1])
    if ledger.add(hero.id, action):
        ledger.explain(hero.id, "mine_wall_material" if want_stone else "mine_for_later" if stockpile else "mine_for_sale",
                       target=target, route_steps=route[0], candidate_count=len(options),
                       skipped=dict(skipped), selected_score=chosen.score, best_score=best.score,
                       retained_previous=chosen is previous, continuation=target_only is not None,
                       retention_ratio=None if target_only is not None else 0.65,
                       free_space=hero.space, day_left=turn.day_left, deadline=deadline,
                       return_margin=cfg.return_margin, mining_sector=mining_sector(turn, hero),
                       target_sector=target_sector(turn, target), remaining_ore=remaining_ore(turn, mem, target),
                       expected_units=chosen.amount, claimed_by=chosen.claimants,
                       collection_cell=chosen.cell,
                       construction_steps=chosen.material.construction_steps if chosen.material else None,
                       return_steps=chosen.material.return_steps if chosen.material else None,
                       construction_targets=chosen.material.targets if chosen.material else ())
        changed = mem.mine_targets.get(hero.id) != target
        mem.mine_targets[hero.id] = target
        ledger.mine_claims[hero.id] = target
        ledger.mine_plans[hero.id] = route[0], chosen.amount
        material_details = ({'stone_sites': tuple(stone_sites) if stone_sites is not None
                             else tuple(sorted(ledger.wall_cells))} if want_stone else {})
        ledger.remember_work(hero, 'mine', target, ore=turn.zones[target],
                             want_stone=want_stone, stockpile=stockpile,
                             deadline=deadline,
                             stone_goal=chosen.material.goal if chosen.material else stone_goal,
                             **material_details)
        record_target(turn, hero, target, route,
                      "wall_material" if want_stone else "carry_for_later" if stockpile else "sell_today", changed)
        return True
    return False


def earn(turn, cfg, mem, nav, ledger, hero, deadline=None, force_sale=False, allow_spare=True,
         vendor_only=None):
    counts = sale_inventory(turn, mem, hero)
    held = hold_inventory(turn, cfg, mem, ledger, hero, counts)
    counts = {k: n - held.get(k, 0) for k, n in counts.items() if n > held.get(k, 0)}
    cashout = cashout_ores(turn, cfg, mem, hero, counts)
    if held:
        ledger.explain(hero.id, "hold_ore_for_news", held=held, prices=turn.prices,
                       tomorrow=turn.day + 1)
    ores = list(counts)
    total = sum(counts[k] for k in ores)
    if not total:
        mem.sale_workers.discard(hero.id)
        mem.sale_targets.pop(hero.id, None)
    if not turn.is_day:
        if mine(turn, cfg, mem, nav, ledger, hero, stockpile=True):
            return True
        return allow_spare and spare_mine(turn, cfg, mem, nav, ledger, hero)
    if hero.id in mem.sold_workers and hero.id not in mem.sale_workers and not force_sale and not cashout:
        if turn.tick < cfg.economy_rounds and mine(turn, cfg, mem, nav, ledger, hero, stockpile=True, local_only=True):
            return True
        if allow_spare and not (turn.tick >= cfg.economy_rounds and total) and spare_mine(turn, cfg, mem, nav, ledger, hero):
            return True
        # A completed daily sale forbids another vendor visit, not useful
        # repositioning. Leave the vendor and return to a base/operator area.
        home, exact = return_destination(turn, nav, ledger, hero)
        if home:
            back = (nav.search(hero, home, ledger.reserved) if exact
                    else nav.approach(hero, home, ledger.reserved))
            if back and back[1] is not None:
                return ledger.add(hero.id, command("move", back[1]))
        return False
    vendors = [p for p, k in turn.zones.items() if k == "vendor"]
    options = [(r[0] != 0, p != mem.sale_targets.get(hero.id), r[0], p, r) for p in vendors
               if (vendor_only is None or p == vendor_only)
               and (force_sale or cashout or hero.id not in mem.sold_workers or p == mem.sale_targets.get(hero.id))
               and not mem.movement.avoids(hero.id, p)
               and (r := nav.approach(hero, [p], ledger.reserved)) is not None]
    choice = min(options, default=None)
    vendor, route = (choice[3], choice[4]) if choice else (None, None)
    due = False
    sale_fits = False
    home, exact = return_destination(turn, nav, ledger, hero)
    if route:
        trip = via(nav, hero, [[vendor], home], ledger.reserved, final_exact=exact,
                   future_return=turn.tick < cfg.economy_rounds) if home else route[0]
        sale_fits = trip is not None and trip + len(ores) + cfg.return_margin <= turn.day_left
        due = (deadline is not None and turn.tick <= deadline
               and deadline - turn.tick <= route[0] + len(ores)
               or trip is not None and turn.day_left <= trip + len(ores) + cfg.return_margin)

    def sell():
        if not ores or route is None or not sale_fits:
            return False
        kind = max(cashout or ores, key=lambda k: counts[k] * turn.prices[k])
        action = (command("sell", name=kind, num=counts[kind]) if route[1] is None
                  else command("move", route[1]))
        if ledger.add(hero.id, action):
            ledger.explain(hero.id, "sell_surplus_ore", vendor=vendor, route_steps=route[0],
                           kind=kind, quantity=counts[kind], price=turn.prices[kind],
                           news_cashout=kind in cashout, held=held,
                           sale_fits=sale_fits, due=due, force_sale=force_sale,
                           day_left=turn.day_left, return_margin=cfg.return_margin)
            mem.sale_workers.add(hero.id)
            if route[1] is None:
                mem.sold_workers.add(hero.id)
                if kind in cashout:
                    mem.market_cashouts.add((turn.day, hero.id, kind))
            mem.sale_targets[hero.id] = vendor
            mem.mine_targets.pop(hero.id, None)
            ledger.remember_work(hero, 'sell', vendor)
            return True
        return False

    if total and (cashout or not hero.space or due or force_sale or hero.id in mem.sale_workers):
        if sell():
            return True
    # A liquidation request must not silently turn into another mining trip.
    if force_sale:
        return False
    if mine(turn, cfg, mem, nav, ledger, hero, deadline=deadline):
        return True
    if sell():
        return True
    # Carry an unsellable late load home before starting any new trip. Once
    # there, the final daytime pass may use adjacent work without moving out.
    if allow_spare and turn.tick >= cfg.economy_rounds and total and not sale_fits and home:
        back = (nav.search(hero, home, ledger.reserved) if exact
                else nav.approach(hero, home, ledger.reserved))
        if back and back[1] is not None:
            return ledger.add(hero.id, command("move", back[1]))
    # A pause before a scheduled build/shop phase is not spare time: even one
    # extra trip can change who reaches a narrow construction entrance first.
    spare_allowed = (allow_spare and not (turn.tick >= cfg.economy_rounds and total)
                     and (deadline is None or deadline >= 70 or turn.tick >= deadline))
    if spare_allowed and spare_mine(turn, cfg, mem, nav, ledger, hero):
        return True
    # When today's sale is impossible, keep the load and head home. This also
    # covers workers left without a reachable weapon assignment.
    if allow_spare and total and not sale_fits and home:
        back = (nav.search(hero, home, ledger.reserved) if exact
                else nav.approach(hero, home, ledger.reserved))
        if back and back[1] is not None:
            return ledger.add(hero.id, command("move", back[1]))
    return False


def night_mine(turn, cfg, mem, nav, ledger, hero, dedicated=False):
    """Stockpile until daylight; exclude danger cells from the entire route."""
    threats = [r for r in turn.robots if turn.threatens_us(r)]
    if not dedicated and any(turn.base_distance(r.pos) <= max(cfg.task_danger_radius, r.attack_range + 2)
                             for r in threats):
        if turn.station:
            route = nav.approach(hero, turn.station.cells, ledger.reserved)
            if route and route[1] is not None:
                return ledger.add(hero.id, command("move", route[1]))
        return False
    original = turn.blocked
    try:
        danger = set()
        # Even an opponent-bound robot makes a poor mining neighbour. Do not
        # approach or route through its range merely because our base is safe.
        for robot in turn.robots:
            radius = robot.attack_range + 2
            danger.update((x, y)
                          for x in range(max(0, robot.pos[0] - radius), min(turn.width, robot.pos[0] + radius + 1))
                          for y in range(max(0, robot.pos[1] - radius), min(turn.height, robot.pos[1] + radius + 1)))
        turn.blocked = original | danger
        # A threatened unassigned worker retreats instead of starting a new run.
        if hero.pos in danger:
            turn.blocked = original
            if turn.station:
                route = nav.approach(hero, turn.station.cells, ledger.reserved)
                if route and route[1] is not None:
                    return ledger.add(hero.id, command("move", route[1]))
            return False
        return mine(turn, cfg, mem, nav, ledger, hero, stockpile=True, dedicated=dedicated)
    finally:
        turn.blocked = original
