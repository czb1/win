"""Observed single-turn targeting and bounded operator state transitions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from time import monotonic
import unittest
from unittest.mock import patch

from test_agent import payload, unit
from test_mixed_battery import mixed_config
from test_logging_system import capture, records
from agent.brain import Agent
from agent.combat import (operator_posts, select_targets, yield_gate_operators,
                          clear_gunner_route, shared_defend)
from agent.commands import Ledger
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, distance, neighbours
from agent.navigation import Navigator, layout, DeadlineExceeded
from agent.projectiles import line_cells, projectile_obstacles, wall_gates


FRAMES = Path(__file__).parent / 'fixtures/gatling_blocked_turns.json'


def frames():
    return json.loads(FRAMES.read_text(encoding='utf-8'))


def railgun_frame(data):
    """Retain the old occlusion scenarios for the weapon that still uses them."""
    data = deepcopy(data)
    next(u for u in data['teamOur']['roles'] if u['id'] == 20020)['roleType'] = 'railgun'
    return data


def setup(data, **options):
    cfg = mixed_config(llm_enabled=False, **options)
    turn = Turn(data, cfg)
    nav = Navigator(turn, monotonic() + 3)
    sites, walls = layout(turn, cfg)
    return turn, cfg, nav, Ledger(turn, cfg, sites, walls), sites, walls


def mirror(data, flip_x, flip_y):
    data = deepcopy(data)
    width, height = data['mapInfo']['width'], data['mapInfo']['height']
    for group in (data['teamOur']['roles'], data['teamEnemy']['roles'],
                  data['robot']['roles'], data['mapInfo']['zones']):
        for role in group:
            point = role['pos']
            if flip_x:
                point['x'] = (width - 2 if role.get('roleType') == 'station' else width - 1) - point['x']
            if flip_y:
                point['y'] = (height if role.get('roleType') == 'station' else height - 1) - point['y']
    return data


class ProjectileModelTests(unittest.TestCase):
    def test_railgun_blocker_counts_and_samples_identify_operator(self):
        for data, count in zip(frames(), (12, 12, 8)):
            with self.subTest(round=data['roundNo']):
                turn, _, nav, _, _, _ = setup(railgun_frame(data))
                tower = next(w for w in turn.weapons if w.id == 20020)
                report = {}
                self.assertEqual(select_targets(turn, tower, {}, nav.deadline,
                                                controller=turn.units[20012], diagnostics=report), [])
                self.assertEqual(report['in_range'], count)
                self.assertEqual(report['clear_paths'], 0)
                self.assertEqual(report['blocker_counts'], {'building': count - 1, 'character': 1})
                sample = next(s for s in report['blocker_samples'] if s['category'] == 'character')
                self.assertEqual(sample['robot_id'], 31027)
                self.assertEqual(sample['blockers'][0]['unit_id'], 20012)

    def test_verified_character_option_changes_railgun_projectiles_only(self):
        turn, _, nav, _, _, _ = setup(railgun_frame(frames()[0]), projectile_characters_block=False)
        tower = next(w for w in turn.weapons if w.id == 20020)
        before = turn.blocked.copy()
        self.assertEqual(select_targets(turn, tower, {}, nav.deadline), [(31, 12)])
        self.assertEqual(turn.blocked, before)
        self.assertIn((31, 11), turn.blocked)
        obstacles = projectile_obstacles(turn, tower)
        self.assertNotIn((31, 11), obstacles)
        self.assertIn((31, 10), obstacles)
        self.assertIn((28, 9), obstacles)

    def test_projected_post_does_not_mutate_observed_occupancy(self):
        turn, _, nav, _, _, _ = setup(railgun_frame(frames()[0]))
        tower = next(w for w in turn.weapons if w.id == 20020)
        before = turn.blocked.copy()
        targets = select_targets(turn, tower, {}, nav.deadline, controller=turn.units[20012],
                                 actor_positions={20012: (33, 10)})
        self.assertEqual(targets, [(31, 12)])
        self.assertEqual(turn.units[20012].pos, (31, 11))
        self.assertEqual(turn.blocked, before)

    def test_controller_origin_is_explicit_and_requires_controller(self):
        turn, _, nav, _, _, _ = setup(frames()[0], projectile_origin='controller')
        tower = next(w for w in turn.weapons if w.id == 20020)
        report = {}
        self.assertEqual(select_targets(turn, tower, {}, nav.deadline, diagnostics=report), [])
        self.assertEqual(report['reason'], 'no_operator')
        self.assertTrue(select_targets(turn, tower, {}, nav.deadline,
                                       controller=turn.units[20012], diagnostics=report))
        self.assertEqual(report['origin'], (31, 11))

    def test_railgun_character_option_never_removes_protected_robot_collision(self):
        data = railgun_frame(frames()[0])
        data['robot']['roles'].append(unit(39999, 'smallRobot', 32, 11,
                                         health=40, targetTeam='challenger'))
        turn, _, nav, _, _, _ = setup(data, projectile_characters_block=False)
        damage, report = {}, {}
        tower = next(w for w in turn.weapons if w.id == 20020)
        self.assertEqual(select_targets(turn, tower, damage, nav.deadline, diagnostics=report), [])
        self.assertEqual(report['reason'], 'protected_robot_in_path')
        self.assertNotIn(39999, damage)

    def test_invalid_rule_options_are_rejected(self):
        for options in ({'projectile_origin': 'guess'}, {'projectile_characters_block': 0},
                        {'projectile_characters_block': 'false'}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'config.json'
                path.write_text(json.dumps(options), encoding='utf-8')
                with self.assertRaises(ValueError):
                    Config.load(path)


class OperatorPostTests(unittest.TestCase):
    def test_blind_railgun_post_is_replaced_with_reachable_firing_post(self):
        turn, _, nav, ledger, _, walls = setup(railgun_frame(frames()[0]))
        hero, tower = turn.units[20012], turn.units[20020]
        posts = operator_posts(turn, nav, [(hero, tower)], walls + [(32, 9)], {hero.id: hero.pos})
        self.assertEqual(posts[hero.id], ((33, 10), 2))
        self.assertNotIn(posts[hero.id][0], walls)
        self.assertNotEqual(posts[hero.id][0], (32, 9))

    def test_useful_existing_post_is_stable(self):
        data = frames()[0]
        next(u for u in data['teamOur']['roles'] if u['id'] == 20012)['pos'] = {'x': 33, 'y': 10}
        turn, _, nav, _, _, walls = setup(data)
        hero, tower = turn.units[20012], turn.units[20020]
        posts = operator_posts(turn, nav, [(hero, tower)], walls + [(32, 9)], {hero.id: hero.pos})
        self.assertEqual(posts[hero.id], (hero.pos, 0))

    def test_deadline_restores_occupancy(self):
        turn, _, nav, _, _, walls = setup(frames()[0])
        original = turn.blocked
        with patch('agent.combat.select_targets', side_effect=DeadlineExceeded):
            with self.assertRaises(DeadlineExceeded):
                operator_posts(turn, nav, [(turn.units[20012], turn.units[20020])], walls)
        self.assertIs(turn.blocked, original)

    def test_real_match_fires_in_all_three_requests_without_leaving_post(self):
        agent = Agent(mixed_config(llm_enabled=False))
        expected = [[(27, 11)] * 2, [(27, 11)] * 2, [(27, 11), (31, 12)]]
        for data, targets in zip(frames(), expected):
            hero = next(u for u in data['teamOur']['roles'] if u['id'] == 20012)
            result = agent.decide(data)['roleCommandMap']
            memory = next(iter(agent.sessions.values()))
            self.assertEqual(memory.gatling_operator_id, 20012)
            self.assertEqual(memory.gatling_targets[20020], 31031)
            self.assertEqual(hero['pos'], {'x': 31, 'y': 11})
            self.assertEqual(result['20020']['controllerId'], '20012')
            self.assertEqual(result['20020']['targetPos'], [{'x': x, 'y': y} for x, y in targets])
            self.assertNotIn('20012', result)

    def test_match_keeps_firing_from_existing_post_in_four_corners(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = mirror(frames()[0], flip_x, flip_y)
                agent = Agent(mixed_config(llm_enabled=False))
                result = agent.decide(data)['roleCommandMap']
                self.assertNotIn('20012', result)
                self.assertEqual(result['20020']['controllerId'], '20012')
                memory = next(iter(agent.sessions.values()))
                self.assertEqual(memory.gatling_operator_id, 20012)
                post = next(u['pos'] for u in data['teamOur']['roles'] if u['id'] == 20012)
                self.assertEqual(memory.return_posts[20012], (post['x'], post['y']))

    def test_nonclustered_explicit_layout_also_fires_from_current_post(self):
        agent = Agent(mixed_config(llm_enabled=False, layout_mode='explicit',
                             weapon_cells=[[32, 8], [35, 8], [32, 10]], wall_cells=[]))
        result = agent.decide(frames()[0])['roleCommandMap']
        self.assertNotIn('20012', result)
        self.assertEqual(result['20020']['controllerId'], '20012')
        self.assertEqual(next(iter(agent.sessions.values())).gatling_operator_id, 20012)

    def test_diagnostics_explain_gatling_firing_without_obstacle_checks(self):
        with capture() as stream:
            Agent(mixed_config(llm_enabled=False)).decide(frames()[0])
        decision = next(r['data'] for r in records(stream)
                        if r['event'] == 'unit_decision' and r['unit_id'] == 20020)
        self.assertEqual(decision['reason'], 'fire_selected_targets')
        self.assertEqual(decision['targeting']['in_range'], 12)
        self.assertEqual(decision['targeting']['clear_paths'], 12)
        self.assertEqual(decision['targeting']['blocker_counts'], {})
        self.assertFalse(decision['targeting']['obstacles_block'])
        self.assertFalse(decision['targeting']['characters_block'])
        operator = next(r['data'] for r in records(stream)
                        if r['event'] == 'unit_decision' and r['unit_id'] == 20012)
        self.assertEqual(operator['night_role'], 'gatling')

    def test_missing_operator_is_reported(self):
        data = frames()[0]
        data['teamOur']['roles'] = [u for u in data['teamOur']['roles'] if u['roleType'] not in ('worker', 'pioneer')]
        with capture() as stream:
            Agent(mixed_config(llm_enabled=False)).decide(data)
        decision = next(r['data'] for r in records(stream)
                        if r['event'] == 'unit_decision' and r['unit_id'] == 20020)
        self.assertEqual(decision['reason'], 'no_operator')


class GateAndLayoutTests(unittest.TestCase):
    def gate_case(self):
        data = frames()[0]
        data['robot']['roles'] = []
        data['teamEnemy']['roles'] = []
        _, _, _, _, _, walls = setup(data)
        data['teamOur']['roles'] = [u for u in data['teamOur']['roles'] if u['roleType'] != 'wall']
        data['teamOur']['roles'] += [unit(42000 + i, 'wall', *p, health=1000) for i, p in enumerate(walls)]
        next(u for u in data['teamOur']['roles'] if u['id'] == 20012)['pos'] = {'x': 33, 'y': 10}
        next(u for u in data['teamOur']['roles'] if u['id'] == 20010)['pos'] = {'x': 32, 'y': 11}
        data['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 38, 'y': 15}})
        return data

    def test_gate_holder_yields_outward_and_reserves_two_passage_turns(self):
        turn, _, nav, ledger, _, walls = setup(self.gate_case())
        mem = Memory(mine_targets={20010: (38, 15)})
        hero, tower = turn.units[20012], turn.units[20020]
        ledger.operator_posts = {20012: hero.pos}
        yield_gate_operators(turn, nav, ledger, mem, walls, [(hero, tower)])
        move = ledger.commands['20012']['targetPos'][0]
        self.assertGreater(move['x'], 33)
        self.assertEqual(distance(hero.pos, (move['x'], move['y'])), 1)
        self.assertEqual(mem.operator_yields[hero.id], {'gate': (33, 10), 'until': 100})
        self.assertNotIn('20010', ledger.commands)
        self.assertEqual(ledger.weapon_diagnostics[tower.id]['reason'], 'operator_yielding_gate')

    def test_gate_holder_does_not_yield_to_mining_traffic_before_wave_clears(self):
        data = self.gate_case()
        data['robot']['roles'] = [unit(45000, 'largeRobot', 15, 20, health=360,
                                      targetTeam='defender', attackRange=3)]
        turn, _, nav, ledger, _, walls = setup(data)
        mem = Memory(mine_targets={20010: (38, 15)})
        hero, tower = turn.units[20012], turn.units[20020]
        ledger.operator_posts = {hero.id: hero.pos}
        yield_gate_operators(turn, nav, ledger, mem, walls, [(hero, tower)])
        self.assertNotIn('20012', ledger.commands)
        self.assertEqual(mem.operator_yields, {})

    def test_temporary_gate_exclusion_blocks_operator_route_through_it(self):
        data = self.gate_case()
        next(u for u in data['teamOur']['roles'] if u['id'] == 20012)['pos'] = {'x': 34, 'y': 9}
        turn, _, nav, _, _, walls = setup(data)
        posts = operator_posts(turn, nav, [(turn.units[20012], turn.units[20020])],
                               walls + [(32, 9)], unavailable={20012: {(33, 10)}})
        self.assertEqual(posts, {})

    def test_gate_passage_keeps_operator_identity_when_wave_clears(self):
        agent = Agent(mixed_config(llm_enabled=False))
        agent.decide(frames()[0])
        memory = next(iter(agent.sessions.values()))
        memory.mine_targets[20010] = (38, 15)
        memory.mine_kinds[38, 15] = 'copper'
        memory.return_posts[20012] = (33, 10)
        data = self.gate_case()
        data['robot']['roles'] = [unit(45000, 'smallRobot', 27, 6, health=30,
                                     targetTeam='defender', attackRange=3)]
        for round_no in (99, 100, 101):
            data['roundNo'] = round_no
            if round_no > 99:
                data['robot']['roles'] = []
            with capture() as stream:
                result = agent.decide(data)['roleCommandMap']
            self.assertEqual(memory.gatling_operator_id, 20012)
            if round_no == 99:
                self.assertNotIn('20012', result)
                self.assertEqual(result['20020']['controllerId'], '20012')
                self.assertNotIn('20010', result)
            else:
                self.assertEqual(result['20012']['action'], 'move')
                reason = next(r['data']['reason'] for r in records(stream)
                              if r['event'] == 'unit_decision' and r['unit_id'] == 20020)
                self.assertEqual(reason, 'no_operator')
                if round_no == 100:
                    self.assertGreater(result['20012']['targetPos'][0]['x'], 33)
                    self.assertNotIn('20010', result)
                else:
                    self.assertEqual(result['20010']['action'], 'move')
                    self.assertEqual(result['20010']['targetPos'], [{'x': 33, 'y': 10}])
            for uid, action in result.items():
                if action['action'] == 'move':
                    actor = next(u for u in data['teamOur']['roles'] if str(u['id']) == uid)
                    actor['pos'] = action['targetPos'][0]

    def test_gunner_corridor_is_protected_before_gatling_return(self):
        data = self.gate_case()
        next(u for u in data['teamOur']['roles'] if u['id'] == 20012)['pos'] = {'x': 32, 'y': 11}
        next(u for u in data['teamOur']['roles'] if u['id'] == 20010)['pos'] = {'x': 38, 'y': 23}
        next(u for u in data['teamOur']['roles'] if u['id'] == 20011)['pos'] = {'x': 34, 'y': 9}
        turn, _, nav, ledger, sites, _ = setup(data)
        pioneer, worker = turn.units[20011], turn.units[20012]
        pairs = [(pioneer, turn.units[20040]), (worker, turn.units[20020])]
        ledger.operator_posts = {pioneer.id: (32, 9), worker.id: (33, 10)}
        mem = Memory(gunner_id=pioneer.id, gunner_post=(32, 9))
        corridor = clear_gunner_route(turn, nav, ledger, mem, pairs)
        shared_defend(turn, nav, ledger, mem, pairs, sites, corridor=corridor)
        self.assertEqual(ledger.commands['20011']['targetPos'], [{'x': 33, 'y': 10}])
        self.assertNotIn('20012', ledger.commands)

    def test_explicit_layout_is_authoritative(self):
        data = frames()[0]
        sites = [[2, 2], [4, 2], [3, 4]]
        turn, cfg, _, _, actual, walls = setup(data, layout_mode='explicit', weapon_cells=sites, wall_cells=[])
        self.assertEqual(actual, [tuple(p) for p in sites])
        self.assertEqual(walls, [])
        self.assertFalse(hasattr(turn, 'weapon_layout'))

    def test_new_mixed_layout_has_independent_post_and_clear_gate_ray(self):
        data = payload(1, [unit(13, 'station', 5, 11, health=1500)])
        turn, cfg, _, _, sites, walls = setup(data)
        gates = wall_gates(turn, walls)
        self.assertEqual(len(gates), 1)
        gatling = sites[cfg.loadout.index('gatling')]
        gate = next(iter(gates))
        self.assertEqual(distance(gatling, gate), 1)
        self.assertEqual(gatling[0], gate[0])
        obstacles = set(walls) | turn.station.cells | (set(sites) - {gatling})
        ray = set(line_cells(gatling, gate))
        self.assertFalse(ray & obstacles)
        common = set(neighbours(sites[0])) & set(neighbours(sites[1]))
        self.assertTrue(set(neighbours(gatling)) - obstacles - ray - common)
        self.assertEqual(turn.weapon_layout['mode'], 'gate_guard')
