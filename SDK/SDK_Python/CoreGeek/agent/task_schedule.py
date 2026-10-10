"""Daylight task scheduling from observed cooldowns, routes and task outcomes.

No task content is available before acceptTask. Learned budgets are explicitly
estimates for a stable task stream, never proof that the next question is equal.
"""
from collections import Counter
from .model import pos, neighbours, DEFENCE_RETURN_TICK
from .mining import return_destination
from .task_skills import shape


def task_descriptor(task):
    return {key: task.get(key) for key in
            ('taskType', 'timeoutRounds', 'scoreReward', 'goldReward')}


def task_signature(contract):
    if not contract.get('family') or not contract.get('example'):
        return None
    return (contract['family'], contract.get('input_kind', 'data'),
            contract.get('kind', 'query'), shape(contract['example']))


def task_budget(task, cfg, outcomes):
    """Accept-to-confirmation budget; return travel is accounted separately."""
    timeout = min(int(task.get('timeoutRounds', cfg.task_max_rounds)), cfg.task_max_rounds)
    fallback = min(cfg.task_min_rounds, timeout) + 1  # confirmation observation
    records = [o for o in outcomes if o.get('point') == pos(task['taskPosition'])]
    descriptor = task_descriptor(task)
    recent = []
    for record in reversed(records):
        if record.get('descriptor') != descriptor:
            break
        if not record.get('signature'):
            # Even a failure before the first document read consumes real time.
            # It must invalidate a previously fast estimate for this stream.
            if not record.get('completionObserved'):
                recent.append(record)
            break
        if (recent
                and record['signature'] != recent[0]['signature']):
            break
        recent.append(record)
        if len(recent) == 3:
            break
    successful = [o['rounds'] for o in recent if o.get('completionObserved')
                  and type(o.get('rounds')) is int and o['rounds'] > 0]
    failed = [o for o in recent if not o.get('completionObserved')]
    if not successful or failed:
        # A failure is a lower bound on needed time, not a fast completion.
        floor = max((o.get('rounds', 0) for o in failed), default=0)
        return dict(rounds=min(timeout + 1, max(fallback, floor)),
                    uncertainty=2, source='failure_fallback' if failed else 'unknown',
                    samples=len(recent))
    # One model reply + one execution turn for repair, plus observed variability.
    uncertainty = max(2, max(successful) - min(successful))
    return dict(rounds=min(timeout + 1, max(successful) + uncertainty),
                uncertainty=uncertainty, source='observed_stream_estimate',
                samples=len(successful))


def wait_cell_safe(turn, nav, ledger, hero, cell, forbidden):
    """Do not park on building/operator sites or cut a transit corridor."""
    if cell in (ledger.wall_cells | ledger.tower_cells | set(ledger.operator_posts.values())):
        return False
    blocked = (turn.blocked - {hero.pos}) | set(forbidden) | {cell}
    adjacent = {p for p in neighbours(cell) if turn.inside(p) and p not in blocked}
    if len(adjacent) < 2:
        return True
    connected = nav.distances_to({min(adjacent)}, vacated={hero.pos},
                                reserved=set(forbidden) | {cell}, exact=True)
    return adjacent <= connected.keys()


