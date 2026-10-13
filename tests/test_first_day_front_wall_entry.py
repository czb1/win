"""Finite snapshots for the enemy wall face toward our own base."""
import copy
import unittest

from test_agent import setup_case, unit
from test_first_day_attack_side import mirror, turret
from test_first_day_wall_block import block_case, memory_for, seal_ring
from test_robot_assault import controlled_robot
from agent.brain import Agent
from agent.config import Config
from agent.model import distance, pos
from agent.opening_offense import _entries
from agent.robot_assault import (RobotAssaultMemory, act_robots, choose_summon_position,
                                 first_day_clear_sides, first_day_wall_entries)
from agent.scouting import ScoutMemory, prepare_first_day_wall_blocker, yield_wall_entry


def log_geometry(tick=35):
    # Only the wall-selection geometry from rounds 34-36; no match identifiers.
    data = turret(block_case(tick))
    data['teamOur']['roles'][1]['pos'] = {'x': 26, 'y': 10}
    data['teamOur']['playerTasks'] = [dict(taskType=f'自进化类{i}',
        taskPosition={'x': x, 'y': y}, coldDownRounds=10, isValid=False)
        for i, x, y in ((1, 14, 14), (2, 16, 17))]
    data['teamEnemy']['roles'] += [unit(41000 + i, 'wall', *p)
        for i, p in enumerate(((31, 7), (32, 7), (33, 7), (30, 7),
                               (33, 8), (29, 7), (33, 9)))]
    return data


