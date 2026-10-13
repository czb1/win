"""Finite observed-state checks for the reported night-20 ingress regression."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_assault_bypass import reflect, reported_frontage
from test_robot_assault import controlled_robot
from test_robot_path_blockers import decide
from agent.commands import command
from agent.model import distance, pos
from agent.robot_assault import RobotAssaultMemory, act_robots, enemy_base


def reported_gap(round_no=90, inside=True):
    # Only the relevant observed geometry, not an account or a full match log.
    data = payload(round_no, [unit(20013, 'station', 30, 10)])
    data['mapInfo'].update(width=41, height=32)
    walls = [(12, 19), (11, 19), (7, 21), (10, 19), (12, 20),
             (7, 23), (12, 21), (12, 22), (12, 23), (12, 24),
             (11, 24), (10, 24), (7, 24)]
    data['teamEnemy']['roles'] = [unit(10013, 'station', 9, 22, health=3000, level=2),
                                 unit(10041, 'rocket', 8, 22, health=1000)]
    data['teamEnemy']['roles'] += [unit(40000+i, 'wall', *p, health=400 if i == 12 else 1000)
                                 for i, p in enumerate(walls)]
    data['teamOur']['summonRobotList'] = [controlled_robot(31035, 8 if inside else 9, 25),
                                        controlled_robot(31036, 8, 23 if inside else 24),
                                        controlled_robot(31037, 7, 25)]
    data['robot']['roles'] = [controlled_robot(30025, 9, 23, roleType='smallRobot', health=20),
                             controlled_robot(30032, 9, 24, roleType='smallRobot', health=40)]
    return data


def observed_memory(data, *, stalled=False, hidden=False):
    memory = RobotAssaultMemory()
    turn, _, _, _ = setup_case(data, layout_mode='explicit')
    memory.observe(turn, enemy_base(turn))
    if stalled:
        memory.approach_stalls[31037] = 3
        memory.focus_target_id = 40012
    if hidden:
        data['roundNo'] += 1
        data['teamEnemy']['roles'] = [r for r in data['teamEnemy']['roles'] if r['id'] != 10041]
    return memory


class RobotInnerProgressTests(unittest.TestCase):
    def test_entered_robot_attacks_through_visible_or_remembered_inner_tower(self):
        for stalled in (False, True):
            for hidden in (False, True):
                with self.subTest(stalled=stalled, hidden=hidden):
                    data = reported_gap()
                    memory = observed_memory(data, stalled=stalled, hidden=hidden)
                    before = copy.deepcopy(data)
                    ledger, _ = decide(data, memory)
                    self.assertEqual(ledger.commands['31036'], command('attack', (9, 21)))
                    self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)
                    self.assertIn(10041, memory.buildings)
                    self.assertEqual(data, before)
                    self.assertNotIn('30025', ledger.commands)
                    self.assertNotIn('30032', ledger.commands)

    def test_inner_rule_is_symmetric_and_not_tied_to_robot_id_order(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            for lead_id in (30001, 39000):
                with self.subTest(flip_x=flip_x, flip_y=flip_y, lead_id=lead_id):
                    data = reported_gap()
                    data['teamOur']['summonRobotList'][1]['id'] = lead_id
                    reflect(data, flip_x, flip_y)
                    ledger, _ = decide(data)
                    self.assertEqual(ledger.commands[str(lead_id)]['action'], 'attack')
                    self.assertIn(pos(ledger.commands[str(lead_id)]['targetPos'][0]),
                                  enemy_base(ledger.turn).cells)
                    self.assertEqual(ledger.notes[lead_id]['conditions']['target_id'], 10013)

    def test_open_inner_route_precedes_an_optional_inner_target(self):
        data = reported_gap()
        # A free inner step reaches another firing stance without exiting.
        tower = next(r for r in data['teamEnemy']['roles'] if r['id'] == 10041)
        tower['pos'] = dict(x=10, y=23)
        ledger, _ = decide(data)
        self.assertEqual(ledger.commands['31036']['action'], 'move')
        step = pos(ledger.commands['31036']['targetPos'][0])
        self.assertEqual(min(distance(step, cell) for cell in enemy_base(ledger.turn).cells), 1)
        self.assertNotIn(step, ledger.turn.blocked)
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)

    def test_clear_base_shot_resumes_as_soon_as_public_blocker_leaves(self):
        data = reported_gap()
        # A worker remains opaque under the confirmed turret-only penetration rule.
        next(r for r in data['teamEnemy']['roles'] if r['id'] == 10041)['roleType'] = 'worker'
        # Isolate recovery of direct fire from the separate cooperative-yield rule.
        data['teamOur']['summonRobotList'] = [data['teamOur']['summonRobotList'][1]]
        ledger, memory = decide(data)
        self.assertEqual(ledger.commands['31036'], command('attack', (8, 22)))
        data['roundNo'] += 1
        data['robot']['roles'][0]['pos'] = dict(x=15, y=23)
        data['lastRoundRoleActionResults'] = {'31036': True}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands['31036']['action'], 'attack')
        self.assertEqual(ledger.notes[31036]['conditions']['target_id'], 10013)

    def test_unstalled_follower_keeps_its_base_route_before_clearance_move(self):
        data = reported_gap(round_no=88, inside=False)
        next(r for r in data['teamEnemy']['roles'] if r['id'] == 10041)['roleType'] = 'worker'
        memory = observed_memory(data, stalled=True)
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands['31035']['action'], 'move')
        self.assertEqual(ledger.notes[31035]['conditions']['target_id'], 10013)
        self.assertEqual(ledger.notes[31037]['conditions']['target_id'], 40012)

    def test_inner_navigation_respects_reserved_and_failed_destinations(self):
        for restriction in ('reserved', 'failed'):
            with self.subTest(restriction=restriction):
                data = reported_gap()
                data['teamEnemy']['roles'] = [r for r in data['teamEnemy']['roles'] if r['id'] != 10041]
                memory = observed_memory(data)
                turn, _, nav, ledger = setup_case(data, layout_mode='explicit')
                if restriction == 'reserved':
                    ledger.reserved.add((8, 22))
                else:
                    memory.failed_steps[31036, (8, 22)] = turn.round + 4
                act_robots(turn, memory, nav, ledger)
                action = ledger.commands.get('31036')
                self.assertNotEqual(action, command('move', (8, 22)))

    def test_failed_inner_shot_is_not_retried_from_the_same_stance(self):
        data = reported_gap()
        data['teamOur']['summonRobotList'] = [data['teamOur']['summonRobotList'][1]]
        ledger, memory = decide(data)
        self.assertEqual(ledger.commands['31036'], command('attack', (9, 21)))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'31036': False}
        ledger, _ = decide(data, memory)
        self.assertNotEqual(ledger.commands.get('31036'), command('attack', (9, 21)))
        self.assertIn((31036, (8, 23), 10013, (9, 21)), memory.failed_shots)

    def test_unstalled_robot_still_supports_clearance_when_base_routes_are_closed(self):
        data = reported_frontage(closed=True)
        data['teamOur']['summonRobotList'][0]['pos']['y'] = 4
        memory = observed_memory(data)
        memory.approach_stalls[30035] = 3
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands['30035']['action'], 'attack')
        self.assertEqual(ledger.commands['30036']['action'], 'move')
        self.assertEqual(ledger.notes[30035]['conditions']['target_id'],
                         ledger.notes[30036]['conditions']['target_id'])
        self.assertEqual(ledger.notes[30036]['conditions']['assault_choice'], 'shared_clearance')


if __name__ == '__main__':
    unittest.main()
