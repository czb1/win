"""Focused gate traffic, dusk/night boundaries and three-rocket dispatch."""
import json
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


def logged_morning_case():
    return json.loads((ROOT / 'tests/fixtures/daytime_gate_turn.json').read_text(encoding='utf-8'))


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
    # An opponent-bound robot can still attack an imp blocking its route.
    # Keep a local wave in traffic tests without diverting our gunner/workers.
    data['robot']['roles'] = [unit(90, 'largeRobot', 10, 14, health=500,
                                  targetTeam='defender', attackRange=3)]
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


class GateGuardEligibilityTests(unittest.TestCase):
    def test_every_unbuilt_or_destroyed_perimeter_wall_releases_night_sabotage(self):
        complete = gate_case(imp=(11, 2), worker=(1, 1))
        wall_ids = [u['id'] for u in complete['teamOur']['roles'] if u['roleType'] == 'wall']
        for uid in wall_ids:
            for destroyed in (False, True):
                with self.subTest(wall=uid, destroyed=destroyed):
                    data = gate_case(imp=(11, 2), worker=(1, 1))
                    if destroyed:
                        next(u for u in data['teamOur']['roles'] if u['id'] == uid)['health'] = 0
                    else:
                        data['teamOur']['roles'] = [u for u in data['teamOur']['roles'] if u['id'] != uid]
                    agent = Agent(Config(llm_enabled=False))
                    commands = agent.decide(data)['roleCommandMap']
                    self.assertEqual(commands['14'], command('destroy', (12, 2)))
                    self.assertEqual(next(iter(agent.sessions.values())).sabotage.gate_states, {})

    def test_unbuilt_perimeter_does_not_recall_at_dusk(self):
        data = gate_case(69, imp=(11, 2), worker=(1, 1))
        data['teamOur']['roles'] = [u for u in data['teamOur']['roles'] if u['roleType'] != 'wall']
        turn, _, nav, ledger = setup_case(data, llm_enabled=False)
        mem = Memory()
        mem.observe(turn, Config())
        towers, walls = layout(turn, Config())
        self.assertIsNone(prepare_gate_guard(turn, Config(), mem, nav, ledger, towers, walls))
        self.assertNotIn(14, ledger.used)

    def test_wall_built_in_gate_disables_guard(self):
        data = gate_case()
        data['teamOur']['roles'].append(unit(200, 'wall', *GATE))
        _, _, ledger, mem, guard = guard_setup(data)
        self.assertIsNone(guard)
        self.assertNotIn(14, ledger.used)
        self.assertEqual(mem.sabotage.gate_states, {})

    def test_no_living_local_robot_releases_stale_guard_and_keeps_channel(self):
        for robots in ([], [unit(90, 'largeRobot', 2, 10, health=0)],
                       [unit(90, 'largeRobot', 14, 14, attackRange=3, targetTeam='challenger')]):
            with self.subTest(robots=robots):
                data = gate_case(imp=(11, 2), worker=(1, 1))
                data['robot']['roles'] = robots
                turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
                mem = Memory()
                mem.observe(turn, cfg)
                mem.sabotage.gate_states[14] = dict(day=turn.day, recalling=True, yield_until=75)
                mem.sabotage.targets[14] = (12, 2), 'copper'
                mem.sabotage.progress[14] = 2
                towers, walls = layout(turn, cfg)
                self.assertIsNone(prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls))
                self.assertEqual(mem.sabotage.gate_states, {})
                self.assertNotIn(14, ledger.used)
                self.assertEqual(mem.sabotage.targets[14], ((12, 2), 'copper'))
                self.assertEqual(mem.sabotage.progress[14], 2)
                act_imps(turn, mem.sabotage, nav, ledger)
                self.assertEqual(ledger.commands['14'], command('destroy', (12, 2)))

    def test_local_robot_boundary_and_long_attack_range(self):
        for point, attack_range, expected in (((10, 4), 3, True), ((11, 3), 3, False),
                                              ((14, 0), 8, True), ((14, 0), 7, False)):
            with self.subTest(point=point, attack_range=attack_range):
                data = gate_case()
                data['robot']['roles'] = [unit(90, 'largeRobot', *point, attackRange=attack_range)]
                _, _, ledger, _, guard = guard_setup(data)
                self.assertEqual(guard is not None, expected)
                self.assertEqual(14 in ledger.used, expected)

    def test_nearby_opponent_bound_and_stunned_robots_still_require_guard(self):
        for target_team in ('challenger', 'defender', ''):
            for abnormal in ('', 'dizzy'):
                with self.subTest(target_team=target_team, abnormal=abnormal):
                    data = gate_case()
                    data['robot']['roles'] = [unit(90, 'largeRobot', 2, 10, attackRange=3,
                                                  targetTeam=target_team, abnormalState=abnormal)]
                    self.assertIsNotNone(guard_setup(data)[-1])

    def test_own_summoned_robot_does_not_keep_gate_guard_active(self):
        data = gate_case(imp=(11, 2), worker=(1, 1))
        data['teamOur']['summonRobotList'] = data['robot']['roles']
        _, _, ledger, mem, guard = guard_setup(data)
        self.assertIsNone(guard)
        self.assertNotIn(14, ledger.used)
        self.assertEqual(mem.sabotage.gate_states, {})

    def test_guard_leaves_when_local_wave_clears_and_returns_for_new_wave(self):
        data, agent = gate_case(), Agent(Config(llm_enabled=False))
        self.assertNotIn('14', agent.decide(data)['roleCommandMap'])
        mem = next(iter(agent.sessions.values()))
        mem.sabotage.gate_states[14].update(recalling=True, yield_until=75)
        data['roundNo'] = 71
        data['robot']['roles'] = []
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['14']['action'], 'move')
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)
        self.assertEqual(mem.sabotage.gate_states, {})
        data['roundNo'] = 72
        data['teamOur']['roles'][3]['pos'] = commands['14']['targetPos'][0]
        data['robot']['roles'] = [unit(90, 'largeRobot', 9, 14, attackRange=3)]
        self.assertEqual(agent.decide(data)['roleCommandMap']['14'], command('move', GATE))
        self.assertIn(14, mem.sabotage.gate_states)

    def test_incomplete_wall_clears_prior_recall_and_yield_state(self):
        data = gate_case(imp=(11, 2), worker=(1, 1))
        data['teamOur']['roles'][-1]['health'] = 0
        turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        mem = Memory()
        mem.observe(turn, cfg)
        mem.sabotage.gate_states[14] = dict(day=turn.day, recalling=True, yield_until=75)
        towers, walls = layout(turn, cfg)
        self.assertIsNone(prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls))
        self.assertEqual(mem.sabotage.gate_states, {})
        self.assertNotIn(14, ledger.used)
        act_imps(turn, mem.sabotage, nav, ledger)
        self.assertEqual(ledger.commands['14'], command('destroy', (12, 2)))

    def test_released_imp_completes_four_consecutive_night_destroy_turns(self):
        data, agent = gate_case(imp=(11, 2), worker=(1, 1)), Agent(Config(llm_enabled=False))
        data['robot']['roles'] = []
        for offset in range(4):
            data['roundNo'] = 70 + offset
            data['lastRoundRoleActionResults'] = {'14': True}
            self.assertEqual(agent.decide(data)['roleCommandMap']['14'], command('destroy', (12, 2)))
            mem = next(iter(agent.sessions.values()))
            self.assertEqual(mem.sabotage.progress.get(14, 0), offset)
            self.assertEqual(mem.sabotage.gate_states, {})


