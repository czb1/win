"""Fast boundaries for surplus BOSS batches and persistent rear assaults."""
import copy
import unittest

from test_agent import setup_case, unit
import test_robot_assault as assault_fixtures
from test_robot_assault import (WALLS, TOWERS,
                               assault_payload, controlled_robot, enemy_ring,
                               fortified_case, placement_payload)
from agent.brain import summon_best_robot
from agent.commands import command
from agent.intelligence import Memory
from agent.model import pos
from agent.navigation import DeadlineExceeded
from agent.robot_assault import (RobotAssaultMemory, act_robots, choose_summon_position,
                                 enemy_base, rear_approach, weakest_approach)
from agent.worker_jobs import resume_daytime_jobs


class SurplusBossBatchTests(unittest.TestCase):
    def data(self, gold=492, round_no=260):
        return assault_fixtures.BestRobotSummoningTests().shop_data(gold=gold, round_no=round_no)

    def buy(self, data, *, reserve=0, memory=None, post=None, return_margin=None):
        turn, cfg, nav, ledger = fortified_case(data)
        if return_margin is not None:
            cfg.return_margin = return_margin
        if post is not None:
            ledger.operator_posts[1] = post
        memory = memory or Memory()
        result = summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS,
                                   excluded={2}, reserve=reserve)
        return result, ledger, memory

    def test_492_gold_buys_four_bosses_in_one_action(self):
        result, ledger, _ = self.buy(self.data())
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1'], command('buy', name='BossRobotSummonOrder', num=4))
        self.assertEqual(ledger.gold, 12)
        self.assertEqual(ledger.spending_plan['robot_purchase']['total_cost'], 480)

    def test_current_shop_price_and_defensive_reserve_limit_batch(self):
        data = self.data()
        for listing in data['weaponShopList']:
            if listing['name'] == 'BossRobotSummonOrder':
                listing['price'] = 130
        result, ledger, _ = self.buy(data)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['num'], 3)
        self.assertEqual(ledger.gold, 102)
        result, ledger, _ = self.buy(self.data(), reserve=30)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['num'], 3)
        self.assertEqual(ledger.gold, 132)

    def test_batch_fits_backpack_and_remaining_daily_uses(self):
        data = self.data()
        data['teamOur']['roles'][1]['backPackCapability'] = 2
        result, ledger, _ = self.buy(data)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['num'], 2)
        memory = Memory()
        memory.robot_summon_state = dict(day=3, count=8, positions=set(), pending=None,
                                         observed_round=260, buyer=None)
        result, ledger, _ = self.buy(self.data(gold=1200), memory=memory)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['num'], 2)
        self.assertEqual(ledger.gold, 960)

    def test_each_use_requires_a_turn_before_operator_return(self):
        # At shop and post, eight turns fit two uses with margin 5 or four with 0.
        for margin, count in ((5, 2), (0, 4)):
            result, ledger, _ = self.buy(self.data(round_no=322), post=(4, 8), return_margin=margin)
            self.assertTrue(result)
            self.assertEqual(ledger.commands['1']['num'], count)
            self.assertEqual(ledger.gold, 492 - 120 * count)
        result, ledger, _ = self.buy(self.data(round_no=329), post=(4, 8))
        self.assertFalse(result)
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.gold, 492)

    def test_zero_shop_price_still_obeys_daily_limit(self):
        data = self.data(gold=0)
        data['weaponShopList'][-1]['price'] = 0
        result, ledger, _ = self.buy(data)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['num'], 10)
        self.assertEqual(ledger.gold, 0)

    def test_walking_buyer_reserves_the_whole_batch(self):
        data = self.data()
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 0}
        result, ledger, _ = self.buy(data)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['action'], 'move')
        self.assertEqual(ledger.work_jobs[1]['quantity'], 4)
        self.assertEqual(ledger.gold, 12)

    def test_continuation_shrinks_to_new_budget_without_expanding_again(self):
        data = self.data(gold=240)
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 0}
        memory = Memory(daytime_jobs={1: dict(kind='robot_buy', target=(4, 9),
                        name='BossRobotSummonOrder', quantity=4)})
        turn, cfg, nav, ledger = fortified_case(data)
        resume_daytime_jobs(turn, cfg, memory, nav, ledger, TOWERS, WALLS, set())
        self.assertEqual(ledger.work_jobs[1]['quantity'], 2)
        self.assertEqual(ledger.gold, 0)
        memory.daytime_jobs = ledger.work_jobs.copy()
        data['roundNo'] += 1
        data['teamOur']['goldNum'] = 492
        turn, cfg, nav, ledger = fortified_case(data)
        resume_daytime_jobs(turn, cfg, memory, nav, ledger, TOWERS, WALLS, set())
        self.assertEqual(ledger.work_jobs[1]['quantity'], 2)
        self.assertEqual(ledger.gold, 252)

    def test_four_carried_orders_use_four_distinct_rear_positions(self):
        data = self.data(gold=12)
        data['teamOur']['roles'][1]['backpack'] = ['BossRobotSummonOrder'] * 4
        memory, sites = Memory(), set()
        for _ in range(4):
            turn, cfg, nav, ledger = fortified_case(data)
            self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, TOWERS, WALLS,
                                              excluded={2}))
            action = ledger.commands['1']
            self.assertEqual(action['action'], 'use')
            site = pos(action['targetPos'][0])
            self.assertNotIn(site, sites)
            self.assertTrue(rear_approach(turn, enemy_base(turn), site))
            sites.add(site)
            data['teamOur']['roles'][1]['backpack'].pop()
            data['lastRoundRoleActionResults'] = {'1': True}
            data['roundNo'] += 1
        self.assertEqual(memory.robot_summon_state['count'], 4)
        self.assertEqual(memory.robot_summon_state['positions'], sites)


