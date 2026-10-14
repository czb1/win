"""Finite decisions for confirmed turret penetration and useful gap clearance."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_assault_bypass import reflect
from test_robot_assault import controlled_robot
from agent.commands import command
from agent.model import pos
from agent.navigation import DeadlineExceeded
from agent.robot_assault import (RobotAssaultMemory, act_robots, enemy_base,
                                 scouted_rear_summon_position)


ENTRY = (33, 7)


def gap_case():
    data = payload(71, [unit(10013, 'station', 9, 22)])
    data['mapInfo'].update(width=41, height=32)
    data['teamEnemy']['roles'] = [unit(20013, 'station', 30, 10, health=1500),
                                 unit(20010, 'worker', *ENTRY, health=500)]
    ring = [(x, y) for x in range(28, 34) for y in range(7, 13)
            if x in (28, 33) or y in (7, 12)]
    data['teamEnemy']['roles'] += [unit(41000+i, 'wall', *p, health=1000)
                                  for i, p in enumerate(ring) if p != ENTRY]
    data['teamOur']['summonRobotList'] = [controlled_robot(30035+i, *p)
        for i, p in enumerate(((35, 9), (35, 8), (35, 7)))]
    return data


def decide(data, memory=None, entry=None, reserved=()):
    turn, _, nav, ledger = setup_case(data, layout_mode='explicit')
    ledger.robot_assault_entry = entry
    ledger.reserved.update(reserved)
    memory = memory if memory is not None else RobotAssaultMemory()
    act_robots(turn, memory, nav, ledger)
    return ledger, memory


def screened_base():
    data = gap_case()
    data['teamEnemy']['roles'] = [data['teamEnemy']['roles'][0],
        unit(20040, 'rocket', 32, 9), unit(20041, 'rocket', 32, 10)]
    data['teamOur']['summonRobotList'] = [controlled_robot(30035, 34, 9)]
    return data


class RobotTurretPenetrationTests(unittest.TestCase):
    def test_all_turret_types_allow_base_fire_but_still_block_movement(self):
        for kind in ('gatling', 'railgun', 'rocket'):
            for side in ('own', 'enemy'):
                for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
                    with self.subTest(kind=kind, side=side, x=flip_x, y=flip_y):
                        data = screened_base()
                        data['teamOur']['summonRobotList'][0]['pos'] = dict(x=33, y=9)
                        for tower in data['teamEnemy']['roles'][1:]:
                            tower['roleType'] = kind
                        if side == 'own':
                            data['teamOur']['roles'] += data['teamEnemy']['roles'][1:]
                            data['teamEnemy']['roles'] = data['teamEnemy']['roles'][:1]
                        reflect(data, flip_x, flip_y)
                        original = copy.deepcopy(data)
                        ledger, _ = decide(data)
                        self.assertEqual(ledger.commands['30035']['action'], 'attack')
                        self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)
                        self.assertEqual(data, original)
                        turn, _, _, fresh = setup_case(data)
                        towers = [u for u in (*turn.ours, *turn.enemies) if u.kind == kind]
                        for tower in towers:
                            self.assertIn(tower.pos, turn.blocked)
                            self.assertFalse(fresh.add(30035, command('move', tower.pos)))

    def test_remembered_turrets_remain_occupied_without_screening_base(self):
        data = screened_base()
        data['roundNo'] = 65
        data['teamOur']['summonRobotList'] = []
        _, memory = decide(data)
        data['roundNo'] = 71
        data['teamEnemy']['roles'] = data['teamEnemy']['roles'][:1]
        data['teamOur']['summonRobotList'] = [controlled_robot(30035, 34, 9)]
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)
        self.assertEqual(ledger.commands['30035']['action'], 'attack')
        self.assertIn((32, 9), ledger.turn.blocked)
        self.assertIn(20040, memory.buildings)

    def test_wall_and_character_behind_turrets_still_block_robot_shots(self):
        for kind in ('wall', 'worker'):
            with self.subTest(kind=kind):
                data = screened_base()
                data['teamEnemy']['roles'] += [unit(20100+i, kind, 33, y)
                                              for i, y in enumerate((9, 10))]
                ledger, _ = decide(data)
                self.assertNotEqual(ledger.commands.get('30035', {}).get('action'), 'attack')
                self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)

    def test_penetration_does_not_extend_robot_range(self):
        data = screened_base()
        data['teamOur']['summonRobotList'][0]['pos'] = dict(x=35, y=9)
        ledger, _ = decide(data)
        self.assertEqual(ledger.commands['30035']['action'], 'move')

    def test_summon_prediction_uses_same_turret_penetration_as_night_attack(self):
        data = screened_base()
        data['roundNo'] = 46
        data['teamOur']['summonRobotList'] = []
        turn, _, nav, ledger = setup_case(data)
        ledger.robot_summon_anchor = (34, 9)
        self.assertEqual(scouted_rear_summon_position(turn, ledger, nav.deadline), (34, 9))
        ledger.summon_pending_positions.add((34, 9))
        self.assertNotEqual(scouted_rear_summon_position(turn, ledger, nav.deadline), (34, 9))

    def test_turret_visibility_changes_do_not_reset_an_unrelated_rejected_aim(self):
        data = screened_base()
        ledger, memory = decide(data)
        rejected = copy.deepcopy(ledger.commands['30035'])
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30035': False}
        data['teamEnemy']['roles'][1]['health'] = 0
        ledger, _ = decide(data, memory)
        self.assertNotEqual(ledger.commands.get('30035'), rejected)
        self.assertTrue(memory.failed_shots)


class RobotGapClearanceTests(unittest.TestCase):
    def test_unavoidable_movement_blocking_turret_can_still_be_attacked(self):
        for kind in ('rocket', 'gatling', 'railgun'):
            for hidden in (False, True):
                with self.subTest(kind=kind, hidden=hidden):
                    data = gap_case()
                    data['teamEnemy']['roles'][1]['roleType'] = kind
                    data['teamOur']['summonRobotList'] = [controlled_robot(30035, 34, 6)]
                    memory = RobotAssaultMemory()
                    if hidden:
                        turn, _, _, _ = setup_case(data)
                        memory.observe(turn, enemy_base(turn))
                        data['roundNo'] += 1
                        data['teamEnemy']['roles'].pop(1)
                    ledger, _ = decide(data, memory, entry=ENTRY)
                    self.assertEqual(ledger.commands['30035'], command('attack', ENTRY))
                    self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20010)
                    self.assertIn(ENTRY, ledger.turn.blocked)

    def test_gap_worker_precedes_unrelated_wall_with_or_without_scout_entry(self):
        for known_entry in (None, ENTRY):
            for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(entry=known_entry, x=flip_x, y=flip_y):
                    data = gap_case()
                    reflect(data, flip_x, flip_y)
                    entry = (40-ENTRY[0] if flip_x else ENTRY[0],
                             31-ENTRY[1] if flip_y else ENTRY[1])
                    original = copy.deepcopy(data)
                    ledger, memory = decide(data, entry=entry if known_entry else None)
                    self.assertEqual(memory.focus_target_id, 20010)
                    self.assertEqual(memory.clearance_entry, entry)
                    for uid in (30036, 30037):
                        self.assertEqual(ledger.commands[str(uid)], command('attack', entry))
                    # This supporter's ray hits the flank wall first. Clear
                    # that wall now while the others keep firing at the worker.
                    wall = next(u for u in ledger.turn.enemies if u.id == 41015)
                    self.assertEqual(ledger.commands['30035'], command('attack', wall.pos))
                    for uid in (30035, 30036, 30037):
                        facts = ledger.notes[uid]['conditions']
                        self.assertEqual(facts.get('clearance_target_id', facts['target_id']), 20010)
                    self.assertEqual(data, original)

    def test_allocation_is_independent_of_robot_ids(self):
        data = gap_case()
        for robot, uid in zip(data['teamOur']['summonRobotList'], (900, 800, 700)):
            robot['id'] = uid
        ledger, _ = decide(data, entry=ENTRY)
        for uid in (700, 800):
            self.assertEqual(ledger.commands[str(uid)], command('attack', ENTRY))
        self.assertEqual(ledger.notes[900]['conditions']['clearance_target_id'], 20010)
        self.assertEqual(ledger.notes[900]['conditions']['target_id'], 41015)

    def test_live_blocker_is_not_skipped_then_observed_clearance_allows_ingress(self):
        data = gap_case()
        data['teamOur']['summonRobotList'] = [controlled_robot(30035, 34, 6)]
        data['teamEnemy']['roles'][1]['health'] = 1
        ledger, memory = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', ENTRY))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'30035': True}
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('attack', ENTRY))
        data['roundNo'] += 1
        data['teamEnemy']['roles'][1]['health'] = 0
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('move', ENTRY))
        data['roundNo'] += 1
        data['teamOur']['summonRobotList'][0]['pos'] = dict(x=33, y=7)
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035'], command('move', (32, 8)))
        data['roundNo'] += 1
        data['teamOur']['summonRobotList'][0]['pos'] = dict(x=32, y=8)
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['30035']['action'], 'attack')
        self.assertEqual(ledger.notes[30035]['conditions']['target_id'], 20013)

    def test_worker_moving_away_releases_focus_without_chasing(self):
        data = gap_case()
        _, memory = decide(data, entry=ENTRY)
        data['roundNo'] += 1
        data['teamEnemy']['roles'][1]['pos'] = dict(x=20, y=15)
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertNotEqual(memory.focus_target_id, 20010)
        for note in ledger.notes.values():
            self.assertNotEqual(note['conditions'].get('target_id'), 20010)

    def test_blocker_just_inside_the_gap_is_cleared_before_adjacent_walls(self):
        data = gap_case()
        data['teamEnemy']['roles'][1]['pos'] = dict(x=31, y=8)
        data['teamEnemy']['roles'] = [r for r in data['teamEnemy']['roles']
                                     if pos(r['pos']) != (31, 7)]
        data['teamEnemy']['roles'].append(unit(42000, 'wall', *ENTRY))
        data['robot']['roles'] = [controlled_robot(31000+i, x, 8) for i, x in enumerate((30, 32))]
        data['teamOur']['summonRobotList'] = [controlled_robot(30035, 31, 6)]
        ledger, memory = decide(data, entry=(31, 7))
        self.assertEqual(memory.focus_target_id, 20010)
        self.assertEqual(ledger.commands['30035'], command('attack', (31, 8)))

    def test_new_wall_on_selected_entry_is_the_shared_target(self):
        data = gap_case()
        data['teamEnemy']['roles'][1].update(roleType='wall', health=1000)
        ledger, memory = decide(data, entry=ENTRY)
        self.assertEqual(memory.focus_target_id, 20010)
        for note in ledger.notes.values():
            facts = note['conditions']
            self.assertEqual(facts.get('clearance_target_id', facts['target_id']), 20010)

    def test_an_existing_base_shooter_does_not_join_gap_clearance(self):
        data = gap_case()
        data['teamOur']['summonRobotList'].append(controlled_robot(30040, 29, 10))
        ledger, _ = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['30040']['action'], 'attack')
        self.assertEqual(ledger.notes[30040]['conditions']['target_id'], 20013)
        self.assertEqual(ledger.notes[30036]['conditions']['target_id'], 20010)

    def test_friendly_public_and_neutral_occupants_are_not_clearance_targets(self):
        for kind in ('friendly', 'public', 'neutral'):
            with self.subTest(kind=kind):
                data = gap_case()
                worker = data['teamEnemy']['roles'].pop(1)
                if kind == 'friendly':
                    data['teamOur']['roles'].append(worker)
                elif kind == 'public':
                    data['robot']['roles'].append(controlled_robot(31000, *ENTRY))
                else:
                    data['mapInfo']['zones'].append(dict(neutralType='stone', pos=worker['pos']))
                ledger, memory = decide(data, entry=ENTRY)
                self.assertNotIn(memory.focus_target_id, (20010, 31000))
                for action in ledger.commands.values():
                    self.assertNotEqual(action, command('attack', ENTRY))
                    if action['action'] == 'move':
                        self.assertNotEqual(pos(action['targetPos'][0]), ENTRY)

    def test_clearance_moves_preserve_reserved_cells_and_failed_steps(self):
        data = gap_case()
        ledger, memory = decide(data, entry=ENTRY)
        destination = pos(ledger.commands['30035']['targetPos'][0])
        for failed in (False, True):
            with self.subTest(failed=failed):
                state = copy.deepcopy(memory)
                state.pending.clear()
                if failed:
                    state.failed_steps[30035, destination] = 80
                ledger, _ = decide(data, state, entry=ENTRY,
                                    reserved=() if failed else (destination,))
                self.assertNotEqual(ledger.commands.get('30035'), command('move', destination))

    def test_day_base_change_and_lost_robots_clear_gap_memory(self):
        for change in ('day', 'base', 'lost'):
            with self.subTest(change=change):
                data = gap_case()
                _, memory = decide(data, entry=ENTRY)
                if change == 'day':
                    data['roundNo'] = 130
                elif change == 'base':
                    data['teamEnemy']['roles'][0]['health'] = 0
                else:
                    data['teamOur']['summonRobotList'] = []
                decide(data, memory)
                self.assertIsNone(memory.clearance_entry)

    def test_budget_expiry_preserves_observed_occupancy(self):
        data = gap_case()
        original = copy.deepcopy(data)
        turn, _, nav, ledger = setup_case(data)
        occupied = turn.blocked.copy()
        nav.deadline = 0
        with self.assertRaises(DeadlineExceeded):
            act_robots(turn, RobotAssaultMemory(), nav, ledger)
        self.assertEqual(turn.blocked, occupied)
        self.assertEqual(data, original)


if __name__ == '__main__':
    unittest.main()
