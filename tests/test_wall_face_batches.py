"""Focused scheduler transitions for face ownership and economic handoff."""
import unittest
from test_agent import payload, unit, setup_case
from agent.intelligence import Memory
from agent.commands import command
from agent.worker_jobs import resume_daytime_jobs, save_daytime_jobs
from agent.wall_batches import face_sites, start_batch
from agent.economy import build, supplies


SITES = [(4, y) for y in range(4, 7)] + [(8, y) for y in range(4, 7)]


class WallFaceBatchTests(unittest.TestCase):
    def frame(self, bag=(), walls=(), at=(3, 4), tick=400, gold=0, zones=(), station_level=3, station_health=4500):
        p = payload(tick, [unit(1, 'worker', *at, health=500, backpack=list(bag)),
                          unit(13, 'station', 6, 6, health=station_health, level=station_level),
                          unit(20, 'rocket', 6, 4, health=2000, level=3)] + list(walls))
        p['teamOur']['goldNum'] = gold
        p['vendorShopList'] = [{'name': 'copper', 'price': 8}, {'name': 'iron', 'price': 5}]
        p['weaponShopList'] = [{'name': 'WallUpgradeVoucher1', 'price': 20},
                               {'name': 'WallUpgradeVoucher2', 'price': 30}]
        p['mapInfo']['zones'] = [{'neutralType': k, 'pos': {'x': x, 'y': y}} for k, x, y in zones]
        return setup_case(p, layout_mode='explicit', wall_cells=SITES, weapon_cells=[(6, 4)])

    def step(self, mem, **kw):
        t, cfg, nav, ledger = self.frame(**kw)
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [(6, 4)], SITES, set())
        save_daytime_jobs(t, mem, ledger)
        return ledger

    def test_faces_are_stable_disjoint_and_cover_corners(self):
        sites = {(x, y) for x in (2, 6) for y in range(2, 7)} | {(x, y) for x in range(3, 6) for y in (2, 6)}
        faces = [face_sites(sites, p) for p in ((2, 2), (6, 6), (4, 2), (4, 6))]
        self.assertEqual(set.union(*faces), sites)
        self.assertEqual(sum(map(len, faces)), len(sites))
        self.assertEqual(face_sites(sites, (2, 2)), face_sites(sites, (2, 6)))

    def test_build_face_before_urgent_voucher_then_finish_delivery(self):
        mem = Memory(day=4, wall_repair_worker=1)
        t, cfg, nav, ledger = self.frame(bag=['stone'] * 2 + ['WallUpgradeVoucher1'])
        self.assertTrue(build(t, cfg, mem, nav, ledger, t.workers[0], [(4, 4)], lambda _: 'wall'))
        save_daytime_jobs(t, mem, ledger)
        wall1 = unit(40, 'wall', 4, 4, health=1000)
        wall3 = unit(42, 'wall', 4, 6, health=200, level=1)
        ledger = self.step(mem, bag=['stone', 'WallUpgradeVoucher1'], walls=[wall1, wall3], tick=401)
        self.assertEqual(ledger.commands['1'], command('build', (4, 5), name='wall'))
        wall2 = unit(41, 'wall', 4, 5, health=1000)
        ledger = self.step(mem, bag=['WallUpgradeVoucher1'], walls=[wall1, wall2, wall3], tick=402)
        self.assertEqual(ledger.commands['1']['action'], 'use')
        self.assertIn(ledger.commands['1']['targetPos'][0], [{'x': 4, 'y': 4}, {'x': 4, 'y': 5}])
        self.assertEqual(mem.wall_batches[1]['phase'], 'maintain')

    def test_occupied_cell_does_not_block_other_cell_on_face(self):
        t, cfg, nav, ledger = self.frame(bag=['stone'], walls=[unit(99, 'worker', 4, 4)])
        mem = Memory(day=4)
        start_batch(t, mem, ledger, t.workers[0], (4, 4))
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [(6, 4)], SITES, set())
        self.assertEqual(ledger.commands['1'], command('build', (4, 5), name='wall'))

    def test_completed_face_sells_copper_then_iron_despite_other_gaps(self):
        complete = [unit(40+y, 'wall', 4, y, health=2000, level=3) for y in range(4, 7)]
        t, cfg, nav, ledger = self.frame(walls=complete)
        mem = Memory(day=4, wall_repair_worker=1)
        start_batch(t, mem, ledger, t.workers[0], (4, 4))
        for tick, bag, kind, n in [(401, ['copper']*25 + ['iron']*8, 'copper', 25),
                                    (402, ['iron']*8, 'iron', 8)]:
            ledger = self.step(mem, bag=bag, walls=complete, zones=[('vendor', 2, 4)], tick=tick)
            self.assertEqual(ledger.commands['1'], command('sell', name=kind, num=n))
            self.assertEqual(mem.wall_batches[1]['phase'], 'economy')
        ledger = self.step(mem, walls=complete, zones=[('vendor', 2, 4)], tick=403)
        self.assertNotIn(1, mem.wall_batches)

    def test_no_money_for_upgrade_exits_to_sale_with_unused_voucher(self):
        complete = [unit(40+y, 'wall', 4, y, health=1500, level=2) for y in range(4, 7)]
        t, cfg, nav, ledger = self.frame(walls=complete)
        mem = Memory(day=4)
        start_batch(t, mem, ledger, t.workers[0], (4, 4))
        ledger = self.step(mem, bag=['copper']*25 + ['WallUpgradeVoucher1'], walls=complete,
                           zones=[('vendor', 2, 4), ('weaponShop', 2, 5)], tick=401)
        self.assertEqual(ledger.commands['1'], command('sell', name='copper', num=25))

    def test_sale_is_not_preempted_by_repair_or_paid_upgrade(self):
        t, cfg, nav, ledger = self.frame(bag=['copper']*25 + ['WallUpgradeVoucher1'],
                                        walls=[unit(40, 'wall', 4, 4, health=200)],
                                        zones=[('vendor', 2, 4)])
        mem = Memory(day=4, daytime_jobs={1: {'kind': 'sell', 'target': (2, 4)}},
                     wall_rebuild_levels={(8, 4): 2})
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [(6, 4)], SITES, set())
        self.assertEqual(ledger.commands['1'], command('sell', name='copper', num=25))

    def test_recall_releases_face(self):
        t, cfg, nav, ledger = self.frame(bag=['stone'])
        mem = Memory(day=4)
        start_batch(t, mem, ledger, t.workers[0], (4, 4))
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [(6, 4)], SITES, {1})
        self.assertNotIn(1, mem.wall_batches)
        self.assertNotIn('1', ledger.commands)

    def test_critical_station_still_preempts_batch(self):
        t, cfg, nav, ledger = self.frame(bag=['stone', 'StationUpgradeVoucher2'],
                                        station_level=2, station_health=100)
        station, hero = t.station, t.workers[0]
        mem = Memory(day=4)
        start_batch(t, mem, ledger, hero, (4, 4))
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [(6, 4)], SITES, set())
        self.assertEqual(ledger.work_jobs[1]['target'], station.pos)

    def test_scoped_purchase_does_not_buy_for_other_face(self):
        complete = [unit(40+y, 'wall', 4, y, health=2000, level=3) for y in range(4, 7)]
        t, cfg, nav, ledger = self.frame(walls=complete + [unit(50, 'wall', 8, 4, health=1000)],
                                        gold=100, zones=[('weaponShop', 2, 4)])
        plan = supplies(t, cfg, Memory(day=4), nav, ledger, t.workers[0],
                        target_sites=face_sites(SITES, (4, 4)), bulk=True)
        self.assertIsNone(plan)

    def test_late_sale_does_not_start_when_return_would_miss_night(self):
        complete = [unit(40+y, 'wall', 4, y, health=2000, level=3) for y in range(4, 7)]
        t, cfg, nav, ledger = self.frame(walls=complete, tick=459)
        mem = Memory(day=4)
        start_batch(t, mem, ledger, t.workers[0], (4, 4))
        ledger = self.step(mem, bag=['copper']*25, walls=complete,
                           zones=[('vendor', 0, 0)], tick=459)
        self.assertNotIn(1, mem.wall_batches)
        self.assertNotEqual(ledger.work_jobs.get(1, {}).get('kind'), 'sell')

    def test_one_face_purchase_is_delivered_before_economic_handoff(self):
        walls = [unit(40+y, 'wall', 4, y, health=1000) for y in range(4, 7)]
        t, cfg, nav, ledger = self.frame(walls=walls, gold=100)
        mem = Memory(day=4)
        start_batch(t, mem, ledger, t.workers[0], (4, 4))
        ledger = self.step(mem, walls=walls, gold=100, zones=[('weaponShop', 2, 4)], tick=401)
        self.assertEqual(ledger.commands['1']['action'], 'buy')
        self.assertEqual(ledger.commands['1']['name'], 'WallUpgradeVoucher1')
        self.assertLessEqual(ledger.commands['1']['num'], 3)
        self.assertTrue(mem.wall_batches[1]['purchased'])
        ledger = self.step(mem, walls=walls, gold=40, bag=['WallUpgradeVoucher1']*3, tick=402)
        self.assertEqual(ledger.commands['1']['action'], 'use')
        upgraded = [unit(40+y, 'wall', 4, y, health=1500, level=2) for y in range(4, 7)]
        ledger = self.step(mem, walls=upgraded, gold=100, bag=['copper']*25,
                           zones=[('weaponShop', 2, 5), ('vendor', 2, 4)], tick=405)
        self.assertEqual(ledger.commands['1'], command('sell', name='copper', num=25))

    def test_other_worker_can_continue_income_during_wall_batch(self):
        p = payload(400, [unit(1, 'worker', 3, 4, health=500, backpack=['stone']),
                          unit(2, 'worker', 1, 1, health=500, backpack=['copper']*10),
                          unit(13, 'station', 6, 6, health=4500, level=3)])
        p['mapInfo']['zones'] = [{'neutralType': 'vendor', 'pos': {'x': 2, 'y': 1}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 8}]
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit', wall_cells=SITES)
        mem = Memory(day=4, daytime_jobs={2: {'kind': 'sell', 'target': (2, 1)}})
        start_batch(t, mem, ledger, t.workers[0], (4, 4))
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [], SITES, set())
        self.assertEqual(ledger.commands['1']['action'], 'build')
        self.assertEqual(ledger.commands['2'], command('sell', name='copper', num=10))


if __name__ == '__main__':
    unittest.main()
