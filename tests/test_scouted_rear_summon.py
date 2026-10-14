"""Finite deployment/attack checks for scouted open-rear assaults."""
import copy
import unittest

from test_agent import setup_case, unit
from test_first_day_attack_side import mirror
from test_first_day_wall_block import BOSS, ENTRY, SPAWN, block_case, memory_for
from test_robot_assault import controlled_robot
from agent.brain import Agent, observe_robot_summons, summon_best_robot
from agent.config import Config
from agent.intelligence import Memory
from agent.model import distance, pos
from agent.navigation import DeadlineExceeded
from agent.robot_assault import (RobotAssaultMemory, act_robots, choose_summon_position,
                                 enemy_entries, first_day_wall_entries, rear_approach,
                                 scouted_rear_summon_position)
from agent.scouting import prepare_first_day_wall_blocker


def rear_case(tick=46, held=True, stock=(BOSS,)):
    # Relevant geometry from the supplied r70 snapshot, viewed by the attacker:
    # southern rockets, a western wall, and an exposed eastern/rear approach.
    data = block_case(tick, held=held, stock=stock)
    data['teamEnemy']['roles'] += [unit(20040, 'rocket', 32, 8),
                                  unit(20041, 'rocket', 31, 8)]
    walls = [(28, 7), (28, 8), (28, 9), (28, 10), (28, 11), (28, 12),
             (29, 12), (29, 7), (31, 7), (33, 7), (30, 7)]
    data['teamEnemy']['roles'] += [unit(41000 + i, 'wall', *p) for i, p in enumerate(walls)]
    return data


def close_rear(data):
    turn, _, _, _ = setup_case(data)
    base = turn.enemies[0]
    built = {u.pos for u in turn.enemies if u.kind == 'wall'}
    data['teamEnemy']['roles'] += [unit(42000 + i, 'wall', *p)
        for i, p in enumerate(enemy_entries(turn, base))
        if p not in built and rear_approach(turn, base, p) and p != ENTRY]
    return data


