"""Current mineral amounts and worker allocation; focused turn-level checks."""
import copy
import unittest

from test_agent import payload, unit, setup_case
from agent.commands import command
from agent.intelligence import Memory
from agent.mining import mine, spare_mine
from agent.model import neighbours
from agent.worker_jobs import resume_daytime_jobs, save_daytime_jobs


def allocation_case(workers=None, mines=None, base=None, tick=140):
    workers = workers or [unit(1, 'worker', 8, 21), unit(2, 'worker', 8, 22)]
    p = payload(tick, workers + ([base] if base else []))
    p['mapInfo'].update(width=41, height=32)
    p['mapInfo']['zones'] = [
        dict(neutralType=kind, pos=dict(x=x, y=y), remain=remain)
        for kind, x, y, remain in (mines or [('copper', 11, 18, 10), ('copper', 11, 24, 10)])]
    p['vendorShopList'] = [dict(name='copper', price=10), dict(name='iron', price=6),
                           dict(name='stone', price=3)]
    return p


def wing_case(**kwargs):
    return allocation_case(base=unit(13, 'station', 3, 28, level=3), **kwargs)


def rotate(p):
    p = copy.deepcopy(p)
    width, height = p['mapInfo']['width'], p['mapInfo']['height']
    for role in p['teamOur']['roles']:
        x, y = role['pos']['x'], role['pos']['y']
        role['pos'] = (dict(x=width - 2 - x, y=height - y) if role['roleType'] == 'station'
                       else dict(x=width - 1 - x, y=height - 1 - y))
    for zone in p['mapInfo']['zones']:
        zone['pos'] = dict(x=width - 1 - zone['pos']['x'], y=height - 1 - zone['pos']['y'])
    p['teamOur']['type'] = 'defender'
    return p


