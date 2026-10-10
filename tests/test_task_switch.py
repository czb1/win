"""Opportunity-based task switching and protecting task-capable pioneers."""
import unittest
from unittest.mock import patch
from test_agent import payload, setup_case, unit
from test_task_schedule import task
from agent.brain import Agent, summon_best_robot
from agent.config import Config
from agent.intelligence import Memory
from agent.task_schedule import switch_task_option


class TaskSwitchTests(unittest.TestCase):
    def case(self, round_no=18, ready=True):
        data = payload(round_no, [unit(11, 'pioneer', 5, 5)])
        data['phaseTask'] = 'current question'
        data['teamOur']['playerTasks'] = [task(), task(10, 5, valid=ready, cooldown=0 if ready else 20)]
        t, c, n, l = setup_case(data, layout_mode='explicit')
        m = Memory(task_started=10, task_point=(6, 5), task_timeout=30)
        return data, t, c, n, l, m

    def test_only_after_seven_elapsed_rounds(self):
        for r, expected in ((17, False), (18, True)):
            _, t, c, n, l, m = self.case(r)
            result = switch_task_option(t, c, m, n, l, t.pioneer)
            self.assertEqual(result is not None, expected)
            if result:
                self.assertEqual(result['point'], (10, 5))

    def test_no_alternative_keeps_working(self):
        _, t, c, n, l, m = self.case(ready=False)
        self.assertIsNone(switch_task_option(t, c, m, n, l, t.pioneer))

    def test_ready_answer_and_submission_are_not_discarded(self):
        for field, value in (('answer', '42'), ('submitted', (17, '42'))):
            _, t, c, n, l, m = self.case()
            setattr(m, field, value)
            self.assertIsNone(switch_task_option(t, c, m, n, l, t.pioneer))

    def test_insufficient_time_and_return_lock_do_not_switch(self):
        for round_no, lock in ((60, False), (18, True), (67, False)):
            _, t, c, n, l, m = self.case(round_no)
            if lock:
                m.return_targets[11] = 99
            self.assertIsNone(switch_task_option(t, c, m, n, l, t.pioneer))

    def test_configurable_effort(self):
        _, t, c, n, l, m = self.case()
        c.task_switch_rounds = 9
        self.assertIsNone(switch_task_option(t, c, m, n, l, t.pioneer))

    def test_agent_moves_without_issuing_more_model_work(self):
        data, _, _, _, _, _ = self.case(10)
        agent = Agent(Config(layout_mode='explicit'))
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        mem.task_started = 10
        mem.task_point = (6, 5)
        mem.task_timeout = 30
        mem.answer = None
        data['roundNo'] = 18
        with patch('agent.intelligence.Intelligence.task') as work:
            response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['11']['action'], 'move')
        self.assertEqual(mem.stop_reason, 'task_switch_budget')
        work.assert_not_called()
        # Still inside old task range after the first step: keep moving out.
        data['teamOur']['roles'][0]['pos'] = response['roleCommandMap']['11']['targetPos'][0]
        data['roundNo'] = 19
        with patch('agent.intelligence.Intelligence.task') as work:
            response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['11']['action'], 'move')
        work.assert_not_called()

    def test_worker_shops_while_pioneer_has_next_task(self):
        for protected_job, expected in ((False, True), (True, False)):
            data = payload(140, [unit(11, 'pioneer', 5, 5), unit(10, 'worker', 1, 1)])
            data['teamEnemy']['roles'] = [unit(99, 'station', 12, 12)]
            data['teamOur']['goldNum'] = 120
            data['teamOur']['playerTasks'] = [task()]
            data['mapInfo']['zones'] = [{'neutralType': 'weaponShop', 'pos': {'x': 5, 'y': 6}}]
            data['weaponShopList'] = [{'name': 'BossRobotSummonOrder', 'price': 120}]
            t, c, n, l = setup_case(data, layout_mode='explicit')
            m = Memory()
            if protected_job:
                m.daytime_jobs[10] = {'kind': 'build', 'target': (2, 2)}
            result = summon_best_robot(t, c, m, n, l, [], [])
            self.assertEqual(result, expected)
            self.assertNotIn('11', l.commands)
            if expected:
                self.assertIn('10', l.commands)
