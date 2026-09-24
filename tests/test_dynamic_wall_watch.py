"""Adaptive stock, shared risk thresholds and observable repair outcomes."""
import unittest
from unittest.mock import patch
from dataclasses import replace
from test_agent import payload, unit, setup_case
import test_wall_watch as fixtures
from agent.brain import Agent
from agent.config import Config
from agent.commands import command
from agent.economy import use_inventory
from agent.intelligence import Memory
from agent.mining import reserve_watch_space
from agent.model import Turn
from agent.navigation import layout
from agent.wall_health import repair_risk
from agent.wall_watch import prepare_watch, repair_watch
from agent.wall_watch_state import NightRecord


def case(**kw):
    return fixtures.WallWatchTests().case(**kw)


class DynamicWallWatchTests(unittest.TestCase):
    def risk_case(self, day=8, health=199, level=1, robots=1):
        p = payload((day - 1) * 130 + 80, [
            unit(1, 'worker', 5, 5, health=220, backpack=['WallFixer'] * 3),
            unit(30, 'wall', 6, 5, level=level, health=health)])
        p['robot']['roles'] = [unit(90+i, 'bossRobot', 8, 5+i, attackPower=40,
                                    attackRange=3, targetTeam='challenger') for i in range(robots)]
        return p

    def test_late_pressure_twenty_percent_boundary_for_all_levels(self):
        for level, maximum in ((1, 1000), (2, 1500), (3, 2000)):
            for hp in (maximum, maximum - 1, maximum // 4 + 1, maximum // 4, maximum // 4 - 1):
                with self.subTest(level=level, hp=hp):
                    t, _, nav, ledger = setup_case(self.risk_case(health=hp, level=level))
                    risk = repair_risk(t, t.units[30])
                    self.assertEqual(risk.threshold, maximum // 4)
                    self.assertEqual(risk.reason, 'late_25pct')
                    self.assertEqual(use_inventory(t, nav, ledger, t.workers[0], mem=Memory()), hp < maximum // 4)

    def test_early_single_attacker_and_late_weak_attacker_keep_ten_percent(self):
        for day, power in ((7, 40), (10, 5)):
            p = self.risk_case(day=day, health=150)
            p['robot']['roles'][0].update(roleType='smallRobot', attackPower=power)
            t, _, _, _ = setup_case(p)
            risk = repair_risk(t, t.units[30])
            self.assertEqual((risk.threshold, risk.needed), (150, False))

    def test_early_focus_fire_lifts_threshold(self):
        t, _, _, _ = setup_case(self.risk_case(day=4, robots=3))
        risk = repair_risk(t, t.units[30])
        self.assertEqual((risk.reason, risk.threshold, risk.needed), ('focus_25pct', 250, True))

    def test_cleared_opponent_and_stunned_waves_do_not_spend(self):
        for mode in ('clear', 'opponent', 'dizzy'):
            p = self.risk_case(health=1)
            if mode == 'clear':
                p['robot']['roles'] = []
            elif mode == 'opponent':
                p['robot']['roles'][0]['targetTeam'] = 'defender'
            else:
                p['robot']['roles'][0]['abnormalState'] = 'dizzy'
            t, _, nav, ledger = setup_case(p)
            self.assertFalse(use_inventory(t, nav, ledger, t.workers[0], mem=Memory()))

    def test_observed_burst_can_override_twenty_percent_but_not_full_health(self):
        t, _, _, _ = setup_case(self.risk_case(health=300))
        mem = Memory()
        mem.wall_watch.damage[(30, 1)] = [(t.round, 260)]
        risk = repair_risk(t, t.units[30], mem)
        self.assertEqual((risk.reason, risk.threshold, risk.needed), ('observed_burst', 325, True))
        self.assertFalse(repair_risk(t, replace(t.units[30], health=1000), mem).needed)
        p = self.risk_case(health=300)
        p['robot']['roles'][0]['pos'] = {'x': 14, 'y': 14}
        t, _, _, _ = setup_case(p)
        self.assertFalse(repair_risk(t, t.units[30], mem).needed)

    def test_robot_range_overlap_alone_cannot_raise_above_twenty_percent(self):
        t, _, _, _ = setup_case(self.risk_case(health=300, robots=3))
        self.assertFalse(repair_risk(t, t.units[30]).needed)

    def observe(self, mem, p, commands=None):
        if commands is not None:
            mem.last_commands = commands
        turn = Turn(p, Config())
        mem.wall_watch.observe(turn, mem)
        return turn

    def test_damage_samples_expire_and_do_not_cross_upgrades_or_rebuilds(self):
        for mutation in ('expiry', 'upgrade', 'rebuild', 'gap'):
            p, mem = self.risk_case(health=1000), Memory()
            self.observe(mem, p)
            p['roundNo'] += 1
            p['teamOur']['roles'][1]['health'] = 740
            t = self.observe(mem, p)
            self.assertEqual(mem.wall_watch.recent_damage(t.units[30], t.round), 260)
            p['roundNo'] += 1 if mutation in ('upgrade', 'rebuild') else 3
            if mutation == 'upgrade':
                p['teamOur']['roles'][1].update(level=2, health=1500)
            elif mutation == 'rebuild':
                p['teamOur']['roles'][1].update(id=31, health=1000)
            t = self.observe(mem, p)
            wall = next(w for w in t.ours if w.kind == 'wall')
            self.assertEqual(mem.wall_watch.recent_damage(wall, t.round), 0)

    def test_repair_counts_inventory_consumption_not_just_issued_command(self):
        for legal, consumed, outcome in ((True, True, 'used'), (None, True, 'used'),
                                         (False, False, 'failed'), (True, False, 'unconfirmed')):
            p, mem = self.risk_case(), Memory()
            self.observe(mem, p)
            p['roundNo'] += 1
            if consumed:
                p['teamOur']['roles'][0]['backpack'].pop()
            p['lastRoundRoleActionResults'] = {'1': legal} if legal is not None else {}
            with self.assertLogs('agent.wall_watch_state', level='INFO') as logs:
                self.observe(mem, p, {'1': command('use', (6, 5), name='WallFixer')})
            self.assertEqual(getattr(mem.wall_watch.night, outcome), 1)
            self.assertIn('wall_repair_result', '\n'.join(logs.output))
            self.observe(mem, p)  # duplicate payload cannot double-count
            self.assertEqual(getattr(mem.wall_watch.night, outcome), 1)

    def test_last_night_use_is_settled_before_dawn_summary(self):
        p, mem = self.risk_case(day=4), Memory()
        p['roundNo'] = 3 * 130 + 129
        self.observe(mem, p)
        p['roundNo'] += 1
        p['teamOur']['roles'][0]['backpack'].pop()
        with self.assertLogs('agent.wall_watch_state', level='INFO') as logs:
            self.observe(mem, p, {'1': command('use', (6, 5), name='WallFixer')})
        self.assertEqual(mem.wall_watch.history[-1].used, 1)
        self.assertEqual(mem.wall_watch.history[-1].remaining, 2)
        self.assertIn('wall_night_summary', '\n'.join(logs.output))
        self.assertIsNone(mem.wall_watch.night)

    def test_skipped_reply_or_dead_actor_does_not_fabricate_success(self):
        for gap, dead in ((2, False), (1, True)):
            p, mem = self.risk_case(), Memory()
            self.observe(mem, p)
            p['roundNo'] += gap
            p['teamOur']['roles'][0]['backpack'].pop()
            if dead:
                p['teamOur']['roles'][0]['health'] = 0
            self.observe(mem, p, {'1': command('use', (6, 5), name='WallFixer')})
            self.assertEqual((mem.wall_watch.night.used, mem.wall_watch.night.unconfirmed), (0, 1))

    def test_successful_heal_clears_burst_history_failed_heal_does_not(self):
        for legal in (False, True):
            p, mem = self.risk_case(health=300), Memory()
            t = self.observe(mem, p)
            mem.wall_watch.damage[(30, 1)] = [(t.round, 260)]
            p['roundNo'] += 1
            p['lastRoundRoleActionResults'] = {'1': legal}
            if legal:
                p['teamOur']['roles'][0]['backpack'].pop()
                p['teamOur']['roles'][1]['health'] = 999
            t = self.observe(mem, p, {'1': command('use', (6, 5), name='WallFixer')})
            self.assertEqual(mem.wall_watch.recent_damage(t.units[30], t.round), 0 if legal else 260)

    def test_stock_baseline_grows_daily_and_is_shared_by_slot_reservation(self):
        for day in range(4, 11):
            p = case(day=day, tick=5, packs=0)
            p['teamOur']['roles'][2].update(backPackCapability=12, backpack=['copper'] * 3)
            t, _, _, _ = setup_case(p)
            mem = Memory(wall_watch_id=2)
            self.assertEqual(mem.wall_watch.stock_target(t), {4: 4, 5: 6, 6: 9, 7: 12}.get(day, 18 + 2 * (day - 8)))
            affordable = t.gold // t.shop['WallFixer']
            self.assertEqual(reserve_watch_space(t, mem, t.workers[1]).space,
                             max(0, 12 - 3 - min(12, mem.wall_watch.stock_target(t), affordable)))

    def test_late_baseline_is_eighteen_but_single_worker_capacity_limits_purchase(self):
        p = case(day=8, tick=40, packs=2, damaged=False)
        p['teamOur']['roles'][2]['backPackCapability'] = 10
        p['teamOur']['goldNum'] = 200
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=2)
        self.assertEqual(mem.wall_watch.stock_target(t), 18)
        prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
        self.assertEqual(ledger.commands['2'], {'action': 'buy', 'name': 'WallFixer', 'num': 8})
        self.assertNotIn('1', ledger.commands)

    def test_early_exhaustion_adds_more_than_late_exhaustion(self):
        t, _, _, _ = setup_case(case(day=5, tick=5))
        targets = []
        for tick in (80, 125):
            mem = Memory()
            mem.wall_watch.history.append(NightRecord(4, 3, 70, {}, used=3,
                                                       empty_tick=tick, unmet={30}))
            targets.append(mem.wall_watch.stock_target(t))
        self.assertEqual(targets, [10, 6])

    def test_stock_target_caps_and_surplus_decays_without_midday_oscillation(self):
        t, _, _, _ = setup_case(case(day=5, tick=5))
        mem = Memory()
        mem.wall_watch.history.append(NightRecord(4, 3, 70, {}, used=30, empty_tick=80, unmet={30}))
        self.assertEqual(mem.wall_watch.stock_target(t), 60)
        mem = Memory()
        record = NightRecord(4, 8, 70, {}, used=2, remaining=6)
        mem.wall_watch.history.append(record)
        self.assertEqual(mem.wall_watch.stock_target(t), 7)
        record.used = 20
        self.assertEqual(mem.wall_watch.stock_target(t), 7)

    def test_shortage_tracks_unique_walls_and_separates_unreachable(self):
        p, mem = self.risk_case(day=4), Memory()
        t = self.observe(mem, p)
        for _ in range(20):
            mem.wall_watch.shortage(t, t.units[30], 'no_pack')
        mem.wall_watch.shortage(t, t.units[30], 'no_route')
        self.assertEqual(mem.wall_watch.night.unmet, {30})
        self.assertEqual(mem.wall_watch.night.unreachable, {30})

    def test_buys_only_difference_and_preserves_firepower_budget(self):
        p = case(day=10, tick=40, packs=1, damaged=False)
        p['teamOur']['goldNum'] = 150
        p['weaponShopList'].append({'name': 'WeaponUpgradeVoucher1', 'price': 100})
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(wall_watch_id=2, gunner_post=(4, 11))
        locked, reserved = prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
        self.assertTrue(locked)
        self.assertEqual((ledger.commands['2']['num'], ledger.gold, reserved), (5, 100, 0))

    def test_minimum_stock_survives_small_budget_and_capacity_limits(self):
        for gold, capacity, expected in ((15, 100, 1), (75, 2, 2), (75, 100, 3)):
            p = case(day=10, tick=40, packs=0)
            p['teamOur']['goldNum'] = gold
            p['teamOur']['roles'][2]['backPackCapability'] = capacity
            p['weaponShopList'].append({'name': 'WeaponUpgradeVoucher1', 'price': 100})
            t, cfg, nav, ledger = setup_case(p)
            prepare_watch(t, cfg, Memory(), nav, ledger, t.workers[1], layout(t, cfg)[1])
            self.assertEqual(ledger.commands['2']['num'], expected)

    def test_previous_stockout_raises_safety_stock_before_optional_upgrade_budget(self):
        p = case(day=6, tick=40, packs=0)
        p['weaponShopList'].append({'name': 'WeaponUpgradeVoucher1', 'price': 100})
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        mem.wall_watch.history.append(NightRecord(5, 5, 70, {}, used=5, empty_tick=90, unmet={30}))
        prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
        self.assertEqual(ledger.commands['2']['num'], 7)
        self.assertEqual(ledger.gold, 5)

    def test_wall_loss_is_logged_once_and_deliberate_removal_is_not_combat_loss(self):
        p, mem = self.risk_case(), Memory()
        self.observe(mem, p)
        p['roundNo'] += 1
        p['teamOur']['roles'][1]['health'] = 0
        with self.assertLogs('agent.wall_watch_state', level='INFO') as logs:
            self.observe(mem, p)
        self.assertIn('wall_loss', '\n'.join(logs.output))
        self.assertEqual(mem.wall_watch.night.lost, {30})
        p, mem = self.risk_case(), Memory()
        self.observe(mem, p)
        p['roundNo'] += 1
        p['teamOur']['roles'][1]['health'] = 0
        self.observe(mem, p, {'1': command('remove', (6, 5))})
        self.assertFalse(mem.wall_watch.night.lost)

    def test_preposition_does_not_spend_and_late_watch_does_not_mine(self):
        for day, health, stunned in ((7, 250, False), (8, 1000, False), (8, 1000, True)):
            p = case(day=day, damaged=False)
            for r in p['teamOur']['roles']:
                if r['roleType'] == 'wall':
                    r['health'] = health
            p['robot']['roles'][0].update(roleType='bossRobot', attackPower=40, attackRange=3)
            if stunned:
                p['robot']['roles'][0]['abnormalState'] = 'dizzy'
            agent = Agent(Config(llm_enabled=False))
            result = agent.decide(p)['roleCommandMap']
            self.assertNotEqual(result.get('2', {}).get('name'), 'WallFixer')
            self.assertNotEqual(result.get('2', {}).get('action'), 'collect')
            self.assertTrue(any(c['action'] == 'attack' for c in result.values()))

    def test_urgent_unreachable_wall_is_logged_without_outside_mining(self):
        p = case()
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        with patch('agent.wall_watch.watch_route', return_value=None):
            with self.assertLogs('agent.wall_watch_state', level='INFO') as logs:
                self.assertTrue(repair_watch(t, mem, nav, ledger, t.workers[1], layout(t, cfg)[1]))
        self.assertIn('no_route', '\n'.join(logs.output))
        self.assertFalse(ledger.commands)

    def test_agent_logs_attempt_status_and_identical_requests_are_idempotent(self):
        p = case()
        agent = Agent(Config(llm_enabled=False))
        with self.assertLogs('agent.wall_watch_state', level='INFO') as logs:
            first = agent.decide(p)
        output = '\n'.join(logs.output)
        self.assertIn('wall_stock_plan', output)
        self.assertIn('wall_repair_attempt', output)
        self.assertIn('wall_watch_status', output)
        self.assertEqual(agent.decide(p), first)
        self.assertEqual(next(iter(agent.sessions.values())).wall_watch.night.used, 0)

    def test_sixty_turn_controlled_replay_balances_packages_and_survival(self):
        # Isolate timing policy with one reachable wall and fixed per-turn
        # damage. This is not a prediction of live robot target selection.
        def replay(mode, damage, starting_health):
            p = self.risk_case(day=4, health=starting_health)
            p['roundNo'] = 3 * 130 + 70
            p['teamOur']['roles'][0]['backpack'] = ['WallFixer'] * 60
            p['robot']['roles'][0]['attackPower'] = damage
            mem, used = Memory(), 0
            wall = p['teamOur']['roles'][1]
            for _ in range(60):
                t = self.observe(mem, p)
                if mode == 'adaptive':
                    heal = repair_risk(t, t.units[30], mem).needed
                else:
                    heal = wall['health'] < (100 if mode == 'fixed10' else 200)
                mem.last_commands = {}
                if heal:
                    used += 1
                    p['teamOur']['roles'][0]['backpack'].pop()
                    wall['health'] = 1000
                    mem.last_commands = {'1': command('use', (6, 5), name='WallFixer')}
                wall['health'] -= damage
                if wall['health'] <= 0:
                    return used, False
                p['roundNo'] += 1
            return used, True

        adaptive = replay('adaptive', 35, 450)
        early = replay('fixed20', 35, 450)
        self.assertEqual(adaptive, (3, True))
        self.assertEqual(early, (3, True))
        self.assertFalse(replay('fixed10', 280, 1000)[1])
        self.assertTrue(replay('adaptive', 280, 1000)[1])

    def test_burst_threatened_flank_precedes_nonlethal_front(self):
        p = case(day=8, damaged=False)
        front = next(w for w in p['teamOur']['roles'] if w['roleType'] == 'wall'
                     and w['pos'] == {'x': 8, 'y': 11})
        flank = next(w for w in p['teamOur']['roles'] if w['roleType'] == 'wall'
                     and w['pos'] == {'x': 7, 'y': 13})
        front['health'], flank['health'] = 199, 50
        p['teamOur']['roles'][2]['pos'] = {'x': 7, 'y': 12}
        p['robot']['roles'][0].update(pos={'x': 9, 'y': 13}, attackPower=80, attackRange=3)
        t, cfg, nav, ledger = setup_case(p)
        self.assertTrue(repair_watch(t, Memory(), nav, ledger, t.workers[1], layout(t, cfg)[1]))
        self.assertEqual(ledger.commands['2']['targetPos'][0], flank['pos'])

    def test_final_night_emits_last_tick_status_without_needing_next_day(self):
        p = case(day=10, tick=129)
        with self.assertLogs('agent.wall_watch_state', level='INFO') as logs:
            Agent(Config(llm_enabled=False)).decide(p)
        output = '\n'.join(logs.output)
        self.assertIn('wall_watch_status', output)
        self.assertIn('"final_tick":true', output)
