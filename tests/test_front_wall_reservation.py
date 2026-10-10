"""Opening-only batch construction, guard safety and permanent handoff."""
import copy
import unittest
from unittest.mock import patch
from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.front_wall import prepare_front, finish_front
from agent.intelligence import Memory
from agent.model import pos


class FrontWallTests(unittest.TestCase):
    sites = [[6, 9], [6, 10], [6, 11]]

    def case(self, stones=0, guard='worker', rno=1):
        p = payload(rno, roles=[unit(13, 'station', 3, 11),
                               unit(10, 'worker', 3, 7, backpack=['stone'] * stones),
                               unit(12, guard, 6, 10)])
        p['mapInfo']['zones'] = [{'neutralType': 'stone', 'pos': {'x': 3, 'y': 6}, 'remain': 10}]
        return p

    def prepare(self, p, mem=None, **kw):
        turn, cfg, nav, ledger = setup_case(p, layout_mode='explicit', wall_cells=self.sites, **kw)
        mem = mem or Memory()
        prepare_front(turn, cfg, mem, nav, ledger, self.sites_as_tuples())
        return ledger, mem

    def sites_as_tuples(self):
        return [tuple(p) for p in self.sites]

    def test_first_round_holds_center_and_gathers_full_front_batch(self):
        p = self.case(stones=1)
        ledger, mem = self.prepare(p, opening_guard='worker')
        self.assertEqual(ledger.commands['10']['action'], 'collect')
        self.assertEqual(ledger.work_jobs[10]['stone_goal'], 3)
        self.assertIn(12, ledger.used)
        self.assertNotIn('12', ledger.commands)
        self.assertEqual(mem.front_wall_job['center'], (6, 10))
        self.assertEqual(mem.front_wall_job['phase'], 'gather')
        p['roundNo'] = 2
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 2
        ledger, mem = self.prepare(p, mem)
        self.assertEqual(ledger.commands['10']['action'], 'collect')
        p['roundNo'] = 3
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 3
        ledger, mem = self.prepare(p, mem)
        self.assertEqual(mem.front_wall_job['phase'], 'build')
        self.assertEqual(ledger.commands['10']['action'], 'move')

    def test_auto_prefers_imp_then_idle_pioneer_over_economic_worker(self):
        for kind in ('imp', 'pioneer'):
            p = self.case(guard=kind)
            p['teamOur']['roles'].append(unit(14, 'worker', 5, 8))
            ledger, mem = self.prepare(p)
            self.assertEqual(mem.front_wall_job['guard'], 12)
            self.assertNotIn(14, ledger.used)

    def test_other_worker_keeps_collecting_while_imp_reserves_front(self):
        p = self.case(guard='imp')
        p['teamOur']['roles'].append(unit(14, 'worker', 8, 8))
        p['mapInfo']['zones'] += [
            {'neutralType': 'copper', 'pos': {'x': 9, 'y': 8}},
            {'neutralType': 'vendor', 'pos': {'x': 8, 'y': 7}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        result = Agent(Config(llm_enabled=False, layout_mode='explicit', wall_cells=self.sites)).decide(p)
        self.assertEqual(result['roleCommandMap']['14']['action'], 'collect')
        self.assertEqual(pos(result['roleCommandMap']['14']['targetPos'][0]), (9, 8))

    def test_finish_recomputes_deadline_without_cancelling_released_guard_work(self):
        p = self.case()
        _, mem = self.prepare(p)
        mem.front_wall_job['guard_released'] = True
        mem.preparation_tick = 34
        guard_job = dict(kind='mine', target=(9, 8))
        mem.daytime_jobs = {12: guard_job}
        finish_front(mem, 'front_complete')
        self.assertEqual(mem.preparation_tick, 70)
        self.assertEqual(mem.daytime_jobs[12], guard_job)
        mem.preparation_tick = 37
        p['roundNo'] = 2
        self.prepare(p, mem)
        self.assertEqual(mem.preparation_tick, 37)

    def test_guard_starts_toward_middle_in_both_corners(self):
        for station, builder, guard in [((3, 11), (2, 8), (2, 9)), ((10, 4), (12, 6), (12, 5))]:
            p = payload(roles=[unit(13, 'station', *station),
                               unit(10, 'worker', *builder, backpack=['stone'] * 6),
                               unit(12, 'worker', *guard)])
            t, c, n, l = setup_case(p, opening_guard='worker')
            mem = Memory()
            prepare_front(t, c, mem, n, l, list(l.wall_cells))
            target = mem.front_wall_job['center']
            front_y = [y for x, y in l.wall_cells if x == target[0]]
            self.assertLessEqual(abs(2 * target[1] - min(front_y) - max(front_y)), 1)
            self.assertEqual(l.commands['12']['action'], 'move')

    def test_guard_handoff_uses_observed_empty_square(self):
        self.sites = [[6, 10]]
        p = self.case(stones=1)
        p['teamOur']['roles'][1]['pos'] = {'x': 5, 'y': 10}
        ledger, mem = self.prepare(p)
        self.assertEqual(ledger.commands['12']['action'], 'move')
        self.assertNotIn('10', ledger.commands)
        self.assertIn(10, ledger.used)
        p['roundNo'] = 2
        p['teamOur']['roles'][2]['pos'] = ledger.commands['12']['targetPos'][0]
        ledger, mem = self.prepare(p, mem)
        self.assertEqual(ledger.commands['10']['action'], 'build')
        self.assertEqual(pos(ledger.commands['10']['targetPos'][0]), (6, 10))

    def test_builder_already_on_last_wall_site_steps_aside_before_building(self):
        self.sites = [[6, 10]]
        p = self.case(stones=1)
        p['teamOur']['roles'][1]['pos'] = {'x': 6, 'y': 10}
        p['teamOur']['roles'][2]['pos'] = {'x': 4, 'y': 8}
        ledger, mem = self.prepare(p)
        self.assertEqual(ledger.commands['10']['action'], 'move')
        p['roundNo'] = 2
        p['teamOur']['roles'][1]['pos'] = ledger.commands['10']['targetPos'][0]
        ledger, mem = self.prepare(p, mem)
        self.assertEqual(ledger.commands['10']['action'], 'build')
        self.assertEqual(pos(ledger.commands['10']['targetPos'][0]), (6, 10))

    def test_failed_guard_move_is_bounded_and_never_builds_on_occupant(self):
        self.sites = [[6, 10]]
        p = self.case(stones=1)
        p['teamOur']['roles'][1]['pos'] = {'x': 5, 'y': 10}
        mem = Memory()
        for rno in (1, 2, 3):
            p['roundNo'] = rno
            ledger, mem = self.prepare(p, mem)
            self.assertNotIn('10', ledger.commands)
        self.assertEqual(mem.front_wall_job['phase'], 'done')
        p['roundNo'] = 4
        ledger, mem = self.prepare(p, mem)
        self.assertFalse(ledger.used)

    def test_does_not_refill_after_each_wall(self):
        p = self.case(stones=3)
        _, mem = self.prepare(p)
        p['roundNo'] = 2
        p['teamOur']['roles'][1].update(pos={'x': 5, 'y': 9}, backpack=['stone'] * 2)
        p['teamOur']['roles'].append(unit(50, 'wall', 6, 9))
        ledger, mem = self.prepare(p, mem)
        self.assertEqual(mem.front_wall_job['phase'], 'build')
        self.assertFalse(any(c['action'] == 'collect' for c in ledger.commands.values()))

    def test_completed_front_never_restarts_after_a_wall_is_lost(self):
        p = self.case(stones=3)
        _, mem = self.prepare(p)
        p['roundNo'] = 2
        p['teamOur']['roles'] += [unit(50+i, 'wall', *point) for i, point in enumerate(self.sites)]
        ledger, mem = self.prepare(p, mem)
        self.assertFalse(ledger.used)
        self.assertEqual(mem.front_wall_job['reason'], 'front_complete')
        for rno in (3, 20, 130):
            p['roundNo'] = rno
            p['teamOur']['roles'] = p['teamOur']['roles'][:3]
            mem.wall_rebuild_levels[(6, 10)] = 3
            ledger, mem = self.prepare(p, mem)
            self.assertFalse(ledger.used)
            self.assertFalse(ledger.commands)

    def test_deadline_release_is_permanent(self):
        p = self.case()
        _, mem = self.prepare(p)
        for rno in (40, 41, 70, 130, 131):
            p['roundNo'] = rno
            ledger, mem = self.prepare(p, mem)
            self.assertFalse(ledger.used)
            self.assertEqual(mem.front_wall_job['phase'], 'done')

    def test_midday_reconnect_and_night_never_start_opening(self):
        for rno in (2, 25, 70, 129, 130):
            ledger, mem = self.prepare(self.case(stones=3, rno=rno))
            self.assertFalse(ledger.used)
            self.assertEqual(mem.front_wall_job['phase'], 'done')

    def test_both_round_origins_can_start(self):
        for origin in (0, 1):
            ledger, mem = self.prepare(self.case(rno=origin), round_origin=origin)
            self.assertEqual(ledger.commands['10']['action'], 'collect')
            self.assertEqual(mem.front_wall_job['phase'], 'gather')

    def test_no_material_source_or_full_pack_releases_everyone(self):
        for full in (False, True):
            p = self.case()
            if full:
                p['teamOur']['roles'][1].update(backpack=['copper'], backPackCapability=1)
                p['teamOur']['roles'][2].update(backpack=['copper'], backPackCapability=1)
            else:
                p['mapInfo']['zones'] = []
            ledger, mem = self.prepare(p)
            self.assertFalse(ledger.used)
            self.assertEqual(mem.front_wall_job['phase'], 'done')

    def test_disappeared_builder_is_not_replaced_with_other_worker(self):
        p = self.case()
        _, mem = self.prepare(p)
        p['roundNo'] = 2
        p['teamOur']['roles'] = [h for h in p['teamOur']['roles'] if h['id'] != 10]
        ledger, mem = self.prepare(p, mem)
        self.assertFalse(ledger.used)
        self.assertEqual(mem.front_wall_job['reason'], 'builder_unavailable')

    def test_failed_material_attempt_does_not_hold_guard(self):
        p = self.case()
        _, mem = self.prepare(p)
        mem.collect_failures[(3, 6)] = 20
        p['roundNo'] = 2
        ledger, mem = self.prepare(p, mem)
        self.assertFalse(ledger.used)
        self.assertEqual(mem.front_wall_job['phase'], 'done')

    def test_visible_worker_redirects_guard_within_front_only(self):
        p = self.case()
        _, mem = self.prepare(p, opening_guard='worker')
        p['roundNo'] = 2
        p['teamEnemy']['roles'] = [unit(99, 'worker', 7, 8)]
        ledger, mem = self.prepare(p, mem)
        self.assertEqual(pos(ledger.commands['12']['targetPos'][0]), (6, 9))
        self.assertEqual(ledger.notes[12]['conditions']['visible_enemy_workers'], [99])

    def test_enemy_occupant_is_never_built_over(self):
        p = self.case(stones=3)
        p['teamOur']['roles'][1]['pos'] = {'x': 5, 'y': 9}
        p['teamOur']['roles'][2]['pos'] = {'x': 4, 'y': 8}
        p['teamEnemy']['roles'] = [unit(99, 'worker', 6, 9)]
        ledger, _ = self.prepare(p)
        self.assertTrue(all(pos(c['targetPos'][0]) != (6, 9) for c in ledger.commands.values()))

    def test_task_pioneer_is_never_borrowed_and_new_task_releases_guard(self):
        for active in (True, False):
            p = self.case(guard='pioneer')
            if active:
                p['phaseTask'] = 'active'
            else:
                p['teamOur']['playerTasks'] = [{'isValid': True, 'taskPosition': {'x': 1, 'y': 1}}]
            ledger, mem = self.prepare(p)
            self.assertNotIn(12, ledger.used)
            self.assertIsNone(mem.front_wall_job['guard'])
        p = self.case(guard='pioneer')
        _, mem = self.prepare(p)
        p.update(roundNo=2, phaseTask='active')
        ledger, mem = self.prepare(p, mem)
        self.assertNotIn(12, ledger.used)
        self.assertTrue(mem.front_wall_job['guard_released'])

    def test_imp_releases_before_enemy_worker_can_catch_it(self):
        p = self.case(guard='imp')
        _, mem = self.prepare(p)
        p['roundNo'] = 2
        p['teamEnemy']['roles'] = [unit(99, 'worker', 8, 10)]
        turn, cfg, nav, ledger = setup_case(p, layout_mode='explicit', wall_cells=self.sites)
        mem.sabotage.observe(turn, mem)
        prepare_front(turn, cfg, mem, nav, ledger, self.sites_as_tuples())
        self.assertNotIn(12, ledger.used)
        self.assertTrue(mem.front_wall_job['guard_released'])

    def test_completed_opening_matches_original_day_and_night_decisions(self):
        for rno in (25, 70, 130, 140):
            p = self.case(stones=3, rno=rno)
            cfg = Config(layout_mode='explicit', wall_cells=self.sites, llm_enabled=False)
            agent = Agent(cfg)
            with patch('agent.brain.prepare_front'):
                expected = agent.decide(copy.deepcopy(p))
            other = Agent(cfg)
            actual = other.decide(copy.deepcopy(p))
            self.assertEqual(actual, expected)
