"""Persistent mining runs and deadline-driven sales, independent of the LLM."""
import logging
from collections import Counter
from dataclasses import replace
from .commands import command
from .model import ORES, distance, neighbours
from .economy_plan import via

LOG = logging.getLogger(__name__)


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


def return_destination(turn, nav, ledger, hero):
    if hero.id in ledger.operator_posts:
        return [ledger.operator_posts[hero.id]], True
    tower = next((w for h, w in ledger.return_pairs if h.id == hero.id), None)
    return (tower.cells if tower else mining_home(turn, nav, hero, ledger.reserved)), False


def spare_mine(turn, cfg, mem, nav, ledger, hero):
    """Use otherwise idle daylight near ore; carry it to a later day's sale."""
    if not turn.is_day or not hero.space:
        LOG.debug("round=%s worker=%s spare_mining=unavailable day=%s free_space=%s",
                  turn.round, hero.id, turn.is_day, hero.space)
        return False
    home, exact = return_destination(turn, nav, ledger, hero)
    if not home:
        LOG.debug("round=%s worker=%s spare_mining=no_return_destination", turn.round, hero.id)
        return False
    options = []
    for target, kind in turn.zones.items():
        if (kind not in ORES or turn.prices.get(kind, 0) <= 0 or target in mem.collect_failures
                or mem.movement.avoids(hero.id, target)):
            continue
        if distance(hero.pos, target) > 3:
            continue
        # Check the return from the actual collection tile, not from a
        # different side of the deposit. At most two steps start a spare trip.
        for cell in neighbours(target):
            route = nav.search(hero, {cell}, ledger.reserved)
            if route is None or route[0] > 2:
                continue
            original = turn.blocked
            try:
                turn.blocked = original - {hero.pos}
                proxy = replace(hero, pos=cell)
                back = (nav.search(proxy, home, ledger.reserved) if exact
                        else nav.approach(proxy, home, ledger.reserved))
            finally:
                turn.blocked = original
            if back is None or route[0] + 1 + back[0] + cfg.return_margin > turn.day_left:
                continue
            options.append((route[0], -turn.prices[kind], back[0], target, cell, route))
    if not options:
        LOG.debug("round=%s worker=%s spare_mining=no_safe_mine_within_two_steps day_left=%s",
                  turn.round, hero.id, turn.day_left)
        return False
    _, _, _, target, _, route = min(options, key=lambda o: o[:5])
    action = command("collect", target) if route[1] is None else command("move", route[1])
    if ledger.add(hero.id, action):
        changed = mem.mine_targets.get(hero.id) != target
        mem.mine_targets[hero.id] = target
        ledger.mine_claims[hero.id] = target
        record_target(turn, hero, target, route, "carry_for_later", changed)
        return True
    return False


def sale_inventory(turn, mem, hero):
    """Only surplus stone can be sold while the wall blueprint needs material."""
    counts = hero.inventory
    counts["stone"] = max(0, counts["stone"] - mem.stone_reserves.get(hero.id, 0))
    return {k: counts[k] for k in ORES if counts[k] and turn.prices.get(k, 0) > 0}


