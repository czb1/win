"""Finite decisions from correlated feedback, without inventing hidden enemies."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import assault_payload, controlled_robot
from test_robot_path_blockers import decide
from test_robot_survival_memory import occupied_error, record_move
from agent.commands import command
from agent.robot_assault import RobotAssaultMemory, enemy_base


def frontage(round_no=95):
    # Relevant observed geometry only; the unknown occupant is not supplied as
    # an enemy worker. This is a finite-state check, not an engine replay.
    data = payload(round_no, [])
    data['mapInfo'].update(width=41, height=32)
    data['teamEnemy']['roles'] = [unit(10013, 'station', 9, 22, health=940),
                                 unit(40013, 'wall', 7, 22, health=800)]
    data['teamOur']['summonRobotList'] = [controlled_robot(31035, 6, 22),
                                        controlled_robot(31036, 7, 21),
                                        controlled_robot(31037, 11, 22)]
    return data


def collision_memory(data, point=(8, 21)):
    memory = RobotAssaultMemory()
    observed = copy.deepcopy(data)
    observed['teamOur']['summonRobotList'][0]['pos'] = {'x': point[0] - 1, 'y': point[1]}
    observed['teamOur']['summonRobotList'][1]['pos'] = {'x': 7, 'y': 20}
    record_move(observed, memory, 31035, point)
    observed['roundNo'] += 1
    observed['lastRoundRoleActionResults'] = {'31035': False}
    observed['errors'] = [occupied_error(31035, point)]
    turn, _, _, _ = setup_case(observed, layout_mode='explicit')
    memory.observe(turn, enemy_base(turn))
    data['roundNo'] = observed['roundNo']
    return memory


def rejected_base_shot(data, memory, issued=100, result=False, feedback=None):
    data['roundNo'] = issued
    turn, _, _, _ = setup_case(data, layout_mode='explicit')
    robot = next(r for r in turn.summon_robots if r.id == 31036)
    memory.remember(turn, robot, 'attack', (9, 21), 10013)
    data['roundNo'] += 1
    data['lastRoundRoleActionResults'] = {**(feedback or {}), '31036': result}
    return decide(data, memory)


class RobotStalledClearanceTests(unittest.TestCase):
    def test_recent_occupied_cell_and_failed_ray_trigger_joint_clearance(self):
        data = frontage()
        memory = collision_memory(data)
        ledger, _ = rejected_base_shot(data, memory)
        for uid in (31035, 31036):
            self.assertEqual(ledger.commands[str(uid)], command('attack', (7, 22)))
        self.assertEqual(ledger.commands['31037'], command('attack', (10, 22)))
        self.assertEqual(memory.focus_target_id, 40013)
        self.assertNotIn((8, 21), ledger.turn.blocked)
        self.assertEqual(set(memory.buildings), set())
        self.assertEqual(len(data['teamEnemy']['roles']), 2)

    def test_inferred_lane_expires_and_allows_another_base_attempt(self):
        data = frontage()
        memory = collision_memory(data)
        rejected_base_shot(data, memory)
        data['roundNo'] = 105
        data['lastRoundRoleActionResults'] = {}
        ledger, _ = decide(data, memory)
        self.assertFalse(memory.failed_base_lanes)
        self.assertEqual(ledger.commands['31036']['action'], 'attack')
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)

    def test_ordinary_sight_of_an_empty_cell_releases_the_inferred_lane(self):
        data = frontage()
        memory = collision_memory(data)
        rejected_base_shot(data, memory)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {}
        data['teamOur']['roles'] = [unit(10, 'worker', 8, 20)]
        ledger, _ = decide(data, memory)
        self.assertNotIn((8, 21), memory.failed_base_lanes)
        self.assertNotIn((8, 21), memory.occupied_seen)
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)

    def test_occupancy_and_failed_shot_in_one_feedback_do_not_depend_on_order(self):
        for shot_first in (True, False):
            with self.subTest(shot_first=shot_first):
                data = frontage(100)
                data['teamOur']['summonRobotList'][0]['pos'] = {'x': 7, 'y': 21}
                data['teamOur']['summonRobotList'][1]['pos'] = {'x': 7, 'y': 20}
                memory = RobotAssaultMemory()
                turn, _, _, _ = setup_case(data)
                memory.observe(turn, enemy_base(turn))
                orders = [(turn.summon_robots[0], 'move', (8, 21), None),
                          (turn.summon_robots[1], 'attack', (9, 21), 10013)]
                for robot, action, point, target in (reversed(orders) if shot_first else orders):
                    memory.remember(turn, robot, action, point, target)
                data['roundNo'] += 1
                data['lastRoundRoleActionResults'] = {'31035': False, '31036': False}
                data['errors'] = [occupied_error(31035, (8, 21))]
                ledger, _ = decide(data, memory)
                self.assertIn((8, 21), memory.failed_base_lanes)
                self.assertNotIn((8, 21), ledger.turn.blocked)

    def test_unconfirmed_attack_feedback_does_not_infer_a_blocked_lane(self):
        for result in (None, True, 'false'):
            with self.subTest(result=result):
                data = frontage()
                memory = collision_memory(data)
                ledger, _ = rejected_base_shot(data, memory, result=result)
                self.assertFalse(memory.failed_base_lanes)
                self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)

    def test_uncorrelated_attack_failure_does_not_infer_a_blocked_lane(self):
        data = frontage()
        memory = collision_memory(data)
        data['roundNo'] = 100
        turn, _, _, _ = setup_case(data)
        memory.remember(turn, turn.summon_robots[1], 'attack', (9, 21), 10013)
        data['roundNo'] = 102
        data['lastRoundRoleActionResults'] = {'31036': False}
        ledger, _ = decide(data, memory)
        self.assertFalse(memory.failed_base_lanes)
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)

    def test_reaching_a_closer_stance_resumes_normal_base_approach(self):
        data = assault_payload(robot=controlled_robot(x=6, y=3))
        data['teamEnemy']['roles'] += [unit(70, 'worker', 8, 3), unit(71, 'rocket', 8, 5)]
        memory = RobotAssaultMemory()
        for destination in (5, 6, 5, 6, 7):
            record_move(data, memory, 30000, (destination, 3))
            data['roundNo'] += 1
            data['lastRoundRoleActionResults'] = {'30000': True}
            data['teamOur']['summonRobotList'][0]['pos']['x'] = destination
            turn, _, _, _ = setup_case(data)
            memory.observe(turn, enemy_base(turn))
        ledger, _ = decide(data, memory)
        self.assertFalse(memory.approach_stalls)
        self.assertEqual(ledger.notes[30000]['conditions']['target_id'], 99)
        self.assertEqual(ledger.commands['30000']['action'], 'move')

    def test_changed_day_or_enemy_base_releases_failure_evidence(self):
        for change in ('day', 'base'):
            with self.subTest(change=change):
                data = frontage()
                memory = collision_memory(data)
                rejected_base_shot(data, memory)
                if change == 'day':
                    data['roundNo'] = 200
                else:
                    data['roundNo'] += 1
                    data['teamEnemy']['roles'][0]['pos'] = {'x': 20, 'y': 22}
                data['lastRoundRoleActionResults'] = {}
                decide(data, memory)
                self.assertFalse(memory.failed_base_lanes)
                self.assertFalse(memory.occupied_seen)
                self.assertFalse(memory.approach_stalls)

    def test_lost_robot_does_not_leave_a_stalled_approach(self):
        data = frontage()
        memory = collision_memory(data)
        rejected_base_shot(data, memory)
        data['roundNo'] += 1
        data['teamOur']['summonRobotList'] = data['teamOur']['summonRobotList'][2:]
        data['lastRoundRoleActionResults'] = {}
        ledger, _ = decide(data, memory)
        self.assertFalse(memory.approach_stalls)
        self.assertEqual(set(memory.approach_best), {31037})
        self.assertEqual(set(ledger.commands), {'31037'})

    def test_occupancy_alone_does_not_reject_a_base_shot(self):
        data = frontage()
        memory = collision_memory(data)
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)
        self.assertEqual(ledger.commands['31036']['action'], 'attack')

    def test_failed_shot_without_matching_occupancy_still_tries_another_aim(self):
        for point in (None, (8, 23)):
            with self.subTest(point=point):
                data = frontage()
                memory = (collision_memory(data, point) if point else RobotAssaultMemory())
                if point is None:
                    turn, _, _, _ = setup_case(data)
                    memory.observe(turn, enemy_base(turn))
                ledger, _ = rejected_base_shot(data, memory)
                self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)
                self.assertEqual(ledger.commands['31036']['action'], 'attack')
                self.assertFalse(memory.failed_base_lanes)

    def test_old_occupancy_does_not_turn_a_new_failure_into_a_blocked_lane(self):
        data = frontage()
        memory = collision_memory(data)
        ledger, _ = rejected_base_shot(data, memory, issued=110)
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)
        self.assertEqual(ledger.commands['31036']['action'], 'attack')
        self.assertFalse(memory.failed_base_lanes)

    def test_a_successful_shooter_on_the_suspected_lane_keeps_its_aim(self):
        data = frontage()
        memory = collision_memory(data)
        data['teamOur']['summonRobotList'][2]['pos'] = {'x': 7, 'y': 20}
        data['roundNo'] = 100
        turn, _, _, _ = setup_case(data, layout_mode='explicit')
        shooter = turn.summon_robots[2]
        memory.remember(turn, shooter, 'attack', (9, 21), 10013)
        ledger, _ = rejected_base_shot(data, memory, feedback={'31037': True})
        self.assertEqual(ledger.commands['31037'], command('attack', (9, 21)))

    def test_joint_clearance_persists_until_a_real_base_shot_opens(self):
        data = frontage()
        memory = collision_memory(data)
        ledger, _ = rejected_base_shot(data, memory)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'31035': True, '31036': True, '31037': True}
        data['teamEnemy']['roles'][1]['health'] = 720
        ledger, _ = decide(data, memory)
        for uid in (31035, 31036):
            self.assertEqual(ledger.commands[str(uid)], command('attack', (7, 22)))
        data['roundNo'] += 1
        data['teamEnemy']['roles'][1]['health'] = 0
        data['teamOur']['summonRobotList'][1]['pos'] = {'x': 8, 'y': 21}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)

    def test_backtracking_prioritizes_a_visible_worker_over_an_optional_detour(self):
        data = assault_payload(robot=controlled_robot(x=6, y=3))
        data['teamOur']['summonRobotList'].append(controlled_robot(30001, 5, 5))
        data['teamEnemy']['roles'] += [unit(70, 'worker', 8, 3, health=500),
                                     unit(71, 'rocket', 8, 5, health=1000)]
        memory = RobotAssaultMemory()
        for origin, destination in ((6, 5), (5, 6), (6, 5)):
            data['teamOur']['summonRobotList'][0]['pos'] = {'x': origin, 'y': 3}
            record_move(data, memory, 30000, (destination, 3))
            data['roundNo'] += 1
            data['lastRoundRoleActionResults'] = {'30000': True}
            data['teamOur']['summonRobotList'][0]['pos'] = {'x': destination, 'y': 3}
            turn, _, _, _ = setup_case(data, layout_mode='explicit')
            memory.observe(turn, enemy_base(turn))
        # A fourth no-progress step ends at the original stance.
        record_move(data, memory, 30000, (6, 3))
        data['roundNo'] += 1
        data['teamOur']['summonRobotList'][0]['pos'] = {'x': 6, 'y': 3}
        ledger, _ = decide(data, memory)
        for uid in (30000, 30001):
            self.assertEqual(ledger.commands[str(uid)], command('attack', (8, 3)))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30000': True, '30001': True}
        data['teamEnemy']['roles'][1]['pos'] = {'x': 3, 'y': 3}
        ledger, _ = decide(data, memory)
        self.assertNotEqual(memory.focus_target_id, 70)
        self.assertFalse(any(c == command('attack', (3, 3)) for c in ledger.commands.values()))


if __name__ == '__main__':
    unittest.main()
