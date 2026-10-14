"""Robot-transparent attacks and physical movement, using finite observations."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import assault_payload, controlled_robot
from test_robot_attack_efficiency import decide
from agent.commands import command
from agent.model import ROBOTS, pos
from agent.robot_assault import (AssaultRoute, RobotAssaultMemory, RobotObjective,
                                 _act_toward_target, _occupants, enemy_base,
                                 scouted_rear_summon_position)


class RobotBaseFocusTests(unittest.TestCase):
    def assert_base_attack(self, ledger, uid, base_id=99):
        self.assertEqual(ledger.commands[str(uid)]['action'], 'attack')
        self.assertEqual(ledger.notes[uid]['reason'], 'robot_attack_base')
        self.assertEqual(ledger.notes[uid]['conditions']['target_id'], base_id)

    def test_all_robot_types_and_unknown_ownership_allow_base_fire(self):
        for kind in ROBOTS:
            for owner in ('ours', 'challenger', 'defender', ''):
                with self.subTest(kind=kind, owner=owner):
                    data = assault_payload(robot=controlled_robot(x=7))
                    blocker = controlled_robot(30001, 8, 4, roleType=kind,
                                               targetTeam=owner if owner != 'ours' else 'defender')
                    data['robot']['roles'] = [blocker]
                    if owner == 'ours':
                        data['teamOur']['summonRobotList'].append(blocker)
                    ledger, _ = decide(data)
                    self.assert_base_attack(ledger, 30000)
                    if owner == 'ours':
                        self.assert_base_attack(ledger, 30001)
                    else:
                        self.assertNotIn('30001', ledger.commands)

    def test_reported_night_43_to_45_robot_31036_keeps_attacking(self):
        for round_no, location in ((112, (10, 18)), (113, (9, 19)), (114, (9, 19))):
            with self.subTest(round_no=round_no):
                data = payload(round_no, [unit(20013, 'station', 32, 11)])
                data['teamOur']['type'] = 'defender'
                data['mapInfo'].update(width=41, height=32)
                data['teamEnemy']['roles'] = [unit(10013, 'station', 9, 22)]
                data['teamOur']['summonRobotList'] = [controlled_robot(31036, *location)]
                data['robot']['roles'] = [controlled_robot(uid, *point, roleType='smallRobot')
                    for uid, point in ((30011, (10, 19)), (30013, (9, 20)), (30021, (10, 20)))]
                ledger, _ = decide(data)
                self.assert_base_attack(ledger, 31036, 10013)

    def test_all_shooters_hold_despite_stale_routes_stalls_and_clearance(self):
        data = assault_payload()
        data['teamEnemy']['roles'] += [unit(50, 'wall', 13, 4)]
        data['teamEnemy']['roles'][0]['health'] = 1
        data['teamOur']['summonRobotList'] = [controlled_robot(uid, x, 4)
            for uid, x in ((30003, 7), (30002, 8), (30001, 9))]
        original = copy.deepcopy(data)
        memory = RobotAssaultMemory(day=1, base_id=99, base_pos=(10, 4),
                                    focus_target_id=50, clearance_entry=(13, 4))
        for robot in data['teamOur']['summonRobotList']:
            uid = robot['id']
            memory.approach_stalls[uid] = 3
            memory.objectives[uid] = RobotObjective((13, 4), 50, 'breach')
            memory.routes[uid] = AssaultRoute(70, 50, (13, 4), (12, 4), 5, (7, 5))
        for round_no in (70, 71, 72):
            data['roundNo'] = round_no
            ledger, memory = decide(data, memory, entry=(8, 4))
            self.assertEqual(len(ledger.commands), 3)
            for robot in data['teamOur']['summonRobotList']:
                self.assert_base_attack(ledger, robot['id'])
            self.assertIsNone(memory.focus_target_id)
            self.assertIsNone(memory.clearance_entry)
            self.assertFalse(memory.routes)
            self.assertFalse(memory.yield_waiters)
            self.assertEqual(data['teamOur'], original['teamOur'])
            data['lastRoundRoleActionResults'] = {uid: True for uid in ledger.commands}

    def test_all_shooters_hold_under_both_axis_reflections(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = assault_payload()
                data['teamOur']['summonRobotList'] = [controlled_robot(30000 + i, x, y)
                    for i, (x, y) in enumerate(((7, 3), (8, 3), (9, 3), (7, 4), (8, 4), (9, 4)))]
                for roles in (data['teamOur']['roles'], data['teamEnemy']['roles'],
                              data['teamOur']['summonRobotList']):
                    for role in roles:
                        x, y = pos(role['pos'])
                        station = role['roleType'] == 'station'
                        role['pos'] = {'x': 14 - x - int(station) if flip_x else x,
                                       'y': 14 - y + int(station) if flip_y else y}
                ledger, _ = decide(data)
                self.assertEqual(len(ledger.commands), 6)
                for robot in data['teamOur']['summonRobotList']:
                    self.assert_base_attack(ledger, robot['id'])

    def test_dizzy_robot_still_allows_its_teammate_to_fire_through(self):
        data = assault_payload(robot=controlled_robot(x=7))
        data['teamOur']['summonRobotList'].append(controlled_robot(30001, 8, 4, abnormalState='dizzy'))
        ledger, _ = decide(data)
        self.assert_base_attack(ledger, 30000)
        self.assertNotIn('30001', ledger.commands)
        self.assertEqual(ledger.notes[30001]['reason'], 'robot_dizzy')

    def test_intermediate_robots_do_not_block_wall_or_character_targets(self):
        for kind in ('wall', 'worker'):
            with self.subTest(kind=kind):
                data = assault_payload(robot=controlled_robot(x=7))
                data['teamEnemy']['roles'] = [unit(50, kind, 10, 4)]
                data['robot']['roles'] = [controlled_robot(31000, 8, 4)]
                turn, _, nav, ledger = setup_case(data)
                memory = RobotAssaultMemory()
                self.assertTrue(_act_toward_target(turn, memory, nav, ledger,
                    turn.summon_robots[0], turn.enemies[0], _occupants(turn, memory), attack_only=True))
                self.assertEqual(ledger.commands['30000'], command('attack', (10, 4)))

    def test_robot_occupancy_and_reserved_destinations_still_block_movement(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                data = assault_payload(robot=controlled_robot(x=7))
                blocker = controlled_robot(30001, 8, 4)
                data['robot']['roles'] = [blocker]
                if owned:
                    data['teamOur']['summonRobotList'].append(blocker)
                turn, _, nav, ledger = setup_case(data)
                self.assertIn((8, 4), turn.blocked)
                self.assertIsNone(nav.search(turn.summon_robots[0], {(8, 4)}))
                if owned:
                    self.assertTrue(ledger.add(30001, command('move', (8, 5))))
                self.assertFalse(ledger.add(30000, command('move', (8, 4))))
                ledger.reserved.add((7, 5))
                self.assertFalse(ledger.add(30000, command('move', (7, 5))))
                self.assertTrue(ledger.add(30000, command('move', (6, 4))))

    def test_followers_can_approach_across_a_teammates_attack_ray(self):
        data = assault_payload(robot=controlled_robot(x=7))
        data['teamOur']['summonRobotList'].append(controlled_robot(30001, 8, 6, attackRange=1))
        # A narrow approach ends on the first robot's ray; it is an empty
        # physical cell even while that robot attacks the base through it.
        data['teamOur']['roles'] += [unit(60, 'wall', 9, 5), unit(61, 'wall', 9, 6)]
        turn, _, nav, ledger = setup_case(data)
        memory = RobotAssaultMemory()
        occupied = _occupants(turn, memory)
        self.assertTrue(ledger.add(30000, command('attack', (10, 4))))
        self.assertTrue(_act_toward_target(turn, memory, nav, ledger,
            turn.summon_robots[1], enemy_base(turn), occupied))
        self.assertEqual(ledger.commands['30001'], command('move', (8, 5)))
        self.assertNotIn(pos(ledger.commands['30001']['targetPos'][0]), turn.blocked)

    def test_known_robot_does_not_turn_movement_memory_into_attack_occlusion(self):
        data = assault_payload(robot=controlled_robot(x=7))
        data['robot']['roles'] = [controlled_robot(31000, 8, 4)]
        memory = RobotAssaultMemory(day=1, base_id=99, base_pos=(10, 4),
                                    occupied_steps={(8, 4): 74}, occupied_seen={(8, 4): 69},
                                    failed_base_lanes={(8, 4): 74})
        ledger, memory = decide(data, memory)
        self.assert_base_attack(ledger, 30000)
        self.assertNotIn((8, 4), memory.failed_base_lanes)
        self.assertIn((8, 4), memory.avoided_steps(30000))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30000': False}
        ledger, memory = decide(data, memory)
        self.assertFalse(memory.failed_base_lanes)
        self.assertTrue(memory.failed_shots)
        self.assertIn((8, 4), memory.avoided_steps(30000))
        self.assert_base_attack(ledger, 30000)

    def test_rear_summon_can_fire_through_a_robot_but_cannot_land_on_it(self):
        data = assault_payload(round_no=40)
        data['teamOur']['summonRobotList'] = []
        data['teamEnemy']['roles'].append(unit(50, 'rocket', 12, 4))
        data['robot']['roles'] = [controlled_robot(31000, 13, 4)]
        turn, _, nav, ledger = setup_case(data)
        ledger.robot_summon_anchor = (14, 4)
        spawn = scouted_rear_summon_position(turn, ledger, nav.deadline)
        self.assertEqual(spawn, (14, 4))
        self.assertIn((13, 4), turn.blocked)
        self.assertNotIn(spawn, turn.blocked | ledger.reserved | ledger.summon_pending_positions)


if __name__ == '__main__':
    unittest.main()
