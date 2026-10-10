"""Daytime lock failures, local fallback and exact return-budget boundaries."""
import unittest
from unittest.mock import patch

from test_agent import payload, unit, setup_case
import test_wall_watch
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.daytime import finish_daytime_work
from agent.economy import worker
from agent.intelligence import Memory
from agent.mining import earn, spare_mine, reserve_watch_space
from agent.navigation import layout
from agent.wall_watch import prepare_watch, watch_route, geometry
from agent.model import neighbours


class DaytimeWorkerIdleTests(unittest.TestCase):
    def watch_case(self, tick=40, packs=0, mirror=False):
        p = test_wall_watch.WallWatchTests().case(tick=tick, packs=packs, damaged=False)
        p['robot']['roles'] = []
        p['teamOur']['goldNum'] = 0
        p['vendorShopList'] = [{'name': 'copper', 'price': 5}]
        p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 6, 'y': 9}})
        if mirror:
            for role in p['teamOur']['roles']:
                role['pos']['x'] = 14 - role['pos']['x'] - (role['roleType'] == 'station')
            for zone in p['mapInfo']['zones']:
                zone['pos']['x'] = 14 - zone['pos']['x']
        return p

    def mine_case(self, tick=40, backpack=()):
        p = payload(tick, [unit(13, 'station', 5, 11, level=3),
                           unit(1, 'worker', 4, 11, health=220, backpack=list(backpack))])
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 3, 'y': 11}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 5}]
        return p

    def test_sold_worker_collects_across_forty_boundary_but_respects_return_margin(self):
        for tick, expected in ((39, True), (40, True), (64, True), (65, False), (69, False)):
            with self.subTest(tick=tick):
                t, cfg, nav, ledger = setup_case(self.mine_case(tick), layout_mode='explicit', return_margin=5)
                result = earn(t, cfg, Memory(sold_workers={1}), nav, ledger, t.workers[0])
                self.assertEqual(bool(result), expected)
                self.assertEqual(ledger.commands.get('1'), command('collect', (3, 11)) if expected else None)

    def test_stockpile_uses_assigned_post_not_nearest_base(self):
        t, cfg, nav, ledger = setup_case(self.mine_case(60), layout_mode='explicit', return_margin=5)
        ledger.operator_posts[1] = (12, 2)
        self.assertFalse(spare_mine(t, cfg, Memory(), nav, ledger, t.workers[0]))
        self.assertFalse(ledger.commands)

    def test_forced_failed_sale_never_becomes_new_mining(self):
        t, cfg, nav, ledger = setup_case(self.mine_case(backpack=['copper']), layout_mode='explicit')
        self.assertFalse(earn(t, cfg, Memory(sold_workers={1}), nav, ledger, t.workers[0], force_sale=True))
        self.assertFalse(ledger.commands)

    def test_preparation_worker_without_executable_job_keeps_working_after_forty(self):
        t, cfg, nav, ledger = setup_case(self.mine_case(45), layout_mode='explicit')
        mem = Memory(sold_workers={1}, preparation_workers={1}, preparation_tick=5)
        worker(t, cfg, mem, nav, ledger, t.workers[0], [], [], False, deadline=5)
        self.assertEqual(ledger.commands['1'], command('collect', (3, 11)))

    def test_empty_unfunded_watcher_at_home_collects_in_both_directions(self):
        for mirror in (False, True):
            p = self.watch_case(mirror=mirror)
            result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
            self.assertEqual(result['2'], command('collect', (8 if mirror else 6, 9)))

    def test_blocked_watcher_keeps_assignment_and_works_without_wandering(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.watch_case(tick=5)
        with patch('agent.wall_watch.watch_route', return_value=None):
            first = agent.decide(p)['roleCommandMap']
            p['roundNo'] += 1
            p['teamOur']['roles'][2]['backpack'] = ['copper']
            second = agent.decide(p)['roleCommandMap']
        self.assertEqual(first['2'], command('collect', (6, 9)))
        self.assertEqual(second['2'], command('collect', (6, 9)))
        self.assertEqual(next(iter(agent.sessions.values())).wall_watch_id, 2)

    def test_rejected_restock_does_not_reserve_money_and_allows_local_work(self):
        p = self.watch_case()
        p['teamOur']['goldNum'] = 75
        p['teamOur']['roles'][2]['backPackCapability'] = 4
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=2, gunner_post=(4, 11))
        add = ledger.add
        with patch.object(ledger, 'add', side_effect=lambda uid, action:
                          False if action['action'] == 'buy' else add(uid, action)):
            locked, reserved = prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
        self.assertTrue(locked)
        self.assertEqual(reserved, 0)
        self.assertEqual(ledger.gold, 75)
        finish_daytime_work(t, cfg, mem, nav, ledger, {2})
        self.assertEqual(ledger.commands['2'], command('collect', (6, 9)))

    def test_pack_slots_follow_executable_reservation_not_raw_gold(self):
        for shop_available, expected in ((True, 1), (False, 5)):
            p = self.watch_case(tick=5)
            p['teamOur']['goldNum'] = 75
            p['teamOur']['roles'][2]['backPackCapability'] = 5
            if not shop_available:
                p['mapInfo']['zones'] = [z for z in p['mapInfo']['zones'] if z['neutralType'] != 'weaponShop']
            t, cfg, nav, ledger = setup_case(p)
            mem = Memory(wall_watch_id=2)
            locked, reserved = prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
            self.assertFalse(locked)
            ledger.gold -= reserved
            self.assertEqual(reserve_watch_space(t, mem, t.workers[1], ledger).space, expected)

    def test_stocked_watch_standby_is_preserved_and_logged(self):
        t, cfg, nav, ledger = setup_case(self.watch_case(packs=3))
        mem = Memory(wall_watch_id=2)
        locked, _ = prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
        self.assertTrue(locked)
        with self.assertLogs('agent.daytime', level='INFO') as captured:
            finish_daytime_work(t, cfg, mem, nav, ledger, {2})
        self.assertNotIn('2', ledger.commands)
        self.assertTrue(any('stocked_watch_standby' in line for line in captured.output))

    def test_empty_watch_does_not_walk_outside_or_mine_at_last_tick(self):
        for tick in (40, 69):
            p = self.watch_case(tick=tick)
            p['mapInfo']['zones'] = [z for z in p['mapInfo']['zones'] if z['neutralType'] != 'copper']
            p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 9, 'y': 10}})
            result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
            self.assertNotIn('2', result)

    def test_local_paid_use_precedes_stockpile_for_locked_watcher(self):
        p = self.watch_case()
        p['teamOur']['roles'][2]['backpack'] = ['WallUpgradeVoucher1']
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=2)
        prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
        finish_daytime_work(t, cfg, mem, nav, ledger, {2})
        self.assertEqual(ledger.commands['2']['action'], 'use')
        self.assertEqual(ledger.commands['2']['name'], 'WallUpgradeVoucher1')

    def test_blocked_operator_only_collects_adjacent_without_releasing_return(self):
        t, cfg, nav, ledger = setup_case(self.mine_case(), layout_mode='explicit')
        ledger.operator_posts[1] = (14, 14)
        ledger.reserved.add((14, 14))
        mem = Memory(return_targets={1: 20}, return_posts={1: (14, 14)})
        finish_daytime_work(t, cfg, mem, nav, ledger, {1})
        self.assertEqual(ledger.commands['1'], command('collect', (3, 11)))
        self.assertEqual(mem.return_targets, {1: 20})
        p = self.mine_case()
        p['mapInfo']['zones'][0]['pos'] = {'x': 2, 'y': 11}
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit')
        ledger.operator_posts[1] = (14, 14)
        ledger.reserved.add((14, 14))
        finish_daytime_work(t, cfg, mem, nav, ledger, {1})
        self.assertFalse(ledger.commands)

    def test_far_robot_does_not_disable_daytime_fallback_but_near_robot_does(self):
        for pos, expected in (((14, 0), True), ((3, 10), False)):
            p = self.mine_case()
            p['robot']['roles'] = [unit(90, 'smallRobot', *pos, attackRange=1, targetTeam='challenger')]
            t, cfg, nav, ledger = setup_case(p, layout_mode='explicit')
            self.assertEqual(spare_mine(t, cfg, Memory(), nav, ledger, t.workers[0]), expected)

    def test_only_current_shared_post_blocks_daytime_watch_routes(self):
        p = self.watch_case()
        p['teamOur']['roles'][2]['pos'] = {'x': 6, 'y': 10}
        t, cfg, nav, ledger = setup_case(p)
        sites = layout(t, cfg)[1]
        inside, _ = geometry(t, sites)
        goals = {p for wall in sites for p in neighbours(wall)} & inside
        ledger.reserved.update(goals - {(7, 9)})
        mem = Memory(gunner_post=(7, 9))
        ledger.daytime_gunner_post = None
        self.assertEqual(watch_route(t, nav, ledger, mem, t.workers[1], sites), (1, (7, 9)))
        ledger.daytime_gunner_post = (7, 9)
        self.assertIsNone(watch_route(t, nav, ledger, mem, t.workers[1], sites))

    def test_empty_watch_short_walk_requires_complete_inside_return_budget(self):
        for tick, expected in ((40, True), (69, False)):
            p = self.watch_case(tick=tick)
            p['mapInfo']['zones'] = [z for z in p['mapInfo']['zones'] if z['neutralType'] != 'copper']
            p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 4, 'y': 9}})
            t, cfg, nav, ledger = setup_case(p)
            mem = Memory(wall_watch_id=2)
            ledger.daytime_waits[2] = 'empty_watch'
            finish_daytime_work(t, cfg, mem, nav, ledger, {2})
            self.assertEqual('2' in ledger.commands, expected)
            if expected:
                self.assertEqual(ledger.commands['2']['action'], 'move')
                target = ledger.commands['2']['targetPos'][0]
                self.assertIn((target['x'], target['y']), geometry(t, ledger.wall_cells)[0])

    def test_empty_watch_short_trip_does_not_alternate_mining_and_recall_moves(self):
        p = self.watch_case()
        p['mapInfo']['zones'] = [z for z in p['mapInfo']['zones'] if z['neutralType'] != 'copper']
        p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 4, 'y': 9}})
        agent = Agent(Config(llm_enabled=False, return_margin=5))
        actions = []
        for tick in (40, 41, 42):
            p['roundNo'] = 3 * 130 + tick
            cmd = agent.decide(p)['roleCommandMap']['2']
            actions.append(cmd['action'])
            if cmd['action'] == 'move':
                p['teamOur']['roles'][2]['pos'] = cmd['targetPos'][0]
        self.assertEqual(actions, ['move', 'move', 'collect'])
        # Once there is no budget for another collect, retain enough daylight
        # to move back inside instead of staying at the collection tile.
        p['teamOur']['roles'][2]['pos'] = {'x': 5, 'y': 10}
        p['roundNo'] = 3 * 130 + 65
        cmd = agent.decide(p)['roleCommandMap']['2']
        self.assertEqual(cmd['action'], 'move')
        self.assertNotIn(2, next(iter(agent.sessions.values())).mine_targets)

    def test_fallback_keeps_existing_actions_and_does_nothing_at_night(self):
        t, cfg, nav, ledger = setup_case(self.mine_case(), layout_mode='explicit')
        ledger.add(1, command('move', (3, 10)))
        finish_daytime_work(t, cfg, Memory(), nav, ledger, set())
        self.assertEqual(ledger.commands, {'1': command('move', (3, 10))})
        t, cfg, nav, ledger = setup_case(self.mine_case(70), layout_mode='explicit')
        finish_daytime_work(t, cfg, Memory(), nav, ledger, set())
        self.assertFalse(ledger.commands)


if __name__ == '__main__':
    unittest.main()