class RearAssaultTests(unittest.TestCase):
    def test_rear_deployment_precedes_a_weaker_front_wall(self):
        data = enemy_ring(placement_payload(), weak=(8, 4), weak_level=1, weak_hp=1)
        turn, _, _, ledger = setup_case(data, layout_mode='explicit')
        plan = weakest_approach(turn, ledger, prefer_rear=True)
        self.assertTrue(rear_approach(turn, enemy_base(turn), plan.entry))
        self.assertNotEqual(plan.wall.pos, (8, 4))
        site = choose_summon_position(turn, ledger)
        self.assertTrue(rear_approach(turn, enemy_base(turn), site))
        self.assertTrue(turn.summon_position_legal(site))

    def test_rear_selection_preserves_gap_level_and_health_priority(self):
        for gap in (None, (13, 3)):
            with self.subTest(gap=gap):
                data = enemy_ring(placement_payload(), gap=gap, weak=(13, 4),
                                  weak_level=1, weak_hp=1000)
                wall = next(r for r in data['teamEnemy']['roles']
                            if r['roleType'] == 'wall' and pos(r['pos']) == (13, 5))
                wall.update(level=1, health=50)
                turn, _, _, ledger = setup_case(data, layout_mode='explicit')
                plan = weakest_approach(turn, ledger, prefer_rear=True)
                self.assertEqual(plan.entry, gap or (13, 5))

    def test_rear_orientation_mirrors_both_axes(self):
        for flip_x in (False, True):
            for flip_y in (False, True):
                with self.subTest(flip_x=flip_x, flip_y=flip_y):
                    data = enemy_ring(placement_payload())
                    for team in ('teamOur', 'teamEnemy'):
                        for role in data[team]['roles']:
                            x, y = pos(role['pos'])
                            station = role['roleType'] == 'station'
                            role['pos'] = dict(x=(13 if station else 14)-x if flip_x else x,
                                               y=(15 if station else 14)-y if flip_y else y)
                    turn, _, _, ledger = setup_case(data, layout_mode='explicit')
                    site = choose_summon_position(turn, ledger)
                    self.assertIsNotNone(site)
                    self.assertTrue(rear_approach(turn, enemy_base(turn), site))
                    self.assertTrue(turn.summon_position_legal(site))

    def test_no_legal_rear_position_falls_back_to_another_entry(self):
        data = enemy_ring(placement_payload())
        initial, _, _, _ = setup_case(data, layout_mode='explicit')
        base = enemy_base(initial)
        data['mapInfo']['zones'] = [dict(neutralType='vendor', pos=dict(x=x, y=y))
            for x in range(initial.width) for y in range(initial.height)
            if rear_approach(initial, base, (x, y)) and initial.summon_position_legal((x, y))]
        turn, _, _, ledger = setup_case(data, layout_mode='explicit')
        site = choose_summon_position(turn, ledger)
        self.assertIsNotNone(site)
        self.assertFalse(rear_approach(turn, enemy_base(turn), site))
        self.assertTrue(turn.summon_position_legal(site))

    def test_rear_robot_stays_behind_then_attacks_observed_base(self):
        data = enemy_ring(assault_payload(robot=controlled_robot(x=14, y=4)),
                          weak=(13, 4), weak_level=2, weak_hp=1500)
        front_wall = next(r for r in data['teamEnemy']['roles']
                          if r['roleType'] == 'wall' and pos(r['pos']) == (8, 4))
        front_wall.update(level=1, health=1)
        memory = RobotAssaultMemory()
        turn, _, nav, ledger = setup_case(data, layout_mode='explicit')
        act_robots(turn, memory, nav, ledger)
        self.assertEqual(ledger.commands['30000'], command('attack', (13, 4)))
        followup = copy.deepcopy(data)
        followup['roundNo'] += 1
        followup['teamEnemy']['roles'] = [r for r in followup['teamEnemy']['roles']
            if r['roleType'] != 'wall' or pos(r['pos']) != (13, 4)]
        turn, _, nav, ledger = setup_case(followup, layout_mode='explicit')
        act_robots(turn, memory, nav, ledger)
        action = ledger.commands['30000']
        self.assertEqual(action['action'], 'attack')
        self.assertIn(pos(action['targetPos'][0]), enemy_base(turn).cells)
        self.assertEqual(memory.objectives[30000].phase, 'base')

    def test_expired_budget_stops_rear_search(self):
        turn, _, _, ledger = setup_case(enemy_ring(placement_payload()), layout_mode='explicit')
        with self.assertRaises(DeadlineExceeded):
            choose_summon_position(turn, ledger, deadline=0)


if __name__ == '__main__':
    unittest.main()
