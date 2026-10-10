"""Focused regressions for carried vouchers, wall work and daylight cutoffs."""
import json
from pathlib import Path
import tempfile
import unittest

from test_agent import payload, setup_case, unit
import test_daytime_task_continuity
import test_wall_upgrade_delivery
from agent.commands import command
from agent.config import Config
from agent.economy import batch_sale_ready, use_inventory, workers
from agent.economy_plan import preparation_start
from agent.intelligence import Memory
from agent.navigation import layout
from agent.wall_watch import prepare_watch
from agent.worker_jobs import resume_daytime_jobs


class WallDaylightPriorityTests(unittest.TestCase):
    def test_default_cutoff_is_five_turns_earlier_and_zero_margin_loads(self):
        default = Config.load(Path(__file__).resolve().parents[1] / 'config/default.json')
        self.assertEqual((Config().economy_rounds, Config().return_margin), (35, 0))
        self.assertEqual((default.economy_rounds, default.return_margin), (35, 0))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for margin in (0, 5):
                path.write_text(json.dumps({'return_margin': margin}))
                self.assertEqual(Config.load(path).return_margin, margin)
            for margin in (-1, True, 0.5, '0'):
                path.write_text(json.dumps({'return_margin': margin}))
                with self.assertRaises(ValueError):
                    Config.load(path)

    def test_paid_v2_is_used_with_new_level_one_wall_and_missing_wall(self):
        case = test_wall_upgrade_delivery.WallUpgradeDeliveryTests()
        for tick in (10, 65, 450):
            for missing in (False, True):
                with self.subTest(tick=tick, missing=missing):
                    p = case.case(tick)
                    p['teamOur']['roles'][0]['backpack'] = ['WallUpgradeVoucher2']
                    p['teamOur']['roles'][3]['level'] = 1
                    if missing:
                        p['teamOur']['roles'].pop(3)
                    t, c, nav, ledger = case.setup(p)
                    mem = Memory()
                    mem.observe(t, c)
                    self.assertTrue(use_inventory(t, nav, ledger, t.workers[0], mem=mem))
                    self.assertEqual(ledger.commands['1'], command('use', (5, 4), name='WallUpgradeVoucher2'))

    def test_paid_v2_never_applies_to_level_one_or_maxed_wall(self):
        case = test_wall_upgrade_delivery.WallUpgradeDeliveryTests()
        for level in (1, 3):
            p = case.case()
            p['teamOur']['roles'][0]['backpack'] = ['WallUpgradeVoucher2']
            for wall in p['teamOur']['roles'][3:]:
                wall['level'] = level
            t, _, nav, ledger = case.setup(p)
            self.assertFalse(use_inventory(t, nav, ledger, t.workers[0], mem=Memory()))

    def test_unreachable_front_does_not_block_paid_adjacent_flank(self):
        case = test_wall_upgrade_delivery.WallUpgradeDeliveryTests()
        p = case.case(450)
        p['teamOur']['roles'][0]['backpack'] = ['WallUpgradeVoucher2']
        p['mapInfo']['zones'] += [{'neutralType': 'stone', 'pos': {'x': 6, 'y': y}}
                                 for y in range(15) if y != 6]
        t, c, nav, ledger = case.setup(p)
        self.assertTrue(use_inventory(t, nav, ledger, t.workers[0], mem=Memory()))
        self.assertEqual(ledger.commands['1'], command('use', (5, 4), name='WallUpgradeVoucher2'))

    def test_carried_voucher_interrupts_pending_purchase_or_income_before_cutoff(self):
        case = test_wall_upgrade_delivery.WallUpgradeDeliveryTests()
        for kind in ('buy', 'mine', 'spare'):
            p = case.case(10)
            p['teamOur']['roles'][0]['backpack'] = ['WallUpgradeVoucher2']
            p['teamOur']['roles'][3]['level'] = 1
            p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 6, 'y': 3}})
            p['vendorShopList'] = [{'name': 'copper', 'price': 5}]
            t, cfg, nav, ledger = case.setup(p)
            job = (dict(kind='buy', target=(6, 6), name='WallUpgradeVoucher1', quantity=1)
                   if kind == 'buy' else dict(kind=kind, target=(6, 3), ore='copper',
                                             want_stone=False, stockpile=True, deadline=None))
            mem = Memory(daytime_jobs={1: job})
            resume_daytime_jobs(t, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
            self.assertEqual(ledger.commands['1'], command('use', (5, 4), name='WallUpgradeVoucher2'))
            self.assertNotIn(1, mem.daytime_jobs)

    def test_income_continuation_releases_at_configured_preparation_cutoff(self):
        case = test_daytime_task_continuity.DaytimeTaskContinuityTests()
        for kind in ('mine', 'spare'):
            for cutoff in (35, 40):
                for tick in (cutoff - 1, cutoff):
                    with self.subTest(kind=kind, cutoff=cutoff, tick=tick):
                        p = case.case(tick)
                        p['mapInfo']['zones'][0]['pos'] = {'x': 6, 'y': 8}
                        t, cfg, nav, ledger = setup_case(
                            p, layout_mode='explicit', loadout=['rocket', 'rocket'],
                            weapon_cells=[[3, 11], [10, 11]], wall_cells=[[7, 8], [2, 8]],
                            economy_rounds=cutoff)
                        job = dict(kind=kind, target=(6, 8), ore='copper', want_stone=False,
                                   stockpile=True, deadline=None, stone_goal=None)
                        mem = Memory(sold_workers={1}, daytime_jobs={1: job})
                        resume_daytime_jobs(t, cfg, mem, nav, ledger, ledger.tower_cells,
                                            ledger.wall_cells, set())
                        self.assertEqual(1 in ledger.used, tick < cutoff)
                        self.assertEqual(1 in mem.daytime_jobs, tick < cutoff)

    def test_released_income_worker_starts_upgrade_preparation_at_thirty_five(self):
        case = test_daytime_task_continuity.DaytimeTaskContinuityTests()
        p = case.case(35)
        t, cfg, nav, ledger = case.setup(p)
        mem = Memory(daytime_jobs={1: dict(kind='mine', target=(8, 8), ore='copper',
                                          want_stone=False, stockpile=True, deadline=None)})
        resume_daytime_jobs(t, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
        workers(t, cfg, mem, nav, ledger, list(ledger.tower_cells), list(ledger.wall_cells))
        self.assertEqual(ledger.work_jobs[1]['kind'], 'buy')
        self.assertEqual(ledger.work_jobs[1]['name'], 'WeaponUpgradeVoucher1')

    def test_completed_defences_keep_feasible_income_after_cutoff(self):
        case = test_daytime_task_continuity.DaytimeTaskContinuityTests()
        p = case.case(35)
        p['mapInfo']['zones'][0]['pos'] = {'x': 6, 'y': 8}
        for role in p['teamOur']['roles']:
            if role['roleType'] == 'rocket':
                role['level'] = 3
        p['teamOur']['roles'] += [unit(30, 'wall', 7, 8, level=3), unit(31, 'wall', 2, 8, level=3)]
        t, cfg, nav, ledger = case.setup(p)
        mem = Memory(sold_workers={1}, daytime_jobs={1: dict(kind='mine', target=(6, 8),
                     ore='copper', want_stone=False, stockpile=True, deadline=None, stone_goal=None)})
        resume_daytime_jobs(t, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
        self.assertEqual(ledger.work_jobs[1]['kind'], 'mine')
        self.assertEqual(ledger.work_jobs[1]['target'], (6, 8))

    def test_material_continuation_survives_income_cutoff(self):
        case = test_daytime_task_continuity.DaytimeTaskContinuityTests()
        p = case.case(35)
        p['mapInfo']['zones'][0]['neutralType'] = 'stone'
        t, cfg, nav, ledger = case.setup(p)
        mem = Memory(daytime_jobs={1: dict(kind='mine', target=(8, 8), ore='stone',
                                          want_stone=True, stockpile=False, deadline=None, stone_goal=2)})
        resume_daytime_jobs(t, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
        self.assertEqual(ledger.work_jobs[1]['target'], (8, 8))

    def test_route_estimate_can_start_preparation_before_thirty_five(self):
        case = test_wall_upgrade_delivery.WallUpgradeDeliveryTests()
        p = case.case(1)
        p['mapInfo']['zones'][0]['pos'] = {'x': 40, 'y': 31}
        p['mapInfo'].update(width=41, height=32)
        t, cfg, nav, _ = case.setup(p)
        cutoff = preparation_start(t, cfg, Memory(), nav, t.workers, [(4, 6)])
        self.assertLess(cutoff, 35)

    def watch_case(self, tick=42, vouchers=(), worker_pos=(26, 16)):
        p = payload(780 + tick, [unit(13, 'station', 30, 10, health=1500),
            unit(1, 'worker', *worker_pos, health=500,
                 backpack=['WallFixer'] * 12 + ['stone'] * 11 + list(vouchers)),
            unit(2, 'worker', 30, 13, health=500)])
        p['mapInfo'].update(width=41, height=32)
        p['teamOur']['goldNum'] = 882
        t, cfg, _, _ = setup_case(p)
        towers, walls = layout(t, cfg)
        p['teamOur']['roles'] += [unit(20+i, 'rocket', *s, level=3) for i, s in enumerate(towers)]
        p['teamOur']['roles'] += [unit(100+i, 'wall', *s, level=1 if s in ((31, 7), (33, 7)) else 2,
                                     health=1000 if s in ((31, 7), (33, 7)) else 1500)
                                 for i, s in enumerate(walls) if s not in ((33, 8), (33, 9))]
        p['mapInfo']['zones'] = [
            {'neutralType': 'vendor', 'pos': {'x': 20, 'y': 16}},
            {'neutralType': 'weaponShop', 'pos': {'x': 25, 'y': 20}}]
        p['vendorShopList'] = [{'name': 'stone', 'price': 2}]
        p['weaponShopList'] = [{'name': 'WallFixer', 'price': 10},
                              {'name': 'WallUpgradeVoucher1', 'price': 20},
                              {'name': 'WallUpgradeVoucher2', 'price': 30}]
        return p

    def test_stocked_watch_finishes_feasible_build_before_small_surplus_sale(self):
        p = self.watch_case()
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=1, sold_workers={1},
                     daytime_jobs={1: dict(kind='build', target=(33, 8), name='wall')})
        # Even when selling is due, the funded watch retains its wall journey.
        cfg.sell_batch = 9
        self.assertTrue(batch_sale_ready(t, cfg, mem, nav, ledger, t.workers[0]))
        locked, funds = prepare_watch(t, cfg, mem, nav, ledger, t.workers[0], ledger.wall_cells)
        self.assertTrue(locked)
        self.assertEqual(funds, 0)
        self.assertEqual(ledger.work_jobs[1]['kind'], 'build')
        self.assertEqual(ledger.work_jobs[1]['target'], (33, 8))
        self.assertNotIn(1, mem.sale_workers)

    def test_stocked_watch_delivers_paid_v2_before_optional_sale(self):
        p = self.watch_case(vouchers=['WallUpgradeVoucher2'] * 6)
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=1, sold_workers={1})
        prepare_watch(t, cfg, mem, nav, ledger, t.workers[0], ledger.wall_cells)
        self.assertEqual(ledger.work_jobs[1]['kind'], 'use')
        self.assertEqual(ledger.work_jobs[1]['name'], 'WallUpgradeVoucher2')
        self.assertNotIn(1, mem.sale_workers)

    def test_watch_uses_adjacent_paid_v2_on_final_daylight_turn(self):
        p = self.watch_case(tick=69, vouchers=['WallUpgradeVoucher2'], worker_pos=(29, 11))
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=1, gunner_post=(32, 9))
        locked, _ = prepare_watch(t, cfg, mem, nav, ledger, t.workers[0], ledger.wall_cells)
        self.assertTrue(locked)
        self.assertEqual(ledger.commands['1'], command('use', (28, 10), name='WallUpgradeVoucher2'))
        self.assertNotIn((32, 9), ledger.reserved)

    def test_watch_does_not_resume_build_that_cannot_finish_before_night(self):
        p = self.watch_case(tick=69)
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=1,
                     daytime_jobs={1: dict(kind='build', target=(33, 8), name='wall')})
        prepare_watch(t, cfg, mem, nav, ledger, t.workers[0], ledger.wall_cells)
        self.assertNotEqual(ledger.work_jobs.get(1, {}).get('kind'), 'build')
