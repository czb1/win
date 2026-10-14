"""Maxed weapons retain the daytime mining window and trading boundary."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.config import Config
from agent.economy import dusk_resources, supplies, workers
from agent.economy_plan import preparation_start
from agent.intelligence import Memory
from agent.mining import earn
from agent.spending import plan_day_spending, prepare_defense_purchase
from agent.worker_jobs import resume_daytime_jobs
from agent.wall_watch import prepare_watch


TOWERS = [(3, 10), (5, 10), (7, 10)]
WALLS = [(4, 8)]


def case(tick=34, day=4, bag=0, level=3):
    roles = [unit(13, 'station', 3, 13, health=4500, level=3),
             unit(1, 'worker', 9, 6, health=500, backpack=['copper'] * bag),
             unit(2, 'worker', 9, 8, health=500, backpack=['copper'] * bag),
             unit(30, 'wall', 4, 8, health=1500, level=2)]
    roles += [unit(20+i, 'rocket', *p, level=level, health=2000)
              for i, p in enumerate(TOWERS)]
    data = payload((day-1)*130+tick, roles)
    data['teamOur']['goldNum'] = 300
    data['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y), remain=10)
                              for k, x, y in [('copper', 8, 7), ('vendor', 10, 7),
                                             ('weaponShop', 10, 5)]]
    data['vendorShopList'] = [dict(name='copper', price=8)]
    data['weaponShopList'] = [dict(name=n, price=p) for n, p in
                             [('WallUpgradeVoucher2', 30), ('WallFixer', 15),
                              ('WeaponUpgradeVoucher2', 150), ('Medicine', 10)]]
    return data


def config(**kw):
    return Config(layout_mode='explicit', loadout=['rocket']*3,
                  weapon_cells=[list(p) for p in TOWERS],
                  wall_cells=[list(p) for p in WALLS], llm_enabled=False, **kw)


def setup(data, **kw):
    cfg = config(**kw)
    return setup_case(data, **vars(cfg))


class MaxedWeaponMiningPhaseTests(unittest.TestCase):
    def test_agent_keeps_both_workers_mining_before_boundary(self):
        for day in (2, 4, 6):
            for tick in (0, 10, 34):
                with self.subTest(day=day, tick=tick):
                    result = Agent(config()).decide(case(tick, day))['roleCommandMap']
                    self.assertEqual(result['1'], command('collect', (8, 7)))
                    self.assertEqual(result['2'], command('collect', (8, 7)))

    def test_sweep_waits_until_boundary_even_with_full_batch(self):
        for tick in (34, 35, 36):
            with self.subTest(tick=tick):
                t, c, n, l = setup(case(tick, bag=40))
                dusk_resources(t, c, Memory(), n, l, TOWERS)
                if tick < 35:
                    self.assertFalse(l.commands)
                else:
                    self.assertEqual(l.commands['1'], command('sell', name='copper', num=40))

    def test_ordinary_purchase_and_preemptive_planner_wait_until_boundary(self):
        for tick in (34, 35, 36):
            with self.subTest(tick=tick):
                t, c, n, l = setup(case(tick))
                mem = Memory()
                actors, reserve = plan_day_spending(t, c, mem, n, l, TOWERS)
                prepare_defense_purchase(t, mem, l, reserve)
                if tick < 35:
                    self.assertEqual((actors, reserve), (set(), 0))
                    self.assertFalse(l.commands)
                else:
                    self.assertEqual(l.commands['1'], command('buy', name='WallUpgradeVoucher2', num=1))

    def test_sale_flags_and_short_deadline_do_not_end_mining_early(self):
        t, c, n, l = setup(case(bag=40))
        mem = Memory(sale_workers={1}, sale_targets={1: (10, 7)})
        self.assertTrue(earn(t, c, mem, n, l, t.workers[0], deadline=10))
        self.assertEqual(l.commands['1'], command('collect', (8, 7)))

    def test_early_forced_sale_does_not_start_vendor_trip(self):
        t, c, n, l = setup(case(bag=40))
        self.assertFalse(earn(t, c, Memory(), n, l, t.workers[0], force_sale=True))
        self.assertFalse(l.commands)

    def test_old_sell_and_buy_jobs_yield_to_mining_before_boundary(self):
        for job in (dict(kind='sell', target=(10, 7)),
                    dict(kind='buy', target=(10, 5), name='WallUpgradeVoucher2', quantity=1)):
            with self.subTest(job=job):
                t, c, n, l = setup(case(bag=40))
                mem = Memory(daytime_jobs={1: job}, supply_worker=1,
                             preparation_workers={1}, sale_workers={1})
                resume_daytime_jobs(t, c, mem, n, l, TOWERS, WALLS, set())
                workers(t, c, mem, n, l, TOWERS, WALLS)
                self.assertEqual(l.commands['1'], command('collect', (8, 7)))

    def test_maxed_weapons_do_not_move_boundary_earlier_for_many_walls(self):
        data = case()
        data['teamOur']['roles'] += [unit(50+i, 'wall', x, 2, health=1000)
                                   for i, x in enumerate(range(1, 14))]
        t, c, n, l = setup(data)
        mem = Memory(preparation_tick=5)
        self.assertEqual(preparation_start(t, c, mem, n, t.workers, TOWERS), 35)

    def test_configured_threshold_and_round_origin_are_respected(self):
        for origin in (0, 1):
            for tick in (19, 20):
                with self.subTest(origin=origin, tick=tick):
                    data = case(tick)
                    data['roundNo'] += origin
                    t, c, n, l = setup(data, economy_rounds=20, round_origin=origin)
                    dusk_resources(t, c, Memory(), n, l, TOWERS)
                    self.assertEqual(bool(l.commands), tick >= 20)

    def test_new_day_reopens_mining_window(self):
        data = case(35, bag=40)
        agent = Agent(config())
        self.assertIn(agent.decide(copy.deepcopy(data))['roleCommandMap']['1']['action'], ('sell', 'buy'))
        data['roundNo'] = 520
        result = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(result['1'], command('collect', (8, 7)))
        self.assertEqual(result['2'], command('collect', (8, 7)))

    def test_emergency_medicine_purchase_still_runs_during_mining(self):
        data = case()
        data['teamOur']['roles'][1]['health'] = 50
        t, c, n, l = setup(data)
        self.assertEqual(supplies(t, c, Memory(), n, l, t.workers[0], urgent_only=True),
                         ('Medicine', (0, None), 1))

    def test_watch_stock_and_accepted_order_wait_until_mining_ends(self):
        from test_daylight_spending import case as watch_case, setup as watch_setup
        for tick in (34, 35, 36):
            for continuing in (False, True):
                with self.subTest(tick=tick, continuing=continuing):
                    data, cfg = watch_case(tick=tick, packs=1)
                    data['teamOur']['roles'][1]['pos'] = dict(x=6, y=10)
                    for p, hp in (((8, 4), 1440), ((8, 5), 1545)):
                        wall = next(r for r in data['teamOur']['roles']
                                    if r['roleType'] == 'wall' and r['pos'] == dict(x=p[0], y=p[1]))
                        wall['health'] = hp
                    t, c, n, l = watch_setup(data, cfg)
                    mem = Memory(wall_watch_id=1)
                    if continuing:
                        mem.daytime_jobs[1] = dict(kind='buy', target=(6, 11), name='WallFixer', quantity=10)
                    locked, funds = prepare_watch(t, c, mem, n, l, t.workers[0], list(l.wall_cells))
                    if tick < 35:
                        self.assertEqual((locked, funds), (False, 30))
                        self.assertFalse(l.commands)
                    else:
                        self.assertEqual((locked, funds), (True, 0))
                        self.assertEqual(l.commands['1'], command('buy', name='WallFixer', num=8))

    def test_new_robot_purchase_waits_but_paid_order_can_be_used(self):
        for tick in (34, 35):
            for paid in (False, True):
                with self.subTest(tick=tick, paid=paid):
                    data = case(tick)
                    data['teamEnemy']['roles'] = [unit(99, 'station', 12, 2, health=1500)]
                    data['weaponShopList'].append(dict(name='MiddleRobotSummonOrder', price=20))
                    if paid:
                        data['teamOur']['roles'][1]['backpack'] = ['MiddleRobotSummonOrder']
                    t, c, n, l = setup(data, robot_purchase_last_day=10)
                    accepted = summon_best_robot(t, c, Memory(), n, l, TOWERS, WALLS)
                    self.assertEqual(accepted, paid or tick >= 35)
                    if accepted:
                        self.assertEqual(l.commands['1']['action'], 'use' if paid else 'buy')
                    else:
                        self.assertFalse(l.commands)

    def test_incomplete_maxed_battery_keeps_original_sale_sweep(self):
        data = case(10, bag=40)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] not in (21, 22)]
        t, c, n, l = setup(data)
        dusk_resources(t, c, Memory(), n, l, TOWERS)
        self.assertEqual(l.commands['1'], command('sell', name='copper', num=40))

    def test_incomplete_maxed_battery_keeps_original_construction_allocation(self):
        data = case(10)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] not in (21, 22)]
        t, c, n, l = setup(data)
        workers(t, c, Memory(), n, l, TOWERS, WALLS)
        self.assertEqual(l.commands['1'], command('collect', (8, 7)))
        self.assertEqual(l.work_jobs[2]['kind'], 'build')
        self.assertEqual(l.work_jobs[2]['name'], 'rocket')

    def test_incomplete_battery_does_not_cancel_watch_mining_window(self):
        from test_daylight_spending import case as watch_case, setup as watch_setup
        data, cfg = watch_case(tick=10, packs=1)
        cfg.loadout = ['rocket'] * 3
        cfg.weapon_cells += [[6, 4], [7, 3]]
        data['teamOur']['roles'][1]['pos'] = dict(x=6, y=10)
        t, c, n, l = watch_setup(data, cfg)
        locked, funds = prepare_watch(t, c, Memory(wall_watch_id=1), n, l,
                                     t.workers[0], list(l.wall_cells))
        self.assertEqual((locked, funds), (False, 30))
        self.assertFalse(l.commands)


if __name__ == '__main__':
    unittest.main()
