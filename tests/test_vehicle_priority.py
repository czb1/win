"""Transport preference and per-turn protocol transitions, without match simulation."""
import unittest
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from test_agent import setup_case, unit
from test_vehicle import case
from agent.commands import command
from agent.intelligence import Memory
from agent.model import pos
from agent.movement import MovementMemory
from agent.navigation import Navigator, DeadlineExceeded
from agent.vehicle import VehicleMemory, driving_segment, home_area, plan_route, take_idle_vehicle
from agent.economy import visit


class VehiclePriorityTests(unittest.TestCase):
    def setup(self, data=None, memory=None, **options):
        t, c, _, l = setup_case(data or case(), layout_mode='explicit', **options)
        n = Navigator(t, monotonic() + 3, memory or MovementMemory())
        return t, c, n, l

    def move(self, data, goal, memory=None, **options):
        t, c, n, l = self.setup(data, memory)
        route = n.search(t.pioneer, {goal})
        self.assertTrue(n.move_along(t.pioneer, l, route, {goal}, **options))
        return t, c, n, l

    def test_pickup_even_when_detour_is_slower(self):
        t, c, n, l = self.move(case(car=(1, 8)), (5, 5))
        target = pos(l.commands['11']['targetPos'][-1])
        self.assertEqual(target[1], 6)  # heads toward the car, not the goal

    def test_explicit_deadline_keeps_direct_walk(self):
        t, c, n, l = self.move(case(car=(1, 13)), (5, 5), max_rounds=4)
        self.assertEqual(l.commands['11'], command('move', n.search(t.pioneer, {(5, 5)})[1]))

    def test_urgent_trip_prefers_car_within_daylight(self):
        t, c, n, l = self.move(case(car=(1, 13)), (5, 5), prefer_vehicle=False)
        self.assertEqual(pos(l.commands['11']['targetPos'][-1])[1], 6)

    def test_end_of_day_does_not_fetch_remote_car(self):
        t, c, n, l = self.move(case(car=(1, 13), round_no=67), (5, 5))
        self.assertEqual(l.commands['11'], command('move', n.search(t.pioneer, {(5, 5)})[1]))

    def test_boarding_needs_observed_flag(self):
        memory = MovementMemory()
        t, c, n, l = self.move(case(), (10, 5), memory)
        observed = case(start=(2, 5), driving=False, round_no=11)
        t, c, n, l = self.setup(observed, memory)
        self.assertFalse(l.add(11, command('move', (4, 5))))

    def test_dismount_walk_finishes_before_reboarding(self):
        memory = MovementMemory()
        t, c, n, l = self.move(case(start=(2, 5), driving=True), (5, 6), memory,
                              journey=('business', (5, 6)))
        for number in range(11, 17):
            target = pos(l.commands['11']['targetPos'][-1])
            was_driving = t.pioneer.is_driving
            moved_car = was_driving and driving_segment(t.pioneer.pos, target) is not None
            car = target if moved_car else next(iter(t.vehicle_cells))
            data = case(start=target, car=car, driving=moved_car, round_no=number)
            old_commands = l.commands
            t, c, n, l = self.setup(data, memory)
            memory.vehicle.observe(t, SimpleNamespace(last_round=number-1, last_commands=old_commands))
            if target == (5, 6):
                break
            route = n.search(t.pioneer, {(5, 6)})
            n.move_along(t.pioneer, l, route, {(5, 6)}, journey=('business', (5, 6)))
            if not moved_car:
                self.assertNotEqual(pos(l.commands['11']['targetPos'][-1]), car)
        else:
            self.fail('did not finish the pedestrian tail')
        # A completed purchase releases the old leg and the same car is reusable.
        memory.vehicle.observe(t, SimpleNamespace(last_round=t.round-1,
                                                  last_commands={'11': {'action': 'buy'}}))
        self.assertFalse(memory.vehicle.walking)
        goal = (12, 12)
        route = n.search(t.pioneer, {goal})
        self.assertTrue(n.move_along(t.pioneer, l, route, {goal}, journey=('next', goal)))
        self.assertEqual(memory.vehicle.journey, ('next', goal))
        self.assertFalse(memory.vehicle.walking)
        self.assertEqual(pos(l.commands['11']['targetPos'][-1]), car)

    def test_worker_movement_cannot_reset_pioneer_tail(self):
        data = case(start=(3, 5))
        data['teamOur']['roles'].append(unit(12, 'worker', 10, 10))
        memory = MovementMemory(vehicle=VehicleMemory(journey='shop', walking=True))
        t, c, n, l = self.setup(data, memory)
        worker = t.units[12]
        n.move_along(worker, l, n.search(worker, {(11, 10)}), {(11, 10)})
        self.assertTrue(memory.vehicle.walking)
        self.assertEqual(memory.vehicle.journey, 'shop')

    def test_buy_in_car_without_dismount(self):
        data = case(start=(2, 5), driving=True)
        data['mapInfo']['zones'].append(dict(neutralType='weaponShop', pos=dict(x=3, y=5)))
        data['weaponShopList'] = [dict(name='Medicine', price=10)]
        t, c, n, l = self.setup(data)
        self.assertTrue(visit(t, n, l, t.pioneer, 'weaponShop', command('buy', name='Medicine')))
        self.assertEqual(l.commands['11']['action'], 'buy')

    def test_shop_can_use_another_adjacent_cell_to_stay_onboard(self):
        data = case(start=(2, 5), driving=True)
        data['mapInfo']['zones'].append(dict(neutralType='weaponShop', pos=dict(x=5, y=6)))
        t, c, n, l = self.setup(data)
        # Pedestrian endpoint has opposite parity. A different trade cell
        # is reachable directly by car and still satisfies the shop action.
        route = n.search(t.pioneer, {(4, 6)})
        n.move_along(t.pioneer, l, route, {(4, 6)}, targets={(5, 6)})
        self.assertIsNotNone(driving_segment(t.pioneer.pos, pos(l.commands['11']['targetPos'][-1])))

    def test_parking_and_driving_remain_outside_incomplete_wall_ring(self):
        walls = {(x, y) for x in range(6, 11) for y in range(3, 8)
                 if (x in (6, 10) or y in (3, 7)) and (x, y) != (6, 5)}
        data = case(start=(2, 5), car=(2, 5), driving=True)
        data['teamOur']['roles'] += [unit(100 + i, 'wall', *p) for i, p in enumerate(sorted(walls))]
        t, c, n, l = self.setup(data, wall_cells=sorted(walls))
        goals = {(8, 5)}
        route = n.search(t.pioneer, goals)
        chosen = plan_route(n, l, t.pioneer, goals, route)
        self.assertIsNotNone(chosen)
        self.assertNotIn(chosen.parking, home_area(t, l))
        self.assertNotIn(chosen.parking, {(5, 4), (5, 5), (5, 6)})  # outside gate access
        segment = driving_segment(t.pioneer.pos, chosen.target)
        if segment:
            self.assertFalse(set(segment) & home_area(t, l))
        # Construction gaps do not become permission to drive into the base.
        data['teamOur']['roles'] = data['teamOur']['roles'][:1]
        t, c, n, l = self.setup(data, wall_cells=sorted(walls))
        chosen = plan_route(n, l, t.pioneer, goals, n.search(t.pioneer, goals))
        self.assertNotIn(chosen.parking, home_area(t, l))

    def test_home_destination_on_foot_fetches_car_and_parks_outside(self):
        t, c, n, l = self.setup(case(), wall_cells=[(6, 3), (10, 7)])
        route = n.search(t.pioneer, {(8, 5)})
        chosen = plan_route(n, l, t.pioneer, {(8, 5)}, route)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.target, (2, 5))
        self.assertNotIn(chosen.parking, home_area(t, l))

    def test_defence_recall_fetches_car_even_when_slower_than_walking(self):
        t, c, n, l = self.setup(case(start=(5, 5), car=(2, 5), round_no=60),
                                wall_cells=[(6, 3), (10, 7)])
        goal = (8, 5)
        l.plans[11] = {'reason': 'defence_recall'}
        l.operator_posts[11] = goal
        route = n.search(t.pioneer, {goal})
        chosen = plan_route(n, l, t.pioneer, {goal}, route)
        self.assertIsNotNone(chosen)
        self.assertGreater(chosen.rounds, route[0])
        self.assertTrue(n.move_along(t.pioneer, l, route, {goal}))
        self.assertEqual(pos(l.commands['11']['targetPos'][-1]), chosen.target)
        self.assertNotEqual(chosen.target, route[1])

    def test_defence_recall_without_car_keeps_walking(self):
        t, c, n, l = self.setup(case(car=None, round_no=60),
                                wall_cells=[(6, 3), (10, 7)])
        goal = (8, 5)
        l.plans[11] = {'reason': 'defence_recall'}
        route = n.search(t.pioneer, {goal})
        self.assertTrue(n.move_along(t.pioneer, l, route, {goal}))
        self.assertEqual(l.commands['11'], command('move', route[1]))

    def late_return_case(self, round_no=63, driving=True):
        # Geometry from the reported return: car at (25,19), post at (8,22).
        data = case(start=(25, 19), car=(25, 19), driving=driving, round_no=round_no)
        data['mapInfo'].update(width=41, height=32)
        walls = [(12, y) for y in range(19, 25)] + [(11, 24), (11, 19), (9, 19), (8, 19)]
        data['teamOur']['roles'] += [unit(13, 'station', 9, 22)]
        data['teamOur']['roles'] += [unit(100+i, 'wall', *p) for i, p in enumerate(walls)]
        data['teamOur']['roles'] += [unit(40+i, 'rocket', *p, cooldown=0)
                                    for i, p in enumerate([(8, 23), (8, 21), (9, 23)])]
        return data

    def test_late_onboard_return_uses_real_gunner_call_and_two_coordinates(self):
        from agent.combat import shared_defend
        t, c, n, l = self.setup(self.late_return_case())
        mem = Memory()
        mem.gunner_id, mem.gunner_post = 11, (8, 22)
        sites = [w.pos for w in t.weapons]
        l.operator_posts[11] = mem.gunner_post
        walking = n.search(t.pioneer, {mem.gunner_post})
        self.assertEqual(walking[0], 19)
        self.assertEqual(t.day_left, 7)
        self.assertIsNone(plan_route(n, l, t.pioneer, {mem.gunner_post}, walking))
        selected = plan_route(n, l, t.pioneer, {mem.gunner_post}, walking, returning=True)
        self.assertIsNotNone(selected)
        self.assertGreater(selected.rounds, t.day_left)
        self.assertLessEqual(selected.rounds - selected.walking_tail, t.day_left)
        self.assertNotIn(selected.parking, home_area(t, l))
        shared_defend(t, n, l, mem, [(t.pioneer, t.weapons[0])], sites)
        segment = l.commands['11']['targetPos']
        self.assertEqual(len(segment), 2)
        self.assertEqual(tuple(map(pos, segment)), driving_segment(t.pioneer.pos, pos(segment[-1])))
        self.assertIsNone(n.vehicle.pending_exit)

    def test_last_daylight_return_drives_but_night_walks(self):
        t, c, n, l = self.setup(self.late_return_case(round_no=69))
        goal = (8, 22)
        route = n.search(t.pioneer, {goal})
        selected = plan_route(n, l, t.pioneer, {goal}, route, returning=True)
        self.assertEqual(selected.rounds - selected.walking_tail, 1)
        n.move_along(t.pioneer, l, route, {goal}, returning=True)
        self.assertEqual(len(l.commands['11']['targetPos']), 2)
        endpoint = pos(l.commands['11']['targetPos'][-1])
        data = self.late_return_case(round_no=70, driving=False)
        data['teamOur']['roles'][0]['pos'] = dict(x=endpoint[0], y=endpoint[1])
        data['mapInfo']['zones'] = []
        t, c, n, l = self.setup(data)
        n.move_along(t.pioneer, l, n.search(t.pioneer, {goal}), {goal}, returning=True)
        self.assertEqual(len(l.commands['11']['targetPos']), 1)

    def test_late_pedestrian_return_boards_adjacent_car_through_defend(self):
        from agent.combat import defend
        data = self.late_return_case(round_no=65, driving=False)
        data['teamOur']['roles'][0]['pos'] = dict(x=24, y=19)
        data['mapInfo']['zones'][0]['pos'] = dict(x=23, y=18)
        t, c, n, l = self.setup(data)
        goal = (8, 22)
        l.operator_posts[11] = goal
        l.plans[11] = {'reason': 'defence_recall'}
        walking = n.search(t.pioneer, {goal})
        self.assertGreater(walking[0], t.day_left)
        selected = plan_route(n, l, t.pioneer, {goal}, walking, returning=True)
        self.assertIsNotNone(selected)
        self.assertGreater(selected.rounds, t.day_left)
        self.assertLessEqual(selected.rounds - selected.walking_tail, t.day_left)
        self.assertNotIn(selected.parking, home_area(t, l))
        defend(t, n, l, pairs=[(t.pioneer, t.weapons[0])], posts={11: goal})
        self.assertEqual(l.commands['11'], command('move', (23, 18)))

    def test_last_daylight_pedestrian_return_boards_then_night_walks(self):
        memory = MovementMemory()
        goal = (12, 5)
        t, c, n, l = self.setup(case(round_no=69), memory)
        n.move_along(t.pioneer, l, n.search(t.pioneer, {goal}), {goal}, returning=True)
        self.assertEqual(l.commands['11'], command('move', (2, 5)))
        previous = l.commands
        t, c, n, l = self.setup(case(start=(2, 5), car=None, round_no=70), memory)
        memory.vehicle.observe(t, SimpleNamespace(last_round=69, last_commands=previous))
        self.assertIsNone(memory.vehicle.journey)
        walking = n.search(t.pioneer, {goal})
        n.move_along(t.pioneer, l, walking, {goal}, returning=True)
        self.assertEqual(l.commands['11'], command('move', walking[1]))
        self.assertEqual(len(l.commands['11']['targetPos']), 1)

    def test_late_return_cannot_fetch_car_before_it_disappears(self):
        t, c, n, l = self.setup(case(car=(1, 13), round_no=67))
        goal = (5, 5)
        walking = n.search(t.pioneer, {goal})
        self.assertIsNone(plan_route(n, l, t.pioneer, {goal}, walking, returning=True))
        n.move_along(t.pioneer, l, walking, {goal}, returning=True)
        self.assertEqual(l.commands['11'], command('move', walking[1]))

    def test_late_return_exception_does_not_extend_business_trip(self):
        for driving, returning in [(False, False), (True, False)]:
            with self.subTest(driving=driving, returning=returning):
                data = case(start=(2, 5) if driving else (1, 5), driving=driving, round_no=69)
                t, c, n, l = self.setup(data)
                goal = (12, 5)
                route = n.search(t.pioneer, {goal})
                self.assertIsNone(plan_route(n, l, t.pioneer, {goal}, route, returning=returning))
                n.move_along(t.pioneer, l, route, {goal}, returning=returning)
                self.assertEqual(l.commands['11'], command('move', route[1]))

    def test_late_return_at_outside_parking_dismounts_and_keeps_foot_tail(self):
        memory = MovementMemory()
        t, c, n, l = self.setup(case(start=(4, 5), car=(4, 5), driving=True, round_no=69),
                                memory, wall_cells=[(6, 3), (10, 7)])
        goal = (8, 5)
        n.move_along(t.pioneer, l, n.search(t.pioneer, {goal}), {goal}, returning=True)
        self.assertEqual(len(l.commands['11']['targetPos']), 1)
        endpoint = pos(l.commands['11']['targetPos'][-1])
        self.assertEqual(memory.vehicle.pending_exit, endpoint)
        next_turn, _, _, _ = self.setup(case(start=endpoint, car=(4, 5), round_no=69))
        memory.vehicle.observe(next_turn, SimpleNamespace(last_round=68, last_commands=l.commands))
        self.assertTrue(memory.vehicle.walking)

    def test_late_return_without_drive_edge_can_exit_on_foot(self):
        data = case(start=(1, 1), car=(1, 1), driving=True, round_no=69)
        allowed = {(1, 1), (2, 1), (2, 2), (2, 3), (2, 4), (2, 5)}
        data['mapInfo']['zones'] += [dict(neutralType='stone', pos=dict(x=x, y=y))
                                    for x in range(15) for y in range(15) if (x, y) not in allowed]
        t, c, n, l = self.setup(data)
        goal = (2, 5)
        n.move_along(t.pioneer, l, n.search(t.pioneer, {goal}), {goal}, returning=True)
        self.assertEqual(len(l.commands['11']['targetPos']), 1)

    def test_idle_pickup_and_onboard_wait(self):
        for driving in (False, True):
            data = case(start=(2, 5) if driving else (1, 5), driving=driving)
            t, c, n, l = self.setup(data)
            self.assertTrue(take_idle_vehicle(t, c, Memory(), n, l, t.pioneer))
            if driving:
                self.assertNotIn('11', l.commands)
                self.assertIn(11, l.used)
            else:
                self.assertEqual(l.commands['11'], command('move', (2, 5)))

    def test_recall_and_active_task_never_use_idle_pickup(self):
        for active in (False, True):
            data = case()
            mem = Memory()
            if active:
                data['phaseTask'] = 'do not leave task range'
            else:
                mem.return_targets[11] = 42
            t, c, n, l = self.setup(data)
            self.assertFalse(take_idle_vehicle(t, c, mem, n, l, t.pioneer))
            self.assertFalse(l.commands)

    def test_state_clears_when_car_disappears_or_night_or_death(self):
        for data in (case(car=None), case(round_no=70), case(round_no=140)):
            t, c, n, l = self.setup(data)
            state = VehicleMemory(journey='shop', walking=True, day=1, actor=11)
            state.observe(t, SimpleNamespace(last_round=t.round-1, last_commands={}))
            self.assertIsNone(state.journey)
        data = case()
        data['teamOur']['roles'] = []
        t, c, n, l = self.setup(data)
        state = VehicleMemory(journey='shop', walking=True, day=1, actor=11)
        state.observe(t, SimpleNamespace(last_round=t.round-1, last_commands={}))
        self.assertIsNone(state.journey)

    def test_search_timeout_falls_back_to_original_step(self):
        t, c, n, l = self.setup()
        route = n.search(t.pioneer, {(10, 5)})
        with patch('agent.vehicle.plan_route', side_effect=DeadlineExceeded):
            self.assertTrue(n.move_along(t.pioneer, l, route, {(10, 5)}))
        self.assertEqual(l.commands['11'], command('move', route[1]))

    def test_first_day_shop_trip_uses_vehicle_and_keeps_purchase_phase(self):
        from test_first_day_pioneer_shopping import FirstDayPioneerShoppingTests
        from test_robot_assault import fortified_case
        from agent.spending import prepare_first_day_pioneer
        data = FirstDayPioneerShoppingTests().data(round_no=51)
        data['teamOur']['roles'][2]['pos'] = dict(x=8, y=8)
        data['mapInfo']['zones'].append(dict(neutralType='challengerVehicle', pos=dict(x=8, y=9)))
        t, c, n, l = fortified_case(data)
        mem = Memory()
        l.return_pairs = [(t.pioneer, t.weapons[0])]
        l.operator_posts = {t.pioneer.id: (1, 9)}
        self.assertTrue(prepare_first_day_pioneer(t, c, mem, n, l))
        self.assertEqual(mem.first_day_pioneer['phase'], 'shop')
        self.assertEqual(pos(l.commands[str(t.pioneer.id)]['targetPos'][-1]), (8, 9))

    def test_full_return_leg_never_drives_inside_or_reboards_after_exit(self):
        walls = {(x, y) for x in range(6, 11) for y in range(3, 8)
                 if (x in (6, 10) or y in (3, 7)) and (x, y) != (6, 5)}
        memory, start, car, driving = MovementMemory(), (2, 5), (2, 5), True
        exited = False
        for number in range(10, 30):
            data = case(start=start, car=car, driving=driving, round_no=number)
            data['teamOur']['roles'] += [unit(100+i, 'wall', *p) for i, p in enumerate(sorted(walls))]
            t, c, n, l = self.setup(data, memory, wall_cells=sorted(walls))
            goal = (8, 5)
            if start == goal:
                self.assertTrue(exited)
                self.assertNotIn(car, home_area(t, l))
                return
            route = n.search(t.pioneer, {goal})
            self.assertIsNotNone(route)
            n.move_along(t.pioneer, l, route, {goal}, journey='return')
            target = pos(l.commands['11']['targetPos'][-1])
            segment = driving_segment(start, target) if driving else None
            if segment:
                self.assertFalse(set(segment) & home_area(t, l))
                car = target
            else:
                if driving:
                    exited = True
                self.assertNotEqual(target, car)
                driving = False
            start = target
            next_data = case(start=start, car=car, driving=driving, round_no=number+1)
            next_turn, _, _, _ = self.setup(next_data)
            memory.vehicle.observe(next_turn, SimpleNamespace(last_round=number, last_commands=l.commands))
        self.fail('return leg failed to reach the interior post')
