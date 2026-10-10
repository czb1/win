"""Coordinated clearance only for obstacles on the assault's base routes."""
import unittest

from test_agent import unit
from test_robot_assault import assault_payload, controlled_robot
from test_robot_path_blockers import decide
from agent.commands import command


def split_blockers():
    data = assault_payload(robot=controlled_robot(x=6, y=3))
    data['teamOur']['summonRobotList'].append(controlled_robot(30001, 6, 5))
    data['teamEnemy']['roles'] += [unit(70, 'wall', 8, 3), unit(71, 'rocket', 8, 5)]
    return data


class RobotFocusTests(unittest.TestCase):
    def test_robots_focus_one_exposed_blocker_instead_of_splitting_damage(self):
        for kind in ('worker', 'pioneer', 'imp', 'rocket', 'gatling', 'railgun', 'wall'):
            with self.subTest(kind=kind):
                data = split_blockers()
                data['teamEnemy']['roles'][1]['roleType'] = kind
                ledger, memory = decide(data)
                self.assertEqual(ledger.commands['30000'], command('attack', (8, 3)))
                self.assertEqual(ledger.commands['30001'], command('attack', (8, 3)))
                self.assertEqual(memory.focus_target_id, 70)

    def test_base_shot_wins_over_another_robots_blocker(self):
        data = split_blockers()
        data['teamOur']['summonRobotList'][1]['pos'] = {'x': 12, 'y': 4}
        ledger, _ = decide(data)
        self.assertEqual(ledger.notes[30000]['conditions']['target_id'], 70)
        self.assertEqual(ledger.notes[30001]['conditions']['target_id'], 99)

    def test_focus_persists_then_releases_a_character_that_leaves_the_path(self):
        data = split_blockers()
        data['teamEnemy']['roles'][1]['roleType'] = 'pioneer'
        _, memory = decide(data)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30000': True, '30001': True}
        ledger, _ = decide(data, memory)
        self.assertEqual(memory.focus_target_id, 70)
        data['roundNo'] += 1
        data['teamEnemy']['roles'][1]['pos'] = {'x': 3, 'y': 3}
        ledger, _ = decide(data, memory)
        self.assertNotEqual(memory.focus_target_id, 70)
        self.assertFalse(any(c == command('attack', (3, 3)) for c in ledger.commands.values()))

    def test_reported_death_releases_focus_without_assuming_other_damage(self):
        data = split_blockers()
        _, memory = decide(data)
        data['roundNo'] += 1
        data['teamEnemy']['roles'][1]['health'] = 0
        ledger, _ = decide(data, memory)
        self.assertNotEqual(memory.focus_target_id, 70)
        self.assertFalse(any(c == command('attack', (8, 3)) for c in ledger.commands.values()))

    def test_off_path_gunner_does_not_delay_base_approach(self):
        data = assault_payload(robot=controlled_robot(x=6, y=4))
        data['teamEnemy']['roles'].append(unit(70, 'pioneer', 5, 6, health=1))
        ledger, memory = decide(data)
        self.assertEqual(ledger.commands['30000']['action'], 'move')
        self.assertEqual(ledger.notes[30000]['conditions']['target_id'], 99)
        self.assertIsNone(memory.focus_target_id)

    def test_one_robots_failed_shot_does_not_stop_another_legal_shooter(self):
        data = split_blockers()
        _, memory = decide(data)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30000': False, '30001': True}
        ledger, _ = decide(data, memory)
        self.assertNotEqual(ledger.commands.get('30000'), command('attack', (8, 3)))
        self.assertEqual(ledger.commands['30001'], command('attack', (8, 3)))

    def test_all_robots_lost_or_next_day_clears_the_focus(self):
        for end in ('lost', 'day'):
            with self.subTest(end=end):
                data = split_blockers()
                _, memory = decide(data)
                if end == 'lost':
                    data['teamOur']['summonRobotList'] = []
                else:
                    data['roundNo'] = 130
                decide(data, memory)
                self.assertIsNone(memory.focus_target_id)


if __name__ == '__main__':
    unittest.main()
