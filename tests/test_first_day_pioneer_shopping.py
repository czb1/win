"""Post-task purchasing, delivery and early recall from observed turn states."""
import copy
import unittest
from unittest.mock import patch

import test_opening_boss_spending as opening_fixtures
from test_robot_assault import fortified_case, TOWERS, WALLS
from test_agent import payload, setup_case, unit
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.intelligence import Memory, Intelligence
from agent.model import pos
from agent.task_schedule import task_options
from agent.spending import (first_day_tasks_finished, first_day_purchase_reserve,
                            prepare_first_day_pioneer, prepare_station_reserve)


BASE = 'StationUpgradeVoucher1'
BOSS = 'BossRobotSummonOrder'


class FirstDayPioneerShoppingTests(unittest.TestCase):
    def data(self, round_no=10, gold=668):
        data = opening_fixtures.OpeningBossSpendingTests().data(round_no=round_no, gold=gold)
        data['teamOur']['roles'][0]['health'] = 1500
        data['teamOur']['roles'][2].update(pos={'x': 5, 'y': 8}, health=500)
        data['teamOur']['playerTasks'] = [dict(taskType=f'自进化类{i}',
            taskPosition={'x': 8, 'y': 10 + i}, coldDownRounds=0,
            isValid=False, timeoutRounds=15, goldReward=120, scoreReward=120)
            for i in (1, 2)]
        return data

    def prepare(self, data, mem):
        turn, cfg, nav, ledger = fortified_case(data)
        ledger.return_pairs = [(turn.pioneer, turn.weapons[0])]
        ledger.operator_posts = {turn.pioneer.id: (1, 9)}
        accepted = prepare_first_day_pioneer(turn, cfg, mem, nav, ledger)
        return accepted, turn, ledger

    def next_turn(self, data, backpack, gold=None, rejected=False):
        data['roundNo'] += 1
        data['teamOur']['roles'][2]['backpack'] = list(backpack)
        data['lastRoundRoleActionResults'] = {'2': not rejected}
        if gold is not None:
            data['teamOur']['goldNum'] = gold

    def test_ready_cooling_active_and_missing_tasks_are_not_all_finished(self):
        for condition in ('ready', 'cooling', 'active', 'missing'):
            with self.subTest(condition=condition):
                data = self.data()
                if condition == 'ready':
                    data['teamOur']['playerTasks'][1]['isValid'] = True
                elif condition == 'cooling':
                    data['teamOur']['playerTasks'][1]['coldDownRounds'] = 30
                elif condition == 'active':
                    data['phaseTask'] = 'still solving the last task'
                else:
                    data['teamOur']['playerTasks'] = []
                turn, _, _, _ = fortified_case(data)
                self.assertFalse(first_day_tasks_finished(turn))
                accepted, _, ledger = self.prepare(data, Memory())
                self.assertFalse(accepted)
                self.assertFalse(ledger.commands)

    def test_early_voucher_and_worker_robot_buyers_yield_to_tasks(self):
        data = self.data()
        data['teamOur']['playerTasks'][0]['isValid'] = True
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory()
        self.assertFalse(prepare_station_reserve(turn, cfg, mem, nav, ledger))
        self.assertFalse(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS))
        self.assertFalse(ledger.commands)
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 460)

    def test_agent_keeps_available_and_cooling_tasks_ahead_of_shopping(self):
        for cooling in (False, True):
            with self.subTest(cooling=cooling):
                data = self.data()
                data['teamOur']['playerTasks'][0].update(
                    isValid=not cooling, coldDownRounds=5 if cooling else 0)
                _, cfg, _, _ = fortified_case(data)
                agent = Agent(cfg)
                response = agent.decide(data)['roleCommandMap']
                self.assertFalse(any(c['action'] == 'buy' for c in response.values()))
                self.assertFalse(next(iter(agent.sessions.values())).first_day_pioneer)

    def test_tick_51_starts_shopping_without_waiting_for_task_refresh(self):
        for origin in (0, 1):
            for condition in ('ready', 'cooling', 'exhausted', 'missing'):
                with self.subTest(origin=origin, condition=condition):
                    data, mem = self.data(round_no=51 + origin), Memory()
                    if condition == 'ready':
                        data['teamOur']['playerTasks'][0]['isValid'] = True
                    elif condition == 'cooling':
                        data['teamOur']['playerTasks'][0]['coldDownRounds'] = 30
                    elif condition == 'missing':
                        data['teamOur']['playerTasks'] = []
                    _, cfg, _, _ = fortified_case(data)
                    cfg.round_origin = origin
                    turn, cfg, nav, ledger = setup_case(data, **vars(cfg))
                    ledger.return_pairs = [(turn.pioneer, turn.weapons[0])]
                    ledger.operator_posts = {2: (1, 9)}
                    self.assertTrue(prepare_first_day_pioneer(turn, cfg, mem, nav, ledger))
                    self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))

    def test_task_selection_stops_at_51_and_after_shopping_has_started(self):
        for day, tick, started, allowed in ((1, 50, False, True), (1, 51, False, False),
                                           (1, 52, False, False), (1, 50, True, False),
                                           (2, 51, True, True)):
            with self.subTest(day=day, tick=tick, started=started):
                data = self.data(round_no=(day - 1) * 130 + tick)
                data['teamOur']['playerTasks'][0].update(isValid=True, timeoutRounds=1)
                turn, cfg, nav, ledger = fortified_case(data)
                mem = Memory(first_day_pioneer={'phase': 'shop'} if started else {})
                self.assertEqual(bool(task_options(turn, cfg, mem, nav, ledger, turn.pioneer)), allowed)

    def test_shopping_continues_when_a_task_point_refreshes(self):
        data, mem = self.data(round_no=51), Memory()
        self.prepare(data, mem)
        self.next_turn(data, [BASE], gold=568)
        data['teamOur']['playerTasks'][0].update(isValid=True, coldDownRounds=0)
        accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=3))

    def test_active_task_at_51_finishes_before_shopping_even_past_67(self):
        data = self.data(round_no=51)
        data['phaseTask'] = 'active task'
        data['teamOur']['playerTasks'][0].update(coldDownRounds=30, timeoutRounds=40)
        _, cfg, _, _ = fortified_case(data)
        agent = Agent(cfg)
        with patch.object(Intelligence, 'task', return_value=('', '')) as task:
            for tick in (51, 67, 70):
                data['roundNo'] = tick
                response = agent.decide(copy.deepcopy(data))['roleCommandMap']
                mem = next(iter(agent.sessions.values()))
                self.assertNotIn('2', response)
                self.assertFalse(mem.first_day_pioneer)
                self.assertNotIn(2, mem.return_targets)
                self.assertFalse(mem.stop_reason)
                self.assertGreater(task.call_args.kwargs['available_rounds'], 0)
        # A separate daylight completion can still buy while the point cools.
        agent = Agent(cfg)
        data['roundNo'] = 51
        with patch.object(Intelligence, 'task', return_value=('', '')):
            agent.decide(copy.deepcopy(data))
        data.update(roundNo=52, phaseTask='')
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['2'], command('buy', name=BASE, num=1))

    def test_short_daylight_allows_a_return_that_extends_into_night(self):
        data, mem = self.data(round_no=64), Memory()
        turn, cfg, nav, ledger = fortified_case(data)
        ledger.return_pairs = [(turn.pioneer, turn.weapons[0])]
        ledger.operator_posts = {2: (0, 0)}
        self.assertGreater(nav.search(turn.pioneer, {(0, 0)})[0], turn.day_left)
        self.assertTrue(prepare_first_day_pioneer(turn, cfg, mem, nav, ledger))
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))

    def test_return_continues_at_night_and_attacks_after_arrival(self):
        data = self.data(round_no=69)
        _, cfg, _, _ = fortified_case(data)
        cfg.loadout = ['rocket'] * 3
        for role in data['teamOur']['roles']:
            if role['roleType'] == 'gatling':
                role['roleType'] = 'rocket'
        agent = Agent(cfg)
        agent.decide(copy.deepcopy(data))
        mem = next(iter(agent.sessions.values()))
        mem.first_day_pioneer = dict(phase='return', station_item=BASE, pending_buy=None)
        data['roundNo'] = 70
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['2']['action'], 'move')
        data['teamOur']['roles'][2]['pos'] = {'x': mem.gunner_post[0], 'y': mem.gunner_post[1]}
        data['roundNo'] = 71
        data['robot']['roles'] = [unit(90, 'smallRobot', 7, 8, targetTeam='challenger')]
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        attacks = [c for c in response.values() if c['action'] == 'attack']
        self.assertTrue(any(c['controllerId'] == '2' for c in attacks))

    def test_pioneer_buys_both_types_before_using_either(self):
        data, mem = self.data(), Memory()
        accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands, {'2': command('buy', name=BASE, num=1)})
        self.assertEqual(ledger.gold, 568)
        self.next_turn(data, [BASE], gold=568)
        accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands, {'2': command('buy', name=BOSS, num=3)})
        self.assertEqual(mem.robot_summon_state['purchased'], 3)
        self.assertEqual(ledger.gold, 208)

    def test_walking_reserves_each_item_cost_once(self):
        data, mem = self.data(), Memory()
        data['teamOur']['roles'][2]['pos'] = {'x': 0, 'y': 0}
        accepted, turn, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2']['action'], 'move')
        self.assertEqual(ledger.gold, 208)
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 0)
        self.assertEqual(mem.robot_summon_state['purchased'], 0)
        self.assertFalse(mem.robot_summon_state['station_purchased'])

    def test_existing_robot_stock_does_not_skip_the_base_purchase(self):
        data, mem = self.data(), Memory()
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))

    def test_low_gold_waits_for_income_and_uses_only_an_affordable_batch(self):
        data, mem = self.data(gold=99), Memory()
        accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertFalse(ledger.commands)
        self.assertIn(2, ledger.used)
        self.next_turn(data, [], gold=340)
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))
        self.next_turn(data, [BASE], gold=240)
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=2))
        self.assertEqual(ledger.gold, 0)

    def test_free_orders_still_respect_the_three_purchase_limit(self):
        data, mem = self.data(), Memory()
        next(item for item in data['weaponShopList'] if item['name'] == BOSS)['price'] = 0
        self.prepare(data, mem)
        self.next_turn(data, [BASE], gold=568)
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=3))

    def test_healthy_base_is_upgraded_then_each_robot_used_then_early_recall(self):
        data, mem = self.data(), Memory()
        self.prepare(data, mem)
        self.next_turn(data, [BASE], gold=568)
        self.prepare(data, mem)
        self.next_turn(data, [BASE] + [BOSS] * 3, gold=208)
        data['teamOur']['roles'][2]['pos'] = {'x': 5, 'y': 10}
        accepted, turn, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2'], command('use', (4, 10), name=BASE))
        self.assertGreaterEqual(turn.station.health, 500)
        data['teamOur']['roles'][0].update(level=2, health=3000)
        targets = set()
        for count in (3, 2, 1):
            self.next_turn(data, [BOSS] * count)
            accepted, turn, ledger = self.prepare(data, mem)
            self.assertTrue(accepted)
            action = ledger.commands['2']
            self.assertEqual((action['action'], action['name']), ('use', BOSS))
            target = pos(action['targetPos'][0])
            self.assertTrue(turn.summon_position_legal(target))
            self.assertNotIn(target, targets)
            targets.add(target)
        self.next_turn(data, [])
        _, turn, ledger = self.prepare(data, mem)
        self.assertLess(turn.tick, 67)
        self.assertEqual(mem.first_day_pioneer['phase'], 'return')
        self.assertEqual(mem.return_targets[2], turn.weapons[0].id)
        self.assertEqual(mem.return_posts[2], (1, 9))
        self.assertEqual(first_day_purchase_reserve(turn, mem, ledger), 0)

    def test_agent_keeps_the_full_sequence_and_returns_before_tick_67(self):
        data = self.data()
        _, cfg, _, _ = fortified_case(data)
        agent = Agent(cfg)
        first = agent.decide(copy.deepcopy(data))
        self.assertEqual(first['roleCommandMap']['2'], command('buy', name=BASE, num=1))
        self.assertEqual(agent.decide(copy.deepcopy(data)), first)
        self.next_turn(data, [BASE], gold=568)
        self.assertEqual(agent.decide(copy.deepcopy(data))['roleCommandMap']['2'],
                         command('buy', name=BOSS, num=3))
        self.next_turn(data, [BASE] + [BOSS] * 3, gold=208)
        data['teamOur']['roles'][2]['pos'] = {'x': 5, 'y': 10}
        self.assertEqual(agent.decide(copy.deepcopy(data))['roleCommandMap']['2'],
                         command('use', (4, 10), name=BASE))
        data['teamOur']['roles'][0].update(level=2, health=3000)
        for count in (3, 2, 1):
            self.next_turn(data, [BOSS] * count)
            result = agent.decide(copy.deepcopy(data))['roleCommandMap']
            self.assertEqual((result['2']['action'], result['2']['name']), ('use', BOSS))
            self.assertFalse(any(c['action'] == 'buy' for uid, c in result.items() if uid != '2'))
        self.next_turn(data, [])
        result = agent.decide(copy.deepcopy(data))['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.first_day_pioneer['phase'], 'return')
        self.assertIn(2, mem.return_targets)
        self.assertEqual(result['2']['action'], 'move')
        turn, _, nav, _ = fortified_case(data)
        route = nav.search(turn.pioneer, {mem.return_posts[2]})
        self.assertEqual(result['2'], command('move', route[1]))

    def test_explicit_robot_purchase_rejection_reopens_shopping(self):
        data, mem = self.data(), Memory()
        self.prepare(data, mem)
        self.next_turn(data, [BASE], gold=568)
        self.prepare(data, mem)
        self.next_turn(data, [BASE], rejected=True)
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=3))
        self.assertEqual(mem.robot_summon_state['purchased'], 3)

    def test_explicit_base_purchase_rejection_can_retry_without_counting_robots(self):
        data, mem = self.data(), Memory()
        self.prepare(data, mem)
        self.next_turn(data, [], rejected=True)
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))
        self.assertEqual(mem.robot_summon_state['purchased'], 0)

    def test_rejected_summon_releases_its_daily_slot_and_position(self):
        data, mem = self.data(), Memory()
        data['teamOur']['roles'][0].update(level=2, health=3000)
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        mem.first_day_pioneer = dict(phase='robots', station_item=BASE, pending_buy=None)
        _, _, ledger = self.prepare(data, mem)
        first = ledger.commands['2']
        self.next_turn(data, [BOSS], rejected=True)
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], first)
        self.assertEqual(mem.robot_summon_state['count'], 1)
        self.assertEqual(len(mem.robot_summon_state['positions']), 1)

    def test_both_base_corners_and_official_pioneer_ids_buy_and_upgrade(self):
        for team, station, hero_cell, shop, towers, enemy in (
            ('challenger', (4, 27), (6, 26), (7, 25), [(3, 26), (3, 25), (4, 25)], (34, 5)),
            ('defender', (34, 5), (33, 4), (32, 3), [(36, 4), (36, 3), (35, 3)], (4, 27))):
            with self.subTest(team=team):
                uid = 10011 if team == 'challenger' else 20011
                roles = [unit(uid + 2, 'station', *station, health=1500),
                         unit(uid, 'pioneer', *hero_cell, health=500)]
                roles += [unit(uid + 29 + i, 'rocket', *p) for i, p in enumerate(towers)]
                data = payload(10, roles)
                data['mapInfo'].update(width=41, height=32, zones=[
                    dict(neutralType='weaponShop', pos={'x': shop[0], 'y': shop[1]})])
                data['teamOur'].update(type=team, teamId=team, goldNum=668,
                                      playerTasks=self.data()['teamOur']['playerTasks'])
                data['teamEnemy']['roles'] = [unit(99, 'station', *enemy, health=1500)]
                data['weaponShopList'] = [{'name': BASE, 'price': 100}, {'name': BOSS, 'price': 120}]
                _, cfg, _, _ = setup_case(data, layout_mode='explicit',
                    weapon_cells=[list(p) for p in towers], wall_cells=[])
                agent = Agent(cfg)
                self.assertEqual(agent.decide(copy.deepcopy(data))['roleCommandMap'][str(uid)],
                                 command('buy', name=BASE, num=1))
                data['roundNo'] += 1
                roles[1]['backpack'] = [BASE]
                data['lastRoundRoleActionResults'] = {str(uid): True}
                data['teamOur']['goldNum'] = 568
                self.assertEqual(agent.decide(copy.deepcopy(data))['roleCommandMap'][str(uid)],
                                 command('buy', name=BOSS, num=3))
                data['roundNo'] += 1
                roles[1]['backpack'] = [BASE] + [BOSS] * 3
                data['teamOur']['goldNum'] = 208
                action = agent.decide(copy.deepcopy(data))['roleCommandMap'][str(uid)]
                self.assertEqual((action['action'], action['name']), ('use', BASE))
                turn, _, _, _ = setup_case(data)
                self.assertTrue(turn.adjacent(turn.pioneer.pos, pos(action['targetPos'][0])))
                self.assertIn(pos(action['targetPos'][0]), turn.station.cells)

    def test_full_backpack_and_wrong_base_tier_do_not_emit_illegal_buys_or_uses(self):
        data = self.data()
        data['teamOur']['roles'][2].update(backPackCapability=1, backpack=['Medicine'])
        _, _, ledger = self.prepare(data, Memory())
        self.assertFalse(ledger.commands)
        data = self.data()
        data['teamOur']['roles'][0]['level'] = 3
        data['teamOur']['roles'][2]['backpack'] = [BASE]
        _, _, ledger = self.prepare(data, Memory())
        self.assertFalse(any(c['action'] == 'use' and c.get('name') == BASE
                             for c in ledger.commands.values()))

    def test_short_daylight_cannot_start_a_shop_trip(self):
        for round_no in (66, 67, 69):
            with self.subTest(round_no=round_no):
                data = self.data(round_no=round_no)
                _, _, ledger = self.prepare(data, Memory())
                self.assertFalse(any(c['action'] == 'buy' for c in ledger.commands.values()))

    def test_remaining_daylight_reduces_batch_to_include_each_use(self):
        data, mem = self.data(round_no=64), Memory()
        self.prepare(data, mem)
        self.next_turn(data, [BASE], gold=568)
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2']['action'], 'buy')
        self.assertEqual(ledger.commands['2']['name'], BOSS)
        self.assertGreater(ledger.commands['2']['num'], 0)
        self.assertLess(ledger.commands['2']['num'], 3)

    def test_tick_67_does_not_preempt_a_feasible_robot_purchase(self):
        data = self.data(round_no=67)
        data['teamOur']['roles'][0].update(level=3, health=4500)
        _, cfg, _, _ = fortified_case(data)
        agent = Agent(cfg)
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['2'], command('buy', name=BOSS, num=2))
        for tick, held in ((68, 2), (69, 1)):
            data['roundNo'] = tick
            data['teamOur']['roles'][2]['backpack'] = [BOSS] * held
            data['teamOur']['goldNum'] = 428
            data['lastRoundRoleActionResults'] = {'2': True}
            response = agent.decide(copy.deepcopy(data))['roleCommandMap']
            self.assertEqual((response['2']['action'], response['2']['name']), ('use', BOSS))
        data['roundNo'] = 70
        data['teamOur']['roles'][2]['backpack'] = []
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['2']['action'], 'move')

    def test_temporarily_blocked_shop_retries_without_finishing_the_chain(self):
        data, mem = self.data(), Memory()
        turn, cfg, nav, ledger = fortified_case(data)
        ledger.operator_posts = {2: (1, 9)}
        with patch.object(nav, 'approach', return_value=None):
            self.assertTrue(prepare_first_day_pioneer(turn, cfg, mem, nav, ledger))
        self.assertFalse(ledger.commands)
        self.assertEqual(mem.first_day_pioneer['phase'], 'shop')
        self.next_turn(data, [])
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))

    def test_blocked_paid_base_delivery_does_not_skip_to_summoning_or_parking(self):
        data, mem = self.data(), Memory()
        self.prepare(data, mem)
        self.next_turn(data, [BASE], gold=568)
        self.prepare(data, mem)
        self.next_turn(data, [BASE, BOSS], gold=448)
        turn, cfg, nav, ledger = fortified_case(data)
        ledger.operator_posts = {2: (1, 9)}
        approach = nav.approach
        with patch.object(nav, 'approach', side_effect=lambda hero, targets, *args, **kw:
                          None if set(targets) == turn.station.cells else approach(hero, targets, *args, **kw)):
            self.assertTrue(prepare_first_day_pioneer(turn, cfg, mem, nav, ledger))
        self.assertFalse(ledger.commands)
        self.assertIn(2, ledger.used)
        self.assertEqual(mem.first_day_pioneer['phase'], 'station')

    def test_night_and_day_two_keep_existing_scheduling(self):
        for round_no in (70, 140):
            with self.subTest(round_no=round_no):
                accepted, _, ledger = self.prepare(self.data(round_no=round_no), Memory())
                self.assertFalse(accepted)
                self.assertFalse(ledger.commands)