class GateGuardTests(unittest.TestCase):
    def test_early_day_at_gate_does_not_recall_or_clear_sabotage(self):
        for point in ((33, 10), (34, 9)):
            with self.subTest(point=point):
                data = logged_morning_case()
                next(u for u in data['teamOur']['roles'] if u['id'] == 20014)['pos'] = dict(zip(('x', 'y'), point))
                turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
                mem = Memory()
                mem.observe(turn, cfg)
                mem.sabotage.targets[20014] = (24, 20), 'stone'
                mem.sabotage.progress[20014] = 2
                towers, walls = layout(turn, cfg)
                self.assertIsNone(prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls))
                self.assertNotIn(20014, ledger.used)
                self.assertEqual(mem.sabotage.targets[20014], ((24, 20), 'stone'))
                self.assertEqual(mem.sabotage.progress[20014], 2)

    def test_logged_morning_imp_leaves_gate_without_two_cell_loop(self):
        data, agent = logged_morning_case(), Agent(Config(llm_enabled=False))
        imp = next(u for u in data['teamOur']['roles'] if u['id'] == 20014)
        positions = [pos(imp['pos'])]
        for _ in range(3):
            commands = agent.decide(data)['roleCommandMap']
            self.assertEqual(commands['20014']['action'], 'move')
            self.assertEqual(next(iter(agent.sessions.values())).sabotage.gate_states, {})
            advance(data, commands)
            positions.append(pos(imp['pos']))
        self.assertEqual(len(set(positions)), 4, positions)

    def test_logged_morning_workers_resume_after_imp_leaves(self):
        data, agent = logged_morning_case(), Agent(Config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['20014'], command('move', (34, 11)))
        self.assertEqual(commands['20010']['action'], 'use')
        advance(data, commands)
        # The supplied feedback confirms the pack was consumed and this wall repaired.
        next(u for u in data['teamOur']['roles'] if u['id'] == 20010)['backpack'] = []
        next(u for u in data['teamOur']['roles'] if u['id'] == 41015)['health'] = 1500
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['20010']['action'], 'move')
        self.assertIn(commands['20012']['action'], ('move', 'collect'))
        self.assertNotEqual(pos(commands['20014']['targetPos'][0]), (33, 10))

    def test_daytime_yield_window_keeps_gate_free_and_retains_mine_target(self):
        data = logged_morning_case()
        data['roundNo'] = 781
        next(u for u in data['teamOur']['roles'] if u['id'] == 20014)['pos'] = dict(x=34, y=9)
        turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        mem = Memory()
        mem.observe(turn, cfg)
        mem.sabotage.targets[20014] = (24, 20), 'stone'
        mem.sabotage.progress[20014] = 2
        mem.sabotage.gate_states[20014] = dict(day=turn.day, recalling=False, yield_until=783, post=(34, 9))
        towers, walls = layout(turn, cfg)
        guard = prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls)
        self.assertIsNotNone(guard)
        act_imps(turn, mem.sabotage, nav, ledger)
        guard.finish()
        self.assertNotIn('20014', ledger.commands)
        self.assertEqual(ledger.notes[20014]['reason'], 'imp_daytime_gate_yield')
        self.assertEqual(mem.sabotage.targets[20014], ((24, 20), 'stone'))
        self.assertNotIn(20014, mem.sabotage.progress)
        data['roundNo'] = 784
        turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        mem.observe(turn, cfg)
        self.assertIsNone(prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls))
        self.assertNotIn(20014, ledger.used)

    def test_last_day_turn_stages_beside_gap_and_first_night_enters(self):
        data, agent = gate_case(69), Agent(Config(llm_enabled=False, return_margin=5))
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

    def test_quiet_night_sabotage_clears_gate_before_worker_moves(self):
        data, agent = gate_case(worker=(4, 9)), Agent(Config(llm_enabled=False))
        data['robot']['roles'] = []
        data['mapInfo']['zones'].append(dict(neutralType='copper', pos=dict(x=1, y=7), remain=10))
        commands = agent.decide(data)['roleCommandMap']
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)
        self.assertNotIn('1', commands)  # No move into the imp's observed cell.
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1'], command('move', GATE))
        self.assertNotEqual(pos(commands['14']['targetPos'][0]), GATE)
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'move')
        self.assertNotEqual(pos(commands['1']['targetPos'][0]), GATE)
        self.assertNotEqual(pos(commands['14']['targetPos'][0]), GATE)
        advance(data, commands)

    def test_quiet_night_far_inside_worker_can_exit_after_imp_leaves(self):
        data, agent = gate_case(worker=(7, 9)), Agent(Config(llm_enabled=False))
        data['robot']['roles'] = []
        data['mapInfo']['zones'].append(dict(neutralType='copper', pos=dict(x=1, y=7), remain=10))
        commands = agent.decide(data)['roleCommandMap']
        self.assertIn(pos(commands['14']['targetPos'][0]), POSTS)
        advance(data, commands)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'move')
        self.assertEqual(next(iter(agent.sessions.values())).sabotage.gate_states, {})

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
        data['robot']['roles'][0]['pos'] = dict(x=1, y=14)
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

    def test_quiet_night_gate_clearance_and_worker_exit_mirror_in_four_corners(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            data = gate_case(worker=(4, 9))
            data['robot']['roles'] = []
            data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=1, y=7), remain=10)]
            expected_posts = {(14-x if flip_x else x, 14-y if flip_y else y) for x, y in POSTS}
            for group in (data['teamOur']['roles'], data['mapInfo']['zones'], data['robot']['roles']):
                for u in group:
                    p = u['pos']
                    if flip_x:
                        p['x'] = (13 if u.get('roleType') == 'station' else 14) - p['x']
                    if flip_y:
                        p['y'] = (15 if u.get('roleType') == 'station' else 14) - p['y']
            turn = Turn(data, Config())
            enemy_mine = (13, 1) if sum(turn.mine_half(p) for p in turn.station.cells) > 0 else (1, 13)
            data['mapInfo']['zones'].append(dict(neutralType='copper', pos=dict(zip(('x', 'y'), enemy_mine)), remain=10))
            agent = Agent(Config(llm_enabled=False))
            commands = agent.decide(data)['roleCommandMap']
            self.assertIn(pos(commands['14']['targetPos'][0]), expected_posts)
            advance(data, commands)
            self.assertEqual(agent.decide(data)['roleCommandMap']['1']['action'], 'move')


if __name__ == '__main__':
    unittest.main()

