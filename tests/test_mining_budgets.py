"""Connected mining routes and complete daytime material-trip budgets."""
import copy
import unittest
from unittest.mock import Mock, patch

from test_agent import Agent, Config, Ledger, Turn, payload, setup_case, unit
from agent.commands import command
from agent.intelligence import Memory
from agent.mining import mine, mining_home, night_mine
from agent.model import neighbours
from agent.navigation import DeadlineExceeded, layout
from agent.worker_jobs import resume_daytime_jobs


def material_case(tick, walls=((5, 6),), backpack=(), **cfg):
    p = payload(130 + tick, [unit(1, 'worker', 5, 5, backpack=list(backpack)),
                             unit(13, 'station', 3, 6, health=1500, level=3)])
    p['mapInfo']['zones'] = [dict(neutralType='stone', pos=dict(x=6, y=5), remain=10)]
    p['vendorShopList'] = [dict(name='stone', price=1)]
    return setup_case(p, layout_mode='explicit', wall_cells=list(walls), **cfg)


def perimeter_case(tick=62, worker=(14, 20)):
    p = payload(130 + tick, [unit(1, 'worker', *worker, health=220),
                             unit(13, 'station', 9, 22, health=1500, level=3)] +
                [unit(20 + i, 'rocket', *q, level=3)
                 for i, q in enumerate(((8, 23), (9, 23), (8, 21)))])
    p['mapInfo'].update(width=34, height=34)
    p['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y), remain=10)
                            for k, x, y in [('copper', 13, 20), ('vendor', 8, 22)]]
    p['vendorShopList'] = [dict(name='copper', price=5)]
    _, walls = layout(Turn(p, Config()), Config())
    p['teamOur']['roles'] += [unit(100 + i, 'wall', *q, health=1000, level=3)
                             for i, q in enumerate(walls) if q != (12, 21)]
    return p


def decide_with_ledger(p):
    ledgers = []

    def capture(*args):
        ledger = Ledger(*args)
        ledgers.append(ledger)
        return ledger

    agent = Agent(Config(llm_enabled=False))
    with patch('agent.brain.Ledger', side_effect=capture):
        response = agent.decide(p)
    return response, ledgers[0], next(iter(agent.sessions.values()))


