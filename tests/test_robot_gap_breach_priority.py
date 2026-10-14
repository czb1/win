"""Finite observations of the r71-r72 switch from a base route to a side wall."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_assault_bypass import reflect
from test_robot_assault import controlled_robot
from test_robot_gap_clearance import decide, gap_case, ENTRY as OTHER_ENTRY
from agent.commands import command
from agent.robot_assault import (AssaultRoute, RobotAssaultMemory, _act_toward_target,
                                 _gap_region, _occupants, enemy_base)


ENTRY = (28, 9)


def reported_case():
    # Only the relevant observed geometry; no raw logs or account metadata.
    data = payload(72, [unit(10013, 'station', 9, 22),
                        unit(10010, 'worker', 26, 7, health=500)])
    data['mapInfo'].update(width=41, height=32)
    data['teamEnemy']['roles'] = [
        unit(20013, 'station', 30, 10, health=1500),
        unit(20010, 'worker', 29, 9, health=500),
        unit(20012, 'worker', 27, 9, health=500),
    ]
    data['teamEnemy']['roles'] += [unit(41000+i, 'wall', *p, health=1000)
        for i, p in enumerate(((28, 7), (29, 7), (30, 7), (28, 8)))]
    data['teamOur']['summonRobotList'] = [controlled_robot(30035, 28, 6),
                                        controlled_robot(30036, 28, 9)]
    return data


class RobotGapBreachPriorityTests(unittest.TestCase):
    def test_worker_screen_does_not_cancel_confirmed_short_base_route(self):
        previous = reported_case()
        previous['roundNo'] = 71
        previous['teamEnemy']['roles'][1]['pos'] = dict(x=28, y=10)
        for robot, point in zip(previous['teamOur']['summonRobotList'], ((27, 7), (27, 10))):
            robot['pos'] = dict(zip(('x', 'y'), point))
        ledger, memory = decide(previous, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('move', (28, 6)))
        self.assertEqual(memory.routes[30035].goal, (31, 6))

        observed = reported_case()
        observed['lastRoundRoleActionResults'] = {'30035': True, '30036': True}
        ledger, _ = decide(observed, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('move', (29, 6)))
        facts = ledger.notes[30035]['conditions']
        self.assertEqual(facts['target_id'], 20013)
        self.assertEqual(facts['route_steps'], 3)
        self.assertTrue(facts['route_continued'])
        self.assertEqual(ledger.commands['30036'], command('attack', (29, 9)))

    def test_fresh_short_base_route_precedes_a_side_wall_after_restart(self):
        data = reported_case()
        original = copy.deepcopy(data)
        ledger, memory = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('move', (29, 6)))
        self.assertEqual(memory.routes[30035].steps, 3)
        self.assertEqual(memory.routes[30035].target_id, 20013)
        self.assertEqual(ledger.commands['30036'], command('attack', (29, 9)))
        self.assertEqual(data, original)

    def test_explicit_arrivals_continue_to_base_fire_without_wall_attack(self):
        memory = RobotAssaultMemory()
        # Explicit finite positions, without predicting movement or damage.
        for offset, point in enumerate(((28, 6), (29, 6), (30, 6), (31, 6))):
            data = reported_case()
            data['roundNo'] += offset
            data['teamOur']['summonRobotList'][0]['pos'] = dict(zip(('x', 'y'), point))
            ledger, memory = decide(data, memory, entry=ENTRY)
            self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)
            if offset < 3:
                self.assertEqual(ledger.commands['30035'], command('move', (29+offset, 6)))
                self.assertEqual(memory.routes[30035].steps, 3-offset)
            else:
                self.assertEqual(ledger.commands['30035'], command('attack', (31, 9)))
            self.assertEqual(ledger.commands['30036'], command('attack', (29, 9)))

    def test_accepted_wall_attack_does_not_lock_out_a_short_route(self):
        data, memory = reported_case(), RobotAssaultMemory()
        turn, _, _, _ = setup_case(data)
        memory.observe(turn, enemy_base(turn))
        memory.remember(turn, turn.summon_robots[0], 'attack', (28, 7), 41000)
        data['roundNo'] = 73
        data['lastRoundRoleActionResults'] = {'30035': True}
        data['teamEnemy']['roles'][3]['health'] = 960
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(memory.accepted_shots[30035, (28, 6), 41000], (28, 7))
        self.assertEqual(ledger.commands['30035'], command('move', (29, 6)))

    def test_short_route_and_current_shooter_survive_reflections_and_id_order(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            for ids in ((30035, 30036), (900, 800)):
                with self.subTest(reflection=(flip_x, flip_y), ids=ids):
                    data = reported_case()
                    for robot, uid in zip(data['teamOur']['summonRobotList'], ids):
                        robot['id'] = uid
                    data['teamOur']['summonRobotList'].reverse()
                    reflect(data, flip_x, flip_y)
                    point = lambda x, y: (40-x if flip_x else x, 31-y if flip_y else y)
                    ledger, memory = decide(data, entry=point(*ENTRY))
                    self.assertEqual(ledger.commands[str(ids[0])], command('move', point(29, 6)))
                    self.assertEqual(memory.routes[ids[0]].target_id, 20013)
                    self.assertEqual(ledger.commands[str(ids[1])], command('attack', point(29, 9)))

    def test_short_support_move_precedes_flank_wall(self):
        ledger, memory = decide(gap_case(), entry=OTHER_ENTRY)
        self.assertEqual(ledger.commands['30035']['action'], 'move')
        self.assertEqual(memory.routes[30035].target_id, 20010)
        self.assertLessEqual(memory.routes[30035].steps, 3)
        for uid in (30036, 30037):
            self.assertEqual(ledger.commands[str(uid)], command('attack', OTHER_ENTRY))

    def test_four_step_detour_does_not_suppress_necessary_local_clearance(self):
        data = reported_case()
        data['teamOur']['summonRobotList'][0]['pos'] = dict(x=27, y=6)
        turn, _, nav, ledger = setup_case(data)
        memory = RobotAssaultMemory()
        memory.observe(turn, enemy_base(turn))
        self.assertTrue(_act_toward_target(turn, memory, nav, ledger, turn.summon_robots[0],
                                          enemy_base(turn), _occupants(turn, memory),
                                          move_within=_gap_region(turn, enemy_base(turn), ENTRY)))
        self.assertEqual(memory.routes[30035].steps, 4)
        ledger, _ = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', (28, 7)))
        self.assertEqual(ledger.commands['30036'], command('attack', (29, 9)))

    def test_actual_occupancy_reservations_and_collision_memory_block_short_moves(self):
        # These are the only eastward first steps on the three-step base route.
        blocked_steps = {(29, 5), (29, 6)}
        for mode in ('occupied', 'reserved', 'failed'):
            with self.subTest(mode=mode):
                data, memory = reported_case(), RobotAssaultMemory()
                turn, _, _, _ = setup_case(data)
                memory.observe(turn, enemy_base(turn))
                reserved = set()
                if mode == 'occupied':
                    data['robot']['roles'] += [controlled_robot(31000+i, *p)
                                               for i, p in enumerate(sorted(blocked_steps))]
                elif mode == 'reserved':
                    reserved = blocked_steps
                else:
                    memory.failed_steps.update(((30035, p), 80) for p in blocked_steps)
                ledger, _ = decide(data, memory, entry=ENTRY, reserved=reserved)
                self.assertEqual(ledger.commands['30035'], command('attack', (28, 7)))
                self.assertEqual(ledger.commands['30036'], command('attack', (29, 9)))

    def test_new_occupant_invalidates_old_goal_without_reusing_it(self):
        data, memory = reported_case(), RobotAssaultMemory()
        turn, _, _, _ = setup_case(data)
        memory.observe(turn, enemy_base(turn))
        memory.routes[30035] = AssaultRoute(71, 20013, (30, 10), (31, 6), 4, (28, 6))
        data['robot']['roles'] = [controlled_robot(31000, 31, 6)]
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', (28, 7)))
        self.assertFalse(ledger.notes[30035]['conditions']['route_continued'])
        self.assertNotIn(30035, memory.routes)

    def test_stalled_base_approach_still_uses_clearance(self):
        data, memory = reported_case(), RobotAssaultMemory()
        turn, _, _, _ = setup_case(data)
        memory.observe(turn, enemy_base(turn))
        memory.approach_stalls[30035] = 3
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', (28, 7)))

    def test_breach_diagnostics_do_not_claim_global_unreachability(self):
        data = reported_case()
        data['teamOur']['summonRobotList'][0]['pos'] = dict(x=27, y=6)
        ledger, _ = decide(data, entry=ENTRY)
        facts = ledger.notes[30035]['conditions']
        self.assertNotIn('focus_unreachable', facts)
        self.assertTrue(facts['focus_target_differs'])
        self.assertEqual(facts['clearance_fallback'], 'no_short_firing_route')
        self.assertEqual(facts['short_route_limit'], 3)


if __name__ == '__main__':
    unittest.main()