def task_options(turn, cfg, mem, nav, ledger, hero, *, switching=False):
    if not turn.is_day or (turn.phase_task and not switching) or hero.id in mem.return_targets:
        return []
    danger = set()
    for robot in turn.robots:
        if turn.threatens_us(robot):
            radius = max(cfg.task_danger_radius, robot.attack_range + 2)
            danger.update((x, y)
                          for x in range(max(0, robot.pos[0] - radius), min(turn.width, robot.pos[0] + radius + 1))
                          for y in range(max(0, robot.pos[1] - radius), min(turn.height, robot.pos[1] + radius + 1)))
    forbidden = set(ledger.reserved) | danger
    if nav.memory:
        forbidden |= nav.memory.blocked(hero.id)
    home, exact = return_destination(turn, nav, ledger, hero)
    back = nav.distances_to(home, vacated={hero.pos}, reserved=forbidden, exact=exact) if home else {}
    options = []
    for task in turn.tasks:
        cooldown = max(0, int(task.get('coldDownRounds', 0)))
        if not task.get('isValid') and not cooldown:
            continue  # exhausted or unavailable; do not invent a refresh
        point = pos(task['taskPosition'])
        if switching and (point == mem.task_point or cooldown or not task.get('isValid')):
            continue
        if nav.memory and nav.memory.avoids(hero.id, point):
            continue
        budget = task_budget(task, cfg, mem.task_outcomes)
        cells = turn.task_cells(task)
        goals = {p for cell in cells for p in neighbours(cell)} - cells
        candidates = []
        for cell in sorted(goals):
            if cell in forbidden or not turn.inside(cell) or home and cell not in back:
                continue
            if switching and any(turn.adjacent(cell, p) for current in turn.tasks
                                 if pos(current["taskPosition"]) == mem.task_point
                                 for p in turn.task_cells(current)):
                continue
            route = nav.search(hero, {cell}, forbidden)
            if route is None:
                continue
            return_steps = back.get(cell, 0)
            start = max(route[0], cooldown)
            finish = start + budget['rounds']
            if finish + return_steps + cfg.return_margin >= turn.day_left:
                continue
            candidates.append((start, route[0], cell != mem.task_wait_cell,
                               return_steps, cell, route))
        for start, _, _, return_steps, cell, route in sorted(candidates):
            # A cooling task may require waiting after arrival. Ready tasks keep
            # the existing adjacent execution semantics, including narrow sites.
            if cooldown > route[0] and not wait_cell_safe(turn, nav, ledger, hero, cell, forbidden):
                continue
            t = mem.treasure
            if (t and not mem.treasure_done and not mem.treasure_attempted
                    and turn.round <= t['endRound']
                    and t['startRound'] < turn.round + turn.day_left):
                altar = nav.approach(hero, [tuple(t['position'])], ledger.reserved)
                if altar and turn.round + altar[0] <= t['endRound']:
                    distances = nav.distances_to([tuple(t['position'])], {hero.pos}, forbidden)
                    onward = max((distances[p] for p in goals if p in distances), default=None)
                    timeout = min(int(task.get('timeoutRounds', cfg.task_max_rounds)), cfg.task_max_rounds)
                    if (Counter(t['items']) - hero.inventory or onward is None
                            or turn.round + start + timeout + onward + 2 + cfg.return_margin >= t['startRound']):
                        mem.trace_treasure(turn, 'treasure_task_gate', dedupe=True,
                                          reason='opening_priority', task=point,
                                          start=t['startRound'], timeout=timeout)
                        continue
            options.append(dict(point=point, task=task, route=route, cell=cell,
                                start=start, finish=start + budget['rounds'],
                                cooldown=cooldown, return_steps=return_steps, budget=budget,
                                value=int(task.get('scoreReward', 0)) + .5 * int(task.get('goldReward', 0))))
            break
    # Reverse fields give obstacle-aware distances between both selected work
    # tiles, without a new forward BFS for every hypothetical start position.
    for first in options:
        first.update(count=1, horizon=first['finish'], total_value=first['value'], next_point=None)
        for second in options:
            if first is second:
                continue
            routes = nav.distances_to({second['cell']}, vacated={hero.pos}, reserved=forbidden, exact=True)
            travel = routes.get(first['cell'])
            if travel is None:
                continue
            finish = max(first['finish'] + travel, second['cooldown']) + second['budget']['rounds']
            if finish + second['return_steps'] + cfg.return_margin >= turn.day_left:
                continue
            # Treasure timing applies to the whole proposed sequence as well.
            t = mem.treasure
            if t and not mem.treasure_done and not mem.treasure_attempted and turn.round <= t['endRound']:
                onward = nav.distances_to([tuple(t['position'])], {hero.pos}, forbidden).get(second['cell'])
                timeout = min(int(second['task'].get('timeoutRounds', cfg.task_max_rounds)), cfg.task_max_rounds)
                safe_finish = max(first['finish'] + travel, second['cooldown']) + timeout
                if onward is None or turn.round + safe_finish + onward + 2 + cfg.return_margin >= t['startRound']:
                    continue
            total = first['value'] + second['value']
            if first['count'] == 1 or total / max(1, finish) > first['total_value'] / max(1, first['horizon']):
                first.update(count=2, horizon=finish, total_value=total, next_point=second['point'])
        first['rate'] = first['total_value'] / max(1, first['horizon'])
    return options


def choose_task(options, mem):
    if not options:
        mem.task_target = mem.task_wait_cell = None
        return None
    best = min(options, key=lambda o: (-o['count'], -o['rate'], o['finish'], o['route'][0], o['point']))
    previous = next((o for o in options if o['point'] == mem.task_target), None)
    if previous and best is not previous and best['count'] == previous['count']:
        # Do not turn around for savings smaller than the estimate uncertainty.
        margin = max(best['budget']['uncertainty'], previous['budget']['uncertainty'])
        if best['total_value'] / max(1, best['horizon'] + margin) <= previous['rate']:
            best = previous
    mem.task_target, mem.task_wait_cell = best['point'], best['cell']
    return best


def switch_task_option(turn, cfg, mem, nav, ledger, hero):
    """After the configured effort, leave only for a ready, feasible other task."""
    if (not turn.phase_task or mem.task_point is None or not turn.is_day
            or turn.tick >= DEFENCE_RETURN_TICK
            or turn.round - mem.task_started <= cfg.task_switch_rounds
            or mem.answer is not None or mem.submitted is not None
            or mem.stop_reason not in ('', 'task_switch_budget')):
        return None
    options = task_options(turn, cfg, mem, nav, ledger, hero, switching=True)
    options = [o for o in options if o['route'][1] is not None
               and turn.tick + o['finish'] < DEFENCE_RETURN_TICK]
    return min(options, key=lambda o: (-o['rate'], o['finish'], o['point']), default=None)
