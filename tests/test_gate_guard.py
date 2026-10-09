"""Focused gate traffic, dusk/night boundaries and three-rocket dispatch."""
from time import monotonic
import unittest

from test_agent import ROOT, payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import build
from agent.gate_guard import prepare_gate_guard
from agent.intelligence import Memory
from agent.model import Turn, distance, neighbours, pos
from agent.navigation import layout
from agent.projectiles import wall_gates
from agent.sabotage import act_imps


GATE, POSTS, TOWERS = (3, 10), {(2, 9), (2, 11)}, [(4, 12), (5, 12), (4, 10)]


def gate_case(round_no=70, imp=GATE, worker=(11, 2), pioneer=(4, 11)):
    data = payload(round_no, [unit(13, 'station', 5, 11, health=1500),
        unit(1, 'worker', *worker, health=500),
        unit(11, 'pioneer', *pioneer, health=500),
        unit(14, 'imp', *imp, health=500, backPackCapability=0),
        *[unit(20+i, 'rocket', *p, level=3, attackRange=20) for i, p in enumerate(TOWERS)]])
    walls = layout(Turn(data, Config()), Config())[1]
    data['teamOur']['roles'] += [unit(100+i, 'wall', *p, health=2000, level=3)
                               for i, p in enumerate(walls)]
    data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=12, y=2), remain=10)]
    data['vendorShopList'] = [dict(name='copper', price=3)]
    return data


def guard_setup(data):
    turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
    mem = Memory()
    mem.observe(turn, cfg)
    nav.memory = mem.movement
    towers, walls = layout(turn, cfg)
    guard = prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls)
    nav.gate_guard = guard
    return turn, nav, ledger, mem, guard


def advance(data, commands):
    occupied = {pos(u['pos']) for u in data['teamOur']['roles'] if u['health'] > 0}
    occupied |= {pos(u['pos']) for u in data['robot']['roles'] if u['health'] > 0}
    occupied |= {pos(u['pos']) for u in data['mapInfo']['zones']}
    destinations = set()
    for uid, action in commands.items():
        if action['action'] != 'move':
            continue
        target = pos(action['targetPos'][0])
        assert target not in occupied and target not in destinations, (uid, target)
        actor = next(u for u in data['teamOur']['roles'] if u['id'] == int(uid))
        assert distance(pos(actor['pos']), target) == 1
        destinations.add(target)
        actor['pos'] = action['targetPos'][0]
    data['roundNo'] += 1


class ThreeRocketTests(unittest.TestCase):
    def test_defaults_build_three_rockets(self):
        for cfg in (Config(), Config.load(ROOT / 'config/default.json')):
            self.assertEqual(cfg.loadout, ['rocket'] * 3)
            for missing in range(3):
                data = payload(10, [unit(13, 'station', 5, 11), unit(1, 'worker', 4, 11),
                    *[unit(20+i, 'rocket', *p) for i, p in enumerate(TOWERS) if i != missing]])
                turn, _, nav, ledger = setup_case(data)
                self.assertTrue(build(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                                      TOWERS, lambda _: 'rocket'))
                self.assertEqual(ledger.commands['1'], command('build', TOWERS[missing], name='rocket'))

    def test_pioneer_rotates_three_rockets_and_workers_keep_mining(self):
        data, agent = gate_case(), Agent(Config(llm_enabled=False))
        data['teamOur']['roles'].append(unit(2, 'worker', 13, 2, health=500))
        data['robot']['roles'] = [unit(90, 'largeRobot', 8, 10, health=500,
                                      targetTeam='challenger', attackRange=3)]
        for offset, expected in enumerate(('20', '21', '22')):
            data['roundNo'] = 70 + offset
            for tower in data['teamOur']['roles'][4:7]:
                tower['cooldown'] = 0 if str(tower['id']) == expected else 2
            commands = agent.decide(data)['roleCommandMap']
            self.assertEqual([uid for uid, c in commands.items() if c['action'] == 'attack'], [expected])
            self.assertEqual(commands[expected]['controllerId'], '11')
            self.assertEqual(commands['1']['action'], 'collect')
            self.assertEqual(commands['2']['action'], 'collect')
            self.assertNotIn('11', commands)
            self.assertIsNone(next(iter(agent.sessions.values())).gatling_operator_id)

    def test_sole_gap_faces_a_tower_before_the_base_in_all_four_corners(self):
        for x, y in ((3, 11), (10, 4), (3, 4), (10, 11)):
            turn = Turn(payload(roles=[unit(13, 'station', x, y)]), Config())
            towers, walls = layout(turn, Config())
            gates = wall_gates(turn, walls)
            self.assertEqual(len(walls), 19)
            self.assertEqual(len(gates), 1)
            gate = next(iter(gates))
            self.assertNotIn(gate, walls)
            facing = (gate[0] + (1 if gate[0] < x else -1), gate[1])
            self.assertIn(facing, towers)
            self.assertNotIn(facing, turn.station.cells)
            self.assertTrue(any(p not in turn.station.cells and p not in walls and p not in towers
                                and all(distance(p, t) == 1 for t in towers)
                                for p in neighbours(towers[0])))


