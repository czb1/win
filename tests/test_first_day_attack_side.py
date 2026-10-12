"""Finite snapshots for a shared day-one unarmed-face preference."""
import copy
import unittest

from test_agent import setup_case, unit
from test_first_day_wall_block import BOSS, ENTRY, SPAWN, block_case, memory_for
from agent.brain import Agent, summon_best_robot
from agent.config import Config
from agent.intelligence import Memory
from agent.model import pos
from agent.navigation import DeadlineExceeded
from agent.robot_assault import (RobotAssaultMemory, act_robots, choose_summon_position,
                                 enemy_entries, first_day_clear_sides, rear_approach)
from agent.scouting import ScoutMemory, prepare_first_day_wall_blocker


LOWER_ENTRY = (30, 7)
LOWER_SPAWN = (30, 6)


def turret(data, kind='rocket', x=30, y=11, health=1000):
    data['teamEnemy']['roles'].append(unit(20040, kind, x, y, health=health))
    return data


def mirror(data, flip_x, flip_y):
    for team in ('teamOur', 'teamEnemy'):
        for role in data[team]['roles']:
            x, y = pos(role['pos'])
            station = role['roleType'] == 'station'
            role['pos'] = {'x': 40 - x - int(station) if flip_x else x,
                           'y': 31 - y + int(station) if flip_y else y}
    data['mapInfo']['zones'] = []
    return data


