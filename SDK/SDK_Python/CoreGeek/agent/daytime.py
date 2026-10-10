"""Last-chance daytime work without giving up recall or watch ownership."""
import logging
from dataclasses import replace

from .commands import command
from .economy import use_inventory
from .mining import spare_mine, return_destination, sale_inventory
from .model import neighbours
from .wall_watch import geometry

LOG = logging.getLogger(__name__)


def park_idle_pioneer(turn, cfg, mem, nav, ledger, hero):
    """Wait outside the rear wall; a gate is a transit cell, never a parking spot."""
    if (not turn.is_day or turn.phase_task or not turn.station or hero.id in ledger.used
            or hero.id in mem.return_targets):
        return False
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
    forbidden = ledger.tower_cells | ledger.wall_cells | ledger.reserved | danger
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
    preparation next turn. A stocked watcher keeps its intentional standby.
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
        if reason == 'watch_ready':
            outcome = 'stocked_watch_standby'
        elif not hero.space:
            outcome = 'backpack_full'
        else:
            # A locked operator or blocked watcher must not abandon a recalled
            # route. Only the empty watcher already inside may walk nearby.
            max_steps = (2 if hero.id not in returning or watcher and reason == 'empty_watch' else 0)
            if turn.tick >= cfg.economy_rounds and sale_inventory(turn, mem, hero):
                max_steps = 0
            if spare_mine(turn, cfg, mem, nav, ledger, hero, home=home, exact=exact,
                          reserved=reserved, max_steps=max_steps,
                          allow_blocked_home=hero.id in returning):
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
        LOG.info('round=%s worker=%s daytime_role=%s idle_reason=%s lock_reason=%s '
                 'day_left=%s return_steps=%s packs=%s free_space=%s',
                 turn.round, hero.id, role, outcome, reason, turn.day_left,
                 back[0] if back else None, hero.inventory['WallFixer'], hero.space)
