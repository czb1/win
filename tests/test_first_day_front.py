"""First-day priority and blocked-site fallback without hero reservations."""
import unittest
from test_agent import payload, unit, setup_case
from agent.economy import first_day_front, build, wall_keeps_access
from agent.intelligence import Memory
from agent.brain import Agent
from agent.config import Config
from agent.model import distance, pos
from agent.worker_jobs import resume_daytime_jobs


class FirstDayFrontTests(unittest.TestCase):
    def front_gap_case(self, round_no=40, position=(27, 12), mirror=False):
        # Reduced geometry from the reported day-one gap at (28, 10).
        p = payload(round_no, roles=[
            unit(20013, 'station', 30, 10),
            unit(20012, 'worker', *position, backpack=['stone'] * 5),
        ] + [unit(41000+y, 'wall', 28, y) for y in (7, 8, 9, 11, 12)])
        p['mapInfo'].update(width=41, height=32)
        p['teamOur'].update(type='defender', goldNum=7)
        if mirror:
            p['teamOur']['type'] = 'challenger'
            for role in p['teamOur']['roles']:
                # A station position is the left edge of its two-cell width.
                role['pos']['x'] = (39 if role['roleType'] == 'station' else 40) - role['pos']['x']
        return p

    def test_front_gap_target_survives_approach_with_unbuilt_guns(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                mem = Memory()
                target = (12 if mirror else 28, 10)
                snapshots = (((28, 13), (27, 12)), ((27, 12), (27, 11)),
                             ((27, 11), None))
                for tick, (point, step) in enumerate(snapshots, 40):
                    t, c, n, l = setup_case(self.front_gap_case(tick, point, mirror))
                    first_day_front(t, c, mem, n, l, list(l.wall_cells))
                    self.assertIn(20012, l.used, (tick, point, l.commands))
                    self.assertEqual(mem.build_targets[20012], target)
                    action = l.commands['20012']
                    if step is None:
                        self.assertEqual(action['action'], 'build')
                        self.assertEqual(action['name'], 'wall')
                        self.assertEqual(pos(action['targetPos'][0]), target)
                    else:
                        self.assertEqual(action['action'], 'move')
                        next_pos = pos(action['targetPos'][0])
                        self.assertEqual(distance(next_pos, target),
                                         distance(t.workers[0].pos, target) - 1)
                        if not mirror:
                            self.assertEqual(next_pos, step)

    def test_day_one_front_can_lengthen_but_not_disconnect_future_gun_access(self):
        t, c, n, l = setup_case(self.front_gap_case())
        hero = t.workers[0]
        before = n.approach(hero, [(31, 8)])[0]
        original = t.blocked.copy()
        t.blocked.add((28, 10))
        after = n.approach(hero, [(31, 8)])[0]
        t.blocked = original
        self.assertGreater(after, before)
        self.assertTrue(wall_keeps_access(t, n, l, (28, 10)))
        self.assertTrue(build(t, c, Memory(), n, l, hero, list(l.wall_cells), lambda _: 'wall'))
        self.assertIn((28, 10), l.build_claims)

        p = payload(40, [unit(13, 'station', 0, 5),
                         unit(10, 'worker', 2, 3, backpack=['stone'])]
                    + [unit(100+y, 'wall', 3, y) for y in range(7) if y != 3])
        p['mapInfo'].update(width=7, height=7)
        t, _, n, l = setup_case(p, layout_mode='explicit',
                                wall_cells=[[3, 3]], weapon_cells=[[5, 3]])
        self.assertIsNotNone(n.approach(t.workers[0], [(5, 3)]))
        self.assertFalse(wall_keeps_access(t, n, l, (3, 3)))

    def test_future_gun_shortcut_protection_remains_for_day_two_and_flanks(self):
        t, _, n, l = setup_case(self.front_gap_case(round_no=170))
        self.assertFalse(wall_keeps_access(t, n, l, (28, 10)))

        p = payload(40, [unit(13, 'station', 30, 10),
                         unit(10, 'worker', 31, 13, backpack=['stone']),
                         unit(50, 'wall', 30, 12), unit(51, 'wall', 32, 12)])
        p['mapInfo'].update(width=41, height=32)
        t, _, n, l = setup_case(p)
        self.assertFalse(wall_keeps_access(t, n, l, (31, 12)))

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
        self.assertEqual(l.notes[10]['conditions']['expected_units'], 10)
        self.assertEqual(len(l.notes[10]['conditions']['construction_targets']), 10)
        p['teamOur']['roles'][1]['backpack'] = ['stone']
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertEqual(l.commands['10']['action'], 'collect')
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 6
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertEqual(l.commands['10']['action'], 'collect')
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 10
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertNotEqual(l.commands['10']['action'], 'collect')
        self.assertTrue(m.day1_wall_delivering)
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 9
        t, c, n, l = setup_case(p)
        first_day_front(t, c, m, n, l, list(l.wall_cells))
        self.assertNotEqual(l.commands['10']['action'], 'collect')

    def test_perimeter_material_stays_within_demand_and_configured_batch(self):
        for batch, expected in ((10, 3), (2, 2)):
            with self.subTest(batch=batch):
                p = self.case()
                p['teamOur']['roles'] = p['teamOur']['roles'][:2]
                p['teamOur']['roles'][1].update(pos={'x': 5, 'y': 7}, backpack=[])
                p['mapInfo']['zones'] = [dict(neutralType='stone', pos=dict(x=4, y=6), remain=10)]
                t, c, n, l = setup_case(p, layout_mode='explicit', stone_batch=batch,
                                       wall_cells=[[6, 8], [6, 9], [5, 9]])
                first_day_front(t, c, Memory(), n, l, list(l.wall_cells))
                self.assertEqual(l.commands['10']['action'], 'collect')
                self.assertEqual(l.notes[10]['conditions']['expected_units'], expected)

    def test_late_front_trip_reduces_extra_material_to_fit_construction(self):
        p = self.case(55)
        p['teamOur']['roles'] = p['teamOur']['roles'][:2]
        p['teamOur']['roles'][1].update(pos={'x': 5, 'y': 7}, backpack=[])
        p['mapInfo']['zones'] = [dict(neutralType='stone', pos=dict(x=4, y=6), remain=10)]
        t, c, n, l = setup_case(p)
        first_day_front(t, c, Memory(), n, l, list(l.wall_cells))
        self.assertEqual(l.commands['10']['action'], 'collect')
        facts = l.notes[10]['conditions']
        self.assertLess(facts['expected_units'], c.stone_batch)
        self.assertLessEqual(facts['expected_units'] + facts['construction_steps'], 67 - t.tick)

    def test_missing_material_route_never_creates_wait_lock(self):
        p = self.case()
        p['teamOur']['roles'][1]['backpack'] = []
        t, c, n, l = setup_case(p)
        first_day_front(t, c, Memory(), n, l, list(l.wall_cells))
        self.assertFalse(l.used)
