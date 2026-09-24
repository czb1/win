"""Treasure diagnostics must remain observational, including failed summons."""
import json
from unittest.mock import patch
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Memory, Intelligence
from agent.model import Turn
from agent.economy import pioneer


class TreasureTraceTests(unittest.TestCase):
    def test_news_capture_truncation_eviction_and_deduplication(self):
        data = payload()
        data['worldNews'] = {'folkLegends': 'clue\n' + 'x' * 21000}
        mem = Memory(news=[{'day': d, 'officialNews': '', 'folkLegends': str(d)}
                           for d in range(1, 11)])
        with self.assertLogs('agent.intelligence', 'INFO') as logs:
            mem.observe(Turn(data, Config()), Config())
            mem.observe(Turn(data, Config()), Config())
        lines = [line for line in logs.output if 'event=news_received' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('[TREASURE_TRACE]', lines[0])
        record = json.loads(lines[0].split('data=', 1)[1])
        self.assertTrue(record['truncated']['folkLegends'])
        self.assertEqual(record['evicted'][0]['day'], 1)
        self.assertEqual(len(mem.news), 10)

    def test_request_reply_and_rejection_preserve_old_plan(self):
        data = payload(2)
        data['weaponShopList'] = [{'name': 'StarSand', 'price': 15}]
        old = {'position': [4, 4], 'items': ['StarSand'], 'startRound': 10,
               'endRound': 20, 'confidence': .99, 'evidence': ['clue']}
        for candidate, expected in ((None, 'no_candidate'), ({**old, 'confidence': .1}, 'confidence'),
                                    (old, '"accepted": true')):
            with self.subTest(candidate=candidate):
                mem = Memory(pending=('news', 1), treasure=old.copy())
                data['llmResp'] = json.dumps({'treasure': candidate})
                with self.assertLogs('agent.intelligence', 'INFO') as logs:
                    mem.observe(Turn(data, Config()), Config())
                self.assertIn('event=news_response', '\n'.join(logs.output))
                self.assertIn(expected, '\n'.join(logs.output))
                self.assertEqual(mem.treasure, old)
        mem = Memory(news=[{'day': 1, 'folkLegends': 'clue'}], news_dirty=True)
        with self.assertLogs('agent.intelligence', 'INFO') as logs:
            prompt = Intelligence(Turn(data, Config()), Config(), mem).news()
        self.assertTrue(prompt)
        self.assertIn('event=news_request', '\n'.join(logs.output))

    def test_result_codes_do_not_change_existing_retry_policy(self):
        for code in range(5):
            with self.subTest(code=code):
                data = payload(2)
                data['lastSummonTreasureResult'] = code
                data['lastRoundRoleActionResults'] = {'11': code != 0}
                mem = Memory(last_round=1, treasure_attempted=True,
                             last_commands={'11': {'action': 'summonTreasure', 'item': ['StarSand']}})
                with self.assertLogs('agent.intelligence', 'INFO') as logs:
                    mem.observe(Turn(data, Config()), Config())
                self.assertIn('event=treasure_result', '\n'.join(logs.output))
                self.assertEqual(mem.treasure_done, code in (1, 4))
                self.assertTrue(mem.treasure_attempted)

    def test_progress_and_summon(self):
        data = payload(10, roles=[unit(11, 'pioneer', 5, 5, backpack=['StarSand'])])
        turn, cfg, nav, ledger = setup_case(data)
        mem = Memory(treasure={'position': [6, 5], 'items': ['StarSand'],
                               'startRound': 10, 'endRound': 20})
        with self.assertLogs('agent.intelligence', 'INFO') as logs:
            pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
        self.assertEqual(ledger.commands['11']['action'], 'summonTreasure')
        self.assertIn('event=treasure_progress', '\n'.join(logs.output))
        self.assertIn('event=treasure_summon', '\n'.join(logs.output))

    def test_trace_disabled_produces_identical_responses(self):
        enabled, muted = Agent(), Agent()
        for round_no in range(1, 5):
            data = payload(round_no)
            data['worldNews'] = {'folkLegends': 'a clue'}
            data['llmResp'] = '{"treasure": null, "oreOutages": []}'
            expected = enabled.decide(data)
            with patch.object(Memory, 'trace_treasure'):
                actual = muted.decide(data)
            self.assertEqual(actual, expected)

    def test_gate_logging_is_deduplicated(self):
        mem = Memory(news=[{'day': 1}], calls=3)
        turn = Turn(payload(), Config())
        with self.assertLogs('agent.intelligence', 'INFO') as logs:
            for _ in range(3):
                self.assertEqual(Intelligence(turn, Config(), mem).news(), '')
        self.assertEqual(len(logs.output), 1)
        self.assertIn('quota', logs.output[0])


if __name__ == '__main__':
    unittest.main()
