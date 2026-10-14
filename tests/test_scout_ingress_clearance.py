"""Finite observations for the scout's five-turn ingress exclusion."""
import copy
import unittest

from test_agent import setup_case, unit
from test_first_day_wall_block import block_case, memory_for, seal_ring
from test_robot_assault import controlled_robot
from agent.brain import Agent
from agent.config import Config
from agent.model import distance, pos
from agent.robot_assault import RobotAssaultMemory, act_robots
from agent.scouting import act_night_scout, yield_wall_entry


ENTRY = (28, 8)
# Gap, two outer approach cells, one inner cell; eight-direction margin.
STRIP = {(x, y) for x in range(25, 31) for y in range(7, 10)}


def case(round_no=70, point=ENTRY, entry=ENTRY):
    data = seal_ring(block_case(round_no), entry)
    data['teamOur']['roles'][1]['pos'] = dict(zip(('x', 'y'), point))
    return data


def decide(data, memory, robots=None):
    turn, _, nav, ledger = setup_case(data)
    scout = yield_wall_entry(turn, memory, nav, ledger)
    if robots is not None:
        ledger.robot_assault_entry = memory.wall_entry
        act_robots(turn, robots, nav, ledger)
    scout = scout or act_night_scout(turn, memory, nav, ledger)
    return turn, ledger, scout


def protected_memory(until=76, cleared=True):
    memory = memory_for(ENTRY)
    memory.ingress_entry, memory.ingress_until = ENTRY, until
    memory.ingress_cleared = cleared
    return memory


