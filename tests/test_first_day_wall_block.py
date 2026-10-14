"""Observed snapshots for the day-one wall denial and night passage."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import controlled_robot
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, distance, pos
from agent.robot_assault import (RobotAssaultMemory, act_robots, base_distance,
                                 choose_summon_position, enemy_entries)
from agent.scouting import (ScoutMemory, act_night_scout,
                            prepare_first_day_wall_blocker, yield_wall_entry)


BOSS = 'BossRobotSummonOrder'
ENTRY = (30, 12)
SPAWN = (30, 13)


def block_case(tick=40, held=False, stock=()):
    data = payload(tick, [unit(13, 'station', 9, 22, health=1500),
                          unit(10, 'worker', *(ENTRY if held else (25, 17)), health=500),
                          unit(12, 'worker', 14, 20, health=500, backpack=list(stock)),
                          unit(11, 'pioneer', 7, 22, health=500)])
    data['teamOur']['roles'] += [unit(20 + i, 'rocket', *p, health=1000)
                               for i, p in enumerate(((8, 23), (9, 23), (8, 21)))]
    data['mapInfo'].update(width=41, height=32,
        zones=[{'neutralType': 'weaponShop', 'pos': {'x': 15, 'y': 20}}])
    data['teamOur']['goldNum'] = 0
    data['weaponShopList'] = [{'name': BOSS, 'price': 120}]
    data['teamEnemy']['roles'] = [unit(99, 'station', 30, 10, health=1500)]
    return data


def memory_for(entry=ENTRY):
    return ScoutMemory(key=(1, 99, (30, 10)), worker_id=10, post=entry, wall_entry=entry)


def seal_ring(data, gap=ENTRY):
    turn = Turn(data, Config())
    data['teamEnemy']['roles'] += [unit(100 + i, 'wall', *p, health=1000)
        for i, p in enumerate(enemy_entries(turn, turn.enemies[0])) if p != gap]
    return data


class FirstDayWallBlockTests(unittest.TestCase):
    def test_agent_reserves_other_worker_for_last_gun_while_offense_worker_buys(self):
        data = block_case(40)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 22]
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        data['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 20}
        data['teamOur']['goldNum'] = 433
        cfg = Config(llm_enabled=False, layout_mode='explicit', wall_cells=[],
                     weapon_cells=[[8, 23], [9, 23], [8, 21]])
        agent = Agent(cfg)
        response = agent.decide(data)['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(response['10'], command('buy', name=BOSS, num=3))
        self.assertEqual(response['12'], command('build', (8, 21), name='gatling'))
        self.assertEqual(mem.first_day_offense['worker_id'], 10)
        self.assertEqual(mem.first_day_offense['phase'], 'buy')

    def test_wall_blocker_does_not_wait_for_last_gun_at_dusk(self):
        data = block_case(54)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 22]
        data['teamOur']['roles'][1]['pos'] = {'x': 25, 'y': 17}
        data['teamOur']['roles'][1]['backpack'] = [BOSS] * 3
        data['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 20}
        data['teamOur']['goldNum'] = 25
        agent = Agent(Config(llm_enabled=False, layout_mode='explicit', wall_cells=[],
                             weapon_cells=[[8, 23], [9, 23], [8, 21]]))
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['12'], command('build', (8, 21), name='gatling'))
        self.assertEqual(next(iter(agent.sessions.values())).night_scout.worker_id, 10)
        self.assertEqual(response['10']['action'], 'move')

    def test_departure_at_25_and_second_day_keeps_existing_policy(self):
        for tick, expected in ((24, False), (25, True), (29, True), (30, True), (35, True), (40, True), (170, False)):
            with self.subTest(tick=tick):
                turn, _, nav, ledger = setup_case(block_case(tick))
                memory = ScoutMemory()
                scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                self.assertEqual(scout is not None, expected)
                if expected:
                    self.assertEqual(scout.id, 10)
                    self.assertEqual(memory.wall_entry, ENTRY)
                    self.assertEqual(base_distance(turn.enemies[0], memory.wall_entry), 2)
                    step = pos(ledger.commands['10']['targetPos'][0])
                    self.assertEqual(distance(scout.pos, step), 1)
                    self.assertNotIn(step, turn.blocked)
                else:
                    self.assertFalse(ledger.used)

    def test_upper_and_lower_entries_mirror_with_both_base_positions(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = block_case()
                for team in ('teamOur', 'teamEnemy'):
                    for role in data[team]['roles']:
                        x, y = pos(role['pos'])
                        station = role['roleType'] == 'station'
                        role['pos'] = {'x': 40 - x - int(station) if flip_x else x,
                                       'y': 31 - y + int(station) if flip_y else y}
                data['mapInfo']['zones'] = []
                turn, _, nav, ledger = setup_case(data)
                memory = ScoutMemory()
                scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                self.assertEqual(scout.id, 10)
                self.assertEqual(memory.wall_entry,
                    (40 - ENTRY[0] if flip_x else ENTRY[0], 31 - ENTRY[1] if flip_y else ENTRY[1]))

    def test_hold_is_stable_and_not_recalled_at_67(self):
        agent = Agent(Config(llm_enabled=False))
        data = block_case(40, stock=[BOSS] * 3)
        agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        entry = memory.night_scout.wall_entry
        data['roundNo'] = 41
        data['teamOur']['roles'][1]['pos'] = {'x': 26, 'y': 16}
        # Another worker getting closer must not exchange the scouting owner.
        data['teamOur']['roles'][2]['pos'] = {'x': 29, 'y': 13}
        agent.decide(data)
        self.assertEqual(memory.night_scout.worker_id, 10)
        self.assertEqual(memory.night_scout.wall_entry, entry)
        data['roundNo'] = 67
        data['teamOur']['roles'][1]['pos'] = {'x': entry[0], 'y': entry[1]}
        response = agent.decide(data)
        self.assertNotIn('10', response['roleCommandMap'])
        self.assertNotIn(10, memory.return_targets)
        self.assertEqual(memory.night_scout.wall_entry, entry)

    def test_occupied_wall_and_npc_cells_are_not_selected(self):
        data = block_case()
        data['teamEnemy']['roles'] += [unit(100, 'wall', *ENTRY, health=1000),
                                       unit(101, 'worker', 31, 12, health=500)]
        data['mapInfo']['zones'].append({'neutralType': 'vendor', 'pos': {'x': 29, 'y': 12}})
        turn, _, nav, ledger = setup_case(data)
        memory = ScoutMemory()
        self.assertIsNotNone(prepare_first_day_wall_blocker(turn, memory, nav, ledger))
        self.assertNotIn(memory.wall_entry, {(30, 12), (31, 12), (29, 12)})
        self.assertNotIn(memory.wall_entry, turn.blocked)

    def test_closed_or_unreachable_ring_does_not_claim_a_gap(self):
        for tick in (40, 69):
            data = seal_ring(block_case(tick), gap=None) if tick == 40 else block_case(tick)
            turn, _, nav, ledger = setup_case(data)
            memory = ScoutMemory()
            self.assertIsNone(prepare_first_day_wall_blocker(turn, memory, nav, ledger))
            self.assertIsNone(memory.wall_entry)
            self.assertFalse(ledger.used)

    def test_paid_orders_and_delivery_work_remain_with_other_worker(self):
        turn, _, nav, ledger = setup_case(block_case(stock=[BOSS]))
        self.assertIsNone(prepare_first_day_wall_blocker(turn, ScoutMemory(), nav, ledger,
            excluded={10}))
        self.assertIsNone(prepare_first_day_wall_blocker(turn, ScoutMemory(), nav, ledger,
            jobs={10: {'kind': 'use'}}))
        self.assertFalse(ledger.used)

    def test_construction_jobs_can_release_one_worker_at_40(self):
        turn, _, nav, ledger = setup_case(block_case())
        scout = prepare_first_day_wall_blocker(turn, ScoutMemory(), nav, ledger,
            jobs={10: {'kind': 'build'}, 12: {'kind': 'build'}})
        self.assertEqual(scout.id, 10)
        self.assertEqual(ledger.used, {10})

    def test_dead_scout_releases_unsecured_target_and_base_change_resets_it(self):
        data = block_case(stock=[BOSS])
        data['teamOur']['roles'][1]['health'] = 0
        turn, _, nav, ledger = setup_case(data)
        memory = memory_for()
        self.assertIsNone(prepare_first_day_wall_blocker(turn, memory, nav, ledger))
        self.assertIsNone(memory.wall_entry)
        memory = memory_for()
        data['teamEnemy']['roles'][0]['health'] = 0
        turn, _, nav, ledger = setup_case(data)
        self.assertIsNone(act_night_scout(turn, memory, nav, ledger))
        self.assertIsNone(memory.worker_id)
        self.assertIsNone(memory.wall_entry)

    def test_summon_waits_for_occupation_then_uses_free_outer_cell(self):
        for tick, held, used in ((39, False, False), (40, False, False),
                                 (46, True, True), (67, False, True)):
            with self.subTest(tick=tick):
                data = block_case(tick, held=held, stock=[BOSS])
                turn, cfg, nav, ledger = setup_case(data)
                memory = Memory(night_scout=memory_for())
                self.assertEqual(summon_best_robot(turn, cfg, memory, nav, ledger,
                    [], [], use_only=True), used)
                if used:
                    self.assertEqual(ledger.commands['12'], command('use', SPAWN, name=BOSS))
                    self.assertTrue(turn.summon_position_legal(SPAWN))
                    self.assertFalse(turn.summon_position_legal(ENTRY))
                else:
                    self.assertFalse(ledger.commands)

    def test_gap_summons_are_unique_avoid_dynamic_occupants_and_keep_fallback(self):
        data = block_case(46, held=True)
        turn, _, _, ledger = setup_case(data)
        ledger.robot_assault_entry = ENTRY
        first = choose_summon_position(turn, ledger)
        self.assertEqual(first, SPAWN)
        ledger.summon_pending_positions.add(first)
        second = choose_summon_position(turn, ledger)
        self.assertNotEqual(second, first)
        self.assertEqual(distance(second, ENTRY), 1)
        data['teamEnemy']['roles'].append(unit(101, 'worker', *SPAWN))
        turn, _, _, ledger = setup_case(data)
        ledger.robot_assault_entry = ENTRY
        self.assertNotEqual(choose_summon_position(turn, ledger), SPAWN)
        data['teamEnemy']['roles'].append(unit(102, 'wall', *ENTRY))
        turn, _, _, ledger = setup_case(data)
        ledger.robot_assault_entry = ENTRY
        fallback = choose_summon_position(turn, ledger)
        self.assertIsNotNone(fallback)
        self.assertTrue(turn.summon_position_legal(fallback))

    def test_npc_outside_gap_allows_an_adjacent_legal_summon(self):
        data = block_case()
        data['mapInfo']['zones'].append({'neutralType': 'vendor', 'pos': {'x': 30, 'y': 13}})
        turn, _, nav, ledger = setup_case(data)
        memory = ScoutMemory()
        self.assertIsNotNone(prepare_first_day_wall_blocker(turn, memory, nav, ledger))
        self.assertEqual(memory.wall_entry, ENTRY)
        ledger.robot_assault_entry = ENTRY
        spawn = choose_summon_position(turn, ledger)
        self.assertNotEqual(spawn, SPAWN)
        self.assertEqual(distance(spawn, ENTRY), 1)
        self.assertTrue(turn.summon_position_legal(spawn))

    def test_agent_yields_then_attacks_through_observed_gap(self):
        agent = Agent(Config(llm_enabled=False))
        data = seal_ring(block_case(69, held=True, stock=[BOSS] * 2))
        before = copy.deepcopy(data)
        response = agent.decide(data)
        self.assertEqual(data, before)
        self.assertEqual(response['roleCommandMap']['12'], command('use', SPAWN, name=BOSS))
        data['roundNo'] = 70
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        data['teamOur']['summonRobotList'] = [controlled_robot(x=SPAWN[0], y=SPAWN[1])]
        response = agent.decide(data)
        self.assertEqual(response, agent.decide(copy.deepcopy(data)))
        step = pos(response['roleCommandMap']['10']['targetPos'][0])
        self.assertNotEqual(step, ENTRY)
        self.assertNotIn('30000', response['roleCommandMap'])
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(memory.night_scout.wall_entry, ENTRY)
        data['roundNo'] = 71
        data['teamOur']['roles'][1]['pos'] = {'x': step[0], 'y': step[1]}
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['30000']['action'], 'attack')
        data['roundNo'] = 72
        data['lastRoundRoleActionResults'] = {'30000': True}
        # Keep the observed spawn position: an open firing lane needs no move.
        response = agent.decide(data)
        shot = response['roleCommandMap']['30000']
        self.assertEqual(shot['action'], 'attack')
        self.assertIn(pos(shot['targetPos'][0]), Turn(data, Config()).enemies[0].cells)

    def test_failed_yield_or_dizzy_scout_never_opens_occupancy_early(self):
        for dizzy in (False, True):
            data = seal_ring(block_case(70, held=True))
            data['teamOur']['summonRobotList'] = [controlled_robot(x=30, y=13)]
            if dizzy:
                data['teamOur']['roles'][1]['abnormalState'] = 'dizzy'
            memory = memory_for()
            turn, _, nav, ledger = setup_case(data)
            self.assertIsNotNone(yield_wall_entry(turn, memory, nav, ledger))
            self.assertIn(ENTRY, turn.blocked)
            ledger.robot_assault_entry = ENTRY
            act_robots(turn, RobotAssaultMemory(), nav, ledger)
            self.assertNotIn('30000', ledger.commands)
            self.assertEqual(ledger.notes[30000]['reason'],
                             'robot_wait_scout_gap' if dizzy else 'robot_wait_gap_queue')
            # The next request still reports the worker in the gap: wait again.
            data['roundNo'] = 71
            turn, _, nav, ledger = setup_case(data)
            ledger.robot_assault_entry = ENTRY
            act_robots(turn, RobotAssaultMemory(), nav, ledger)
            self.assertNotIn('30000', ledger.commands)

    def test_all_robots_in_range_keep_attacking_through_occupied_gap(self):
        data = seal_ring(block_case(71))
        data['teamOur']['roles'][1]['pos'] = {'x': 29, 'y': 13}
        data['teamOur']['summonRobotList'] = [controlled_robot(x=30, y=12),
                                             controlled_robot(30001, 30, 13)]
        turn, _, nav, ledger = setup_case(data)
        ledger.robot_assault_entry = ENTRY
        memory = RobotAssaultMemory()
        act_robots(turn, memory, nav, ledger)
        for uid in ('30000', '30001'):
            self.assertEqual(ledger.commands[uid]['action'], 'attack')
            self.assertIn(pos(ledger.commands[uid]['targetPos'][0]), turn.enemies[0].cells)
        self.assertIn(ENTRY, turn.blocked)
        self.assertFalse(ledger.reserved)
        data['roundNo'] = 72
        turn, _, nav, ledger = setup_case(data)
        ledger.robot_assault_entry = ENTRY
        act_robots(turn, memory, nav, ledger)
        self.assertEqual(ledger.commands['30001']['action'], 'attack')
        self.assertEqual(ledger.commands['30000']['action'], 'attack')

    def test_daybreak_releases_entry_and_crossing_state(self):
        data = block_case(130)
        turn, _, nav, ledger = setup_case(data)
        scout = memory_for()
        self.assertIsNone(act_night_scout(turn, scout, nav, ledger))
        self.assertIsNone(scout.worker_id)
        self.assertIsNone(scout.wall_entry)
        assault = RobotAssaultMemory(day=1, base_id=99, base_pos=(30, 10),
                                     entry_crossed={30000})
        act_robots(turn, assault, nav, ledger)
        self.assertFalse(assault.entry_crossed)


if __name__ == '__main__':
    unittest.main()
