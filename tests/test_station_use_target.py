"""Base vouchers target the adjacent footprint cell, not a distant anchor."""
import unittest

from test_agent import payload, setup_case, unit
from agent.commands import command
from agent.economy import use_inventory
from agent.intelligence import Memory
from agent.model import pos
from agent.worker_jobs import resume_daytime_jobs


class StationUseTargetTests(unittest.TestCase):
    def case(self, origin=(3, 11), actor=(5, 9), level=1, kind='worker', round_no=131):
        return payload(round_no, [
            unit(13, 'station', *origin, health=100, level=level),
            unit(10, kind, *actor, health=500,
                 backpack=[f'StationUpgradeVoucher{level}']),
        ])

    def test_all_sides_and_corners_use_nearest_cell_for_both_bases(self):
        for origin in ((3, 11), (10, 4)):
            x, y = origin
            ring = {(a, b) for a in range(x - 1, x + 3)
                    for b in range(y - 2, y + 2)
                    if a not in (x, x + 1) or b not in (y - 1, y)}
            for actor in sorted(ring):
                expected = (max(x, min(actor[0], x + 1)),
                            max(y - 1, min(actor[1], y)))
                for level in (1, 2):
                    for kind in ('worker', 'pioneer'):
                        for round_no in (131, 461):
                            with self.subTest(origin=origin, actor=actor, level=level,
                                              kind=kind, round_no=round_no):
                                t, _, n, l = setup_case(self.case(origin, actor, level, kind, round_no))
                                self.assertTrue(use_inventory(t, n, l, t.heroes[0],
                                                              local_only=True, mem=Memory()))
                                self.assertEqual(l.commands['10'], command(
                                    'use', expected, name=f'StationUpgradeVoucher{level}'))
                                self.assertTrue(t.adjacent(actor, expected))

    def test_distant_anchor_is_rejected_even_when_actor_is_next_to_base(self):
        t, _, _, l = setup_case(self.case())
        self.assertEqual(t.base_distance((5, 9)), 1)
        self.assertFalse(l.add(10, command('use', (3, 11), name='StationUpgradeVoucher1')))
        self.assertNotIn(10, l.used)
        self.assertTrue(l.add(10, command('use', (4, 10), name='StationUpgradeVoucher1')))

    def test_different_cells_share_one_base_upgrade_claim(self):
        data = self.case(actor=(5, 10))
        data['teamOur']['roles'].append(unit(12, 'worker', 2, 11,
                                             backpack=['StationUpgradeVoucher1']))
        t, _, _, l = setup_case(data)
        self.assertTrue(l.add(10, command('use', (4, 10), name='StationUpgradeVoucher1')))
        self.assertFalse(l.add(12, command('use', (3, 11), name='StationUpgradeVoucher1')))
        self.assertEqual(l.upgrade_claims, {13})

    def test_nonanchor_cell_keeps_kind_and_level_checks(self):
        for level, name in ((1, 'StationUpgradeVoucher2'), (3, 'StationUpgradeVoucher2'),
                            (1, 'WallUpgradeVoucher1'), (1, 'WeaponUpgradeVoucher1')):
            with self.subTest(level=level, name=name):
                data = self.case(level=level)
                data['teamOur']['roles'][1]['backpack'] = [name]
                _, _, _, l = setup_case(data)
                self.assertFalse(l.add(10, command('use', (4, 10), name=name)))
                self.assertFalse(l.upgrade_claims)

    def test_nonbase_or_far_cell_is_rejected(self):
        for actor, target in (((5, 9), (5, 10)), ((7, 8), (4, 10))):
            with self.subTest(actor=actor, target=target):
                _, _, _, l = setup_case(self.case(actor=actor))
                self.assertFalse(l.add(10, command('use', target, name='StationUpgradeVoucher1')))

    def test_recovery_uses_current_position_instead_of_stored_base_anchor(self):
        for actor, expected in (((5, 9), (4, 10)), ((2, 12), (3, 11))):
            with self.subTest(actor=actor):
                t, c, n, l = setup_case(self.case(actor=actor))
                mem = Memory()
                goal = (10, 'use', (3, 11))
                action = mem.recovery.action(goal, t, c, mem, n, l)
                self.assertEqual(action, command('use', expected, name='StationUpgradeVoucher1'))
                self.assertTrue(l.add(10, action))

    def test_resumed_delivery_recomputes_target_after_movement(self):
        data = self.case(actor=(7, 8))
        t, _, n, l = setup_case(data)
        mem = Memory()
        self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=mem))
        self.assertEqual(l.commands['10']['action'], 'move')
        self.assertEqual(l.work_jobs[10]['target'], (3, 11))
        mem.daytime_jobs = l.work_jobs.copy()
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['pos'] = {'x': 5, 'y': 9}
        t, c, n, l = setup_case(data)
        resume_daytime_jobs(t, c, mem, n, l, [], [], returning=set())
        self.assertEqual(pos(l.commands['10']['targetPos'][0]), (4, 10))
        self.assertEqual(l.commands['10']['action'], 'use')


if __name__ == '__main__':
    unittest.main()
