"""Focused role scheduling checks; no simulation."""
import unittest
from test_agent import payload, unit, setup_case
import test_shared_gunner
from agent.brain import Agent
from agent.config import Config
from agent.combat import block_enemy_workers


class NightRolesTests(unittest.TestCase):
    def test_first_two_nights_use_pioneer_and_both_workers_mine(self):
        for round_no in (70, 200):
            data = test_shared_gunner.SharedGunnerTests().case(round_no)
            data['teamOur']['roles'][1]['pos'] = {'x': 11, 'y': 3}
            data['teamOur']['roles'].append(unit(11, 'pioneer', 4, 11))
            result = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
            shots = [c for c in result.values() if c['action'] == 'attack']
            self.assertTrue(shots)
            self.assertEqual({c['controllerId'] for c in shots}, {'11'})
            self.assertEqual(result['1']['action'], 'collect')
            self.assertEqual(result['2']['action'], 'collect')

    def test_third_night_replaces_previous_pioneer_gunner(self):
        agent = Agent(Config(llm_enabled=False))
        data = test_shared_gunner.SharedGunnerTests().case(200)
        data['teamOur']['roles'][1]['pos'] = {'x': 3, 'y': 11}
        data['teamOur']['roles'].append(unit(11, 'pioneer', 4, 11))
        agent.decide(data)
        data['roundNo'] = 330
        data['teamOur']['roles'][-1]['pos'] = {'x': 8, 'y': 5}
        data['teamOur']['roles'][1]['pos'] = {'x': 4, 'y': 11}
        data['teamEnemy']['roles'] = [unit(50, 'rocket', 10, 5)]
        result = agent.decide(data)['roleCommandMap']
        self.assertEqual({c['controllerId'] for c in result.values() if c['action'] == 'attack'}, {'1'})
        self.assertEqual(result['11']['targetPos'][0]['x'], 9)

    def test_late_night_never_returns_home_even_without_target_or_with_task(self):
        for task in ('', 'unfinished task'):
            data = payload(330, [unit(13, 'station', 3, 11), unit(11, 'pioneer', 8, 5),
                                 unit(20, 'rocket', 3, 12)])
            data['phaseTask'] = task
            result = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
            self.assertNotIn('11', result)

    def test_worker_block_fallback_moves_into_wall_base_gap_and_holds(self):
        for mirrored in (False, True):
            data = payload(330, [unit(11, 'pioneer', 8, 5)])
            data['teamEnemy']['roles'] = [unit(50, 'station', 12, 5), unit(51, 'wall', 8, 4),
                                         unit(52, 'worker', 10, 4)]
            if mirrored:
                for team in ('teamOur', 'teamEnemy'):
                    for role in data[team]['roles']:
                        role['pos']['x'] = 14 - role['pos']['x']
            turn, _, nav, ledger = setup_case(data)
            self.assertTrue(block_enemy_workers(turn, nav, ledger, turn.pioneer))
            self.assertEqual(ledger.commands['11']['action'], 'move')
            step = ledger.commands['11']['targetPos'][0]
            self.assertEqual(step['x'], 5 if mirrored else 9)
            data['teamOur']['roles'][0]['pos'] = step
            turn, _, nav, ledger = setup_case(data)
            self.assertTrue(block_enemy_workers(turn, nav, ledger, turn.pioneer))
            self.assertNotIn('11', ledger.commands)
