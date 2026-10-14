"""Opening purchase boundaries and the transition back to ordinary production."""
import copy
import unittest

import test_first_day_offense as offense
import test_first_day_pioneer_shopping as shopping
import test_opening_boss_spending as opening
from test_first_day_wall_block import ENTRY, memory_for
from test_robot_assault import fortified_case, TOWERS, WALLS
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.intelligence import Memory
from agent.model import SUMMON_ORDERS
from agent.spending import first_day_purchase_reserve
from agent.worker_jobs import resume_daytime_jobs


BOSS, BASE = shopping.BOSS, shopping.BASE
REASON = 'insufficient_first_day_robots'


class FirstDayRobotMinimumTests(unittest.TestCase):
    def assert_no_robot_orders(self, commands):
        self.assertFalse(any(c['action'] in ('buy', 'use') and c.get('name') in SUMMON_ORDERS
                             for c in commands.values()), commands)

    def test_worker_purchase_requires_two_after_reserving_base_gold(self):
        for gold, quantity in ((220, 0), (339, 0), (340, 2), (459, 2), (460, 3)):
            with self.subTest(gold=gold):
                data = opening.OpeningBossSpendingTests().data(gold=gold)
                turn, cfg, nav, ledger = fortified_case(data)
                memory = Memory()
                accepted = summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS)
                self.assertEqual(accepted, quantity > 0)
                if quantity:
                    self.assertEqual(ledger.commands['1'], command('buy', name=BOSS, num=quantity))
                    self.assertEqual(ledger.gold, gold - quantity * 120)
                    self.assertFalse(memory.night_scout.cancelled)
                else:
                    self.assertFalse(ledger.commands)
                    self.assertEqual(ledger.gold, gold)
                    self.assertEqual(memory.night_scout.cancel_reason, REASON)
                    self.assertEqual(first_day_purchase_reserve(turn, memory, ledger), 100)

    def test_current_shop_price_sets_the_boundary(self):
        for gold, quantity in ((173, 0), (174, 2)):
            with self.subTest(gold=gold):
                data = opening.OpeningBossSpendingTests().data(gold=gold)
                next(item for item in data['weaponShopList'] if item['name'] == BOSS)['price'] = 37
                turn, cfg, nav, ledger = fortified_case(data)
                memory = Memory()
                self.assertEqual(summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS),
                                 quantity > 0)
                self.assertEqual(ledger.commands.get('1', {}).get('num', 0), quantity)

    def test_pioneer_buys_base_then_two_orders_at_exact_budget(self):
        fixture = shopping.FirstDayPioneerShoppingTests()
        data, memory = fixture.data(gold=340), Memory()
        _, _, ledger = fixture.prepare(data, memory)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))
        fixture.next_turn(data, [BASE], gold=240)
        _, _, ledger = fixture.prepare(data, memory)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=2))
        self.assertEqual(ledger.gold, 0)
        self.assertFalse(memory.night_scout.cancelled)

    def test_pioneer_short_budget_cancels_without_waiting_for_future_income(self):
        fixture = shopping.FirstDayPioneerShoppingTests()
        for gold in (0, 99, 100, 220, 339):
            with self.subTest(gold=gold):
                data, memory = fixture.data(gold=gold), Memory()
                _, _, ledger = fixture.prepare(data, memory)
                self.assert_no_robot_orders(ledger.commands)
                self.assertTrue(memory.night_scout.cancelled)
                self.assertNotIn('robot_purchase', ledger.spending_plan)
                fixture.next_turn(data, [BASE], gold=1000)
                _, _, ledger = fixture.prepare(data, memory)
                self.assert_no_robot_orders(ledger.commands)
                self.assertEqual(memory.first_day_pioneer['phase'], 'return')
                self.assertTrue(memory.first_day_pioneer['robots_cancelled'])

    def test_pioneer_capacity_cannot_reduce_initial_purchase_to_one(self):
        fixture = shopping.FirstDayPioneerShoppingTests()
        for space, quantity in ((1, 0), (2, 2)):
            with self.subTest(space=space):
                data, memory = fixture.data(), Memory()
                data['teamOur']['roles'][2].update(backpack=[BASE], backPackCapability=space + 1)
                _, _, ledger = fixture.prepare(data, memory)
                self.assertEqual(ledger.commands.get('2', {}).get('num', 0), quantity)
                self.assertEqual(memory.night_scout.cancelled, quantity == 0)

    def test_late_pioneer_trip_keeps_two_order_minimum_for_carried_work(self):
        fixture = shopping.FirstDayPioneerShoppingTests()
        data, memory = fixture.data(round_no=69, gold=240), Memory()
        data['teamOur']['roles'][2]['backpack'] = [BASE]
        _, _, ledger = fixture.prepare(data, memory)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=2))
        self.assertFalse(memory.night_scout.cancelled)

    def test_worker_capacity_and_daylight_cannot_create_single_order_batch(self):
        for tick, space in ((35, 1), (68, 100)):
            with self.subTest(tick=tick, space=space):
                data = opening.OpeningBossSpendingTests().data(round_no=tick)
                data['teamOur']['roles'][1]['backPackCapability'] = space
                data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                            if r['roleType'] != 'pioneer']
                turn, cfg, nav, ledger = fortified_case(data)
                memory = Memory()
                self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                   TOWERS, WALLS, excluded={2}))
                self.assert_no_robot_orders(ledger.commands)
                self.assertTrue(memory.night_scout.cancelled)

    def test_unfunded_worker_returns_to_mining_instead_of_waiting_at_shop(self):
        for gold in (0, 119, 120, 239):
            with self.subTest(gold=gold):
                memory = Memory()
                worker, ledger = offense.step(offense.case(35, gold=gold), memory, walls=[])
                self.assertIsNone(worker)
                self.assertEqual(ledger.commands['10'], command('collect', (8, 20)))
                self.assertEqual(memory.first_day_offense['phase'], 'mining')
                self.assertIsNone(ledger.first_day_offense_worker_id)
                self.assert_no_robot_orders(ledger.commands)

    def test_agent_releases_current_wall_hold_and_stale_jobs_on_same_turn(self):
        data = offense.case(34, gold=339)
        data['teamOur']['playerTasks'] = shopping.FirstDayPioneerShoppingTests().data()['teamOur']['playerTasks']
        data['teamOur']['playerTasks'][0]['isValid'] = True
        data['weaponShopList'].append({'name': BASE, 'price': 100})
        data['teamOur']['roles'][3]['pos'] = {'x': 14, 'y': 20}
        agent = Agent(offense.cfg(walls=[]))
        agent.decide(copy.deepcopy(data))
        memory = next(iter(agent.sessions.values()))
        self.assertFalse(memory.night_scout.cancelled)
        memory.night_scout = memory_for()
        memory.first_day_offense.update(key=(1, 99, (30, 10)), worker_id=10, phase='block')
        memory.daytime_jobs = {uid: dict(kind='robot_buy', target=(15, 20), name=BOSS, quantity=1)
                               for uid in (10, 12)}
        memory.sold_workers.add(10)
        memory.return_targets[10] = 20
        data['roundNo'] = 35
        data['teamOur']['playerTasks'][0]['isValid'] = False
        data['teamOur']['roles'][1]['pos'] = {'x': ENTRY[0], 'y': ENTRY[1]}
        data['mapInfo']['zones'] += [dict(neutralType='iron', pos=dict(x=30, y=13), remain=10),
                                    dict(neutralType='vendor', pos=dict(x=28, y=14))]
        response = agent.decide(copy.deepcopy(data))
        self.assertEqual(response['roleCommandMap']['10'], command('collect', (30, 13)))
        self.assertEqual(response['roleCommandMap']['11'], command('buy', name=BASE, num=1))
        self.assert_no_robot_orders(response['roleCommandMap'])
        self.assertTrue(memory.night_scout.cancelled)
        self.assertIsNone(memory.night_scout.worker_id)
        self.assertIsNone(memory.night_scout.wall_entry)
        self.assertNotIn(10, memory.sold_workers)
        self.assertFalse(any(job['kind'] == 'robot_buy' for job in memory.daytime_jobs.values()))
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)

    def test_rejected_pioneer_purchase_rechecks_the_two_order_budget(self):
        fixture = shopping.FirstDayPioneerShoppingTests()
        data, memory = fixture.data(gold=240), Memory()
        data['teamOur']['roles'][2]['backpack'] = [BASE]
        _, _, ledger = fixture.prepare(data, memory)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=2))
        fixture.next_turn(data, [BASE], gold=120, rejected=True)
        _, _, ledger = fixture.prepare(data, memory)
        self.assertEqual(memory.robot_summon_state['purchased'], 0)
        self.assertTrue(memory.night_scout.cancelled)
        self.assert_no_robot_orders(ledger.commands)

    def test_agent_withdraws_wall_approach_and_collects_without_an_extra_move(self):
        data = offense.case(35, gold=339)
        data['teamOur']['playerTasks'] = shopping.FirstDayPioneerShoppingTests().data()['teamOur']['playerTasks']
        data['weaponShopList'].append({'name': BASE, 'price': 100})
        data['teamOur']['roles'][3]['pos'] = {'x': 14, 'y': 20}
        agent = Agent(offense.cfg(walls=[]))
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('collect', (8, 20)))
        self.assert_no_robot_orders(response)
        self.assertTrue(next(iter(agent.sessions.values())).night_scout.cancelled)

    def test_rejected_worker_purchase_cancels_even_during_retry_cooldown(self):
        data = offense.case(35, gold=240)
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        agent = Agent(offense.cfg(walls=[]))
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=BOSS, num=2))
        data['roundNo'] = 36
        data['lastRoundRoleActionResults'] = {'10': False}
        data['teamOur']['goldNum'] = 120
        response = agent.decide(data)['roleCommandMap']
        memory = next(iter(agent.sessions.values()))
        self.assertTrue(memory.night_scout.cancelled)
        self.assertEqual(memory.robot_summon_state['purchased'], 0)
        self.assertEqual(memory.first_day_offense['phase'], 'mining')
        self.assert_no_robot_orders(response)

    def test_two_confirmed_orders_allow_the_remaining_third_order(self):
        data = opening.OpeningBossSpendingTests().data(gold=220)
        turn, cfg, nav, ledger = fortified_case(data)
        memory = Memory()
        memory.robot_summon_state = dict(day=1, count=2, positions=set(), pending=None,
                                        observed_round=-1, purchased=2, buyer=None)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS))
        self.assertEqual(ledger.commands['1'], command('buy', name=BOSS, num=1))
        self.assertFalse(memory.night_scout.cancelled)

    def test_cancellation_survives_more_gold_night_and_next_day(self):
        data = opening.OpeningBossSpendingTests().data(gold=220)
        turn, cfg, nav, ledger = fortified_case(data)
        memory = Memory()
        summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS)
        for tick in (11, 70, 130):
            with self.subTest(tick=tick):
                data['roundNo'] = tick
                data['teamOur']['goldNum'] = 1000
                data['teamOur']['roles'][1]['backpack'] = [BOSS]
                turn, cfg, nav, ledger = fortified_case(data)
                memory.daytime_jobs[1] = dict(kind='robot_buy', target=(4, 9), name=BOSS, quantity=3)
                self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS))
                resume_daytime_jobs(turn, cfg, memory, nav, ledger, TOWERS, WALLS, set())
                self.assert_no_robot_orders(ledger.commands)
                self.assertEqual(ledger.spending_plan['robot_blocked'], REASON)

    def test_day_two_without_prior_abort_still_allows_single_order(self):
        data = opening.OpeningBossSpendingTests().data(round_no=140, gold=120)
        turn, cfg, nav, ledger = fortified_case(data)
        memory = Memory()
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS))
        self.assertEqual(ledger.commands['1'], command('buy', name=BOSS, num=1))
        self.assertFalse(memory.night_scout.cancelled)


if __name__ == '__main__':
    unittest.main()
