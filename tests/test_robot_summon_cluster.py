"""Finite snapshots for temporary gap occupancy and day-one summon grouping."""
import copy
import unittest

from test_agent import setup_case, unit
from test_first_day_attack_side import LOWER_ENTRY, mirror
from test_first_day_wall_block import BOSS, ENTRY, SPAWN, block_case, memory_for, seal_ring
from agent.brain import Agent, observe_robot_summons, summon_best_robot
from agent.config import Config
from agent.intelligence import Memory
from agent.model import distance, pos
from agent.navigation import DeadlineExceeded
from agent.robot_assault import choose_summon_position
from agent.scouting import prepare_first_day_wall_blocker


class RobotSummonClusterTests(unittest.TestCase):
    def test_reported_temporary_enemy_occupancy_waits_then_groups_three_orders(self):
        agent = Agent(Config(llm_enabled=False))
        data = seal_ring(block_case(56, stock=[BOSS] * 3))
        data['teamOur']['roles'][1]['pos'] = {'x': 26, 'y': 12}
        data['teamEnemy']['roles'].append(unit(20012, 'worker', 29, 11, health=500))
        agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(memory.night_scout.wall_entry, ENTRY)

        data['roundNo'] = 57
        data['teamOur']['roles'][1]['pos'] = {'x': 27, 'y': 12}
        data['teamEnemy']['roles'][-1]['pos'] = {'x': 30, 'y': 12}
        original = copy.deepcopy(data)
        response = agent.decide(data)
        self.assertEqual(data, original)
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)
        self.assertEqual(memory.night_scout.wall_entry, ENTRY)
        self.assertEqual(memory.robot_summon_state['count'], 0)
        self.assertNotIn('10', response['roleCommandMap'])
        self.assertNotEqual(response['roleCommandMap'].get('12', {}).get('action'), 'use')

        data['roundNo'] = 58
        data['teamEnemy']['roles'][-1]['pos'] = {'x': 29, 'y': 13}
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['10']['action'], 'move')
        self.assertEqual(memory.night_scout.wall_entry, ENTRY)
        self.assertNotEqual(response['roleCommandMap'].get('12', {}).get('action'), 'use')

        placements = []
        for tick in (62, 63, 64):
            data['roundNo'] = tick
            data['teamOur']['roles'][1]['pos'] = {'x': ENTRY[0], 'y': ENTRY[1]}
            data['teamOur']['roles'][2]['backpack'] = [BOSS] * (65 - tick)
            if placements:
                data['lastRoundRoleActionResults'] = {'12': True}
            response = agent.decide(data)
            action = response['roleCommandMap']['12']
            self.assertEqual(agent.decide(copy.deepcopy(data)), response)
            self.assertEqual(memory.robot_summon_state['count'], tick - 61)
            self.assertEqual(action['action'], 'use')
            point = pos(action['targetPos'][0])
            self.assertLessEqual(distance(point, SPAWN), 2)
            self.assertNotIn(point, placements)
            placements.append(point)
        self.assertEqual(placements[0], SPAWN)
        self.assertEqual(memory.robot_summon_state['count'], 3)

    def test_missing_entry_waits_until_existing_dusk_cutoff(self):
        for tick, expected in ((39, False), (40, False), (57, False),
                               (66, False), (67, True), (69, True)):
            with self.subTest(tick=tick):
                turn, cfg, nav, ledger = setup_case(block_case(tick, stock=[BOSS]))
                used = summon_best_robot(turn, cfg, Memory(), nav, ledger,
                                         [], [], use_only=True)
                self.assertEqual(used, expected)
                if not expected:
                    self.assertFalse(ledger.commands)
                    self.assertEqual(ledger.spending_plan['robot_use_blocked'],
                                     'first_day_wall_block_pending')

    def test_temporary_occupant_retains_available_scout_without_claiming_its_cell(self):
        for side, kind in (('teamEnemy', 'worker'), ('teamEnemy', 'imp'),
                           ('teamOur', 'worker'), ('robot', 'smallRobot')):
            with self.subTest(side=side, kind=kind):
                data = seal_ring(block_case(57, stock=[BOSS]))
                if side == 'teamOur':
                    data['teamOur']['roles'][2]['pos'] = {'x': ENTRY[0], 'y': ENTRY[1]}
                else:
                    data[side]['roles'].append(unit(20012, kind, *ENTRY, health=500))
                turn, _, nav, ledger = setup_case(data)
                memory = memory_for()
                scout = prepare_first_day_wall_blocker(turn, memory, nav, ledger)
                self.assertIsNotNone(scout)
                self.assertEqual(memory.wall_entry, ENTRY)
                self.assertIn(scout.id, ledger.used)
                self.assertIn(scout.pos, ledger.reserved)
                self.assertNotIn(ENTRY, ledger.reserved)
                self.assertEqual(ledger.notes[scout.id]['reason'], 'first_day_wall_block_wait')
                self.assertFalse(ledger.commands)

    def test_closed_wall_or_npc_releases_entry_and_does_not_bypass_wait(self):
        for npc in (False, True):
            with self.subTest(npc=npc):
                data = seal_ring(block_case(57, stock=[BOSS]))
                if npc:
                    data['mapInfo']['zones'].append(
                        {'neutralType': 'vendor', 'pos': {'x': ENTRY[0], 'y': ENTRY[1]}})
                else:
                    data['teamEnemy']['roles'].append(unit(999, 'wall', *ENTRY))
                turn, cfg, nav, ledger = setup_case(data)
                memory = Memory(night_scout=memory_for())
                self.assertIsNone(prepare_first_day_wall_blocker(
                    turn, memory.night_scout, nav, ledger))
                self.assertIsNone(memory.night_scout.wall_entry)
                self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                  [], [], use_only=True))
                self.assertFalse(ledger.commands)

    def test_waiting_does_not_take_an_excluded_or_already_used_scout(self):
        for already_used in (False, True):
            with self.subTest(already_used=already_used):
                data = seal_ring(block_case(57, stock=[BOSS]))
                data['teamEnemy']['roles'].append(unit(20012, 'worker', *ENTRY))
                turn, _, nav, ledger = setup_case(data)
                memory = memory_for()
                if already_used:
                    ledger.used.add(10)
                excluded = () if already_used else (10,)
                self.assertIsNone(prepare_first_day_wall_blocker(
                    turn, memory, nav, ledger, excluded=excluded))
                self.assertEqual(memory.wall_entry, ENTRY)
                self.assertNotIn(10, ledger.notes)

    def test_later_summon_stays_near_first_when_gap_closes_on_both_axes(self):
        for flip_x in (False, True):
            for flip_y in (False, True):
                with self.subTest(flip_x=flip_x, flip_y=flip_y):
                    data = mirror(block_case(66, held=True, stock=[BOSS] * 2), flip_x, flip_y)
                    entry = (40 - ENTRY[0] if flip_x else ENTRY[0],
                             31 - ENTRY[1] if flip_y else ENTRY[1])
                    memory = Memory(night_scout=memory_for(entry))
                    base = data['teamEnemy']['roles'][0]
                    memory.night_scout.key = (1, base['id'], pos(base['pos']))
                    turn, cfg, nav, ledger = setup_case(data)
                    self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                     [], [], use_only=True))
                    first = pos(ledger.commands['12']['targetPos'][0])
                    data['roundNo'] = 67
                    data['teamOur']['roles'][2]['backpack'] = [BOSS]
                    data['lastRoundRoleActionResults'] = {'12': True}
                    data['teamEnemy']['roles'].append(unit(999, 'wall', *entry))
                    memory.night_scout.wall_entry = None
                    turn, cfg, nav, ledger = setup_case(data)
                    self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                     [], [], use_only=True))
                    second = pos(ledger.commands['12']['targetPos'][0])
                    self.assertNotEqual(first, second)
                    self.assertLessEqual(distance(first, second), 2)
                    self.assertTrue(turn.summon_position_legal(second))
                    self.assertNotIn(second, turn.blocked)
                    self.assertEqual(memory.robot_summon_state['anchor'], first)

    def test_full_cluster_waits_instead_of_falling_back_to_other_side(self):
        data = block_case(66, held=True, stock=[BOSS] * 2)
        memory = Memory(night_scout=memory_for())
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                         [], [], use_only=True))
        first = pos(ledger.commands['12']['targetPos'][0])
        data['roundNo'] = 67
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        data['lastRoundRoleActionResults'] = {'12': True}
        turn, cfg, nav, ledger = setup_case(data)
        ledger.reserved.update((x, y) for x in range(first[0] - 2, first[0] + 3)
                               for y in range(first[1] - 2, first[1] + 3))
        self.assertFalse(summon_best_robot(turn, cfg, memory, nav, ledger,
                                          [], [], use_only=True))
        self.assertNotIn('12', ledger.commands)
        self.assertEqual(memory.robot_summon_state['count'], 1)
        self.assertEqual(ledger.spending_plan['robot_use_blocked'],
                         'first_day_summon_cluster_full')
        with self.assertRaises(DeadlineExceeded):
            choose_summon_position(turn, ledger, deadline=0)

    def test_rejected_first_use_releases_anchor_but_missing_feedback_retains_it(self):
        for result in (False, None):
            with self.subTest(result=result):
                data = block_case(66, held=True, stock=[BOSS])
                memory = Memory(night_scout=memory_for())
                turn, cfg, nav, ledger = setup_case(data)
                self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                 [], [], use_only=True))
                first = pos(ledger.commands['12']['targetPos'][0])
                data['roundNo'] = 67
                if result is not None:
                    data['lastRoundRoleActionResults'] = {'12': result}
                turn, _, _, _ = setup_case(data)
                state = observe_robot_summons(turn, memory)
                self.assertEqual(state['anchor'], None if result is False else first)
                if result is False:
                    data['teamOur']['roles'][1]['pos'] = {'x': LOWER_ENTRY[0], 'y': LOWER_ENTRY[1]}
                    memory.night_scout.wall_entry = LOWER_ENTRY
                    turn, cfg, nav, ledger = setup_case(data)
                    self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                                     [], [], use_only=True))
                    self.assertGreater(distance(first, memory.robot_summon_state['anchor']), 2)
                data['roundNo'] = 130
                turn, _, _, _ = setup_case(data)
                self.assertIsNone(observe_robot_summons(turn, memory)['anchor'])

    def test_rejected_later_use_keeps_first_reservation_and_anchor(self):
        data = block_case(66, held=True, stock=[BOSS] * 2)
        memory = Memory(night_scout=memory_for())
        placements = []
        for tick in (66, 67):
            data['roundNo'] = tick
            data['teamOur']['roles'][2]['backpack'] = [BOSS] * (68 - tick)
            if placements:
                data['lastRoundRoleActionResults'] = {'12': True}
            turn, cfg, nav, ledger = setup_case(data)
            self.assertTrue(summon_best_robot(turn, cfg, memory, nav, ledger,
                                             [], [], use_only=True))
            placements.append(pos(ledger.commands['12']['targetPos'][0]))
        data['roundNo'] = 68
        data['lastRoundRoleActionResults'] = {'12': False}
        turn, _, _, _ = setup_case(data)
        state = observe_robot_summons(turn, memory)
        self.assertEqual(state['positions'], {placements[0]})
        self.assertEqual(state['anchor'], placements[0])
        self.assertEqual(state['count'], 1)


if __name__ == '__main__':
    unittest.main()
