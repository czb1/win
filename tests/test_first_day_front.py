"""First-day priority and blocked-site fallback without hero reservations."""
import unittest
from test_agent import payload, unit, setup_case
from agent.economy import first_day_front, build
from agent.intelligence import Memory
from agent.brain import Agent
from agent.config import Config
from agent.model import pos
from agent.worker_jobs import resume_daytime_jobs


class FirstDayFrontTests(unittest.TestCase):
    def case(self, round_no=1):
        p = payload(round_no, roles=[unit(13, 'station', 3, 11),
                                    unit(10, 'worker', 5, 10, backpack=['stone'] * 6),
                                    unit(12, 'worker', 8, 9), unit(14, 'imp', 7, 10)])
        return p

    def test_front_before_shop_and_only_builder_used(self):
        t, c, n, l = setup_case(self.case())
        m = Memory()
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertEqual(l.used, {10})
        self.assertEqual(l.commands['10']['action'], 'build')
        self.assertEqual(pos(l.commands['10']['targetPos'][0])[0], 6)
        self.assertFalse(l.work_jobs)

    def test_blocked_middle_does_not_prevent_other_front_segment(self):
        p = self.case()
        p['teamOur']['roles'].append(unit(50, 'wall', 6, 8))
        p['teamEnemy']['roles'] = [unit(90, 'worker', 6, 9)]
        t, c, n, l = setup_case(p)
        self.assertTrue(build(t, c, Memory(), n, l, t.workers[0],
                              [(6, 9), (6, 10), (6, 11)], lambda _: 'wall'))
        self.assertEqual(l.commands['10']['action'], 'build')
        self.assertNotEqual(pos(l.commands['10']['targetPos'][0]), (6, 9))

    def test_all_front_blocked_allows_other_wall_positions(self):
        p = self.case()
        p['teamOur']['roles'] = p['teamOur']['roles'][:2]
        p['teamOur']['roles'][1]['pos'] = {'x': 5, 'y': 9}
        p['teamEnemy']['roles'] = [unit(90+i, 'worker', 6, y) for i, y in enumerate(range(8, 14))]
        t, c, n, l = setup_case(p)
        m = Memory()
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertFalse(l.used)
        self.assertTrue(build(t, c, m, n, l, t.workers[0], list(l.wall_cells), lambda _: 'wall'))
        self.assertEqual(l.commands['10']['action'], 'build')
        self.assertNotEqual(pos(l.commands['10']['targetPos'][0])[0], 6)

    def test_friendly_occupant_not_held_or_ordered_to_vacate(self):
        p = self.case()
        p['teamOur']['roles'][3]['pos'] = {'x': 6, 'y': 10}
        t, c, n, l = setup_case(p)
        first_day_front(t, c, Memory(), n, l, list(l.wall_cells))
        self.assertNotIn(14, l.used)
        self.assertEqual(l.commands['10']['action'], 'build')
        self.assertNotEqual(pos(l.commands['10']['targetPos'][0]), (6, 10))

    def test_day_two_and_night_do_not_get_priority(self):
        for r in (70, 130, 260):
            t, c, n, l = setup_case(self.case(r))
            first_day_front(t, c, Memory(), n, l, list(l.wall_cells))
            self.assertFalse(l.used)

    def test_delivery_sale_and_recalled_worker_are_not_borrowed(self):
        for kind in ('sell', 'buy', 'use'):
            t, c, n, l = setup_case(self.case())
            m = Memory(daytime_jobs={10: {'kind': kind, 'target': (8, 8)}})
            first_day_front(t, c, m, n, l, list(l.wall_cells), excluded={12})
            self.assertFalse(l.used)
            self.assertEqual(m.daytime_jobs[10]['kind'], kind)
        t, c, n, l = setup_case(self.case())
        first_day_front(t, c, Memory(), n, l, list(l.wall_cells), excluded={10, 12})
        self.assertFalse(l.used)

    def test_finished_front_releases_builder_and_other_worker_keeps_mining(self):
        p = self.case()
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 9, 'y': 9}, 'remain': 10}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        p['teamOur']['roles'] += [unit(50+i, 'wall', 6, y) for i, y in enumerate(range(8, 14))]
        t, c, n, l = setup_case(p)
        m = Memory(day1_wall_worker=10, day1_wall_delivering=True)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertFalse(l.used)
        self.assertIsNone(m.day1_wall_worker)
        result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertIn('12', result)
        self.assertNotEqual(result['12']['action'], 'build')

    def test_gather_batch_once_then_spend_remaining_stones(self):
        p = self.case()
        p['teamOur']['roles'] = p['teamOur']['roles'][:2]
        p['teamOur']['roles'][1]['pos'] = {'x': 5, 'y': 7}
        p['teamOur']['roles'][1]['backpack'] = []
        p['mapInfo']['zones'] = [{'neutralType': 'stone', 'pos': {'x': 4, 'y': 6}, 'remain': 10}]
        m = Memory()
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertEqual(l.commands['10']['action'], 'collect')
        p['teamOur']['roles'][1]['backpack'] = ['stone']
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertEqual(l.commands['10']['action'], 'collect')
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 6
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertNotEqual(l.commands['10']['action'], 'collect')
        self.assertTrue(m.day1_wall_delivering)
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 5
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertNotEqual(l.commands['10']['action'], 'collect')

    def test_missing_material_route_never_creates_wait_lock(self):
        p = self.case()
        p['teamOur']['roles'][1]['backpack'] = []
        t, c, n, l = setup_case(p)
        first_day_front(t, c, Memory(), n, l, list(l.wall_cells))
        self.assertFalse(l.used)
