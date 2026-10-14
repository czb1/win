"""Day-two tick-50 purchase, task handoff and confirmed rear deployment."""
import copy
import unittest
from unittest.mock import patch

import test_first_day_pioneer_shopping as opening
from test_agent import setup_case, unit
from test_robot_assault import fortified_case, controlled_robot
from agent.brain import Agent, observe_robot_summons
from agent.commands import command
from agent.daily_offense import (BOSS, observe_daily_pioneer, prepare_daily_pioneer,
                                 daily_pioneer_active)
from agent.intelligence import Intelligence, Memory
from agent.model import pos
from agent.robot_assault import enemy_base, rear_approach
from agent.task_schedule import task_options


class DailyPioneerOffenseTests(unittest.TestCase):
    def data(self, day=2, tick=50, gold=600, origin=0):
        data = opening.FirstDayPioneerShoppingTests().data(
            round_no=(day - 1) * 130 + tick + origin, gold=gold)
        _, cfg, _, _ = fortified_case(data)
        cfg.robot_purchase_last_day = 1
        cfg.round_origin = origin
        cfg.llm_enabled = False
        return data, cfg

    def prepare(self, data, cfg, mem=None):
        mem = mem or Memory()
        turn, cfg, nav, ledger = setup_case(data, **vars(cfg))
        ledger.return_pairs = [(turn.pioneer, turn.weapons[0])]
        ledger.operator_posts = {2: (1, 9)}
        accepted = prepare_daily_pioneer(turn, cfg, mem, nav, ledger)
        return accepted, turn, nav, ledger, mem

    def next_turn(self, data, backpack, gold=None, result=True):
        data['roundNo'] += 1
        data['teamOur']['roles'][2]['backpack'] = list(backpack)
        data['lastRoundRoleActionResults'] = {'2': result}
        if gold is not None:
            data['teamOur']['goldNum'] = gold

    def test_only_day_two_onward_at_tick_50_with_both_round_origins(self):
        for origin in (0, 1):
            for day, tick, buys in ((1, 50, False), (2, 49, False), (2, 50, True),
                                    (3, 50, True), (6, 50, True), (10, 50, True), (2, 70, False)):
                with self.subTest(origin=origin, day=day, tick=tick):
                    data, cfg = self.data(day, tick, origin=origin)
                    _, _, _, ledger, _ = self.prepare(data, cfg)
                    self.assertEqual(ledger.commands.get('2') == command('buy', name=BOSS, num=5), buys)

    def test_whole_batch_budget_uses_current_price_including_zero(self):
        for price, gold, buys in ((120, 599, False), (120, 600, True), (120, 9999, True),
                                  (130, 649, False), (130, 650, True), (0, 0, True), (-1, 600, False)):
            with self.subTest(price=price, gold=gold):
                data, cfg = self.data(gold=gold)
                next(x for x in data['weaponShopList'] if x['name'] == BOSS)['price'] = price
                _, _, _, ledger, _ = self.prepare(data, cfg)
                self.assertEqual(bool(ledger.commands), buys)
                if buys:
                    self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=5))
                    self.assertEqual(ledger.gold, gold - price * 5)

    def test_missing_price_or_enemy_base_does_not_buy_other_robot_types(self):
        for missing in ('price', 'base'):
            data, cfg = self.data()
            if missing == 'price':
                data['weaponShopList'] = [x for x in data['weaponShopList'] if x['name'] != BOSS]
            else:
                data['teamEnemy']['roles'] = []
            _, _, _, ledger, _ = self.prepare(data, cfg)
            self.assertFalse(ledger.commands)

    def test_insufficient_gold_is_checked_once_and_again_next_day(self):
        data, cfg = self.data(gold=599)
        _, _, _, _, mem = self.prepare(data, cfg)
        self.next_turn(data, [], gold=600)
        self.assertFalse(self.prepare(data, cfg, mem)[3].commands)
        data['roundNo'] = 260 + 50
        self.assertEqual(self.prepare(data, cfg, mem)[3].commands['2'], command('buy', name=BOSS, num=5))

    def test_first_day_cancellation_and_incomplete_defenses_do_not_block_batch(self):
        data, cfg = self.data()
        for role in data['teamOur']['roles']:
            if role['roleType'] in ('wall', 'rocket', 'gatling'):
                role['level'] = 1
        mem = Memory(first_day_pioneer=dict(phase='shop'))
        mem.night_scout.cancelled = True
        _, _, _, ledger, _ = self.prepare(data, cfg, mem)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=5))

    def test_route_reserves_the_full_batch_before_other_shoppers(self):
        data, cfg = self.data(gold=700)
        data['teamOur']['roles'][2]['pos'] = dict(x=0, y=7)
        _, _, _, ledger, mem = self.prepare(data, cfg)
        self.assertEqual(ledger.commands['2']['action'], 'move')
        self.assertEqual(ledger.gold, 100)
        self.assertEqual(mem.robot_summon_state['purchased'], 0)

    def test_task_at_50_finishes_before_the_balance_is_checked(self):
        data, cfg = self.data(gold=0)
        data['phaseTask'] = 'active task'
        data['teamOur']['playerTasks'][0].update(coldDownRounds=30, timeoutRounds=40)
        agent = Agent(cfg)
        with patch.object(Intelligence, 'task', return_value=('', '')):
            response = agent.decide(copy.deepcopy(data))
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.daily_pioneer_offense['phase'], 'task')
        self.assertNotIn('2', response['roleCommandMap'])
        data['phaseTask'] = ''
        self.next_turn(data, [], gold=600)
        self.assertEqual(agent.decide(data)['roleCommandMap']['2'], command('buy', name=BOSS, num=5))

    def test_active_task_is_not_recalled_at_67_or_nightfall(self):
        data, cfg = self.data()
        data['phaseTask'] = 'active task'
        data['teamOur']['playerTasks'][0].update(coldDownRounds=30, timeoutRounds=40)
        agent = Agent(cfg)
        with patch.object(Intelligence, 'task', return_value=('', '')) as task:
            for tick in (50, 67, 70):
                data['roundNo'] = 130 + tick
                commands = agent.decide(copy.deepcopy(data))['roleCommandMap']
                mem = next(iter(agent.sessions.values()))
                self.assertNotIn('2', commands)
                self.assertNotIn(2, mem.return_targets)
                self.assertFalse(mem.stop_reason)
                self.assertGreater(task.call_args.kwargs['available_rounds'], 0)

    def test_funded_task_completion_past_67_still_buys_five(self):
        data, cfg = self.data()
        data['phaseTask'] = 'active task'
        data['teamOur']['playerTasks'][0].update(coldDownRounds=30, timeoutRounds=40)
        agent = Agent(cfg)
        with patch.object(Intelligence, 'task', return_value=('', '')):
            agent.decide(copy.deepcopy(data))
        data.update(roundNo=198, phaseTask='')
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['2'], command('buy', name=BOSS, num=5))
        self.assertNotIn(2, next(iter(agent.sessions.values())).return_targets)

    def test_new_tasks_and_task_switches_wait_for_the_batch(self):
        data, cfg = self.data()
        data['teamOur']['playerTasks'][0].update(isValid=True, timeoutRounds=1)
        _, turn, nav, ledger, mem = self.prepare(data, cfg)
        ledger.used.clear()
        self.assertFalse(task_options(turn, cfg, mem, nav, ledger, turn.pioneer))
        self.assertFalse(task_options(turn, cfg, mem, nav, ledger, turn.pioneer, switching=True))

    def test_agent_buys_five_uses_five_unique_rear_positions_then_returns(self):
        data, cfg = self.data(gold=2000)
        agent = Agent(cfg)
        response = agent.decide(copy.deepcopy(data))
        self.assertEqual(response['roleCommandMap']['2'], command('buy', name=BOSS, num=5))
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)
        points = []
        for count in range(5, 0, -1):
            self.next_turn(data, [BOSS] * count, gold=1400)
            commands = agent.decide(copy.deepcopy(data))['roleCommandMap']
            self.assertEqual((commands['2']['action'], commands['2']['name']), ('use', BOSS))
            points.append(pos(commands['2']['targetPos'][0]))
            self.assertFalse(any(c['action'] == 'buy' and c['name'] == BOSS for c in commands.values()))
        self.next_turn(data, [], gold=1400)
        commands = agent.decide(copy.deepcopy(data))['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        turn, _, _, _ = setup_case(data, **vars(cfg))
        self.assertEqual(len(set(points)), 5)
        self.assertTrue(all(turn.summon_position_legal(p) and rear_approach(turn, enemy_base(turn), p)
                            for p in points))
        self.assertEqual(mem.robot_summon_state['count'], 5)
        self.assertEqual(mem.daily_pioneer_offense['remaining'], 0)
        self.assertEqual(mem.daily_pioneer_offense['phase'], 'return')
        self.assertIn(2, mem.return_targets)
        self.assertEqual(commands['2']['action'], 'move')

    def test_confirmed_orders_are_not_bought_twice_and_existing_stock_is_preserved(self):
        data, cfg = self.data(gold=1200)
        data['teamOur']['roles'][2]['backpack'] = [BOSS] * 2
        _, _, _, ledger, mem = self.prepare(data, cfg)
        self.assertEqual(ledger.commands['2']['num'], 5)
        for count in range(7, 2, -1):
            self.next_turn(data, [BOSS] * count)
            ledger = self.prepare(data, cfg, mem)[3]
            self.assertEqual(ledger.commands['2']['action'], 'use')
        self.next_turn(data, [BOSS] * 2)
        ledger = self.prepare(data, cfg, mem)[3]
        self.assertFalse(ledger.commands)
        self.assertEqual(mem.daily_pioneer_offense['phase'], 'return')
        self.assertEqual(mem.robot_summon_state['purchased'], 7)

    def test_failed_or_undelivered_purchase_retries_exactly_five(self):
        for result in (False, True):
            with self.subTest(result=result):
                data, cfg = self.data()
                _, _, _, _, mem = self.prepare(data, cfg)
                self.next_turn(data, [], result=result)
                ledger = self.prepare(data, cfg, mem)[3]
                self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=5))
                self.assertEqual(mem.robot_summon_state['purchased'], 5)

    def test_failed_or_undelivered_summon_releases_its_slot_and_retries(self):
        for result in (False, True):
            with self.subTest(result=result):
                data, cfg = self.data()
                _, _, _, _, mem = self.prepare(data, cfg)
                self.next_turn(data, [BOSS] * 5, gold=0)
                first = self.prepare(data, cfg, mem)[3].commands['2']
                self.next_turn(data, [BOSS] * 5, result=result)
                retry = self.prepare(data, cfg, mem)[3].commands['2']
                self.assertEqual(first, retry)
                self.assertEqual(mem.robot_summon_state['count'], 1)
                self.assertEqual(len(mem.robot_summon_state['positions']), 1)
                self.assertEqual(mem.daily_pioneer_offense['remaining'], 5)

    def test_daily_limit_includes_carried_and_already_reserved_orders(self):
        for used, held, buys in ((5, 0, True), (6, 0, False), (4, 1, True), (4, 2, False)):
            with self.subTest(used=used, held=held):
                data, cfg = self.data()
                data['teamOur']['roles'][1]['backpack'] = [BOSS] * held
                mem = Memory()
                turn, _, _, _ = setup_case(data, **vars(cfg))
                observe_robot_summons(turn, mem)['count'] = used
                ledger = self.prepare(data, cfg, mem)[3]
                self.assertEqual(bool(ledger.commands), buys)

    def test_full_backpack_or_no_route_does_not_buy_partial_batch(self):
        for condition in ('capacity', 'route'):
            data, cfg = self.data()
            if condition == 'capacity':
                data['teamOur']['roles'][2]['backPackCapability'] = 4
            elif condition == 'route':
                data['mapInfo']['zones'] = []
            ledger = self.prepare(data, cfg)[3]
            self.assertFalse(ledger.commands)

    def test_late_batch_pauses_for_night_guns_and_resumes_without_rebuying(self):
        data, cfg = self.data(tick=68)
        _, _, _, ledger, mem = self.prepare(data, cfg)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=5))
        self.next_turn(data, [BOSS] * 5, gold=0)
        self.assertEqual(self.prepare(data, cfg, mem)[3].commands['2']['action'], 'use')
        self.next_turn(data, [BOSS] * 4)
        self.assertFalse(self.prepare(data, cfg, mem)[3].commands)
        self.assertEqual(mem.daily_pioneer_offense['phase'], 'robots')
        data['roundNo'] = 260
        for count in range(4, 0, -1):
            data['teamOur']['roles'][2]['backpack'] = [BOSS] * count
            ledger = self.prepare(data, cfg, mem)[3]
            self.assertEqual(ledger.commands['2']['action'], 'use')
            data['roundNo'] += 1
        data['teamOur']['roles'][2]['backpack'] = []
        self.prepare(data, cfg, mem)
        self.assertEqual(mem.daily_pioneer_offense['phase'], 'return')
        self.assertEqual(mem.robot_summon_state['count'], 4)
        data['roundNo'] = 310
        data['teamOur']['goldNum'] = 600
        self.assertEqual(self.prepare(data, cfg, mem)[3].commands['2'], command('buy', name=BOSS, num=5))

    def test_night_task_completion_checks_gold_the_next_morning(self):
        data, cfg = self.data()
        data['phaseTask'] = 'active task'
        _, _, _, _, mem = self.prepare(data, cfg)
        data.update(roundNo=200, phaseTask='')
        self.assertFalse(self.prepare(data, cfg, mem)[3].commands)
        data['roundNo'] = 260
        self.assertEqual(self.prepare(data, cfg, mem)[3].commands['2'], command('buy', name=BOSS, num=5))

    def test_unconfirmed_old_day_use_does_not_release_a_new_day_slot(self):
        data, cfg = self.data(tick=68)
        _, _, _, _, mem = self.prepare(data, cfg)
        self.next_turn(data, [BOSS] * 5, gold=0)
        self.prepare(data, cfg, mem)
        data['roundNo'] = 260
        ledger = self.prepare(data, cfg, mem)[3]
        self.assertEqual(ledger.commands['2']['action'], 'use')
        self.assertEqual(mem.robot_summon_state['count'], 1)
        self.assertEqual(mem.daily_pioneer_offense['remaining'], 5)

    def test_rear_positions_mirror_with_the_bases(self):
        for mirror in (False, True):
            data, cfg = self.data()
            if mirror:
                width, height = data['mapInfo']['width'], data['mapInfo']['height']
                for team in ('teamOur', 'teamEnemy'):
                    for role in data[team]['roles']:
                        x, y = pos(role['pos'])
                        base = role['roleType'] == 'station'
                        role['pos'] = dict(x=width - 1 - x - int(base), y=height - 1 - y + int(base))
                for zone in data['mapInfo']['zones']:
                    x, y = pos(zone['pos'])
                    zone['pos'] = dict(x=width - 1 - x, y=height - 1 - y)
                cfg.weapon_cells = [[width - 1 - x, height - 1 - y] for x, y in cfg.weapon_cells]
                cfg.wall_cells = [[width - 1 - x, height - 1 - y] for x, y in cfg.wall_cells]
            _, _, _, ledger, mem = self.prepare(data, cfg)
            self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=5))
            for count in range(5, 0, -1):
                self.next_turn(data, [BOSS] * count, gold=0)
                _, turn, _, ledger, _ = self.prepare(data, cfg, mem)
                point = pos(ledger.commands['2']['targetPos'][0])
                self.assertTrue(rear_approach(turn, enemy_base(turn), point))
                self.assertTrue(turn.summon_position_legal(point))

    def test_night_uses_existing_robot_attack_logic_and_cannon_assignment(self):
        data, cfg = self.data(tick=69)
        agent = Agent(cfg)
        agent.decide(copy.deepcopy(data))
        mem = next(iter(agent.sessions.values()))
        mem.daily_pioneer_offense = dict(day=2, phase='return', remaining=0)
        data['roundNo'] = 200
        data['teamOur']['summonRobotList'] = [controlled_robot(x=14, y=4)]
        data['robot']['roles'] = [unit(90, 'smallRobot', 6, 8, targetTeam='challenger')]
        commands = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertIn(commands['30000']['action'], ('attack', 'move'))
        data['teamOur']['roles'][2]['pos'] = dict(zip(('x', 'y'), mem.return_posts.get(2, mem.gunner_post)))
        data['roundNo'] += 1
        commands = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertTrue(any(c['action'] == 'attack' and c.get('controllerId') == '2'
                            for c in commands.values()))

    def test_state_from_previous_day_does_not_take_over_next_morning(self):
        data, cfg = self.data(day=3, tick=0)
        mem = Memory(daily_pioneer_offense=dict(day=2, phase='return'))
        turn, _, _, _ = setup_case(data, **vars(cfg))
        observe_daily_pioneer(turn, mem)
        self.assertFalse(daily_pioneer_active(turn, mem))


if __name__ == '__main__':
    unittest.main()
