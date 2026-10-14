"""Observed gap/inner-worker regressions; no combat or damage simulation."""
import copy
import unittest

from test_agent import setup_case, payload, unit
from test_assault_bypass import reflect
from test_robot_assault import controlled_robot
from test_robot_gap_clearance import decide
from agent.commands import command
from agent.model import pos
from agent.robot_assault import RobotAssaultMemory, act_robots, enemy_base


ENTRY = (12, 22)


def reported_gap(round_no=71):
    # Only the relevant visible geometry from the supplied r71-r73 excerpts.
    data = payload(round_no, [unit(20013, 'station', 30, 10)])
    data['mapInfo'].update(width=41, height=32)
    data['teamEnemy']['roles'] = [unit(10013, 'station', 9, 22, health=1500),
                                 unit(10012, 'worker', 11, 22, health=500)]
    walls = [(12, 21), (12, 20), (12, 19), (11, 19), (10, 19),
             (12, 23), (12, 24), (11, 24), (10, 24), (9, 24),
             (8, 24), (9, 19), (8, 19), (7, 19), (7, 22)]
    data['teamEnemy']['roles'] += [unit(40000+i, 'wall', *p, health=1000)
                                  for i, p in enumerate(walls)]
    data['teamOur']['summonRobotList'] = [controlled_robot(31035+i, *p)
        for i, p in enumerate(((13, 22), (13, 20), (13, 24)))]
    return data