class FirstDayFrontWallEntryTests(unittest.TestCase):
    def test_log_geometry_chooses_near_front_face_in_all_mirrors(self):
        for flip_x in (False, True):
            for flip_y in (False, True):
                with self.subTest(flip_x=flip_x, flip_y=flip_y):
                    data = mirror(log_geometry(), flip_x, flip_y)
                    before = copy.deepcopy(data)
                    turn, _, nav, ledger = setup_case(data)
                    base = turn.enemies[0]
                    old = (8 if flip_x else 32, 19 if flip_y else 12)
                    expected = (12 if flip_x else 28, 23 if flip_y else 8)
                    memory = ScoutMemory((1, base.id, base.pos), 10, old, old)
                    prepare_first_day_wall_blocker(turn, memory, nav, ledger, worker_id=10)
                    self.assertEqual(memory.wall_entry, expected)
                    facts = ledger.notes[10]['conditions']
                    self.assertEqual(facts['entry_axis'], 'x')
                    self.assertFalse(facts['corner_entry'])
                    self.assertEqual(facts['nearest_turret_distance'], 3)
                    self.assertFalse(facts['turret_in_front'])
                    self.assertEqual(facts['route_steps'], 2)
                    step = pos(ledger.commands['10']['targetPos'][0])
                    self.assertEqual(distance(turn.workers[0].pos, step), 1)
                    self.assertEqual(distance(step, expected), 1)
                    self.assertNotIn(step, turn.blocked)
                    self.assertEqual(data, before)

    def test_agent_selects_front_entry_from_the_reported_position(self):
        data = log_geometry()
        agent = Agent(Config(llm_enabled=False))
        response = agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(memory.night_scout.wall_entry, (28, 8))
        self.assertEqual(response['roleCommandMap']['10']['action'], 'move')
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)

    def test_all_front_cells_are_candidates_and_tail_estimates_use_them(self):
        for flip_x in (False, True):
            with self.subTest(flip_x=flip_x):
                turn, _, _, _ = setup_case(mirror(block_case(), flip_x, False))
                base = turn.enemies[0]
                front, back = (12, 7) if flip_x else (28, 33)
                entries = set(first_day_wall_entries(turn, base))
                self.assertTrue({(front, y) for y in range(8, 12)} <= entries)
                self.assertTrue({(back, y) for y in range(8, 12)}.isdisjoint(entries))
                self.assertEqual(len(entries), 16)
                self.assertEqual(_entries(turn, base), entries)

    def test_lateral_clear_face_uses_live_and_remembered_weapons(self):
        for remembered in (False, True):
            with self.subTest(remembered=remembered):
                data = turret(log_geometry(), x=29, y=10)
                data['teamEnemy']['roles'][-1]['id'] = 20041
                turn, _, nav, ledger = setup_case(data)
                weapon = next(u for u in turn.enemies if u.id == 20041)
                if remembered:
                    data['teamEnemy']['roles'].pop()
                    turn, _, nav, ledger = setup_case(data)
                    ledger.robot_known_enemies = (weapon,)
                    turn.blocked.add(weapon.pos)
                self.assertEqual(first_day_clear_sides(turn, turn.enemies[0],
                                 ledger.robot_known_enemies, axis=0), {33})
                memory = memory_for((32, 12))
                prepare_first_day_wall_blocker(turn, memory, nav, ledger, worker_id=10)
                self.assertEqual(memory.wall_entry, (32, 12))

    def test_lateral_turret_in_front_checks_the_inward_horizontal_cell(self):
        for flip_x in (False, True):
            for direct in (False, True):
                with self.subTest(flip_x=flip_x, direct=direct):
                    data = seal_ring(turret(block_case(), x=29, y=10 if direct else 9), (28, 10))
                    data['teamOur']['roles'][1]['pos'] = {'x': 27, 'y': 10}
                    turn, _, nav, ledger = setup_case(mirror(data, flip_x, False))
                    memory = ScoutMemory()
                    prepare_first_day_wall_blocker(turn, memory, nav, ledger, worker_id=10)
                    self.assertEqual(memory.wall_entry, (12 if flip_x else 28, 10))
                    self.assertEqual(ledger.notes[10]['conditions']['turret_in_front'], direct)

    def test_front_gap_and_followup_summons_stay_outside_the_same_face(self):
        for flip_x in (False, True):
            for flip_y in (False, True):
                with self.subTest(flip_x=flip_x, flip_y=flip_y):
                    data = seal_ring(block_case(), (28, 10))
                    data['teamOur']['roles'][1]['pos'] = {'x': 28, 'y': 10}
                    turn, _, nav, ledger = setup_case(mirror(data, flip_x, flip_y))
                    memory = ScoutMemory()
                    prepare_first_day_wall_blocker(turn, memory, nav, ledger, worker_id=10)
                    ledger.robot_assault_entry = memory.wall_entry
                    spawn = choose_summon_position(turn, ledger)
                    self.assertEqual(spawn, (13 if flip_x else 27, 21 if flip_y else 10))
                    ledger.robot_summon_anchor = spawn
                    for _ in range(3):
                        self.assertTrue(turn.summon_position_legal(spawn))
                        self.assertNotIn(spawn, turn.blocked | ledger.summon_pending_positions)
                        self.assertLessEqual(distance(spawn, ledger.robot_summon_anchor), 2)
                        self.assertTrue(spawn[0] >= 13 if flip_x else spawn[0] <= 27)
                        ledger.summon_pending_positions.add(spawn)
                        spawn = choose_summon_position(turn, ledger)
                        self.assertIsNotNone(spawn)

    def test_front_gap_cannot_use_a_spawn_around_another_face(self):
        data = block_case()
        turn, _, _, ledger = setup_case(data)
        ledger.robot_assault_entry = (28, 11)
        ledger.robot_summon_anchor = (27, 11)
        ledger.robot_summon_mode = 'scout_gap'
        ledger.reserved.update((x, y) for x in range(25, 29) for y in range(9, 14))
        self.assertTrue(turn.summon_position_legal((29, 13)))
        self.assertNotIn((29, 13), turn.blocked | ledger.reserved)
        self.assertIsNone(choose_summon_position(turn, ledger))

    def test_front_entry_requires_an_available_outward_spawn(self):
        data = seal_ring(block_case(), (28, 10))
        data['mapInfo']['zones'] += [{'neutralType': 'vendor', 'pos': {'x': 27, 'y': y}}
                                   for y in (9, 10, 11)]
        turn, _, nav, ledger = setup_case(data)
        self.assertIsNone(prepare_first_day_wall_blocker(turn, ScoutMemory(), nav,
                                                       ledger, worker_id=10))
        self.assertNotIn('10', ledger.commands)

    def test_pending_summons_keep_front_gap_until_it_is_built(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                data = log_geometry()
                if closed:
                    data['teamEnemy']['roles'].append(unit(41999, 'wall', 28, 10))
                turn, _, nav, ledger = setup_case(data)
                ledger.summon_pending_positions.add((27, 10))
                ledger.robot_summon_anchor = (27, 10)
                memory = memory_for((28, 10))
                prepare_first_day_wall_blocker(turn, memory, nav, ledger, worker_id=10)
                self.assertEqual(memory.wall_entry, (28, 8) if closed else (28, 10))

    def test_front_gap_obeys_daylight_and_occupied_entry_constraints(self):
        for reason in ('late', 'occupied', 'reserved'):
            with self.subTest(reason=reason):
                data = seal_ring(block_case(69 if reason == 'late' else 35), (28, 10))
                data['teamOur']['roles'][1]['pos'] = {'x': 26, 'y': 10}
                if reason == 'occupied':
                    data['teamEnemy']['roles'].append(unit(20010, 'worker', 28, 10))
                turn, _, nav, ledger = setup_case(data)
                if reason == 'reserved':
                    ledger.reserved.add((28, 10))
                self.assertIsNone(prepare_first_day_wall_blocker(turn, ScoutMemory(), nav,
                                                               ledger, worker_id=10))
                self.assertNotIn('10', ledger.commands)

    def test_front_scout_yields_and_robots_can_attack_after_observed_departure(self):
        data = seal_ring(block_case(70), (28, 10))
        data['teamOur']['roles'][1]['pos'] = {'x': 28, 'y': 10}
        data['teamOur']['summonRobotList'] = [controlled_robot(x=27, y=10)]
        turn, _, nav, ledger = setup_case(data)
        memory = memory_for((28, 10))
        self.assertIsNotNone(yield_wall_entry(turn, memory, nav, ledger))
        move = ledger.commands['10']
        self.assertEqual(move['action'], 'move')
        self.assertIn((28, 10), turn.blocked)
        data['roundNo'] = 71
        data['teamOur']['roles'][1]['pos'] = move['targetPos'][0]
        turn, _, nav, ledger = setup_case(data)
        act_robots(turn, RobotAssaultMemory(), nav, ledger)
        shot = ledger.commands['30000']
        self.assertEqual(shot['action'], 'attack')
        self.assertIn(pos(shot['targetPos'][0]), turn.enemies[0].cells)


if __name__ == '__main__':
    unittest.main()