class MiningAmountTests(unittest.TestCase):
    def setup(self, p):
        return setup_case(p, layout_mode='explicit')

    def test_actual_remaining_changes_the_profitable_target(self):
        p = allocation_case(workers=[unit(1, 'worker', 5, 5)],
                            mines=[('copper', 8, 5, 1), ('iron', 5, 7, 10)])
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory(mine_collected={(8, 5): 0, (5, 7): 9})
        self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[0], stockpile=True))
        self.assertEqual(ledger.mine_claims[1], (5, 7))
        self.assertEqual(ledger.commands['1']['action'], 'move')
        facts = ledger.notes[1]['conditions']
        self.assertEqual((facts['remaining_ore'], facts['expected_units']), (10, 10))

    def test_legacy_count_is_used_only_when_remain_is_absent(self):
        p = allocation_case(workers=[unit(1, 'worker', 5, 5)], mines=[('copper', 8, 5, None)])
        for remain, expected in ((None, 1), (10, 10)):
            with self.subTest(remain=remain):
                p['mapInfo']['zones'][0]['remain'] = remain
                t, cfg, nav, ledger = self.setup(p)
                self.assertTrue(mine(t, cfg, Memory(mine_collected={(8, 5): 9}),
                                     nav, ledger, t.workers[0], stockpile=True))
                self.assertEqual(ledger.notes[1]['conditions']['expected_units'], expected)

    def test_worker_does_not_travel_to_ore_exhausted_before_arrival(self):
        p = allocation_case(workers=[unit(1, 'worker', 7, 4), unit(2, 'worker', 5, 6)],
                            mines=[('copper', 8, 4, 1), ('iron', 5, 7, 10)])
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory(mine_targets={1: (8, 4)})
        self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[1], stockpile=True))
        self.assertEqual(ledger.commands['2'], command('collect', (5, 7)))
        self.assertEqual(ledger.notes[2]['conditions']['skipped']['other_worker_exhausts_mine'], 1)

    def test_same_turn_claim_prevents_a_wasted_second_journey(self):
        p = allocation_case(workers=[unit(1, 'worker', 7, 4), unit(2, 'worker', 5, 6)],
                            mines=[('copper', 8, 4, 1)])
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory()
        self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[0], stockpile=True))
        self.assertFalse(mine(t, cfg, mem, nav, ledger, t.workers[1], stockpile=True))
        self.assertNotIn('2', ledger.commands)

    def test_one_off_collection_also_reserves_the_last_unit(self):
        p = allocation_case(workers=[unit(1, 'worker', 7, 4), unit(2, 'worker', 5, 6)],
                            mines=[('copper', 8, 4, 1)])
        t, cfg, nav, ledger = self.setup(p)
        self.assertTrue(ledger.add(1, command('collect', (8, 4))))
        self.assertFalse(mine(t, cfg, Memory(), nav, ledger, t.workers[1], stockpile=True))
        self.assertNotIn('2', ledger.commands)

    def test_two_adjacent_workers_can_each_collect_the_last_unit(self):
        p = allocation_case(workers=[unit(1, 'worker', 5, 5), unit(2, 'worker', 5, 6)],
                            mines=[('copper', 6, 5, 1)])
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory()
        for h in t.workers:
            self.assertTrue(mine(t, cfg, mem, nav, ledger, h, stockpile=True))
            self.assertEqual(ledger.commands[str(h.id)], command('collect', (6, 5)))
            self.assertEqual(ledger.notes[h.id]['conditions']['expected_units'], 1)

    def test_simultaneous_arrivals_can_share_the_last_unit(self):
        p = allocation_case(workers=[unit(1, 'worker', 5, 4), unit(2, 'worker', 5, 6)],
                            mines=[('copper', 8, 5, 1)])
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory()
        for h in t.workers:
            self.assertTrue(mine(t, cfg, mem, nav, ledger, h, stockpile=True))
            self.assertEqual(ledger.notes[h.id]['conditions']['route_steps'], 2)
            self.assertEqual(ledger.notes[h.id]['conditions']['expected_units'], 1)

    def test_a_full_or_selling_worker_does_not_consume_the_forecast(self):
        for status in ('full', 'selling', 'shopping', 'dizzy'):
            with self.subTest(status=status):
                p = allocation_case(workers=[
                    unit(1, 'worker', 7, 4, backpack=['iron'] * 100 if status == 'full' else [],
                         abnormalState='dizzy' if status == 'dizzy' else ''),
                    unit(2, 'worker', 5, 6)], mines=[('copper', 8, 4, 1)])
                t, cfg, nav, ledger = self.setup(p)
                mem = Memory(mine_targets={1: (8, 4)}, sale_workers={1} if status == 'selling' else set())
                if status == 'shopping':
                    mem.daytime_jobs[1] = dict(kind='buy', target=(8, 4))
                self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[1], stockpile=True))
                self.assertEqual(ledger.notes[2]['conditions']['claimed_by'], ())

    def test_accepted_non_mining_action_releases_a_stale_claim(self):
        p = allocation_case(workers=[unit(1, 'worker', 7, 4), unit(2, 'worker', 5, 6)],
                            mines=[('copper', 8, 4, 1)])
        t, cfg, nav, ledger = self.setup(p)
        self.assertTrue(ledger.add(1, command('move', (7, 3))))
        self.assertTrue(mine(t, cfg, Memory(mine_targets={1: (8, 4)}),
                             nav, ledger, t.workers[1], stockpile=True))
        self.assertEqual(ledger.notes[2]['conditions']['claimed_by'], ())

    def test_partner_capacity_and_material_goal_limit_pre_arrival_collection(self):
        for status in ('one_slot', 'material_goal', 'spare'):
            with self.subTest(status=status):
                p = allocation_case(workers=[
                    unit(1, 'worker', 6, 5, backpack=['iron'] * 99 if status == 'one_slot' else []),
                    unit(2, 'worker', 3, 5)], mines=[('stone', 7, 5, 3)])
                t, cfg, nav, ledger = self.setup(p)
                mem = Memory(mine_targets={1: (7, 5)})
                if status != 'one_slot':
                    mem.daytime_jobs[1] = dict(kind='spare' if status == 'spare' else 'mine',
                                             target=(7, 5), want_stone=True, stone_goal=1)
                self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[1], stockpile=True))
                self.assertEqual(ledger.notes[2]['conditions']['expected_units'], 2)

    def test_real_obstacle_detour_replaces_straight_line_arrival_estimate(self):
        p = allocation_case(workers=[unit(1, 'worker', 5, 5), unit(2, 'worker', 12, 5)],
                            mines=[('copper', 8, 5, 1)])
        p['mapInfo'].update(width=15, height=15)
        p['teamOur']['roles'] += [unit(100 + y, 'wall', 6, y) for y in range(13)]
        t, cfg, nav, ledger = self.setup(p)
        self.assertGreater(nav.approach(t.workers[0], [(8, 5)])[0], 3)
        self.assertTrue(mine(t, cfg, Memory(mine_targets={1: (8, 5)}),
                             nav, ledger, t.workers[1], stockpile=True))
        self.assertEqual(ledger.notes[2]['conditions']['expected_units'], 1)

    def test_exhausted_ore_is_rejected_even_by_the_final_ledger(self):
        p = allocation_case(workers=[unit(1, 'worker', 5, 5)], mines=[('copper', 6, 5, 0)])
        t, cfg, nav, ledger = self.setup(p)
        self.assertFalse(mine(t, cfg, Memory(), nav, ledger, t.workers[0], stockpile=True))
        self.assertFalse(ledger.add(1, command('collect', (6, 5))))