class RobotGapQueueTests(unittest.TestCase):
    def test_worker_between_wall_and_base_is_cleared_before_a_long_detour(self):
        for known in (False, True):
            for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(known=known, reflection=(flip_x, flip_y)):
                    data = reported_gap()
                    reflect(data, flip_x, flip_y)
                    entry = (40-ENTRY[0] if flip_x else ENTRY[0],
                             31-ENTRY[1] if flip_y else ENTRY[1])
                    before = copy.deepcopy(data)
                    ledger, memory = decide(data, entry=entry if known else None)
                    target = next(u for u in ledger.turn.enemies if u.id == 10012)
                    self.assertEqual(ledger.commands['31035'], command('attack', target.pos))
                    self.assertEqual(memory.focus_target_id, 10012)
                    self.assertEqual(memory.clearance_entry, entry)
                    self.assertEqual(data, before)

    def test_short_support_routes_precede_walls_in_front_of_gap_target(self):
        data = reported_gap(73)
        data['teamEnemy']['roles'].append(unit(10011, 'pioneer', *ENTRY, health=500))
        for r, p in zip(data['teamOur']['summonRobotList'], ((13, 23), (12, 18), (11, 25))):
            r['pos'] = dict(zip(('x', 'y'), p))
        ledger, memory = decide(data, entry=ENTRY)
        self.assertEqual(memory.focus_target_id, 10011)
        for uid, point, steps in ((31035, (13, 22), 1), (31036, (13, 19), 3), (31037, (12, 25), 3)):
            self.assertEqual(ledger.commands[str(uid)], command('move', point))
            self.assertNotIn(point, ledger.turn.blocked)
            self.assertEqual(ledger.notes[uid]['conditions']['target_id'], 10011)
            self.assertEqual(ledger.notes[uid]['conditions']['route_steps'], steps)

    def test_outer_lead_moves_through_gap_and_followers_wait_for_observation(self):
        data = reported_gap()
        data['teamEnemy']['roles'] = [u for u in data['teamEnemy']['roles'] if u['id'] != 10012]
        data['teamEnemy']['roles'].append(unit(10011, 'pioneer', 13, 21))
        data['teamOur']['roles'].append(unit(20010, 'worker', 13, 23))
        ledger, _ = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['31035'], command('move', ENTRY))
        for uid in (31036, 31037):
            self.assertNotIn(str(uid), ledger.commands)
            self.assertIn('wait', ledger.notes[uid]['reason'])
        self.assertIn((13, 22), ledger.turn.blocked)
        self.assertIn(ENTRY, ledger.reserved)

    def test_failed_entry_step_does_not_hide_the_inner_worker(self):
        data = reported_gap()
        data['teamOur']['summonRobotList'] = [controlled_robot(31035, 13, 22)]
        turn, _, _, _ = setup_case(data)
        memory = RobotAssaultMemory()
        memory.observe(turn, enemy_base(turn))
        memory.failed_steps[31035, ENTRY] = 75
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['31035'], command('attack', (11, 22)))
        self.assertIn(ENTRY, memory.avoided_steps(31035))

    def test_stationary_friendly_queue_releases_after_bounded_observed_stall(self):
        data = reported_gap()
        data['teamEnemy']['roles'] = [u for u in data['teamEnemy']['roles'] if u['id'] != 10012]
        data['teamOur']['roles'].append(unit(20010, 'worker', *ENTRY))
        data['teamOur']['summonRobotList'] = [controlled_robot(31035, 13, 23)]
        memory = RobotAssaultMemory()
        notes = []
        for round_no in range(71, 75):
            data['roundNo'] = round_no
            ledger, memory = decide(data, memory, entry=ENTRY)
            notes.append(ledger.notes[31035]['reason'])
            self.assertNotEqual(ledger.commands.get('31035'), command('move', ENTRY))
        self.assertEqual(notes[:3], ['robot_wait_gap_queue'] * 3)
        self.assertNotEqual(notes[-1], 'robot_wait_gap_queue')

    def test_direct_base_shooter_does_not_join_inner_worker_clearance(self):
        data = reported_gap()
        data['teamOur']['summonRobotList'].append(controlled_robot(31999, 11, 21))
        ledger, _ = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['31999']['action'], 'attack')
        self.assertEqual(ledger.notes[31999]['conditions']['target_id'], 10013)

    def test_inner_worker_clearance_also_handles_horizontal_walls_and_id_order(self):
        data = reported_gap()
        data['mapInfo'].update(width=32, height=41)
        for roles in (data['teamOur']['roles'], data['teamEnemy']['roles'],
                      data['teamOur']['summonRobotList']):
            for role in roles:
                x, y = pos(role['pos'])
                station = role['roleType'] == 'station'
                role['pos'] = dict(x=y-int(station), y=x+int(station))
        for robot, uid in zip(data['teamOur']['summonRobotList'], (900, 800, 700)):
            robot['id'] = uid
        data['teamOur']['summonRobotList'].reverse()
        ledger, memory = decide(data, entry=(22, 12))
        self.assertEqual(ledger.commands['900'], command('attack', (22, 11)))
        self.assertEqual(memory.focus_target_id, 10012)

    def screened_queue(self):
        data = reported_gap()
        data['teamOur']['roles'].append(unit(20010, 'worker', 13, 22))
        data['teamOur']['summonRobotList'] = [controlled_robot(31035, 14, 22)]
        data['teamEnemy']['roles'].append(unit(10011, 'pioneer', *ENTRY, health=500))
        return data

    def test_friendly_screen_does_not_hide_a_gap_target_or_authorize_firing_through_it(self):
        data = self.screened_queue()
        ledger, memory = decide(data, entry=ENTRY)
        self.assertEqual(memory.focus_target_id, 10011)
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.notes[31035]['reason'], 'robot_wait_gap_queue')
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['pos'] = dict(x=15, y=24)
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['31035'], command('attack', ENTRY))

    def test_observed_clearance_damage_keeps_the_queue_but_legal_feedback_alone_does_not(self):
        for damage in ('none', 'target', 'flank'):
            with self.subTest(damage=damage):
                data, memory = self.screened_queue(), RobotAssaultMemory()
                for offset in range(4):
                    data['roundNo'] = 71 + offset
                    data['lastRoundRoleActionResults'] = {'20010': True}
                    uid = 40000 if damage == 'flank' else 10011
                    target = next(u for u in data['teamEnemy']['roles'] if u['id'] == uid)
                    target['health'] = (1000 if damage == 'flank' else 500) - (
                        40*offset if damage != 'none' else 0)
                    ledger, memory = decide(data, memory, entry=ENTRY)
                self.assertEqual(ledger.notes[31035]['reason'] == 'robot_wait_gap_queue', damage != 'none')
                self.assertNotEqual(ledger.commands.get('31035'), command('attack', (13, 22)))

    def test_queue_rechecks_actual_yield_and_never_uses_reserved_or_occupied_steps(self):
        data = reported_gap()
        data['teamEnemy']['roles'] = [u for u in data['teamEnemy']['roles'] if u['id'] != 10012]
        data['teamEnemy']['roles'].append(unit(10011, 'pioneer', 13, 21))
        data['teamOur']['roles'].append(unit(20010, 'worker', 13, 23))
        _, memory = decide(data, entry=ENTRY)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'31035': True}
        # The lead has NOT actually moved despite the legal-command feedback.
        ledger, _ = decide(data, memory, entry=ENTRY, reserved={(13, 21)})
        self.assertIn((31035, ENTRY), memory.failed_steps)
        for cmd in ledger.commands.values():
            if cmd['action'] == 'move':
                self.assertNotIn(pos(cmd['targetPos'][0]), ledger.turn.blocked | {(13, 21), ENTRY})

    def test_neutral_public_and_dizzy_occupants_are_not_assumed_to_yield(self):
        for kind in ('neutral', 'public', 'dizzy'):
            with self.subTest(kind=kind):
                data = self.screened_queue()
                data['teamOur']['roles'].pop()
                if kind == 'neutral':
                    data['mapInfo']['zones'].append(dict(neutralType='stone', pos=dict(x=13, y=22)))
                elif kind == 'public':
                    data['robot']['roles'].append(controlled_robot(32000, 13, 22))
                else:
                    data['teamOur']['summonRobotList'].append(
                        controlled_robot(32000, 13, 22, abnormalState='dizzy'))
                ledger, _ = decide(data, entry=ENTRY)
                self.assertNotIn('32000', ledger.commands)
                self.assertNotEqual(ledger.commands.get('31035'), command('attack', (13, 22)))
                self.assertNotEqual(ledger.commands.get('31035'), command('move', (13, 22)))

    def test_gap_wait_memory_is_scoped_to_contiguous_live_battle_observations(self):
        for change in ('day', 'base', 'lost', 'skip'):
            with self.subTest(change=change):
                data = self.screened_queue()
                _, memory = decide(data, entry=ENTRY)
                self.assertIn(31035, memory.gap_waits)
                data['roundNo'] += 2 if change == 'skip' else 1
                if change == 'day':
                    data['roundNo'] = 130
                elif change == 'base':
                    data['teamEnemy']['roles'][0]['health'] = 0
                elif change == 'lost':
                    data['teamOur']['summonRobotList'] = []
                turn, _, _, _ = setup_case(data)
                memory.observe(turn, enemy_base(turn))
                self.assertFalse(memory.gap_waits)

    def test_cleared_entrance_still_requires_clearing_the_worker_between_wall_and_base(self):
        data = reported_gap()
        data['teamOur']['summonRobotList'] = data['teamOur']['summonRobotList'][:1]
        data['teamEnemy']['roles'].append(unit(10011, 'pioneer', *ENTRY, health=500))
        ledger, memory = decide(data, entry=ENTRY)
        self.assertEqual(ledger.commands['31035'], command('attack', ENTRY))
        data['roundNo'] += 1
        data['teamEnemy']['roles'].pop()
        ledger, _ = decide(data, memory, entry=ENTRY)
        self.assertEqual(ledger.commands['31035'], command('attack', (11, 22)))
        self.assertEqual(memory.focus_target_id, 10012)
        self.assertEqual(memory.clearance_entry, ENTRY)

    def test_missing_dead_or_departed_inner_worker_is_not_chased(self):
        for change in ('missing', 'dead', 'departed'):
            with self.subTest(change=change):
                data = reported_gap()
                data['teamOur']['summonRobotList'] = data['teamOur']['summonRobotList'][:1]
                _, memory = decide(data, entry=ENTRY)
                data['roundNo'] += 1
                target = data['teamEnemy']['roles'][1]
                if change == 'missing':
                    data['teamEnemy']['roles'].remove(target)
                elif change == 'dead':
                    target['health'] = 0
                else:
                    target['pos'] = dict(x=20, y=15)
                ledger, _ = decide(data, memory, entry=ENTRY)
                self.assertEqual(ledger.commands['31035']['action'], 'attack')
                self.assertEqual(ledger.notes[31035]['conditions']['target_id'], 10013)


if __name__ == '__main__':
    unittest.main()
