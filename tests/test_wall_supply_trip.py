"""Late worker ownership, multi-item shopping and deadline boundaries."""
import unittest
from unittest.mock import patch

from test_agent import payload, setup_case, unit
from agent.commands import command
from agent.economy import supplies, pioneer, workers, use_inventory
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Memory
from agent.wall_watch import prepare_watch
from agent.wall_supply import (active, requirements, shopping_list, prepare,
                               other_worker, station_plan, PACK, V1, V2)
from agent.worker_jobs import resume_daytime_jobs
from agent.wall_watch_state import NightRecord


class WallSupplyTripTests(unittest.TestCase):
    sites = [(6, 5), (6, 8), (6, 11), (1, 5), (1, 11)]

    def case(self, day=4, tick=40, gold=1000, backpack=()):
        data = payload((day-1)*130 + tick, [
            unit(13, 'station', 3, 8, health=1500),
            unit(1, 'worker', 8, 8, health=500, backpack=list(backpack)),
            unit(2, 'worker', 2, 6, health=500),
            unit(11, 'pioneer', 8, 9, health=500),
            unit(20, 'rocket', 4, 6, level=3),
            unit(30, 'wall', 6, 8, level=1, health=1000),
            unit(31, 'wall', 6, 5, level=2, health=1500),
            unit(32, 'wall', 1, 5, level=3, health=2000),
            unit(33, 'wall', 1, 11, level=3, health=2000)])
        data['teamOur']['goldNum'] = gold
        data['weaponShopList'] = [{'name': n, 'price': p} for n, p in (
            (PACK, 15), (V1, 20), (V2, 30), ('StationUpgradeVoucher1', 100))]
        data['vendorShopList'] = [{'name': 'copper', 'price': 5}, {'name': 'stone', 'price': 1}]
        data['mapInfo']['zones'] = [
            {'neutralType': 'weaponShop', 'pos': {'x': 9, 'y': 8}},
            {'neutralType': 'vendor', 'pos': {'x': 10, 'y': 8}},
            {'neutralType': 'copper', 'pos': {'x': 12, 'y': 12}},
            {'neutralType': 'stone', 'pos': {'x': 2, 'y': 5}}]
        return data

    def setup(self, data):
        return setup_case(data, layout_mode='explicit', loadout=['rocket'],
                          weapon_cells=[[4, 6]], wall_cells=[list(p) for p in self.sites],
                          llm_enabled=False)

    def test_activation_requires_day_four_and_live_watch_assignment(self):
        for day, owner, expected in ((3, 1, False), (4, None, False), (4, 1, True), (4, 99, False)):
            t, _, _, _ = self.setup(self.case(day=day))
            self.assertEqual(active(t, Memory(wall_watch_id=owner)), expected)

    def test_first_three_days_never_enter_new_trip(self):
        for day in (1, 2, 3):
            t, cfg, nav, l = self.setup(self.case(day=day))
            with patch('agent.wall_supply.prepare') as new_trip:
                prepare_watch(t, cfg, Memory(wall_watch_id=1), nav, l, t.units[1], l.wall_cells)
            new_trip.assert_not_called()

    def test_mixed_levels_and_unbuilt_site_prefetch_both_tiers(self):
        t, _, _, l = self.setup(self.case(backpack=[V1, V2, PACK]))
        report = requirements(t, Memory(wall_watch_id=1), t.units[1], l.wall_cells)
        self.assertEqual((report['one'], report['two'], report['gaps']), (2, 1, 1))
        self.assertEqual(report['demand'], {V1: 1, V2: 2, PACK: 3})

    def test_hit_counts_count_walls_not_number_of_hits(self):
        data = self.case()
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 33]
        data['mapInfo']['zones'].append({'neutralType': 'stone', 'pos': dict(x=6, y=11)})
        t, _, _, l = self.setup(data)
        report = requirements(t, Memory(wall_watch_id=1, wall_hits={(1, 11): 100}),
                              t.units[1], l.wall_cells)
        self.assertEqual(report['key_three'], 3)

    def test_tight_budget_buys_level_two_before_level_three(self):
        t, _, _, l = self.setup(self.case(gold=85))
        report = shopping_list(t, Memory(wall_watch_id=1), t.units[1], l.wall_cells, 85, 100)
        self.assertEqual(report['order'], {PACK: 3, V1: 2})
        self.assertEqual(report['unfunded'][V2], 3)
        self.assertEqual(report['cost'], 85)

    def test_unfunded_prerequisites_never_buy_level_three(self):
        t, _, _, l = self.setup(self.case())
        mem = Memory(wall_watch_id=1)
        t.shop.pop(V1)
        report = shopping_list(t, mem, t.units[1], l.wall_cells, 1000, 100)
        self.assertNotIn(V2, report['order'])

    def test_stockout_prioritizes_repair_reserve_before_level_three(self):
        t, _, _, l = self.setup(self.case())
        mem = Memory(wall_watch_id=1)
        mem.wall_watch.history.append(NightRecord(3, 3, 70, {}, used=3,
                                                  empty_tick=95, unmet={(6, 8)}))
        report = shopping_list(t, mem, t.units[1], l.wall_cells, 145, 100)
        self.assertNotIn(V2, report['order'])
        self.assertEqual(report['order'][V1], 2)

    def test_backpack_limit_keeps_upgrade_prerequisite_order(self):
        t, _, _, l = self.setup(self.case())
        report = shopping_list(t, Memory(wall_watch_id=1), t.units[1], l.wall_cells, 1000, 4)
        self.assertEqual(report['order'], {PACK: 3, V1: 1})
        self.assertNotIn(V2, report['order'])

    def test_shop_stays_for_all_item_types_before_delivery(self):
        data, mem = self.case(), Memory(wall_watch_id=1)
        purchases = []
        for _ in range(3):
            t, cfg, nav, l = self.setup(data)
            locked, reserve = prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
            self.assertTrue(locked)
            action = l.commands['1']
            self.assertEqual(action['action'], 'buy')
            purchases.append((action['name'], action['num']))
            data['teamOur']['roles'][1]['backpack'] += [action['name']] * action['num']
            data['teamOur']['goldNum'] -= t.shop[action['name']] * action['num']
            self.assertLessEqual(reserve, l.gold)
            data['roundNo'] += 1
        self.assertEqual(purchases, [(PACK, 4), (V1, 2), (V2, 3)])
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(mem.wall_supply_trip['phase'], 'deliver')
        self.assertNotEqual(l.commands['1']['action'], 'buy')

    def test_failed_buy_retries_from_observed_inventory(self):
        data, mem = self.case(), Memory(wall_watch_id=1)
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        data['roundNo'] += 1
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1'], command('buy', name=PACK, num=4))
        self.assertEqual(mem.wall_supply_trip['phase'], 'shop')

    def test_new_gold_at_shop_extends_order_without_delivery_detour(self):
        data, mem = self.case(gold=85), Memory(wall_watch_id=1)
        for extra in (0, 500):
            t, cfg, nav, l = self.setup(data)
            prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
            action = l.commands['1']
            data['teamOur']['roles'][1]['backpack'] += [action['name']] * action['num']
            data['teamOur']['goldNum'] += extra - t.shop[action['name']] * action['num']
            data['roundNo'] += 1
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1']['action'], 'buy')
        self.assertEqual(mem.wall_supply_trip['phase'], 'shop')

    def test_first_35_rounds_mine_despite_carried_upgrade(self):
        t, cfg, nav, l = self.setup(self.case(tick=34, backpack=[V1]))
        mem = Memory(wall_watch_id=1)
        with patch('agent.economy.earn', return_value=True) as earn:
            self.assertFalse(prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)[0])
            workers(t, cfg, mem, nav, l, l.tower_cells, l.wall_cells, excluded={2})
        earn.assert_called_once()
        self.assertEqual(mem.wall_supply_trip['phase'], 'mine')
        self.assertFalse(l.commands)

    def test_old_job_resume_cannot_preempt_late_watch_trip(self):
        t, cfg, nav, l = self.setup(self.case(backpack=[V1]))
        mem = Memory(wall_watch_id=1, daytime_jobs={1: dict(kind='use', target=(6, 8), name=V1)})
        resume_daytime_jobs(t, cfg, mem, nav, l, [], self.sites, set())
        self.assertFalse(l.commands)
        prepare_watch(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1']['action'], 'buy')

    def test_early_mining_run_keeps_accepted_target(self):
        t, cfg, nav, l = self.setup(self.case(tick=34))
        job = dict(kind='mine', target=(12, 12), ore='copper', want_stone=False)
        mem = Memory(wall_watch_id=1, daytime_jobs={1: job})
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(mem.daytime_jobs[1], job)
        with patch('agent.worker_jobs._continue', return_value=(True, 'continued')) as resume:
            workers(t, cfg, mem, nav, l, l.tower_cells, l.wall_cells, excluded={2})
        self.assertEqual(resume.call_args.args[6], job)

    def test_unfunded_first_visit_can_start_after_late_reward(self):
        data, mem = self.case(gold=0), Memory(wall_watch_id=1)
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(mem.wall_supply_trip['phase'], 'deliver')
        self.assertIsNone(mem.wall_supply_trip['shop'])
        data['roundNo'] += 1
        data['teamOur']['goldNum'] = 500
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1']['action'], 'buy')

    def test_wall_upgrades_continue_on_same_wall_without_global_tier_gate(self):
        data = self.case(backpack=[PACK]*4 + [V1]*2 + [V2]*3)
        data['teamOur']['roles'][1]['pos'] = dict(x=5, y=8)
        data['mapInfo']['zones'] = []
        mem = Memory(wall_watch_id=1)
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1'], command('use', (6, 8), name=V1))
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'].remove(V1)
        data['teamOur']['roles'][5].update(level=2, health=1500)
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1'], command('use', (6, 8), name=V2))

    def test_active_task_keeps_worker_stock_purchase_running(self):
        data = self.case()
        data['phaseTask'] = 'active self evolution task'
        t, cfg, nav, l = self.setup(data)
        mem = Memory(wall_watch_id=1)
        prepare_watch(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1'], command('buy', name=PACK, num=4))

    def test_near_dusk_returns_without_waiting_for_missing_vouchers(self):
        t, cfg, nav, l = self.setup(self.case(tick=68))
        mem = Memory(wall_watch_id=1)
        prepare(t, cfg, mem, nav, l, t.units[1], l.wall_cells)
        self.assertEqual(mem.wall_supply_trip['phase'], 'return')
        self.assertNotEqual(l.commands.get('1', {}).get('action'), 'buy')

    def test_helper_cannot_buy_wall_or_station_vouchers_after_activation(self):
        data = self.case()
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['roleType'] != 'wall']
        data['teamOur']['roles'] += [unit(30+i, 'wall', *p, level=3, health=2000)
                                   for i, p in enumerate(self.sites)]
        t, cfg, nav, l = self.setup(data)
        mem = Memory(wall_watch_id=1)
        self.assertIsNone(supplies(t, cfg, mem, nav, l, t.units[2], bulk=True))
        self.assertIsNotNone(station_plan(t, cfg, mem, nav, l, t.pioneer))
        pioneer(t, cfg, mem, nav, l, t.pioneer)
        self.assertEqual(l.commands['11'], command('buy', name='StationUpgradeVoucher1', num=1))

    def test_active_task_does_not_move_pioneer_for_station_shopping(self):
        data = self.case()
        data['phaseTask'] = 'active self evolution task'
        data['teamOur']['roles'][0]['health'] = 100
        t, cfg, nav, l = self.setup(data)
        self.assertIsNone(station_plan(t, cfg, Memory(wall_watch_id=1), nav, l, t.pioneer))

    def test_helper_repairs_without_starting_wall_shopping(self):
        t, cfg, nav, l = self.setup(self.case())
        mem = Memory(wall_watch_id=1)
        with patch('agent.economy.repair_walls', return_value={(6, 11)}) as repair:
            other_worker(t, cfg, mem, nav, l, t.units[2], l.tower_cells, l.wall_cells)
        self.assertEqual(repair.call_args.args[5], [t.units[2]])

    def test_helper_delivers_legacy_stock_without_buying_another_batch(self):
        data = self.case()
        data['teamOur']['roles'][2].update(pos=dict(x=5, y=8), backpack=[V1])
        t, cfg, nav, l = self.setup(data)
        mem = Memory(wall_watch_id=1)
        with patch('agent.economy.repair_walls', return_value=set()):
            other_worker(t, cfg, mem, nav, l, t.units[2], l.tower_cells, l.wall_cells)
        self.assertEqual(l.commands['2'], command('use', (6, 8), name=V1))

    def test_legacy_worker_station_stock_cannot_heal_base(self):
        data = self.case(backpack=['StationUpgradeVoucher1'])
        data['teamOur']['roles'][0]['health'] = 499
        data['teamOur']['roles'][1]['pos'] = dict(x=5, y=8)
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, Memory(wall_watch_id=1), nav, l, t.units[1], l.wall_cells)
        self.assertFalse(any(c.get('name', '').startswith('StationUpgradeVoucher')
                             for c in l.commands.values()))

    def test_pioneer_buys_own_base_reserve_even_with_stranded_worker_stock(self):
        data = self.case(backpack=['StationUpgradeVoucher1'])
        data['teamOur']['roles'][0]['health'] = 499
        t, cfg, nav, l = self.setup(data)
        self.assertIsNotNone(station_plan(t, cfg, Memory(wall_watch_id=1), nav, l, t.pioneer))
        self.assertIsNone(station_plan(t, cfg, Memory(wall_watch_id=1), nav, l, t.units[2]))

    def test_only_pioneer_uses_station_coupon_in_day_and_night(self):
        for tick in (40, 70):
            data = self.case(tick=tick, backpack=['StationUpgradeVoucher1'])
            data['teamOur']['roles'][0]['health'] = 499
            data['teamOur']['roles'][1]['pos'] = dict(x=5, y=8)
            data['teamOur']['roles'][3].update(pos=dict(x=5, y=9), backpack=['StationUpgradeVoucher1'])
            t, cfg, nav, l = self.setup(data)
            self.assertFalse(use_inventory(t, nav, l, t.units[1], mem=Memory(), target_only=t.station.pos))
            self.assertTrue(use_inventory(t, nav, l, t.pioneer, mem=Memory(), target_only=t.station.pos))
            self.assertEqual(l.commands['11']['name'], 'StationUpgradeVoucher1')

    def gunner_case(self, tick):
        data = payload(390 + tick, [unit(13, 'station', 5, 11, health=1500),
            unit(1, 'worker', 6, 9, health=500), unit(2, 'worker', 11, 2, health=500),
            unit(11, 'pioneer', 4, 11, health=500),
            unit(20, 'rocket', 4, 12, level=3), unit(21, 'rocket', 5, 12, level=3),
            unit(22, 'rocket', 4, 10, level=3)])
        data['robot']['roles'] = [unit(90, 'smallRobot', 8, 10, targetTeam='challenger')]
        return data

    def test_night_rockets_are_controlled_by_pioneer(self):
        commands = Agent(Config(llm_enabled=False)).decide(self.gunner_case(70))['roleCommandMap']
        shots = [c for c in commands.values() if c['action'] == 'attack']
        self.assertTrue(shots)
        self.assertEqual({c['controllerId'] for c in shots}, {'11'})

    def test_active_daytime_task_never_recalls_worker_as_temporary_gunner(self):
        data = self.gunner_case(40)
        data['phaseTask'] = 'active task'
        data['robot']['roles'] = []
        agent = Agent(Config())
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        self.assertNotIn(1, mem.return_targets)
        self.assertNotIn(2, mem.return_targets)

    def test_wounded_watcher_can_buy_medicine_during_mining_window(self):
        data = self.case(tick=10)
        data['teamOur']['roles'][1]['health'] = 50
        data['weaponShopList'].append(dict(name='Medicine', price=10))
        t, cfg, nav, l = self.setup(data)
        prepare(t, cfg, Memory(wall_watch_id=1), nav, l, t.units[1], l.wall_cells)
        self.assertEqual(l.commands['1'], command('buy', name='Medicine', num=1))


if __name__ == '__main__':
    unittest.main()
