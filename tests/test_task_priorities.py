"""Focused scheduling boundaries; no multi-day simulation or live model."""
import json
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.config import Config
from agent.economy import pioneer, pioneer_task_options, reserve_treasure_gold
from agent.intelligence import Memory, Intelligence


def case(round_no=1, x=5, timeout=15):
    data = payload(round_no, roles=[unit(11, 'pioneer', x, 5)])
    data['teamOur']['playerTasks'] = [dict(isValid=True, coldDownRounds=0,
        taskPosition={'x': 5, 'y': 6}, timeoutRounds=timeout, scoreReward=100, goldReward=100)]
    data['weaponShopList'] = [{'name': 'StarSand', 'price': 10}]
    return data


def plan(start=25):
    return dict(position=[8, 5], items=['StarSand'], startRound=start, endRound=65)


class TaskPriorityTests(unittest.TestCase):
    def test_morning_news_finishes_before_accept(self):
        agent = Agent(Config(layout_mode='explicit'))
        data = case()
        data['worldNews'] = {'folkLegends': '祭坛位置尚不明确'}
        response = agent.decide(data)
        self.assertTrue(response['prompt'])
        self.assertNotEqual(response['roleCommandMap'].get('11', {}).get('action'), 'acceptTask')
        data['roundNo'] = 2
        data['llmResp'] = json.dumps({'treasure': None, 'treasureClues': []})
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['11']['action'], 'acceptTask')
        self.assertFalse(response['prompt'])

    def test_news_allows_travel_but_blocks_accept(self):
        for x, expected in [(0, 'move'), (5, None)]:
            turn, cfg, nav, ledger = setup_case(case(x=x))
            mem = Memory(news=[{'day': 1}], pending=('news', 0))
            pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
            self.assertEqual(ledger.commands.get('11', {}).get('action'), expected)

    def test_quota_or_disabled_news_does_not_starve_tasks(self):
        turn, cfg, nav, ledger = setup_case(case())
        mem = Memory(news=[{'day': 1}], news_dirty=True, calls=3)
        pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
        self.assertEqual(ledger.commands['11']['action'], 'acceptTask')

    def test_post_task_review_once_per_day(self):
        turn, cfg, _, _ = setup_case(case(20))
        mem = Memory(day=1, task_text='old task', task_started=1, news=[{'day': 1}], calls=1)
        mem.observe(turn, cfg)
        prompt = Intelligence(turn, cfg, mem).news()
        self.assertIn('"reviewAfterEvolution": true', prompt)
        self.assertEqual(mem.calls, 2)
        mem.pending = None
        mem.task_text = 'second task'
        mem.observe(turn, cfg)
        self.assertFalse(Intelligence(turn, cfg, mem).news())
        self.assertEqual(mem.calls, 2)

    def test_no_review_at_night_or_exhausted_quota(self):
        for r, calls in [(70, 1), (20, 3)]:
            turn, cfg, _, _ = setup_case(case(r))
            mem = Memory(task_text='old', news=[{'day': 1}], calls=calls)
            # Keep the quota in this same day rather than exercising daily reset.
            mem.day = turn.day
            mem.observe(turn, cfg)
            self.assertFalse(Intelligence(turn, cfg, mem).news())

    def test_active_task_not_interrupted_for_news(self):
        data = case()
        data['phaseTask'] = 'active'
        turn, cfg, _, _ = setup_case(data)
        mem = Memory(news=[{'day': 1}], news_dirty=True)
        self.assertFalse(Intelligence(turn, cfg, mem).news())

    def test_treasure_conflict_uses_timeout_not_learned_speed(self):
        data = case(10, timeout=15)
        data['teamOur']['roles'][0]['backpack'] = ['StarSand']
        turn, cfg, nav, ledger = setup_case(data)
        mem = Memory(treasure=plan(30), skills=[dict(point=(5, 6), workflow='check_token', rounds=1)])
        self.assertFalse(pioneer_task_options(turn, cfg, mem, nav, ledger, turn.pioneer))
        mem.treasure['startRound'] = 55
        self.assertTrue(pioneer_task_options(turn, cfg, mem, nav, ledger, turn.pioneer))

    def test_early_arrival_waits_without_accepting_task(self):
        data = case(20, x=7)
        data['teamOur']['roles'][0]['backpack'] = ['StarSand']
        turn, cfg, nav, ledger = setup_case(data)
        mem = Memory(treasure=plan(25))
        pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
        self.assertIn(11, ledger.used)
        self.assertFalse(ledger.commands)
        self.assertFalse(mem.treasure_attempted)

    def test_opening_summons(self):
        data = case(25, x=7)
        data['teamOur']['roles'][0]['backpack'] = ['StarSand']
        turn, cfg, nav, ledger = setup_case(data)
        mem = Memory(treasure=plan())
        pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
        self.assertEqual(ledger.commands['11']['action'], 'summonTreasure')

    def test_full_plan_reserves_supplies_before_worker_spending(self):
        turn, cfg, nav, ledger = setup_case(case(10))
        self.assertEqual(reserve_treasure_gold(turn, cfg, Memory(treasure=plan()), nav,
                                             ledger, turn.pioneer), 10)

    def test_expired_or_done_plan_does_not_block_tasks(self):
        for state in ('expired', 'done', 'attempted'):
            turn, cfg, nav, ledger = setup_case(case(10))
            mem = Memory(treasure=plan())
            if state == 'expired':
                mem.treasure['endRound'] = 9
            else:
                setattr(mem, 'treasure_' + state, True)
            self.assertTrue(pioneer_task_options(turn, cfg, mem, nav, ledger, turn.pioneer))

    def test_malformed_news_retries_are_bounded_by_daily_quota(self):
        agent = Agent(Config(layout_mode='explicit'))
        data = case()
        data['worldNews'] = {'folkLegends': '未知线索'}
        for r in range(1, 5):
            data['roundNo'] = r
            data['llmResp'] = 'invalid' if r > 1 else ''
            response = agent.decide(data)
            if r < 4:
                self.assertTrue(response['prompt'])
                self.assertNotEqual(response['roleCommandMap'].get('11', {}).get('action'), 'acceptTask')
            else:
                self.assertFalse(response['prompt'])
                self.assertEqual(response['roleCommandMap']['11']['action'], 'acceptTask')

    def test_review_gates_next_task_until_reply(self):
        data = case(20)
        turn, cfg, nav, ledger = setup_case(data)
        mem = Memory(day=1, task_text='finished', task_started=2,
                     news=[{'day': 1}], calls=1)
        mem.observe(turn, cfg)
        pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
        self.assertNotIn('11', ledger.commands)
        self.assertTrue(Intelligence(turn, cfg, mem).news())
        mem.last_round = 20
        data['roundNo'] = 21
        data['llmResp'] = '{"treasure": null}'
        turn, cfg, nav, ledger = setup_case(data)
        mem.observe(turn, cfg)
        pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
        self.assertEqual(ledger.commands['11']['action'], 'acceptTask')
