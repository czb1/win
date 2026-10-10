"""Daily recall and wall cutoff regressions at ticks 66 and 67."""
import unittest
from unittest.mock import patch

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import build, first_day_front, wall_keeps_access, finish_preparation
from agent.intelligence import Memory, Intelligence
from agent.model import pos
from agent.worker_jobs import _continue


class DefenceReturnTickTests(unittest.TestCase):
    def wall_case(self, round_no, **config):
        data = payload(round_no, roles=[
            unit(10, 'worker', 11, 25, backpack=['stone'] * 3),
            unit(11, 'pioneer', 6, 18), unit(13, 'station', 9, 22),
            unit(20, 'rocket', 8, 23), unit(21, 'rocket', 8, 21),
            unit(22, 'rocket', 9, 23)])
        data['mapInfo'].update(width=34, height=34)
        turn, cfg, nav, ledger = setup_case(data, **config)
        ledger.return_pairs = [(turn.pioneer, turn.weapons[0])]
        ledger.operator_posts = {11: (8, 22)}
        return turn, cfg, nav, ledger

    def test_round_64_wall_is_allowed_with_a_four_step_operator_return(self):
        turn, cfg, nav, ledger = self.wall_case(64)
        self.assertEqual(nav.search(turn.pioneer, {(8, 22)})[0], 4)
        self.assertTrue(wall_keeps_access(turn, nav, ledger, (12, 24)))
        self.assertTrue(build(turn, cfg, Memory(), nav, ledger,
                              turn.workers[0], [(12, 24)], lambda _: 'wall'))
        self.assertEqual(ledger.commands['10'], command('build', (12, 24), name='wall'))

    def test_wall_cutoff_repeats_each_day_and_respects_round_origin(self):
        for day in (1, 2, 10):
            for origin in (0, 1):
                for tick, allowed in ((66, True), (67, False), (69, False)):
                    with self.subTest(day=day, origin=origin, tick=tick):
                        turn, cfg, nav, ledger = self.wall_case(
                            (day - 1) * 130 + tick + origin, round_origin=origin)
                        self.assertEqual(wall_keeps_access(turn, nav, ledger, (12, 24)), allowed)
                        self.assertEqual(build(turn, cfg, Memory(), nav, ledger,
                                               turn.workers[0], [(12, 24)], lambda _: 'wall'), allowed)
                        self.assertEqual(bool(ledger.commands), allowed)

    def test_ledger_rejects_wall_commands_at_cutoff_without_an_operator(self):
        for tick, allowed in ((66, True), (67, False), (69, False)):
            with self.subTest(tick=tick):
                turn, _, _, ledger = self.wall_case(tick)
                ledger.return_pairs = []
                ledger.operator_posts = {}
                self.assertEqual(ledger.add(10, command('build', (12, 24), name='wall')), allowed)
                if not allowed:
                    self.assertEqual(ledger.rejections[10], {'wall_construction_cutoff': 1})
                    self.assertFalse(ledger.used)

    def test_wall_cutoff_does_not_apply_to_weapon_construction(self):
        data = payload(67, roles=[unit(10, 'worker', 2, 2)])
        _, _, _, ledger = setup_case(data, layout_mode='explicit', weapon_cells=[[3, 3]])
        self.assertTrue(ledger.add(10, command('build', (3, 3), name='rocket')))

    def test_existing_wall_job_stops_at_cutoff_without_an_outbound_move(self):
        turn, cfg, nav, ledger = self.wall_case(67)
        job = {'kind': 'build', 'target': (12, 24), 'name': 'wall', 'work_cell': None}
        ok, _ = _continue(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                          job, ledger.tower_cells, ledger.wall_cells)
        self.assertFalse(ok)
        self.assertFalse(ledger.commands)
        self.assertFalse(ledger.work_jobs)

    def test_first_day_priority_releases_its_builder_at_cutoff(self):
        turn, cfg, nav, ledger = self.wall_case(67)
        mem = Memory(day1_wall_worker=10, day1_wall_delivering=True)
        first_day_front(turn, cfg, mem, nav, ledger, list(ledger.wall_cells))
        self.assertIsNone(mem.day1_wall_worker)
        self.assertFalse(mem.day1_wall_delivering)
        self.assertFalse(ledger.commands)

    def test_recalled_operator_does_not_take_a_preparation_action_at_cutoff(self):
        turn, cfg, nav, ledger = self.wall_case(67)
        with patch('agent.economy.use_inventory') as use:
            self.assertFalse(finish_preparation(turn, cfg, Memory(), nav, ledger,
                                                turn.workers[0], turn.weapons[0], list(ledger.wall_cells)))
        use.assert_not_called()
        self.assertFalse(ledger.commands)

    def operator_case(self, round_no, position=(0, 0)):
        return payload(round_no, roles=[unit(13, 'station', 10, 12),
            unit(11, 'pioneer', *position), unit(10, 'worker', 9, 12),
            unit(20, 'rocket', 4, 4)])

    def operator_config(self, **settings):
        return Config(layout_mode='explicit', weapon_cells=[[4, 4]],
                      loadout=['rocket'], llm_enabled=False, **settings)

    def test_operator_recall_starts_at_67_independent_of_distance_and_margin(self):
        for day in (1, 2, 10):
            for position in ((0, 0), (3, 3)):
                for margin in (0, 5):
                    with self.subTest(day=day, position=position, margin=margin):
                        agent = Agent(self.operator_config(return_margin=margin))
                        data = self.operator_case((day - 1) * 130 + 66, position)
                        agent.decide(data)
                        mem = next(iter(agent.sessions.values()))
                        self.assertNotIn(11, mem.return_targets)
                        data['roundNo'] += 1
                        data['lastRoundRoleActionResults'] = {}
                        result = agent.decide(data)['roleCommandMap']
                        self.assertEqual(mem.return_targets[11], 20)
                        if position == (0, 0):
                            self.assertEqual(result['11']['action'], 'move')
                            self.assertNotEqual(pos(result['11']['targetPos'][0]), position)

    def test_active_task_releases_pioneer_at_67_on_every_day(self):
        for day in (1, 2):
            with self.subTest(day=day):
                agent = Agent(self.operator_config())
                data = self.operator_case((day - 1) * 130 + 66)
                data['phaseTask'] = 'active task'
                with patch.object(Intelligence, 'task', return_value=('', '')) as task:
                    agent.decide(data)
                    mem = next(iter(agent.sessions.values()))
                    self.assertNotIn(11, mem.return_targets)
                    self.assertFalse(mem.stop_reason)
                    self.assertEqual(task.call_args.kwargs['available_rounds'], 1)
                    data['roundNo'] += 1
                    result = agent.decide(data)['roleCommandMap']
                self.assertEqual(mem.return_targets[11], 20)
                self.assertEqual(mem.stop_reason, 'first_wave_deadline' if day == 1 else 'defence_return_deadline')
                self.assertEqual(result['11']['action'], 'move')

    def test_ready_task_answer_cannot_take_the_recall_action_at_67(self):
        agent = Agent(self.operator_config())
        data = self.operator_case(66)
        data['phaseTask'] = 'active task'
        with patch.object(Intelligence, 'task', return_value=('', '')) as task:
            agent.decide(data)
            mem = next(iter(agent.sessions.values()))
            mem.answer = '42'
            task.reset_mock()
            data['roundNo'] = 67
            result = agent.decide(data)['roleCommandMap']
        task.assert_not_called()
        self.assertEqual(result['11']['action'], 'move')
        self.assertEqual(mem.return_targets[11], 20)

    def test_carried_boss_order_cannot_take_the_recall_action_at_67(self):
        data = payload(67, roles=[unit(13, 'station', 9, 22),
            unit(11, 'pioneer', 6, 18, backpack=['BossRobotSummonOrder']),
            unit(20, 'rocket', 8, 23), unit(21, 'rocket', 8, 21),
            unit(22, 'rocket', 9, 23)])
        data['mapInfo'].update(width=34, height=34)
        agent = Agent(Config(llm_enabled=False))
        result = agent.decide(data)['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(result['11']['action'], 'move')
        self.assertEqual(mem.return_targets[11], 20)


if __name__ == '__main__':
    unittest.main()
