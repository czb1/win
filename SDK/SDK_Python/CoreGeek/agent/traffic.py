"""Daytime passage ownership, ahead of ordinary worker action allocation."""
from collections import deque

from .commands import command
from .model import ORES, distance, neighbours
from .navigation import check_time
from .economy_plan import late_logistics_ready


def _path(turn, nav, start, goals, blocked):
    """Planning-only BFS. Its steps must still pass the real movement ledger."""
    parents = {start: None}
    queue = deque([start])
    goals = set(goals) - blocked
    while queue:
        if len(parents) % 32 == 0:
            check_time(nav.deadline)
        p = queue.popleft()
        if p in goals:
            path = []
            while p is not None:
                path.append(p)
                p = parents[p]
            return path[::-1]
        for q in neighbours(p):
            if turn.inside(q) and q not in blocked and q not in parents:
                parents[q] = p
                queue.append(q)
    return None


def transit_cells(turn, nav, ledger):
    """Articulation cells of the eight-neighbour map, without friendly bodies.

    Iterative Tarjan traversal avoids recursion limits and a BFS per square.
    Blueprint gate cells are excluded as well, even before the ring is built.
    """
    blocked = turn.blocked - {h.pos for h in turn.heroes}
    # The shared pioneer post can close the alternative inside branch. An
    # idle worker must not park in the only remaining entrance branch merely
    # because removing every friendly body makes that branch look optional.
    blocked |= {post for uid, post in ledger.operator_posts.items()
                if (actor := turn.units.get(uid)) and
                actor.kind == 'pioneer'}
    key = frozenset(blocked), frozenset(ledger.wall_cells)
    cached = getattr(nav, '_transit_cache', None)
    if cached and cached[0] == key:
        return cached[1]
    found, low, parent, children, cuts = {}, {}, {}, {}, set()
    for x in range(turn.width):
        check_time(nav.deadline)
        for y in range(turn.height):
            start = x, y
            if start in blocked or start in found:
                continue
            found[start] = low[start] = len(found)
            children[start] = 0
            stack = [(start, iter(neighbours(start)))]
            while stack:
                p, iterator = stack[-1]
                q = next(iterator, None)
                if q is None:
                    stack.pop()
                    if p in parent:
                        above = parent[p]
                        low[above] = min(low[above], low[p])
                        if above in parent and low[p] >= found[above]:
                            cuts.add(above)
                    elif children[p] > 1:
                        cuts.add(p)
                elif turn.inside(q) and q not in blocked:
                    if q not in found:
                        if len(found) % 32 == 0:
                            check_time(nav.deadline)
                        parent[q] = p
                        children[p] += 1
                        children[q] = 0
                        found[q] = low[q] = len(found)
                        stack.append((q, iter(neighbours(q))))
                    elif q != parent.get(p):
                        low[p] = min(low[p], found[q])
    walls = ledger.wall_cells
    # Only holes bounded by blueprint walls are gates; do not invent a closed
    # perimeter for a sparse explicit layout.
    for wall in walls:
        for p in neighbours(wall):
            x, y = p
            if p not in walls and (((x-1, y) in walls and (x+1, y) in walls)
                                  or ((x, y-1) in walls and (x, y+1) in walls)):
                cuts.add(p)
    # Both inner neighbours can be usable routes individually while parking
    # on either one blocks the supplier together with the reserved gunner post.
    # Keep the inner mouth clear even when it is not a graph articulation.
    from .projectiles import wall_gates
    from .wall_watch import geometry
    inside, _ = geometry(turn, walls)
    cuts.update(p for gate in wall_gates(turn, walls) for p in neighbours(gate) if p in inside)
    nav._transit_cache = key, cuts
    return cuts


def parking_goals(turn, nav, ledger, hero, *, near_home=False, forbidden=()):
    excluded = (transit_cells(turn, nav, ledger) | ledger.wall_cells | ledger.tower_cells
                | set(ledger.operator_posts.values()) | ledger.reserved | set(forbidden)
                | set(ledger.traffic_forbidden.get(hero.id, ())))
    blocked = turn.blocked - {hero.pos}
    return {(x, y) for x in range(turn.width) for y in range(turn.height)
            if (p := (x, y)) not in excluded and p not in blocked
            and (turn.base_distance(p) <= 5 if near_home else distance(hero.pos, p) <= 4)
            and not any(turn.threatens_us(r) and distance(p, r.pos) <= r.attack_range + 1
                        for r in turn.robots)}


def park_idle_worker(turn, nav, ledger, hero):
    if (not turn.is_day or hero.id in ledger.used
            or ledger.plans.get(hero.id, {}).get('reason') == 'defence_recall'):
        return False
    goals = parking_goals(turn, nav, ledger, hero)
    route = nav.search(hero, goals, ledger.reserved)
    if route is None:
        return False
    if route[1] is None:
        ledger.explain(hero.id, 'off_route_standby', post=hero.pos)
        return True
    if ledger.add(hero.id, command('move', route[1])):
        ledger.explain(hero.id, 'leave_idle_transit_cell', steps=route[0])
        return True
    return False


