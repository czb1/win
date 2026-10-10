"""Focused job-state transitions; no match simulation or economy benchmark."""
import copy
import unittest
from collections import deque
from unittest.mock import patch

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import (build, buy_supply, supplies, use_inventory,
                          dusk_resources, batch_sale_ready)
from agent.intelligence import Memory
from agent.mining import mine, earn, spare_mine
from agent.worker_jobs import resume_daytime_jobs, save_daytime_jobs
from agent.wall_watch import prepare_watch
from agent.navigation import layout
import test_daytime_worker_idle


class DaytimeTaskContinuityTests(unittest.TestCase):
    def case(self, tick=10, backpack=()):
        # Upgrade jobs are available from day two; later absolute rounds stay intact.
        round_no = 130 + tick if tick < 70 else tick
        p = payload(round_no, [unit(13, 'station', 0, 12, level=3, health=1500),
                           unit(1, 'worker', 4, 8, health=220, backpack=list(backpack)),
                           unit(20, 'rocket', 3, 11), unit(21, 'rocket', 10, 11)])
        p['teamOur']['goldNum'] = 1000
        p['mapInfo']['zones'] = [
            {'neutralType': 'copper', 'pos': {'x': 8, 'y': 8}, 'remain': 10},
            {'neutralType': 'vendor', 'pos': {'x': 8, 'y': 13}},
            {'neutralType': 'weaponShop', 'pos': {'x': 8, 'y': 5}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 5}, {'name': 'gold', 'price': 100}]
        p['weaponShopList'] = [{'name': 'WeaponUpgradeVoucher1', 'price': 20},
                              {'name': 'WeaponUpgradeVoucher2', 'price': 30}]
        return p

    def setup(self, p):
        return setup_case(p, layout_mode='explicit', loadout=['rocket', 'rocket'],
                          weapon_cells=[[3, 11], [10, 11]], wall_cells=[[7, 8], [2, 8]])

    def next_state(self, p, mem, ledger):
        save_daytime_jobs(ledger.turn, mem, ledger)
        p = copy.deepcopy(p)
        action = ledger.commands['1']
        self.assertEqual(action['action'], 'move')
        p['teamOur']['roles'][1]['pos'] = action['targetPos'][0]
        p['roundNo'] += 1
        return p

    def resume(self, p, mem, returning=()):
        t, c, n, l = self.setup(p)
        resume_daytime_jobs(t, c, mem, n, l, list(l.tower_cells), list(l.wall_cells), set(returning))
        return t, c, n, l

    def test_income_job_keeps_mine_when_a_better_mine_appears(self):
        p = self.case()
        t, c, n, l = self.setup(p)
        mem = Memory(preparation_tick=40)
        self.assertTrue(mine(t, c, mem, n, l, t.workers[0], deadline=40, target_only=(8, 8)))
        p = self.next_state(p, mem, l)
        p['mapInfo']['zones'].append({'neutralType': 'gold', 'pos': {'x': 4, 'y': 6}, 'remain': 10})
        t, c, n, l = self.resume(p, mem)
        self.assertEqual(l.mine_claims[1], (8, 8))
        self.assertIn(1, l.used)

    def test_material_batch_releases_for_construction_at_goal(self):
        p = self.case(backpack=['stone'] * 10)
        mem = Memory(daytime_jobs={1: dict(kind='mine', target=(8, 8), ore='stone',
                                         want_stone=True, stockpile=False, deadline=None, stone_goal=10)})
        p['mapInfo']['zones'][0]['neutralType'] = 'stone'
        _, _, _, l = self.resume(p, mem)
        self.assertNotIn(1, l.used)
        self.assertNotIn(1, mem.daytime_jobs)

    def test_buy_keeps_item_shop_and_quantity_through_walk(self):
        p = self.case()
        t, c, n, l = self.setup(p)
        mem = Memory()
        plan = supplies(t, c, mem, n, l, t.workers[0], bulk=True)
        self.assertEqual(plan[0], 'WeaponUpgradeVoucher1')
        self.assertTrue(buy_supply(t, l, t.workers[0], plan))
        p = self.next_state(p, mem, l)
        # The other weapon now wants a different tier. Do not divert this trip.
        p['teamOur']['roles'][2]['level'] = 2
        p['mapInfo']['zones'].append({'neutralType': 'weaponShop', 'pos': {'x': 2, 'y': 5}})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.work_jobs[1]['name'], 'WeaponUpgradeVoucher1')
        self.assertEqual(l.work_jobs[1]['target'], (8, 5))
        self.assertLessEqual(l.work_jobs[1]['quantity'], plan[2])

    def test_delivery_does_not_switch_to_nearer_target_at_sixty(self):
        p = self.case(59, ['WeaponUpgradeVoucher1'])
        t, c, n, l = self.setup(p)
        mem = Memory()
        self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=mem, target_only=(10, 11)))
        p = self.next_state(p, mem, l)
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.work_jobs[1]['target'], (10, 11))
        self.assertEqual(l.work_jobs[1]['kind'], 'use')

    def test_build_target_survives_builder_reallocation(self):
        p = self.case(backpack=['stone'] * 10)
        t, c, n, l = self.setup(p)
        mem = Memory()
        self.assertTrue(build(t, c, mem, n, l, t.workers[0], [(7, 8)], lambda _: 'wall'))
        p = self.next_state(p, mem, l)
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.build_claims[(7, 8)], (1, 'wall'))

    def test_buyer_reserves_before_builder_spends_the_same_gun_budget(self):
        p = self.case()
        p['teamOur']['roles'].pop(2)  # One observed gun, one missing gun.
        p['teamOur']['roles'][1]['pos'] = {'x': 4, 'y': 10}
        p['teamOur']['roles'].append(unit(2, 'worker', 4, 5, health=220))
        p['teamOur']['goldNum'] = 45  # Exactly one gun (25) + voucher (20).
        mem = Memory(daytime_jobs={
            1: dict(kind='build', target=(3, 11), name='rocket'),
            2: dict(kind='buy', target=(8, 5), name='WeaponUpgradeVoucher1', quantity=1)})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.commands['1'], command('build', (3, 11), name='rocket'))
        self.assertEqual(l.work_jobs[2]['kind'], 'buy')
        self.assertEqual(l.gold, 0)

    def test_assigned_breach_builder_keeps_its_target(self):
        p = self.case(backpack=['stone'])
        p['teamOur']['roles'].extend([unit(30, 'wall', 7, 7, health=1000),
                                    unit(31, 'wall', 7, 9, health=1000)])
        mem = Memory(wall_repair_worker=1, wall_hits={(7, 8): 1, (2, 8): 1},
                     daytime_jobs={1: dict(kind='build', target=(7, 8), name='wall')})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.build_claims[(7, 8)], (1, 'wall'))

    def test_finished_action_clears_its_job_and_stale_mine_ownership(self):
        p = self.case(backpack=['WeaponUpgradeVoucher1'])
        p['teamOur']['roles'][1]['pos'] = {'x': 9, 'y': 11}
        mem = Memory(mine_targets={1: (8, 8)}, daytime_jobs={
            1: dict(kind='use', target=(10, 11), name='WeaponUpgradeVoucher1')})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.commands['1'], command('use', (10, 11), name='WeaponUpgradeVoucher1'))
        save_daytime_jobs(l.turn, mem, l)
        self.assertFalse(mem.daytime_jobs)
        self.assertFalse(mem.mine_targets)

    def test_sale_continues_to_same_vendor_instead_of_buying(self):
        p = self.case(backpack=['copper'] * 40)
        t, c, n, l = self.setup(p)
        mem = Memory()
        self.assertTrue(earn(t, c, mem, n, l, t.workers[0], force_sale=True))
        p = self.next_state(p, mem, l)
        p['mapInfo']['zones'].append({'neutralType': 'vendor', 'pos': {'x': 2, 'y': 5}})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.work_jobs[1], dict(kind='sell', target=(8, 13)))

    def test_short_stockpile_walk_collects_before_new_assignment(self):
        p = self.case()
        p['mapInfo']['zones'][0]['pos'] = {'x': 6, 'y': 8}
        t, c, n, l = self.setup(p)
        mem = Memory()
        self.assertTrue(spare_mine(t, c, mem, n, l, t.workers[0]))
        p = self.next_state(p, mem, l)
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.commands['1'], command('collect', (6, 8)))
        save_daytime_jobs(l.turn, mem, l)
        self.assertFalse(mem.daytime_jobs)  # One pickup finishes a short trip.

    def test_failed_exhausted_changed_and_unreachable_targets_release(self):
        for condition in ('failed', 'exhausted', 'changed', 'unreachable'):
            with self.subTest(condition=condition):
                p = self.case()
                mem = Memory(daytime_jobs={1: dict(kind='mine', target=(8, 8), ore='copper',
                                                 want_stone=False, stockpile=False, deadline=40, stone_goal=None)})
                if condition == 'failed':
                    mem.collect_failures[(8, 8)] = 20
                elif condition == 'exhausted':
                    p['mapInfo']['zones'][0]['remain'] = 0
                elif condition == 'changed':
                    p['mapInfo']['zones'][0]['neutralType'] = 'stone'
                else:
                    p['teamEnemy']['roles'] = [unit(50+i, 'wall', x, y) for i, (x, y) in enumerate(
                        ((7, 7), (8, 7), (9, 7), (7, 8), (9, 8), (7, 9), (8, 9), (9, 9)))]
                _, _, _, l = self.resume(p, mem)
                self.assertNotIn(1, l.used)
                self.assertNotIn(1, mem.daytime_jobs)

    def test_medicine_interrupts_active_job(self):
        p = self.case(backpack=['Medicine'])
        p['teamOur']['roles'][1]['health'] = 100
        mem = Memory(daytime_jobs={1: dict(kind='build', target=(7, 8), name='wall')})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.commands['1'], command('use', name='Medicine'))
        save_daytime_jobs(l.turn, mem, l)
        self.assertFalse(mem.daytime_jobs)

    def test_critical_station_preempts_an_ordinary_delivery(self):
        p = self.case(backpack=['StationUpgradeVoucher1', 'WeaponUpgradeVoucher1'])
        p['teamOur']['roles'][0].update(level=1, health=300)
        mem = Memory(daytime_jobs={1: dict(kind='use', target=(10, 11), name='WeaponUpgradeVoucher1')})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.work_jobs[1]['target'], (0, 12))
        self.assertEqual(l.work_jobs[1]['name'], 'StationUpgradeVoucher1')

    def test_observed_breach_borrows_one_worker_from_income(self):
        p = self.case(backpack=['stone'])
        p['teamOur']['roles'].extend([unit(30, 'wall', 7, 7, health=1000),
                                    unit(31, 'wall', 7, 9, health=1000)])
        mem = Memory(wall_hits={(7, 8): 1}, daytime_jobs={1: dict(
            kind='mine', target=(8, 8), ore='copper', want_stone=False,
            stockpile=False, deadline=40, stone_goal=None)})
        _, _, _, l = self.resume(p, mem)
        self.assertEqual(l.build_claims[(7, 8)], (1, 'wall'))
        self.assertEqual(mem.wall_repair_worker, 1)

    def test_completed_upgrade_purchase_and_material_changes_release(self):
        for kind in ('use', 'buy', 'build'):
            with self.subTest(kind=kind):
                p = self.case()
                job = dict(kind=kind, target=(10, 11), name='WeaponUpgradeVoucher1')
                if kind == 'use':
                    p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
                    p['teamOur']['roles'][3]['level'] = 2
                elif kind == 'buy':
                    job.update(target=(8, 5), quantity=1)
                    p['teamOur']['goldNum'] = 0
                else:
                    job.update(target=(7, 8), name='wall')
                mem = Memory(daytime_jobs={1: job})
                _, _, _, l = self.resume(p, mem)
                self.assertNotIn(1, l.used)
                self.assertFalse(mem.daytime_jobs)

    def test_purchase_loop_uses_the_same_destination_cooldown(self):
        p = self.case()
        t, c, _, _ = self.setup(p)
        mem = Memory(day=t.day, last_round=t.round-1,
                     last_commands={'1': command('move', (4, 8))},
                     daytime_jobs={1: dict(kind='buy', target=(8, 5), name='WeaponUpgradeVoucher1', quantity=1)})
        mem.movement.trails[1] = deque([(4, 8), (5, 8), (4, 8), (5, 8), (4, 8), (5, 8), (4, 8)], maxlen=8)
        mem.observe(t, c)
        self.assertTrue(mem.movement.avoids(1, (8, 5)))
        _, _, _, l = self.resume(p, mem)
        self.assertNotIn(1, l.used)
        self.assertFalse(mem.daytime_jobs)

    def test_watcher_does_not_sell_one_new_ore_that_cannot_fund_a_pack(self):
        p = test_daytime_worker_idle.DaytimeWorkerIdleTests().watch_case()
        p['teamOur']['roles'][2]['backpack'] = ['copper']
        p['mapInfo']['zones'].append({'neutralType': 'vendor', 'pos': {'x': 10, 'y': 10}})
        t, c, n, l = setup_case(p)
        mem = Memory(wall_watch_id=2, sold_workers={2})
        prepare_watch(t, c, mem, n, l, t.workers[1], layout(t, c)[1])
        self.assertNotIn(2, mem.sale_workers)

    def test_watch_purchase_keeps_shop_until_route_enters_cooldown(self):
        p = test_daytime_worker_idle.DaytimeWorkerIdleTests().watch_case()
        p['teamOur']['goldNum'] = 75
        p['mapInfo']['zones'] = [z for z in p['mapInfo']['zones'] if z['pos'] != {'x': 6, 'y': 9}]
        p['mapInfo']['zones'].append({'neutralType': 'weaponShop', 'pos': {'x': 4, 'y': 9}})
        for cooldown in (False, True):
            with self.subTest(cooldown=cooldown):
                t, c, n, l = setup_case(p)
                mem = Memory(wall_watch_id=2, daytime_jobs={2: dict(
                    kind='buy', target=(4, 9), name='WallFixer', quantity=3)})
                if cooldown:
                    mem.movement.targets[2, (4, 9)] = t.round + 8
                prepare_watch(t, c, mem, n, l, t.workers[1], layout(t, c)[1])
                if cooldown:
                    self.assertEqual(l.commands['2']['action'], 'buy')
                else:
                    self.assertEqual(l.work_jobs[2]['target'], (4, 9))

    def test_recall_and_deadline_release_without_remote_work(self):
        for recalled, tick in ((True, 10), (False, 69)):
            p = self.case(tick, ['stone'] * 10)
            mem = Memory(daytime_jobs={1: dict(kind='build', target=(7, 8), name='wall')})
            _, _, _, l = self.resume(p, mem, {1} if recalled else ())
            self.assertNotIn(1, l.used)
            self.assertFalse(mem.daytime_jobs)

    def test_small_surplus_does_not_reopen_distant_vendor_trip(self):
        for count, expected in ((1, False), (2, False), (40, True)):
            p = self.case(45, ['copper'] * count)
            p['weaponShopList'] = []
            t, c, n, l = self.setup(p)
            mem = Memory(sold_workers={1}, preparation_tick=40)
            self.assertEqual(batch_sale_ready(t, c, mem, n, l, t.workers[0]), expected)
            dusk_resources(t, c, mem, n, l, list(l.tower_cells))
            self.assertEqual('1' in l.commands, expected)

    def test_small_load_can_sell_adjacent_or_for_first_preparation_deadline(self):
        for adjacent in (True, False):
            p = self.case(40, ['copper'])
            if adjacent:
                p['teamOur']['roles'][1]['pos'] = {'x': 7, 'y': 13}
            t, c, n, l = self.setup(p)
            mem = Memory(preparation_tick=40)
            self.assertTrue(batch_sale_ready(t, c, mem, n, l, t.workers[0]))

    def test_death_and_night_discard_jobs(self):
        p = self.case(70)
        t, c, n, l = self.setup(p)
        mem = Memory(daytime_jobs={1: dict(kind='sell', target=(8, 13))})
        resume_daytime_jobs(t, c, mem, n, l, list(l.tower_cells), [], set())
        self.assertFalse(l.commands)
        save_daytime_jobs(t, mem, l)
        self.assertFalse(mem.daytime_jobs)
        p = self.case()
        p['teamOur']['roles'][1]['health'] = 0
        t, _, _, l = self.setup(p)
        mem.daytime_jobs[1] = dict(kind='sell', target=(8, 13))
        save_daytime_jobs(t, mem, l)
        self.assertFalse(mem.daytime_jobs)

    def test_agent_resumes_before_dusk_robot_and_worker_dispatch(self):
        p = self.case(45)
        cfg = Config(layout_mode='explicit', loadout=['rocket', 'rocket'],
                     weapon_cells=[[3, 11], [10, 11]], wall_cells=[], llm_enabled=False)
        agent = Agent(cfg)
        agent.decide(p)
        mem = next(iter(agent.sessions.values()))
        mem.daytime_jobs = {1: dict(kind='use', target=(10, 11), name='WeaponUpgradeVoucher1')}
        p['roundNo'] += 1
        p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        with patch('agent.brain.dusk_resources') as dusk, patch('agent.brain.summon_best_robot') as boss:
            dusk.side_effect = lambda *args: self.assertIn(1, args[4].used)
            boss.side_effect = lambda *args, **kw: self.assertIn(1, args[4].used)
            result = agent.decide(p)
        self.assertEqual(result['roleCommandMap']['1']['action'], 'move')
        self.assertEqual(mem.daytime_jobs[1]['target'], (10, 11))
        self.assertEqual(agent.decide(copy.deepcopy(p)), result)