class MaterialBudgetTests(unittest.TestCase):
    def test_last_buildable_tick_requires_both_collection_and_construction(self):
        for tick, accepted in ((65, True), (66, False), (67, False), (69, False)):
            with self.subTest(tick=tick):
                t, c, n, l = material_case(tick)
                self.assertEqual(mine(t, c, Memory(), n, l, t.workers[0],
                                      want_stone=True, stone_goal=10), accepted)
                if accepted:
                    self.assertEqual(l.work_jobs[1]['stone_goal'], 1)
                    self.assertEqual(l.notes[1]['conditions']['construction_steps'], 1)
                else:
                    self.assertFalse(l.commands)
                    self.assertFalse(l.work_jobs)

    def test_forecast_counts_transport_and_each_wall_in_a_batch(self):
        for tick, goal in ((59, 3), (60, 2), (63, 1)):
            with self.subTest(tick=tick):
                t, c, n, l = material_case(tick, walls=((5, 6), (5, 7), (5, 8)))
                self.assertTrue(mine(t, c, Memory(), n, l, t.workers[0],
                                     want_stone=True, stone_goal=3))
                facts = l.notes[1]['conditions']
                self.assertEqual(l.work_jobs[1]['stone_goal'], goal)
                self.assertEqual(facts['expected_units'], goal)
                self.assertEqual(facts['construction_steps'], {3: 5, 2: 3, 1: 1}[goal])
                self.assertLessEqual(goal + facts['construction_steps'], 67 - tick)

    def test_existing_stone_reduces_collection_but_preserves_wall_cost(self):
        t, c, n, l = material_case(65, backpack=['stone'], wall_stones=2)
        self.assertTrue(mine(t, c, Memory(), n, l, t.workers[0],
                             want_stone=True, stone_goal=2))
        self.assertEqual(l.work_jobs[1]['stone_goal'], 2)
        self.assertEqual(l.notes[1]['conditions']['expected_units'], 1)

    def test_return_margin_applies_to_the_complete_material_trip(self):
        for tick, margin, accepted in ((63, 5, True), (64, 5, False), (64, 0, True)):
            with self.subTest(tick=tick, margin=margin):
                t, c, n, l = material_case(tick, return_margin=margin)
                self.assertEqual(mine(t, c, Memory(), n, l, t.workers[0],
                                      want_stone=True), accepted)

    def test_assigned_operator_post_is_the_return_destination(self):
        for tick, accepted in ((63, True), (64, False)):
            with self.subTest(tick=tick):
                t, c, n, l = material_case(tick)
                l.operator_posts[1] = (0, 0)
                self.assertEqual(mine(t, c, Memory(), n, l, t.workers[0],
                                      want_stone=True), accepted)
                if accepted:
                    self.assertEqual(l.notes[1]['conditions']['return_steps'], 5)

    def test_material_budget_uses_the_requested_construction_sites(self):
        t, c, n, l = material_case(60, walls=((5, 6), (14, 14)))
        self.assertFalse(mine(t, c, Memory(), n, l, t.workers[0], want_stone=True,
                              stone_sites=[(14, 14)]))
        self.assertTrue(mine(t, c, Memory(), n, l, t.workers[0], want_stone=True,
                             stone_sites=[(5, 6)]))
        self.assertEqual(l.work_jobs[1]['stone_sites'], ((5, 6),))

    def test_forecast_does_not_build_on_the_collection_standing_cell(self):
        t, c, n, l = material_case(55, walls=((5, 5),))
        self.assertFalse(mine(t, c, Memory(), n, l, t.workers[0], want_stone=True))
        self.assertFalse(l.commands)

    def test_wall_at_vacated_start_cell_remains_an_obstacle_for_later_walls(self):
        p = payload(140, [unit(1, 'worker', 5, 5), unit(13, 'station', 2, 6, health=1500)] +
                    [unit(100 + y, 'wall', 5, y, health=1000)
                     for y in range(15) if y not in (5, 8)])
        p['mapInfo']['zones'] = [dict(neutralType='stone', pos=dict(x=7, y=5), remain=10)]
        t, c, n, l = setup_case(p, layout_mode='explicit', wall_cells=[[5, 5], [5, 8]])
        self.assertTrue(mine(t, c, Memory(), n, l, t.workers[0], want_stone=True, stone_goal=2))
        # Closing the first passage at the old actor position is legal; closing
        # the second would seal off the base and must not count as deliverable.
        self.assertEqual(l.work_jobs[1]['stone_goal'], 1)
        self.assertEqual(l.notes[1]['conditions']['construction_targets'], ((5, 5),))
        self.assertEqual(l.notes[1]['conditions']['return_steps'], 5)

    def test_continuation_reuses_the_construction_budget_and_shrinks_goal(self):
        for tick, accepted in ((65, True), (66, False)):
            with self.subTest(tick=tick):
                t, c, n, l = material_case(tick)
                mem = Memory(daytime_jobs={1: dict(kind='mine', target=(6, 5), ore='stone',
                             want_stone=True, stockpile=False, deadline=None, stone_goal=10,
                             stone_sites=((5, 6),))})
                resume_daytime_jobs(t, c, mem, n, l, [], [(5, 6)], set())
                self.assertEqual(1 in l.work_jobs, accepted)
                if accepted:
                    self.assertEqual(l.work_jobs[1]['stone_goal'], 1)
                else:
                    self.assertNotIn(1, mem.daytime_jobs)
                    self.assertFalse(l.commands)

    def test_last_daylight_action_chooses_near_spare_ore_over_far_material(self):
        p = payload(69, [unit(1, 'worker', 7, 21, health=220),
                         unit(11, 'pioneer', 8, 22),
                         unit(13, 'station', 9, 22, health=1500, level=3)] +
                    [unit(20 + i, 'rocket', *q, level=3)
                     for i, q in enumerate(((8, 23), (9, 23), (8, 21)))])
        p['mapInfo'].update(width=34, height=34)
        p['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y), remain=10)
                                for k, x, y in [('stone', 22, 21), ('copper', 6, 21),
                                               ('vendor', 3, 4), ('weaponShop', 4, 4)]]
        p['vendorShopList'] = [dict(name='stone', price=1), dict(name='copper', price=5)]
        t, c, n, l = setup_case(p)
        self.assertEqual(n.approach(t.workers[0], [(22, 21)])[0], 14)
        self.assertFalse(mine(t, c, Memory(), n, l, t.workers[0], want_stone=True))
        response, ledger, mem = decide_with_ledger(p)
        self.assertEqual(response['roleCommandMap']['1'], command('collect', (6, 21)))
        self.assertEqual(ledger.notes[1]['reason'], 'spare_mining_for_later')
        self.assertFalse(any(job.get('want_stone') for job in mem.daytime_jobs.values()))

    def test_forecast_restores_real_occupancy_and_planning_state(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                t, c, n, l = material_case(55, walls=((5, 6), (6, 6), (7, 6)))
                mem = Memory()
                guard = n.gate_guard = Mock()
                blocked, units = t.blocked, t.ours
                claims, targets = copy.deepcopy(l.build_claims), copy.deepcopy(mem.build_targets)
                if interrupted:
                    with patch('agent.mining.MaterialPlan', side_effect=DeadlineExceeded):
                        with self.assertRaises(DeadlineExceeded):
                            mine(t, c, mem, n, l, t.workers[0], want_stone=True)
                    self.assertFalse(l.commands)
                else:
                    self.assertTrue(mine(t, c, mem, n, l, t.workers[0], want_stone=True))
                self.assertIs(t.blocked, blocked)
                self.assertIs(t.ours, units)
                self.assertEqual(l.build_claims, claims)
                self.assertEqual(mem.build_targets, targets)
                self.assertIs(n.gate_guard, guard)
                # Only the real outbound route requests passage, never a
                # hypothetical construction or return route.
                self.assertEqual(guard.observe_search.call_count, 1)

    def test_night_stockpiling_remains_available(self):
        t, c, n, l = material_case(70)
        self.assertTrue(night_mine(t, c, Memory(), n, l, t.workers[0], dedicated=True))
        self.assertEqual(l.commands['1'], command('collect', (6, 5)))


class ConnectedSaleRouteTests(unittest.TestCase):
    def test_opposite_side_sale_distance_cannot_be_joined_to_current_position(self):
        t, c, n, l = setup_case(perimeter_case())
        hero = t.workers[0]
        sales = n.distances_to([(8, 22)], vacated={hero.pos})
        self.assertEqual(n.approach(hero, [(13, 20)], with_endpoint=True),
                         (0, None, (14, 20)))
        self.assertEqual(sales[hero.pos], 7)
        self.assertEqual(min(sales[q] for q in neighbours((13, 20)) if q in sales), 5)
        self.assertFalse(mine(t, c, Memory(), n, l, hero))
        self.assertEqual(l.notes[1]['conditions']['skipped'], {'sale_deadline': 1})
        self.assertFalse(l.work_jobs)

    def test_complete_sale_trip_accepts_exact_boundary_and_limits_batch(self):
        for tick, expected in ((61, 1), (60, 2)):
            with self.subTest(tick=tick):
                t, c, n, l = setup_case(perimeter_case(tick))
                self.assertTrue(mine(t, c, Memory(), n, l, t.workers[0]))
                self.assertEqual(l.notes[1]['conditions']['expected_units'], expected)
                self.assertEqual(l.notes[1]['conditions']['collection_cell'], (14, 20))

    def test_sale_margin_is_counted_after_the_connected_route(self):
        t, c, n, l = setup_case(perimeter_case(60), return_margin=2)
        self.assertFalse(mine(t, c, Memory(), n, l, t.workers[0]))

    def test_main_scheduler_can_collect_for_later_without_promising_a_sale(self):
        response, ledger, mem = decide_with_ledger(perimeter_case())
        self.assertEqual(response['roleCommandMap']['1'], command('collect', (13, 20)))
        self.assertEqual(ledger.notes[1]['reason'], 'spare_mining_for_later')
        self.assertFalse(mem.daytime_jobs)

    def test_stockpile_home_leg_uses_the_same_collection_endpoint(self):
        for tick, accepted in ((65, True), (66, False)):
            with self.subTest(tick=tick):
                t, c, n, l = setup_case(perimeter_case(tick))
                hero = t.workers[0]
                home = n.distances_to(mining_home(t, n, hero), vacated={hero.pos})
                self.assertEqual(home[hero.pos], 4)
                self.assertLess(min(home[q] for q in neighbours((13, 20)) if q in home), 4)
                self.assertEqual(mine(t, c, Memory(), n, l, hero, stockpile=True), accepted)
                if accepted:
                    self.assertEqual(l.notes[1]['conditions']['expected_units'], 1)

    def test_endpoint_matches_existing_route_and_cached_tie_breaking(self):
        t, _, n, _ = setup_case(perimeter_case(worker=(15, 20)))
        hero = t.workers[0]
        route = n.approach(hero, [(13, 20)])
        arrival = n.approach(hero, [(13, 20)], with_endpoint=True)
        self.assertEqual(arrival[:2], route)
        self.assertEqual(arrival[2], route[1])
        self.assertIn(arrival[2], neighbours((13, 20)))


if __name__ == '__main__':
    unittest.main()
