"""Opening construction/BOSS priority and unrestricted surplus purchasing."""
import copy
import unittest
from unittest.mock import patch

from test_agent import unit
import test_robot_assault as robot_fixtures
from test_robot_assault import fortified_case, TOWERS, WALLS
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.economy import supplies, use_inventory
from agent.intelligence import Memory
from agent.recovery import Recovery
from agent.spending import plan_day_spending
from agent.worker_jobs import resume_daytime_jobs


class OpeningBossSpendingTests(unittest.TestCase):
    def data(self, round_no=10, gold=668):
        data = robot_fixtures.BestRobotSummoningTests().shop_data(round_no=round_no, gold=gold)
        for role in data['teamOur']['roles']:
            if role['roleType'] in ('rocket', 'gatling', 'wall'):
                role['level'] = 1
        data['weaponShopList'] += [{'name': name, 'price': price} for name, price in (
            ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
            ('WallUpgradeVoucher1', 20), ('StationUpgradeVoucher1', 100),
            ('WallFixer', 15), ('Medicine', 10))]
        return data

    def test_first_day_vouchers_are_not_planned_or_reserved(self):
        turn, cfg, nav, ledger = fortified_case(self.data())
        self.assertIsNone(supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                                   item_only='WeaponUpgradeVoucher1'))
        _, reserve = plan_day_spending(turn, cfg, Memory(), nav, ledger, TOWERS)
        self.assertEqual(reserve, 0)
        self.assertFalse(ledger.spending_plan['purchases'])

    def test_first_day_owned_weapon_voucher_waits_until_day_two(self):
        data = self.data()
        data['teamOur']['roles'][1].update(pos={'x': 4, 'y': 9},
                                          backpack=['WeaponUpgradeVoucher1'])
        for round_no, expected in ((10, False), (140, True)):
            with self.subTest(round_no=round_no):
                data['roundNo'] = round_no
                turn, _, nav, ledger = fortified_case(data)
                used = use_inventory(turn, nav, ledger, turn.workers[0], mem=Memory(),
                                     target_only=(3, 9), name_only='WeaponUpgradeVoucher1')
                self.assertEqual(used, expected)
                if expected:
                    self.assertEqual(ledger.commands['1'], command('use', (3, 9),
                                                                  name='WeaponUpgradeVoucher1'))

    def test_opening_boss_waits_for_three_built_guns(self):
        data = self.data()
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 22]
        turn, cfg, nav, ledger = fortified_case(data)
        _, reserve = plan_day_spending(turn, cfg, Memory(), nav, ledger, TOWERS)
        self.assertEqual(reserve, cfg.weapon_cost)
        self.assertFalse(summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS,
                                          reserve=reserve))
        self.assertEqual(ledger.spending_plan['robot_blocked'], 'first_day_tower_construction')
        self.assertFalse(ledger.commands)

    def test_first_day_668_gold_buys_five_boss_orders_with_level_one_defenses(self):
        turn, cfg, nav, ledger = fortified_case(self.data())
        self.assertTrue(summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS))
        self.assertEqual(ledger.commands['1'], command('buy', name='BossRobotSummonOrder', num=5))
        self.assertEqual(ledger.gold, 68)

    def test_first_day_saves_sub_boss_remainder_instead_of_buying_smaller_orders(self):
        turn, cfg, nav, ledger = fortified_case(self.data(gold=119))
        self.assertFalse(summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS))
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.gold, 119)

    def test_day_two_weapon_purchase_is_enabled(self):
        turn, cfg, nav, ledger = fortified_case(self.data(round_no=140))
        plan = supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                        item_only='WeaponUpgradeVoucher1', bulk=True)
        self.assertIsNotNone(plan)
        self.assertEqual((plan[0], plan[2]), ('WeaponUpgradeVoucher1', 3))

    def test_incomplete_walls_rebuild_and_missing_night_stock_do_not_gate_purchase(self):
        data = self.data(round_no=270)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 32]
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory(wall_watch_id=1, wall_rebuild_levels={(7, 11): 3})
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS))
        self.assertEqual(ledger.commands['1'], command('buy', name='BossRobotSummonOrder', num=5))

    def test_purchase_does_not_need_home_or_a_summon_position(self):
        data = self.data(round_no=270)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                    if r['roleType'] in ('worker', 'pioneer')]
        data['teamEnemy']['roles'] = []
        turn, cfg, nav, ledger = fortified_case(data)
        with patch('agent.brain.choose_summon_position', side_effect=AssertionError('buy needs no spawn')):
            self.assertTrue(summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS))
        self.assertEqual(ledger.commands['1']['num'], 5)

    def test_first_day_agent_preempts_old_work_and_recall_for_boss_purchase(self):
        for round_no in (10, 69):
            with self.subTest(round_no=round_no):
                data = self.data(round_no=round_no)
                data['teamOur']['roles'].append(unit(3, 'worker', 5, 8, health=500))
                _, cfg, _, _ = fortified_case(data)
                cfg.llm_enabled = False
                agent = Agent(cfg)
                warmup = copy.deepcopy(data)
                warmup['roundNo'] -= 1
                warmup['teamOur']['goldNum'] = 0
                agent.decide(warmup)
                mem = next(iter(agent.sessions.values()))
                mem.daytime_jobs = {1: dict(kind='buy', target=(4, 9),
                    name='WeaponUpgradeVoucher1', quantity=3)}
                response = agent.decide(data)
                buys = [c for c in response['roleCommandMap'].values() if c['action'] == 'buy']
                self.assertIn(command('buy', name='BossRobotSummonOrder', num=5), buys)
                self.assertTrue(all(c['name'] == 'BossRobotSummonOrder' for c in buys))

    def test_first_day_robot_shopping_continues_without_defense_reservations(self):
        data = self.data()
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 0}
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory(daytime_jobs={1: dict(kind='robot_buy', target=(4, 9),
                                         name='BossRobotSummonOrder', quantity=5)})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, WALLS, set())
        self.assertEqual(ledger.work_jobs[1]['quantity'], 5)
        self.assertEqual(ledger.gold, 68)

    def test_recovery_cannot_use_first_day_weapon_voucher(self):
        data = self.data()
        data['teamOur']['roles'][1].update(pos={'x': 4, 'y': 9},
                                          backpack=['WeaponUpgradeVoucher1'])
        for round_no, allowed in ((10, False), (140, True)):
            with self.subTest(round_no=round_no):
                data['roundNo'] = round_no
                turn, cfg, nav, ledger = fortified_case(data)
                action = Recovery().action((1, 'use', (3, 9)), turn, cfg, Memory(), nav, ledger)
                self.assertEqual(action is not None, allowed)

    def test_first_day_treasure_shopping_does_not_spend_boss_remainder(self):
        data = self.data()
        data['teamOur']['roles'][2]['pos'] = {'x': 5, 'y': 9}
        data['weaponShopList'].append({'name': 'AcientTablet', 'price': 10})
        _, cfg, _, _ = fortified_case(data)
        cfg.llm_enabled = False
        agent = Agent(cfg)
        warmup = copy.deepcopy(data)
        warmup['roundNo'] -= 1
        warmup['teamOur']['goldNum'] = 0
        agent.decide(warmup)
        next(iter(agent.sessions.values())).treasure = dict(
            items=['AcientTablet'], position=[6, 9], startRound=1, endRound=100)
        response = agent.decide(data)
        buys = [c for c in response['roleCommandMap'].values() if c['action'] == 'buy']
        self.assertEqual(buys, [command('buy', name='BossRobotSummonOrder', num=5)])

    def test_day_two_agent_buys_weapon_upgrades_and_spends_surplus_on_boss(self):
        data = self.data(round_no=140)
        data['teamOur']['roles'].append(unit(3, 'worker', 5, 10, health=500))
        _, cfg, _, _ = fortified_case(data)
        cfg.llm_enabled = False
        response = Agent(cfg).decide(data)
        buys = [c for c in response['roleCommandMap'].values() if c['action'] == 'buy']
        self.assertIn(command('buy', name='WeaponUpgradeVoucher1', num=3), buys)
        self.assertIn(command('buy', name='BossRobotSummonOrder', num=3), buys)
