"""First-night base shielding, legal movement and phase boundaries."""
from time import monotonic
import unittest

from test_agent import payload, setup_case, unit
from test_gate_guard import advance, gate_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.gate_guard import FirstNightBaseGuard, prepare_gate_guard
from agent.intelligence import Memory
from agent.model import Turn, pos
from agent.navigation import layout
from agent.sabotage import act_imps


def base_case(round_no=70, point=(8, 10)):
    data = payload(round_no, [unit(13, 'station', 5, 11, health=1500),
                              unit(14, 'imp', *point, health=500, backPackCapability=0)])
    data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=12, y=2), remain=10)]
    return data


def prepare(data, **config):
    turn, cfg, nav, ledger = setup_case(data, llm_enabled=False, **config)
    mem = Memory()
    mem.observe(turn, cfg)
    nav.memory = mem.movement
    towers, walls = layout(turn, cfg)
    guard = prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls)
    nav.gate_guard = guard
    return turn, nav, ledger, mem, guard


class FirstNightImpTests(unittest.TestCase):
    def test_returns_without_walls_or_robots_instead_of_destroying_mine(self):
        for round_no in (70, 129):
            data = base_case(round_no, point=(11, 2))
            turn, nav, ledger, mem, guard = prepare(data)
            self.assertIsInstance(guard, FirstNightBaseGuard)
            self.assertIn(14, ledger.used)
            act_imps(turn, mem.sabotage, nav, ledger)
            guard.finish()
            action = ledger.commands['14']
            self.assertEqual(action['action'], 'move')
            self.assertLess(turn.base_distance(pos(action['targetPos'][0])),
                            turn.base_distance(turn.imps[0].pos))
            self.assertEqual(ledger.notes[14]['reason'], 'imp_first_night_base_return')

    def test_front_posts_mirror_with_the_base_in_all_four_corners(self):
        for base, start, expected in (((3, 11), (6, 10), {(5, 10), (5, 11)}),
                                      ((10, 4), (8, 4), {(9, 3), (9, 4)}),
                                      ((3, 4), (6, 4), {(5, 3), (5, 4)}),
                                      ((10, 11), (8, 10), {(9, 10), (9, 11)})):
            with self.subTest(base=base):
                data = base_case(point=start)
                data['teamOur']['roles'][0]['pos'] = dict(zip(('x', 'y'), base))
                # Actual station position, rather than team label, sets the front.
                commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
                landing = pos(commands['14']['targetPos'][0])
                self.assertIn(landing, expected)
                self.assertEqual(Turn(data, Config()).base_distance(landing), 1)

    def test_holds_front_post_even_at_low_health_and_under_robot_pressure(self):
        for health in (500, 1):
            for robots in ([], [unit(90, 'largeRobot', 9, 10, attackRange=3)]):
                with self.subTest(health=health, robots=bool(robots)):
                    data = base_case(point=(7, 10))
                    data['teamOur']['roles'][1]['health'] = health
                    data['robot']['roles'] = robots
                    agent = Agent(Config(llm_enabled=False))
                    response = agent.decide(data)
                    self.assertNotIn('14', response['roleCommandMap'])
                    self.assertEqual(agent.decide(data), response)
                    data['roundNo'] += 1
                    self.assertNotIn('14', agent.decide(data)['roleCommandMap'])

    def test_post_selection_respects_occupants_reservations_and_operator_posts(self):
        for blocked_by in ('worker', 'reservation', 'operator', 'wall_blueprint', 'tower_blueprint'):
            with self.subTest(blocked_by=blocked_by):
                data = base_case()
                if blocked_by == 'worker':
                    data['teamOur']['roles'].append(unit(1, 'worker', 7, 10))
                turn, _, ledger, _, guard = prepare(data)
                if blocked_by == 'reservation':
                    ledger.reserved.add((7, 10))
                elif blocked_by == 'operator':
                    ledger.operator_posts[1] = (7, 10)
                elif blocked_by == 'wall_blueprint':
                    ledger.wall_cells.add((7, 10))
                elif blocked_by == 'tower_blueprint':
                    ledger.tower_cells.add((7, 10))
                guard.finish()
                self.assertEqual(ledger.commands['14'], command('move', (7, 11)))
                self.assertNotIn((7, 11), turn.blocked)

    def test_unreachable_posts_and_expired_budget_do_not_issue_illegal_moves(self):
        for blocked in ('occupied', 'sealed', 'deadline'):
            with self.subTest(blocked=blocked):
                data = base_case()
                if blocked == 'occupied':
                    data['teamOur']['roles'] += [unit(1, 'worker', 7, 10), unit(2, 'worker', 7, 11)]
                elif blocked == 'sealed':
                    data['teamOur']['roles'] += [unit(100+y, 'wall', 8, y) for y in range(15)]
                    data['teamOur']['roles'][1]['pos'] = dict(x=9, y=10)
                turn, nav, ledger, _, guard = prepare(data)
                original = turn.blocked
                if blocked == 'deadline':
                    nav.deadline = monotonic() - 1
                guard.finish()
                self.assertNotIn('14', ledger.commands)
                self.assertIn(14, ledger.used)
                self.assertIs(turn.blocked, original)

    def test_returns_from_rear_gate_to_front_and_keeps_rocket_fire(self):
        data, agent = gate_case(70), Agent(Config(llm_enabled=False))
        data['robot']['roles'] = [unit(90, 'largeRobot', 10, 10, attackRange=3,
                                      targetTeam='challenger')]
        for _ in range(7):
            commands = agent.decide(data)['roleCommandMap']
            shots = [c for c in commands.values() if c['action'] == 'attack']
            self.assertEqual(len(shots), 1)
            self.assertEqual(shots[0]['controllerId'], '11')
            advance(data, commands)
        imp = next(u for u in data['teamOur']['roles'] if u['id'] == 14)
        self.assertIn(pos(imp['pos']), {(7, 10), (7, 11)})
        self.assertNotIn('14', agent.decide(data)['roleCommandMap'])

    def test_front_guard_yields_for_an_actual_worker_route(self):
        data = base_case(point=(7, 10))
        data['teamOur']['roles'].append(unit(1, 'worker', 7, 9))
        data['teamOur']['roles'] += [unit(100+x, 'wall', x, 10)
                                     for x in range(15) if x not in (5, 6, 7)]
        turn, nav, ledger, _, guard = prepare(data)
        self.assertIsNone(nav.search(turn.workers[0], {(7, 12)}))
        self.assertEqual(guard.requests, {1: {(7, 12)}})
        guard.finish()
        self.assertEqual(ledger.commands['14'], command('move', (7, 11)))
        self.assertNotIn('1', ledger.commands)
        advance(data, ledger.commands)
        turn, nav, _, _, _ = prepare(data)
        self.assertIsNotNone(nav.search(turn.workers[0], {(7, 12)}))

    def test_accepted_collection_does_not_displace_front_guard(self):
        data = base_case(point=(7, 10))
        data['teamOur']['roles'].append(unit(1, 'worker', 7, 9))
        data['teamOur']['roles'] += [unit(100+x, 'wall', x, 10)
                                     for x in range(15) if x not in (5, 6, 7)]
        data['mapInfo']['zones'].append(dict(neutralType='copper', pos=dict(x=8, y=9), remain=10))
        turn, nav, ledger, _, guard = prepare(data)
        self.assertIsNone(nav.search(turn.workers[0], {(7, 12)}))
        self.assertTrue(ledger.add(1, command('collect', (8, 9))))
        guard.finish()
        self.assertNotIn('14', ledger.commands)

    def test_first_night_clears_old_mine_and_gate_memory(self):
        data = base_case()
        turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        mem = Memory()
        mem.sabotage.targets[14] = ((12, 2), 'copper')
        mem.sabotage.progress[14] = 3
        mem.sabotage.gate_states[14] = dict(day=1, recalling=True, yield_until=80)
        towers, walls = layout(turn, cfg)
        guard = prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls)
        self.assertIsInstance(guard, FirstNightBaseGuard)
        self.assertEqual(mem.sabotage.targets, {})
        self.assertEqual(mem.sabotage.progress, {})
        self.assertEqual(mem.sabotage.gate_states, {})

    def test_enemy_catch_still_escapes_instead_of_waiting_for_instant_death(self):
        data = base_case(point=(7, 10))
        data['teamEnemy']['roles'] = [unit(99, 'pioneer', 8, 10)]
        _, _, ledger, _, guard = prepare(data)
        self.assertIsNone(guard)
        self.assertEqual(ledger.commands['14']['action'], 'move')
        self.assertEqual(ledger.notes[14]['reason'], 'imp_escape')

    def test_daylight_later_nights_and_round_origin_keep_existing_sabotage(self):
        for origin in (0, 1):
            agent = Agent(Config(llm_enabled=False, round_origin=origin))
            for round_no in (66, 70, 129, 130, 200):
                data = base_case(round_no + origin, point=(11, 2))
                action = agent.decide(data)['roleCommandMap']['14']
                with self.subTest(origin=origin, round_no=round_no):
                    self.assertEqual(action['action'], 'move' if round_no in (70, 129) else 'destroy')

    def test_dead_imp_or_base_does_not_activate_front_guard(self):
        for dead in (13, 14):
            data = base_case()
            next(u for u in data['teamOur']['roles'] if u['id'] == dead)['health'] = 0
            self.assertIsNone(prepare(data)[-1])


if __name__ == '__main__':
    unittest.main()
