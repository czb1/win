"""Focused driving legality, travel costs and task integration boundaries."""
import unittest
from unittest.mock import patch

from test_agent import payload, setup_case, unit
from test_task_schedule import task
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import pioneer, prepare_treasure
from agent.intelligence import Memory
from agent.model import pos
from agent.movement import MovementMemory
from agent.navigation import DeadlineExceeded
from agent.vehicle import driving_segment, faster_route


def case(start=(1, 5), car=(2, 5), driving=False, kind='pioneer', round_no=10):
    data = payload(round_no, [unit(11, kind, *start, isDriving=driving)])
    if car is not None:
        data['mapInfo']['zones'].append(dict(neutralType='challengerVehicle',
                                            pos=dict(x=car[0], y=car[1])))
    return data


def travel(data, goal=(10, 5), reserved=()):
    t, c, n, l = setup_case(data, layout_mode='explicit')
    h = t.units[11]
    route = n.search(h, {goal}, reserved)
    best = faster_route(n, l, h, {goal}, route, reserved)
    return t, c, n, l, route, best


class VehicleTests(unittest.TestCase):
    def test_pickup_only_when_total_trip_is_shorter(self):
        t, _, n, l, walking, faster = travel(case())
        self.assertEqual(faster[0], 5)  # one boarding action, four driving actions
        self.assertEqual(faster[1], (2, 5))
        self.assertLess(faster[0], walking[0])
        self.assertTrue(n.move_along(t.pioneer, l, walking, {(10, 5)}))
        self.assertEqual(pos(l.commands['11']['targetPos'][0]), (2, 5))

    def test_equal_cost_and_long_detour_keep_original_move(self):
        for data, goal in ((case(), (3, 5)), (case(car=(1, 13)), (5, 5))):
            t, _, n, l, walking, faster = travel(data, goal)
            self.assertIsNone(faster)
            self.assertTrue(n.move_along(t.pioneer, l, walking, {goal}))
            self.assertEqual(pos(l.commands['11']['targetPos'][0]), walking[1])

    def test_already_driving_two_cell_action_reserves_both_cells(self):
        t, _, n, l, walking, faster = travel(case(start=(2, 5), driving=True))
        self.assertEqual(faster[0], 4)
        self.assertTrue(n.move_along(t.pioneer, l, walking, {(10, 5)}))
        self.assertTrue(set(driving_segment(t.pioneer.pos, faster[1])) <= l.reserved)

    def test_one_step_exit_cannot_be_followed_by_imaginary_driving(self):
        # A one-cell-wide bent passage forces an early exit. It cannot be
        # priced as ceil(walking_distance/2) after leaving the car.
        data = case(start=(1, 1), car=(1, 1), driving=True)
        allowed = {(1, 1), (2, 1), (2, 2), (2, 3), (2, 4), (2, 5)}
        data['mapInfo']['zones'] += [dict(neutralType='stone', pos=dict(x=x, y=y))
                                    for x in range(15) for y in range(15)
                                    if (x, y) not in allowed]
        self.assertIsNone(travel(data, (2, 5))[-1])
        t, _, _, l = setup_case(case(start=(2, 5), driving=True))
        self.assertTrue(l.add(11, command('move', (3, 5))))
        data = case(start=(3, 5), driving=False)
        _, _, _, l = setup_case(data)
        self.assertFalse(l.add(11, command('move', (5, 5))))

    def test_blocked_middle_or_landing_is_rejected(self):
        for cell in ((3, 5), (4, 5)):
            data = case(start=(2, 5), driving=True)
            data['teamOur']['roles'].append(unit(12, 'worker', *cell))
            _, _, _, l = setup_case(data)
            self.assertFalse(l.add(11, command('move', (4, 5))))
            _, _, _, l = setup_case(case(start=(2, 5), driving=True))
            l.reserved.add(cell)
            self.assertFalse(l.add(11, command('move', (4, 5))))

    def test_prior_and_later_friendly_moves_cannot_share_driving_path(self):
        data = case(start=(2, 5), driving=True)
        data['teamOur']['roles'].append(unit(12, 'worker', 3, 6))
        for first_driver in (False, True):
            _, _, _, l = setup_case(data)
            commands = [(11, command('move', (4, 5))), (12, command('move', (3, 5)))]
            if not first_driver:
                commands.reverse()
            self.assertTrue(l.add(*commands[0]))
            self.assertFalse(l.add(*commands[1]))

    def test_only_pioneer_daytime_with_observed_car_can_drive(self):
        for data in (case(start=(2, 5), driving=True, kind='worker'),
                     case(start=(2, 5), driving=True, kind='imp'),
                     case(start=(2, 5), driving=True, round_no=70),
                     case(start=(2, 5), driving=True, car=None),
                     case(start=(2, 5), driving=False)):
            _, _, _, l = setup_case(data)
            self.assertFalse(l.add(11, command('move', (4, 5))))
            self.assertIsNone(travel(data)[-1])

    def test_no_car_worker_and_night_preserve_original_route(self):
        for data in (case(car=None), case(kind='worker'), case(round_no=70)):
            t, _, n, l, walking, faster = travel(data)
            self.assertIsNone(faster)
            self.assertTrue(n.move_along(t.units[11], l, walking, {(10, 5)}))
            self.assertEqual(l.commands['11'], command('move', walking[1]))

    def test_enemy_car_and_occupied_own_car_cannot_be_boarded(self):
        for enemy in (False, True):
            data = case()
            if enemy:
                data['mapInfo']['zones'][0]['neutralType'] = 'defenderVehicle'
            else:
                data['teamOur']['roles'].append(unit(12, 'worker', 2, 5, isDriving=True))
            _, _, _, l = setup_case(data)
            self.assertFalse(l.add(11, command('move', (2, 5))))
            self.assertIsNone(travel(data)[-1])

    def test_vehicle_is_still_a_building_obstacle(self):
        data = case(kind='worker')
        _, _, _, l = setup_case(data, layout_mode='explicit', weapon_cells=[(2, 5)])
        self.assertFalse(l.add(11, command('build', (2, 5), name='rocket')))

    def test_safety_mask_and_movement_failures_are_preserved(self):
        t, _, n, l, walking, _ = travel(case())
        self.assertIsNone(faster_route(n, l, t.pioneer, {(10, 5)}, walking, {(2, 5)}))
        n.memory = MovementMemory(failures={(11, (2, 5)): 20})
        self.assertIsNone(faster_route(n, l, t.pioneer, {(10, 5)}, walking))

    def test_no_car_trip_extends_past_daylight(self):
        self.assertIsNone(travel(case(round_no=68))[-1])

    def test_operator_goal_requires_parking_outside_post(self):
        t, _, n, l, walking, _ = travel(case(start=(2, 5), driving=True))
        l.operator_posts[11] = (10, 5)
        faster = faster_route(n, l, t.pioneer, {(10, 5)}, walking)
        self.assertEqual(faster[0], 5)  # four drives and one exit, not four drives

    def test_driving_rejects_turning_and_outside_map_commands(self):
        for target in ((4, 6), (-1, 5), (5, 5)):
            _, _, _, l = setup_case(case(start=(2, 5), driving=True))
            self.assertFalse(l.add(11, command('move', target)))

    def test_task_target_and_budget_unchanged_but_travel_can_board(self):
        data = case()
        data['teamOur']['playerTasks'] = [task(x=12)]
        t, c, n, l = setup_case(data, layout_mode='explicit')
        mem = Memory()
        pioneer(t, c, mem, n, l, t.pioneer, shopping=False)
        self.assertEqual(mem.task_target, (12, 5))
        self.assertEqual(pos(l.commands['11']['targetPos'][0]), (2, 5))
        self.assertIsNone(mem.accepted_round)

    def test_task_on_car_accepts_without_dismounting(self):
        data = case(start=(2, 5), driving=True)
        data['teamOur']['playerTasks'] = [task(x=3)]
        t, c, n, l = setup_case(data, layout_mode='explicit')
        pioneer(t, c, Memory(), n, l, t.pioneer, shopping=False)
        self.assertEqual(l.commands['11']['action'], 'acceptTask')

    def test_active_task_never_starts_a_vehicle_trip(self):
        data = case(start=(2, 5), driving=True)
        data['phaseTask'] = 'Return the integer 1.'
        data['teamOur']['playerTasks'] = [task(x=3)]
        agent = Agent(Config(layout_mode='explicit'))
        with patch('agent.vehicle.faster_route', wraps=faster_route) as planner:
            response = agent.decide(data)
        self.assertNotEqual(response['roleCommandMap'].get('11', {}).get('action'), 'move')
        planner.assert_not_called()

    def test_selected_treasure_shop_trip_can_board(self):
        data = case(round_no=140)
        data['mapInfo']['zones'].append(dict(neutralType='weaponShop', pos=dict(x=12, y=5)))
        data['weaponShopList'] = [dict(name='arbitrary_material', price=1)]
        t, c, n, l = setup_case(data, layout_mode='explicit')
        mem = Memory(treasure=dict(items=['arbitrary_material'], position=[12, 10],
                                   startRound=150, endRound=180))
        self.assertTrue(prepare_treasure(t, c, mem, n, l, t.pioneer))
        self.assertEqual(pos(l.commands['11']['targetPos'][0]), (2, 5))

    def test_observed_board_drive_and_exit_sequence(self):
        # A small protocol state transition check, not a match simulator.
        t, _, n, l, walking, _ = travel(case())
        self.assertTrue(n.move_along(t.pioneer, l, walking, {(10, 5)}))
        self.assertEqual(pos(l.commands['11']['targetPos'][0]), (2, 5))
        observed = case(start=(2, 5), driving=True, round_no=11)
        t, _, n, l, walking, _ = travel(observed, (6, 5))
        self.assertTrue(n.move_along(t.pioneer, l, walking, {(6, 5)}))
        destination = pos(l.commands['11']['targetPos'][0])
        self.assertIsNotNone(driving_segment((2, 5), destination))
        observed = case(start=destination, car=destination, driving=True, round_no=12)
        exit_cell = destination[0] + 1, destination[1]
        t, _, n, l, walking, _ = travel(observed, exit_cell)
        self.assertTrue(n.move_along(t.pioneer, l, walking, {exit_cell}))
        self.assertEqual(l.commands['11'], command('move', exit_cell))
        observed = case(start=exit_cell, car=destination, driving=False, round_no=13)
        _, _, _, l = setup_case(observed)
        self.assertFalse(l.add(11, command('move', (exit_cell[0] + 2, exit_cell[1]))))

    def test_observed_vehicle_disappearance_reverts_to_walking(self):
        t, _, n, l, walking, faster = travel(case(start=(2, 5), car=None, driving=True))
        self.assertIsNone(faster)
        self.assertTrue(n.move_along(t.pioneer, l, walking, {(10, 5)}))
        self.assertEqual(l.commands['11'], command('move', walking[1]))

    def test_search_timeout_keeps_valid_original_action(self):
        t, _, n, l, walking, _ = travel(case())
        with patch('agent.vehicle.faster_route', side_effect=DeadlineExceeded):
            self.assertTrue(n.move_along(t.pioneer, l, walking, {(10, 5)}))
        self.assertEqual(l.commands['11'], command('move', walking[1]))

    def test_defender_uses_only_its_own_vehicle(self):
        data = case()
        data['teamOur']['type'] = 'defender'
        self.assertIsNone(travel(data)[-1])
        data['mapInfo']['zones'][0]['neutralType'] = 'defenderVehicle'
        self.assertEqual(travel(data)[-1][1], (2, 5))

    def test_parked_vehicle_avoids_blueprint_and_gate(self):
        t, _, n, l, walking, _ = travel(case(start=(2, 5), driving=True))
        # An exact goal on a future wall is reachable only after a safe exit.
        l.wall_cells = {(10, 5)}
        self.assertEqual(faster_route(n, l, t.pioneer, {(10, 5)}, walking)[0], 5)
        l.wall_cells = {(x, y) for x in range(8, 13) for y in range(3, 8)
                        if (x in (8, 12) or y in (3, 7)) and (x, y) != (8, 5)}
        walking = n.search(t.pioneer, {(8, 5)})
        best = faster_route(n, l, t.pioneer, {(8, 5)}, walking)
        self.assertIsNotNone(best)
        self.assertEqual(best[0], 4)  # cannot park on the gate in three drives


if __name__ == '__main__':
    unittest.main()