class FirstDayAttackSideTests(unittest.TestCase):
    def test_every_turret_type_and_entire_upper_face_outweigh_shorter_route(self):
        for kind in ('gatling', 'railgun', 'rocket'):
            for column in (29, 30, 31, 32):
                with self.subTest(kind=kind, column=column):
                    turn, _, nav, ledger = setup_case(turret(block_case(), kind, x=column))
                    memory = ScoutMemory()
                    scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                    self.assertEqual(scout.id, 10)
                    self.assertEqual(memory.wall_entry, LOWER_ENTRY)
                    self.assertEqual(first_day_clear_sides(turn, turn.enemies[0]), {7})

    def test_worker_and_summon_mirror_together_on_both_axes(self):
        for flip_x in (False, True):
            for flip_y in (False, True):
                with self.subTest(flip_x=flip_x, flip_y=flip_y):
                    data = mirror(turret(block_case(stock=[BOSS])), flip_x, flip_y)
                    turn, cfg, nav, ledger = setup_case(data)
                    scout_memory = ScoutMemory()
                    scout = prepare_first_day_wall_blocker(turn, scout_memory, nav, ledger)
                    entry = scout_memory.wall_entry
                    self.assertEqual(entry[1], 31 - LOWER_ENTRY[1] if flip_y else LOWER_ENTRY[1])
                    self.assertIn(entry[0], {p[0] for p in turn.enemies[0].cells})
                    spawn = (entry[0],
                             31 - LOWER_SPAWN[1] if flip_y else LOWER_SPAWN[1])
                    data['roundNo'] = 55
                    data['teamOur']['roles'][1]['pos'] = {'x': entry[0], 'y': entry[1]}
                    turn, cfg, nav, ledger = setup_case(data)
                    memory = Memory(night_scout=scout_memory)
                    self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                     [], [], use_only=True))
                    self.assertEqual(pos(ledger.commands['12']['targetPos'][0]), spawn)
                    self.assertTrue(turn.summon_position_legal(spawn))
                    self.assertNotIn(spawn, turn.blocked)

    def test_equal_faces_keep_route_and_incumbent_ties(self):
        for armed in (False, True):
            with self.subTest(armed=armed):
                data = block_case(held=True)
                if armed:
                    turret(data)
                    data['teamEnemy']['roles'].append(unit(20041, 'railgun', 31, 8))
                turn, _, nav, ledger = setup_case(data)
                memory = memory_for()
                scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                self.assertEqual(scout.id, 10)
                self.assertEqual(memory.wall_entry, ENTRY)
                self.assertFalse(ledger.commands)

    def test_dead_own_and_lateral_weapons_do_not_mark_upper_face_armed(self):
        data = turret(block_case(), health=0)
        data['teamEnemy']['roles'].append(unit(20041, 'gatling', 29, 10))
        data['teamOur']['roles'].append(unit(21, 'rocket', 30, 11))
        turn, _, nav, ledger = setup_case(data)
        memory = ScoutMemory()
        prepare_first_day_wall_blocker(turn, memory, nav, ledger)
        self.assertEqual(memory.wall_entry, ENTRY)
        self.assertEqual(first_day_clear_sides(turn, turn.enemies[0]), {7, 12})

    def test_new_weapon_reselects_before_use_and_waits_for_real_occupation(self):
        data = turret(block_case(46, held=True, stock=[BOSS]))
        turn, cfg, nav, ledger = setup_case(data)
        memory = Memory(night_scout=memory_for())
        scout = prepare_first_day_wall_blocker(turn, memory.night_scout, nav, ledger)
        self.assertEqual(scout.id, 10)
        self.assertEqual(memory.night_scout.wall_entry, LOWER_ENTRY)
        self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        self.assertNotIn('12', ledger.commands)
        data['roundNo'] = 55
        data['teamOur']['roles'][1]['pos'] = {'x': 30, 'y': 7}
        turn, cfg, nav, ledger = setup_case(data)
        prepare_first_day_wall_blocker(turn, memory.night_scout, nav, ledger)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        self.assertEqual(pos(ledger.commands['12']['targetPos'][0]), LOWER_SPAWN)

    def test_reserved_uses_keep_existing_gap_when_a_weapon_is_revealed(self):
        agent = Agent(Config(llm_enabled=False))
        data = block_case(46, held=True, stock=[BOSS])
        response = agent.decide(data)
        self.assertEqual(pos(response['roleCommandMap']['12']['targetPos'][0]), SPAWN)
        data['roundNo'] = 47
        data['teamOur']['roles'][2]['backpack'] = []
        data['lastRoundRoleActionResults'] = {'12': True}
        turret(data)
        response = agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(memory.night_scout.wall_entry, ENTRY)
        self.assertEqual(memory.robot_summon_state['positions'], {SPAWN})
        self.assertNotIn('10', response['roleCommandMap'])

    def test_closed_or_too_distant_clear_face_falls_back_to_reachable_gap(self):
        for sealed in (False, True):
            with self.subTest(sealed=sealed):
                data = turret(block_case(40 if sealed else 67, held=True, stock=[BOSS]))
                if sealed:
                    turn, _, _, _ = setup_case(data)
                    data['teamEnemy']['roles'] += [unit(40000 + i, 'wall', *p)
                        for i, p in enumerate(enemy_entries(turn, turn.enemies[0])) if p[1] == 7]
                turn, _, nav, ledger = setup_case(data)
                memory = ScoutMemory()
                self.assertIsNotNone(prepare_first_day_wall_blocker(turn, memory, nav, ledger))
                self.assertEqual(memory.wall_entry, ENTRY)
                ledger.robot_assault_entry = memory.wall_entry
                self.assertEqual(choose_summon_position(turn, ledger), SPAWN)

    def test_summon_without_scout_prefers_clear_face_over_rear(self):
        for y, clear_row in ((11, 7), (8, 12)):
            with self.subTest(weapon_y=y):
                turn, _, _, ledger = setup_case(turret(block_case(67), y=y))
                spawn = choose_summon_position(turn, ledger)
                self.assertIsNotNone(spawn)
                self.assertTrue(spawn[1] <= clear_row if clear_row == 7 else spawn[1] >= clear_row)
                self.assertTrue(turn.summon_position_legal(spawn))
                ledger.summon_pending_positions.add(spawn)
                next_spawn = choose_summon_position(turn, ledger)
                self.assertNotEqual(next_spawn, spawn)
                self.assertTrue(next_spawn[1] <= clear_row if clear_row == 7 else next_spawn[1] >= clear_row)

    def test_no_legal_clear_side_spawn_uses_existing_fallback_and_deadline(self):
        data = turret(block_case(67))
        data['mapInfo']['zones'] += [{'neutralType': 'vendor', 'pos': {'x': x, 'y': y}}
            for x in range(41) for y in range(8)]
        turn, _, _, ledger = setup_case(data)
        spawn = choose_summon_position(turn, ledger)
        self.assertIsNotNone(spawn)
        self.assertGreater(spawn[1], 7)
        self.assertTrue(turn.summon_position_legal(spawn))
        self.assertNotIn(spawn, turn.blocked)
        with self.assertRaises(DeadlineExceeded):
            choose_summon_position(turn, ledger, deadline=0)

    def test_daytime_memory_is_available_before_worker_and_summon_selection(self):
        agent = Agent(Config(llm_enabled=False))
        data = turret(block_case(39))
        agent.decide(data)
        data['roundNo'] = 40
        data['teamEnemy']['roles'] = data['teamEnemy']['roles'][:1]
        original = copy.deepcopy(data)
        response = agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(memory.night_scout.wall_entry, LOWER_ENTRY)
        self.assertIn(20040, memory.robot_assault.buildings)
        self.assertEqual(data, original)
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)
        turn, _, nav, ledger = setup_case(data)
        act_robots(turn, memory.robot_assault, nav, ledger)
        self.assertEqual(first_day_clear_sides(turn, turn.enemies[0], ledger.robot_known_enemies), {7})
        spawn = choose_summon_position(turn, ledger)
        self.assertLessEqual(spawn[1], 7)

    def test_shared_sight_or_explicit_death_removes_remembered_turret(self):
        for death in (False, True):
            with self.subTest(death=death):
                memory = RobotAssaultMemory()
                data = turret(block_case(39))
                turn, _, nav, ledger = setup_case(data)
                act_robots(turn, memory, nav, ledger)
                data['roundNo'] = 40
                if death:
                    data['teamEnemy']['roles'][-1]['health'] = 0
                else:
                    data['teamEnemy']['roles'] = data['teamEnemy']['roles'][:1]
                    data['teamOur']['roles'][1]['pos'] = {'x': 29, 'y': 13}
                turn, _, nav, ledger = setup_case(data)
                act_robots(turn, memory, nav, ledger)
                self.assertFalse(memory.buildings)
                self.assertEqual(first_day_clear_sides(turn, turn.enemies[0], ledger.robot_known_enemies), {7, 12})

    def test_second_day_retains_rear_policy(self):
        turn, _, nav, ledger = setup_case(turret(block_case(170), y=8))
        self.assertFalse(first_day_clear_sides(turn, turn.enemies[0]))
        self.assertIsNone(prepare_first_day_wall_blocker(turn, ScoutMemory(), nav, ledger))
        spawn = choose_summon_position(turn, ledger)
        self.assertTrue(rear_approach(turn, turn.enemies[0], spawn))


if __name__ == '__main__':
    unittest.main()
