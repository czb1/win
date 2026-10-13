"""Opening purchases persist in daylight and yield to night defense."""
import copy
import unittest
from unittest.mock import patch

import test_first_day_pioneer_shopping as shopping
import test_vehicle as vehicles
from test_robot_assault import fortified_case
from agent.brain import Agent
from agent.commands import command
from agent.intelligence import Memory
from agent.model import distance, pos
from agent.config import Config
from test_agent import payload, unit


BASE, BOSS = shopping.BASE, shopping.BOSS


class PioneerPurchaseCompletionTests(unittest.TestCase):
    def data(self, **kwargs):
        return shopping.FirstDayPioneerShoppingTests().data(**kwargs)

    def prepare(self, data, mem):
        return shopping.FirstDayPioneerShoppingTests().prepare(data, mem)

    def state(self, phase='shop', **kwargs):
        return dict(phase=phase, station_item=BASE, pending_buy=None, **kwargs)

    def test_late_shop_trip_keeps_procurement_instead_of_returning(self):
        data, mem = self.data(round_no=66), Memory()
        data['teamOur']['roles'][2]['pos'] = {'x': 0, 'y': 0}
        accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual(mem.first_day_pioneer['phase'], 'shop')
        self.assertEqual(ledger.notes[2]['reason'], 'first_day_pioneer_shop')
        self.assertNotIn(2, mem.return_targets)

    def test_unreachable_base_leg_does_not_cancel_the_shop_purchase(self):
        data, mem = self.data(round_no=55), Memory()
        with patch('agent.spending.via', return_value=None):
            accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))
        self.assertEqual(mem.first_day_pioneer['phase'], 'shop')

    def test_tight_daylight_uses_robots_while_reserving_base_voucher(self):
        data = self.data(round_no=66)
        data['teamOur']['roles'][2]['backpack'] = [BASE] + [BOSS] * 3
        mem = Memory(first_day_pioneer=self.state('station'))
        accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertEqual((ledger.commands['2']['action'], ledger.commands['2'].get('name')),
                         ('use', BOSS))
        self.assertEqual(mem.first_day_pioneer['phase'], 'robots')
        self.assertNotIn(2, mem.return_targets)

    def test_daily_limit_does_not_release_a_carried_order_to_recall(self):
        data = self.data(round_no=69)
        data['teamOur']['roles'][0].update(level=2, health=3000)
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        mem = Memory(first_day_pioneer=self.state('robots'))
        mem.robot_summon_state = dict(day=1, count=10, positions=set(), pending=None,
                                      observed_round=-1, buyer=None, purchased=1)
        accepted, _, ledger = self.prepare(data, mem)
        self.assertTrue(accepted)
        self.assertFalse(ledger.commands)
        self.assertIn(2, ledger.used)
        self.assertEqual(mem.first_day_pioneer['phase'], 'robots')
        self.assertNotIn(2, mem.return_targets)

    def test_legal_purchase_without_received_inventory_is_not_completion(self):
        data, mem = self.data(), Memory()
        self.prepare(data, mem)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'2': True}
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))
        self.assertEqual(mem.first_day_pioneer['phase'], 'shop')
        self.assertEqual(mem.robot_summon_state['purchased'], 0)

    def test_missing_robot_delivery_retries_without_double_counting(self):
        data, mem = self.data(), Memory()
        self.prepare(data, mem)
        data.update(roundNo=11, lastRoundRoleActionResults={'2': True})
        data['teamOur']['roles'][2]['backpack'] = [BASE]
        data['teamOur']['goldNum'] = 568
        self.prepare(data, mem)
        data['roundNo'] = 12
        _, _, ledger = self.prepare(data, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=3))
        self.assertEqual(mem.robot_summon_state['purchased'], 3)
        self.assertEqual(mem.first_day_pioneer['robot_purchased'], 3)
        self.assertNotIn(2, mem.return_targets)

    def test_match_coordinates_keep_shopping_after_round_55_driving_rejection(self):
        data = payload(55, [unit(20013, 'station', 30, 10, health=1500),
                            unit(20011, 'pioneer', 25, 16, isDriving=True)])
        data['mapInfo'].update(width=41, height=32, zones=[
            dict(neutralType='weaponShop', pos=dict(x=25, y=20)),
            dict(neutralType='defenderVehicle', pos=dict(x=25, y=16))])
        data['teamOur'].update(type='defender', goldNum=515)
        data['teamEnemy']['roles'] = [unit(10013, 'station', 9, 22)]
        data['weaponShopList'] = [dict(name=BASE, price=100), dict(name=BOSS, price=120)]
        agent = Agent(Config(layout_mode='explicit', llm_enabled=False))
        first = agent.decide(copy.deepcopy(data))['roleCommandMap']['20011']
        self.assertEqual(first, {'action': 'move', 'targetPos': [{'x': 24, 'y': 17}, {'x': 23, 'y': 18}]})
        data.update(roundNo=56, lastRoundRoleActionResults={'20011': False})
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.first_day_pioneer['phase'], 'shop')
        self.assertEqual(response['20011'], command('move', (24, 17)))
        self.assertNotIn(20011, mem.return_targets)

    def test_night_rejection_preserves_order_until_next_legal_day(self):
        data = self.data(round_no=69)
        data['teamOur']['roles'][0].update(level=3, health=4500)
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        _, cfg, _, _ = fortified_case(data)
        agent = Agent(cfg)
        agent.decide(copy.deepcopy(data))
        mem = next(iter(agent.sessions.values()))
        mem.first_day_pioneer = self.state('robots')
        data.update(roundNo=70, lastRoundRoleActionResults={'2': False})
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['2']['action'], 'move')
        self.assertEqual(mem.first_day_pioneer['phase'], 'robots')
        self.assertIn(2, mem.return_targets)
        data['roundNo'] = 130
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual((response['2']['action'], response['2']['name']), ('use', BOSS))
        self.assertNotIn(2, mem.return_targets)
        data.update(roundNo=131, lastRoundRoleActionResults={'2': True})
        data['teamOur']['roles'][2]['backpack'] = []
        agent.decide(copy.deepcopy(data))
        self.assertEqual(mem.first_day_pioneer['phase'], 'return')

    def test_legacy_base_delivery_yields_at_night_then_returns_with_voucher(self):
        data = self.data(round_no=70)
        data['teamOur']['roles'][2]['backpack'] = [BASE]
        mem = Memory(first_day_pioneer=self.state('station', robots_done=True))
        accepted, _, ledger = self.prepare(data, mem)
        self.assertFalse(accepted)
        self.assertFalse(ledger.commands)
        self.assertNotIn(2, ledger.used)
        self.assertEqual(mem.first_day_pioneer['phase'], 'station')
        self.assertNotIn(2, mem.return_targets)
        data['roundNo'] = 130
        data['teamOur']['roles'][2]['pos'] = {'x': 5, 'y': 10}
        _, _, ledger = self.prepare(data, mem)
        self.assertFalse(any(c['action'] == 'use' and c.get('name') == BASE for c in ledger.commands.values()))
        self.assertEqual(mem.first_day_pioneer['phase'], 'return')
        self.assertIn(2, mem.return_targets)

    def test_unfinished_opening_stages_return_and_fire_with_orders_at_night(self):
        for phase, backpack in (('shop', []), ('station', [BASE, BOSS]), ('robots', [BOSS])):
            with self.subTest(phase=phase):
                data = self.data(round_no=69)
                _, cfg, _, _ = fortified_case(data)
                cfg.loadout = ['rocket'] * 3
                for role in data['teamOur']['roles']:
                    if role['roleType'] == 'gatling':
                        role['roleType'] = 'rocket'
                agent = Agent(cfg)
                agent.decide(copy.deepcopy(data))
                mem = next(iter(agent.sessions.values()))
                mem.first_day_pioneer = self.state(phase)
                data['teamOur']['roles'][2]['backpack'] = backpack
                if phase == 'robots':
                    mem.robot_summon_state['count'] = 10
                data['roundNo'] = 70
                response = agent.decide(copy.deepcopy(data))['roleCommandMap']
                self.assertEqual(response['2']['action'], 'move')
                self.assertIn(2, mem.return_targets)
                self.assertEqual(mem.first_day_pioneer['phase'], phase)
                data['teamOur']['roles'][2]['pos'] = dict(zip(('x', 'y'), mem.gunner_post))
                data['roundNo'] = 71
                data['robot']['roles'] = [unit(90, 'smallRobot', 7, 8, targetTeam='challenger')]
                response = agent.decide(copy.deepcopy(data))['roleCommandMap']
                self.assertTrue(any(c['action'] == 'attack' and c['controllerId'] == '2'
                                    for c in response.values()))
                self.assertEqual(mem.first_day_pioneer['phase'], phase)

    def test_carried_orders_release_the_pioneer_every_night_without_losing_work(self):
        for round_no in (69, 70, 129, 130, 199, 200, 259, 260):
            with self.subTest(round_no=round_no):
                data = self.data(round_no=round_no)
                data['teamOur']['roles'][0].update(level=3, health=4500)
                data['teamOur']['roles'][2]['backpack'] = [BOSS]
                mem = Memory(first_day_pioneer=self.state('robots', robot_purchased=3))
                accepted, turn, ledger = self.prepare(data, mem)
                self.assertEqual(accepted, turn.is_day)
                self.assertEqual(2 in ledger.used, turn.is_day)
                self.assertEqual(mem.first_day_pioneer['phase'], 'robots')
                self.assertEqual(mem.first_day_pioneer['robot_purchased'], 3)
                if turn.is_day:
                    self.assertEqual((ledger.commands['2']['action'], ledger.commands['2']['name']),
                                     ('use', BOSS))
                else:
                    self.assertFalse(ledger.commands)


