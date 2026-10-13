"""A live day-one Gatling sighting cancels the opening assault for the match."""
import copy
import unittest

from test_agent import unit
import test_first_day_offense as offense
import test_first_day_pioneer_shopping as shopping
from test_first_day_wall_block import ENTRY, memory_for
from test_robot_assault import fortified_case, TOWERS, WALLS
from agent.brain import Agent, observe_robot_summons, summon_best_robot
from agent.commands import command
from agent.intelligence import Memory
from agent.model import SUMMON_ORDERS
from agent.scouting import (ScoutMemory, act_night_scout, prepare_first_day_wall_blocker,
                            prepare_night_scout, yield_wall_entry)
from agent.spending import first_day_purchase_reserve
from agent.worker_jobs import resume_daytime_jobs


BOSS, BASE = shopping.BOSS, shopping.BASE


def gatling(data, health=1000):
    data['teamEnemy']['roles'].append(unit(20020, 'gatling', 30, 11, health=health))


class FirstDayGatlingAbortTests(unittest.TestCase):
    def shopping_data(self, **kwargs):
        return shopping.FirstDayPioneerShoppingTests().data(**kwargs)

    def prepare_pioneer(self, data, memory):
        return shopping.FirstDayPioneerShoppingTests().prepare(data, memory)

    def assert_no_robot_orders(self, commands):
        self.assertFalse(any(c['action'] in ('buy', 'use') and c.get('name') in SUMMON_ORDERS
                             for c in commands.values()), commands)

    def test_wall_worker_immediately_mines_and_stays_released_after_losing_sight(self):
        agent = Agent(offense.cfg(walls=[]))
        data = offense.case(34, stock=[BOSS], gold=0)
        agent.decide(copy.deepcopy(data))
        memory = next(iter(agent.sessions.values()))
        memory.night_scout = memory_for()
        memory.first_day_offense.update(worker_id=10, phase='block')
        memory.daytime_jobs = {uid: dict(kind='robot_buy', target=(15, 20), name=BOSS, quantity=1)
                               for uid in (10, 12)}
        memory.sold_workers.add(10)
        memory.sale_workers.add(10)
        memory.preparation_workers.add(10)
        memory.build_targets[10] = (8, 21)
        memory.return_targets[10] = 20
        memory.return_posts[10] = (7, 23)
        memory.supply_worker = 10
        data['roundNo'] = 35
        data['teamOur']['goldNum'] = 500
        data['teamOur']['roles'][1]['pos'] = dict(x=ENTRY[0], y=ENTRY[1])
        data['mapInfo']['zones'] = [dict(neutralType='iron', pos=dict(x=30, y=13), remain=10),
                                  dict(neutralType='vendor', pos=dict(x=28, y=14))]
        gatling(data)
        response = agent.decide(copy.deepcopy(data))
        self.assertEqual(response['roleCommandMap']['10'], command('collect', (30, 13)))
        self.assert_no_robot_orders(response['roleCommandMap'])
        self.assertTrue(memory.night_scout.cancelled)
        self.assertIsNone(memory.night_scout.worker_id)
        self.assertIsNone(memory.night_scout.wall_entry)
        self.assertEqual(memory.first_day_offense['phase'], 'mining')
        self.assertNotIn(10, memory.build_targets)
        self.assertNotIn(10, memory.sold_workers)
        self.assertFalse(any(j['kind'] == 'robot_buy' for j in memory.daytime_jobs.values()))
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)

        data['roundNo'] = 36
        data['teamEnemy']['roles'].pop()
        data['lastRoundRoleActionResults'] = {'10': True}
        response = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(response['10'], command('collect', (30, 13)))
        self.assert_no_robot_orders(response)
        self.assertTrue(memory.night_scout.cancelled)
        self.assertIsNone(memory.night_scout.worker_id)

    def test_sighting_blocks_all_carried_tiers_even_on_last_daylight_turn(self):
        for name in SUMMON_ORDERS:
            with self.subTest(name=name):
                data = self.shopping_data(round_no=69)
                data['teamOur']['roles'][1]['backpack'] = [name]
                gatling(data)
                turn, cfg, nav, ledger = fortified_case(data)
                memory = Memory()
                self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                   TOWERS, WALLS, use_only=True))
                self.assertFalse(ledger.commands)
                self.assertEqual(ledger.gold, turn.gold)
                self.assertEqual(ledger.spending_plan['robot_use_blocked'], 'enemy_gatling_spotted')

    def test_sighting_blocks_purchase_and_releases_only_the_robot_budget(self):
        data = self.shopping_data()
        gatling(data)
        turn, cfg, nav, ledger = fortified_case(data)
        memory = Memory()
        self.assertEqual(first_day_purchase_reserve(turn, memory, ledger), 100)
        self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS))
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.gold, turn.gold)
        self.assertIsNone(memory.robot_summon_state['buyer'])

    def test_pioneer_keeps_base_voucher_purchase_then_skips_robot_shop(self):
        data, memory = self.shopping_data(), Memory()
        gatling(data)
        accepted, _, ledger = self.prepare_pioneer(data, memory)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2'], command('buy', name=BASE, num=1))
        self.assertNotIn('robot_purchase', ledger.spending_plan)
        data['roundNo'] += 1
        data['teamOur']['roles'][2]['backpack'] = [BASE]
        data['lastRoundRoleActionResults'] = {'2': True}
        data['teamEnemy']['roles'].pop()
        _, _, ledger = self.prepare_pioneer(data, memory)
        self.assertEqual(memory.first_day_pioneer['phase'], 'return')
        self.assertTrue(memory.first_day_pioneer['robots_cancelled'])
        self.assertFalse(memory.first_day_pioneer['robots_done'])
        self.assert_no_robot_orders(ledger.commands)
        self.assertNotIn(2, ledger.used)

    def test_pioneer_with_orders_does_not_wait_in_any_opening_phase(self):
        for phase in ('shop', 'station', 'robots'):
            with self.subTest(phase=phase):
                data = self.shopping_data(round_no=55)
                data['teamOur']['roles'][2]['backpack'] = [BASE, BOSS]
                gatling(data)
                memory = Memory(first_day_pioneer=dict(phase=phase, station_item=BASE,
                                                        pending_buy=None))
                _, _, ledger = self.prepare_pioneer(data, memory)
                self.assert_no_robot_orders(ledger.commands)
                self.assertEqual(memory.first_day_pioneer['phase'], 'return')
                self.assertNotIn(2, ledger.used)

    def test_pending_robot_purchase_is_reconciled_without_retrying_after_abort(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                data = self.shopping_data(round_no=55)
                data['teamOur']['roles'][2]['backpack'] = [BASE] + ([BOSS] * 3 if accepted else [])
                data['lastRoundRoleActionResults'] = {'2': accepted}
                gatling(data)
                memory = Memory(first_day_pioneer=dict(phase='robots', station_item=BASE,
                    pending_buy=(54, BOSS, 3, 0), robot_purchased=3))
                memory.robot_summon_state = dict(day=1, count=0, positions=set(), pending=None,
                    observed_round=54, purchased=3, pending_purchase=(54, 2, 3))
                _, _, ledger = self.prepare_pioneer(data, memory)
                self.assert_no_robot_orders(ledger.commands)
                self.assertEqual(memory.first_day_pioneer['phase'], 'return')
                self.assertIsNone(memory.first_day_pioneer['pending_buy'])
                self.assertEqual(memory.robot_summon_state['purchased'], 3 if accepted else 0)

    def test_cancelled_scout_cannot_be_reassigned_by_day_or_night_fallbacks(self):
        for tick, actions in ((55, (prepare_first_day_wall_blocker, prepare_night_scout)),
                              (70, (yield_wall_entry, act_night_scout))):
            for action in actions:
                with self.subTest(tick=tick, action=action.__name__):
                    data = offense.case(tick)
                    turn, _, nav, ledger = fortified_case(data)
                    scout = memory_for()
                    scout.cancelled = True
                    self.assertIsNone(action(turn, scout, nav, ledger))
                    self.assertIsNone(scout.worker_id)
                    self.assertIsNone(scout.wall_entry)
                    self.assertFalse(ledger.used)
                    self.assertFalse(ledger.commands)

    def test_stale_robot_purchase_job_cannot_restart(self):
        data = self.shopping_data(round_no=35)
        data['teamOur']['roles'][1]['backpack'] = [BOSS]
        turn, cfg, nav, ledger = fortified_case(data)
        memory = Memory(night_scout=ScoutMemory(cancelled=True), daytime_jobs={
            1: dict(kind='robot_buy', target=(4, 9), name=BOSS, quantity=3)})
        resume_daytime_jobs(turn, cfg, memory, nav, ledger, TOWERS, WALLS, set())
        self.assertNotIn(1, memory.daytime_jobs)
        self.assert_no_robot_orders(ledger.commands)

    def test_abort_survives_night_and_next_day_without_erasing_submitted_summons(self):
        data = self.shopping_data(round_no=55)
        gatling(data)
        memory = Memory()
        turn, _, _, _ = fortified_case(data)
        memory.robot_summon_state = dict(day=1, count=1, positions={(25, 13)},
            anchor=(25, 13), pending=(54, 1, (25, 13)), observed_round=54, purchased=3)
        observe_robot_summons(turn, memory)
        self.assertEqual(memory.robot_summon_state['count'], 1)
        self.assertEqual(memory.robot_summon_state['positions'], {(25, 13)})
        data['teamEnemy']['roles'].pop()
        for tick in (70, 130):
            for stock in ([], [BOSS]):
                with self.subTest(tick=tick, stock=stock):
                    data['roundNo'] = tick
                    data['teamOur']['roles'][1]['backpack'] = stock
                    turn, cfg, nav, ledger = fortified_case(data)
                    self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS))
                    self.assertFalse(ledger.commands)
                    self.assertTrue(memory.night_scout.cancelled)

    def test_only_live_enemy_gatling_seen_in_first_daylight_triggers_abort(self):
        for tick, kind, health, team in ((35, 'gatling', 1000, 'teamOur'),
                (35, 'gatling', 0, 'teamEnemy'), (35, 'rocket', 1000, 'teamEnemy'),
                (35, 'railgun', 1000, 'teamEnemy'), (70, 'gatling', 1000, 'teamEnemy'),
                (130, 'gatling', 1000, 'teamEnemy')):
            with self.subTest(tick=tick, kind=kind, health=health, team=team):
                data = self.shopping_data(round_no=tick)
                data[team]['roles'].append(unit(20020, kind, 12, 7, health=health))
                turn, _, _, _ = fortified_case(data)
                memory = Memory()
                observe_robot_summons(turn, memory)
                self.assertFalse(memory.night_scout.cancelled)

    def test_nonzero_round_origin_and_new_match_memory(self):
        config = offense.cfg(walls=[])
        config.round_origin = 1
        agent = Agent(config)
        data = offense.case(70, stock=[BOSS])  # Last daylight tick with origin 1.
        gatling(data)
        self.assert_no_robot_orders(agent.decide(copy.deepcopy(data))['roleCommandMap'])
        memory = next(iter(agent.sessions.values()))
        self.assertTrue(memory.night_scout.cancelled)
        data['roundNo'] = 1  # Rewind starts a new match, without inherited sight.
        data['teamEnemy']['roles'].pop()
        agent.decide(data)
        self.assertFalse(next(iter(agent.sessions.values())).night_scout.cancelled)
