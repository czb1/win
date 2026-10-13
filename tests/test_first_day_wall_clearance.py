"""Finite checks for keeping the worker's robot entrance away from turrets."""
import copy
import unittest

from test_agent import setup_case, unit
from test_first_day_attack_side import assert_base_attack, mirror, turret
from test_first_day_wall_block import ENTRY, block_case, memory_for
from agent.model import distance, pos
from agent.robot_assault import (RobotAssaultMemory, act_robots,
                                 choose_summon_position, enemy_entries)
from agent.scouting import ScoutMemory, prepare_first_day_wall_blocker


def close_other_entries(data, gaps):
    turn, _, _, _ = setup_case(data)
    data['teamEnemy']['roles'] += [unit(40000 + i, 'wall', *p)
        for i, p in enumerate(enemy_entries(turn, turn.enemies[0])) if p not in gaps]
    return data


class FirstDayWallClearanceTests(unittest.TestCase):
    def test_farther_entry_beats_old_target_alignment_and_shorter_route(self):
        for kind in ('gatling', 'railgun', 'rocket'):
            for flip_x in (False, True):
                for flip_y in (False, True):
                    with self.subTest(kind=kind, flip_x=flip_x, flip_y=flip_y):
                        data = close_other_entries(turret(block_case(held=True), kind),
                                                   {(x, 12) for x in range(28, 34)})
                        data = mirror(data, flip_x, flip_y)
                        turn, _, nav, ledger = setup_case(data)
                        base, worker = turn.enemies[0], turn.workers[0]
                        memory = ScoutMemory((1, base.id, base.pos), worker.id,
                                             worker.pos, worker.pos)
                        before = copy.deepcopy(data)
                        scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                        expected = (8 if flip_x else 32, 19 if flip_y else 12)
                        self.assertEqual(scout.id, worker.id)
                        self.assertEqual(memory.wall_entry, expected)
                        self.assertEqual(data, before)
                        ledger.robot_assault_entry = expected
                        spawn = choose_summon_position(turn, ledger)
                        # An open rear can now bypass the selected worker gap;
                        # it must still use this unsealed face and shoot the base.
                        self.assertEqual(spawn[1], 18 if flip_y else 13)
                        self.assertTrue(turn.summon_position_legal(spawn))
                        assert_base_attack(self, data, spawn)

    def test_log_geometry_moves_away_from_10_20_rocket(self):
        data = mirror(turret(block_case(38)), True, True)
        data['teamOur']['roles'][1]['pos'] = {'x': 13, 'y': 16}
        walls = ((9, 24), (8, 24), (7, 24), (7, 23), (7, 22), (10, 24),
                 (11, 24), (7, 21), (7, 20), (12, 24), (12, 23), (7, 19))
        data['teamEnemy']['roles'] += [unit(40000 + i, 'wall', *p)
                                      for i, p in enumerate(walls)]
        turn, _, nav, ledger = setup_case(data)
        memory = ScoutMemory((1, 99, (9, 22)), 10, (10, 19), (10, 19))
        prepare_first_day_wall_blocker(turn, memory, nav, ledger, worker_id=10)
        # The newly eligible front face is unarmed and closer than the south row.
        self.assertEqual(memory.wall_entry, (12, 21))
        self.assertGreater(distance(memory.wall_entry, (10, 20)),
                           distance((10, 19), (10, 20)))
        self.assertFalse(ledger.notes[10]['conditions']['turret_in_front'])
        step = pos(ledger.commands['10']['targetPos'][0])
        self.assertEqual(distance(turn.workers[0].pos, step), 1)
        self.assertNotIn(step, turn.blocked)
        self.assertLess(distance(step, memory.wall_entry),
                        distance(turn.workers[0].pos, memory.wall_entry))

    def test_uses_nearest_of_multiple_turrets(self):
        data = turret(block_case(held=True))
        data['teamEnemy']['roles'].append(unit(20041, 'railgun', 32, 11))
        close_other_entries(data, {(x, 12) for x in range(28, 34)})
        turn, _, nav, ledger = setup_case(data)
        memory = memory_for()
        prepare_first_day_wall_blocker(turn, memory, nav, ledger)
        self.assertEqual(memory.wall_entry, (31, 12))

    def test_equal_distance_prefers_entry_not_directly_in_front(self):
        data = close_other_entries(turret(block_case(held=True)), {ENTRY, (31, 12)})
        turn, _, nav, ledger = setup_case(data)
        memory = memory_for()
        prepare_first_day_wall_blocker(turn, memory, nav, ledger)
        self.assertEqual(memory.wall_entry, (31, 12))

    def test_only_feasible_gap_keeps_front_of_turret_as_fallback(self):
        for reason in ('walls', 'occupied', 'reserved', 'unreachable', 'late', 'spawn'):
            with self.subTest(reason=reason):
                alternate = (33, 12)
                data = close_other_entries(turret(block_case(68 if reason == 'late' else 40,
                                                            held=True)),
                                           {ENTRY} if reason == 'walls' else {ENTRY, alternate})
                if reason == 'occupied':
                    data['teamEnemy']['roles'].append(unit(10010, 'worker', *alternate))
                if reason in ('unreachable', 'spawn'):
                    cells = {(x, y) for x in range(32, 35) for y in range(11, 14)
                             if (x, y) != alternate and (reason == 'unreachable' or y >= 12)}
                    data['mapInfo']['zones'] += [{'neutralType': 'vendor',
                        'pos': {'x': x, 'y': y}} for x, y in sorted(cells)]
                turn, _, nav, ledger = setup_case(data)
                if reason == 'reserved':
                    ledger.reserved.add(alternate)
                memory = memory_for()
                scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                self.assertEqual(scout.id, 10)
                self.assertEqual(memory.wall_entry, ENTRY)
                self.assertNotIn('10', ledger.commands)

    def test_committed_summons_keep_gap_until_it_is_closed(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                data = close_other_entries(turret(block_case(held=True)), {ENTRY, (32, 12)})
                if closed:
                    data['teamOur']['roles'][1]['pos'] = {'x': 30, 'y': 13}
                    data['teamEnemy']['roles'].append(unit(41000, 'wall', *ENTRY))
                turn, _, nav, ledger = setup_case(data)
                ledger.summon_pending_positions.add((30, 13))
                memory = memory_for()
                prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                self.assertEqual(memory.wall_entry, (32, 12) if closed else ENTRY)

    def test_remembered_turret_guides_distance_until_death_or_shared_sight(self):
        for evidence in ('hidden', 'dead', 'empty'):
            with self.subTest(evidence=evidence):
                data = close_other_entries(turret(block_case()),
                                           {(x, 12) for x in range(28, 34)})
                memory = RobotAssaultMemory()
                turn, _, nav, ledger = setup_case(data)
                act_robots(turn, memory, nav, ledger)
                data['roundNo'] += 1
                if evidence == 'dead':
                    data['teamEnemy']['roles'][1]['health'] = 0
                else:
                    data['teamEnemy']['roles'].pop(1)
                if evidence == 'empty':
                    data['teamOur']['roles'][1]['pos'] = {'x': 30, 'y': 13}
                turn, _, nav, ledger = setup_case(data)
                act_robots(turn, memory, nav, ledger)
                scout_memory = memory_for()
                prepare_first_day_wall_blocker(turn, scout_memory, nav, ledger)
                self.assertEqual(scout_memory.wall_entry,
                                 (32, 12) if evidence == 'hidden' else ENTRY)

    def test_no_enemy_turrets_retains_existing_gap(self):
        data = close_other_entries(turret(block_case(held=True), health=0),
                                   {(x, 12) for x in range(28, 34)})
        data['teamOur']['roles'].append(unit(23, 'rocket', 30, 11))
        turn, _, nav, ledger = setup_case(data)
        memory = memory_for()
        prepare_first_day_wall_blocker(turn, memory, nav, ledger)
        self.assertEqual(memory.wall_entry, ENTRY)
        self.assertNotIn('10', ledger.commands)

    def test_each_corner_yields_to_a_non_corner_before_summoning(self):
        for flip_x in (False, True):
            for flip_y in (False, True):
                with self.subTest(flip_x=flip_x, flip_y=flip_y):
                    data = close_other_entries(block_case(), {(33, 12), (32, 12)})
                    data['teamOur']['roles'][1]['pos'] = {'x': 33, 'y': 12}
                    turn, _, nav, ledger = setup_case(mirror(data, flip_x, flip_y))
                    base, worker = turn.enemies[0], turn.workers[0]
                    memory = ScoutMemory((1, base.id, base.pos), worker.id,
                                         worker.pos, worker.pos)
                    prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                    expected = (8 if flip_x else 32, 19 if flip_y else 12)
                    self.assertEqual(memory.wall_entry, expected)
                    self.assertEqual(pos(ledger.commands[str(worker.id)]['targetPos'][0]), expected)
                    self.assertFalse(ledger.notes[worker.id]['conditions']['corner_entry'])

    def test_non_corner_precedes_clear_face_and_turret_distance(self):
        data = close_other_entries(turret(block_case()), {(33, 7), ENTRY})
        data['teamOur']['roles'][1]['pos'] = {'x': 33, 'y': 7}
        turn, _, nav, ledger = setup_case(data)
        memory = memory_for((33, 7))
        prepare_first_day_wall_blocker(turn, memory, nav, ledger)
        self.assertEqual(memory.wall_entry, ENTRY)
        self.assertTrue(ledger.notes[10]['conditions']['turret_in_front'])

    def test_corner_fallback_when_non_corner_is_not_feasible(self):
        for reason in ('walls', 'occupied', 'reserved', 'unreachable', 'late', 'spawn'):
            with self.subTest(reason=reason):
                corner, alternate = (33, 12), ENTRY
                data = close_other_entries(block_case(69 if reason == 'late' else 40),
                                           {corner} if reason == 'walls' else {corner, alternate})
                data['teamOur']['roles'][1]['pos'] = {'x': corner[0], 'y': corner[1]}
                if reason == 'occupied':
                    data['teamEnemy']['roles'].append(unit(10010, 'worker', *alternate))
                if reason in ('unreachable', 'spawn'):
                    cells = {(x, y) for x in range(29, 32) for y in range(11, 14)
                             if (x, y) != alternate and (reason == 'unreachable' or y >= 12)}
                    data['mapInfo']['zones'] += [{'neutralType': 'vendor',
                        'pos': {'x': x, 'y': y}} for x, y in sorted(cells)]
                turn, _, nav, ledger = setup_case(data)
                if reason == 'reserved':
                    ledger.reserved.add(alternate)
                memory = memory_for(corner)
                scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger, worker_id=10)
                self.assertEqual(scout.id, 10)
                self.assertEqual(memory.wall_entry, corner)
                self.assertNotIn('10', ledger.commands)
                self.assertTrue(ledger.notes[10]['conditions']['corner_entry'])

    def test_committed_summons_keep_corner_until_it_is_closed(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                corner, alternate = (33, 12), (32, 12)
                data = close_other_entries(block_case(), {corner, alternate})
                data['teamOur']['roles'][1]['pos'] = {'x': 33, 'y': 13 if closed else 12}
                if closed:
                    data['teamEnemy']['roles'].append(unit(41000, 'wall', *corner))
                turn, _, nav, ledger = setup_case(data)
                ledger.summon_pending_positions.add((33, 13))
                memory = memory_for(corner)
                prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                self.assertEqual(memory.wall_entry, alternate if closed else corner)


if __name__ == '__main__':
    unittest.main()
