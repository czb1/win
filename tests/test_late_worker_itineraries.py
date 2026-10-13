"""Bounded snapshots for split duties, shared gold and complete walking costs."""
import unittest
from unittest.mock import patch
from dataclasses import replace

import test_wall_supply_trip as fixture
from test_agent import unit
from agent.commands import command
from agent.intelligence import Memory
from agent.wall_supply import (PACK, V1, V2, prepare, other_worker, shared_orders,
                               builder_home, _sale, mining_tail)
from agent.mining import mine, return_destination
from agent.economy_plan import itinerary_route, itinerary_field
from agent.brain import Agent
from agent.model import neighbours


class LateWorkerItinerariesTests(unittest.TestCase):
    def setup(self, data=None, **kwargs):
        case = fixture.WallSupplyTripTests()
        return case.setup(data if data is not None else case.case(**kwargs))

    def test_shared_budget_funds_minimum_packs_then_l2_before_extras(self):
        t, cfg, nav, ledger = self.setup(gold=85)
        orders = shared_orders(t, cfg, Memory(wall_watch_id=1), ledger, ledger.wall_cells)
        self.assertEqual(orders, {1: {PACK: 3}, 2: {V1: 2}})
        self.assertEqual(ledger.wall_budget_report['cost'], 85)
        self.assertIs(orders, shared_orders(t, cfg, Memory(wall_watch_id=1), ledger, ledger.wall_cells))

    def test_mining_forecast_includes_sale_funded_shop_without_spending_future_gold(self):
        t, cfg, nav, ledger = self.setup(tick=15, gold=0)
        mem = Memory(wall_watch_id=1)
        tail = mining_tail(t, cfg, mem, nav, ledger, t.units[1], (11, 12), 'copper', 10)
        self.assertIsNotNone(tail)
        self.assertEqual(tail[2], (9, 8))
        self.assertEqual(ledger.gold, 0)
        self.assertEqual(ledger.wall_orders[1], {})

    def test_builder_capacity_cannot_consume_watcher_pack_slots(self):
        data = fixture.WallSupplyTripTests().case(gold=1000)
        data['teamOur']['roles'][2]['backPackCapability'] = 1
        t, cfg, _, ledger = self.setup(data)
        orders = shared_orders(t, cfg, Memory(wall_watch_id=1), ledger, ledger.wall_cells)
        self.assertEqual(orders[1], {PACK: 4})
        self.assertEqual(orders[2], {V1: 1})
        self.assertLessEqual(ledger.wall_budget_report['cost'], t.gold)

    def test_watcher_legacy_vouchers_do_not_cancel_builder_demand(self):
        t, cfg, _, ledger = self.setup(backpack=[V1] * 10 + [V2] * 10)
        orders = shared_orders(t, cfg, Memory(wall_watch_id=1), ledger, ledger.wall_cells)
        self.assertEqual(orders[2], {V1: 2, V2: 3})
        self.assertEqual(set(orders[1]), {PACK})

    def test_both_workers_mine_before35_even_with_paid_wall_vouchers(self):
        data = fixture.WallSupplyTripTests().case(tick=20, backpack=[V1])
        data['teamOur']['roles'][2]['backpack'] = [V2]
        t, cfg, nav, ledger = self.setup(data)
        mem = Memory(wall_watch_id=1)
        with patch('agent.mining.earn', return_value=True) as earn, patch('agent.economy.repair_walls') as build:
            self.assertFalse(prepare(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)[0])
            other_worker(t, cfg, mem, nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
        earn.assert_called_once()
        build.assert_not_called()
        self.assertFalse(ledger.commands)

    def test_sale_can_fit_without_any_shop(self):
        t, cfg, nav, ledger = self.setup(tick=62, backpack=['copper'] * 30)
        mem = Memory(wall_watch_id=1)
        t.zones = {p: k for p, k in t.zones.items() if k != 'weaponShop'}
        home, _ = return_destination(t, nav, ledger, t.units[1], mem)
        self.assertTrue(_sale(t, cfg, mem, nav, ledger, t.units[1], home, {PACK: 4}))
        self.assertEqual(ledger.work_jobs[1]['kind'], 'sell')
        self.assertTrue(ledger.notes[1]['conditions']['sale_only'])

    def test_sale_rejected_when_even_safe_landing_is_too_late(self):
        t, cfg, nav, ledger = self.setup(tick=69, backpack=['copper'] * 30)
        mem = Memory(wall_watch_id=1)
        home, _ = return_destination(t, nav, ledger, t.units[1], mem)
        self.assertFalse(_sale(t, cfg, mem, nav, ledger, t.units[1], home, {}))
        self.assertFalse(ledger.commands)

    def test_large_builder_load_sells_before_paid_upgrade(self):
        data = fixture.WallSupplyTripTests().case(gold=0)
        data['teamOur']['roles'][2].update(pos=dict(x=5, y=8), backpack=[V1] + ['copper'] * 30)
        t, cfg, nav, ledger = self.setup(data)
        other_worker(t, cfg, Memory(wall_watch_id=1), nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
        self.assertEqual(ledger.work_jobs[2]['kind'], 'sell')
        self.assertNotEqual(ledger.commands['2'].get('name'), V1)

    def test_builder_uses_outside_adjacent_wall_tile(self):
        data = fixture.WallSupplyTripTests().case(gold=0)
        data['teamOur']['roles'][2].update(pos=dict(x=7, y=8), backpack=[V1])
        t, cfg, nav, ledger = self.setup(data)
        with patch('agent.economy.repair_walls', return_value=set()):
            other_worker(t, cfg, Memory(wall_watch_id=1), nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
        self.assertEqual(ledger.commands['2'], command('use', (6, 8), name=V1))
        self.assertEqual(ledger.notes[2]['conditions']['work_cell'], (7, 8))

    def test_builder_keeps_face_before_switching_to_other_side(self):
        data = fixture.WallSupplyTripTests().case(gold=0)
        data['teamOur']['roles'][2].update(pos=dict(x=2, y=6), backpack=[V2])
        data['teamOur']['roles'][7].update(level=2, health=1500)
        t, cfg, nav, ledger = self.setup(data)
        mem = Memory(wall_watch_id=1, wall_supply_trip=dict(key=(4, 1), phase='mine', shop=None,
            pending=None, builder=dict(actor=2, phase='deliver', shop=None, pending=None, closed=True, anchor=(6, 8))))
        with patch('agent.economy.repair_walls', return_value=set()):
            other_worker(t, cfg, mem, nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
        self.assertEqual(ledger.work_jobs[2]['target'], (6, 5))

    def test_builder_landing_is_independent_of_watcher_inside_goals(self):
        t, cfg, nav, ledger = self.setup()
        mem = Memory(wall_watch_id=1)
        watcher, _ = return_destination(t, nav, ledger, t.units[1], mem)
        builder, _ = builder_home(t, mem, nav, ledger, t.units[2], ledger.wall_cells)
        self.assertTrue(builder - watcher)
        ledger.operator_posts[2] = (2, 7)
        self.assertEqual(builder_home(t, mem, nav, ledger, t.units[2], ledger.wall_cells), ({(2, 7)}, True))

    def test_remaining_itinerary_contains_material_and_upgrading_actions(self):
        t, cfg, nav, ledger = self.setup(tick=20)
        mem = Memory(wall_watch_id=1)
        tail = mining_tail(t, cfg, mem, nav, ledger, t.units[2], (11, 12), 'copper')
        self.assertIsNotNone(tail)
        self.assertGreater(tail[4], 1)
        self.assertIn(2, ledger.wall_mining_material)
        self.assertGreater(ledger.wall_mining_material[2][1], 0)

    def test_separate_deposit_beats_previous_shared_target(self):
        data = fixture.WallSupplyTripTests().case(tick=15, gold=0)
        data['mapInfo']['zones'] += [dict(neutralType='copper', pos=dict(x=10, y=12), remain=10)]
        data['teamOur']['roles'][2]['pos'] = dict(x=11, y=11)
        t, cfg, nav, ledger = self.setup(data)
        mem = Memory(wall_watch_id=1, mine_targets={1: (12, 12), 2: (12, 12)})
        ledger.mine_claims[1] = (12, 12)
        self.assertTrue(mine(t, cfg, mem, nav, ledger, t.units[2], deadline=35,
                             target_only=(12, 12)))
        self.assertEqual(ledger.mine_claims[2], (10, 12))

    def test_complete_itinerary_rejects_far_mine_near_cutoff(self):
        t, cfg, nav, ledger = self.setup(tick=34)
        self.assertFalse(mine(t, cfg, Memory(wall_watch_id=1), nav, ledger, t.units[1], deadline=35))
        self.assertFalse(ledger.commands)

    def test_itinerary_field_matches_enumerated_endpoint_paths(self):
        t, cfg, nav, ledger = self.setup()
        hero = t.units[1]
        groups = [[(10, 8)], [(9, 8)], {(5, 7), (2, 7)}]
        trip = itinerary_route(nav, hero, groups, final_exact=True)
        field = itinerary_field(nav, hero, groups, final_exact=True)
        self.assertEqual(trip[0], field[hero.pos])
        totals = []
        for vendor_cell in neighbours(groups[0][0]):
            first = nav.search(hero, {vendor_cell})
            if first is None:
                continue
            at_vendor = replace(hero, pos=vendor_cell)
            for shop_cell in neighbours(groups[1][0]):
                second = nav.search(at_vendor, {shop_cell})
                if second is None:
                    continue
                last = nav.search(replace(hero, pos=shop_cell), groups[2])
                if last:
                    totals.append(first[0] + second[0] + last[0])
        self.assertEqual(trip[0], min(totals))
        self.assertNotIn(trip[1][2], t.blocked)
        self.assertEqual(t.units[1].pos, hero.pos)

    def test_agent_uses_split_budget_during_active_task(self):
        data = fixture.WallSupplyTripTests().case(gold=85)
        data['phaseTask'] = 'ongoing'
        case = fixture.WallSupplyTripTests()
        _, cfg, _, _ = case.setup(data)
        agent = Agent(cfg)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1'], command('buy', name=PACK, num=3))
        self.assertNotEqual(commands.get('2', {}).get('name'), PACK)
        self.assertEqual(next(iter(agent.sessions.values())).wall_watch_id, 1)

    def test_builder_buys_both_tiers_before_any_delivery(self):
        data = fixture.WallSupplyTripTests().case()
        data['teamOur']['roles'][2]['pos'] = dict(x=8, y=9)
        data['teamOur']['roles'][3]['pos'] = dict(x=7, y=10)
        data['teamOur']['roles'].append(unit(40, 'wall', 6, 11, level=1, health=1000))
        mem = Memory(wall_watch_id=1)
        for name, count in ((V1, 2), (V2, 3)):
            t, cfg, nav, ledger = self.setup(data)
            prepare(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
            ledger.gold -= sum(n * t.shop[k] for k, n in ledger.wall_orders[1].items()) if '1' not in ledger.commands else 0
            other_worker(t, cfg, mem, nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
            self.assertEqual(ledger.commands['2'], command('buy', name=name, num=count))
            self.assertEqual(mem.wall_supply_trip['builder']['phase'], 'shop')
            data['teamOur']['roles'][2]['backpack'] += [name] * count
            data['teamOur']['goldNum'] -= count * t.shop[name]
            data['roundNo'] += 1
        t, cfg, nav, ledger = self.setup(data)
        other_worker(t, cfg, mem, nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
        self.assertNotEqual(ledger.commands.get('2', {}).get('action'), 'buy')
        self.assertTrue(mem.wall_supply_trip['builder']['closed'])

    def test_failed_builder_purchase_retries_before_delivering(self):
        data = fixture.WallSupplyTripTests().case()
        data['teamOur']['roles'][2]['pos'] = dict(x=8, y=9)
        data['teamOur']['roles'][3]['pos'] = dict(x=7, y=10)
        data['teamOur']['roles'].append(unit(40, 'wall', 6, 11, level=1, health=1000))
        mem = Memory(wall_watch_id=1)
        for _ in range(2):
            t, cfg, nav, ledger = self.setup(data)
            other_worker(t, cfg, mem, nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
            self.assertEqual(ledger.commands['2'], command('buy', name=V1, num=2))
            data['roundNo'] += 1

    def test_material_stone_is_retained_while_copper_load_is_sold(self):
        data = fixture.WallSupplyTripTests().case(gold=0)
        data['teamOur']['roles'][2]['backpack'] = ['stone'] * 2 + ['copper'] * 20
        t, cfg, nav, ledger = self.setup(data)
        mem = Memory(wall_watch_id=1)
        other_worker(t, cfg, mem, nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
        self.assertEqual(mem.stone_reserves[2], cfg.wall_stones)
        self.assertEqual(ledger.work_jobs[2]['kind'], 'sell')

    def test_unfinished_firepower_uses_only_unallocated_wall_budget(self):
        data = fixture.WallSupplyTripTests().case()
        data['teamOur']['roles'][4]['level'] = 2
        t, cfg, nav, ledger = self.setup(data)
        mem = Memory(wall_watch_id=1)
        seen = []
        def finish_gun(*args, **kwargs):
            seen.append(args[4].gold)
            args[4].used.add(2)
        with patch('agent.economy.worker', side_effect=finish_gun):
            reserve = other_worker(t, cfg, mem, nav, ledger, t.units[2], ledger.tower_cells, ledger.wall_cells)
        self.assertEqual(seen, [t.gold - reserve])
        self.assertGreater(reserve, 0)


if __name__ == '__main__':
    unittest.main()
