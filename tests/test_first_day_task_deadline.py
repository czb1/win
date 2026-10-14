"""Finite task-to-shopping transitions; no simulation or live task execution."""
import copy
import unittest
from unittest.mock import patch

import test_first_day_pioneer_shopping as shopping
from test_agent import unit
from test_robot_assault import fortified_case
from agent.brain import Agent, observe_robot_summons
from agent.commands import command
from agent.intelligence import Intelligence, Memory
from agent.model import distance, pos
from agent.spending import first_day_pioneer_task_due, FIRST_DAY_ROBOT_TASK_STOP


BASE, BOSS = shopping.BASE, shopping.BOSS


class FirstDayTaskDeadlineTests(unittest.TestCase):
    def data(self, tick=53, gold=668):
        data = shopping.FirstDayPioneerShoppingTests().data(round_no=tick, gold=gold)
        data['phaseTask'] = 'unfinished opening task'
        data['teamOur']['roles'][2]['pos'] = {'x': 8, 'y': 10}
        data['teamOur']['playerTasks'][0].update(coldDownRounds=30, timeoutRounds=60)
        data['mapInfo']['zones'].append(
            {'neutralType': 'challengerTaskPoint1', 'pos': {'x': 8, 'y': 11}})
        return data

    def agent(self, data, origin=0):
        _, cfg, _, _ = fortified_case(data)
        cfg.round_origin = origin
        return Agent(cfg)

    def test_51_to_53_hold_task_then_54_moves_to_shop_for_both_origins(self):
        for origin in (0, 1):
            with self.subTest(origin=origin):
                data = self.data()
                agent = self.agent(data, origin)
                with patch.object(Intelligence, 'task', return_value=('', '')) as task:
                    for tick in (51, 52, 53):
                        data['roundNo'] = tick + origin
                        response = agent.decide(copy.deepcopy(data))
                        self.assertNotIn('2', response['roleCommandMap'])
                    self.assertEqual(task.call_count, 3)
                    data['roundNo'] = 54 + origin
                    response = agent.decide(copy.deepcopy(data))
                    mem = next(iter(agent.sessions.values()))
                    self.assertEqual(task.call_count, 3)
                    self.assertEqual(response['roleCommandMap']['2']['action'], 'move')
                    destination = pos(response['roleCommandMap']['2']['targetPos'][0])
                    self.assertLess(distance(destination, (4, 9)), distance((8, 10), (4, 9)))
                    self.assertGreater(distance(destination, (8, 11)), 1)
                    self.assertEqual(mem.stop_reason, FIRST_DAY_ROBOT_TASK_STOP)
                    self.assertEqual(mem.first_day_pioneer['trigger'], 'tick_54')
                    self.assertEqual(mem.first_day_pioneer['phase'], 'shop')
                    self.assertIsNone(mem.task_target)
                    self.assertIsNone(mem.task_wait_cell)
                    self.assertFalse(response['executeCmd'])
                    self.assertFalse(response['prompt'])
                    self.assertEqual(agent.decide(copy.deepcopy(data)), response)
                    self.assertEqual(task.call_count, 3)

    def test_interrupted_task_buys_and_uses_orders_even_before_phase_clears(self):
        data = self.data(54)
        agent = self.agent(data)
        with patch.object(Intelligence, 'task', return_value=('unexpected task', 'unexpected command')) as task:
            response = agent.decide(copy.deepcopy(data))
            self.assertEqual(response['roleCommandMap']['2']['action'], 'move')
            # Observed arrival and backpack snapshots, with a delayed task-clear report.
            data['teamOur']['roles'][2]['pos'] = {'x': 5, 'y': 8}
            for tick, backpack, gold, expected in (
                    (55, [], 668, command('buy', name=BASE, num=1)),
                    (56, [BASE], 568, command('buy', name=BOSS, num=3)),
                    (57, [BASE, BOSS, BOSS, BOSS], 208, None),
                    (58, [BASE, BOSS, BOSS], 208, None),
                    (59, [BASE, BOSS], 208, None)):
                data.update(roundNo=tick, lastRoundRoleActionResults={'2': True})
                data['teamOur']['roles'][2]['backpack'] = backpack
                data['teamOur']['goldNum'] = gold
                response = agent.decide(copy.deepcopy(data))
                action = response['roleCommandMap']['2']
                if expected:
                    self.assertEqual(action, expected)
                else:
                    self.assertEqual((action['action'], action['name']), ('use', BOSS))
                    self.assertEqual(len(action['targetPos']), 1)
                self.assertFalse(response['prompt'])
                self.assertFalse(response['executeCmd'])
            task.assert_not_called()
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.robot_summon_state['purchased'], 3)
        self.assertEqual(mem.robot_summon_state['count'], 3)
        self.assertEqual(len(mem.robot_summon_state['positions']), 3)
        data.update(roundNo=60, phaseTask='')
        data['teamOur']['roles'][2]['backpack'] = [BASE]
        agent.decide(copy.deepcopy(data))
        self.assertEqual(mem.first_day_pioneer['phase'], 'return')
        self.assertEqual(mem.task_outcomes[-1]['reason'], FIRST_DAY_ROBOT_TASK_STOP)
        self.assertFalse(mem.task_outcomes[-1]['completionObserved'])

    def test_pending_task_work_and_ready_answer_do_not_delay_deadline(self):
        for state in ('answer', 'python', 'pending'):
            with self.subTest(state=state):
                data = self.data()
                agent = self.agent(data)
                with patch.object(Intelligence, 'task', return_value=('', '')):
                    agent.decide(copy.deepcopy(data))
                mem = next(iter(agent.sessions.values()))
                setattr(mem, state, ('cmd', 53) if state == 'pending' else 'ready work')
                data['roundNo'] = 54
                with patch.object(Intelligence, 'task') as task:
                    response = agent.decide(copy.deepcopy(data))
                    task.assert_not_called()
                self.assertEqual(response['roleCommandMap']['2']['action'], 'move')
                self.assertFalse(response['executeCmd'])
                self.assertFalse(response['prompt'])

    def test_failed_move_keeps_shopping_even_if_gold_then_falls(self):
        data = self.data(54)
        agent = self.agent(data)
        with patch.object(Intelligence, 'task') as task:
            agent.decide(copy.deepcopy(data))
            data.update(roundNo=55, lastRoundRoleActionResults={'2': False})
            data['teamOur']['goldNum'] = 100
            response = agent.decide(copy.deepcopy(data))
            task.assert_not_called()
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.stop_reason, FIRST_DAY_ROBOT_TASK_STOP)
        self.assertEqual(mem.first_day_pioneer['phase'], 'shop')
        self.assertEqual(response['roleCommandMap']['2']['action'], 'move')

    def test_cancelled_or_unaffordable_offense_keeps_task(self):
        for condition in ('enemy_gatling', 'insufficient_gold', 'missing_enemy', 'missing_item', 'full_bag'):
            with self.subTest(condition=condition):
                data = self.data(54)
                if condition == 'enemy_gatling':
                    data['teamEnemy']['roles'].append(unit(98, 'gatling', 12, 12))
                elif condition == 'insufficient_gold':
                    data['teamOur']['goldNum'] = 339
                elif condition == 'missing_enemy':
                    data['teamEnemy']['roles'] = []
                elif condition == 'missing_item':
                    data['weaponShopList'] = [v for v in data['weaponShopList'] if v['name'] != BOSS]
                else:
                    data['teamOur']['roles'][2].update(backPackCapability=2, backpack=['stone'] * 2)
                agent = self.agent(data)
                with patch.object(Intelligence, 'task', return_value=('', '')) as task:
                    response = agent.decide(copy.deepcopy(data))
                    task.assert_called_once()
                mem = next(iter(agent.sessions.values()))
                self.assertNotIn('2', response['roleCommandMap'])
                self.assertFalse(mem.first_day_pioneer)
                self.assertFalse(mem.stop_reason)

    def test_budget_uses_actual_prices_base_reserve_and_remaining_capacity(self):
        for price, gold, backpack, capacity, expected in (
                (120, 339, [], 40, False), (120, 340, [], 40, True),
                (150, 399, [], 40, False), (150, 400, [], 40, True),
                (120, 240, [BASE], 40, True), (120, 1000, [], 2, False),
                (120, 1000, [], 3, True)):
            with self.subTest(price=price, gold=gold, backpack=backpack, capacity=capacity):
                data = self.data(54, gold)
                data['teamOur']['roles'][2].update(backpack=backpack, backPackCapability=capacity)
                next(v for v in data['weaponShopList'] if v['name'] == BOSS)['price'] = price
                turn, _, _, ledger = fortified_case(data)
                self.assertEqual(first_day_pioneer_task_due(turn, Memory(), ledger), expected)

    def test_purchase_and_summon_caps_do_not_interrupt_task(self):
        for field, value in (('purchased', 3), ('count', 10)):
            with self.subTest(field=field):
                turn, _, _, ledger = fortified_case(self.data(54))
                mem = Memory()
                observe_robot_summons(turn, mem)[field] = value
                self.assertFalse(first_day_pioneer_task_due(turn, mem, ledger))

    def test_owned_orders_respect_summon_limit_and_new_cancellation(self):
        for condition in ('usable', 'daily_limit', 'enemy_gatling', 'base_unaffordable'):
            with self.subTest(condition=condition):
                data = self.data(54, gold=0)
                data['teamOur']['roles'][2]['backpack'] = [BASE, BOSS]
                if condition == 'enemy_gatling':
                    data['teamEnemy']['roles'].append(unit(98, 'gatling', 12, 12))
                elif condition == 'base_unaffordable':
                    data['teamOur']['roles'][2]['backpack'] = [BOSS]
                turn, _, _, ledger = fortified_case(data)
                mem = Memory()
                if condition == 'daily_limit':
                    observe_robot_summons(turn, mem)['count'] = 10
                self.assertEqual(first_day_pioneer_task_due(turn, mem, ledger), condition == 'usable')

    def test_night_and_later_days_do_not_trigger_first_day_deadline(self):
        for tick in (70, 184):
            with self.subTest(tick=tick):
                data = self.data(tick)
                turn, _, _, ledger = fortified_case(data)
                self.assertFalse(first_day_pioneer_task_due(turn, Memory(), ledger))

    def test_critical_base_heal_keeps_priority_over_shopping(self):
        data = self.data(54)
        data['teamOur']['roles'][0]['health'] = 300
        data['teamOur']['roles'][2].update(pos={'x': 5, 'y': 10}, backpack=[BASE])
        agent = self.agent(data)
        with patch.object(Intelligence, 'task') as task:
            response = agent.decide(copy.deepcopy(data))
            task.assert_not_called()
        action = response['roleCommandMap']['2']
        self.assertEqual((action['action'], action['name']), ('use', BASE))
        self.assertIn(pos(action['targetPos'][0]), {(3, 11), (4, 11), (3, 10), (4, 10)})


if __name__ == '__main__':
    unittest.main()
