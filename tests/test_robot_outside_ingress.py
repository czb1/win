"""Finite observations for a shooter holding the lane outside an open wall gap."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import controlled_robot
from test_robot_ingress import decide
from agent.commands import command
from agent.model import pos
from agent.projectiles import line_cells
from agent.robot_assault import RobotAssaultMemory, base_distance, enemy_base


def outside_gap_case(round_no=71, positions=((30, 6), (29, 5), (31, 6))):
    # Local geometry from the supplied r71 snapshot, including the two fixed
    # towers last observed at r70. The scout has already vacated (30,7).
    data = payload(round_no, [unit(10013, 'station', 9, 22),
                             unit(10010, 'worker', 29, 6)])
    data['mapInfo'].update(width=41, height=32)
    data['teamEnemy']['roles'] = [
        unit(20013, 'station', 30, 10, health=1500),
        unit(20012, 'worker', 31, 8), unit(20011, 'pioneer', 32, 10),
        unit(20040, 'rocket', 32, 9), unit(20041, 'rocket', 32, 11),
        unit(20042, 'rocket', 31, 11),
    ]
    walls = [(28, 9), (28, 8), (28, 7), (29, 7), (28, 10), (28, 11),
             (28, 12), (29, 12), (30, 12), (31, 7), (31, 12), (32, 12), (33, 9)]
    data['teamEnemy']['roles'] += [unit(41000 + i, 'wall', *p, health=1000)
                                    for i, p in enumerate(walls)]
    data['teamOur']['summonRobotList'] = [
        controlled_robot(uid, *p) for uid, p in zip((30035, 30036, 30037), positions)]
    return data


class RobotOutsideIngressTests(unittest.TestCase):
    def test_outside_shooter_enters_gap_before_reserving_its_firing_lane(self):
        data = outside_gap_case()
        original = copy.deepcopy(data)
        turn, ledger, _ = decide(data, entry=(30, 7))
        self.assertEqual(ledger.commands['30035'], command('move', (30, 7)))
        self.assertEqual(ledger.notes[30035]['reason'], 'robot_make_room_inside')
        for uid in (30036, 30037):
            self.assertNotIn(str(uid), ledger.commands)
            self.assertEqual(ledger.notes[uid]['reason'], 'robot_wait_assault_lane')
        self.assertIn((30, 6), turn.blocked)
        self.assertEqual(data, original)

    def test_observed_queue_finishes_at_three_adjacent_base_attack_positions(self):
        # Explicit successive observations, not a physics/damage simulation.
        # Each listed next observation separately confirms the submitted move.
        frames = [
            (((30, 6), (29, 5), (31, 6)), {30035: (30, 7)}),
            (((30, 7), (29, 5), (31, 6)), {30035: (30, 8)}),
            (((30, 8), (29, 5), (31, 6)), {30036: (30, 6), 30037: (30, 7)}),
            (((30, 8), (30, 6), (30, 7)), {30035: (29, 9), 30037: (29, 8)}),
            (((29, 9), (30, 6), (29, 8)), {30035: (29, 10)}),
            (((29, 10), (30, 6), (29, 8)), {30037: (29, 9)}),
            (((29, 10), (30, 6), (29, 9)), {30036: (30, 7)}),
            (((29, 10), (30, 7), (29, 9)), {30036: (29, 8)}),
            (((29, 10), (29, 8), (29, 9)), {}),
        ]
        memory = None
        for round_no, (positions, expected_moves) in enumerate(frames, 71):
            with self.subTest(round_no=round_no):
                data = outside_gap_case(round_no, positions)
                if memory:
                    data['lastRoundRoleActionResults'] = {str(uid): True for uid in memory.pending}
                turn, ledger, memory = decide(data, memory, entry=(30, 7))
                moves = {int(uid): pos(action['targetPos'][0]) for uid, action in ledger.commands.items()
                         if action['action'] == 'move'}
                self.assertEqual(moves, expected_moves)
                self.assertEqual(len(set(moves.values())), len(moves))
                for destination in moves.values():
                    self.assertNotIn(destination, turn.blocked)
                for uid, action in ledger.commands.items():
                    if action['action'] == 'attack':
                        self.assertEqual(ledger.notes[int(uid)]['conditions']['target_id'], 20013)
                        ray = line_cells(turn.units[int(uid)].pos, pos(action['targetPos'][0]))
                        self.assertTrue(set(moves.values()).isdisjoint(ray))
                if round_no == 79:
                    self.assertEqual(len(ledger.commands), 3)
                    self.assertFalse(memory.ingress_ids)
                    for robot in turn.summon_robots:
                        self.assertEqual(base_distance(enemy_base(turn), robot.pos), 1)
                        self.assertEqual(ledger.commands[str(robot.id)]['action'], 'attack')

    def test_outside_yield_is_symmetric_and_does_not_require_lowest_id(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            for selected_entry in (None, (30, 7)):
                with self.subTest(flip_x=flip_x, flip_y=flip_y, entry=selected_entry):
                    def reflected(p, station=False):
                        x, y = p
                        return (40 - x - int(station) if flip_x else x,
                                31 - y + int(station) if flip_y else y)
                    data = outside_gap_case()
                    for group in (data['teamOur']['roles'], data['teamEnemy']['roles'],
                                  data['teamOur']['summonRobotList']):
                        for role in group:
                            x, y = reflected(pos(role['pos']), role['roleType'] == 'station')
                            role['pos'] = {'x': x, 'y': y}
                    for robot, uid in zip(data['teamOur']['summonRobotList'], (900, 700, 800)):
                        robot['id'] = uid
                    _, ledger, memory = decide(data, entry=reflected(selected_entry) if selected_entry else None)
                    self.assertEqual(ledger.commands['900'], command('move', reflected((30, 7))))
                    self.assertEqual(memory.ingress_ids, {700, 800, 900})

    def test_blocked_gap_is_not_speculatively_opened(self):
        for blocker in ('wall', 'worker', 'public_robot', 'npc', 'reserved'):
            with self.subTest(blocker=blocker):
                data = outside_gap_case()
                reserved = ()
                if blocker in ('wall', 'worker'):
                    data['teamEnemy']['roles'].append(unit(50000, blocker, 30, 7))
                elif blocker == 'public_robot':
                    data['robot']['roles'].append(controlled_robot(50000, 30, 7))
                elif blocker == 'npc':
                    data['mapInfo']['zones'].append({'neutralType': 'vendor', 'pos': {'x': 30, 'y': 7}})
                else:
                    reserved = {(30, 7)}
                _, ledger, memory = decide(data, reserved=reserved, entry=(30, 7))
                self.assertFalse(memory.ingress_ids)
                for action in ledger.commands.values():
                    if action['action'] == 'move':
                        self.assertNotEqual(pos(action['targetPos'][0]), (30, 7))

    def test_public_or_dizzy_lead_cannot_be_ordered_to_yield(self):
        for kind in ('public', 'dizzy'):
            with self.subTest(kind=kind):
                data = outside_gap_case()
                robots = data['teamOur']['summonRobotList']
                if kind == 'public':
                    data['robot']['roles'].append(robots.pop(0))
                else:
                    robots[0]['abnormalState'] = 'dizzy'
                _, ledger, memory = decide(data, entry=(30, 7))
                self.assertNotIn('30035', ledger.commands)
                self.assertFalse(memory.ingress_ids)

    def test_failed_yield_retains_observed_occupancy_and_pending_ingress(self):
        for result in (True, False, None):
            with self.subTest(result=result):
                data = outside_gap_case()
                _, _, memory = decide(data, entry=(30, 7))
                data['roundNo'] = 72
                if result is not None:
                    data['lastRoundRoleActionResults'] = {'30035': result}
                turn, ledger, memory = decide(data, memory, entry=(30, 7))
                self.assertEqual(memory.ingress_ids, {30035, 30036, 30037})
                self.assertIn((30, 6), turn.blocked)
                self.assertIn((30035, (30, 7)), memory.failed_steps)
                self.assertNotEqual(ledger.commands.get('30035'), command('move', (30, 7)))
                for action in ledger.commands.values():
                    if action['action'] == 'move':
                        self.assertNotIn(pos(action['targetPos'][0]), turn.blocked)

    def test_last_robot_continues_inside_even_when_it_can_already_shoot(self):
        data = outside_gap_case(77, ((29, 10), (30, 6), (29, 9)))
        memory = RobotAssaultMemory(day=1, base_id=20013, base_pos=(30, 10), ingress_ids={30036})
        _, ledger, _ = decide(data, memory, entry=(30, 7))
        self.assertEqual(ledger.commands['30036'], command('move', (30, 7)))
        for uid in (30035, 30037):
            self.assertEqual(ledger.commands[str(uid)]['action'], 'attack')

    def test_unavailable_inner_route_preserves_a_legal_current_attack(self):
        data = outside_gap_case(77, ((29, 10), (30, 6), (29, 9)))
        memory = RobotAssaultMemory(day=1, base_id=20013, base_pos=(30, 10), ingress_ids={30036})
        _, ledger, memory = decide(data, memory, reserved={(30, 7)}, entry=(30, 7))
        self.assertEqual(ledger.commands['30036'], command('attack', (30, 9)))
        self.assertEqual(memory.ingress_ids, {30036})

    def test_ingress_memory_uses_positions_and_resets_with_its_actors(self):
        for change in ('arrived', 'dead', 'public', 'day', 'base'):
            with self.subTest(change=change):
                data = outside_gap_case()
                _, _, memory = decide(data, entry=(30, 7))
                data['roundNo'] = 72
                robots = data['teamOur']['summonRobotList']
                expected = {30035, 30036}
                if change == 'arrived':
                    robots[2]['pos'] = {'x': 29, 'y': 8}
                elif change == 'dead':
                    robots[2]['health'] = 0
                elif change == 'public':
                    data['robot']['roles'].append(robots.pop())
                elif change == 'day':
                    data['roundNo'] = 200
                    expected = set()
                else:
                    data['teamEnemy']['roles'][0]['id'] = 60000
                    expected = set()
                turn, _, _, _ = setup_case(data, layout_mode='explicit')
                memory.observe(turn, enemy_base(turn))
                self.assertEqual(memory.ingress_ids, expected)


if __name__ == '__main__':
    unittest.main()
