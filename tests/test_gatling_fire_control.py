"""Observed single-turn targeting and bounded operator state transitions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from time import monotonic
import unittest
from unittest.mock import patch

from test_agent import payload, unit
from test_logging_system import capture, records
from agent.brain import Agent
from agent.combat import (operator_posts, select_targets, yield_gate_operators,
                          clear_gunner_route, shared_defend)
from agent.commands import Ledger
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, distance
from agent.navigation import Navigator, layout, DeadlineExceeded
from agent.projectiles import projectile_obstacles, wall_gates


FRAMES = Path(__file__).parent / 'fixtures/gatling_blocked_turns.json'


def frames():
    return json.loads(FRAMES.read_text(encoding='utf-8'))


def setup(data, **options):
    cfg = Config(llm_enabled=False, **options)
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
    def test_observed_blocker_counts_and_samples_identify_operator(self):
        for data, count in zip(frames(), (12, 12, 8)):
            with self.subTest(round=data['roundNo']):
                turn, _, nav, _, _, _ = setup(data)
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

    def test_verified_character_option_changes_projectiles_only(self):
        turn, _, nav, _, _, _ = setup(frames()[0], projectile_characters_block=False)
        tower = next(w for w in turn.weapons if w.id == 20020)
        before = turn.blocked.copy()
        self.assertEqual(select_targets(turn, tower, {}, nav.deadline), [(31, 12)] * 2)
        self.assertEqual(turn.blocked, before)
        self.assertIn((31, 11), turn.blocked)
        obstacles = projectile_obstacles(turn, tower)
        self.assertNotIn((31, 11), obstacles)
        self.assertIn((31, 10), obstacles)
        self.assertIn((28, 9), obstacles)

    def test_projected_post_does_not_mutate_observed_occupancy(self):
        turn, _, nav, _, _, _ = setup(frames()[0])
        tower = next(w for w in turn.weapons if w.id == 20020)
        before = turn.blocked.copy()
        targets = select_targets(turn, tower, {}, nav.deadline, controller=turn.units[20012],
                                 actor_positions={20012: (33, 10)})
        self.assertEqual(targets, [(31, 12)] * 2)
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

    def test_character_option_never_removes_protected_robot_collision(self):
        data = frames()[0]
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
    def test_blind_fixed_post_is_replaced_with_reachable_firing_post(self):
        turn, _, nav, ledger, _, walls = setup(frames()[0])
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

    def test_real_match_repositions_in_three_bounded_requests_then_fires(self):
        agent = Agent(Config(llm_enabled=False))
        for index, data in enumerate(frames()):
            hero = next(u for u in data['teamOur']['roles'] if u['id'] == 20012)
            if index:
                hero['pos'] = move
            result = agent.decide(data)['roleCommandMap']
            memory = next(iter(agent.sessions.values()))
            self.assertEqual(memory.gatling_operator_id, 20012)
            if index < 2:
                self.assertEqual(result['20012']['action'], 'move')
                self.assertNotIn('20020', result)
                move = result['20012']['targetPos'][0]
            else:
                self.assertEqual(hero['pos'], {'x': 33, 'y': 10})
                self.assertEqual(result['20020']['controllerId'], '20012')
                self.assertEqual(result['20020']['targetPos'], [{'x': 31, 'y': 12}] * 2)
                self.assertNotIn('20012', result)

    def test_match_recall_and_existing_layout_work_in_four_corners(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = mirror(frames()[0], flip_x, flip_y)
                agent = Agent(Config(llm_enabled=False))
                result = agent.decide(data)['roleCommandMap']
                self.assertEqual(result['20012']['action'], 'move')
                memory = next(iter(agent.sessions.values()))
                self.assertEqual(memory.gatling_operator_id, 20012)
                gate = (7 if flip_x else 33, 21 if flip_y else 10)
                self.assertEqual(memory.return_posts[20012], gate)

    def test_nonclustered_explicit_layout_also_repositions(self):
        agent = Agent(Config(llm_enabled=False, layout_mode='explicit',
                             weapon_cells=[[32, 8], [35, 8], [32, 10]], wall_cells=[]))
        result = agent.decide(frames()[0])['roleCommandMap']
        self.assertEqual(result['20012']['action'], 'move')
        self.assertNotIn('20020', result)
        self.assertEqual(next(iter(agent.sessions.values())).gatling_operator_id, 20012)

    def test_diagnostics_explain_repositioning_with_original_blockers(self):
        with capture() as stream:
            Agent(Config(llm_enabled=False)).decide(frames()[0])
        decision = next(r['data'] for r in records(stream)
                        if r['event'] == 'unit_decision' and r['unit_id'] == 20020)
        self.assertEqual(decision['reason'], 'operator_moving')
        self.assertEqual(decision['targeting']['in_range'], 12)
        self.assertEqual(decision['targeting']['blocker_counts']['character'], 1)
        operator = next(r['data'] for r in records(stream)
                        if r['event'] == 'unit_decision' and r['unit_id'] == 20012)
        self.assertEqual(operator['night_role'], 'gatling')

    def test_missing_operator_is_reported(self):
        data = frames()[0]
        data['teamOur']['roles'] = [u for u in data['teamOur']['roles'] if u['roleType'] not in ('worker', 'pioneer')]
        with capture() as stream:
            Agent(Config(llm_enabled=False)).decide(data)
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

    def test_temporary_gate_exclusion_blocks_operator_route_through_it(self):
        data = self.gate_case()
        next(u for u in data['teamOur']['roles'] if u['id'] == 20012)['pos'] = {'x': 34, 'y': 9}
        turn, _, nav, _, _, walls = setup(data)
        posts = operator_posts(turn, nav, [(turn.units[20012], turn.units[20020])],
                               walls + [(32, 9)], unavailable={20012: {(33, 10)}})
        self.assertEqual(posts, {})

    def test_gate_passage_keeps_operator_identity_when_wave_clears(self):
        agent = Agent(Config(llm_enabled=False))
        agent.decide(frames()[0])
        memory = next(iter(agent.sessions.values()))
        memory.mine_targets[20010] = (38, 15)
        memory.mine_kinds[38, 15] = 'copper'
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
                self.assertEqual(result['20012']['targetPos'], [{'x': 34, 'y': 9}])
                self.assertNotIn('20010', result)
            else:
                self.assertNotIn('20012', result)
                self.assertEqual(result['20010']['action'], 'move')
                reason = next(r['data']['reason'] for r in records(stream)
                              if r['event'] == 'unit_decision' and r['unit_id'] == 20020)
                self.assertEqual(reason, 'operator_waiting_for_passage')
                if round_no == 100:
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

    def test_new_mixed_layout_has_independent_gate_facing_post(self):
        data = payload(1, [unit(13, 'station', 5, 11, health=1500)])
        turn, cfg, _, _, sites, walls = setup(data)
        gates = wall_gates(turn, walls)
        self.assertEqual(len(gates), 1)
        gatling = sites[cfg.loadout.index('gatling')]
        self.assertEqual(distance(gatling, next(iter(gates))), 1)
        self.assertEqual(turn.weapon_layout['mode'], 'gate_guard')
