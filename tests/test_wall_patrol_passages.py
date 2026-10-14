"""Finite regression of the day-six base pocket and delayed night spawning."""
import unittest

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, pos
from agent.navigation import layout
from agent.night_economy import (WorkNavigator, _deliver, awaiting_night_wave,
                                release_gatling, work)
from agent.wall_watch import geometry, patrol_area, repair_watch, watch_route


class WallPatrolPassageTests(unittest.TestCase):
    def case(self, mirror=False, tick=71, robots=True, opening=False):
        walls = {(x, y) for x in range(28, 34) for y in range(7, 13)
                 if x in (28, 33) or y in (7, 12)} - {(31, 12), (33, 10)}
        data = payload(650 + tick, [
            unit(13, 'station', 30, 10, health=2800, level=2),
            unit(1, 'worker', 32, 10 if opening else 11, health=255,
                 backpack=['WallFixer'] * 5),
            unit(2, 'worker', 22, 12, health=425),
            unit(11, 'pioneer', 32, 9, health=500),
            unit(20, 'gatling', 31, 11, level=3, health=1990, attackRange=7),
            unit(40, 'rocket', 32, 8, level=3, health=2000, attackRange=2147483647),
            unit(41, 'rocket', 31, 8, level=3, health=2000, attackRange=2147483647),
        ] + [unit(100+i, 'wall', x, y, level=2,
                  health=100 if (x, y) == (28, 7) else 1500)
             for i, (x, y) in enumerate(sorted(walls))])
        data['mapInfo'].update(width=41, height=32)
        data['teamOur'].update(type='defender', goldNum=0)
        if robots:
            data['robot']['roles'] = [unit(90, 'smallRobot', 10, 20, health=40,
                                         attackRange=3, targetTeam='defender')]
        if mirror:
            for r in data['teamOur']['roles'] + data['robot']['roles']:
                r['pos']['x'] = 40 - r['pos']['x'] - (1 if r['roleType'] == 'station' else 0)
        return data

    def context(self, data):
        turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        pioneer = turn.units[11]
        mem = Memory(wall_watch_id=1, gatling_operator_id=2,
                     gunner_id=11, gunner_post=pioneer.pos)
        return turn, cfg, nav, ledger, mem, layout(turn, cfg)[1]

    def test_boundary_gap_connects_real_pocket_in_both_directions(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                turn, _, nav, ledger, mem, sites = self.context(self.case(mirror))
                gap = (9 if mirror else 31, 12)
                self.assertNotIn(gap, geometry(turn, sites)[0])
                self.assertIn(gap, patrol_area(turn, sites))
                self.assertTrue(repair_watch(turn, mem, nav, ledger, turn.units[1], sites))
                self.assertEqual(ledger.commands['1']['action'], 'move')
                self.assertEqual(pos(ledger.commands['1']['targetPos'][0]), gap)

    def test_boundary_transit_continues_to_inside_repair_and_spends_pack(self):
        data = self.case()
        mem = None
        moved = []
        for _ in range(8):
            turn, _, nav, ledger, initial, sites = self.context(data)
            mem = mem or initial
            self.assertTrue(repair_watch(turn, mem, nav, ledger, turn.units[1], sites))
            cmd = ledger.commands['1']
            if cmd['action'] == 'use':
                self.assertEqual(cmd['name'], 'WallFixer')
                self.assertEqual(pos(cmd['targetPos'][0]), (28, 7))
                self.assertIn(turn.units[1].pos, geometry(turn, sites)[0])
                break
            moved.append(pos(cmd['targetPos'][0]))
            data['teamOur']['roles'][1]['pos'] = cmd['targetPos'][0]
            data['roundNo'] += 1
        else:
            self.fail('The finite passage never reaches its repair target')
        self.assertIn((31, 12), moved)
        self.assertIn((30, 11), moved)

    def test_actual_wall_neutral_and_reservation_still_block_gap(self):
        for obstacle in ('wall', 'neutral', 'reservation', 'worker'):
            with self.subTest(obstacle=obstacle):
                data = self.case()
                if obstacle == 'wall':
                    data['teamOur']['roles'].append(unit(999, 'wall', 31, 12))
                elif obstacle == 'neutral':
                    data['mapInfo']['zones'].append(dict(neutralType='stone', pos=dict(x=31, y=12)))
                elif obstacle == 'worker':
                    data['teamOur']['roles'][2]['pos'] = dict(x=30, y=11)
                turn, _, nav, ledger, mem, sites = self.context(data)
                if obstacle == 'reservation':
                    ledger.reserved.add((31, 12))
                wall = next(w for w in turn.ours if w.pos == (28, 7))
                self.assertIsNone(watch_route(turn, nav, ledger, mem, turn.units[1], sites, wall))

    def test_stocked_outside_watcher_enters_reachable_pocket_when_target_is_blocked(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                data = self.case(mirror=mirror)
                data['teamOur']['roles'][1]['pos'] = dict(x=6 if mirror else 34, y=13)
                data['teamOur']['roles'][2]['pos'] = dict(x=10 if mirror else 30, y=11)
                turn, _, nav, ledger, mem, sites = self.context(data)
                wall = next(w for w in turn.ours if w.kind == 'wall' and w.health == 100)
                self.assertIsNone(watch_route(turn, nav, ledger, mem, turn.units[1], sites, wall))
                for _ in range(6):
                    if turn.units[1].pos in geometry(turn, sites)[0]:
                        break
                    self.assertTrue(repair_watch(turn, mem, nav, ledger, turn.units[1], sites))
                    self.assertEqual(ledger.commands['1']['action'], 'move')
                    step = pos(ledger.commands['1']['targetPos'][0])
                    self.assertNotIn(step, turn.blocked)
                    data['teamOur']['roles'][1]['pos'] = ledger.commands['1']['targetPos'][0]
                    data['roundNo'] += 1
                    turn, _, nav, ledger, _, sites = self.context(data)
                self.assertIn(turn.units[1].pos, geometry(turn, sites)[0])

    def test_boundary_under_enemy_attack_is_not_a_transit_cell(self):
        data = self.case()
        data['robot']['roles'][0]['pos'] = dict(x=31, y=15)
        turn, _, nav, ledger, mem, sites = self.context(data)
        self.assertNotIn((31, 12), patrol_area(turn, sites))
        wall = next(w for w in turn.ours if w.pos == (28, 7))
        self.assertIsNone(watch_route(turn, nav, ledger, mem, turn.units[1], sites, wall))

    def test_cleared_night_wall_delivery_uses_same_boundary_passage(self):
        data = self.case(tick=110, robots=False)
        data['teamOur']['roles'][1]['backpack'].append('WallUpgradeVoucher2')
        turn, _, nav, ledger, mem, _ = self.context(data)
        state = dict(end_round=815)
        self.assertTrue(_deliver(turn, mem, WorkNavigator(nav, ledger), ledger,
                                 turn.units[1], state, watcher=True))
        self.assertEqual(pos(ledger.commands['1']['targetPos'][0]), (31, 12))
        self.assertEqual(ledger.notes[1]['reason'], 'night_wall_delivery')

    def test_empty_opening_keeps_both_fixed_defenders_on_defense(self):
        for mirror in (False, True):
            data = self.case(mirror, tick=70, robots=False, opening=True)
            agent = Agent(Config(llm_enabled=False))
            commands = agent.decide(data)['roleCommandMap']
            mem = next(iter(agent.sessions.values()))
            self.assertEqual((mem.wall_watch_id, mem.gatling_operator_id), (1, 2))
            self.assertNotIn(1, mem.night_work)
            self.assertNotIn(2, mem.night_work)
            self.assertEqual(commands['1']['action'], 'move')
            self.assertEqual(commands['2']['action'], 'move')
            # A second empty request is still pre-spawn, not a cleared night.
            data['roundNo'] += 1
            for r in data['teamOur']['roles']:
                if str(r['id']) in commands and commands[str(r['id'])]['action'] == 'move':
                    r['pos'] = commands[str(r['id'])]['targetPos'][0]
            agent.decide(data)
            self.assertNotIn(1, mem.night_work)
            self.assertNotIn(2, mem.night_work)

    def test_opening_guard_handles_round_origins_and_waits_for_actual_wave(self):
        for origin in (0, 1):
            data = self.case(tick=70, robots=False)
            data['roundNo'] += origin
            cfg = Config(round_origin=origin)
            mem = Memory()
            self.assertTrue(awaiting_night_wave(Turn(data, cfg), mem))
            data['roundNo'] += 1
            self.assertTrue(awaiting_night_wave(Turn(data, cfg), mem))
            data['robot']['roles'] = [unit(90, 'smallRobot', 10, 20, targetTeam='defender')]
            self.assertFalse(awaiting_night_wave(Turn(data, cfg), mem))
            data['robot']['roles'] = []
            data['roundNo'] += 1
            self.assertFalse(awaiting_night_wave(Turn(data, cfg), mem))
            data['roundNo'] = 780 + 70 + origin
            self.assertTrue(awaiting_night_wave(Turn(data, cfg), mem))

    def test_direct_release_and_work_cannot_bypass_opening(self):
        data = self.case(tick=70, robots=False)
        turn, cfg, nav, ledger, mem, _ = self.context(data)
        pairs = [(turn.units[11], turn.units[40]), (turn.units[2], turn.units[20])]
        self.assertFalse(release_gatling(turn, mem, ledger, pairs))
        for uid in (1, 2):
            mem.night_work[uid] = dict(day=turn.day, end_round=815, phase='mine')
            self.assertFalse(work(turn, cfg, mem, nav, ledger, turn.units[uid], start=True))
            self.assertNotIn(uid, mem.night_work)
        self.assertFalse(ledger.commands)
