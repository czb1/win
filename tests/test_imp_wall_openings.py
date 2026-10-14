"""Dynamic wall holes, committed traffic and release after an observed wave."""
from time import monotonic
import unittest

from test_agent import payload, setup_case, unit
from test_gate_guard import GATE, advance, gate_case, guard_setup
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.gate_guard import NightWallGuard, prepare_gate_guard
from agent.intelligence import Memory
from agent.model import Turn, pos
from agent.navigation import layout
from agent.sabotage import act_imps


def remove_wall(data, point):
    data['teamOur']['roles'] = [u for u in data['teamOur']['roles']
                              if u['roleType'] != 'wall' or pos(u['pos']) != point]


class ImpWallOpeningTests(unittest.TestCase):
    def test_second_fourth_and_eighth_nights_fill_actual_broken_wall(self):
        for day in (2, 4, 8):
            data = gate_case((day - 1) * 130 + 80, imp=(8, 10))
            remove_wall(data, (8, 10))
            _, _, ledger, _, guard = guard_setup(data)
            self.assertIsInstance(guard, NightWallGuard)
            guard.finish()
            self.assertEqual(guard.state['gate'], (8, 10))
            self.assertNotIn('14', ledger.commands)
            self.assertTrue(ledger.notes[14]['conditions']['bracketed'])

    def test_bracketed_gap_precedes_a_nearer_open_corner(self):
        data = gate_case(210, imp=(8, 8))
        remove_wall(data, (8, 8))
        _, _, ledger, _, guard = guard_setup(data)
        guard.finish()
        self.assertEqual(guard.state['gate'], GATE)
        self.assertEqual(ledger.commands['14']['action'], 'move')

    def test_door_built_shut_does_not_disable_other_broken_wall(self):
        data = gate_case(210, imp=(8, 10))
        remove_wall(data, (8, 10))
        data['teamOur']['roles'].append(unit(900, 'wall', *GATE))
        _, _, ledger, _, guard = guard_setup(data)
        self.assertIsInstance(guard, NightWallGuard)
        guard.finish()
        self.assertEqual(guard.state['gate'], (8, 10))
        self.assertNotIn('14', ledger.commands)

    def test_actual_wall_gap_mirrors_in_four_base_corners(self):
        for base in ((3, 11), (10, 4), (3, 4), (10, 11)):
            cfg = Config(llm_enabled=False)
            data = payload(210, [unit(13, 'station', *base)])
            towers, walls = layout(Turn(data, cfg), cfg)
            x = max(p[0] for p in walls)
            ys = sorted({p[1] for p in walls})
            gap = (x, ys[2])
            data['teamOur']['roles'] += [unit(14, 'imp', *gap, health=500),
                *[unit(100+i, 'wall', *p) for i, p in enumerate(walls) if p != gap]]
            data['robot']['roles'] = [unit(90, 'largeRobot', 7, 7, targetTeam='challenger')]
            turn, _, nav, ledger = setup_case(data)
            guard = prepare_gate_guard(turn, cfg, Memory(), nav, ledger, towers, walls)
            self.assertIsInstance(guard, NightWallGuard)
            guard.finish()
            self.assertEqual(guard.state['gate'], gap)
            self.assertNotIn('14', ledger.commands)

    def test_guard_does_not_enter_only_door_a_moving_worker_will_need(self):
        for kind in ('worker', 'pioneer'):
            data = gate_case(210, imp=(2, 9), worker=(4, 11))
            data['teamOur']['roles'] = [u for u in data['teamOur']['roles']
                                      if u['roleType'] != 'pioneer']
            data['teamOur']['roles'][1]['roleType'] = kind
            turn, nav, ledger, _, guard = guard_setup(data)
            helper = turn.heroes[0]
            route = nav.search(helper, {(1, 7)})
            self.assertIsNotNone(route)
            self.assertTrue(ledger.add(helper.id, command('move', route[1])))
            guard.finish()
            if '14' in ledger.commands:
                self.assertNotEqual(pos(ledger.commands['14']['targetPos'][0]), GATE)
            self.assertEqual(ledger.commands[str(helper.id)], command('move', route[1]))

    def test_free_other_gap_can_be_filled_while_door_is_needed_for_traffic(self):
        data = gate_case(210, imp=(8, 10), worker=(4, 11))
        remove_wall(data, (8, 10))
        turn, nav, ledger, _, guard = guard_setup(data)
        route = nav.search(turn.workers[0], {(1, 7)})
        self.assertTrue(ledger.add(1, command('move', route[1])))
        guard.finish()
        self.assertEqual(guard.state['gate'], (8, 10))
        self.assertNotIn('14', ledger.commands)

    def test_reservations_and_operator_post_exclude_a_gap(self):
        for mode in ('reserved', 'operator'):
            data = gate_case(210, imp=(8, 9))
            remove_wall(data, (8, 10))
            _, _, ledger, _, guard = guard_setup(data)
            if mode == 'reserved':
                ledger.reserved.add((8, 10))
            else:
                ledger.operator_posts[1] = (8, 10)
            guard.finish()
            self.assertEqual(guard.state['gate'], GATE)
            self.assertNotEqual(pos(ledger.commands['14']['targetPos'][0]), (8, 10))

    def test_empty_night_opening_waits_for_spawn_then_cleared_wave_releases(self):
        data, agent = gate_case(200, imp=(11, 2)), Agent(Config(llm_enabled=False))
        data['teamOur']['roles'] = [u for u in data['teamOur']['roles']
                                  if u['roleType'] not in ('worker', 'pioneer')]
        data['robot']['roles'] = []
        self.assertEqual(agent.decide(data)['roleCommandMap']['14']['action'], 'move')
        data['roundNo'] = 201
        self.assertEqual(agent.decide(data)['roleCommandMap']['14']['action'], 'move')
        data['roundNo'] = 202
        data['robot']['roles'] = [unit(90, 'largeRobot', 10, 14, targetTeam='defender')]
        self.assertEqual(agent.decide(data)['roleCommandMap']['14']['action'], 'move')
        data['roundNo'] = 203
        data['robot']['roles'] = []
        self.assertEqual(agent.decide(data)['roleCommandMap']['14'], command('destroy', (12, 2)))

    def test_cleared_wave_leaves_home_then_resumes_enemy_mine(self):
        data, agent = gate_case(210), Agent(Config(llm_enabled=False))
        data['teamOur']['roles'] = [u for u in data['teamOur']['roles']
                                  if u['roleType'] not in ('worker', 'pioneer')]
        agent.decide(data)
        data['roundNo'] = 211
        data['robot']['roles'] = []
        for _ in range(4):
            commands = agent.decide(data)['roleCommandMap']
            advance(data, commands)
            self.assertNotEqual(pos(next(u for u in data['teamOur']['roles']
                                         if u['roleType'] == 'imp')['pos']), GATE)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.sabotage.targets[14], ((12, 2), 'copper'))
        self.assertEqual(mem.sabotage.gate_states, {})

    def test_quiet_night_can_leave_through_only_broken_blueprint(self):
        data = gate_case(210, imp=(8, 10), worker=(1, 1))
        remove_wall(data, (8, 10))
        data['teamOur']['roles'].append(unit(900, 'wall', *GATE))
        data['robot']['roles'] = []
        turn, nav, ledger, mem, guard = guard_setup(data)
        self.assertIsNone(guard)
        act_imps(turn, mem.sabotage, nav, ledger)
        self.assertEqual(ledger.commands['14']['action'], 'move')
        self.assertGreaterEqual(pos(ledger.commands['14']['targetPos'][0])[0], 9)

    def test_expired_budget_uses_legal_clearance_for_blocked_home_route(self):
        data = gate_case(210, worker=(4, 9))
        turn, nav, ledger, _, guard = guard_setup(data)
        self.assertIsNone(nav.search(turn.workers[0], {(1, 7)}))
        nav.deadline = monotonic() - 1
        guard.finish()
        self.assertEqual(ledger.commands['14']['action'], 'move')
        self.assertNotIn(pos(ledger.commands['14']['targetPos'][0]), turn.blocked)


if __name__ == '__main__':
    unittest.main()
