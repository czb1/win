"""Last-chance daytime work without giving up recall or watch ownership."""
import logging
from dataclasses import replace

from .commands import command
from .economy import use_inventory
from .mining import spare_mine, return_destination, sale_inventory
from .model import distance, neighbours
from .wall_watch import geometry
from .economy_plan import late_logistics_ready

LOG = logging.getLogger(__name__)


def guard_station_with_pioneer(turn, mem, nav, ledger):
    """Without guns, wait on a reachable rear neighbour ready to heal the base."""
    hero, station = turn.pioneer, turn.station
    if (not hero or not station or turn.weapons or hero.id in ledger.used
            or turn.is_day and turn.phase_task):
        return False
    from .robot_assault import enemy_base
    base = enemy_base(turn)
    centre = (2 * station.pos[0] + 1, 2 * station.pos[1] - 1)
    enemy = (2 * base.pos[0] + 1, 2 * base.pos[1] - 1) if base else (turn.width - 1, turn.height - 1)
    direction = enemy[0] - centre[0], enemy[1] - centre[1]
    threats = [r for r in turn.robots if turn.threatens_us(r)]

    def risk(point):
        return sum(r.power for r in threats if distance(point, r.pos) <= r.attack_range + 1)

    danger = {(x, y) for r in threats
              for x in range(max(0, r.pos[0] - r.attack_range - 1),
                             min(turn.width, r.pos[0] + r.attack_range + 2))
              for y in range(max(0, r.pos[1] - r.attack_range - 1),
                             min(turn.height, r.pos[1] + r.attack_range + 2))}
    reserved = ledger.reserved | set(ledger.operator_posts.values())
    forbidden = reserved | ledger.tower_cells | ledger.wall_cells
    posts = {p for cell in station.cells for p in neighbours(cell)
             if turn.inside(p) and p not in station.cells and p not in forbidden}
    options = []
    for avoid in (danger, set()):
        for post in sorted(posts - avoid):
            route = nav.search(hero, {post}, reserved | avoid)
            if route is None or risk(route[1] or hero.pos) > risk(hero.pos):
                continue
            clearance = min((distance(post, r.pos) - r.attack_range for r in threats), default=0)
            facing = sum((2 * post[i] - centre[i]) * direction[i] for i in (0, 1))
            options.append((risk(post), -clearance, facing, route[0], post, route))
        if options:
            break
    mem.return_targets.pop(hero.id, None)
    mem.return_posts.pop(hero.id, None)
    if not options:
        ledger.used.add(hero.id)
        ledger.reserved.add(hero.pos)
        ledger.explain(hero.id, 'pioneer_station_guard_blocked', station_health=station.health)
        return True
    _, _, _, _, post, route = min(options)
    if route[1] is not None and not nav.move_along(
            hero, ledger, route, {post}, reserved | danger, prefer_vehicle=False,
            max_rounds=route[0], returning=True):
        return False
    mem.pioneer_wait_post = post
    ledger.used.add(hero.id)
    if route[1] is None:
        ledger.reserved.add(post)
    ledger.explain(hero.id, 'pioneer_station_guard', post=post, route_steps=route[0],
                   station_health=station.health, heal_below=500)
    return True