class GateGuardTests(unittest.TestCase):
    def test_last_day_turn_stages_beside_gap_and_first_night_enters(self):
        data, agent = gate_case(69), Agent(Config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['14'], command('move', GATE))

    def test_far_imp_recalls_before_last_turn(self):
        data, agent = gate_case(64, imp=(13, 4)), Agent(Config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['14']['action'], 'move')
        state = next(iter(agent.sessions.values())).sabotage.gate_states[14]
        self.assertTrue(state['recalling'])
        self.assertIn(state['post'], POSTS)

    def test_robot_damage_does_not_make_guard_flee(self):
        data = gate_case()
        data['robot']['roles'] = [unit(90, 'largeRobot', 2, 10, health=500, attackRange=3)]
        turn, _, ledger, mem, guard = guard_setup(data)
        act_imps(turn, mem.sabotage, guard.nav, ledger)
        guard.finish()
        self.assertNotIn('14', ledger.commands)
        self.assertEqual(ledger.notes[14]['reason'], 'imp_guarding_gate')
        self.assertTrue(ledger.notes[14]['conditions']['at_gate'])

    def test_exit_without_remembered_mine_clears_gate_before_worker_moves(self):
        data, agent = gate_case(worker=(4, 9)), Agent(Config(llm_enabled=False))
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=1, y=7), remain=10)]
        commands = agent.decide(data)['roleCommandMap']
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)
        self.assertNotIn('1', commands)  # No move into the imp's observed cell.
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1'], command('move', GATE))
        self.assertNotIn('14', commands)
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'move')
        self.assertNotEqual(pos(commands['1']['targetPos'][0]), GATE)
        self.assertNotIn('14', commands)
        advance(data, commands)

    def test_far_inside_worker_can_request_exit_instead_of_stalling(self):
        data = gate_case(worker=(7, 9))
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=1, y=7), remain=10)]
        commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)

    def test_outside_wall_watcher_enters_and_imp_waits_for_observed_clearance(self):
        data, agent = gate_case(460, worker=(2, 10)), Agent(Config(llm_enabled=False))
        data['teamOur']['roles'][1]['backpack'] = ['WallFixer']
        commands = agent.decide(data)['roleCommandMap']
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1'], command('move', GATE))
        self.assertNotIn('14', commands)
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'move')
        self.assertNotIn('14', commands)
        advance(data, commands)

    def test_pioneer_also_gets_access_to_common_control_post(self):
        data, agent = gate_case(pioneer=(2, 10)), Agent(Config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['11'], command('move', GATE))
        self.assertNotIn('14', commands)
        advance(data, commands)

    def test_accepted_local_collection_does_not_trigger_unneeded_yield(self):
        data = gate_case(worker=(7, 9))
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=7, y=10), remain=10),
                                  dict(neutralType='copper', pos=dict(x=1, y=7), remain=10)]
        commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'collect')
        self.assertNotIn('14', commands)

    def test_guard_returns_when_yield_window_ends_and_no_traffic_remains(self):
        data, agent = gate_case(), Agent(Config(llm_enabled=False))
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        mem.sabotage.gate_states[14].update(yield_until=72, post=(2, 9))
        data['teamOur']['roles'][3]['pos'] = dict(x=2, y=9)
        data['roundNo'] = 72
        self.assertNotIn('14', agent.decide(data)['roleCommandMap'])
        data['roundNo'] = 73
        self.assertEqual(agent.decide(data)['roleCommandMap']['14'], command('move', GATE))

    def test_worker_occupying_gate_is_never_swapped_or_displaced(self):
        data = gate_case(imp=(2, 9), worker=GATE)
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=2, y=10), remain=10)]
        commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'collect')
        self.assertNotIn('14', commands)

    def test_reserved_side_step_uses_other_post(self):
        turn, nav, ledger, _, guard = guard_setup(gate_case(worker=(4, 9)))
        ledger.reserved.add((2, 9))
        self.assertIsNone(nav.search(turn.workers[0], {(1, 7)}))
        guard.finish()
        self.assertEqual(ledger.commands['14'], command('move', (2, 11)))

    def test_waiting_imp_clears_an_outside_aisle_too(self):
        data = gate_case(imp=(2, 9), worker=GATE)
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=1, y=7), remain=10)]
        data['mapInfo']['zones'] += [dict(neutralType='vendor', pos=dict(x=x, y=10))
                                    for x in range(15) if x not in (3, 4, 5, 6)]
        turn, nav, ledger, _, guard = guard_setup(data)
        self.assertIsNone(nav.search(turn.workers[0], {(1, 8)}))
        guard.finish()
        self.assertEqual(ledger.commands['14']['action'], 'move')
        target = pos(ledger.commands['14']['targetPos'][0])
        original = turn.blocked
        turn.blocked = (original - {(2, 9)}) | {target}
        self.assertIsNotNone(nav._search(turn.workers[0], {(1, 8)}))
        turn.blocked = original

    def test_synthetic_danger_mask_is_not_removed_to_request_mining_exit(self):
        turn, nav, ledger, _, guard = guard_setup(gate_case(worker=(4, 9)))
        original = turn.blocked
        turn.blocked = original | {(2, 10)}
        self.assertIsNone(nav.search(turn.workers[0], {(1, 7)}))
        self.assertEqual(guard.requests, {})
        turn.blocked = original
        guard.finish()
        self.assertNotIn('14', ledger.commands)

    def test_failed_or_expired_route_search_restores_occupancy(self):
        turn, nav, _, _, guard = guard_setup(gate_case(worker=(4, 9)))
        original = turn.blocked
        self.assertIsNone(nav.search(turn.workers[0], {(1, 7)}))
        self.assertIs(turn.blocked, original)
        nav.deadline = monotonic() - 1
        guard.finish()
        self.assertIs(turn.blocked, original)
        self.assertIn(pos(guard.ledger.commands['14']['targetPos'][0]), POSTS)

    def test_explicit_layout_without_one_aligned_gap_does_not_invent_guard(self):
        for walls in ([], [[8, 9], [8, 10], [8, 11]]):
            data = gate_case()
            turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit',
                                              weapon_cells=[list(p) for p in TOWERS], wall_cells=walls)
            mem = Memory()
            mem.observe(turn, cfg)
            actual_towers, actual_walls = layout(turn, cfg)
            self.assertIsNone(prepare_gate_guard(turn, cfg, mem, nav, ledger, actual_towers, actual_walls))

    def test_next_morning_releases_night_guard_for_daytime_sabotage(self):
        data, agent = gate_case(), Agent(Config(llm_enabled=False))
        agent.decide(data)
        data['roundNo'] = 130
        data['teamOur']['roles'][3]['pos'] = dict(x=2, y=9)
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=1, y=0), remain=10)]
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['14']['action'], 'move')
        self.assertEqual(next(iter(agent.sessions.values())).sabotage.gate_states, {})

    def test_catch_threat_retains_survival_escape(self):
        data = gate_case()
        data['teamEnemy']['roles'] = [unit(99, 'pioneer', 2, 10)]
        commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['14']['action'], 'move')
        self.assertNotEqual(pos(commands['14']['targetPos'][0]), GATE)

    def test_guard_staging_and_worker_exit_mirror_in_four_corners(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            data = gate_case(worker=(4, 9))
            data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=1, y=7), remain=10)]
            expected_posts = {(14-x if flip_x else x, 14-y if flip_y else y) for x, y in POSTS}
            for group in (data['teamOur']['roles'], data['mapInfo']['zones']):
                for u in group:
                    p = u['pos']
                    if flip_x:
                        p['x'] = (13 if u.get('roleType') == 'station' else 14) - p['x']
                    if flip_y:
                        p['y'] = (15 if u.get('roleType') == 'station' else 14) - p['y']
            commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
            self.assertIn(pos(commands['14']['targetPos'][0]), expected_posts)


if __name__ == '__main__':
    unittest.main()
