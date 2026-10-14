"""Single entrance geometry and safe migration from the logged side gate."""
import copy
from itertools import product
import unittest

from test_agent import ROOT, payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import build, open_wall_gate, wall_keeps_access
from agent.gate_guard import prepare_gate_guard
from agent.intelligence import Memory
from agent.model import Turn, distance, neighbours
from agent.navigation import layout, wall_gaps
from agent.projectiles import wall_gates


class GatlingWallGateTests(unittest.TestCase):
    def test_one_aligned_gate_in_four_corners_and_all_partial_batteries(self):
        for cfg in (Config(), Config.load(ROOT / 'config/default.json'),
                    Config(loadout=['gatling', 'rocket', 'rocket'])):
            for station in ((3, 11), (10, 4), (3, 4), (10, 11)):
                data = payload(roles=[unit(13, 'station', *station)])
                turn = Turn(data, cfg)
                towers, walls = layout(turn, cfg)
                gatling = towers[cfg.loadout.index('gatling')]
                outward_y = -1 if station[1] > 7 else 1
                gate = gatling[0], gatling[1] + outward_y
                self.assertEqual(wall_gates(turn, walls), {gate})
                self.assertEqual(len(walls), 19)
                self.assertEqual(len(set(walls)), 19)
                self.assertTrue(all(turn.base_distance(p) == 2 for p in walls))
                old_gate = (station[0] - 2 if station[0] < 7 else station[0] + 3,
                            station[1] - 1 if station[1] > 7 else station[1])
                self.assertIn(old_gate, walls)
                for built in product((False, True), repeat=3):
                    with self.subTest(loadout=cfg.loadout, station=station, built=built):
                        case = copy.deepcopy(data)
                        case['teamOur']['roles'] += [unit(20+i, kind, *p)
                            for i, (kind, p) in enumerate(zip(cfg.loadout, towers)) if built[i]]
                        # Neither a passing character nor partial walls moves the entrance.
                        case['teamOur']['roles'] += [unit(1, 'worker', *gate),
                                                     unit(100, 'wall', *walls[0])]
                        self.assertEqual(layout(Turn(case, cfg), cfg), (towers, walls))

    def test_completed_wall_has_only_one_passage_and_reachable_operator_posts(self):
        for station in ((3, 11), (10, 4), (3, 4), (10, 11)):
            data = payload(roles=[unit(13, 'station', *station), unit(1, 'worker', 7, 7)])
            turn, cfg, _, _ = setup_case(data)
            towers, walls = layout(turn, cfg)
            gate = next(iter(wall_gates(turn, walls)))
            data['teamOur']['roles'] += [unit(100+i, 'wall', *p) for i, p in enumerate(walls)]
            data['teamOur']['roles'] += [unit(20+i, kind, *p)
                                        for i, (kind, p) in enumerate(zip(cfg.loadout, towers))]
            turn, _, nav, _ = setup_case(data)
            common = set(neighbours(towers[0])) & set(neighbours(towers[1])) - turn.blocked
            independent = set(neighbours(towers[2])) - turn.blocked - common - {gate}
            self.assertTrue(common)
            self.assertTrue(independent)
            inner = {p for x in range(turn.width) for y in range(turn.height)
                     if turn.base_distance(p := (x, y)) == 1 and p not in turn.blocked}
            self.assertTrue(all(nav.search(turn.workers[0], {p}) is not None for p in inner))
            self.assertTrue(any(nav.search(turn.workers[0], {p}) for p in common))
            self.assertTrue(any(nav.search(turn.workers[0], {p}) for p in independent))
            turn.blocked.add(gate)
            self.assertIsNone(nav.approach(turn.workers[0], turn.station.cells))

    def logged_case(self, round_no=304):
        data = payload(round_no, [unit(13, 'station', 9, 22, health=1500),
            unit(1, 'worker', 10, 18, health=500),
            unit(2, 'worker', 8, 21, health=500, backpack=['stone']),
            unit(20, 'gatling', 9, 20), unit(40, 'rocket', 8, 23), unit(41, 'rocket', 9, 23)])
        data['mapInfo'].update(width=41, height=32)
        data['teamOur']['goldNum'] = 0
        cfg = Config()
        walls = set(layout(Turn(data, cfg), cfg)[1])
        # Old layout: the now-aligned entrance (9,19) was a wall, (7,21) was open.
        old_walls = walls - {(7, 21)} | {(9, 19)}
        data['teamOur']['roles'] += [unit(100+i, 'wall', *p) for i, p in enumerate(sorted(old_walls))]
        return data

    def test_logged_gatling_gets_adjacent_gap_and_old_side_is_closed(self):
        data = self.logged_case()
        turn, cfg, _, _ = setup_case(data)
        towers, walls = layout(turn, cfg)
        self.assertEqual(towers, [(8, 23), (9, 23), (9, 20)])
        self.assertEqual(wall_gates(turn, walls), {(9, 19)})
        self.assertIn((7, 21), walls)
        # Opponent log: same 6x6 rectangular perimeter, with one gun-facing gap.
        opponent = {(x, y) for x in range(28, 34) for y in range(7, 13)
                    if x in (28, 33) or y in (7, 12)} - {(30, 12)}
        self.assertEqual(len(walls), len(opponent))
        self.assertEqual(wall_gates(turn, opponent), {(30, 12)})

    def test_removal_is_observed_before_closing_old_entrance(self):
        data = self.logged_case()
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(open_wall_gate(turn, cfg, nav, ledger))
        self.assertEqual(ledger.commands['1'], command('remove', (9, 19)))
        self.assertNotIn('2', ledger.commands)
        self.assertFalse(wall_keeps_access(turn, nav, ledger, (7, 21)))
        self.assertFalse(build(turn, cfg, Memory(), nav, ledger, turn.units[2],
                               list(ledger.wall_cells), lambda _: 'wall'))
        # A failed/unconfirmed removal cannot close the other entrance next turn.
        data['roundNo'] += 1
        turn, cfg, nav, ledger = setup_case(data)
        self.assertFalse(wall_keeps_access(turn, nav, ledger, (7, 21)))
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                   if r['pos'] != dict(x=9, y=19)]
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(wall_keeps_access(turn, nav, ledger, (7, 21)))
        self.assertTrue(build(turn, cfg, Memory(), nav, ledger, turn.units[2],
                              list(ledger.wall_cells), lambda _: 'wall'))
        self.assertEqual(ledger.commands['2'], command('build', (7, 21), name='wall'))

    def test_agent_opens_blocked_entrance_before_ordinary_worker_jobs(self):
        commands = Agent(Config(llm_enabled=False)).decide(self.logged_case())['roleCommandMap']
        self.assertEqual(commands['1'], command('remove', (9, 19)))
        self.assertFalse(any(c['action'] == 'build' and c.get('name') == 'wall'
                             for c in commands.values()))

    def test_migration_respects_timing_occupancy_and_explicit_configuration(self):
        for round_no in (327, 330):  # Day-three recall and night.
            turn, cfg, nav, ledger = setup_case(self.logged_case(round_no))
            self.assertFalse(open_wall_gate(turn, cfg, nav, ledger))
            self.assertFalse(ledger.commands)
        data = self.logged_case()
        turn, cfg, nav, ledger = setup_case(data)
        ledger.used.update(w.id for w in turn.workers)
        self.assertFalse(open_wall_gate(turn, cfg, nav, ledger))
        towers, walls = layout(turn, cfg)
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit',
            weapon_cells=list(map(list, towers)), wall_cells=list(map(list, walls)))
        self.assertFalse(open_wall_gate(turn, cfg, nav, ledger))
        self.assertEqual(layout(turn, cfg), (towers, walls))
        # A distant worker approaches the wall; it never moves into that wall.
        data['teamOur']['roles'][1]['pos'] = dict(x=13, y=18)
        data['teamOur']['roles'][2]['health'] = 0
        turn, cfg, nav, ledger = setup_case(data)
        self.assertTrue(open_wall_gate(turn, cfg, nav, ledger))
        self.assertEqual(ledger.commands['1']['action'], 'move')
        self.assertNotEqual(ledger.commands['1']['targetPos'], [dict(x=9, y=19)])

    def test_removed_gate_does_not_inherit_breach_or_upgrade_repair(self):
        data = self.logged_case()
        cfg = Config()
        mem = Memory(wall_rebuild_levels={(9, 19): 3}, wall_hits={(9, 19): 10})
        mem.observe(Turn(data, cfg), cfg)
        mem.last_round = data['roundNo']
        data['roundNo'] += 1
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                   if r['pos'] != dict(x=9, y=19)]
        turn = Turn(data, cfg)
        mem.observe(turn, cfg)
        self.assertNotIn((9, 19), mem.wall_rebuild_levels)
        self.assertNotIn((9, 19), wall_gaps(turn, layout(turn, cfg)[1], mem.wall_hits))

    def test_second_night_guard_tracks_new_gate_after_wall_completion(self):
        data = self.logged_case(210)
        cfg = Config()
        towers, walls = layout(Turn(data, cfg), cfg)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['roleType'] != 'wall']
        data['teamOur']['roles'] += [unit(100+i, 'wall', *p) for i, p in enumerate(walls)]
        data['teamOur']['roles'].append(unit(14, 'imp', 9, 19, health=500))
        data['robot']['roles'] = [unit(90, 'largeRobot', 9, 17, targetTeam='challenger')]
        turn, cfg, nav, ledger = setup_case(data)
        guard = prepare_gate_guard(turn, cfg, Memory(), nav, ledger, towers, walls)
        self.assertIsNotNone(guard)
        self.assertEqual(guard.gate, (9, 19))
        guard.finish()
        self.assertEqual(guard.state['gate'], (9, 19))
        self.assertNotIn('14', ledger.commands)


if __name__ == '__main__':
    unittest.main()
