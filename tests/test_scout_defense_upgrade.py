"""Finite snapshots of the recalled scout's shop and weapon delivery trip."""
import copy
import unittest

from test_agent import setup_case
import test_first_day_offense as offense
from test_first_day_gatling_abort import gatling
from test_first_day_wall_block import ENTRY, memory_for
from agent.brain import Agent
from agent.commands import command
from agent.economy import supplies, use_inventory
from agent.intelligence import Memory
from agent.model import SUMMON_ORDERS, distance, pos
from agent.opening_offense import act_first_day_offense
from agent.worker_jobs import resume_daytime_jobs


V1, V2 = 'WeaponUpgradeVoucher1', 'WeaponUpgradeVoucher2'
BASE = 'StationUpgradeVoucher1'


def case(tick=35, stock=(), gold=500, worker_pos=ENTRY):
    data = offense.case(tick, stock=stock, gold=gold)
    data['teamOur']['roles'][1]['pos'] = dict(zip(('x', 'y'), worker_pos))
    data['weaponShopList'] += [dict(name=name, price=price) for name, price in
                              ((V1, 100), (V2, 150), (BASE, 100))]
    gatling(data)
    return data


def setup(data):
    return setup_case(data, llm_enabled=False, layout_mode='explicit',
                      weapon_cells=offense.TOWERS, wall_cells=[])


def memory(worker_id=10):
    result = Memory(night_scout=memory_for(),
                    first_day_offense=dict(worker_id=worker_id, phase='block'))
    result.night_scout.worker_id = worker_id
    return result


def step(data, mem):
    turn, cfg, nav, ledger = setup(data)
    worker = act_first_day_offense(turn, cfg, mem, nav, ledger,
                                   sorted(ledger.tower_cells), [])
    return worker, ledger


