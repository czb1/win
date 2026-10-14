"""Finite observations for the reported r71-r89 backtracking, without an engine."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_assault_bypass import reflect
from test_robot_assault import controlled_robot
from test_robot_ingress import decide
from agent.commands import command
from agent.model import pos
from agent.robot_assault import (AssaultRoute, RobotAssaultMemory, act_robots,
                                 enemy_base, _firing_point, _occupants)


def frontage(round_no=71, positions=((31, 6), (32, 6), (34, 8))):
    # Relevant known geometry only, including the three previously seen turrets.
    # No team/account metadata, raw logs or unobserved enemy positions are stored.
    data = payload(round_no, [unit(10013, 'station', 9, 22)])
    data['mapInfo'].update(width=41, height=32)
    data['teamEnemy']['roles'] = [
        unit(20013, 'station', 30, 10, health=1500),
        unit(20010, 'worker', 31, 8, health=500),
        unit(20011, 'pioneer', 32, 10, health=500),
        unit(20012, 'worker', 30, 8, health=500),
        unit(20040, 'rocket', 32, 9, health=1000),
        unit(20041, 'rocket', 32, 11, health=1000),
        unit(20042, 'rocket', 31, 11, health=1000),
    ]
    walls = [(28, 9), (28, 8), (28, 7), (28, 10), (28, 11), (28, 12),
             (29, 12), (30, 12), (31, 12), (32, 12), (30, 7), (31, 7),
             (32, 7), (33, 9)]
    data['teamEnemy']['roles'] += [unit(41000+i, 'wall', *p, health=1000)
                                  for i, p in enumerate(walls)]
    data['teamOur']['summonRobotList'] = [controlled_robot(uid, *p)
        for uid, p in zip((30035, 30036, 30037), positions)]
    return data


def detour():
    data = payload(80, [])
    data['teamEnemy']['roles'] = [unit(99, 'station', 8, 5)]
    data['teamEnemy']['roles'] += [unit(100+y, 'wall', 5, y, health=1000) for y in range(11)]
    data['teamOur']['summonRobotList'] = [controlled_robot(30036, 4, 4)]
    return data


class RobotRouteContinuityTests(unittest.TestCase):
    def clearing_approach(self):
        data = frontage(72, ((30, 6), (33, 7), (33, 8)))
        # An opaque character, rather than a teammate's firing ray, closes
        # the direct stance so this still exercises real clearance routing.
        data['teamOur']['roles'].append(unit(10010, 'worker', 32, 8))
        # Exercise ordinary clearance continuity without a prepared entrance;
        # prepared-gap ray blockers now deliberately preempt these detours.
        _, ledger, memory = decide(data)
        self.assertEqual(ledger.notes[30036]['conditions']['target_id'], 20011)
        self.assertEqual(ledger.commands['30036'], command('move', (34, 8)))
        # Keep the route but no entrance commitment in this continuity fixture.
        memory.clearance_entry = None
        # The next frame is supplied explicitly, not a simulated move result.
        data = frontage(73, ((29, 7), (34, 8), (33, 8)))
        data['teamOur']['roles'].append(unit(10010, 'worker', 32, 8))
        return data, memory

    def test_simultaneous_arrivals_keep_their_planned_base_shots(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            for ids in ((30035, 30036, 30037), (39000, 38000, 37000)):
                with self.subTest(reflection=(flip_x, flip_y), ids=ids):
                    data = frontage()
                    for robot, uid in zip(data['teamOur']['summonRobotList'], ids):
                        robot['id'] = uid
                    reflect(data, flip_x, flip_y)
                    original = copy.deepcopy(data)
                    _, ledger, memory = decide(data)
                    arrivals = [uid for uid, route in memory.routes.items() if route.steps == 1]
                    self.assertTrue(arrivals)
                    self.assertEqual(data, original)
                    # Check both robots' resulting stances together. Only the
                    # positions are varied; no attack/damage engine is assumed.
                    observed = copy.deepcopy(data)
                    observed['roundNo'] += 1
                    for robot in observed['teamOur']['summonRobotList']:
                        action = ledger.commands.get(str(robot['id']), {})
                        if action.get('action') == 'move':
                            robot['pos'] = action['targetPos'][0]
                    turn, _, _, _ = setup_case(observed)
                    occupied = _occupants(turn, memory)
                    for robot in turn.summon_robots:
                        if robot.id in arrivals:
                            self.assertIsNotNone(_firing_point(robot.pos, robot, enemy_base(turn),
                                                              memory, occupied))

    def test_confirmed_detour_keeps_shrinking_while_base_distance_increases(self):
        for result in (True, False, None):
            with self.subTest(feedback=result):
                data, memory = detour(), RobotAssaultMemory()
                for offset, point in enumerate(((4, 4), (3, 5), (2, 6), (1, 7))):
                    data['roundNo'] = 80 + offset
                    data['teamOur']['summonRobotList'][0]['pos'] = dict(zip(('x', 'y'), point))
                    data['lastRoundRoleActionResults'] = {} if result is None else {'30036': result}
                    _, ledger, memory = decide(data, memory)
                    note = ledger.notes[30036]['conditions']
                    self.assertEqual(note['target_id'], 99)
                    self.assertEqual(note['route_steps'], 10 - offset)
                    self.assertEqual(note['firing_goal'], (6, 8))
                    self.assertEqual(note['route_continued'], offset > 0)
                    self.assertEqual(memory.approach_stalls.get(30036, 0), 0)

    def test_clearance_is_not_abandoned_for_an_unhelpful_yield_or_base_detour(self):
        data, memory = self.clearing_approach()
        _, ledger, memory = decide(data, memory)
        note = ledger.notes[30036]
        self.assertEqual(note['reason'], 'robot_approach_path_blocker')
        self.assertEqual(note['conditions']['target_id'], 20011)
        self.assertTrue(note['conditions']['route_continued'])
        self.assertEqual(ledger.commands['30036'], command('move', (34, 9)))
        self.assertNotIn(30036, memory.yield_waiters)
        self.assertTrue(all(30036 not in waiters for waiters in memory.yield_waiters.values()))
        self.assertNotIn(30036, memory.approach_stalls)

    def test_scout_entry_does_not_choose_a_far_side_yield(self):
        data = frontage(73, ((29, 7), (34, 8), (33, 8)))
        _, ledger, _ = decide(data, entry=(29, 7))
        self.assertNotEqual(ledger.notes[30035]['reason'], 'robot_make_room_inside')
        self.assertEqual(ledger.notes[30037]['reason'], 'robot_attack_base')

    def test_direct_base_shot_preempts_a_clearance_commitment(self):
        data, memory = self.clearing_approach()
        # The east wall disappears and the opaque worker vacates the ray.
        # The teammate may stay: it no longer blocks the direct base shot.
        data['teamEnemy']['roles'] = [r for r in data['teamEnemy']['roles']
                                     if r['roleType'] != 'wall' or pos(r['pos']) != (33, 9)]
        next(r for r in data['teamOur']['roles'] if r['id'] == 10010)['pos'] = dict(x=36, y=8)
        _, ledger, memory = decide(data, memory)
        self.assertEqual(ledger.commands['30036']['action'], 'attack')
        self.assertEqual(ledger.notes[30036]['conditions']['target_id'], 20013)
        self.assertNotIn(30036, memory.routes)

    def test_missing_moved_or_dead_clearance_target_releases_the_route(self):
        for change in ('missing', 'moved', 'dead'):
            with self.subTest(change=change):
                data, memory = self.clearing_approach()
                target = next(r for r in data['teamEnemy']['roles'] if r['id'] == 20011)
                if change == 'missing':
                    data['teamEnemy']['roles'].remove(target)
                elif change == 'moved':
                    target['pos'] = dict(x=20, y=15)
                else:
                    target['health'] = 0
                _, ledger, memory = decide(data, memory)
                self.assertNotEqual(ledger.notes[30036]['conditions'].get('target_id'), 20011)
                self.assertFalse(ledger.notes[30036]['conditions'].get('route_continued', False))

    def test_blocked_or_reserved_old_stance_is_replanned(self):
        for restriction in ('wall', 'reservation', 'failed_step'):
            with self.subTest(restriction=restriction):
                data, memory = self.clearing_approach()
                goal = memory.routes[30036].goal
                if restriction == 'wall':
                    data['teamEnemy']['roles'].append(unit(49999, 'wall', *goal, health=1000))
                elif restriction == 'failed_step':
                    memory.failed_steps[30036, goal] = 80
                _, ledger, memory = decide(data, memory,
                                           reserved=(goal,) if restriction == 'reservation' else ())
                self.assertFalse(ledger.notes[30036]['conditions'].get('route_continued', False))
                self.assertNotEqual(ledger.commands.get('30036'), command('move', goal))

    def test_legal_but_unexecuted_move_does_not_reset_real_stalls(self):
        data = detour()
        _, _, memory = decide(data)
        memory.approach_stalls[30036] = 2
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30036': True}
        _, ledger, memory = decide(data, memory)
        self.assertEqual(memory.approach_stalls[30036], 3)
        self.assertEqual(ledger.notes[30036]['conditions']['assault_choice'], 'shared_clearance')
        self.assertIn((30036, (3, 5)), memory.failed_steps)

    def test_planned_moves_cannot_hide_repeated_backtracking(self):
        data = detour()
        turn, _, _, _ = setup_case(data)
        memory = RobotAssaultMemory()
        memory.observe(turn, enemy_base(turn))
        for point in ((3, 5), (4, 4), (3, 5), (4, 4)):
            robot = turn.summon_robots[0]
            memory.remember(turn, robot, 'move', point, 99)
            memory.routes[robot.id] = AssaultRoute(turn.round, 99, (8, 5), (6, 8), 10, point)
            data['roundNo'] += 1
            data['teamOur']['summonRobotList'][0]['pos'] = dict(zip(('x', 'y'), point))
            turn, _, _, _ = setup_case(data)
            memory.observe(turn, enemy_base(turn))
        self.assertEqual(memory.approach_stalls[30036], 3)
        _, ledger, _ = decide(data, memory)
        self.assertEqual(ledger.notes[30036]['conditions']['assault_choice'], 'shared_clearance')

    def test_route_lifetime_requires_contiguous_observation_and_same_battle(self):
        for change in ('skip', 'day', 'base', 'lost_robot'):
            with self.subTest(change=change):
                data = detour()
                _, _, memory = decide(data)
                old_route = memory.routes[30036]
                data['roundNo'] += 2 if change == 'skip' else 1
                data['teamOur']['summonRobotList'][0]['pos'] = dict(x=3, y=5)
                if change == 'day':
                    data['roundNo'] = 210
                elif change == 'base':
                    data['teamEnemy']['roles'][0]['pos'] = dict(x=9, y=5)
                elif change == 'lost_robot':
                    data['teamOur']['summonRobotList'] = []
                turn, _, _, _ = setup_case(data)
                memory.observe(turn, enemy_base(turn))
                self.assertNotIn(old_route, memory.routes.values())
                if change in ('day', 'base', 'lost_robot'):
                    self.assertNotIn((4, 4), memory.recent_positions.get(30036, ()))

    def test_replacing_command_releases_route_and_reservations_are_per_turn(self):
        data = detour()
        turn, ledger, memory = decide(data)
        memory.remember(turn, turn.summon_robots[0], 'attack', (5, 4), 104)
        self.assertNotIn(30036, memory.routes)
        _, _, _, new_ledger = setup_case(data)
        ledger.reserved.add((4, 5))
        self.assertFalse(new_ledger.reserved)


if __name__ == '__main__':
    unittest.main()
