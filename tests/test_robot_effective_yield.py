"""Production scheduling: useful yielding, followed by direct base attacks."""
import copy
import unittest

from test_agent import setup_case, unit
from test_robot_ingress import frontage_case, corner_aisle_case
from test_robot_direct_base import decide
from agent.commands import command
from agent.model import distance, pos
from agent.robot_assault import act_robots, enemy_base


def blocked_queue():
    """Use actual negative attack feedback to exhaust the follower's aims."""
    data, memory = frontage_case(), None
    for _ in range(4):
        _, ledger, memory = decide(data, memory)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {
            uid: int(uid) != 30036 for uid in ledger.commands}
        # Other shooters keep damaging the base; don't pause their good shots.
        data['teamEnemy']['roles'][0]['health'] -= 10
    return data, memory


def run_reserved(data, memory, reserved=()):
    turn, _, nav, ledger = setup_case(data, layout_mode='explicit')
    ledger.reserved.update(reserved)
    act_robots(turn, memory, nav, ledger)
    return turn, ledger


class EffectiveYieldTests(unittest.TestCase):
    def test_effective_yield_precedes_clearance_and_resumes_attack(self):
        data, memory = blocked_queue()
        original = copy.deepcopy(data)
        turn, first = run_reserved(data, memory)
        self.assertEqual(first.commands['30035'], command('move', (30, 8)))
        self.assertGreater(first.notes[30035]['conditions']['ingress_routes_improved'], 0)
        self.assertEqual(first.notes[30036]['reason'], 'robot_wait_assault_lane')
        self.assertNotIn('30036', first.commands)
        self.assertIn((31, 8), turn.blocked)
        self.assertEqual(data, original)
        data['teamOur']['summonRobotList'][0]['pos'] = {'x': 30, 'y': 8}
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {uid: True for uid in first.commands}
        _, next_turn = run_reserved(data, memory)
        self.assertEqual(next_turn.commands['30035']['action'], 'attack')
        self.assertEqual(next_turn.notes[30035]['conditions']['target_id'], 20013)
        self.assertNotEqual(next_turn.notes[30035]['reason'], 'robot_make_room_inside')

    def test_no_yield_when_every_robot_can_already_attack_in_range(self):
        for factory in (frontage_case, corner_aisle_case):
            with self.subTest(factory=factory.__name__):
                turn, ledger, _ = decide(factory())
                base = enemy_base(turn)
                for robot in turn.summon_robots:
                    self.assertLessEqual(min(distance(robot.pos, p) for p in base.cells), 3)
                    self.assertEqual(ledger.commands[str(robot.id)]['action'], 'attack')
                    self.assertEqual(ledger.notes[robot.id]['conditions']['target_id'], base.id)

    def test_reserved_or_occupied_yield_destination_keeps_base_attack(self):
        for occupied in (False, True):
            with self.subTest(occupied=occupied):
                data, memory = blocked_queue()
                if occupied:
                    data['teamOur']['roles'].append(unit(10012, 'worker', 30, 8))
                _, ledger = run_reserved(data, memory, () if occupied else {(30, 8)})
                self.assertEqual(ledger.commands['30035']['action'], 'attack')
                self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)

    def test_failed_yield_keeps_observed_occupancy_and_attacks_next_turn(self):
        data, memory = blocked_queue()
        _, first = run_reserved(data, memory)
        self.assertEqual(first.commands['30035']['action'], 'move')
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30035': False}
        turn, ledger = run_reserved(data, memory)
        self.assertIn((31, 8), turn.blocked)
        self.assertEqual(ledger.commands['30035']['action'], 'attack')
        for action in ledger.commands.values():
            if action['action'] == 'move':
                self.assertNotEqual(pos(action['targetPos'][0]), (31, 8))

    def test_far_robot_does_not_force_a_useless_front_move(self):
        data = frontage_case()
        data['teamOur']['summonRobotList'][2]['pos'] = {'x': 35, 'y': 6}
        _, ledger, _ = decide(data)
        self.assertEqual(ledger.commands['30035']['action'], 'attack')
        self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)
        self.assertFalse(any(note['reason'] == 'robot_make_room_inside'
                             for note in ledger.notes.values()))

    def test_yield_memory_is_removed_at_day_boundary(self):
        data, memory = blocked_queue()
        _, first = run_reserved(data, memory)
        self.assertEqual(first.commands['30035']['action'], 'move')
        data['roundNo'] = 130
        _, ledger = run_reserved(data, memory)
        self.assertFalse(memory.yielded_rounds)
        self.assertFalse(ledger.commands)