class ScoutDefenseUpgradeTests(unittest.TestCase):
    def assert_no_robots(self, commands):
        self.assertFalse(any(c.get('name') in SUMMON_ORDERS
                             and c['action'] in ('buy', 'use') for c in commands.values()))

    def test_agent_recalls_scout_instead_of_mining_and_keeps_trip_after_losing_sight(self):
        agent = Agent(offense.cfg(walls=[]))
        data = case(stock=[offense.BOSS])
        data['teamEnemy']['roles'].pop()
        agent.decide(copy.deepcopy(data))
        mem = next(iter(agent.sessions.values()))
        mem.night_scout = memory_for()
        mem.first_day_offense.update(worker_id=10, phase='block')
        mem.daytime_jobs[10] = dict(kind='mine', target=(30, 13), ore='iron')
        data['mapInfo']['zones'].append(
            dict(neutralType='iron', pos=dict(x=30, y=13), remain=10))
        gatling(data)
        data['roundNo'] += 1
        response = agent.decide(copy.deepcopy(data))
        action = response['roleCommandMap']['10']
        self.assertEqual(action['action'], 'move')
        self.assertLess(distance(pos(action['targetPos'][0]), (15, 20)), distance(ENTRY, (15, 20)))
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_buy')
        self.assertIsNone(mem.night_scout.worker_id)
        self.assertTrue(mem.night_scout.cancelled)
        self.assert_no_robots(response['roleCommandMap'])
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)

        data['roundNo'] += 1
        data['teamEnemy']['roles'].pop()
        data['teamOur']['roles'][1]['pos'] = action['targetPos'][0]
        data['lastRoundRoleActionResults'] = {'10': True}
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10']['action'], 'move')
        self.assertEqual(mem.first_day_offense['worker_id'], 10)
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_buy')
        self.assert_no_robots(response)

    def test_original_scout_buys_even_when_other_worker_is_nearer(self):
        data, mem = case(worker_pos=(14, 19)), memory(12)
        data['teamOur']['roles'][2]['pos'] = dict(x=18, y=20)
        worker, ledger = step(data, mem)
        self.assertEqual(worker.id, 12)
        self.assertEqual(ledger.commands['12']['action'], 'move')
        self.assertNotIn('10', ledger.commands)

    def test_shop_buys_batch_then_waits_for_inventory_and_delivers_matching_voucher(self):
        data, mem = case(worker_pos=(14, 19)), memory()
        worker, ledger = step(data, mem)
        self.assertEqual(worker.id, 10)
        self.assertEqual(ledger.commands['10'], command('buy', name=V1, num=2))
        self.assertEqual(ledger.gold, 0)  # The second tier is reserved as well.
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_buy')

        data['roundNo'] += 1
        data['teamEnemy']['roles'].pop()
        data['teamOur']['roles'][1]['backpack'] = [V1] * 2
        data['teamOur']['goldNum'] -= 200
        worker, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('buy', name=V2, num=2))
        self.assertEqual(ledger.gold, 0)
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_buy')

        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'] += [V2] * 2
        data['teamOur']['goldNum'] = 0
        worker, ledger = step(data, mem)
        self.assertEqual(worker.id, 10)
        self.assertEqual(ledger.commands['10']['action'], 'move')
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_use')
        self.assertNotIn(V1, ledger.purchases)

        data['roundNo'] += 1
        data['teamOur']['roles'][1]['pos'] = dict(x=7, y=23)
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V1))
        self.assertIn(20, ledger.upgrade_claims)

    def test_failed_use_retries_and_confirmed_delivery_releases_worker_without_new_purchase(self):
        data, mem = case(stock=[V1], worker_pos=(7, 23)), memory()
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V1))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'10': False}
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V1))
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_use')

        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'10': True}
        data['teamOur']['roles'][1]['backpack'] = []
        data['teamOur']['roles'][4]['level'] = 2
        worker, ledger = step(data, mem)
        self.assertIsNone(worker)
        self.assertEqual(mem.first_day_offense['phase'], 'mining')
        self.assertFalse(ledger.purchases)

    def test_agent_preserves_purchase_retry_cooldown_then_uses_observed_stock(self):
        agent = Agent(offense.cfg(walls=[]))
        data = case(worker_pos=(14, 19))
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=V1, num=2))
        mem = next(iter(agent.sessions.values()))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'10': False}
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertIn((10, V1), mem.buy_failures)
        self.assertNotEqual(response.get('10', {}).get('action'), 'buy')
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_buy')

        data['roundNo'] += 5
        data['lastRoundRoleActionResults'] = {}
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=V1, num=2))
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'] = [V1] * 2
        data['teamOur']['goldNum'] = 300
        data['lastRoundRoleActionResults'] = {'10': True}
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=V2, num=2))
        data['roundNo'] += 1
        data['teamOur']['roles'][1].update(backpack=[V1, V1, V2, V2], pos=dict(x=7, y=23))
        data['teamOur']['goldNum'] = 0
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('use', (8, 23), name=V1))
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_use')
        self.assert_no_robots(response)

    def test_purchase_counts_other_carriers_without_buying_their_prerequisite_again(self):
        data = case(worker_pos=(14, 19), gold=225)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 22]
        data['teamOur']['roles'][2]['backpack'] = [V1]
        _, ledger = step(data, memory())
        self.assertEqual(ledger.commands['10'], command('buy', name=V1, num=1))
        self.assertEqual(ledger.gold, 125)

    def test_existing_paid_voucher_is_used_without_a_shop_or_enemy_base(self):
        data, mem = case(stock=[V1], worker_pos=(7, 23), gold=0), memory()
        data['teamEnemy']['roles'] = data['teamEnemy']['roles'][1:]
        data['mapInfo']['zones'] = []
        worker, ledger = step(data, mem)
        self.assertEqual(worker.id, 10)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V1))

    def test_only_scout_gets_day_one_upgrade_exception(self):
        data, mem = case(stock=[V1], worker_pos=(7, 23)), memory()
        data['teamOur']['roles'][2].update(backpack=[V1], pos=dict(x=7, y=21))
        step(data, mem)
        turn, cfg, nav, ledger = setup(data)
        self.assertFalse(use_inventory(turn, nav, ledger, turn.workers[1], mem=mem))
        self.assertIsNone(supplies(turn, cfg, mem, nav, ledger, turn.workers[1], item_only=V1))
        mem.night_scout.cancelled = False
        self.assertFalse(use_inventory(turn, nav, ledger, turn.workers[0], mem=mem))

    def test_level_two_tower_uses_level_two_voucher_and_maxed_towers_are_skipped(self):
        data, mem = case(stock=[V1, V2], worker_pos=(7, 23)), memory()
        for role in data['teamOur']['roles'][4:]:
            role['level'] = 3
        data['teamOur']['roles'][4]['level'] = 2
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V2))
        data['teamOur']['roles'][4]['level'] = 3
        _, ledger = step(data, mem)
        self.assertFalse(any(c.get('name') in (V1, V2) for c in ledger.commands.values()))

    def test_capacity_and_live_shop_price_limit_batch(self):
        data = case(worker_pos=(14, 19), stock=['iron'] * 99, gold=500)
        next(item for item in data['weaponShopList'] if item['name'] == V1)['price'] = 125
        _, ledger = step(data, memory())
        self.assertEqual(ledger.commands['10'], command('buy', name=V1, num=1))
        self.assertEqual(ledger.gold, 375)

    def test_last_daylight_turn_uses_local_paid_voucher_but_does_not_start_new_trip(self):
        data, mem = case(69, stock=[V1], worker_pos=(7, 23)), memory()
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V1))
        data = case(69, worker_pos=(14, 19))
        worker, ledger = step(data, memory())
        self.assertIsNone(worker)
        self.assertFalse(ledger.commands)

    def test_night_or_missing_scout_does_not_assign_the_other_worker(self):
        for dead, tick in ((False, 70), (True, 35)):
            with self.subTest(dead=dead, tick=tick):
                data, mem = case(tick, worker_pos=(14, 19)), memory()
                if dead:
                    data['teamOur']['roles'][1]['health'] = 0
                worker, ledger = step(data, mem)
                self.assertIsNone(worker)
                self.assertFalse(ledger.commands)

    def test_early_sighting_selects_opening_worker_before_tick_25(self):
        data, mem = case(10, worker_pos=(14, 19)), Memory()
        worker, ledger = step(data, mem)
        self.assertEqual(worker.id, 10)
        self.assertEqual(ledger.commands['10'], command('buy', name=V1, num=2))
        self.assertEqual(mem.first_day_offense['worker_id'], 10)

    def test_scout_spends_all_250_gold_despite_base_and_missing_gun(self):
        data = case(worker_pos=(14, 19), gold=250)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 21]
        data['teamOur']['roles'][-1]['roleType'] = 'gatling'
        data['teamOur']['roles'][3]['pos'] = dict(x=14, y=21)  # Pioneer can buy here too.
        agent = Agent(offense.cfg(walls=[]))
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=V1, num=1))
        self.assertFalse(any(c['action'] == 'buy' and c.get('name') == BASE
                             or c['action'] == 'build' for c in response.values()))
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'] = [V1]
        data['teamOur']['goldNum'] = 150
        data['lastRoundRoleActionResults'] = {'10': True}
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=V2, num=1))
        self.assert_no_robots(response)

    def test_dead_scout_does_not_reserve_gold_away_from_pioneer(self):
        data = case(worker_pos=(14, 19), gold=250)
        data['teamOur']['roles'][3]['pos'] = dict(x=14, y=21)
        agent = Agent(offense.cfg(walls=[]))
        agent.decide(copy.deepcopy(data))
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['health'] = 0
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['11'], command('buy', name=BASE, num=1))
        self.assertNotIn('10', response)

    def test_surplus_gold_stops_at_real_level_three_demand_and_free_items_are_bounded(self):
        for price, gold in ((100, 1000), (0, 0)):
            with self.subTest(price=price):
                data, mem = case(worker_pos=(14, 19), gold=gold), memory()
                for item in data['weaponShopList']:
                    if item['name'] in (V1, V2) and not price:
                        item['price'] = 0
                _, ledger = step(data, mem)
                self.assertEqual(ledger.commands['10'], command('buy', name=V1, num=3))
                data['roundNo'] += 1
                data['teamOur']['roles'][1]['backpack'] = [V1] * 3
                data['teamOur']['goldNum'] -= price * 3
                _, ledger = step(data, mem)
                self.assertEqual(ledger.commands['10'], command('buy', name=V2, num=3))
                data['roundNo'] += 1
                data['teamOur']['roles'][1]['backpack'] += [V2] * 3
                data['teamOur']['goldNum'] -= 450 if price else 0
                _, ledger = step(data, mem)
                self.assertEqual(ledger.commands['10']['action'], 'move')
                self.assertEqual(mem.first_day_offense['phase'], 'upgrade_use')
                self.assertEqual(ledger.gold, 250 if price else 0)

    def test_unaffordable_second_tier_keeps_remainder_for_gatling(self):
        data = case(worker_pos=(14, 19), gold=220)
        data['teamOur']['roles'][-1]['roleType'] = 'gatling'
        _, ledger = step(data, memory())
        self.assertEqual(ledger.commands['10'], command('buy', name=V1, num=1))
        self.assertEqual(ledger.gold, 120)

    def test_new_income_during_delivery_does_not_send_scout_back_to_shop(self):
        data, mem = case(stock=[V1, V2], gold=1000, worker_pos=(7, 23)), memory()
        mem.night_scout.cancelled = True
        mem.first_day_offense.update(phase='upgrade_use', upgrade_shop=(15, 20))
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V1))
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_use')

    def test_gatling_reaches_level_three_before_lower_id_rocket(self):
        data, mem = case(stock=[V1, V1, V2], gold=0, worker_pos=(7, 21)), memory()
        data['teamOur']['roles'][-1]['roleType'] = 'gatling'
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 21), name=V1))
        data['roundNo'] += 1
        data['teamOur']['roles'][-1]['level'] = 2
        data['teamOur']['roles'][1]['backpack'] = [V1, V2]
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 21), name=V2))
        data['roundNo'] += 1
        data['teamOur']['roles'][-1]['level'] = 3
        data['teamOur']['roles'][1]['backpack'] = [V1]
        data['teamOur']['roles'][1]['pos'] = dict(x=7, y=23)
        _, ledger = step(data, mem)
        self.assertEqual(ledger.commands['10'], command('use', (8, 23), name=V1))

    def test_gatling_priority_beats_old_rocket_target_and_dusk_proximity(self):
        for tick in (35, 65):
            for day in (1, 2):
                with self.subTest(tick=tick, day=day):
                    data = case(tick + 130 * (day - 1), stock=[V1, V2], worker_pos=(7, 24))
                    data['teamOur']['roles'][-1].update(roleType='gatling', level=2)
                    mem = memory()
                    mem.night_scout.cancelled = True
                    mem.first_day_offense['phase'] = 'upgrade_use'
                    mem.upgrade_targets[10] = (8, 23)
                    turn, _, nav, ledger = setup(data)
                    self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[0], mem=mem))
                    self.assertEqual(mem.upgrade_targets[10], (8, 21))
                    self.assertIn(22, ledger.upgrade_claims)

    def test_normal_procurement_prefers_gatling_second_tier_over_rocket_first_tier(self):
        data = case(165, worker_pos=(14, 19), gold=150)
        data['teamOur']['roles'][-1].update(roleType='gatling', level=2)
        turn, cfg, nav, ledger = setup(data)
        plan = supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)
        self.assertEqual((plan[0], plan[2]), (V2, 1))

    def test_saved_rocket_delivery_waits_for_gatling_even_without_its_voucher(self):
        for has_second_tier in (False, True):
            with self.subTest(has_second_tier=has_second_tier):
                data = case(165, stock=[V1] + ([V2] if has_second_tier else []),
                            worker_pos=(7, 23), gold=0)
                data['teamOur']['roles'][-1].update(roleType='gatling', level=2)
                mem = Memory(daytime_jobs={10: dict(kind='use', target=(8, 23), name=V1)},
                             upgrade_targets={10: (8, 23)})
                turn, cfg, nav, ledger = setup(data)
                resume_daytime_jobs(turn, cfg, mem, nav, ledger, offense.TOWERS, [], set())
                if has_second_tier:
                    self.assertEqual(mem.upgrade_targets[10], (8, 21))
                    self.assertIn(22, ledger.upgrade_claims)
                else:
                    self.assertNotIn('10', ledger.commands)

    def test_missing_second_tier_and_full_backpack_still_leave_for_delivery(self):
        for reason in ('item', 'capacity', 'deadline'):
            with self.subTest(reason=reason):
                data, mem = case(stock=[V1], worker_pos=(14, 19)), memory()
                mem.night_scout.cancelled = True
                mem.first_day_offense.update(phase='upgrade_buy', upgrade_shop=(15, 20))
                data['teamOur']['roles'] = data['teamOur']['roles'][:5]
                if reason == 'item':
                    data['weaponShopList'] = [i for i in data['weaponShopList'] if i['name'] != V2]
                elif reason == 'capacity':
                    data['teamOur']['roles'][1]['backPackCapability'] = 1
                else:
                    data['roundNo'] = 62
                _, ledger = step(data, mem)
                self.assertEqual(ledger.commands['10']['action'], 'move')
                self.assertEqual(mem.first_day_offense['phase'], 'upgrade_use')

    def test_no_gold_missing_item_or_unreachable_shop_never_buys(self):
        for reason in ('gold', 'item', 'route'):
            with self.subTest(reason=reason):
                data = case(worker_pos=(14, 19))
                if reason == 'gold':
                    data['teamOur']['goldNum'] = 99
                elif reason == 'item':
                    data['weaponShopList'] = [i for i in data['weaponShopList'] if i['name'] != V1]
                else:
                    data['mapInfo']['zones'] = []
                _, ledger = step(data, memory())
                self.assertFalse(any(c['action'] == 'buy' for c in ledger.commands.values()))
                self.assert_no_robots(ledger.commands)
