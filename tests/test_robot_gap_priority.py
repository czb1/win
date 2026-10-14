"""Finite observations of the r76 worker-blocked gap with a long open detour."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_assault_bypass import reflect
from test_robot_assault import controlled_robot
from test_robot_gap_clearance import decide
from agent.commands import command
from agent.model import distance, pos
from agent.robot_assault import (AssaultRoute, RobotAssaultMemory, _act_toward_target,
                                 _occupants, enemy_base)


ENTRY = (31, 7)


def blocked_entry_case():
    # Only observed local geometry from r76. The second opening at (29,12)
    # leaves a real base route, which used to suppress all entry clearance.
    data = payload(76, [unit(10013, 'station', 9, 22),
                        unit(10010, 'worker', 28, 6)])
    data['mapInfo'].update(width=41, height=32)
    data['teamEnemy']['roles'] = [
        unit(20013, 'station', 30, 10, health=1500),
        unit(20010, 'worker', *ENTRY, health=500),
        unit(20014, 'imp', 29, 10, health=500),
        unit(20041, 'rocket', 32, 10, health=1000),
    ]
    ring = [(x, y) for x in range(28, 34) for y in range(7, 13)
            if x in (28, 33) or y in (7, 12)]
    data['teamEnemy']['roles'] += [unit(41000+i, 'wall', *p, health=1000)
                                  for i, p in enumerate(ring) if p not in (ENTRY, (29, 12))]
    data['teamOur']['summonRobotList'] = [controlled_robot(30035+i, *p)
        for i, p in enumerate(((31, 5), (29, 5), (33, 6)))]
    return data


class RobotGapPriorityTests(unittest.TestCase):
    def test_reachable_base_detour_does_not_suppress_gap_focus(self):
        data = blocked_entry_case()
        turn, _, nav, ledger = setup_case(data)
        memory = RobotAssaultMemory()
        memory.observe(turn, enemy_base(turn))
        for robot in turn.summon_robots:
            self.assertTrue(_act_toward_target(turn, memory, nav, ledger, robot,
                                              enemy_base(turn), _occupants(turn, memory)))
            self.assertEqual(ledger.commands[str(robot.id)]['action'], 'move')
            self.assertGreater(ledger.notes[robot.id]['conditions']['route_steps'], 6)
        ledger, memory = decide(data)
        self.assertEqual(memory.focus_target_id, 20010)
        self.assertEqual(memory.clearance_entry, ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', ENTRY))
        for robot in data['teamOur']['summonRobotList']:
            facts = ledger.notes[robot['id']]['conditions']
            self.assertEqual(facts['target_id'], 20010)
            self.assertTrue(facts['gap_priority'])
            self.assertLessEqual(facts.get('route_steps', 0), 1)

    def test_selected_and_inferred_gap_work_with_reflections_and_reordered_ids(self):
        for known in (False, True):
            for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(known=known, reflection=(flip_x, flip_y)):
                    data = blocked_entry_case()
                    for robot, uid in zip(data['teamOur']['summonRobotList'], (900, 800, 700)):
                        robot['id'] = uid
                    data['teamOur']['summonRobotList'].reverse()
                    reflect(data, flip_x, flip_y)
                    entry = (40-ENTRY[0] if flip_x else ENTRY[0],
                             31-ENTRY[1] if flip_y else ENTRY[1])
                    before = copy.deepcopy(data)
                    ledger, memory = decide(data, entry=entry if known else None)
                    self.assertEqual(memory.clearance_entry, entry)
                    for note in ledger.notes.values():
                        self.assertEqual(note['conditions']['target_id'], 20010)
                    self.assertEqual(data, before)

    def test_actual_arrivals_join_the_shooter_on_the_next_observation(self):
        data = blocked_entry_case()
        ledger, memory = decide(data, entry=ENTRY)
        # Explicit next observation, without predicting combat or simulating HP.
        data['roundNo'] += 1
        data['teamOur']['summonRobotList'][1]['pos'] = dict(x=29, y=4)
        data['teamOur']['summonRobotList'][2]['pos'] = dict(x=32, y=5)
        ledger, _ = decide(data, memory, entry=ENTRY)
        for uid in (30035, 30036, 30037):
            self.assertEqual(ledger.commands[str(uid)], command('attack', ENTRY))

    def test_gap_preempts_a_confirmed_old_detour(self):
        data = blocked_entry_case()
        turn, _, _, _ = setup_case(data)
        memory = RobotAssaultMemory()
        memory.observe(turn, enemy_base(turn))
        for robot in turn.summon_robots:
            memory.routes[robot.id] = AssaultRoute(turn.round-1, 20013, (30, 10),
                                                   (29, 12), 30, robot.pos)
        ledger, _ = decide(data, memory, entry=ENTRY)
        for note in ledger.notes.values():
            self.assertEqual(note['conditions']['target_id'], 20010)
            self.assertFalse(note['conditions']['route_continued'])

    def test_collision_memory_does_not_hide_an_observed_attackable_gap_blocker(self):
        data = blocked_entry_case()
        turn, _, _, _ = setup_case(data)
        memory = RobotAssaultMemory()
        memory.observe(turn, enemy_base(turn))
        memory.occupied_steps[ENTRY] = 80
        for robot in turn.summon_robots:
            memory.failed_steps[robot.id, ENTRY] = 80
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', ENTRY))
        self.assertIn(ENTRY, ledger.turn.blocked)
        for note in ledger.notes.values():
            self.assertEqual(note['conditions']['target_id'], 20010)

    def test_priority_focus_is_released_when_the_battle_or_controlled_group_ends(self):
        for change in ('day', 'base', 'lost'):
            with self.subTest(change=change):
                data = blocked_entry_case()
                _, memory = decide(data, entry=ENTRY)
                self.assertEqual(memory.priority_target_id, 20010)
                if change == 'day':
                    data['roundNo'] = 130
                elif change == 'base':
                    data['teamEnemy']['roles'][0]['health'] = 0
                else:
                    data['teamOur']['summonRobotList'] = []
                ledger, _ = decide(data, memory)
                self.assertIsNone(memory.priority_target_id)
                self.assertIsNone(memory.clearance_entry)
                self.assertFalse(ledger.commands)

    def test_alive_blocker_persists_despite_legal_attack_and_expected_lethal_damage(self):
        data = blocked_entry_case()
        data['teamEnemy']['roles'][1]['health'] = 1
        data['teamOur']['summonRobotList'] = [controlled_robot(30035, 31, 6)]
        _, memory = decide(data, entry=ENTRY)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30035': True}
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', ENTRY))
        self.assertIn(ENTRY, ledger.turn.blocked)

    def test_observed_death_disappearance_or_departure_releases_gap_for_base_attack(self):
        for change in ('dead', 'missing', 'moved'):
            with self.subTest(change=change):
                data = blocked_entry_case()
                data['teamOur']['summonRobotList'] = [controlled_robot(30035, 31, 5)]
                _, memory = decide(data, entry=ENTRY)
                data['roundNo'] += 1
                blocker = data['teamEnemy']['roles'][1]
                if change == 'dead':
                    blocker['health'] = 0
                elif change == 'missing':
                    data['teamEnemy']['roles'].remove(blocker)
                else:
                    blocker['pos'] = dict(x=20, y=15)
                ledger, _ = decide(data, memory, entry=ENTRY)
                self.assertEqual(ledger.commands['30035'], command('move', (31, 6)))
                self.assertIsNone(memory.focus_target_id)
                data['roundNo'] += 1
                data['teamOur']['summonRobotList'][0]['pos'] = dict(x=31, y=6)
                ledger, _ = decide(data, memory, entry=ENTRY)
                self.assertEqual(ledger.commands['30035']['action'], 'attack')
                self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)

    def test_replacement_wall_and_characters_share_the_selected_gap_target(self):
        for kind in ('wall', 'pioneer', 'imp'):
            with self.subTest(kind=kind):
                data = blocked_entry_case()
                data['teamEnemy']['roles'][1]['roleType'] = kind
                ledger, memory = decide(data, entry=ENTRY)
                self.assertEqual(memory.focus_target_id, 20010)
                for note in ledger.notes.values():
                    self.assertEqual(note['conditions']['target_id'], 20010)

    def test_penetrable_turret_allows_a_nearby_base_shot_without_demolition(self):
        for kind in ('rocket', 'gatling', 'railgun'):
            with self.subTest(kind=kind):
                data = blocked_entry_case()
                data['teamEnemy']['roles'][1]['roleType'] = kind
                data['teamOur']['summonRobotList'] = [controlled_robot(30035, 31, 5)]
                ledger, memory = decide(data, entry=ENTRY)
                self.assertEqual(ledger.commands['30035'], command('move', (31, 6)))
                self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)
                self.assertIsNone(memory.priority_target_id)

    def test_direct_base_shooter_keeps_firing_during_gap_clearance(self):
        data = blocked_entry_case()
        data['teamOur']['summonRobotList'].append(controlled_robot(30040, 32, 8))
        ledger, _ = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['30040']['action'], 'attack')
        self.assertEqual(ledger.notes[30040]['conditions']['target_id'], 20013)
        self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20010)

    def test_open_nearby_gap_does_not_trigger_clearance_at_distant_blocked_gap(self):
        data = blocked_entry_case()
        data['teamEnemy']['roles'][1]['pos'] = dict(x=29, y=12)
        ledger, memory = decide(data)
        self.assertIsNone(memory.focus_target_id)
        for note in ledger.notes.values():
            self.assertEqual(note['conditions']['target_id'], 20013)

    def test_friendly_unknown_and_neutral_occupants_are_never_attacked(self):
        for kind in ('friendly', 'public', 'neutral'):
            with self.subTest(kind=kind):
                data = blocked_entry_case()
                blocker = data['teamEnemy']['roles'].pop(1)
                if kind == 'friendly':
                    data['teamOur']['roles'].append(blocker)
                elif kind == 'public':
                    data['robot']['roles'].append(controlled_robot(31000, *ENTRY))
                else:
                    data['mapInfo']['zones'].append(dict(neutralType='stone', pos=blocker['pos']))
                ledger, _ = decide(data, entry=ENTRY)
                for action in ledger.commands.values():
                    self.assertNotEqual(action, command('attack', ENTRY))
                    self.assertNotEqual(action, command('move', ENTRY))

    def test_support_moves_respect_actual_occupancy_reservations_and_failed_steps(self):
        data = blocked_entry_case()
        _, memory = decide(data, entry=ENTRY)
        memory.pending.clear()
        memory.failed_steps[30036, (29, 4)] = 80
        ledger, _ = decide(data, memory, entry=ENTRY, reserved={(32, 5)})
        destinations = set()
        for robot in data['teamOur']['summonRobotList']:
            action = ledger.commands.get(str(robot['id']), {})
            if action.get('action') == 'move':
                step = pos(action['targetPos'][0])
                self.assertEqual(distance(pos(robot['pos']), step), 1)
                self.assertNotIn(step, ledger.turn.blocked | {(32, 5)})
                self.assertNotIn(step, destinations)
                destinations.add(step)
        self.assertNotEqual(ledger.commands.get('30036'), command('move', (29, 4)))

    def test_dizzy_robot_does_not_act_or_supply_a_shooting_position(self):
        data = blocked_entry_case()
        data['teamOur']['summonRobotList'][0]['abnormalState'] = 'dizzy'
        ledger, _ = decide(data, entry=ENTRY)
        self.assertNotIn('30035', ledger.commands)
        self.assertEqual(ledger.notes[30035]['reason'], 'robot_dizzy')
        for uid in (30036, 30037):
            self.assertEqual(ledger.notes[uid]['conditions']['target_id'], 20010)
            self.assertNotEqual(ledger.commands[str(uid)], command('move', (31, 5)))


if __name__ == '__main__':
    unittest.main()