def _intent(turn, mem, ledger, hero, returning):
    request = ledger.traffic_requests.get(hero.id)
    if request:
        return request['signature'], request['goals'], request['urgency']
    post = ledger.operator_posts.get(hero.id)
    if hero.id in returning and post is not None:
        return ('recall', post), {post}, 0
    job = mem.daytime_jobs.get(hero.id, {})
    target, kind = job.get('target'), job.get('kind')
    if target is None:
        return None
    target = tuple(target)
    if kind in ('mine', 'spare') and (turn.zones.get(target) not in ORES
                                     or (turn.mine_remain.get(target) is not None and turn.mine_remain[target] <= 0) or not hero.space):
        return None
    if kind in ('mine', 'spare') and not job.get('want_stone'):
        from .economy_plan import development_pending
        run = job.get('run')
        if run and (run['day'] != turn.day or turn.tick >= run['end_tick']
                    or hero.inventory[job['ore']] >= run['collect_goal']):
            return None
        # Match ordinary job continuation: a mining trip superseded by today's
        # construction/purchase phase has no right to borrow a builder.
        if (not run and hero.id != ledger.income_worker_id
                and turn.tick >= min(ledger.cfg.economy_rounds, mem.preparation_tick)
                and development_pending(turn, ledger.cfg, mem, ledger.tower_cells, ledger.wall_cells)):
            return None
    if target in mem.collect_failures and kind in ('mine', 'spare'):
        return None
    if mem.movement.avoids(hero.id, target):
        return None
    if kind == 'sell' and not any(hero.inventory[k] for k in ORES):
        return None
    if kind in ('buy', 'robot_buy') and (turn.zones.get(target) != 'weaponShop'
                                       or not hero.space or ledger.gold < turn.shop.get(job.get('name'), 0)):
        return None
    if kind == 'use' and not hero.inventory[job.get('name')]:
        return None
    if kind not in ('mine', 'spare', 'buy', 'robot_buy', 'sell', 'use', 'build'):
        return None
    urgency = (1 if kind == 'use' or kind == 'buy' and job.get('name') in ('Medicine', 'WallFixer')
               else 2 if kind in ('buy', 'robot_buy', 'sell', 'build') else 3)
    return (kind, target, job.get('name')), set(neighbours(target)) - {target}, urgency


def _hold(turn, mem, ledger, hero, reason, **facts):
    ledger.used.add(hero.id)
    ledger.traffic_holds.add(hero.id)
    if hero.id in mem.daytime_jobs:
        ledger.work_jobs[hero.id] = mem.daytime_jobs[hero.id].copy()
    ledger.explain(hero.id, reason, **facts)