def park_idle_pioneer(turn, cfg, mem, nav, ledger, hero):
    """Wait outside the rear wall; a gate is a transit cell, never a parking spot."""
    if (not turn.is_day or turn.phase_task or not turn.station or hero.id in ledger.used
            or hero.id in mem.return_targets):
        return False
    if not turn.weapons:
        return guard_station_with_pioneer(turn, mem, nav, ledger)
    home, exact = return_destination(turn, nav, ledger, hero)
    if not home:
        return False
    sites = ledger.wall_cells or turn.station.cells
    right = turn.station.pos[0] < turn.width / 2
    rear = (min if right else max)(x for x, _ in sites)
    danger = {p for r in turn.robots if turn.threatens_us(r)
              for p in ((x, y) for x in range(max(0, r.pos[0] - r.attack_range - 2),
                                               min(turn.width, r.pos[0] + r.attack_range + 3))
                                for y in range(max(0, r.pos[1] - r.attack_range - 2),
                                               min(turn.height, r.pos[1] + r.attack_range + 3)))}
    from .traffic import transit_cells
    forbidden = ledger.tower_cells | ledger.wall_cells | ledger.reserved | danger
    if late_logistics_ready(turn, cfg):
        forbidden |= transit_cells(turn, nav, ledger) | set(ledger.traffic_forbidden.get(hero.id, ()))
    forbidden.update(ledger.operator_posts.values())
    options = []
    for x in range(max(0, rear - 3), min(turn.width, rear + 4)):
        if not (x < rear if right else x > rear):
            continue
        for y in range(max(0, turn.station.pos[1] - 5), min(turn.height, turn.station.pos[1] + 6)):
            point = x, y
            if point in forbidden or not 3 <= turn.base_distance(point) <= 5:
                continue
            route = nav.search(hero, {point}, forbidden)
            if route is not None:
                options.append((point != mem.pioneer_wait_post, route[0],
                                turn.base_distance(point), point, route))
    for _, _, _, point, route in sorted(options):
        original = turn.blocked
        try:
            turn.blocked = original - {hero.pos}
            proxy = replace(hero, pos=point)
            back = (nav.search(proxy, home, danger) if exact else nav.approach(proxy, home, danger))
            if back is None or route[0] + back[0] + 2 * cfg.return_margin >= turn.day_left:
                continue
            # Reject articulation cells even outside the blueprint, e.g. a
            # one-cell gap between a mine and a wall. Neighbours that connected
            # through this square must remain connected when it is occupied.
            turn.blocked |= {point}
            adjacent = [p for p in neighbours(point) if turn.inside(p)
                        and p not in turn.blocked and p not in ledger.reserved]
            if adjacent:
                probe = replace(hero, pos=adjacent[0])
                if any(nav.search(probe, {p}, ledger.reserved) is None for p in adjacent[1:]):
                    continue
        finally:
            turn.blocked = original
        mem.pioneer_wait_post = point
        if route[1] is None:
            ledger.explain(hero.id, 'outside_daytime_standby', post=point)
            return True
        if ledger.add(hero.id, command('move', route[1])):
            ledger.explain(hero.id, 'outside_daytime_standby', post=point,
                           route_steps=route[0], return_steps=back[0])
            return True
    return False