class ScoutedRearSummonTests(unittest.TestCase):
    def test_observed_open_rear_without_gap_attacks_base_on_both_axes(self):
        for kind in ('gatling', 'railgun', 'rocket'):
            for flip_x in (False, True):
                for flip_y in (False, True):
                    with self.subTest(kind=kind, flip_x=flip_x, flip_y=flip_y):
                        data = rear_case()
                        for role in data['teamEnemy']['roles'][1:3]:
                            role['roleType'] = kind
                        mirror(data, flip_x, flip_y)
                        entry = (40 - ENTRY[0] if flip_x else ENTRY[0],
                                 31 - ENTRY[1] if flip_y else ENTRY[1])
                        old_spawn = (40 - SPAWN[0] if flip_x else SPAWN[0],
                                     31 - SPAWN[1] if flip_y else SPAWN[1])
                        turn, _, nav, ledger = setup_case(data)
                        spawn = choose_summon_position(turn, ledger, nav.deadline)
                        self.assertNotEqual(spawn, old_spawn)
                        self.assertTrue(rear_approach(turn, turn.enemies[0], spawn))
                        self.assertTrue(turn.summon_position_legal(spawn))
                        self.assertNotIn(spawn, turn.blocked)
                        self.assertEqual(ledger.robot_summon_mode, 'rear_direct')
                        data['roundNo'] = 71
                        data['teamOur']['summonRobotList'] = [controlled_robot(x=spawn[0], y=spawn[1])]
                        turn, _, nav, ledger = setup_case(data)
                        ledger.robot_assault_entry = entry
                        act_robots(turn, RobotAssaultMemory(), nav, ledger)
                        shot = ledger.commands['30000']
                        self.assertEqual(shot['action'], 'attack')
                        self.assertIn(pos(shot['targetPos'][0]), turn.enemies[0].cells)

    def test_worker_gap_precedes_open_rear_on_horizontal_and_vertical_faces(self):
        for entry, expected in ((ENTRY, SPAWN), ((31, 7), (31, 6)), ((28, 10), (27, 10))):
            for flip_x in (False, True):
                for flip_y in (False, True):
                    with self.subTest(entry=entry, flip_x=flip_x, flip_y=flip_y):
                        data = block_case(46, held=True, stock=[BOSS])
                        data['teamOur']['roles'][1]['pos'] = {'x': entry[0], 'y': entry[1]}
                        data['teamEnemy']['roles'].append(unit(20040, 'rocket', 32, 8))
                        mirror(data, flip_x, flip_y)
                        gap = (40 - entry[0] if flip_x else entry[0],
                               31 - entry[1] if flip_y else entry[1])
                        spawn = (40 - expected[0] if flip_x else expected[0],
                                 31 - expected[1] if flip_y else expected[1])
                        turn, cfg, nav, ledger = setup_case(data)
                        memory = Memory(night_scout=memory_for(gap))
                        base = turn.enemies[0]
                        memory.night_scout.key = (1, base.id, base.pos)
                        self.assertIsNotNone(scouted_rear_summon_position(turn, ledger))
                        ledger.robot_assault_entry = gap
                        self.assertEqual(choose_summon_position(turn, ledger), spawn)
                        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                         [], [], use_only=True))
                        self.assertEqual(pos(ledger.commands['12']['targetPos'][0]), spawn)
                        self.assertEqual(memory.robot_summon_state['mode'], 'scout_gap')
                        self.assertEqual(memory.robot_summon_state['anchor'], spawn)

    def test_unavailable_worker_gap_keeps_rear_fallback(self):
        for reason in ('missing', 'wall', 'reserved_exterior'):
            with self.subTest(reason=reason):
                data = rear_case()
                if reason == 'wall':
                    data['teamEnemy']['roles'].append(unit(50000, 'wall', *ENTRY))
                turn, cfg, nav, ledger = setup_case(data)
                memory = Memory(night_scout=memory_for(None if reason == 'missing' else ENTRY))
                if reason == 'reserved_exterior':
                    ledger.reserved.update((x, 13) for x in range(29, 32))
                self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                 [], [], use_only=True))
                self.assertEqual(memory.robot_summon_state['mode'], 'rear_direct')
                spawn = pos(ledger.commands['12']['targetPos'][0])
                self.assertTrue(rear_approach(turn, turn.enemies[0], spawn))
                self.assertTrue(turn.summon_position_legal(spawn))
                self.assertNotIn(spawn, ledger.reserved)

    def test_new_gap_takes_priority_over_distant_existing_anchor(self):
        for mode in ('rear_direct', 'weakest_entry', 'scout_gap'):
            with self.subTest(mode=mode):
                turn, _, _, ledger = setup_case(rear_case())
                old = (34, 9)
                ledger.robot_summon_anchor = old
                ledger.robot_summon_mode = mode
                ledger.summon_pending_positions.add(old)
                ledger.robot_assault_entry = ENTRY
                self.assertEqual(choose_summon_position(turn, ledger), SPAWN)
                self.assertEqual(ledger.robot_summon_mode, 'scout_gap')
                self.assertEqual(ledger.summon_pending_positions, {old})

    def test_waiting_for_new_gap_preserves_deployment_and_worker_route(self):
        data = rear_case(46, held=False, stock=[BOSS] * 2)
        memory = Memory(night_scout=memory_for(None))
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        rear = pos(ledger.commands['12']['targetPos'][0])
        data['roundNo'] = 47
        data['lastRoundRoleActionResults'] = {'12': True}
        memory.night_scout.wall_entry = ENTRY
        turn, cfg, nav, ledger = setup_case(data)
        self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        self.assertEqual(memory.robot_summon_state['anchor'], rear)
        self.assertEqual(memory.robot_summon_state['positions'], {rear})
        self.assertEqual(ledger.robot_summon_mode, 'rear_direct')
        scout = prepare_first_day_wall_blocker(turn, memory.night_scout, nav, ledger)
        self.assertIsNotNone(scout)
        self.assertEqual(memory.night_scout.wall_entry, ENTRY)
        self.assertEqual(ledger.commands[str(scout.id)]['action'], 'move')

    def test_rear_walls_keep_original_worker_gap(self):
        for flip_x in (False, True):
            for flip_y in (False, True):
                with self.subTest(flip_x=flip_x, flip_y=flip_y):
                    data = mirror(close_rear(rear_case()), flip_x, flip_y)
                    turn, _, nav, ledger = setup_case(data)
                    ledger.robot_assault_entry = (40 - ENTRY[0] if flip_x else ENTRY[0],
                                                  31 - ENTRY[1] if flip_y else ENTRY[1])
                    self.assertIsNone(scouted_rear_summon_position(turn, ledger, nav.deadline))
                    self.assertEqual(choose_summon_position(turn, ledger),
                        (40 - SPAWN[0] if flip_x else SPAWN[0],
                         31 - SPAWN[1] if flip_y else SPAWN[1]))
                    self.assertEqual(ledger.robot_summon_mode, 'scout_gap')

    def test_unobserved_dead_and_own_towers_do_not_enable_rear_policy(self):
        for kind in ('unobserved', 'dead', 'own'):
            with self.subTest(kind=kind):
                data = rear_case()
                weapons = data['teamEnemy']['roles'][1:3]
                data['teamEnemy']['roles'] = [data['teamEnemy']['roles'][0]] + data['teamEnemy']['roles'][3:]
                if kind == 'own':
                    data['teamOur']['roles'] += weapons
                elif kind == 'dead':
                    for weapon in weapons:
                        weapon['health'] = 0
                    data['teamEnemy']['roles'] += weapons
                turn, _, _, ledger = setup_case(data)
                ledger.robot_assault_entry = ENTRY
                self.assertIsNone(scouted_rear_summon_position(turn, ledger))
                self.assertEqual(choose_summon_position(turn, ledger), SPAWN)

    def test_selected_gap_waits_for_worker_even_when_rear_is_open(self):
        for tick, closed, expected in ((24, False, False), (46, False, False),
                                       (46, True, False), (67, False, True), (67, True, True)):
            with self.subTest(tick=tick, closed=closed):
                data = rear_case(tick, held=False)
                if closed:
                    close_rear(data)
                turn, cfg, nav, ledger = setup_case(data)
                memory = Memory(night_scout=memory_for())
                self.assertEqual(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                  [], [], use_only=True), expected)
                if expected:
                    spawn = pos(ledger.commands['12']['targetPos'][0])
                    self.assertTrue(turn.summon_position_legal(spawn))
                    self.assertEqual(memory.robot_summon_state['mode'], 'scout_gap')
                else:
                    self.assertFalse(ledger.commands)
                    self.assertIsNone(memory.robot_summon_state['anchor'])

    def test_agent_offense_carrier_selects_gap_before_first_summon(self):
        for held in (False, True):
            with self.subTest(held=held):
                data = rear_case(46, held=held, stock=())
                data['teamOur']['roles'][1]['backpack'] = [BOSS] * 3
                turn, _, _, ledger = setup_case(data)
                built = {u.pos for u in turn.enemies if u.kind == 'wall'}
                data['teamEnemy']['roles'] += [unit(50000 + i, 'wall', *p)
                    for i, p in enumerate(first_day_wall_entries(turn, turn.enemies[0]))
                    if p != ENTRY and p not in built]
                turn, _, _, ledger = setup_case(data)
                self.assertIsNotNone(scouted_rear_summon_position(turn, ledger))
                agent = Agent(Config(llm_enabled=False))
                original = copy.deepcopy(data)
                response = agent.decide(data)
                memory = next(iter(agent.sessions.values()))
                action = response['roleCommandMap']['10']
                self.assertEqual(memory.night_scout.wall_entry, ENTRY)
                self.assertEqual(action['action'], 'use' if held else 'move')
                if held:
                    self.assertEqual(pos(action['targetPos'][0]), SPAWN)
                    self.assertEqual(memory.robot_summon_state['mode'], 'scout_gap')
                self.assertEqual(data, original)
                self.assertEqual(agent.decide(copy.deepcopy(data)), response)
                self.assertEqual(memory.robot_summon_state['count'], int(held))

    def test_rear_orders_stay_nearby_unique_and_retain_base_shots(self):
        data = rear_case(stock=[BOSS] * 3)
        memory = Memory(night_scout=memory_for(None))
        placements = []
        for tick in (46, 47, 48):
            data['roundNo'] = tick
            data['lastRoundRoleActionResults'] = {'12': True}
            data['teamOur']['roles'][2]['backpack'] = [BOSS] * (49 - tick)
            turn, cfg, nav, ledger = setup_case(data)
            self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
            placements.append(pos(ledger.commands['12']['targetPos'][0]))
        self.assertEqual(len(set(placements)), 3)
        self.assertTrue(all(distance(p, placements[0]) <= 2 for p in placements))
        data['roundNo'] = 71
        data['teamOur']['summonRobotList'] = [controlled_robot(30000 + i, *p)
                                             for i, p in enumerate(placements)]
        turn, _, nav, ledger = setup_case(data)
        ledger.robot_assault_entry = ENTRY
        act_robots(turn, RobotAssaultMemory(), nav, ledger)
        for uid in ('30000', '30001', '30002'):
            self.assertEqual(ledger.commands[uid]['action'], 'attack')
            self.assertIn(pos(ledger.commands[uid]['targetPos'][0]), turn.enemies[0].cells)

    def test_new_rear_wall_returns_to_gap_and_rejected_switch_restores_plan(self):
        for accepted in (True, False, None):
            with self.subTest(accepted=accepted):
                data = rear_case(stock=[BOSS] * 3)
                memory = Memory(night_scout=memory_for(None))
                turn, cfg, nav, ledger = setup_case(data)
                self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
                first = pos(ledger.commands['12']['targetPos'][0])
                data['roundNo'] = 47
                data['lastRoundRoleActionResults'] = {'12': True}
                close_rear(data)
                memory.night_scout.wall_entry = ENTRY
                turn, cfg, nav, ledger = setup_case(data)
                self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
                self.assertEqual(pos(ledger.commands['12']['targetPos'][0]), SPAWN)
                self.assertEqual(memory.robot_summon_state['positions'], {first, SPAWN})
                self.assertEqual(memory.robot_summon_state['anchor'], SPAWN)
                data['roundNo'] = 48
                data['lastRoundRoleActionResults'] = {} if accepted is None else {'12': accepted}
                turn, _, _, _ = setup_case(data)
                state = observe_robot_summons(turn, memory)
                rejected = accepted is False
                self.assertEqual(state['anchor'], first if rejected else SPAWN)
                self.assertEqual(state['mode'], 'rear_direct' if rejected else 'scout_gap')
                self.assertEqual(state['count'], 1 if rejected else 2)

    def test_observed_turrets_survive_lost_sight_but_not_confirmed_absence(self):
        data = rear_case(46, held=False)
        memory = RobotAssaultMemory()
        turn, _, nav, ledger = setup_case(data)
        act_robots(turn, memory, nav, ledger)
        data['roundNo'] = 47
        data['teamEnemy']['roles'] = [r for r in data['teamEnemy']['roles']
                                      if r['roleType'] != 'rocket']
        turn, _, nav, ledger = setup_case(data)
        act_robots(turn, memory, nav, ledger)
        self.assertIsNotNone(scouted_rear_summon_position(turn, ledger))
        data['roundNo'] = 48
        data['teamOur']['roles'][1]['pos'] = {'x': 33, 'y': 10}
        turn, _, nav, ledger = setup_case(data)
        act_robots(turn, memory, nav, ledger)
        self.assertIsNone(scouted_rear_summon_position(turn, ledger))

    def test_new_rear_sighting_keeps_gap_group_and_submitted_spawn(self):
        data = block_case(46, held=True, stock=[BOSS] * 2)
        memory = Memory(night_scout=memory_for())
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        self.assertEqual(pos(ledger.commands['12']['targetPos'][0]), SPAWN)
        data['roundNo'] = 47
        data['lastRoundRoleActionResults'] = {'12': True}
        data['teamEnemy']['roles'].append(unit(20040, 'rocket', 30, 11))
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        second = pos(ledger.commands['12']['targetPos'][0])
        self.assertNotEqual(second, SPAWN)
        self.assertLessEqual(distance(second, SPAWN), 2)
        self.assertGreater(second[1], ENTRY[1])
        self.assertEqual(memory.robot_summon_state['mode'], 'scout_gap')
        self.assertEqual(memory.robot_summon_state['anchor'], SPAWN)
        self.assertEqual(memory.robot_summon_state['positions'], {SPAWN, second})

    def test_rear_cluster_does_not_pull_worker_away_from_fallback_gap(self):
        data = block_case(46, held=True, stock=[BOSS] * 2)
        data['teamEnemy']['roles'].append(unit(20040, 'rocket', 30, 11))
        memory = Memory(night_scout=memory_for(None))
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        rear = pos(ledger.commands['12']['targetPos'][0])
        self.assertGreater(distance(rear, ENTRY), 3)
        data['roundNo'] = 47
        data['lastRoundRoleActionResults'] = {'12': True}
        turn, cfg, nav, ledger = setup_case(data)
        state = observe_robot_summons(turn, memory)
        ledger.robot_summon_anchor = state['anchor']
        ledger.robot_summon_mode = state['mode']
        ledger.summon_pending_positions = state['positions'].copy()
        memory.night_scout.wall_entry = ENTRY
        scout = prepare_first_day_wall_blocker(turn, memory.night_scout, nav, ledger)
        self.assertEqual(scout.pos, ENTRY)
        self.assertEqual(memory.night_scout.wall_entry, ENTRY)
        self.assertNotIn('10', ledger.commands)
        turn, cfg, nav, ledger = setup_case(data)
        self.assertIsNotNone(scouted_rear_summon_position(turn, ledger))
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        self.assertEqual(pos(ledger.commands['12']['targetPos'][0]), SPAWN)
        self.assertEqual(memory.robot_summon_state['mode'], 'scout_gap')
        self.assertEqual(memory.robot_summon_state['anchor'], SPAWN)
        self.assertEqual(memory.robot_summon_state['positions'], {rear, SPAWN})

    def test_rejected_first_rear_use_and_daybreak_clear_deployment_state(self):
        data = rear_case()
        memory = Memory(night_scout=memory_for(None))
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        data['roundNo'] = 47
        data['lastRoundRoleActionResults'] = {'12': False}
        turn, cfg, nav, ledger = setup_case(data)
        state = observe_robot_summons(turn, memory)
        self.assertIsNone(state['mode'])
        self.assertIsNone(state['anchor'])
        self.assertEqual(state['count'], 0)
        self.assertFalse(state['positions'])
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger, [], [], use_only=True))
        data['roundNo'] = 130
        turn, _, _, _ = setup_case(data)
        state = observe_robot_summons(turn, memory)
        self.assertIsNone(state['mode'])
        self.assertIsNone(state['anchor'])
        self.assertIsNone(state['pending_plan'])
        self.assertFalse(state['positions'])

    def test_reserved_occupied_and_neutral_spawns_are_avoided(self):
        for obstacle in ('reserve', 'pending', 'worker', 'stone', 'vendor'):
            with self.subTest(obstacle=obstacle):
                data = rear_case()
                turn, _, _, ledger = setup_case(data)
                first = scouted_rear_summon_position(turn, ledger)
                if obstacle == 'worker':
                    data['teamEnemy']['roles'].append(unit(50000, 'worker', *first))
                elif obstacle in ('stone', 'vendor'):
                    data['mapInfo']['zones'].append({'neutralType': obstacle,
                        'pos': {'x': first[0], 'y': first[1]}})
                turn, _, _, ledger = setup_case(data)
                if obstacle == 'reserve':
                    ledger.reserved.add(first)
                elif obstacle == 'pending':
                    ledger.summon_pending_positions.add(first)
                spawn = scouted_rear_summon_position(turn, ledger)
                self.assertIsNotNone(spawn)
                self.assertNotEqual(spawn, first)
                self.assertTrue(turn.summon_position_legal(spawn))

    def test_deadline_and_no_extension_to_later_days_or_night(self):
        turn, _, _, ledger = setup_case(rear_case())
        with self.assertRaises(DeadlineExceeded):
            scouted_rear_summon_position(turn, ledger, deadline=0)
        for tick in (70, 170, 300):
            with self.subTest(tick=tick):
                turn, _, _, ledger = setup_case(rear_case(tick))
                self.assertIsNone(scouted_rear_summon_position(turn, ledger))


if __name__ == '__main__':
    unittest.main()
