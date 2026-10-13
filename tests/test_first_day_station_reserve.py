"""First-day purchase limits and the pioneer's reserved base heal."""
import copy
import unittest

from test_agent import unit
import test_opening_boss_spending as opening_fixtures
from test_robot_assault import fortified_case, TOWERS, WALLS
from agent.brain import Agent, observe_robot_summons, summon_best_robot
from agent.commands import command
from agent.economy import build, supplies, use_inventory
from agent.intelligence import Memory
from agent.model import distance
from agent.spending import (first_day_purchase_reserve, heal_station_with_pioneer,
                            plan_day_spending, prepare_station_reserve)


class FirstDayStationReserveTests(unittest.TestCase):
    def data(self, gold=668, round_no=10):
        data = opening_fixtures.OpeningBossSpendingTests().data(round_no=round_no, gold=gold)
        data['teamOur']['roles'][0]['health'] = 1500
        data['teamOur']['roles'][2].update(pos={'x': 5, 'y': 8}, health=500)
        data['weaponShopList'].append({'name': 'StationUpgradeVoucher2', 'price': 150})
        return data

    def buy(self, data, mem=None):
        turn, cfg, nav, ledger = fortified_case(data)
        mem = mem or Memory()
        result = summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS, excluded={2})
        return result, turn, cfg, nav, ledger, mem

    def test_first_day_purchase_is_capped_even_with_free_robots(self):
        for free in (False, True):
            with self.subTest(free=free):
                data = self.data(gold=1200)
                if free:
                    next(x for x in data['weaponShopList']
                         if x['name'] == 'BossRobotSummonOrder')['price'] = 0
                result, _, _, _, ledger, mem = self.buy(data)
                self.assertTrue(result)
                self.assertEqual(ledger.commands['1']['num'], 3)
                self.assertEqual(mem.robot_summon_state['purchased'], 3)

    def test_cap_survives_consumption_or_death_of_the_order_carrier(self):
        _, _, _, _, _, mem = self.buy(self.data())
        for dead in (False, True):
            with self.subTest(dead=dead):
                data = self.data(round_no=11)
                data['lastRoundRoleActionResults'] = {'1': True}
                data['teamOur']['roles'][1]['health'] = 0 if dead else 500
                result, _, _, _, ledger, _ = self.buy(data, mem)
                self.assertFalse(result)
                self.assertFalse(ledger.purchases)
                self.assertEqual(ledger.spending_plan['robot_blocked'], 'first_day_purchase_limit')

    def test_partial_batches_share_the_three_purchase_limit(self):
        _, _, _, _, ledger, mem = self.buy(self.data(gold=340))
        self.assertEqual(ledger.commands['1']['num'], 2)
        data = self.data(round_no=11)
        data['lastRoundRoleActionResults'] = {'1': True}
        result, _, _, _, ledger, _ = self.buy(data, mem)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['num'], 1)
        self.assertEqual(mem.robot_summon_state['purchased'], 3)

    def test_only_a_consecutive_explicit_purchase_rejection_releases_slots(self):
        for key in ('1', 1):
            for round_no, feedback, retry in ((11, False, True), (11, True, False),
                                             (11, None, False), (12, False, False)):
                with self.subTest(key=key, round_no=round_no, feedback=feedback):
                    _, _, _, _, _, mem = self.buy(self.data())
                    data = self.data(round_no=round_no)
                    data['lastRoundRoleActionResults'] = {key: feedback}
                    result, _, _, _, ledger, _ = self.buy(data, mem)
                    self.assertEqual(result, retry)
                    self.assertEqual(ledger.commands.get('1', {}).get('num'), 3 if retry else None)

    def test_walking_does_not_count_as_a_completed_purchase(self):
        data = self.data()
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 0}
        result, _, _, _, ledger, mem = self.buy(data)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['action'], 'move')
        self.assertEqual(ledger.work_jobs[1]['quantity'], 3)
        self.assertEqual(mem.robot_summon_state['purchased'], 0)

    def test_initial_carried_stock_including_dead_heroes_reserves_slots(self):
        for dead in (False, True):
            with self.subTest(dead=dead):
                data = self.data()
                data['teamOur']['roles'][1].update(
                    backpack=['BossRobotSummonOrder'] * 3, health=0 if dead else 500)
                result, _, _, _, ledger, _ = self.buy(data)
                self.assertFalse(result)
                self.assertFalse(ledger.purchases)

    def test_day_two_keeps_the_existing_ten_order_capacity(self):
        _, _, _, _, _, mem = self.buy(self.data())
        result, _, _, _, ledger, _ = self.buy(self.data(gold=1200, round_no=140), mem)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['num'], 10)

    def test_base_voucher_is_bought_before_any_robot_at_its_price_boundary(self):
        for gold, expected in ((99, False), (100, True), (1200, True)):
            with self.subTest(gold=gold):
                turn, cfg, nav, ledger = fortified_case(self.data(gold=gold))
                mem = Memory()
                result = prepare_station_reserve(turn, cfg, mem, nav, ledger)
                self.assertEqual(result, expected)
                self.assertEqual(ledger.commands.get('2'), command(
                    'buy', name='StationUpgradeVoucher1', num=1) if expected else None)
                self.assertEqual(ledger.gold, gold - (100 if expected else 0))

    def test_base_voucher_precedes_partial_robot_purchase(self):
        turn, cfg, nav, ledger = fortified_case(self.data(gold=350))
        mem = Memory()
        self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS))
        self.assertEqual(ledger.commands['1']['num'], 2)
        self.assertEqual(ledger.commands['2'], command('buy', name='StationUpgradeVoucher1', num=1))
        self.assertEqual(ledger.gold, 10)
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 120)

    def empty_guns_data(self, gold=485, round_no=10):
        data = self.data(gold=gold, round_no=round_no)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                    if r['roleType'] not in ('rocket', 'gatling')]
        data['teamOur']['roles'].append(unit(3, 'worker', 1, 9, health=500))
        return data

    def test_unbuilt_guns_receive_only_the_remainder_after_heal_and_three_robots(self):
        for gold, count, building in ((100, 0, False), (219, 0, False), (220, 1, False),
                                      (459, 2, False), (460, 3, False),
                                      (484, 3, False), (485, 3, True)):
            with self.subTest(gold=gold):
                turn, cfg, nav, ledger = fortified_case(self.empty_guns_data(gold))
                mem = Memory()
                self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
                self.assertEqual(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS,
                                                  excluded={3}), bool(count))
                self.assertEqual(ledger.commands.get('1', {}).get('num', 0), count)
                self.assertEqual(build(turn, cfg, mem, nav, ledger, turn.units[3],
                                       TOWERS, lambda _: 'rocket'), building)
                self.assertEqual(ledger.gold, gold - 100 - 120 * count - 25 * building)

    def test_agent_buys_heal_and_three_robots_before_building_its_first_gun(self):
        data = self.empty_guns_data()
        _, cfg, _, _ = fortified_case(data)
        cfg.llm_enabled = False
        response = Agent(cfg).decide(data)['roleCommandMap']
        self.assertEqual(response['2'], command('buy', name='StationUpgradeVoucher1', num=1))
        self.assertEqual(response['1'], command('buy', name='BossRobotSummonOrder', num=3))
        self.assertEqual(response['3'], command('build', TOWERS[0], name='rocket'))

    def test_walking_buyers_reserve_each_cost_once_and_leave_the_tower_remainder(self):
        data = self.empty_guns_data()
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 0}
        data['teamOur']['roles'][2]['pos'] = {'x': 7, 'y': 8}
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory()
        self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS, excluded={3}))
        self.assertEqual(ledger.commands['1']['action'], 'move')
        self.assertEqual(ledger.commands['2']['action'], 'move')
        self.assertEqual(mem.robot_summon_state['purchased'], 0)
        self.assertFalse(mem.robot_summon_state['station_purchased'])
        plan_day_spending(turn, cfg, mem, nav, ledger, TOWERS)
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 0)
        self.assertEqual(ledger.gold, 25)
        self.assertTrue(build(turn, cfg, mem, nav, ledger, turn.units[3], TOWERS, lambda _: 'rocket'))

    def test_new_turn_reserves_unbought_robot_instead_of_spending_it_on_a_gun(self):
        for gold, building in ((144, False), (145, True)):
            with self.subTest(gold=gold):
                data = self.empty_guns_data(gold)
                data['teamOur']['roles'][2]['backpack'] = ['StationUpgradeVoucher1']
                turn, cfg, nav, ledger = fortified_case(data)
                mem = Memory()
                mem.robot_summon_state = dict(day=1, count=0, purchased=2,
                    positions=set(), pending=None, observed_round=-1, buyer=None)
                self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 120)
                self.assertEqual(build(turn, cfg, mem, nav, ledger, turn.units[3],
                                       TOWERS, lambda _: 'rocket'), building)

    def test_day_two_releases_opening_reserves_and_can_build_with_only_25_gold(self):
        turn, cfg, nav, ledger = fortified_case(self.empty_guns_data(25, round_no=140))
        mem = Memory()
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 0)
        self.assertFalse(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertTrue(build(turn, cfg, mem, nav, ledger, turn.units[3], TOWERS, lambda _: 'rocket'))

    def test_ordinary_medicine_and_wall_stock_cannot_spend_the_opening_budget(self):
        from agent.wall_watch import prepare_watch
        data = self.data(gold=119)
        data['teamOur']['roles'][1]['health'] = 50
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory(wall_watch_id=1)
        self.assertIsNone(supplies(turn, cfg, mem, nav, ledger, turn.workers[0], urgent_only=True))
        _, reserved = prepare_watch(turn, cfg, mem, nav, ledger, turn.workers[0], WALLS)
        self.assertEqual(reserved, 0)
        self.assertFalse(any(c['action'] == 'buy' for c in ledger.commands.values()))

    def test_shop_prices_set_the_budget_instead_of_fixed_gold_thresholds(self):
        data = self.empty_guns_data(gold=159)
        for item in data['weaponShopList']:
            if item['name'] == 'StationUpgradeVoucher1':
                item['price'] = 60
            elif item['name'] == 'BossRobotSummonOrder':
                item['price'] = 25
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory()
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 135)
        self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS, excluded={3}))
        self.assertEqual(ledger.gold, 24)
        self.assertFalse(build(turn, cfg, mem, nav, ledger, turn.units[3], TOWERS, lambda _: 'rocket'))

    def test_walking_robot_budget_is_not_reserved_twice(self):
        data = self.data(gold=460)
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 0}
        _, turn, cfg, nav, ledger, mem = self.buy(data)
        self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertEqual(ledger.commands['2'], command('buy', name='StationUpgradeVoucher1', num=1))
        self.assertEqual(ledger.gold, 0)

    def test_pioneer_walks_to_shop_and_buys_on_arrival(self):
        data = self.data()
        data['teamOur']['roles'][2]['pos'] = {'x': 7, 'y': 8}
        _, turn, cfg, nav, ledger, mem = self.buy(data)
        self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertEqual(ledger.commands['2']['action'], 'move')
        self.assertFalse(mem.robot_summon_state['station_purchased'])
        data['roundNo'] += 1
        data['teamOur']['roles'][2]['pos'] = {'x': 5, 'y': 8}
        turn, cfg, nav, ledger = fortified_case(data)
        observe_robot_summons(turn, mem)
        self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertEqual(ledger.commands['2']['action'], 'buy')

    def test_station_purchase_is_single_and_can_retry_after_explicit_rejection(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                _, turn, cfg, nav, ledger, mem = self.buy(self.data())
                self.assertTrue(prepare_station_reserve(turn, cfg, mem, nav, ledger))
                data = self.data(round_no=11)
                data['lastRoundRoleActionResults'] = {'1': True, '2': not rejected}
                turn, cfg, nav, ledger = fortified_case(data)
                observe_robot_summons(turn, mem)
                self.assertEqual(prepare_station_reserve(turn, cfg, mem, nav, ledger), rejected)

    def test_initial_base_voucher_counts_after_it_is_consumed_for_healing(self):
        data = self.data()
        data['teamOur']['roles'][2]['backpack'] = ['StationUpgradeVoucher1']
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory()
        self.assertFalse(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertTrue(mem.robot_summon_state['station_purchased'])
        data['roundNo'] += 1
        data['teamOur']['roles'][0].update(level=2, health=3000)
        data['teamOur']['roles'][2]['backpack'] = []
        turn, cfg, nav, ledger = fortified_case(data)
        self.assertFalse(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger, include_robots=False), 0)

    def test_reserve_respects_existing_stock_capacity_and_task_or_recall_cutoff(self):
        for condition in ('owned', 'full', 'task', 'recall', 'max_level', 'missing_shop_item'):
            with self.subTest(condition=condition):
                data = self.data()
                hero = data['teamOur']['roles'][2]
                if condition == 'owned':
                    hero['backpack'] = ['StationUpgradeVoucher1']
                elif condition == 'full':
                    hero['backPackCapability'] = 0
                elif condition == 'task':
                    data['phaseTask'] = 'active task'
                elif condition == 'recall':
                    data['roundNo'] = 67
                elif condition == 'max_level':
                    data['teamOur']['roles'][0]['level'] = 3
                else:
                    data['weaponShopList'] = [x for x in data['weaponShopList']
                                              if x['name'] != 'StationUpgradeVoucher1']
                _, turn, cfg, nav, ledger, mem = self.buy(data)
                self.assertFalse(prepare_station_reserve(turn, cfg, mem, nav, ledger))

    def test_healing_uses_strict_500_boundary_for_both_voucher_levels_day_and_night(self):
        for round_no in (10, 70, 391, 461):
            for level in (1, 2):
                for health in (499, 500, 501, 1500, 3000):
                    with self.subTest(round_no=round_no, level=level, health=health):
                        data = self.data(round_no=round_no)
                        data['teamOur']['roles'][0].update(level=level, health=health)
                        data['teamOur']['roles'][2].update(
                            pos={'x': 2, 'y': 10}, backpack=[f'StationUpgradeVoucher{level}'])
                        turn, _, nav, ledger = fortified_case(data)
                        mem = Memory(station_health_peaks={level: 4500})
                        self.assertEqual(heal_station_with_pioneer(turn, mem, nav, ledger), health < 500)
                        self.assertEqual(ledger.commands.get('2'), command(
                            'use', (3, 10), name=f'StationUpgradeVoucher{level}') if health < 500 else None)
                        if health >= 500:
                            self.assertFalse(use_inventory(turn, nav, ledger, turn.pioneer, mem=mem))

    def test_healing_walks_to_base_at_night_and_on_final_daylight_turn(self):
        for round_no in (69, 70):
            data = self.data(round_no=round_no)
            data['teamOur']['roles'][0]['health'] = 499
            data['teamOur']['roles'][2]['backpack'] = ['StationUpgradeVoucher1']
            turn, _, nav, ledger = fortified_case(data)
            self.assertTrue(heal_station_with_pioneer(turn, Memory(), nav, ledger))
            self.assertEqual(ledger.commands['2']['action'], 'move')
            target = ledger.commands['2']['targetPos'][0]
            self.assertLess(min(distance((target['x'], target['y']), p) for p in turn.station.cells),
                            min(distance(turn.pioneer.pos, p) for p in turn.station.cells))

    def test_reserved_base_heal_precedes_the_pioneers_own_medicine(self):
        data = self.data(round_no=70)
        data['teamOur']['roles'][0]['health'] = 499
        data['teamOur']['roles'][2].update(pos={'x': 2, 'y': 10}, health=100,
                                          backpack=['Medicine', 'StationUpgradeVoucher1'])
        turn, _, nav, ledger = fortified_case(data)
        self.assertTrue(heal_station_with_pioneer(turn, Memory(), nav, ledger))
        self.assertEqual(ledger.commands['2'], command('use', (3, 10), name='StationUpgradeVoucher1'))

    def test_wrong_tier_max_level_missing_or_claimed_base_cannot_consume_voucher(self):
        for level, name, claimed in ((1, 'StationUpgradeVoucher2', False),
                                     (2, 'StationUpgradeVoucher1', False),
                                     (3, 'StationUpgradeVoucher2', False),
                                     (1, 'StationUpgradeVoucher1', True)):
            with self.subTest(level=level, name=name, claimed=claimed):
                data = self.data()
                data['teamOur']['roles'][0].update(level=level, health=499)
                data['teamOur']['roles'][2].update(pos={'x': 2, 'y': 10}, backpack=[name])
                turn, _, nav, ledger = fortified_case(data)
                if claimed:
                    ledger.upgrade_claims.add(turn.station.id)
                self.assertFalse(heal_station_with_pioneer(turn, Memory(), nav, ledger))
                self.assertFalse(ledger.commands)

    def test_agent_healing_preempts_task_and_night_weapon_assignment(self):
        for round_no, phase in ((10, 'active task'), (70, '')):
            with self.subTest(round_no=round_no):
                data = self.data(round_no=round_no)
                data['teamOur']['roles'][0]['health'] = 499
                data['teamOur']['roles'][2].update(
                    pos={'x': 2, 'y': 10}, backpack=['StationUpgradeVoucher1'])
                data['phaseTask'] = phase
                _, cfg, _, _ = fortified_case(data)
                cfg.llm_enabled = False
                agent = Agent(cfg)
                result = agent.decide(copy.deepcopy(data))
                self.assertEqual(result['roleCommandMap'].get('2'), command(
                    'use', (3, 10), name='StationUpgradeVoucher1'))
                self.assertEqual(agent.decide(copy.deepcopy(data)), result)

    def test_agent_buys_three_robots_and_one_pioneer_voucher(self):
        data = self.data()
        _, cfg, _, _ = fortified_case(data)
        cfg.llm_enabled = False
        result = Agent(cfg).decide(data)['roleCommandMap']
        self.assertEqual(result.get('1'), command('buy', name='BossRobotSummonOrder', num=3))
        self.assertEqual(result.get('2'), command('buy', name='StationUpgradeVoucher1', num=1))


if __name__ == '__main__':
    unittest.main()