class MiningSectorTests(unittest.TestCase):
    def setup(self, p):
        return setup_case(p, layout_mode='explicit')

    def test_two_workers_choose_different_wings_on_rectangular_and_rotated_maps(self):
        for mirrored, reverse in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(mirrored=mirrored, reverse=reverse):
                p = rotate(wing_case()) if mirrored else wing_case()
                t, cfg, nav, ledger = self.setup(p)
                mem = Memory()
                for h in reversed(t.workers) if reverse else t.workers:
                    self.assertTrue(mine(t, cfg, mem, nav, ledger, h, stockpile=True))
                expected = {1: (11, 18), 2: (11, 24)}
                if mirrored:
                    expected = {uid: (40 - x, 31 - y) for uid, (x, y) in expected.items()}
                self.assertEqual(ledger.mine_claims, expected)

    def test_worker_crosses_when_own_wing_is_empty_unreachable_or_poor(self):
        for reason in ('empty', 'unreachable', 'poor', 'exhausted', 'cooldown'):
            with self.subTest(reason=reason):
                p = wing_case()
                mem = Memory()
                if reason == 'empty':
                    p['mapInfo']['zones'].pop(0)
                elif reason == 'unreachable':
                    p['teamOur']['roles'] += [unit(100 + i, 'wall', *q)
                                             for i, q in enumerate(neighbours((11, 18)))]
                elif reason == 'poor':
                    p['mapInfo']['zones'][0]['neutralType'] = 'iron'
                    p['vendorShopList'][1]['price'] = 1
                elif reason == 'exhausted':
                    p['mapInfo']['zones'][0]['remain'] = 0
                else:
                    mem.movement.targets[1, (11, 18)] = 150
                t, cfg, nav, ledger = self.setup(p)
                self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[0], stockpile=True))
                self.assertEqual(ledger.mine_claims[1], (11, 24))

    def test_material_requirement_can_cross_a_wing(self):
        p = wing_case(mines=[('copper', 11, 18, 10), ('stone', 11, 24, 10)])
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit', wall_cells=[[6, 26], [6, 27]])
        self.assertTrue(mine(t, cfg, Memory(), nav, ledger, t.workers[0], want_stone=True, stone_goal=2))
        self.assertEqual(ledger.mine_claims[1], (11, 24))
        self.assertEqual(ledger.notes[1]['conditions']['expected_units'], 2)

    def test_eighty_percent_boundary_is_inclusive_but_worse_ore_is_not_preferred(self):
        for price, expected in ((8, (11, 18)), (7, (11, 24))):
            with self.subTest(price=price):
                p = wing_case(mines=[('iron', 11, 18, 10), ('copper', 11, 24, 10)])
                p['vendorShopList'][1]['price'] = price
                t, cfg, nav, ledger = self.setup(p)
                self.assertTrue(mine(t, cfg, Memory(), nav, ledger, t.workers[0], stockpile=True))
                self.assertEqual(ledger.mine_claims[1], expected)

    def test_a_boundary_deposit_is_available_to_both_workers(self):
        p = allocation_case(workers=[unit(1, 'worker', 7, 4), unit(2, 'worker', 7, 5)],
                            base=unit(13, 'station', 3, 9), mines=[('copper', 8, 4, 10)])
        p['mapInfo'].update(width=15, height=15)
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory()
        for h in t.workers:
            self.assertTrue(mine(t, cfg, mem, nav, ledger, h, stockpile=True))
            self.assertEqual(ledger.commands[str(h.id)], command('collect', (8, 4)))
            self.assertEqual(ledger.notes[h.id]['conditions']['target_sector'], 'boundary')

    def test_worker_position_changes_do_not_swap_direction_preferences(self):
        p = wing_case(workers=[unit(1, 'worker', 8, 22), unit(2, 'worker', 8, 21)])
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory()
        for h in t.workers:
            self.assertTrue(mine(t, cfg, mem, nav, ledger, h, stockpile=True))
        self.assertEqual(ledger.mine_claims, {1: (11, 18), 2: (11, 24)})

    def test_no_actual_mines_produces_no_corner_exploration(self):
        p = wing_case()
        p['mapInfo']['zones'] = []
        t, cfg, nav, ledger = self.setup(p)
        for h in t.workers:
            self.assertFalse(mine(t, cfg, Memory(), nav, ledger, h, stockpile=True))
        self.assertFalse(ledger.commands)

    def test_direction_preference_does_not_send_a_lone_worker_away_from_better_ore(self):
        p = wing_case(workers=[unit(1, 'worker', 8, 22)])
        t, cfg, nav, ledger = self.setup(p)
        self.assertTrue(mine(t, cfg, Memory(), nav, ledger, t.workers[0], stockpile=True))
        self.assertEqual(ledger.mine_claims[1], (11, 24))
        self.assertIsNone(ledger.notes[1]['conditions']['mining_sector'])

    def test_without_a_base_a_worker_still_selects_a_real_mine(self):
        t, cfg, nav, ledger = self.setup(allocation_case())
        self.assertTrue(mine(t, cfg, Memory(), nav, ledger, t.workers[0], stockpile=True))
        self.assertIn(ledger.mine_claims[1], {(11, 18), (11, 24)})
        self.assertIsNone(ledger.notes[1]['conditions']['mining_sector'])

    def test_current_job_keeps_a_cross_wing_target_when_new_targets_are_comparable(self):
        p = wing_case()
        t, cfg, nav, ledger = self.setup(p)
        mem = Memory(preparation_tick=70)
        self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[0],
                             stockpile=True, target_only=(11, 24)))
        save_daytime_jobs(t, mem, ledger)
        p['teamOur']['roles'][0]['pos'] = ledger.commands['1']['targetPos'][0]
        p['roundNo'] += 1
        t, cfg, nav, ledger = self.setup(p)
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [], [], set())
        self.assertEqual(ledger.mine_claims[1], (11, 24))
        self.assertTrue(ledger.notes[1]['conditions']['continuation'])

    def test_exhaustion_releases_a_job_and_refresh_uses_the_new_amount(self):
        p = wing_case()
        mem = Memory(preparation_tick=70, mine_targets={1: (11, 18)},
                     mine_collected={(11, 18): 9}, daytime_jobs={1: dict(
                         kind='mine', target=(11, 18), ore='copper', want_stone=False,
                         stockpile=True, deadline=None, stone_goal=None)})
        p['mapInfo']['zones'][0]['remain'] = 0
        t, cfg, nav, ledger = self.setup(p)
        resume_daytime_jobs(t, cfg, mem, nav, ledger, [], [], set())
        self.assertNotIn(1, mem.daytime_jobs)
        p['roundNo'] += 1
        p['mapInfo']['zones'][0]['remain'] = 10
        t, cfg, nav, ledger = self.setup(p)
        self.assertTrue(mine(t, cfg, mem, nav, ledger, t.workers[0], stockpile=True))
        self.assertEqual(ledger.mine_claims[1], (11, 18))
        self.assertEqual(ledger.notes[1]['conditions']['expected_units'], 10)

    def test_existing_target_is_not_reassigned_by_a_wing_preference(self):
        t, cfg, nav, ledger = self.setup(wing_case())
        self.assertTrue(mine(t, cfg, Memory(mine_targets={1: (11, 24)}),
                             nav, ledger, t.workers[0], stockpile=True))
        self.assertEqual(ledger.mine_claims[1], (11, 24))
        self.assertTrue(ledger.notes[1]['conditions']['retained_previous'])

    def test_wing_preference_does_not_override_a_return_deadline(self):
        t, cfg, nav, ledger = setup_case(wing_case(tick=199), layout_mode='explicit', return_margin=5)
        self.assertFalse(mine(t, cfg, Memory(), nav, ledger, t.workers[0], stockpile=True))
        self.assertFalse(ledger.commands)


