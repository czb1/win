"""Finite material priorities and real collect/sell/build travel budgets."""
import unittest

from test_agent import unit
from agent.front_rebuild import act, missing_front, finish_plan
from agent.night_economy import work, WorkNavigator
from agent.commands import command
from agent.model import pos


class FrontRebuildMaterialTests(unittest.TestCase):
    def case(self, tick=110, mirrored=False):
        from test_night_economy import NightEconomyTests
        fixture = NightEconomyTests()
        data = fixture.case(tick=tick)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                   if not (r['roleType'] == 'wall' and r['pos']['x'] == 6)]
        data['teamOur']['roles'][1]['backpack'] = ['copper'] * 8
        data['mapInfo']['zones'].append(dict(neutralType='stone', pos=dict(x=8, y=9), remain=10))
        if mirrored:
            for r in data['teamOur']['roles']:
                r['pos']['x'] = 14 - r['pos']['x']
                # Stations are top-left footprint origins in X.
                if r['roleType'] == 'station':
                    r['pos']['x'] -= 1
            for zone in data['mapInfo']['zones']:
                zone['pos']['x'] = 14 - zone['pos']['x']
            cfg = fixture.config()
            cfg.wall_cells = [[14-x, y] for x, y in cfg.wall_cells]
            cfg.weapon_cells = [[14-x, y] for x, y in cfg.weapon_cells]
            from test_agent import setup_case
            turn, _, nav, ledger = setup_case(data, **cfg.__dict__)
            from agent.intelligence import Memory
            mem = Memory(day=turn.day, gatling_operator_id=1, wall_watch_id=2)
            return data, turn, cfg, mem, nav, ledger
        turn, cfg, mem, nav, ledger = fixture.setup(data)
        return data, turn, cfg, mem, nav, ledger

    def test_no_damage_memory_all_front_gone_stone_preempts_high_price_copper_and_sale(self):
        for mirrored in (False, True):
            _, t, cfg, mem, nav, ledger = self.case(mirrored=mirrored)
            self.assertEqual(len(missing_front(t, mem, ledger)), 4)
            self.assertFalse(mem.wall_hits)
            self.assertTrue(work(t, cfg, mem, nav, ledger, t.units[1], start=True))
            self.assertEqual(ledger.commands['1']['action'], 'collect')
            self.assertEqual(t.zones[pos(ledger.commands['1']['targetPos'][0])], 'stone')
            self.assertEqual(mem.front_rebuild[1]['stone_goal'], 4)
            self.assertEqual(ledger.notes[1]['reason'], 'front_rebuild_material')

    def test_required_stone_is_kept_while_other_ores_are_sold(self):
        data, _, _, mem, _, _ = self.case()
        data['teamOur']['roles'][1]['backpack'] += ['stone'] * 4
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertTrue(work(t, cfg, mem, nav, ledger, t.units[1], start=True))
        self.assertEqual(ledger.commands['1'], command('sell', name='copper', num=8))
        self.assertEqual(mem.stone_reserves[1], 4)

    def test_failed_sale_keeps_stage_and_stone(self):
        data, _, _, mem, _, _ = self.case()
        data['teamOur']['roles'][1]['backpack'] += ['stone'] * 4
        from test_night_economy import NightEconomyTests
        for _ in range(2):
            t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
            work(t, cfg, mem, nav, ledger, t.units[1], start=True)
            self.assertEqual(ledger.commands['1'], command('sell', name='copper', num=8))
            data['roundNo'] += 1

    def test_no_build_at_night_ready_stone_builds_at_dawn_before_old_income(self):
        data, _, _, mem, _, _ = self.case(tick=129)
        hero = data['teamOur']['roles'][1]
        hero.update(backpack=['stone'] * 4, pos=dict(x=7, y=8))
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertNotEqual(ledger.commands.get('1', {}).get('action'), 'build')
        data['roundNo'] += 1
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        work(t, cfg, mem, nav, ledger, t.units[1])
        self.assertEqual(ledger.commands['1']['action'], 'build')
        self.assertEqual(ledger.commands['1']['name'], 'wall')
        self.assertEqual(pos(ledger.commands['1']['targetPos'][0])[0], 6)

    def test_near_rear_hole_does_not_win_over_front(self):
        data, _, _, mem, _, _ = self.case(tick=5)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                   if r['roleType'] != 'wall']
        data['teamOur']['roles'][1].update(backpack=['stone'] * 4, pos=dict(x=2, y=5))
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertIn(mem.build_targets[1], missing_front(t, mem, ledger))

    def test_multi_deposit_plan_does_not_assume_ten_left_in_first_mine(self):
        data, _, _, mem, _, _ = self.case()
        data['mapInfo']['zones'][-1]['remain'] = 2
        data['mapInfo']['zones'].append(dict(neutralType='stone', pos=dict(x=9, y=10), remain=2))
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertEqual(ledger.notes[1]['conditions']['expected_units'], 2)
        self.assertEqual(mem.front_rebuild[1]['stone_goal'], 4)

    def test_blueprint_gate_and_permanent_occupied_sites_are_excluded(self):
        data, _, _, mem, _, _ = self.case()
        data['teamOur']['roles'].append(unit(900, 'rocket', 6, 8))
        data['mapInfo']['zones'].append(dict(neutralType='vendor', pos=dict(x=6, y=9)))
        mem.build_failures[(6, 5)] = 1000
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertEqual(missing_front(t, mem, ledger), [(6, 11)])

    def test_temporary_character_on_gap_does_not_reduce_material_goal(self):
        data, _, _, mem, _, _ = self.case()
        data['teamOur']['roles'].append(unit(900, 'imp', 6, 8))
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertEqual(len(missing_front(t, mem, ledger)), 4)
        self.assertFalse(act(t, cfg, mem, nav, ledger, t.units[2]))
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertEqual(mem.front_rebuild[1]['stone_goal'], 4)

    def test_sale_forecast_includes_every_wall_and_return_no_live_mutation(self):
        _, t, cfg, mem, nav, ledger = self.case(tick=5)
        sites = missing_front(t, mem, ledger)
        blocked, roles = set(t.blocked), t.ours
        plan = finish_plan(t, cfg, mem, WorkNavigator(nav, ledger), ledger,
                           t.units[1], sites, vendor=(9, 8))
        self.assertIsNotNone(plan)
        self.assertGreaterEqual(plan['construction_end'], len(sites) + 1)
        self.assertEqual(t.blocked, blocked)
        self.assertEqual(t.ours, roles)
        self.assertFalse(ledger.commands)

    def test_final_night_has_no_next_day_material_budget(self):
        data, _, _, mem, _, _ = self.case()
        data['roundNo'] = 1290
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertFalse(act(t, cfg, mem, nav, ledger, t.units[1]))

    def test_late_carried_batch_builds_without_optional_sale_detour(self):
        data, _, _, mem, _, _ = self.case(tick=65)
        data['teamOur']['roles'][1].update(backpack=['stone', 'copper'], pos=dict(x=7, y=8))
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertEqual(ledger.commands['1']['action'], 'build')

    def test_agent_daytime_preempts_saved_copper_run_before_35(self):
        data, _, _, _, _, _ = self.case(tick=5)
        from test_night_economy import NightEconomyTests
        fixture = NightEconomyTests()
        agent, mem = fixture.seed_agent(data)
        mem.night_work[1] = dict(day=3, phase='mine', mine=(12, 12), ore='copper',
                                 collect_goal=20, end_round=data['roundNo'] + 30)
        result = agent.decide(data)['roleCommandMap']
        self.assertEqual(result['1'], command('collect', (8, 9)))

    def test_zero_stone_price_still_collects_material_and_ignores_other_workers_stock(self):
        data, _, _, mem, _, _ = self.case(tick=5)
        data['vendorShopList'][-1]['price'] = 0
        data['teamOur']['roles'][2]['backpack'] += ['stone'] * 20
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertEqual(ledger.commands['1'], command('collect', (8, 9)))
        self.assertEqual(mem.front_rebuild[1]['stone_goal'], 4)

    def test_insufficient_total_stone_uses_feasible_partial_batch_before_income(self):
        data, _, _, mem, _, _ = self.case(tick=5)
        data['mapInfo']['zones'][-1]['remain'] = 2
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertEqual(ledger.commands['1'], command('collect', (8, 9)))
        self.assertEqual(mem.front_rebuild[1]['stone_goal'], 2)

    def test_cleared_front_releases_worker_back_to_ordinary_income(self):
        data, _, _, mem, _, _ = self.case(tick=5)
        data['teamOur']['roles'] += [unit(500+i, 'wall', x, y, level=3, health=2000)
                                    for i, (x, y) in enumerate(((6, 5), (6, 8), (6, 9), (6, 11)))]
        mem.front_rebuild[1] = dict(phase='build', stone_goal=4)
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertFalse(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertNotIn(1, mem.front_rebuild)
        from agent.wall_supply import other_worker
        other_worker(t, cfg, mem, nav, ledger, t.units[1], ledger.tower_cells, ledger.wall_cells)
        self.assertNotEqual(ledger.commands.get('1', {}).get('name'), 'wall')
        self.assertIn(ledger.commands['1']['action'], ('buy', 'move', 'sell', 'collect'))

    def test_material_route_respects_robot_arrival_and_actual_reserved_cells(self):
        data, _, _, mem, _, _ = self.case()
        data['robot']['roles'] = [unit(900, 'bossRobot', 8, 10, attackRange=3)]
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertFalse(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertFalse(ledger.commands)

    def test_far_sale_that_would_lose_build_window_does_not_block_carried_material(self):
        data, _, _, mem, _, _ = self.case(tick=60)
        data['teamOur']['roles'][1].update(backpack=['stone'] * 4 + ['copper'], pos=dict(x=7, y=8))
        data['mapInfo']['zones'][0]['pos'] = dict(x=14, y=14)
        from test_night_economy import NightEconomyTests
        t, cfg, mem, nav, ledger = NightEconomyTests().setup(data, mem)
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertEqual(ledger.commands['1']['action'], 'build')

    def test_default_connected_blueprint_whole_front_material_and_gate(self):
        data, _, cfg, mem, _, _ = self.case(tick=5)
        cfg.layout_mode = 'demo_inferred'
        cfg.wall_cells, cfg.weapon_cells = [], []
        data['mapInfo'].update(width=41, height=32)
        # Recreate the live battery on the slots inferred for this footprint.
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                   if r['roleType'] in ('station', 'worker', 'pioneer')]
        from test_agent import setup_case
        from agent.economy_plan import front_sites
        t, _, nav, ledger = setup_case(data, **cfg.__dict__)
        front = set(front_sites(t, ledger.wall_cells))
        data['teamOur']['roles'] += [unit(500+i, cfg.loadout[i], *p, level=3, health=2000)
                                    for i, p in enumerate(sorted(ledger.tower_cells))]
        data['teamOur']['roles'] += [unit(600+i, 'wall', *p, level=2, health=1500)
                                    for i, p in enumerate(sorted(ledger.wall_cells - front))]
        t, _, nav, ledger = setup_case(data, **cfg.__dict__)
        self.assertTrue(act(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertEqual(mem.front_rebuild[1]['stone_goal'], len(front))
        self.assertEqual(ledger.commands['1'], command('collect', (8, 9)))


if __name__ == '__main__':
    unittest.main()
