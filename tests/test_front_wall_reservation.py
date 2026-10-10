"""Front-site ownership and legal day/night handoff regressions."""
import unittest
from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.front_wall import prepare_front, stage_front_breach
from agent.intelligence import Memory
from agent.model import pos


class FrontWallTests(unittest.TestCase):
    def prepare(self, data, mem=None, **kw):
        turn, cfg, nav, ledger = setup_case(data, **kw)
        mem = mem or Memory()
        prepare_front(turn, cfg, mem, nav, ledger, list(ledger.wall_cells))
        return turn, cfg, nav, ledger, mem

    def test_first_day_builds_before_normal_economy_in_both_corners(self):
        for station, worker, target in [((3, 11), (5, 10), (6, 10)),
                                         ((10, 4), (9, 4), (8, 4))]:
            p = payload(roles=[unit(13, 'station', *station),
                               unit(10, 'worker', *worker, backpack=['stone'])])
            p['mapInfo']['zones'] = [{'neutralType': 'vendor', 'pos': {'x': 0, 'y': 0}}]
            result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
            self.assertEqual(result['10']['action'], 'build')
            self.assertEqual(result['10']['name'], 'wall')
            self.assertEqual(pos(result['10']['targetPos'][0])[0], target[0])

    def test_guard_vacates_then_worker_builds_from_observed_empty_tile(self):
        p = payload(roles=[unit(13, 'station', 3, 11),
                           unit(10, 'worker', 5, 10, backpack=['stone']),
                           unit(14, 'imp', 6, 10)])
        mem = Memory(front_wall_job=dict(target=(6, 10), worker=10, started=1))
        _, _, _, ledger, mem = self.prepare(p, mem)
        self.assertEqual(ledger.commands['14']['action'], 'move')
        self.assertNotIn('10', ledger.commands)
        self.assertIn(10, ledger.used)
        p['roundNo'] = 2
        p['teamOur']['roles'][2]['pos'] = ledger.commands['14']['targetPos'][0]
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertEqual(ledger.commands['10']['action'], 'build')
        self.assertEqual(pos(ledger.commands['10']['targetPos'][0]), (6, 10))
        self.assertNotIn('14', ledger.commands)

    def test_failed_guard_move_does_not_build_on_occupied_cell(self):
        p = payload(roles=[unit(13, 'station', 3, 11),
                           unit(10, 'worker', 5, 10, backpack=['stone']), unit(14, 'imp', 6, 10)])
        mem = Memory(front_wall_job=dict(target=(6, 10), worker=10, started=1))
        self.prepare(p, mem)
        p['roundNo'] = 2
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertNotIn('10', ledger.commands)

    def test_guard_holds_while_builder_approaches(self):
        p = payload(roles=[unit(13, 'station', 3, 11),
                           unit(10, 'worker', 3, 7, backpack=['stone']), unit(14, 'imp', 6, 10)])
        mem = Memory(front_wall_job=dict(target=(6, 10), worker=10, started=1))
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertIn(14, ledger.used)
        self.assertNotIn('14', ledger.commands)
        self.assertEqual(ledger.commands['10']['action'], 'move')

    def test_guard_moves_to_reserved_site_while_worker_collects(self):
        p = payload(roles=[unit(13, 'station', 3, 11),
                           unit(10, 'worker', 3, 7), unit(14, 'imp', 6, 9)])
        p['mapInfo']['zones'] = [{'neutralType': 'stone', 'pos': {'x': 3, 'y': 6}, 'remain': 10}]
        mem = Memory(front_wall_job=dict(target=(6, 10), worker=10, started=1))
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertEqual(ledger.commands['10']['action'], 'collect')
        self.assertEqual(pos(ledger.commands['14']['targetPos'][0]), (6, 10))

    def test_enemy_occupant_is_never_built_over(self):
        p = payload(roles=[unit(13, 'station', 3, 11),
                           unit(10, 'worker', 5, 10, backpack=['stone'])])
        p['teamEnemy']['roles'] = [unit(99, 'worker', 6, 10)]
        _, _, _, ledger, _ = self.prepare(p)
        self.assertTrue(all(pos(c['targetPos'][0]) != (6, 10) for c in ledger.commands.values()))

    def test_does_not_move_pioneer_during_task(self):
        p = payload(roles=[unit(13, 'station', 3, 11),
                           unit(10, 'worker', 5, 10, backpack=['stone']), unit(11, 'pioneer', 6, 10)])
        p['phaseTask'] = 'active'
        mem = Memory(front_wall_job=dict(target=(6, 10), worker=10, started=1))
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertNotIn('11', ledger.commands)

    def test_no_material_source_does_not_hold_guard(self):
        p = payload(roles=[unit(13, 'station', 3, 11), unit(10, 'worker', 5, 10),
                           unit(14, 'imp', 6, 10)])
        _, _, _, ledger, _ = self.prepare(p)
        self.assertFalse(ledger.used)

    def test_second_worker_can_hold_and_release_completed_site(self):
        p = payload(roles=[unit(13, 'station', 3, 11),
                           unit(10, 'worker', 3, 7, backpack=['stone']),
                           unit(12, 'worker', 6, 10)])
        mem = Memory(front_wall_job=dict(target=(6, 10), worker=10, started=1))
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertIn(12, ledger.used)
        self.assertEqual(ledger.notes[12]['reason'], 'front_wall_hold')
        p['teamOur']['roles'][2]['pos'] = {'x': 7, 'y': 10}
        p['teamOur']['roles'].append(unit(50, 'wall', 6, 10))
        _, _, _, ledger, mem = self.prepare(
            p, mem, layout_mode='explicit', wall_cells=[[6, 10]])
        self.assertFalse(ledger.used)
        self.assertFalse(mem.front_wall_job)

    def test_expired_reservation_releases_actors(self):
        p = payload(25, roles=[unit(13, 'station', 3, 11),
                               unit(10, 'worker', 3, 7, backpack=['stone']), unit(14, 'imp', 6, 10)])
        mem = Memory(front_wall_job=dict(target=(6, 10), worker=10, started=1))
        _, _, _, ledger, mem = self.prepare(p, mem)
        self.assertFalse(ledger.used)
        self.assertFalse(mem.front_wall_job)

    def test_night_stages_inside_and_dawn_rebuilds(self):
        p = payload(129, roles=[unit(13, 'station', 3, 11),
                                unit(10, 'worker', 5, 10, backpack=['stone'])])
        mem = Memory(wall_rebuild_levels={(6, 10): 3})
        t, cfg, nav, ledger = setup_case(p)
        stage_front_breach(t, cfg, mem, nav, ledger, list(ledger.wall_cells))
        self.assertIn(10, ledger.used)
        self.assertFalse(ledger.commands)
        p['roundNo'] = 130
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertEqual(ledger.commands['10']['action'], 'build')
        self.assertEqual(pos(ledger.commands['10']['targetPos'][0]), (6, 10))

    def test_night_leaves_defenders_and_threatened_workers_alone(self):
        for danger, excluded in [(False, {10}), (True, set())]:
            p = payload(100, roles=[unit(13, 'station', 3, 11),
                                    unit(10, 'worker', 5, 10, backpack=['stone'])])
            if danger:
                p['robot']['roles'] = [unit(90, 'smallRobot', 12, 10, targetTeam='challenger')]
            t, cfg, nav, ledger = setup_case(p)
            stage_front_breach(t, cfg, Memory(wall_rebuild_levels={(6, 10): 3}),
                               nav, ledger, list(ledger.wall_cells), excluded)
            self.assertFalse(ledger.used)

    def test_front_critical_site_can_be_built_before_wall_chain_reaches_it(self):
        p = payload(roles=[unit(13, 'station', 3, 11), unit(50, 'wall', 6, 8),
                           unit(10, 'worker', 5, 12, backpack=['stone'])])
        mem = Memory(front_wall_job=dict(target=(6, 12), worker=10, started=1))
        _, _, _, ledger, _ = self.prepare(p, mem)
        self.assertEqual(ledger.commands['10']['action'], 'build')
        self.assertEqual(pos(ledger.commands['10']['targetPos'][0]), (6, 12))