def mine(turn, cfg, mem, nav, ledger, hero, want_stone=False, local_only=False,
         deadline=None):
    if not hero.space:
        LOG.debug("round=%s worker=%s mining=backpack_full", turn.round, hero.id)
        return False
    vendors = [p for p, kind in turn.zones.items() if kind == "vendor"]
    home = mining_home(turn, nav, hero, ledger.reserved)
    # Pick one reachable sale-and-return corridor. Using the worst home walk
    # over every vendor would let a distant enemy-side vendor stop local mining.
    home_dist = nav.distances_to(home, {hero.pos}, ledger.reserved) if home and not want_stone else {}
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
        value = 1 if want_stone else max(0, turn.prices.get(kind, 0))
        if not value:
            skipped["no_positive_price"] += 1
            continue
        route = nav.approach(hero, [p], ledger.reserved)
        if route is None:
            skipped["unreachable"] += 1
            continue
        cells = [q for q in neighbours(p) if q in sale_dist]
        # A distant worker must not chase the last units another worker can
        # exhaust before arrival. Sharing a nearby deposit remains legal.
        remaining = max(1, 10 - mem.mine_collected.get(p, 0))
        others = [h for h in turn.workers if h.id != hero.id
                  and (ledger.mine_claims.get(h.id, mem.mine_targets.get(h.id)) == p)
                  and h.id not in mem.sale_workers]
        for other in others:
            arrival = max(0, distance(other.pos, p) - 1)
            remaining -= max(0, route[0] - arrival)
        if remaining <= 0:
            skipped["other_worker_exhausts_mine"] += 1
            continue
        share = max(1, (remaining + len(others)) // (1 + len(others)))
        amount = min(hero.space, share)
        sale_walk = min((sale_dist[q] for q in cells), default=None)
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
        options.append((score, route[0], turn.base_distance(p), p, route))
    if not options:
        LOG.debug("round=%s worker=%s mining=no_candidate skipped=%s day_left=%s deadline=%s",
                  turn.round, hero.id, dict(skipped), turn.day_left, deadline)
        mem.mine_targets.pop(hero.id, None)
        return False
    best = min(options, key=lambda o: (-o[0], o[1], o[2], o[3]))
    previous = next((o for o in options if o[3] == mem.mine_targets.get(hero.id)), None)
    # Finish a productive deposit, including its last few units. Replan only
    # when it vanishes, becomes inaccessible/unprofitable, or cannot meet dusk.
    chosen = previous if previous and previous[0] >= .65 * best[0] else best
    _, _, _, target, route = chosen
    action = command("collect", target) if route[1] is None else command("move", route[1])
    if ledger.add(hero.id, action):
        changed = mem.mine_targets.get(hero.id) != target
        mem.mine_targets[hero.id] = target
        ledger.mine_claims[hero.id] = target
        record_target(turn, hero, target, route, "wall_material" if want_stone else "sell_today", changed)
        return True
    return False


def earn(turn, cfg, mem, nav, ledger, hero, deadline=None, funding_goal=0, allow_spare=True):
    counts = sale_inventory(turn, mem, hero)
    ores = list(counts)
    total = sum(counts[k] for k in ores)
    if not total:
        mem.sale_workers.discard(hero.id)
        mem.sale_targets.pop(hero.id, None)
    vendors = [p for p, k in turn.zones.items() if k == "vendor"]
    options = [(r[0] != 0, p != mem.sale_targets.get(hero.id), r[0], p, r) for p in vendors
               if not mem.movement.avoids(hero.id, p)
               and (r := nav.approach(hero, [p], ledger.reserved)) is not None]
    choice = min(options, default=None)
    vendor, route = (choice[3], choice[4]) if choice else (None, None)
    due = False
    sale_fits = False
    home, exact = return_destination(turn, nav, ledger, hero)
    if route:
        trip = via(nav, hero, [[vendor], home], ledger.reserved, final_exact=exact) if home else route[0]
        sale_fits = trip is not None and trip + len(ores) + cfg.return_margin <= turn.day_left
        due = (deadline is not None and turn.tick <= deadline
               and deadline - turn.tick <= route[0] + len(ores)
               or trip is not None and turn.day_left <= trip + len(ores) + cfg.return_margin)
    batch = min(hero.capacity, cfg.sell_batch_max,
                max(cfg.sell_batch, 2 * route[0] + 10 if route else cfg.sell_batch))
    value = sum(counts[k] * turn.prices[k] for k in ores)
    funds = ledger.gold < funding_goal <= ledger.gold + value

    def sell():
        if not ores or route is None or not sale_fits:
            return False
        kind = max(ores, key=lambda k: counts[k] * turn.prices[k])
        action = (command("sell", name=kind, num=counts[kind]) if route[1] is None
                  else command("move", route[1]))
        if ledger.add(hero.id, action):
            mem.sale_workers.add(hero.id)
            mem.sale_targets[hero.id] = vendor
            mem.mine_targets.pop(hero.id, None)
            return True
        return False

    if total and (total >= batch or not hero.space or due or funds or hero.id in mem.sale_workers):
        if sell():
            return True
    if mine(turn, cfg, mem, nav, ledger, hero, deadline=deadline):
        return True
    if sell():
        return True
    # A pause before a scheduled build/shop phase is not spare time: even one
    # extra trip can change who reaches a narrow construction entrance first.
    spare_allowed = allow_spare and (deadline is None or deadline >= 70 or turn.tick >= deadline)
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
