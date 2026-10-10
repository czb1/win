"""Focused task planning/state boundaries, without model or match simulation."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.config import Config
from agent.economy import pioneer, pioneer_task_options
from agent.intelligence import Memory
from agent.model import pos
from agent.task_schedule import (task_budget, task_descriptor, task_signature,
                                 choose_task, wait_cell_safe)


def task(x=6, y=5, cooldown=0, valid=True, timeout=15, reward=100):
    return dict(taskType='self-evolution', taskPosition=dict(x=x, y=y),
                coldDownRounds=cooldown, isValid=valid, timeoutRounds=timeout,
                scoreReward=reward, goldReward=reward)


def record(t, rounds=4, completed=True, family='parser/task_#'):
    return dict(point=pos(t['taskPosition']), rounds=rounds, completionObserved=completed,
                descriptor=task_descriptor(t),
                signature=task_signature(dict(family=family, input_kind='logs',
                                             example={'total': 0})))


def case(round_no=10, x=1, y=5, tasks=None, home=False):
    data = payload(round_no, [unit(11, 'pioneer', x, y)])
    if home:
        data['teamOur']['roles'].append(unit(13, 'station', 1, 11))
    data['teamOur']['playerTasks'] = tasks if tasks is not None else [task()]
    return data


class TaskScheduleTests(unittest.TestCase):
    def dispatch(self, data, mem=None, **cfg):
        t, c, n, l = setup_case(data, layout_mode='explicit', **cfg)
        mem = mem or Memory()
        pioneer(t, c, mem, n, l, t.pioneer, shopping=False)
        return t, c, n, l, mem

    def test_cooling_invalid_point_allows_pretravel_not_accept(self):
        data = case(tasks=[task(cooldown=3, valid=False)])
        _, _, _, ledger, mem = self.dispatch(data)
        self.assertEqual(ledger.commands['11']['action'], 'move')
        self.assertEqual(mem.task_target, (6, 5))
        self.assertIsNone(mem.accepted_round)

    def test_wait_holds_position_and_refresh_authorizes_accept(self):
        data = case(x=5, tasks=[task(cooldown=2, valid=False)])
        _, _, _, ledger, mem = self.dispatch(data)
        self.assertFalse(ledger.commands)
        self.assertIn(11, ledger.used)
        self.assertEqual(ledger.notes[11]['reason'], 'task_cooldown_wait')
        data['roundNo'] += 2
        data['teamOur']['playerTasks'][0].update(coldDownRounds=0, isValid=True)
        _, _, _, ledger, mem = self.dispatch(data, mem)
        self.assertEqual(ledger.commands['11']['action'], 'acceptTask')

    def test_exhausted_point_releases_target(self):
        data = case(tasks=[task(valid=False)])
        mem = Memory(task_target=(6, 5), task_wait_cell=(5, 5))
        _, _, _, ledger, mem = self.dispatch(data, mem)
        self.assertFalse(ledger.commands)
        self.assertIsNone(mem.task_target)
        self.assertIsNone(mem.task_wait_cell)

    def test_agent_cooldown_wait_does_not_return_to_base(self):
        data = case(x=5, tasks=[task(cooldown=3, valid=False)], home=True)
        response = Agent(Config(layout_mode='explicit')).decide(data)
        self.assertNotIn('11', response['roleCommandMap'])

    def test_cooldown_longer_than_daylight_does_not_start_trip(self):
        _, _, _, ledger, _ = self.dispatch(case(55, tasks=[task(cooldown=20, valid=False)], home=True))
        self.assertFalse(ledger.commands)

    def test_short_verified_stream_can_fit_where_unknown_cannot(self):
        data = case(58, x=5)
        _, _, _, ledger, _ = self.dispatch(data)
        self.assertFalse(ledger.commands)
        mem = Memory()
        mem.task_outcomes.append(record(data['teamOur']['playerTasks'][0]))
        _, _, _, ledger, mem = self.dispatch(data, mem)
        self.assertEqual(ledger.commands['11']['action'], 'acceptTask')
        self.assertEqual(mem.task_timeout, 15)

    def test_budget_counts_confirmation_variation_and_repair(self):
        t, cfg = task(), Config()
        self.assertEqual(task_budget(t, cfg, [])['rounds'], 13)
        self.assertEqual(task_budget(t, cfg, [record(t)])['rounds'], 6)
        self.assertEqual(task_budget(t, cfg, [record(t, 4), record(t, 7)])['rounds'], 10)

    def test_failures_never_look_like_fast_success(self):
        t, cfg = task(), Config()
        for elapsed in (2, 16):
            budget = task_budget(t, cfg, [record(t), record(t, elapsed, False)])
            self.assertGreaterEqual(budget['rounds'], 13)
            self.assertEqual(budget['source'], 'failure_fallback')

    def test_failed_task_blocks_late_accept(self):
        data = case(58, x=5)
        mem = Memory()
        mem.task_outcomes.extend([record(data['teamOur']['playerTasks'][0]),
                                 record(data['teamOur']['playerTasks'][0], 15, False)])
        self.assertFalse(self.dispatch(data, mem)[3].commands)

    def test_failure_before_reading_contract_invalidates_fast_history(self):
        t = task()
        failed = record(t, 16, False)
        failed['signature'] = None
        budget = task_budget(t, Config(), [record(t), failed])
        self.assertEqual(budget['rounds'], 16)
        self.assertEqual(budget['source'], 'failure_fallback')

    def test_metadata_changes_and_untyped_history_use_fallback(self):
        t, cfg = task(), Config()
        old = record(t)
        for key, value in [('taskType', 'different'), ('timeoutRounds', 20),
                           ('scoreReward', 200), ('goldReward', 200)]:
            changed = dict(t, **{key: value})
            self.assertEqual(task_budget(changed, cfg, [old])['source'], 'unknown')
        for key in ('signature', 'descriptor'):
            incomplete = dict(old)
            incomplete.pop(key)
            self.assertEqual(task_budget(t, cfg, [incomplete])['source'], 'unknown')

    def test_new_family_does_not_inherit_old_fast_samples(self):
        t = task()
        history = [record(t), record(t, 10, family='new/task_#')]
        budget = task_budget(t, Config(), history)
        self.assertEqual(budget['samples'], 1)
        self.assertEqual(budget['rounds'], 12)

    def test_shape_and_workflow_are_part_of_family_identity(self):
        contract = dict(family='same/task_#', example={'total': 0})
        self.assertNotEqual(task_signature(contract),
                            task_signature(dict(contract, input_kind='logs')))
        self.assertNotEqual(task_signature(contract),
                            task_signature(dict(contract, example={'token': ''})))
        self.assertNotEqual(task_signature(contract),
                            task_signature(dict(contract, kind='check_token')))

    def test_return_route_and_confirmation_are_reserved(self):
        data = case(62, x=5, home=True)
        mem = Memory()
        mem.task_outcomes.append(record(data['teamOur']['playerTasks'][0]))
        self.assertFalse(self.dispatch(data, mem)[3].commands)

    def test_operator_post_overrides_nearby_base_return(self):
        data = case(55, x=5, home=True)
        t, c, n, l = setup_case(data, layout_mode='explicit')
        l.operator_posts[11] = (14, 14)
        mem = Memory()
        mem.task_outcomes.append(record(data['teamOur']['playerTasks'][0]))
        self.assertFalse(pioneer_task_options(t, c, mem, n, l, t.pioneer))

    def test_active_task_and_recall_never_plan_new_trip(self):
        data = case(x=5)
        t, c, n, l = setup_case(data)
        mem = Memory(return_targets={11: 20})
        self.assertFalse(pioneer_task_options(t, c, mem, n, l, t.pioneer))
        mem.return_targets.clear()
        t.phase_task = 'current task'
        self.assertFalse(pioneer_task_options(t, c, mem, n, l, t.pioneer))

    def test_unreachable_point_releases_previous_target(self):
        data = case(tasks=[task(cooldown=3, valid=False)])
        data['teamEnemy']['roles'] = [unit(50 + y, 'wall', 3, y) for y in range(15)]
        mem = Memory(task_target=(6, 5))
        self.assertIsNone(self.dispatch(data, mem)[4].task_target)

    def test_cooldown_endpoints_avoid_building_and_reserved_posts(self):
        data = case(x=5, tasks=[task(cooldown=3, valid=False)])
        t, c, n, l = setup_case(data, layout_mode='explicit')
        l.wall_cells.add((5, 5))
        l.operator_posts[10] = (5, 4)
        l.reserved.add((5, 6))
        options = pioneer_task_options(t, c, Memory(), n, l, t.pioneer)
        self.assertTrue(options)
        self.assertNotIn(options[0]['cell'], {(5, 5), (5, 4), (5, 6)})

    def test_wait_cell_does_not_cut_corridor(self):
        data = case(x=5)
        data['teamEnemy']['roles'] = [unit(50 + y, 'wall', 5, y) for y in range(15) if y != 5]
        t, c, n, l = setup_case(data, layout_mode='explicit')
        self.assertFalse(wait_cell_safe(t, n, l, t.pioneer, (5, 5), set()))

    def test_visible_threat_prevents_cooldown_wait(self):
        data = case(x=5, tasks=[task(cooldown=3, valid=False)])
        data['robot']['roles'] = [unit(99, 'smallRobot', 6, 7, attackRange=1, targetTeam='challenger')]
        self.assertFalse(self.dispatch(data)[3].commands)

    def test_two_step_plan_overlaps_cooldown_with_first_task(self):
        data = case(x=5, tasks=[task(), task(10, 5, cooldown=20, valid=False)])
        t, c, n, l = setup_case(data, layout_mode='explicit')
        mem = Memory()
        options = pioneer_task_options(t, c, mem, n, l, t.pioneer)
        chosen = choose_task(options, mem)
        self.assertEqual(chosen['point'], (6, 5))
        self.assertEqual(chosen['count'], 2)
        self.assertEqual(chosen['next_point'], (10, 5))
        self.assertLess(chosen['horizon'], 20 + 2 * chosen['budget']['rounds'])

    def test_small_route_advantage_keeps_target_but_extra_task_switches(self):
        def option(point, horizon, count=2):
            return dict(point=point, cell=(point[0]-1, point[1]), count=count,
                        horizon=horizon, finish=10, total_value=200, rate=200/horizon,
                        route=(1, (2, 2)), budget=dict(uncertainty=2))
        old, new = option((6, 5), 30), option((10, 5), 29)
        mem = Memory(task_target=old['point'])
        self.assertIs(choose_task([old, new], mem), old)
        old['count'] = 1
        self.assertIs(choose_task([old, new], mem), new)

    def test_outcome_records_task_identity_before_reset(self):
        data = case(15, x=5)
        data['lastRoundRoleActionResults'] = {'11': True}
        t, c, _, _ = setup_case(data)
        descriptor = task_descriptor(data['teamOur']['playerTasks'][0])
        contract = dict(family='log/task_#', input_kind='logs', example={'total': 0})
        mem = Memory(day=1, task_text='old', task_started=10, task_point=(6, 5), task_timeout=15,
                     submitted=(14, '{"total":2}'), contract=copy.deepcopy(contract), task_descriptor=descriptor)
        mem.observe(t, c)
        outcome = mem.task_outcomes[-1]
        self.assertEqual(outcome['rounds'], 5)
        self.assertTrue(outcome['completionObserved'])
        self.assertEqual(outcome['descriptor'], descriptor)
        self.assertEqual(outcome['signature'], task_signature(contract))
        self.assertFalse(mem.task_descriptor)

    def test_night_clears_target_without_clearing_learning(self):
        data = case(70)
        t, c, _, _ = setup_case(data)
        mem = Memory(day=1, task_target=(6, 5), task_wait_cell=(5, 5))
        mem.task_outcomes.append(record(data['teamOur']['playerTasks'][0]))
        mem.observe(t, c)
        self.assertIsNone(mem.task_target)
        self.assertEqual(len(mem.task_outcomes), 1)


if __name__ == '__main__':
    unittest.main()
