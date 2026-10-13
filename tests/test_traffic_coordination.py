"""Focused occupancy, daylight budget and cross-turn passage transitions."""
from dataclasses import replace
import unittest

from test_agent import payload, unit, setup_case
from agent.commands import command
from agent.intelligence import Memory
from agent.mining import earn, mine, return_destination
from agent.traffic import coordinate_traffic, park_idle_worker, transit_cells
from agent.worker_jobs import save_daytime_jobs, resume_daytime_jobs
from agent.economy import first_day_front, workers
from agent.wall_watch import watch_route
from agent.navigation import layout


def gate_case(tick=20, worker=(3, 3), requester=(1, 3), day=4, ready=True):
    data = payload((day-1)*130+tick, [unit(1, 'worker', *requester, health=500),
                          unit(2, 'worker', *worker, health=500),
                          *[unit(20+y, 'wall', 3, y) for y in range(15) if y != 3]])
    if ready:
        data['teamOur']['roles'] += [unit(60+i, 'rocket', 10+i, 10, level=3) for i in range(3)]
    data['mapInfo']['zones'] = [dict(neutralType='weaponShop', pos=dict(x=7, y=3))]
    data['weaponShopList'] = [dict(name='WallFixer', price=15)]
    return data


class TrafficCoordinationTests(unittest.TestCase):
    def shopping_memory(self):
        return Memory(daytime_jobs={1: dict(kind='buy', target=(7, 3),
                                           name='WallFixer', quantity=3)})

    def test_new_traffic_and_parking_skip_opening_or_unfinished_firepower(self):
        for day, ready, downgrade in ((1, True, False), (2, False, False), (4, True, True)):
            with self.subTest(day=day, ready=ready, downgrade=downgrade):
                data = gate_case(day=day, ready=ready)
                if downgrade:
                    data['teamOur']['roles'][-1]['level'] = 2
                turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
                mem = self.shopping_memory()
                coordinate_traffic(turn, cfg, mem, nav, ledger, set())
                self.assertFalse(ledger.commands)
                self.assertFalse(ledger.traffic_holds)
                self.assertFalse(mem.traffic_leases)
                self.assertFalse(park_idle_worker(turn, nav, ledger, turn.units[2]))

    def test_dedicated_late_work_does_not_start_another_remote_income_cycle(self):
        for kind in ('build', 'buy', 'use', 'robot_buy', 'recovery', 'return', 'wall_batch'):
            with self.subTest(kind=kind):
                data = payload(435, [unit(1, 'worker', 1, 3, health=500),
                    unit(13, 'station', 1, 7),
                    *[unit(60+i, 'rocket', 10+i, 10, level=3) for i in range(3)]])
                data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=6, y=3), remain=10),
                                          dict(neutralType='vendor', pos=dict(x=7, y=5))]
                data['vendorShopList'] = [dict(name='copper', price=10)]
                turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
                mem = Memory(sold_workers={1})
                if kind == 'return':
                    mem.return_targets[1] = 60
                elif kind == 'wall_batch':
                    mem.wall_batches[1] = {'phase': 'build'}
                else:
                    mem.daytime_jobs[1] = {'kind': kind, 'target': (6, 3)}
                earn(turn, cfg, mem, nav, ledger, turn.units[1])
                self.assertNotIn(1, mem.mine_targets)
                self.assertNotEqual(ledger.work_jobs.get(1, {}).get('kind'), 'mine')

    def test_early_days_sale_still_funds_available_weapon_before_another_mine(self):
        for day in (1, 2, 3):
            with self.subTest(day=day):
                data = payload((day-1)*130+10, [unit(13, 'station', 3, 11),
                                               unit(1, 'worker', 5, 10, health=500)])
                data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=6, y=9), remain=10),
                                          dict(neutralType='vendor', pos=dict(x=7, y=10))]
                data['vendorShopList'] = [dict(name='copper', price=10)]
                turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', weapon_cells=[[5, 11]])
                workers(turn, cfg, Memory(sold_workers={1}), nav, ledger, [(5, 11)], [])
                self.assertEqual(ledger.commands['1'], command('build', (5, 11), name='rocket'))
                self.assertNotIn(1, ledger.work_jobs)

    def test_superseded_mining_cannot_borrow_front_builder_in_early_days(self):
        for day in (1, 2, 3):
            with self.subTest(day=day):
                data = gate_case(40, day=day, ready=False)
                data['teamOur']['roles'].append(unit(13, 'station', 5, 6))
                data['teamOur']['roles'][1]['backpack'] = ['stone']
                data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=7, y=3), remain=10),
                                          dict(neutralType='vendor', pos=dict(x=7, y=5))]
                data['vendorShopList'] = [dict(name='copper', price=10)]
                turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', wall_cells=[[4, 4]])
                mem = Memory(preparation_tick=20, daytime_jobs={1: dict(kind='mine', target=(7, 3),
                    ore='copper', want_stone=False, stockpile=False, deadline=20, stone_goal=None)})
                coordinate_traffic(turn, cfg, mem, nav, ledger, set())
                self.assertFalse(ledger.traffic_holds)
                self.assertFalse(mem.traffic_leases)
                if day == 1:
                    first_day_front(turn, cfg, mem, nav, ledger, [(4, 4)])
                    self.assertEqual(ledger.commands['2'], command('build', (4, 4), name='wall'))

    def test_first_day_front_action_is_protected_from_valid_ordinary_traffic(self):
        data = gate_case(10, day=1, ready=False)
        data['teamOur']['roles'].append(unit(13, 'station', 5, 6))
        data['teamOur']['roles'][1]['backpack'] = ['stone']
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', wall_cells=[[4, 4]])
        mem = self.shopping_memory()
        first_day_front(turn, cfg, mem, nav, ledger, [(4, 4)])
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertEqual(ledger.commands['2'], command('build', (4, 4), name='wall'))
        self.assertNotIn(2, ledger.traffic_holds)
        self.assertNotIn(2, ledger.traffic_forbidden)

    def test_protected_late_income_job_can_still_coordinate_after_preparation_cutoff(self):
        data = gate_case(40)
        data['teamOur']['roles'].append(unit(13, 'station', 5, 6))
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=7, y=3), remain=10)]
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', wall_cells=[[4, 4]])
        ledger.income_worker_id = 1
        mem = Memory(preparation_tick=20, daytime_jobs={1: dict(kind='mine', target=(7, 3),
            ore='copper', want_stone=False, stockpile=False, deadline=70, stone_goal=None)})
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertIn(1, mem.traffic_leases)
        self.assertIn(1, ledger.traffic_holds)

    def test_current_main_mining_run_keeps_passage_after_preparation_cutoff(self):
        data = gate_case(40)
        data['teamOur']['roles'].append(unit(13, 'station', 5, 6))
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=7, y=3), remain=10)]
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', wall_cells=[[4, 4]])
        mem = Memory(preparation_tick=20, daytime_jobs={1: dict(kind='mine', target=(7, 3),
            ore='copper', want_stone=False, stockpile=True, deadline=70, stone_goal=None,
            run=dict(day=turn.day, mode='carry_for_later', vendor=None, end_tick=70, collect_goal=5))})
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertIn(1, mem.traffic_leases)
        self.assertIn(1, ledger.traffic_holds)
        self.assertEqual(ledger.work_jobs[1]['run'], mem.daytime_jobs[1]['run'])

    def test_forecast_vacates_own_gate_but_not_another_hero(self):
        data = gate_case(worker=(1, 3), requester=(3, 3))
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        hero = turn.units[1]
        occupied = set(turn.blocked)
        proxy = replace(hero, pos=(6, 3))
        self.assertIsNotNone(nav.search(proxy, {(1, 2)}))
        reverse = nav.actor_distances_to(proxy, {(1, 2)}, exact=True)
        self.assertIn(proxy.pos, reverse)
        self.assertNotIn((1, 3), reverse)
        self.assertIsNone(nav.search(proxy, {(1, 3)}))  # teammate remains occupied
        self.assertIsNone(nav.search(proxy, {(1, 2)}, {(3, 3)}))  # reservation remains
        self.assertEqual(turn.blocked, occupied)
        self.assertFalse(ledger.add(2, command('move', (3, 3))))

    def test_forecast_does_not_remove_wall_built_on_old_cell(self):
        turn, cfg, nav, ledger = setup_case(gate_case(worker=(1, 3), requester=(3, 3)),
                                           layout_mode='explicit')
        turn.ours += (replace(turn.units[20], id=100, pos=(3, 3)),)
        self.assertIsNone(nav.search(replace(turn.units[1], pos=(6, 3)), {(1, 2)}))

    def test_logged_gate_positions_both_allow_shop_round_trip(self):
        for point in ((9, 20), (8, 20)):
            with self.subTest(point=point):
                data = payload(911, [unit(13, 'station', 9, 22, level=3),
                    unit(10012, 'worker', *point, health=500),
                    unit(10010, 'worker', 8, 22, health=500),
                    *[unit(20+i, 'rocket', *p, level=3)
                      for i, p in enumerate(((8, 21), (8, 23), (9, 23)))]])
                data['mapInfo'].update(width=41, height=32)
                data['mapInfo']['zones'] = [dict(neutralType='weaponShop', pos=dict(x=25, y=20))]
                turn, cfg, nav, ledger = setup_case(data)
                walls = layout(turn, cfg)[1]
                data['teamOur']['roles'] += [unit(100+i, 'wall', *p, level=3) for i, p in enumerate(walls)]
                turn, cfg, nav, ledger = setup_case(data)
                ledger.daytime_gunner_post = (8, 22)
                hero = turn.units[10012]
                outbound = nav.approach(hero, {(25, 20)}, with_endpoint=True)
                self.assertIsNotNone(outbound)
                back = watch_route(turn, nav, ledger, Memory(),
                                   replace(hero, pos=outbound[2]), walls)
                self.assertIsNotNone(back)
                self.assertLess(outbound[0] + back[0] + 1 + cfg.return_margin, turn.day_left)

    def test_proactive_reservation_stops_nearby_worker_entering_free_gate(self):
        turn, cfg, nav, ledger = setup_case(gate_case(worker=(2, 4)), layout_mode='explicit')
        mem = self.shopping_memory()
        self.assertIsNotNone(nav.search(turn.units[1], {(6, 3)}))
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertIn(1, mem.traffic_leases)
        self.assertFalse(ledger.add(2, command('move', (3, 3))))

    def test_equal_priority_uses_distance_before_actor_id(self):
        data = gate_case(worker=(4, 4), requester=(0, 3))
        data['mapInfo']['zones'].append(dict(neutralType='weaponShop', pos=dict(x=0, y=7)))
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        mem = self.shopping_memory()
        mem.daytime_jobs[2] = dict(kind='buy', target=(0, 7), name='WallFixer', quantity=3)
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertIn(2, mem.traffic_leases)
        self.assertNotIn(1, mem.traffic_leases)

    def test_elapsed_shopping_budget_cancels_passage_hold(self):
        mem = self.shopping_memory()
        turn, cfg, nav, ledger = setup_case(gate_case(), layout_mode='explicit')
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        turn, cfg, nav, ledger = setup_case(gate_case(69), layout_mode='explicit')
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertFalse(mem.traffic_leases)
        self.assertFalse(ledger.traffic_holds)

    def test_yield_precedes_work_and_survives_next_turn_until_passed(self):
        mem = self.shopping_memory()
        turn, cfg, nav, ledger = setup_case(gate_case(), layout_mode='explicit')
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertIn(2, ledger.traffic_holds)
        self.assertIn(1, ledger.traffic_holds)
        self.assertEqual(ledger.notes[1]['reason'], 'wait_for_passage')
        self.assertEqual(ledger.commands['2']['action'], 'move')
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, [], [], set())
        save_daytime_jobs(turn, mem, ledger)
        self.assertIn(1, mem.daytime_jobs)
        side = ledger.commands['2']['targetPos'][0]
        turn, cfg, nav, ledger = setup_case(
            gate_case(21, worker=(side['x'], side['y'])), layout_mode='explicit')
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertIn(1, mem.traffic_leases)
        self.assertFalse(ledger.add(2, command('move', (3, 3))))
        self.assertIsNone(nav.search(turn.units[2], {(3, 3)}))
        self.assertIsNotNone(nav.search(turn.units[1], {(6, 3)}))
        # After the owner has crossed the gate and exited its corridor, release.
        turn, cfg, nav, ledger = setup_case(
            gate_case(24, worker=(side['x'], side['y']), requester=(5, 3)), layout_mode='explicit')
        coordinate_traffic(turn, cfg, mem, nav, ledger, set())
        self.assertFalse(mem.traffic_leases)
        self.assertFalse(ledger.traffic_forbidden)

    def test_cancelled_dead_or_stalled_request_releases_lease(self):
        for change in ('cancel', 'dead', 'stalled'):
            with self.subTest(change=change):
                mem = self.shopping_memory()
                turn, cfg, nav, ledger = setup_case(gate_case(), layout_mode='explicit')
                coordinate_traffic(turn, cfg, mem, nav, ledger, set())
                self.assertIn(1, mem.traffic_leases)
                data = gate_case(28, worker=(2, 5))
                if change == 'cancel':
                    mem.daytime_jobs.clear()
                elif change == 'dead':
                    data['teamOur']['roles'][0]['health'] = 0
                turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
                coordinate_traffic(turn, cfg, mem, nav, ledger, set())
                self.assertFalse(mem.traffic_leases)

    def test_explicit_recall_beats_opposing_shopping(self):
        data = gate_case(worker=(3, 3), requester=(1, 3))
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        mem = self.shopping_memory()
        ledger.operator_posts[2] = (0, 3)
        coordinate_traffic(turn, cfg, mem, nav, ledger, {2})
        self.assertNotIn(1, mem.traffic_leases)
        self.assertNotIn('2', ledger.commands)

    def test_gate_is_never_idle_destination_even_without_requester(self):
        turn, cfg, nav, ledger = setup_case(gate_case(requester=(0, 0)), layout_mode='explicit')
        self.assertIn((3, 3), transit_cells(turn, nav, ledger))
        self.assertTrue(park_idle_worker(turn, nav, ledger, turn.units[2]))
        target = ledger.commands['2']['targetPos'][0]
        self.assertNotEqual((target['x'], target['y']), (3, 3))

    def test_safe_idle_position_stays_put_instead_of_going_home(self):
        turn, cfg, nav, ledger = setup_case(gate_case(worker=(1, 6)), layout_mode='explicit')
        self.assertTrue(park_idle_worker(turn, nav, ledger, turn.units[2]))
        self.assertFalse(ledger.commands)

    def test_sold_worker_starts_new_remote_mine_and_can_sell_again(self):
        data = payload(435, [unit(1, 'worker', 1, 3, health=500),
                            unit(13, 'station', 1, 7),
                            *[unit(60+i, 'rocket', 10+i, 10, level=3) for i in range(3)]])
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=6, y=3), remain=10),
                                  dict(neutralType='vendor', pos=dict(x=7, y=5))]
        data['vendorShopList'] = [dict(name='copper', price=10)]
        mem = Memory(sold_workers={1})
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        self.assertTrue(earn(turn, cfg, mem, nav, ledger, turn.units[1]))
        self.assertEqual(ledger.work_jobs[1]['kind'], 'mine')
        self.assertFalse(ledger.work_jobs[1]['stockpile'])
        data['teamOur']['roles'][0].update(pos=dict(x=6, y=5), backpack=['copper']*10)
        data['mapInfo']['zones'][0]['remain'] = 0
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        self.assertTrue(earn(turn, cfg, mem, nav, ledger, turn.units[1]))
        self.assertEqual(ledger.commands['1'], command('sell', name='copper', num=10))

    def test_new_mining_cycle_includes_assigned_return_not_just_sale(self):
        data = payload(449, [unit(1, 'worker', 1, 3, health=500), unit(13, 'station', 1, 7),
                             *[unit(60+i, 'rocket', 10+i, 10, level=3) for i in range(3)]])
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=2, y=3), remain=10),
                                  dict(neutralType='vendor', pos=dict(x=3, y=3))]
        data['vendorShopList'] = [dict(name='copper', price=10)]
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', return_margin=5)
        ledger.operator_posts[1] = (14, 14)
        self.assertFalse(mine(turn, cfg, Memory(sold_workers={1}), nav, ledger, turn.units[1]))
        self.assertFalse(ledger.commands)


if __name__ == '__main__':
    unittest.main()
