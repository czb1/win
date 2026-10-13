"""Ordered driving path and observed execution, independent of route selection."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_agent import setup_case
from test_vehicle import case, travel
from agent.commands import command, move_command
from agent.diagnostics import observe
from agent.intelligence import Memory
from agent.vehicle import take_idle_vehicle


class VehicleProtocolTests(unittest.TestCase):
    def test_straight_and_diagonal_wire_coordinates(self):
        for destination, middle in (((4, 5), (3, 5)), ((4, 7), (3, 6))):
            with self.subTest(destination=destination):
                t, _, _, ledger = setup_case(case(start=(2, 5), driving=True))
                cmd = move_command(t.pioneer, destination)
                self.assertEqual(cmd, {'action': 'move', 'targetPos': [
                    {'x': middle[0], 'y': middle[1]},
                    {'x': destination[0], 'y': destination[1]}]})
                self.assertTrue(ledger.add(11, cmd))
                self.assertTrue({middle, destination} <= ledger.reserved)

    def test_single_far_endpoint_and_malformed_paths_are_rejected(self):
        paths = [[(4, 5)], [], [(3, 5), (4, 5), (5, 5)],
                 [(4, 5), (3, 5)], [(3, 6), (4, 5)], [(3, 5), (3, 5)],
                 [(3, 5), (4, 6)]]
        for path in paths:
            with self.subTest(path=path):
                _, _, _, ledger = setup_case(case(start=(2, 5), driving=True))
                self.assertFalse(ledger.add(11, {'action': 'move', 'targetPos': [
                    dict(x=x, y=y) for x, y in path]}))
                self.assertFalse(ledger.reserved)
                self.assertFalse(ledger.used)

    def test_board_and_exit_still_have_one_coordinate(self):
        for start, target, driving in (((1, 5), (2, 5), False), ((2, 5), (3, 5), True)):
            t, _, _, ledger = setup_case(case(start=start, driving=driving))
            cmd = move_command(t.pioneer, target)
            self.assertEqual(len(cmd['targetPos']), 1)
            self.assertTrue(ledger.add(11, cmd))

    def test_both_driving_cells_obey_traffic_reservations(self):
        for cell in ((3, 5), (4, 5)):
            t, _, _, ledger = setup_case(case(start=(2, 5), driving=True))
            ledger.traffic_forbidden[11] = {cell}
            self.assertFalse(ledger.add(11, move_command(t.pioneer, (4, 5))))

    def test_feedback_distinguishes_end_middle_and_no_progress(self):
        for landed, effect in (((4, 5), 'target_observed'),
                               ((3, 5), 'intermediate_observed'),
                               ((2, 5), 'target_not_observed')):
            with self.subTest(landed=landed):
                old, _, _, _ = setup_case(case(start=(2, 5), driving=True))
                cmd = move_command(old.pioneer, (4, 5))
                new, _, _, _ = setup_case(case(start=landed, car=landed, driving=True, round_no=11))
                previous = dict(roles={str(r['id']): r for r in old.raw['teamOur']['roles']},
                                commands={'11': cmd}, request_id='test', gold=0, score=0)
                memory = SimpleNamespace(log_history={10: previous})
                with patch('agent.diagnostics.emit_event') as emit:
                    observe(new, memory)
                feedback = next(call.args[1] for call in emit.call_args_list
                                if call.args[0] == 'previous_feedback')
                self.assertEqual(feedback['actions'][0]['effect'], effect)

    def test_disabled_driver_does_not_idle_reboard(self):
        t, cfg, nav, ledger, _, _ = travel(case())
        mem = Memory()
        nav.memory = mem.movement
        mem.movement.driving_disabled.add(11)
        self.assertFalse(take_idle_vehicle(t, cfg, mem, nav, ledger, t.pioneer))
        self.assertFalse(ledger.commands)
