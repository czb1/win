"""Paid defense work keeps its action while spare actors continue robot work."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.economy import workers
from agent.intelligence import Memory
from agent.spending import plan_day_spending
from agent.worker_jobs import resume_daytime_jobs, save_daytime_jobs


TOWERS = [(5, 9)]
SHOP = (6, 10)
BOSS = 'BossRobotSummonOrder'
VOUCHER = 'WeaponUpgradeVoucher1'


def case(backpack=(), gold=360, round_no=140, second=None):
    roles = [unit(13, 'station', 4, 7, level=3, health=4500),
             unit(1, 'worker', 6, 9, health=500, backpack=list(backpack)),
             unit(20, 'rocket', 5, 9, level=1, health=1000)]
    if second is not None:
        roles.append(unit(2, 'worker', 7, 9, health=500, backpack=list(second)))
    data = payload(round_no, roles)
    data['mapInfo'].update(width=20, height=16,
        zones=[{'neutralType': 'weaponShop', 'pos': {'x': SHOP[0], 'y': SHOP[1]}}])
    data['teamOur']['goldNum'] = gold
    data['teamEnemy']['roles'] = [unit(99, 'station', 15, 4, health=1500)]
    data['weaponShopList'] = [{'name': VOUCHER, 'price': 100},
                              {'name': BOSS, 'price': 120}]
    return data


def setup(data, **overrides):
    options = dict(robot_purchase_last_day=10, layout_mode='explicit', loadout=['rocket'],
                   weapon_cells=[[5, 9]], wall_cells=[], llm_enabled=False)
    options.update(overrides)
    return setup_case(data, **options)


class DefenseActionSpendingTests(unittest.TestCase):
    def agent(self, data):
        return Agent(setup(data)[1])

    def test_paid_voucher_precedes_new_boss_purchase(self):
        data = case([VOUCHER])
        result = self.agent(data).decide(data)
        self.assertEqual(result['roleCommandMap']['1'], command('use', (5, 9), name=VOUCHER))

    def test_paid_voucher_precedes_boss_and_robot_work_resumes(self):
        for stock, gold, action in (([BOSS] * 3, 0, 'use'), ([], 360, 'buy')):
            with self.subTest(stock=stock):
                data = case([VOUCHER] + stock, gold=gold)
                agent = self.agent(data)
                self.assertEqual(agent.decide(data)['roleCommandMap']['1'],
                                 command('use', (5, 9), name=VOUCHER))
                following = copy.deepcopy(data)
                following['roundNo'] += 1
                following['teamOur']['roles'][1]['backpack'] = stock
                following['teamOur']['roles'][2].update(level=2, health=1500)
                result = agent.decide(following)['roleCommandMap']['1']
                self.assertEqual((result['action'], result['name']), (action, BOSS))
                if action == 'buy':
                    self.assertEqual(result['num'], 3)

    def test_spare_worker_buys_boss_while_paid_upgrade_is_delivered(self):
        data = case([VOUCHER], second=[])
        result = self.agent(data).decide(data)['roleCommandMap']
        self.assertEqual(result['1'], command('use', (5, 9), name=VOUCHER))
        self.assertEqual(result['2'], command('buy', name=BOSS, num=3))

    def test_spare_carrier_summons_while_another_carrier_upgrades(self):
        data = case([VOUCHER, BOSS], gold=0, second=[BOSS])
        result = self.agent(data).decide(data)['roleCommandMap']
        self.assertEqual(result['1'], command('use', (5, 9), name=VOUCHER))
        self.assertEqual((result['2']['action'], result['2']['name']), ('use', BOSS))

    def test_one_defense_buyer_leaves_a_spare_boss_buyer(self):
        for stock in ([], [BOSS] * 3):
            with self.subTest(stock=stock):
                data = case(stock, second=[])
                result = self.agent(data).decide(data)['roleCommandMap']
                self.assertEqual(result['1'], command('buy', name=VOUCHER, num=1))
                self.assertEqual(result['2'], command('buy', name=BOSS, num=2))

    def test_selected_defense_buyer_purchases_even_with_income_available(self):
        data = case(second=[])
        data['mapInfo']['zones'] += [
            {'neutralType': 'copper', 'pos': {'x': 2, 'y': 8}, 'remain': 10},
            {'neutralType': 'vendor', 'pos': {'x': 3, 'y': 8}}]
        data['vendorShopList'] = [{'name': 'copper', 'price': 5}]
        result = self.agent(data).decide(data)['roleCommandMap']
        self.assertEqual(result['1'], command('buy', name=VOUCHER, num=1))
        self.assertEqual(result['2'], command('buy', name=BOSS, num=2))

    def test_old_robot_purchase_yields_to_paid_delivery(self):
        turn, cfg, nav, ledger = setup(case([VOUCHER]))
        mem = Memory(daytime_jobs={1: dict(kind='robot_buy', target=SHOP,
                                          name=BOSS, quantity=3)})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, [], set())
        self.assertEqual(ledger.commands['1'], command('use', (5, 9), name=VOUCHER))
        self.assertNotIn(1, mem.daytime_jobs)
        self.assertEqual(ledger.gold, 360)
        self.assertFalse(ledger.purchases)

    def test_old_robot_purchase_releases_selected_defense_buyer(self):
        turn, cfg, nav, ledger = setup(case())
        mem = Memory(daytime_jobs={1: dict(kind='robot_buy', target=SHOP,
                                          name=BOSS, quantity=3)})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, [], set())
        self.assertNotIn(1, mem.daytime_jobs)
        self.assertEqual(ledger.commands['1'], command('buy', name=VOUCHER, num=1))
        self.assertEqual(ledger.gold, 260)

    def test_old_robot_purchase_does_not_borrow_a_returning_defense_buyer(self):
        turn, cfg, nav, ledger = setup(case(second=[]))
        mem = Memory(supply_worker=1, daytime_jobs={2: dict(kind='robot_buy', target=SHOP,
                                                         name=BOSS, quantity=3)})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, [], {1})
        self.assertNotIn('1', ledger.commands)
        self.assertEqual(ledger.commands['2'], command('buy', name=VOUCHER, num=1))
        self.assertEqual(ledger.gold, 260)

    def test_full_backpack_paid_delivery_has_no_second_purchase_reservation(self):
        data = case([VOUCHER] + [BOSS] * 3)
        data['teamOur']['roles'][1]['backPackCapability'] = 4
        turn, cfg, nav, ledger = setup(data)
        mem = Memory()
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, [], set())
        self.assertEqual(ledger.commands['1']['name'], VOUCHER)
        _, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, TOWERS)
        self.assertEqual(reserve, 0)
        self.assertEqual(ledger.gold, 360)

    def test_paid_delivery_move_survives_as_a_delivery_job(self):
        data = case([VOUCHER, BOSS], gold=0, second=[])
        data['teamOur']['roles'][1]['pos'] = {'x': 1, 'y': 9}
        turn, cfg, nav, ledger = setup(data)
        mem = Memory(supply_worker=1)
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, [], set())
        self.assertEqual(ledger.commands['1']['action'], 'move')
        self.assertEqual(ledger.work_jobs[1]['kind'], 'use')
        self.assertEqual(ledger.work_jobs[1]['name'], VOUCHER)
        workers(turn, cfg, mem, nav, ledger, TOWERS, [])
        self.assertEqual(mem.supply_worker, 1)
        save_daytime_jobs(turn, mem, ledger)
        self.assertEqual(mem.daytime_jobs[1]['target'], (5, 9))

    def test_wrong_tier_or_maxed_target_does_not_lock_boss_carrier(self):
        for voucher, level in (('WeaponUpgradeVoucher2', 1), (VOUCHER, 3)):
            with self.subTest(voucher=voucher, level=level):
                data = case([voucher, BOSS], gold=0)
                data['teamOur']['roles'][2]['level'] = level
                result = self.agent(data).decide(data)['roleCommandMap']['1']
                self.assertEqual((result['action'], result['name']), ('use', BOSS))

    def test_unreachable_paid_target_does_not_lock_boss_carrier(self):
        data = case([VOUCHER, BOSS], gold=0)
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 0}
        data['teamOur']['roles'] += [unit(100 + (dx + 1) * 3 + dy + 1, 'wall',
            5 + dx, 9 + dy, level=3, health=2000)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
        result = self.agent(data).decide(data)['roleCommandMap']['1']
        self.assertEqual((result['action'], result['name']), ('use', BOSS))

    def test_delivery_past_return_deadline_does_not_lock_boss_carrier(self):
        turn, cfg, nav, ledger = setup(case([VOUCHER, BOSS], gold=0, round_no=196))
        ledger.operator_posts[1] = (0, 0)
        self.assertTrue(summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, []))
        self.assertEqual((ledger.commands['1']['action'], ledger.commands['1']['name']),
                         ('use', BOSS))

    def test_first_day_keeps_boss_purchase_and_carried_order_priority(self):
        for stock, gold, action in (([VOUCHER], 360, 'buy'),
                                    ([VOUCHER, BOSS], 0, 'use')):
            with self.subTest(stock=stock):
                data = case(stock, gold=gold, round_no=10)
                result = self.agent(data).decide(data)['roleCommandMap']['1']
                self.assertEqual((result['action'], result['name']), (action, BOSS))
                if action == 'buy':
                    self.assertEqual(result['num'], 3)

    def test_single_missing_weapon_protects_only_one_builder(self):
        data = case(second=[])
        data['teamOur']['roles'][2]['level'] = 3
        data['weaponShopList'] = [{'name': BOSS, 'price': 120}]
        towers = [(5, 9), (6, 8)]
        turn, cfg, nav, ledger = setup(data, loadout=['rocket', 'rocket'],
                                     weapon_cells=[list(p) for p in towers])
        mem = Memory()
        actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, towers)
        self.assertEqual(len(actors), 1)
        self.assertEqual(reserve, cfg.weapon_cost)
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, towers, [],
                                         excluded=actors, reserve=reserve))
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=2))

    def test_unfunded_construction_does_not_lock_paid_boss_carrier(self):
        data = case([BOSS], gold=0)
        data['teamOur']['roles'][2]['level'] = 3
        towers = [(5, 9), (6, 8)]
        turn, cfg, nav, ledger = setup(data, loadout=['rocket', 'rocket'],
                                     weapon_cells=[list(p) for p in towers])
        mem = Memory()
        actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, towers)
        self.assertFalse(actors)
        self.assertEqual(reserve, 0)
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, towers, [],
                                         excluded=actors, reserve=reserve))
        self.assertEqual(ledger.commands['1']['name'], BOSS)

    def test_partial_construction_budget_keeps_an_extra_boss_carrier_free(self):
        data = case([BOSS], gold=25, second=[BOSS])
        data['teamOur']['roles'][2]['level'] = 3
        towers = [(5, 9), (6, 8), (7, 8)]
        turn, cfg, nav, ledger = setup(data, loadout=['rocket'] * 3,
                                     weapon_cells=[list(p) for p in towers])
        mem = Memory()
        actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, towers)
        self.assertEqual(actors, {1})
        self.assertEqual(reserve, 25)
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, towers, [],
                                         excluded=actors, reserve=reserve))
        self.assertEqual(ledger.commands['2']['name'], BOSS)
