"""Finite decision checks for the reported r71 geometry and bypass priority."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import assault_payload, controlled_robot
from test_robot_path_blockers import decide
from agent.model import distance, pos
from agent.projectiles import line_cells
from agent.robot_assault import RobotAssaultMemory, act_robots, enemy_entries, enemy_base
from agent.scouting import ScoutMemory, act_night_scout


def reported_frontage(closed=False):
    # Only relevant geometry/observed HP from the supplied r71 snapshot. No
    # task, account, full log, inferred occupant or simulated damage is copied.
    data = payload(71, [unit(13, 'station', 9, 22),
                        unit(10, 'worker', 7, 19, health=500),
                        unit(12, 'worker', 21, 16, health=500)])
    data['mapInfo'].update(width=41, height=32)
    walls = [(28, 10), (28, 9), (28, 8), (28, 11), (28, 12), (28, 7),
             (29, 7), (30, 7), (29, 12), (30, 12), (33, 7), (32, 7),
             (31, 7), (33, 8)]
    data['teamEnemy']['roles'] = [unit(99, 'station', 30, 10, health=1500),
                                unit(50, 'rocket', 31, 11, health=1000),
                                unit(51, 'rocket', 32, 9, health=1000),
                                unit(52, 'rocket', 32, 11, health=1000),
                                unit(53, 'pioneer', 32, 10, health=500)]
    data['teamEnemy']['roles'] += [unit(100+i, 'wall', *p, health=1000) for i, p in enumerate(walls)]
    data['teamOur']['summonRobotList'] = [controlled_robot(30035, 30, 6),
                                        controlled_robot(30036, 31, 6),
                                        controlled_robot(30037, 34, 12)]
    if closed:
        turn, _, _, _ = setup_case(data)
        missing = sorted(set(enemy_entries(turn, enemy_base(turn))) - set(walls))
        data['teamEnemy']['roles'] += [unit(200+i, 'wall', *p, health=1000) for i, p in enumerate(missing)]
        data['teamOur']['summonRobotList'].pop()
    return data


def reflect(data, flip_x, flip_y):
    for roles in (data['teamOur']['roles'], data['teamEnemy']['roles'],
                  data['teamOur']['summonRobotList'], data['robot']['roles']):
        for role in roles:
            x, y = pos(role['pos'])
            station = role['roleType'] == 'station'
            role['pos'] = {'x': 40-x-int(station) if flip_x else x,
                           'y': 31-y+int(station) if flip_y else y}


class AssaultBypassTests(unittest.TestCase):
    def test_reported_frontage_uses_open_routes_for_all_three_bosses(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = reported_frontage()
                reflect(data, flip_x, flip_y)
                before = copy.deepcopy(data)
                ledger, memory = decide(data)
                destinations = set()
                for robot in data['teamOur']['summonRobotList']:
                    action = ledger.commands[str(robot['id'])]
                    self.assertEqual(action['action'], 'move')
                    step = pos(action['targetPos'][0])
                    self.assertEqual(distance(pos(robot['pos']), step), 1)
                    self.assertNotIn(step, destinations)
                    destinations.add(step)
                    facts = ledger.notes[robot['id']]['conditions']
                    self.assertEqual(facts['target_id'], 99)
                    self.assertEqual(facts['assault_choice'], 'base_route')
                    self.assertLessEqual(facts['route_steps'], 7)
                self.assertIsNone(memory.focus_target_id)
                self.assertEqual(data, before)

    def test_even_a_low_health_optional_blocker_is_bypassed(self):
        for kind in ('wall', 'rocket', 'pioneer'):
            data = assault_payload(robot=controlled_robot(x=7))
            data['teamEnemy']['roles'].append(unit(50, kind, 8, 4, health=1))
            ledger, _ = decide(data)
            self.assertEqual(ledger.commands['30000']['action'], 'move')
            self.assertEqual(ledger.notes[30000]['conditions']['target_id'], 99)

    def test_closed_frontage_repositions_second_robot_to_share_first_wall(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = reported_frontage(closed=True)
                # The lead shooter leaves a free supporting stance. When it
                # stands adjacent to the wall instead, its own body can make
                # sharing impossible; that separate case is checked below.
                data['teamOur']['summonRobotList'][0]['pos']['y'] = 4
                reflect(data, flip_x, flip_y)
                ledger, memory = decide(data)
                self.assertEqual(memory.focus_target_id, 107)
                self.assertEqual(ledger.commands['30035']['action'], 'attack')
                self.assertEqual(ledger.commands['30036']['action'], 'move')
                facts = ledger.notes[30036]['conditions']
                self.assertEqual(facts['target_id'], 107)
                self.assertEqual(facts['assault_choice'], 'shared_clearance')
                self.assertFalse(facts['focus_unreachable'])

    def test_unreachable_shared_stance_explains_local_clearance(self):
        ledger, memory = decide(reported_frontage(closed=True))
        self.assertEqual(memory.focus_target_id, 107)
        facts = ledger.notes[30036]['conditions']
        self.assertEqual(facts['target_id'], 112)
        self.assertEqual(facts['assault_choice'], 'local_clearance')
        self.assertTrue(facts['focus_unreachable'])

    def test_supporting_move_preserves_lead_shot_then_both_fire(self):
        data = reported_frontage(closed=True)
        data['teamOur']['summonRobotList'][0]['pos']['y'] = 4
        first, memory = decide(data)
        step = first.commands['30036']['targetPos'][0]
        self.assertNotIn(pos(step), line_cells((30, 4), (30, 7)))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30035': True, '30036': True}
        data['teamOur']['summonRobotList'][1]['pos'] = step
        second, _ = decide(data, memory)
        for uid in (30035, 30036):
            self.assertEqual(second.commands[str(uid)]['action'], 'attack')
            self.assertEqual(second.notes[uid]['conditions']['target_id'], 107)

    def test_higher_id_base_shooter_reserves_its_lane_before_other_moves(self):
        data = assault_payload(robot=controlled_robot(30000, 6, 5))
        data['teamOur']['summonRobotList'].append(controlled_robot(30001, 7, 4))
        ledger, _ = decide(data)
        self.assertEqual(ledger.commands['30001']['action'], 'attack')
        self.assertEqual(ledger.commands['30000']['action'], 'move')
        aim = pos(ledger.commands['30001']['targetPos'][0])
        self.assertNotIn(pos(ledger.commands['30000']['targetPos'][0]), line_cells((7, 4), aim))

    def test_observed_opening_overrides_previous_shared_clearance(self):
        data = reported_frontage(closed=True)
        _, memory = decide(data)
        self.assertIsNotNone(memory.focus_target_id)
        data['roundNo'] += 1
        data['teamEnemy']['roles'] = [r for r in data['teamEnemy']['roles'] if r['id'] < 200]
        ledger, memory = decide(data, memory)
        self.assertIsNone(memory.focus_target_id)
        for uid in (30035, 30036):
            self.assertEqual(ledger.commands[str(uid)]['action'], 'move')
            self.assertEqual(ledger.notes[uid]['conditions']['target_id'], 99)

    def test_direct_base_shot_does_not_move_or_join_clearance(self):
        data = reported_frontage(closed=True)
        data['teamOur']['summonRobotList'].append(controlled_robot(30037, 29, 10))
        ledger, memory = decide(data)
        self.assertIsNotNone(memory.focus_target_id)
        self.assertEqual(ledger.commands['30037']['action'], 'attack')
        self.assertEqual(ledger.notes[30037]['conditions']['target_id'], 99)
        self.assertIsNone(ledger.notes[30037]['conditions']['focus_target_id'])

    def test_scout_establishes_full_base_coverage_without_long_frontage_detour(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = reported_frontage()
                reflect(data, flip_x, flip_y)
                turn, _, nav, ledger = setup_case(data)
                act_robots(turn, RobotAssaultMemory(), nav, ledger)
                memory = ScoutMemory()
                scout = act_night_scout(turn, memory, nav, ledger)
                facts = ledger.notes[scout.id]['conditions']
                self.assertEqual(scout.id, 12)
                self.assertLessEqual(facts['route_steps'], 7)
                self.assertEqual(facts['post_base_cells'], 4)
                self.assertTrue(all(distance(memory.post, p) <= 4 for p in enemy_base(turn).cells))
                self.assertNotIn(memory.post, turn.blocked)


if __name__ == '__main__':
    unittest.main()
