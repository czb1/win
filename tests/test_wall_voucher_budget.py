"""Wall purchase budgets across breaches, construction and inventory changes."""
import unittest

from test_agent import unit
import test_wall_supply_trip
from agent.commands import command
from agent.economy import use_inventory, supplies
from agent.economy_plan import wall_level_limit
from agent.intelligence import Memory
from agent.late_offense import maintenance_budget
from agent.wall_supply import requirements, shopping_list, prepare, PACK, V1, V2


class WallVoucherBudgetTests(unittest.TestCase):
    def frame(self, mirror=False):
        fixture = test_wall_supply_trip.WallSupplyTripTests()
        sites = [(x, y) for x in range(1, 7) for y in range(5, 12)
                 if x in (1, 6) or y in (5, 11)]
        data = fixture.case(backpack=[PACK] * 4)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['roleType'] != 'wall']
        # Front and side breached last night; a rear segment remains unbuilt.
        gaps = {(6, 9), (3, 5), (1, 8), (1, 9)}
        data['teamOur']['roles'] += [
            unit(100+i, 'wall', *p, level=1 if p == (2, 11) else 2,
                 health=1000 if p == (2, 11) else 1500)
            for i, p in enumerate(sites) if p not in gaps]
        data['mapInfo']['zones'] = [z for z in data['mapInfo']['zones']
                                    if (z['pos']['x'], z['pos']['y']) != (2, 5)]
        mem = Memory(wall_watch_id=1, wall_hits={(6, 9): 1, (3, 5): 2},
                     wall_rebuild_levels={(6, 9): 3, (3, 5): 2})
        if mirror:
            width, height = data['mapInfo']['width'], data['mapInfo']['height']

            def reflect(p):
                return width - 1 - p[0], height - 1 - p[1]

            for r in data['teamOur']['roles']:
                x, y = reflect((r['pos']['x'], r['pos']['y']))
                r['pos'] = dict(x=x - int(r['roleType'] == 'station'),
                                y=y + int(r['roleType'] == 'station'))
            for z in data['mapInfo']['zones']:
                x, y = reflect((z['pos']['x'], z['pos']['y']))
                z['pos'] = dict(x=x, y=y)
            sites = [reflect(p) for p in sites]
            mem.wall_hits = {reflect(p): n for p, n in mem.wall_hits.items()}
            mem.wall_rebuild_levels = {reflect(p): n for p, n in mem.wall_rebuild_levels.items()}
        cfg = dict(layout_mode='explicit', loadout=['rocket'],
                   wall_cells=sites, llm_enabled=False)
        from test_agent import setup_case
        return data, mem, cfg, setup_case(data, **cfg)

    def test_mirrored_budget_counts_actual_l1_and_front_side_breaches(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                _, mem, _, (turn, _, _, ledger) = self.frame(mirror)
                report = requirements(turn, mem, turn.units[1], ledger.wall_cells)
                self.assertEqual(report['gaps'], 2)
                self.assertEqual(report['demand'][V1], 3)
                self.assertEqual(report['demand'][V2], 7)
                order = shopping_list(turn, mem, turn.units[1], ledger.wall_cells, 1000, 100)
                self.assertEqual(order['order'], {V1: 3, V2: 7})
                self.assertEqual(order['cost'], 270)

    def test_destroyed_rear_is_not_prefetched_even_with_hits_and_rebuild_memory(self):
        _, mem, _, (turn, _, _, ledger) = self.frame()
        mem.wall_hits[(1, 8)] = 10
        mem.wall_rebuild_levels[(1, 8)] = 3
        report = requirements(turn, mem, turn.units[1], ledger.wall_cells)
        self.assertEqual(report['gaps'], 2)
        self.assertEqual(report['demand'][V1], 3)
        self.assertEqual(report['demand'][V2], 7)

    def test_rear_only_needs_voucher_after_real_wall_is_built(self):
        data, mem, cfg, (turn, _, _, ledger) = self.frame()
        before = requirements(turn, mem, turn.units[1], ledger.wall_cells)
        data['teamOur']['roles'].append(unit(999, 'wall', 1, 8, level=1, health=1000))
        from test_agent import setup_case
        turn, _, _, ledger = setup_case(data, **cfg)
        after = requirements(turn, mem, turn.units[1], ledger.wall_cells)
        self.assertEqual(after['demand'][V1], before['demand'][V1] + 1)
        self.assertEqual(after['demand'][V2], before['demand'][V2])

    def test_side_damage_and_legacy_l3_intent_do_not_add_l3_purchase(self):
        data, mem, cfg, _ = self.frame()
        side = next(r for r in data['teamOur']['roles'] if r['roleType'] == 'wall'
                    and r['pos'] == dict(x=4, y=5))
        side['health'] = 1
        mem.wall_hits[(4, 5)] = 10
        mem.wall_rebuild_levels[(4, 5)] = 3
        from test_agent import setup_case
        turn, _, _, ledger = setup_case(data, **cfg)
        report = requirements(turn, mem, turn.units[1], ledger.wall_cells)
        self.assertEqual(report['key_three'], 7)
        self.assertEqual(report['demand'][V2], 7)

    def test_failed_or_permanently_occupied_breaches_are_not_prepaid(self):
        for obstacle in ('failed', 'enemy_building', 'neutral'):
            data, mem, cfg, _ = self.frame()
            if obstacle == 'failed':
                mem.build_failures[(3, 5)] = 999
            elif obstacle == 'enemy_building':
                data['teamEnemy']['roles'].append(unit(999, 'wall', 3, 5))
            else:
                data['mapInfo']['zones'].append(dict(neutralType='stone', pos=dict(x=3, y=5)))
            from test_agent import setup_case
            turn, _, _, ledger = setup_case(data, **cfg)
            report = requirements(turn, mem, turn.units[1], ledger.wall_cells)
            self.assertEqual(report['gaps'], 1)
            self.assertEqual(report['demand'][V1], 2)
            self.assertEqual(report['demand'][V2], 7)

    def test_stock_on_both_workers_is_subtracted_and_excess_never_rebought(self):
        data, mem, cfg, _ = self.frame()
        data['teamOur']['roles'][1]['backpack'] += [V1, V2]
        data['teamOur']['roles'][2]['backpack'] = [V1] * 2 + [V2] * 6
        from test_agent import setup_case
        turn, _, _, ledger = setup_case(data, **cfg)
        self.assertFalse(shopping_list(turn, mem, turn.units[1], ledger.wall_cells,
                                      1000, 100)['order'])

    def test_front_destruction_does_not_reclassify_side_as_front(self):
        data, mem, cfg, _ = self.frame()
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                    if r['roleType'] != 'wall' or r['pos']['x'] != 6]
        from test_agent import setup_case
        turn, _, _, ledger = setup_case(data, **cfg)
        turn.planned_wall_sites = ledger.wall_cells
        self.assertEqual(wall_level_limit(turn, (5, 5)), 2)
        self.assertEqual(wall_level_limit(turn, (6, 9)), 3)
        self.assertEqual(requirements(turn, mem, turn.units[1], ledger.wall_cells)['demand'][V2], 7)

    def test_complete_l2_sides_do_not_reserve_attack_maintenance_gold(self):
        from test_daylight_spending import case, setup
        data, cfg = case(tick=40, packs=9)
        data['teamOur']['roles'][3]['backpack'] = ['StationUpgradeVoucher1']
        turn, _, _, ledger = setup(data, cfg)
        mem = Memory(wall_watch_id=1)
        self.assertEqual(maintenance_budget(turn, mem, ledger, turn.units[1]), (0, 90))
        self.assertFalse(shopping_list(turn, mem, turn.units[1], ledger.wall_cells,
                                      1000, 100)['order'])

    def test_paid_front_stock_keeps_delivery_priority_over_side_stock(self):
        fixture = test_wall_supply_trip.WallSupplyTripTests()
        data = fixture.case(backpack=[PACK] * 4 + [V2])
        data['teamOur']['roles'][1]['pos'] = dict(x=5, y=8)
        data['teamOur']['roles'][5].update(level=2, health=1500)
        data['teamOur']['roles'][7].update(level=2, health=1)
        turn, _, nav, ledger = fixture.setup(data)
        self.assertTrue(use_inventory(turn, nav, ledger, turn.units[1], mem=Memory(wall_watch_id=1)))
        self.assertEqual(ledger.commands['1'], command('use', (6, 8), name=V2))

    def test_general_purchase_does_not_prefetch_side_l3_from_paid_l2(self):
        from test_wall_upgrade_delivery import WallUpgradeDeliveryTests
        fixture = WallUpgradeDeliveryTests()
        data = fixture.case()
        data['teamOur']['roles'][3]['level'] = 3
        data['teamOur']['roles'][4]['level'] = 1
        data['teamOur']['roles'][0]['backpack'] = [V1]
        turn, cfg, nav, ledger = fixture.setup(data)
        self.assertIsNone(supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                                   bulk=True, item_only=V2))

    def test_same_face_anchor_does_not_consume_front_voucher_on_side(self):
        fixture = test_wall_supply_trip.WallSupplyTripTests()
        data = fixture.case(backpack=[PACK] * 4 + [V2] * 2)
        data['teamOur']['roles'][1]['pos'] = dict(x=2, y=10)
        data['teamOur']['roles'][5].update(level=2, health=1500)
        data['teamOur']['roles'][8].update(level=2, health=1)
        mem = Memory(wall_watch_id=1,
                     wall_supply_trip=dict(key=(4, 1), phase='deliver', shop=(9, 8),
                                           anchor=(1, 11), pending=None))
        turn, cfg, nav, ledger = fixture.setup(data)
        prepare(turn, cfg, mem, nav, ledger, turn.units[1], ledger.wall_cells)
        self.assertNotEqual(ledger.commands.get('1'), command('use', (1, 11), name=V2))
        self.assertIn(mem.wall_supply_trip['anchor'], {(6, 5), (6, 8)})

    def test_late_watch_reserves_paid_front_stock_when_only_side_is_adjacent(self):
        fixture = test_wall_supply_trip.WallSupplyTripTests()
        for tick in (40, 70):
            data = fixture.case(tick=tick, backpack=[PACK] * 4 + [V2])
            data['teamOur']['roles'][1]['pos'] = dict(x=2, y=10)
            data['teamOur']['roles'][8].update(level=2, health=1)
            turn, _, nav, ledger = fixture.setup(data)
            self.assertFalse(use_inventory(turn, nav, ledger, turn.units[1],
                                           mem=Memory(wall_watch_id=1), local_only=True))


if __name__ == '__main__':
    unittest.main()