class DrivingFailureFallbackTests(unittest.TestCase):
    def observed_move(self, landed):
        mem = Memory()
        data = vehicles.case(start=(2, 5), driving=True, round_no=55)
        turn, cfg, _, _, _, _ = vehicles.travel(data)
        mem.movement.observe(turn, mem)
        mem.last_round = 55
        mem.last_commands = {'11': {'action': 'move', 'targetPos': [{'x': 3, 'y': 5}, {'x': 4, 'y': 5}]}}
        data = vehicles.case(start=landed, car=landed, driving=True, round_no=56)
        data['lastRoundRoleActionResults'] = {'11': landed == (4, 5)}
        turn, cfg, nav, ledger, _, _ = vehicles.travel(data)
        mem.movement.observe(turn, mem)
        nav.memory = mem.movement
        return mem, turn, nav, ledger

    def test_failed_two_cell_move_uses_one_step_even_towards_another_goal(self):
        for landed in ((2, 5), (3, 5)):
            with self.subTest(landed=landed):
                _, turn, nav, ledger = self.observed_move(landed)
                route = nav.search(turn.pioneer, {(10, 7)})
                self.assertTrue(nav.move_along(turn.pioneer, ledger, route, {(10, 7)}))
                self.assertEqual(distance(turn.pioneer.pos,
                                          pos(ledger.commands['11']['targetPos'][-1])), 1)

    def test_successful_driving_keeps_two_cell_acceleration(self):
        _, turn, nav, ledger = self.observed_move((4, 5))
        route = nav.search(turn.pioneer, {(10, 5)})
        self.assertTrue(nav.move_along(turn.pioneer, ledger, route, {(10, 5)}))
        self.assertEqual(distance(turn.pioneer.pos,
                                  pos(ledger.commands['11']['targetPos'][-1])), 2)

    def test_new_day_can_retry_observed_driving(self):
        mem, _, _, _ = self.observed_move((2, 5))
        data = vehicles.case(start=(2, 5), car=(2, 5), driving=True, round_no=130)
        turn, cfg, nav, ledger, _, _ = vehicles.travel(data)
        mem.movement.observe(turn, mem)
        nav.memory = mem.movement
        route = nav.search(turn.pioneer, {(10, 5)})
        self.assertTrue(nav.move_along(turn.pioneer, ledger, route, {(10, 5)}))
        self.assertEqual(distance(turn.pioneer.pos,
                                  pos(ledger.commands['11']['targetPos'][-1])), 2)