def coordinate_traffic(turn, cfg, mem, nav, ledger, returning):
    """Select a passage owner by urgency then actual BFS distance.

    A lease belongs to an ongoing job. Yielders cannot re-enter its corridor
    until the owner passes, changes jobs, disappears, or stops progressing.
    No predicted vacancy authorizes a same-turn move into an occupied cell.
    """
    if not turn.is_day:
        mem.traffic_leases.clear()
        return
    ready = late_logistics_ready(turn, cfg)
    if not ready and not ledger.traffic_requests:
        mem.traffic_leases.clear()
        return
    nav.traffic_forbidden = ledger.traffic_forbidden
    heroes = {h.id: h for h in turn.heroes}
    static = turn.blocked - {h.pos for h in turn.heroes}
    intents = {}
    for uid, hero in heroes.items():
        if not ready and uid not in ledger.traffic_requests and uid not in returning:
            continue
        intent = _intent(turn, mem, ledger, hero, returning)
        if intent and uid not in ledger.used:
            signature, goals, urgency = intent
            path = _path(turn, nav, hero.pos, goals, static | ledger.reserved)
            if path and signature[0] not in ('recall', 'wall_supply_home'):
                from .mining import return_destination
                home, exact = return_destination(turn, nav, ledger, hero, mem)
                home_goals = (set(home) if exact else
                              {p for target in home for p in neighbours(target)} - set(home))
                back = _path(turn, nav, path[-1], home_goals, static | ledger.reserved) if home else [path[-1]]
                if back is None or len(path) + len(back) - 1 + cfg.return_margin > turn.day_left:
                    continue
            if path and len(path) > 1:
                intents[uid] = signature, goals, urgency, path

    expired = set()
    for uid, lease in list(mem.traffic_leases.items()):
        intent = intents.get(uid)
        hero = heroes.get(uid)
        complete = (intent is not None and hero.pos not in lease['corridor'][:-1]
                    and len(intent[3]) - 1 <= lease['remaining'])
        if (intent is None or intent[0] != lease['job'] or complete
                or turn.round - lease['progress_round'] > 6):
            del mem.traffic_leases[uid]
            expired.add(uid)
            continue
        if hero.pos != lease['position']:
            lease.update(position=hero.pos, progress_round=turn.round)

    # Existing leases keep priority over equally urgent new trips. A real
    # recall may preempt economic traffic; distance breaks other ties.
    ordered = sorted(intents, key=lambda uid: (intents[uid][2], uid not in mem.traffic_leases,
                                               len(intents[uid][3]), uid))
    ranks = {uid: i for i, uid in enumerate(ordered)}
    owned = set()
    for uid in ordered:
        if uid in ledger.used or uid in expired:
            continue
        hero = heroes[uid]
        signature, goals, urgency, path = intents[uid]
        lease = mem.traffic_leases.get(uid)
        if lease is None:
            blockers = [h for h in heroes.values() if h.id != uid and h.pos in path[1:]]
            open_route = nav.search(hero, goals, ledger.reserved)
            if open_route is not None:
                # Reserve a forthcoming bottleneck before a neighbour's normal
                # action can occupy it. An ordinary open-field route needs none.
                gates = [i for i, p in enumerate(path[1:], 1)
                         if p in transit_cells(turn, nav, ledger)]
                if not gates:
                    continue
                last = max(gates)
                blockers = [h for h in heroes.values() if h.id != uid
                            and any(distance(h.pos, path[i]) <= 3 for i in gates)]
            elif blockers:
                last = max(path.index(h.pos) for h in blockers)
                if signature[0] == 'wall_supply_home':
                    last = max([last] + [i for i, p in enumerate(path[1:], 1)
                                        if p in transit_cells(turn, nav, ledger)])
            if not blockers or any(h.id in ledger.used or h.health <= 165 and h.inventory['Medicine']
                                   for h in blockers):
                continue
            end = min(len(path)-1, last+2)
            lease = dict(job=signature, corridor=path[:end+1], remaining=len(path)-1-end,
                         blockers=[h.id for h in blockers], position=hero.pos,
                         progress_round=turn.round, aside={})
        corridor = set(lease['corridor'])
        if corridor & owned:
            mem.traffic_leases.pop(uid, None)
            continue
        # A request may never evict a higher-priority opposing trip.
        if any(b in ranks and ranks[b] < ranks[uid] for b in lease['blockers']):
            mem.traffic_leases.pop(uid, None)
            continue
        owned.update(corridor)
        mem.traffic_leases[uid] = lease
        for bid in lease['blockers']:
            blocker = heroes.get(bid)
            if blocker is None:
                continue
            ledger.traffic_forbidden.setdefault(bid, set()).update(corridor)
            if blocker.id in ledger.used:
                continue
            if blocker.pos in corridor:
                goals_aside = parking_goals(turn, nav, ledger, blocker, forbidden=goals | corridor)
                aside = lease.setdefault('aside', {})
                previous = aside.get(bid)
                if previous in goals_aside:
                    goals_aside = {previous}
                # Some inner aisles have no sideways exit: the blocker must
                # first walk through the corridor to reach a safe pocket. Only
                # this departure may cross its own lease; real occupancy and
                # all other requests/reservations still apply at every step.
                forbidden = ledger.traffic_forbidden[bid]
                nav.traffic_forbidden[bid] = forbidden - corridor
                try:
                    route = nav.search(blocker, goals_aside, ledger.reserved, with_endpoint=True)
                    accepted = bool(route and route[1] is not None
                                    and ledger.add(bid, command('move', route[1])))
                finally:
                    nav.traffic_forbidden[bid] = forbidden
                if accepted:
                    aside[bid] = route[2]
                    _hold(turn, mem, ledger, blocker, 'yield_blocked_route', requester=uid,
                          previous=blocker.pos, landing=route[1], target=signature[1])
            elif signature[0] == 'wall_supply_home' and bid in returning:
                _hold(turn, mem, ledger, blocker, 'keep_wall_passage_clear', requester=uid,
                      corridor=lease['corridor'])
        # Preserve a blocked buy/mine/sell job through this turn. Otherwise
        # normal fallback would replace it with an unrelated homeward action.
        if nav.search(hero, goals, ledger.reserved) is None:
            request = ledger.traffic_requests.get(uid, {})
            if request.get('local_work'):
                continue  # The supplier may use its adjacent paid coupon now.
            if request.get('advance') and len(path) > 1:
                step = nav.search(hero, {path[1]}, ledger.reserved)
                if step and step[0] == 1 and ledger.add(uid, command('move', path[1])):
                    _hold(turn, mem, ledger, hero, 'approach_wall_passage',
                          target=sorted(goals), blockers=lease['blockers'])
                    continue
            _hold(turn, mem, ledger, hero, 'wait_for_passage', blockers=lease['blockers'],
                  target=signature[1], urgency=urgency, route_steps=len(path)-1)