class SpareMiningAllocationTests(unittest.TestCase):
    def test_short_spare_trips_also_split_the_two_wings(self):
        p = wing_case(workers=[unit(1, 'worker', 8, 23), unit(2, 'worker', 8, 24)],
                      mines=[('copper', 6, 22, 10), ('copper', 10, 24, 10)])
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit')
        mem = Memory()
        for h in t.workers:
            self.assertTrue(spare_mine(t, cfg, mem, nav, ledger, h))
        self.assertEqual(ledger.mine_claims, {1: (6, 22), 2: (10, 24)})

    def test_spare_worker_does_not_chase_a_deposit_being_exhausted(self):
        p = wing_case(workers=[unit(1, 'worker', 7, 22), unit(2, 'worker', 5, 24)],
                      mines=[('copper', 8, 22, 1)])
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit')
        mem = Memory(mine_targets={1: (8, 22)})
        self.assertFalse(spare_mine(t, cfg, mem, nav, ledger, t.workers[1]))
        self.assertFalse(ledger.commands)

    def test_spare_worker_crosses_when_its_wing_has_no_nearby_mine(self):
        p = wing_case(workers=[unit(1, 'worker', 8, 23), unit(2, 'worker', 8, 24)],
                      mines=[('copper', 10, 24, 10)])
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit')
        self.assertTrue(spare_mine(t, cfg, Memory(), nav, ledger, t.workers[0]))
        self.assertEqual(ledger.mine_claims[1], (10, 24))


if __name__ == '__main__':
    unittest.main()

