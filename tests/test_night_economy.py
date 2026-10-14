"""Finite requests for night release, delivery, trade and dawn continuity."""
import copy
import unittest

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, pos
from agent.night_economy import (WorkNavigator, hostiles, rockets_sufficient,
                                  release_gatling, work, resume_morning)


class NightEconomyTests(unittest.TestCase):
    sites = [[6, 8], [6, 9], [1, 5], [1, 11], [6, 5], [6, 11]]

    def config(self):
        return Config(layout_mode='explicit', weapon_cells=[[2, 10], [3, 10], [2, 6]],
                      wall_cells=self.sites, llm_enabled=False)

    def case(self, tick=110, day=4, gold=300):
        data = payload((day - 1) * 130 + tick, [
            unit(13, 'station', 3, 8, health=1500),
            unit(1, 'worker', 8, 8, health=500),
            unit(2, 'worker', 5, 8, health=500, backpack=['WallFixer'] * 4),
            unit(11, 'pioneer', 2, 9, health=500),
            unit(20, 'rocket', 2, 10, level=3, attackRange=30),
            unit(21, 'rocket', 3, 10, level=3, attackRange=30),
            unit(22, 'gatling', 2, 6, level=1, attackRange=7)])
        data['teamOur']['goldNum'] = gold
        data['teamOur']['roles'] += [unit(100+i, 'wall', *p, level=3, health=2000)
                                    for i, p in enumerate(self.sites)]
        data['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y), remain=10)
            for k, x, y in [('vendor', 9, 8), ('weaponShop', 10, 7),
                             ('copper', 12, 12), ('iron', 11, 10)]]
        data['weaponShopList'] = [dict(name=n, price=p) for n, p in [
            ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
            ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30), ('WallFixer', 15)]]
        data['vendorShopList'] = [dict(name=n, price=p) for n, p in [('copper', 5), ('iron', 3), ('stone', 1)]]
        return data

    def setup(self, data, mem=None):
        cfg = self.config()
        turn, _, nav, ledger = setup_case(data, **cfg.__dict__)
        mem = mem or Memory(day=turn.day, gatling_operator_id=1, wall_watch_id=2,
                            gunner_id=11, gunner_post=(2, 9))
        return turn, cfg, mem, nav, ledger

    def seed_agent(self, data):
        agent = Agent(self.config())
        turn = Turn(data, agent.cfg)
        mem = Memory(day=turn.day, gatling_operator_id=1, wall_watch_id=2,
                     gunner_id=11, gunner_post=(2, 9))
        agent.sessions[(*turn.key, turn.station.pos)] = mem
        return agent, mem

    def test_stocked_watcher_uses_matching_coupon_before_wait_or_fixer(self):
        data = self.case()
        data['teamOur']['roles'][2]['backpack'] += ['WallUpgradeVoucher1']
        data['teamOur']['roles'][7].update(level=1, health=100)
        agent, mem = self.seed_agent(data)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['2'], command('use', (6, 8), name='WallUpgradeVoucher1'))
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (1, 2))
        self.assertFalse(any(c.get('name') == 'WallFixer' for c in commands.values()))

    def test_two_observed_levels_deliver_consecutively_inside(self):
        data = self.case()
        hero, wall = data['teamOur']['roles'][2], data['teamOur']['roles'][7]
        hero['backpack'] += ['WallUpgradeVoucher1', 'WallUpgradeVoucher2']
        wall.update(level=1, health=100)
        agent, _ = self.seed_agent(data)
        self.assertEqual(agent.decide(data)['roleCommandMap']['2']['name'], 'WallUpgradeVoucher1')
        data['roundNo'] += 1
        hero['backpack'].remove('WallUpgradeVoucher1')
        wall.update(level=2, health=1500)
        self.assertEqual(agent.decide(data)['roleCommandMap']['2']['name'], 'WallUpgradeVoucher2')

    def test_failed_use_retries_correct_tier_instead_of_advancing(self):
        data = self.case()
        data['teamOur']['roles'][2]['backpack'] += ['WallUpgradeVoucher1', 'WallUpgradeVoucher2']
        data['teamOur']['roles'][7].update(level=1, health=100)
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[2], start=True)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'2': False}
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        work(t, cfg, mem, nav, ledger, t.units[2], start=True)
        self.assertEqual(ledger.commands['2']['name'], 'WallUpgradeVoucher1')

    def test_wall_delivery_never_uses_an_outside_shortcut(self):
        data = self.case()
        data['teamOur']['roles'][2]['backpack'] += ['WallUpgradeVoucher1']
        data['teamOur']['roles'][7].update(level=1, health=100)
        # Occupy every inner tile adjacent to this wall. Outer tiles are free.
        data['teamOur']['roles'] += [unit(200+i, 'imp', 5, y) for i, y in enumerate((7, 8, 9))]
        data['teamOur']['roles'][2]['pos'] = dict(x=2, y=8)
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[2], start=True)
        self.assertNotEqual(ledger.notes[2]['reason'], 'night_wall_delivery')

    def test_live_or_stunned_hostiles_keep_watch_repair(self):
        for state in ('', 'dizzy'):
            data = self.case()
            data['robot']['roles'] = [unit(90, 'bossRobot', 7, 8, health=800,
                attackRange=3, targetTeam='challenger', abnormalState=state)]
            data['teamOur']['roles'][7].update(level=1, health=100)
            data['teamOur']['roles'][2]['backpack'] += ['WallUpgradeVoucher1']
            agent, _ = self.seed_agent(data)
            commands = agent.decide(data)['roleCommandMap']
            self.assertNotEqual(commands.get('2', {}).get('action'), 'sell')

    def test_gatling_sells_existing_load_before_shopping_or_more_mining(self):
        data = self.case()
        data['teamOur']['roles'][1]['backpack'] = ['copper'] * 8
        t, cfg, mem, nav, ledger = self.setup(data)
        self.assertTrue(work(t, cfg, mem, nav, ledger, t.units[1], start=True))
        self.assertEqual(ledger.commands['1'], command('sell', name='copper', num=8))
        self.assertEqual(ledger.gold, data['teamOur']['goldNum'])

    def test_mixed_ores_and_failed_sale_remain_at_vendor(self):
        data = self.case()
        hero = data['teamOur']['roles'][1]
        hero['backpack'] = ['copper'] * 8 + ['iron'] * 2
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        for failed in (True, False):
            data['roundNo'] += 1
            if not failed:
                hero['backpack'] = ['iron'] * 2
            t, cfg, mem, nav, ledger = self.setup(data, mem)
            work(t, cfg, mem, nav, ledger, t.units[1])
            self.assertEqual(ledger.commands['1']['action'], 'sell')
            self.assertEqual(ledger.commands['1']['name'], 'copper' if failed else 'iron')

    def test_shop_buys_own_weapon_coupon_not_watch_items(self):
        data = self.case()
        data['teamOur']['roles'][1]['pos'] = dict(x=9, y=7)
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertEqual(ledger.commands['1'], command('buy', name='WeaponUpgradeVoucher1', num=1))
        self.assertEqual(ledger.gold, 200)

    def test_same_item_purchase_is_batched_and_existing_inventory_deducted(self):
        data = self.case(gold=700)
        data['teamOur']['roles'][1]['pos'] = dict(x=9, y=7)
        for role in data['teamOur']['roles'][4:7]:
            role.update(roleType='gatling', level=1)
        data['teamOur']['roles'][2]['backpack'].append('WeaponUpgradeVoucher1')
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertEqual(ledger.commands['1'], command('buy', name='WeaponUpgradeVoucher1', num=2))

    def test_watcher_reserve_and_backpack_capacity_limit_purchase(self):
        data = self.case(gold=99)
        data['teamOur']['roles'][1]['pos'] = dict(x=9, y=7)
        data['teamOur']['roles'][2]['backpack'] = []
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertNotEqual(ledger.commands.get('1', {}).get('action'), 'buy')

    def test_complete_same_shop_order_before_delivery_then_no_second_visit(self):
        data = self.case(gold=700)
        data['teamOur']['roles'][1]['pos'] = dict(x=9, y=7)
        # Both coupons serve eligible Gatlings; rockets wait for their cap.
        data['teamOur']['roles'][4].update(roleType='gatling', level=2)
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertEqual(ledger.commands['1']['name'], 'WeaponUpgradeVoucher1')
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        data['teamOur']['goldNum'] -= 100
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        work(t, cfg, mem, nav, ledger, t.units[1])
        self.assertEqual(ledger.commands['1']['name'], 'WeaponUpgradeVoucher2')
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'].append('WeaponUpgradeVoucher2')
        data['teamOur']['goldNum'] -= 150
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        work(t, cfg, mem, nav, ledger, t.units[1])
        self.assertEqual(ledger.notes[1]['reason'], 'night_weapon_delivery')
        self.assertTrue(mem.night_work[1]['shopping_done'])

    def test_failed_purchase_does_not_create_imaginary_delivery(self):
        data = self.case()
        data['teamOur']['roles'][1]['pos'] = dict(x=9, y=7)
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        mem.last_round, mem.last_commands = t.round, ledger.commands
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'1': False}
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        mem.observe(t, cfg)
        work(t, cfg, mem, nav, ledger, t.units[1])
        self.assertNotEqual(ledger.notes[1]['reason'], 'night_weapon_delivery')
        self.assertFalse(any(c['action'] == 'use' for c in ledger.commands.values()))
        self.assertFalse(mem.night_work[1].get('shopping_done'))

    def test_far_mine_cannot_spend_the_next_preparation_window(self):
        data = self.case(tick=129)
        data['mapInfo'].update(width=41, height=32)
        data['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y), remain=10)
            for k, x, y in [('vendor', 9, 8), ('copper', 40, 31)]]
        data['teamOur']['roles'][6]['level'] = 3
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertNotIn(1, ledger.mine_claims)

    def test_existing_rebuild_intent_does_not_reset_dawn_income(self):
        data = self.case(tick=129)
        data['teamOur']['roles'][1]['pos'] = dict(x=12, y=11)
        data['teamOur']['roles'][6]['level'] = 3
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        data['roundNo'] += 1
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        mem.wall_rebuild_levels[(6, 8)] = 3
        resume_morning(t, cfg, mem, nav, ledger)
        self.assertEqual(ledger.commands['1']['action'], 'collect')

    def test_future_enemy_weapon_knowledge_blocks_night_route(self):
        data = self.case()
        t, cfg, mem, nav, ledger = self.setup(data)
        from agent.model import Unit
        ledger.robot_known_enemies = (Unit.load(unit(90, 'rocket', 12, 12, attackRange=4)),)
        self.assertIsNone(WorkNavigator(nav, ledger).leg(t.units[1], [(12, 11)], exact=True))
        data = self.case()
        data['teamOur']['roles'][1].update(pos=dict(x=9, y=7), backPackCapability=1, backpack=['stone'])
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertNotEqual(ledger.commands.get('1', {}).get('action'), 'buy')

    def test_supplier_keeps_wall_coupon_and_pack_ownership(self):
        data = self.case(tick=40)
        data['teamOur']['roles'][2].update(pos=dict(x=9, y=7), backpack=[])
        data['teamOur']['roles'][7].update(level=1, health=1000)
        from agent.wall_supply import prepare
        t, cfg, mem, nav, ledger = self.setup(data)
        prepare(t, cfg, mem, nav, ledger, t.units[2], ledger.wall_cells)
        self.assertEqual(ledger.commands['2']['action'], 'buy')
        self.assertIn(ledger.commands['2']['name'], ('WallFixer', 'WallUpgradeVoucher1'))

    def test_material_stone_for_gaps_is_not_sold(self):
        data = self.case()
        data['teamOur']['roles'][1]['backpack'] = ['stone'] * 3 + ['copper']
        data['teamOur']['roles'].pop(7)
        t, cfg, mem, nav, ledger = self.setup(data)
        from agent.night_economy import _sale_counts
        self.assertEqual(_sale_counts(t, cfg, mem, t.units[1], ledger), {'stone': 2, 'copper': 1})

    def test_clear_wave_releases_fixed_identity_without_nightly_recall(self):
        data = self.case()
        t, cfg, mem, nav, ledger = self.setup(data)
        pairs = [(t.units[11], t.units[20]), (t.units[1], t.units[22])]
        self.assertTrue(release_gatling(t, mem, ledger, pairs))
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['pos'] = dict(x=12, y=13)
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        self.assertTrue(release_gatling(t, mem, ledger, [(t.units[11], t.units[20])]))
        self.assertEqual(mem.gatling_operator_id, 1)

    def test_far_boss_and_stunned_boss_are_not_local_clearance(self):
        for state in ('', 'dizzy'):
            data = self.case()
            data['robot']['roles'] = [unit(90, 'bossRobot', 14, 0, health=800,
                attackRange=3, targetTeam='challenger', abnormalState=state)]
            t, cfg, mem, nav, ledger = self.setup(data)
            self.assertFalse(release_gatling(t, mem, ledger, [(t.units[11], t.units[20])]))

    def test_pioneer_can_release_gatling_for_provably_small_remaining_wave(self):
        data = self.case()
        data['robot']['roles'] = [unit(90, 'smallRobot', 14, 0, health=40,
                                      attackRange=3, targetTeam='challenger')]
        t, _, _, _, _ = self.setup(data)
        self.assertTrue(rockets_sufficient(t, [(t.units[11], t.units[20])]))
        for uid in (20, 21):
            next(r for r in data['teamOur']['roles'] if r['id'] == uid)['cooldown'] = 20
        t, _, _, _, _ = self.setup(data)
        self.assertFalse(rockets_sufficient(t, [(t.units[11], t.units[20])]))

    def test_unknown_destination_is_threat_own_summon_is_not(self):
        data = self.case()
        robot = unit(90, 'smallRobot', 14, 0, attackRange=3)
        data['robot']['roles'] = [robot]
        t, _, _, _, _ = self.setup(data)
        self.assertEqual([r.id for r in hostiles(t)], [90])
        data['teamOur']['summonRobotList'] = [robot]
        t, _, _, _, _ = self.setup(data)
        self.assertFalse(hostiles(t))

    def test_new_threat_is_emergency_and_does_not_toggle_release_again(self):
        data = self.case()
        t, cfg, mem, nav, ledger = self.setup(data)
        self.assertTrue(release_gatling(t, mem, ledger, [(t.units[11], t.units[20])]))
        data['roundNo'] += 1
        data['robot']['roles'] = [unit(90, 'bossRobot', 7, 8, health=800, attackRange=3)]
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        self.assertFalse(release_gatling(t, mem, ledger, [(t.units[11], t.units[20])]))
        data['robot']['roles'] = []
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        self.assertFalse(release_gatling(t, mem, ledger, [(t.units[11], t.units[20])]))

    def test_income_route_continues_across_dawn_and_stops_at_35(self):
        data = self.case(tick=129)
        data['teamOur']['roles'][1]['pos'] = dict(x=12, y=11)
        data['teamOur']['roles'][6]['level'] = 3
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertEqual(ledger.commands['1'], command('collect', (12, 12)))
        target = mem.night_work[1]['mine']
        end = mem.night_work[1]['end_round']
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'] = ['copper']
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        mem.observe(t, cfg)
        resume_morning(t, cfg, mem, nav, ledger)
        self.assertEqual(ledger.commands['1'], command('collect', target))
        self.assertEqual(mem.night_work[1]['end_round'], end)
        data['roundNo'] = end
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        self.assertFalse(work(t, cfg, mem, nav, ledger, t.units[1]))
        self.assertNotIn(1, mem.night_work)

    def test_last_night_never_budgets_nonexistent_day_eleven(self):
        data = self.case(day=10, tick=129)
        data['teamOur']['roles'][1]['pos'] = dict(x=12, y=11)
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        self.assertEqual(mem.night_work[1]['end_round'], 1300)
        self.assertNotEqual(ledger.commands.get('1', {}).get('action'), 'collect')

    def test_temporal_safety_checks_route_wait_and_enemy_bound_robots(self):
        data = self.case(tick=120)
        data['robot']['roles'] = [unit(90, 'smallRobot', 14, 0, attackRange=3,
                                      targetTeam='defender', abnormalState='dizzy')]
        t, _, _, nav, ledger = self.setup(data)
        work_nav = WorkNavigator(nav, ledger)
        self.assertTrue(work_nav.safe((12, 10)))
        self.assertFalse(work_nav.safe((12, 10), stay=8))
        self.assertTrue(work_nav.safe((14, 0), elapsed=10))
        self.assertIsNone(work_nav.leg(t.units[1], [(14, 1)], elapsed=5, exact=True))

    def test_occupied_and_reserved_targets_stay_illegal(self):
        t, cfg, mem, nav, ledger = self.setup(self.case())
        ledger.reserved.add((9, 9))
        self.assertIsNone(WorkNavigator(nav, ledger).leg(t.units[1], [(9, 9)], exact=True))
        self.assertIsNone(WorkNavigator(nav, ledger).leg(t.units[1], [(9, 8)], exact=True))

    def test_daylight_worker_without_night_job_keeps_old_allocator(self):
        t, cfg, mem, nav, ledger = self.setup(self.case(tick=5))
        resume_morning(t, cfg, mem, nav, ledger)
        self.assertFalse(ledger.used)
        self.assertFalse(work(t, cfg, mem, nav, ledger, t.units[1]))

    def test_first_night_does_not_start_new_job(self):
        t, cfg, mem, nav, ledger = self.setup(self.case(day=1))
        self.assertFalse(work(t, cfg, mem, nav, ledger, t.units[1], start=True))
        self.assertFalse(release_gatling(t, mem, ledger, [(t.units[11], t.units[20])]))

    def test_repeated_agent_request_does_not_duplicate_state_or_purchase(self):
        data = self.case()
        data['teamOur']['roles'][1]['pos'] = dict(x=9, y=7)
        agent, mem = self.seed_agent(data)
        result = agent.decide(data)
        state = copy.deepcopy(mem.night_work)
        self.assertEqual(agent.decide(data), result)
        self.assertEqual(mem.night_work, state)

    def test_death_discards_income_job(self):
        data = self.case()
        t, cfg, mem, nav, ledger = self.setup(data)
        work(t, cfg, mem, nav, ledger, t.units[1], start=True)
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['health'] = 0
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        mem.observe(t, cfg)
        self.assertNotIn(1, mem.night_work)
