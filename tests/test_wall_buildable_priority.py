"""Finite snapshots for buildable front walls and connecting side segments."""
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import repair_walls
from agent.economy_plan import front_sites
from agent.intelligence import Memory
from agent.model import Turn, pos
from agent.navigation import layout


def broken_front(mirrored=False, stones=10, front_connected=False, tick=10):
    data = payload(390 + tick, [
        unit(13, 'station', 9, 22, health=1500),
        unit(1, 'worker', 9, 25, health=500, backpack=['stone'] * stones),
        unit(2, 'worker', 10, 23, health=500),
        unit(11, 'pioneer', 8, 22, health=500),
    ])
    data['mapInfo'].update(width=41, height=32)
    data['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y), remain=10)
        for k, x, y in [('vendor', 14, 25), ('copper', 15, 25),
                         ('stone', 16, 25), ('weaponShop', 14, 23)]]
    data['vendorShopList'] = [dict(name='stone', price=1), dict(name='copper', price=5)]
    cfg = Config(llm_enabled=False)
    towers, walls = layout(Turn(data, cfg), cfg)
    data['teamOur']['roles'] += [unit(20+i, kind, *p, health=2000, level=3)
        for i, (p, kind) in enumerate(zip(towers, cfg.loadout))]
    data['teamOur']['roles'] += [unit(100+i, 'wall', *p, health=1500, level=2)
        for i, p in enumerate(walls) if p[0] <= 8]
    if front_connected:
        data['teamOur']['roles'].append(unit(200, 'wall', 12, 23, health=1500, level=2))
    if mirrored:
        data['teamOur']['type'] = 'defender'
        for role in data['teamOur']['roles']:
            x, y = pos(role['pos'])
            station = role['roleType'] == 'station'
            role['pos'] = dict(x=40-x-int(station), y=31-y+int(station))
        for zone in data['mapInfo']['zones']:
            x, y = pos(zone['pos'])
            zone['pos'] = dict(x=40-x, y=31-y)
    return data


def repair_memory(turn, walls):
    live = {u.pos for u in turn.ours if u.kind == 'wall'}
    return Memory(day=turn.day, gatling_operator_id=1, wall_watch_id=2,
                  wall_rebuild_levels={p: 2 for p in walls if p not in live})


class WallBuildablePriorityTests(unittest.TestCase):
    def setup(self, data):
        turn, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        return turn, cfg, repair_memory(turn, ledger.wall_cells), nav, ledger

    def repair(self, turn, cfg, mem, nav, ledger):
        return repair_walls(turn, cfg, mem, nav, ledger, [turn.units[1]], sorted(ledger.wall_cells))

    def test_disconnected_front_builds_a_connecting_side_in_both_teams(self):
        for mirrored, expected in ((False, (9, 24)), (True, (31, 7))):
            with self.subTest(mirrored=mirrored):
                t, cfg, mem, nav, ledger = self.setup(broken_front(mirrored))
                self.assertTrue(self.repair(t, cfg, mem, nav, ledger))
                self.assertEqual(ledger.commands['1'], command('build', expected, name='wall'))

    def test_agent_repairs_connection_before_ordinary_copper_income(self):
        for mirrored, expected in ((False, (9, 24)), (True, (31, 7))):
            with self.subTest(mirrored=mirrored):
                data = broken_front(mirrored)
                agent = Agent(Config(llm_enabled=False))
                turn = Turn(data, agent.cfg)
                _, walls = layout(turn, agent.cfg)
                agent.sessions[(*turn.key, turn.station.pos)] = repair_memory(turn, walls)
                commands = agent.decide(data)['roleCommandMap']
                self.assertEqual(commands['1'], command('build', expected, name='wall'))

    def test_buildable_front_still_precedes_nearer_side(self):
        for mirrored in (False, True):
            with self.subTest(mirrored=mirrored):
                t, cfg, mem, nav, ledger = self.setup(broken_front(mirrored, front_connected=True))
                self.assertTrue(self.repair(t, cfg, mem, nav, ledger))
                self.assertIn(mem.build_targets[1], front_sites(t, ledger.wall_cells))

    def test_reserved_or_claimed_front_does_not_hide_a_buildable_side(self):
        for restriction in ('reserved', 'claimed', 'occupied'):
            with self.subTest(restriction=restriction):
                data = broken_front(front_connected=True)
                if restriction == 'occupied':
                    data['teamOur']['roles'] += [unit(300+i, 'imp', 12, y)
                                                for i, y in enumerate((22, 24))]
                t, cfg, mem, nav, ledger = self.setup(data)
                if restriction == 'reserved':
                    ledger.reserved.update({(12, 22), (12, 24)})
                elif restriction == 'claimed':
                    ledger.build_claims.update({p: (2, 'wall') for p in ((12, 22), (12, 24))})
                self.assertTrue(self.repair(t, cfg, mem, nav, ledger))
                self.assertEqual(ledger.commands['1'], command('build', (9, 24), name='wall'))

    def test_connection_keeps_material_for_the_whole_selected_face(self):
        data = broken_front(stones=0)
        # This mine returns the worker to the outside of the selected face,
        # where all three walls fit the existing collect/build/return budget.
        stone = next(z for z in data['mapInfo']['zones'] if z['neutralType'] == 'stone')
        stone['pos'] = dict(x=8, y=26)
        t, cfg, mem, nav, ledger = self.setup(data)
        self.assertTrue(self.repair(t, cfg, mem, nav, ledger))
        job = ledger.work_jobs[1]
        self.assertEqual(job['kind'], 'mine')
        self.assertTrue(job['want_stone'])
        self.assertEqual(job['stone_goal'], 3)
        self.assertEqual(set(job['stone_sites']), {(9, 24), (10, 24), (11, 24)})

    def test_observed_connection_restores_front_priority(self):
        data = broken_front()
        t, cfg, mem, nav, ledger = self.setup(data)
        self.assertTrue(self.repair(t, cfg, mem, nav, ledger))
        self.assertEqual(mem.build_targets[1], (9, 24))
        # A later observation confirms the side now reaches the front corner.
        data['roundNo'] += 3
        data['teamOur']['roles'] += [unit(300+i, 'wall', x, 24, health=1000)
                                    for i, x in enumerate((9, 10, 11))]
        t, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        self.assertTrue(self.repair(t, cfg, mem, nav, ledger))
        self.assertEqual(mem.build_targets[1], (12, 24))

    def test_no_buildable_wall_or_daylight_leaves_no_repair_lock(self):
        for restriction in ('reserved', 'failed', 'cutoff'):
            with self.subTest(restriction=restriction):
                t, cfg, mem, nav, ledger = self.setup(broken_front(tick=67 if restriction == 'cutoff' else 10))
                if restriction == 'reserved':
                    ledger.reserved.update(ledger.wall_cells)
                elif restriction == 'failed':
                    mem.build_failures.update({p: t.round + 20 for p in ledger.wall_cells})
                self.assertFalse(self.repair(t, cfg, mem, nav, ledger))
                self.assertFalse(ledger.commands)
                self.assertIsNone(mem.wall_repair_worker)
                self.assertFalse(mem.wall_repair_delivering)


if __name__ == '__main__':
    unittest.main()