def finish_daytime_work(turn, cfg, mem, nav, ledger, returning):
    """Use only unspent actions; reserve return time from the actual work tile.

    A blocked recall may use an adjacent resource, never start another trip.
    An empty watcher at home can make a short inside trip, then retry stock
    preparation next turn. A stocked watcher works until its recall is due.
    """
    if not turn.is_day:
        return
    for hero in turn.workers:
        if hero.id in ledger.used:
            continue
        watcher = hero.id == mem.wall_watch_id
        role = ('watch' if watcher else 'returning' if hero.id in returning
                else 'preparation' if hero.id in mem.preparation_workers else 'economy')
        reason = ledger.daytime_waits.get(hero.id, 'no_primary_action')
        home, exact = return_destination(turn, nav, ledger, hero)
        reserved = set(ledger.reserved)
        if watcher:
            inside, _ = geometry(turn, ledger.wall_cells)
            home = {p for wall in ledger.wall_cells for p in neighbours(wall)} & inside
            exact = True
            # Only current operator posts are reservations, not a stale shared
            # post left in memory after the daytime assignment failed.
            reserved.update(ledger.operator_posts.values())
            if hero.pos in inside:
                reserved.update((x, y) for x in range(turn.width) for y in range(turn.height)
                                if (x, y) not in inside)
        back = ((nav.search(hero, home, reserved) if exact else nav.approach(hero, home, reserved))
                if home else None)
        # An adjacent paid use changes no position. Keep enough time to return,
        # but do not suppress it merely because the return path is blocked.
        if (back is None or 1 + back[0] + cfg.return_margin <= turn.day_left
                or watcher and back[0] == 0):
            if use_inventory(turn, nav, ledger, hero, local_only=True, mem=mem):
                LOG.info('round=%s worker=%s daytime_role=%s fallback=local_use reason=%s',
                         turn.round, hero.id, role, reason)
                continue
        watch_due = back is None or turn.day_left <= back[0] + cfg.return_margin + 2
        if reason == 'watch_ready' and watch_due:
            outcome = 'stocked_watch_standby'
        elif not hero.space:
            outcome = 'backpack_full'
        else:
            # A stocked watcher with daylight left may use the same bounded
            # inside fallback as an empty watcher. Actual blocked returns only
            # permit collection in place, never an optimistic outward walk.
            max_steps = (2 if hero.id not in returning
                         or watcher and (reason == 'empty_watch' or not watch_due) else 0)
            if back is None:
                max_steps = 0
            if turn.tick >= cfg.economy_rounds and sale_inventory(turn, mem, hero):
                max_steps = 0
            if spare_mine(turn, cfg, mem, nav, ledger, hero, home=home, exact=exact,
                          reserved=reserved, max_steps=max_steps,
                          allow_blocked_home=back is None or hero.id in returning):
                LOG.info('round=%s worker=%s daytime_role=%s fallback=stockpile reason=%s '
                         'day_left=%s return_steps=%s', turn.round, hero.id, role, reason,
                         turn.day_left, back[0] if back else None)
                continue
            outcome = ('no_home_route' if back is None else 'return_deadline'
                       if 1 + back[0] + cfg.return_margin > turn.day_left else 'no_safe_local_work')
        if watcher and reason == 'empty_watch' and back and back[1] is not None:
            if ledger.add(hero.id, command('move', back[1])):
                mem.mine_targets.pop(hero.id, None)
                LOG.info('round=%s worker=%s daytime_role=%s fallback=return reason=%s '
                         'day_left=%s return_steps=%s', turn.round, hero.id, role, outcome,
                         turn.day_left, back[0])
                continue
        if hero.id not in returning and not watcher:
            from .traffic import park_idle_worker
            park_idle_worker(turn, nav, ledger, hero)
        LOG.info('round=%s worker=%s daytime_role=%s idle_reason=%s lock_reason=%s '
                 'day_left=%s return_steps=%s packs=%s free_space=%s',
                 turn.round, hero.id, role, outcome, reason, turn.day_left,
                 back[0] if back else None, hero.inventory['WallFixer'], hero.space)


def yield_idle_worker_for_traffic(turn, mem, nav, ledger, returning):
    """Move an idle worker aside only when it demonstrably opens a route.

    Recheck the occupied map before submitting the move. This is a one-turn
    cooperative yield, not permission to walk through an occupied cell.
    """
    if not turn.is_day or late_logistics_ready(turn, ledger.cfg):
        return
    for blocker in turn.workers:
        if (blocker.id in ledger.used or blocker.id in returning
                or blocker.id == mem.wall_watch_id):
            continue
        for requester in turn.heroes:
            if requester.id == blocker.id or str(requester.id) in ledger.commands:
                continue
            post = ledger.operator_posts.get(requester.id)
            job = mem.daytime_jobs.get(requester.id, {})
            target = job.get('target')
            if (post is None and target is None
                    or requester.id in ledger.used and post is None):
                continue
            goals = ({post} if post is not None else
                     {p for p in neighbours(target)} - {target})
            if nav.search(requester, goals, ledger.reserved) is not None:
                continue
            original = turn.blocked
            try:
                turn.blocked = original - {blocker.pos}
                if nav.search(requester, goals, ledger.reserved) is None:
                    continue
                forbidden = (ledger.tower_cells | ledger.wall_cells
                             | set(ledger.operator_posts.values()) | goals)
                options = []
                for side in neighbours(blocker.pos):
                    if (not turn.inside(side) or side in original or side in ledger.reserved
                            or side in forbidden or any(turn.threatens_us(r)
                            and distance(side, r.pos) <= r.attack_range + 1 for r in turn.robots)):
                        continue
                    turn.blocked = (original - {blocker.pos}) | {side}
                    if nav.search(requester, goals, ledger.reserved) is not None:
                        options.append(side)
                turn.blocked = original
                for side in sorted(options, key=lambda p: (distance(p, requester.pos), p), reverse=True):
                    if ledger.add(blocker.id, command('move', side)):
                        ledger.explain(blocker.id, 'yield_blocked_route', requester=requester.id,
                                       previous=blocker.pos, landing=side, target=post or target)
                        break
                if blocker.id in ledger.used:
                    break
            finally:
                turn.blocked = original