class ScoutIngressClearanceTests(unittest.TestCase):
    def test_reported_frontage_does_not_reoccupy_approach_or_margin(self):
        data = case()
        data['teamEnemy']['roles'].append(unit(50, 'worker', 29, 9))
        memory, robots = memory_for(ENTRY), RobotAssaultMemory()
        observed_exit = None
        for tick in range(70, 80):
            data['roundNo'] = tick
            if tick == 71:
                # In the supplied log the worker yields before these robots
                # first appear; placing all three around it on r70 seals it in.
                data['teamOur']['summonRobotList'] = [
                    controlled_robot(30035, 27, 8), controlled_robot(30036, 27, 6),
                    controlled_robot(30037, 27, 9)]
            before = copy.deepcopy(data)
            turn, ledger, scout = decide(data, memory, robots)
            self.assertEqual(data, before)
            if scout.pos not in STRIP and observed_exit is None:
                observed_exit = tick
            action = ledger.commands.get('10', {})
            if action.get('action') == 'move':
                step = pos(action['targetPos'][0])
                self.assertEqual(distance(scout.pos, step), 1)
                self.assertNotIn(step, turn.blocked)
                if observed_exit is not None:
                    self.assertNotIn(step, STRIP)
                data['teamOur']['roles'][1]['pos'] = action['targetPos'][0]
        self.assertIsNotNone(observed_exit)
        self.assertLessEqual(observed_exit, 73)
        self.assertIsNotNone(memory.ingress_entry)  # Outside robots still wait.

    def test_round_76_neighbour_clears_instead_of_returning_to_27_8(self):
        data = case(76, (27, 7))
        data['teamOur']['summonRobotList'] = [
            controlled_robot(30035, 31, 8), controlled_robot(30036, 27, 6),
            controlled_robot(30037, 27, 9)]
        _, ledger, _ = decide(data, protected_memory(cleared=False))
        step = pos(ledger.commands['10']['targetPos'][0])
        self.assertNotIn(step, STRIP)
        self.assertEqual(ledger.notes[10]['reason'], 'night_scout_clear_ingress')

    def test_five_complete_decisions_start_at_observed_exit(self):
        memory = protected_memory(until=0, cleared=False)
        for tick in range(72, 77):
            _, ledger, _ = decide(case(tick, (27, 6)), memory)
            self.assertEqual(memory.ingress_until, 77)
            self.assertEqual(memory.ingress_entry, ENTRY)
            if ledger.commands.get('10', {}).get('action') == 'move':
                self.assertNotIn(pos(ledger.commands['10']['targetPos'][0]), STRIP)
        decide(case(77, (27, 6)), memory)
        self.assertIsNone(memory.ingress_entry)

    def test_submitted_or_failed_yield_does_not_start_cooldown(self):
        memory = memory_for(ENTRY)
        for tick in (70, 71, 75):
            _, ledger, _ = decide(case(tick), memory)
            self.assertEqual(ledger.commands['10']['action'], 'move')
            self.assertFalse(memory.ingress_cleared)
            self.assertEqual(memory.ingress_until, 0)

    def test_nearby_outside_robot_extends_until_observed_inside(self):
        memory = protected_memory(until=75)
        data = case(80, (27, 6))
        data['teamOur']['summonRobotList'] = [controlled_robot(x=27, y=8)]
        decide(data, memory)
        self.assertEqual(memory.ingress_entry, ENTRY)
        data['roundNo'] = 81
        data['teamOur']['summonRobotList'][0]['pos'] = {'x': 29, 'y': 8}
        decide(data, memory)
        self.assertIsNone(memory.ingress_entry)

    def test_unrelated_public_dead_or_other_face_robots_do_not_extend(self):
        for kind in ('remote', 'inner_side', 'public', 'dead', 'closed'):
            with self.subTest(kind=kind):
                data = case(80, (27, 6))
                robot = controlled_robot(x=27, y=8)
                if kind == 'remote':
                    robot['pos'] = {'x': 20, 'y': 8}
                if kind == 'inner_side':
                    robot['pos'] = {'x': 30, 'y': 6}
                if kind == 'dead':
                    robot['health'] = 0
                if kind == 'closed':
                    data['teamEnemy']['roles'].append(unit(600, 'wall', *ENTRY))
                if kind == 'public':
                    data['robot']['roles'] = [robot]
                else:
                    data['teamOur']['summonRobotList'] = [robot]
                memory = protected_memory(until=75)
                decide(data, memory)
                self.assertIsNone(memory.ingress_entry)

    def test_all_wall_normals_clear_the_full_one_cell_margin(self):
        faces = [((28, 8), (25, 30, 7, 9)), ((33, 10), (31, 36, 9, 11)),
                 ((30, 7), (29, 31, 4, 9)), ((30, 12), (29, 31, 10, 15))]
        for entry, (x0, x1, y0, y1) in faces:
            with self.subTest(entry=entry):
                strip = {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
                data, memory = case(entry=entry, point=entry), memory_for(entry)
                exited = False
                for tick in range(70, 74):
                    data['roundNo'] = tick
                    _, ledger, scout = decide(data, memory)
                    exited |= scout.pos not in strip
                    action = ledger.commands.get('10', {})
                    if action.get('action') == 'move':
                        step = pos(action['targetPos'][0])
                        if exited:
                            self.assertNotIn(step, strip)
                        data['teamOur']['roles'][1]['pos'] = action['targetPos'][0]
                self.assertTrue(exited)

    def test_normal_scout_cannot_cross_strip_to_reach_a_post(self):
        data = case(73, (24, 8))
        # The only physical way towards any observation post crosses the strip.
        walls = [(x, y) for x in range(23, 32) for y in (6, 10)]
        walls += [(23, y) for y in range(7, 10)]
        data['teamOur']['roles'] += [unit(700+i, 'wall', *p) for i, p in enumerate(walls)]
        memory = protected_memory(until=78)
        _, ledger, _ = decide(data, memory)
        self.assertNotIn('10', ledger.commands)

    def test_emergency_can_leave_trap_via_strip_and_restarts_observed_cooldown(self):
        data = case(73, (26, 6))
        data['robot']['roles'] = [controlled_robot(50000, 25, 2)]
        blocked = [(25, 5), (25, 6), (25, 7), (26, 5), (27, 5), (27, 6), (27, 7)]
        data['teamOur']['roles'] += [unit(700+i, 'wall', *p) for i, p in enumerate(blocked)]
        memory = protected_memory(until=76)
        _, ledger, _ = decide(data, memory)
        self.assertEqual(pos(ledger.commands['10']['targetPos'][0]), (26, 7))
        facts = ledger.notes[10]['conditions']
        self.assertTrue(facts['ingress_emergency'])
        self.assertLess(facts['next_risk'], facts['observed_risk'])
        decide(case(74, (26, 7)), memory)
        self.assertFalse(memory.ingress_cleared)
        decide(case(75, (26, 6)), memory)
        self.assertEqual(memory.ingress_until, 80)

    def test_blocked_or_dizzy_scout_retains_guard_without_illegal_move(self):
        for dizzy in (False, True):
            data = case()
            if dizzy:
                data['teamOur']['roles'][1]['abnormalState'] = 'dizzy'
            else:
                data['teamOur']['roles'] += [unit(700+i, 'wall', x, y)
                    for i, (x, y) in enumerate((x, y) for x in range(27, 30)
                    for y in range(7, 10) if (x, y) != ENTRY)]
            memory = memory_for(ENTRY)
            _, ledger, _ = decide(data, memory)
            self.assertNotIn('10', ledger.commands)
            self.assertEqual(memory.ingress_entry, ENTRY)
            self.assertEqual(memory.ingress_until, 0)

    def test_day_base_change_cancellation_and_worker_death_clear_old_guard(self):
        for change in ('day', 'base_missing', 'base_changed', 'cancelled', 'worker_dead'):
            with self.subTest(change=change):
                data, memory = case(73, (27, 6)), protected_memory()
                if change == 'day':
                    data['roundNo'] = 130
                elif change == 'base_missing':
                    data['teamEnemy']['roles'] = []
                elif change == 'base_changed':
                    data['teamEnemy']['roles'][0]['id'] = 999
                elif change == 'cancelled':
                    memory.cancelled = True
                else:
                    data['teamOur']['roles'][1]['health'] = 0
                decide(data, memory)
                self.assertIsNone(memory.ingress_entry)

    def test_agent_duplicate_request_does_not_advance_clearance_state(self):
        data = case(70)
        agent = Agent(Config(llm_enabled=False))
        # Establish the same first-day worker/entry ownership as the real game.
        day = copy.deepcopy(data)
        day['roundNo'] = 69
        agent.decide(day)
        memory = next(iter(agent.sessions.values()))
        memory.night_scout = memory_for(ENTRY)
        first = agent.decide(data)
        state = copy.deepcopy(memory.night_scout)
        self.assertEqual(agent.decide(copy.deepcopy(data)), first)
        self.assertEqual(memory.night_scout, state)
        self.assertEqual(state.ingress_entry, ENTRY)


if __name__ == '__main__':
    unittest.main()
