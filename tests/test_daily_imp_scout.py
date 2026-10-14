"""Daily offense handoff, safe shared sight and original imp duty boundaries."""
import copy
from time import monotonic
import unittest
from unittest.mock import patch

from test_agent import setup_case, unit
import test_daily_pioneer_offense as daily_fixtures
from test_gate_guard import gate_case, guard_setup
from test_imp_sabotage import imp_payload
from agent.brain import Agent, observe_robot_summons
from agent.commands import command
from agent.config import Config
from agent.daily_offense import prepare_daily_pioneer
from agent.gate_guard import prepare_gate_guard
from agent.intelligence import Intelligence, Memory
from agent.model import distance, neighbours, pos
from agent.navigation import layout
from agent.projectiles import line_cells
from agent.robot_assault import base_distance, enemy_base
from agent.sabotage import act_imp_scout, act_imps, exposed, sync_imp_scout, threats


def daily_case(day=2, tick=50, gold=600, origin=0, point=(14, 4)):
    data, cfg = daily_fixtures.DailyPioneerOffenseTests().data(day, tick, gold, origin)
    data['teamOur']['roles'].append(unit(14, 'imp', *point, backPackCapability=0))
    return data, cfg


def scout_case(day=2, tick=60, point=(39, 5), mirror=False):
    data = imp_payload((day - 1) * 130 + tick, point=point)
    data['mapInfo']['zones'] = []
    data['teamOur']['roles'].append(unit(11, 'pioneer', 10, 20))
    data['teamEnemy']['roles'] = [unit(99, 'station', 35, 5, health=1500)]
    if mirror:
        for team in ('teamOur', 'teamEnemy'):
            for role in data[team]['roles']:
                x, y = pos(role['pos'])
                role['pos'] = dict(x=39-x if role['roleType'] == 'station' else 40-x,
                                   y=32-y if role['roleType'] == 'station' else 31-y)
        data['teamOur']['type'] = 'defender'
    return data


def prepare_scout(data, mem=None):
    turn, cfg, nav, ledger = setup_case(data, llm_enabled=False, layout_mode='explicit')
    mem = mem or Memory()
    mem.observe(turn, cfg)
    nav.memory = mem.movement
    return turn, cfg, nav, ledger, mem


def planned(day=2):
    return Memory(daily_pioneer_offense=dict(day=day, phase='shop', attack_day=day))


class DailyImpAssignmentTests(unittest.TestCase):
    def test_each_later_day_can_confirm_its_own_new_plan(self):
        for day in range(2, 11):
            with self.subTest(day=day):
                data, cfg = daily_case(day=day)
                turn, _, nav, ledger, mem = prepare_scout(data)
                prepare_daily_pioneer(turn, cfg, mem, nav, ledger)
                self.assertTrue(sync_imp_scout(turn, mem))
                self.assertEqual(mem.sabotage.scout['day'], day)

    def test_checked_trip_enables_sight_in_the_same_agent_response(self):
        for origin in (0, 1):
            data, cfg = daily_case(origin=origin)
            agent = Agent(cfg)
            response = agent.decide(copy.deepcopy(data))
            mem = next(iter(agent.sessions.values()))
            self.assertEqual(response['roleCommandMap']['2'], command('buy', name='BossRobotSummonOrder', num=5))
            self.assertEqual(mem.sabotage.scout['day'], 2)
            self.assertEqual(mem.sabotage.scout['hero_id'], 14)
            self.assertIsNotNone(mem.sabotage.scout['post'])
            self.assertFalse(mem.sabotage.gate_states)
            self.assertEqual(agent.decide(copy.deepcopy(data)), response)

    def test_waiting_task_and_unchecked_state_do_not_enable_scout(self):
        for phase in ('task', 'check', 'shop', 'robots'):
            data, _ = daily_case()
            turn, _, _, _, mem = prepare_scout(data, Memory(daily_pioneer_offense=dict(day=2, phase=phase)))
            self.assertFalse(sync_imp_scout(turn, mem))
        data, cfg = daily_case()
        data['phaseTask'] = 'finish the current task first'
        data['teamOur']['playerTasks'][0].update(coldDownRounds=30, timeoutRounds=40)
        agent = Agent(cfg)
        with patch.object(Intelligence, 'task', return_value=('', '')):
            agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.daily_pioneer_offense['phase'], 'task')
        self.assertFalse(mem.sabotage.scout)

    def test_no_budget_and_carried_order_alone_do_not_enable_scout(self):
        data, cfg = daily_case(gold=599)
        data['teamOur']['roles'][2]['backpack'] = ['BossRobotSummonOrder']
        turn, _, nav, ledger, mem = prepare_scout(data)
        prepare_daily_pioneer(turn, cfg, mem, nav, ledger)
        self.assertEqual(mem.daily_pioneer_offense['phase'], 'skipped')
        self.assertFalse(sync_imp_scout(turn, mem))

    def test_day_one_still_uses_the_original_imp_mine_action(self):
        for day in (1, 2):
            data = imp_payload((day - 1) * 130 + 10)
            data['teamEnemy']['roles'] = [unit(99, 'station', 35, 5)]
            turn, _, nav, ledger, mem = prepare_scout(data, planned(day))
            if day == 2:
                mem.daily_pioneer_offense.clear()
            self.assertFalse(sync_imp_scout(turn, mem))
            act_imps(turn, mem.sabotage, nav, ledger)
            self.assertEqual(ledger.commands['14'], command('destroy', (28, 12)))

    def test_scout_clears_the_gate_and_an_unfinished_destroy_channel(self):
        turn, _, _, _, mem = prepare_scout(scout_case(), planned())
        mem.sabotage.gate_states[14] = dict(day=2, recalling=True)
        mem.sabotage.targets[14] = ((28, 12), 'copper')
        mem.sabotage.progress[14] = 3
        self.assertTrue(sync_imp_scout(turn, mem))
        self.assertFalse(mem.sabotage.gate_states)
        self.assertFalse(mem.sabotage.targets)
        self.assertFalse(mem.sabotage.progress)

    def test_assignment_continues_through_night_and_expires_next_day(self):
        mem = planned()
        for tick in (60, 70, 129):
            turn, _, nav, ledger, mem = prepare_scout(scout_case(tick=tick), mem)
            self.assertTrue(sync_imp_scout(turn, mem))
            act_imp_scout(turn, mem.sabotage, nav, ledger)
            self.assertIn(14, ledger.used)
            self.assertTrue(ledger.notes[14]['reason'].startswith('imp_scout'))
        turn, _, _, _, mem = prepare_scout(scout_case(day=3, tick=0), mem)
        self.assertFalse(sync_imp_scout(turn, mem))
        self.assertFalse(mem.sabotage.scout)

    def test_cancelled_plan_releases_only_uncommitted_scout(self):
        for count in (0, 1):
            turn, _, _, _, mem = prepare_scout(scout_case(), planned())
            self.assertTrue(sync_imp_scout(turn, mem))
            mem.daily_pioneer_offense['phase'] = 'skipped'
            mem.robot_summon_state = dict(day=2, count=count)
            self.assertEqual(sync_imp_scout(turn, mem), bool(count))

    def test_cross_day_paid_work_requires_a_new_use_or_check(self):
        data, cfg = daily_case(day=3, tick=1)
        data['teamOur']['roles'][2]['backpack'] = ['BossRobotSummonOrder']
        mem = Memory(daily_pioneer_offense=dict(day=2, phase='robots', attack_day=2,
            remaining=1, pending_buy=None, pending_use=None, anchor=None))
        turn, _, nav, ledger, mem = prepare_scout(data, mem)
        self.assertFalse(sync_imp_scout(turn, mem))
        prepare_daily_pioneer(turn, cfg, mem, nav, ledger)
        self.assertEqual(ledger.commands['2']['action'], 'use')
        self.assertTrue(sync_imp_scout(turn, mem))
        self.assertEqual(mem.sabotage.scout['day'], 3)

    def test_rejected_only_summon_does_not_create_a_plan(self):
        data = scout_case(tick=61)
        data['lastRoundRoleActionResults'] = {'11': False}
        mem = Memory()
        mem.robot_summon_state = dict(day=2, count=1, positions={(39, 8)},
            pending=(190, 11, (39, 8)), observed_round=190)
        turn, _, _, _, mem = prepare_scout(data, mem)
        observe_robot_summons(turn, mem)
        self.assertFalse(sync_imp_scout(turn, mem))

    def test_gate_recall_is_cancelled_on_a_new_same_turn_plan(self):
        data = gate_case(130+68, imp=(10, 2))
        data['robot']['roles'] = []
        data['mapInfo']['zones'] = [dict(neutralType='weaponShop', pos=dict(x=10, y=10))]
        data['teamOur']['goldNum'] = 600
        data['teamEnemy']['roles'] = [unit(99, 'station', 11, 4)]
        data['weaponShopList'] = [dict(name='BossRobotSummonOrder', price=120)]
        cfg = Config(llm_enabled=False, return_margin=0, robot_purchase_last_day=1)
        _, _, _, _, guard = guard_setup(copy.deepcopy(data))
        self.assertIsNotNone(guard)
        agent = Agent(cfg)
        with patch('agent.gate_guard.NightWallGuard.finish') as finish:
            response = agent.decide(data)
        finish.assert_not_called()
        mem = next(iter(agent.sessions.values()))
        self.assertTrue(mem.sabotage.scout)
        self.assertFalse(mem.sabotage.gate_states)
        move = response['roleCommandMap'].get('14')
        if move:
            self.assertEqual(move['action'], 'move')

    def test_agent_restores_the_guard_when_the_uncommitted_trip_is_cancelled(self):
        data = gate_case(130+68, imp=(10, 2))
        data['robot']['roles'] = []
        data['mapInfo']['zones'] = [dict(neutralType='weaponShop', pos=dict(x=10, y=10))]
        data['teamOur']['goldNum'] = 600
        data['teamEnemy']['roles'] = [unit(99, 'station', 11, 4)]
        data['weaponShopList'] = [dict(name='BossRobotSummonOrder', price=120)]
        agent = Agent(Config(llm_enabled=False, return_margin=0, robot_purchase_last_day=1))
        agent.decide(copy.deepcopy(data))
        mem = next(iter(agent.sessions.values()))
        self.assertTrue(mem.sabotage.scout)
        data['roundNo'] += 1
        data['teamOur']['goldNum'] = 0
        agent.decide(data)
        self.assertFalse(mem.sabotage.scout)
        self.assertTrue(mem.sabotage.gate_states[14]['recalling'])


class ImpScoutSightTests(unittest.TestCase):
    def act(self, data, mem=None, configure=None):
        turn, cfg, nav, ledger, mem = prepare_scout(data, mem or planned())
        self.assertTrue(sync_imp_scout(turn, mem))
        if configure:
            configure(turn, nav, ledger, mem)
        act_imp_scout(turn, mem.sabotage, nav, ledger)
        return turn, nav, ledger, mem

    def test_safe_stationary_posts_cover_all_base_cells_in_both_sides(self):
        for mirror in (False, True):
            for tick in (60, 80):
                turn, _, ledger, mem = self.act(scout_case(tick=tick, mirror=mirror))
                post = mem.sabotage.scout['post']
                self.assertEqual(post, turn.imps[0].pos)
                self.assertTrue(all(distance(post, cell) <= 4 for cell in enemy_base(turn).cells))
                self.assertNotIn('14', ledger.commands)
                self.assertEqual(ledger.notes[14]['reason'], 'imp_scout_hold')

    def test_approach_uses_legal_movement_and_does_not_destroy_a_nearby_mine(self):
        data = scout_case(point=(27, 12))
        data['mapInfo']['zones'].append(dict(neutralType='copper', pos=dict(x=28, y=12), remain=10))
        turn, _, ledger, mem = self.act(data)
        self.assertEqual(ledger.commands['14']['action'], 'move')
        landing = pos(ledger.commands['14']['targetPos'][0])
        self.assertEqual(distance(turn.imps[0].pos, landing), 1)
        self.assertNotIn(landing, turn.blocked)
        self.assertFalse(mem.sabotage.targets)

    def test_catch_risk_has_priority_and_retains_the_scout_role(self):
        data = scout_case(point=(39, 5))
        data['teamEnemy']['roles'].append(unit(50, 'worker', 38, 5))
        turn, _, ledger, mem = self.act(data)
        self.assertEqual(ledger.notes[14]['reason'], 'imp_escape')
        landing = pos(ledger.commands['14']['targetPos'][0])
        # At the edge, no single step can leave the predictive catch envelope;
        # the legal escape must still increase clearance and keep its role.
        self.assertGreater(distance(landing, (38, 5)), distance(turn.imps[0].pos, (38, 5)))
        self.assertEqual(mem.sabotage.scout['hero_id'], 14)

    def test_own_robot_is_not_a_threat_but_unknown_ownership_still_is(self):
        for own in (False, True):
            data = scout_case(tick=80)
            robot = unit(300, 'bossRobot', 40, 8, targetTeam='defender', attackRange=3)
            data['robot']['roles'] = [robot]
            if own:
                data['teamOur']['summonRobotList'] = [copy.deepcopy(robot)]
            _, _, ledger, _ = self.act(data)
            self.assertEqual(ledger.notes[14]['reason'].startswith('imp_escape'), not own)
            if own:
                self.assertEqual(ledger.notes[14]['reason'], 'imp_scout_hold')

    def test_accepted_robot_move_and_attack_lanes_are_avoided(self):
        for action in ('move', 'attack'):
            data = scout_case(tick=80, point=(38, 4))
            robot = unit(300, 'bossRobot', 39, 4, targetTeam='defender', attackRange=3)
            data['robot']['roles'] = [copy.deepcopy(robot)]
            data['teamOur']['summonRobotList'] = [robot]
            target = (38, 4) if action == 'move' else (36, 4)
            def configure(turn, nav, ledger, mem):
                # A move blocked by the imp is an assault approach to clear,
                # not a claim that the official engine executed the move.
                ledger.commands['300'] = command(action, target)
            turn, _, ledger, _ = self.act(data, configure=configure)
            self.assertEqual(ledger.commands['14']['action'], 'move')
            landing = pos(ledger.commands['14']['targetPos'][0])
            self.assertNotIn(landing, line_cells(pos(robot['pos']), target))
            self.assertNotEqual(landing, target)

    def test_reserved_summon_position_is_vacated(self):
        data = scout_case()
        def configure(turn, nav, ledger, mem):
            ledger.summon_pending_positions.add(turn.imps[0].pos)
        turn, _, ledger, _ = self.act(data, configure=configure)
        self.assertEqual(ledger.commands['14']['action'], 'move')
        self.assertNotEqual(pos(ledger.commands['14']['targetPos'][0]), turn.imps[0].pos)

    def test_scout_is_not_taken_by_night_wall_gap_duty(self):
        data = gate_case(210)
        data['teamEnemy']['roles'] = [unit(99, 'station', 11, 4)]
        turn, cfg, nav, ledger, mem = prepare_scout(data, planned())
        self.assertTrue(sync_imp_scout(turn, mem))
        towers, walls = layout(turn, cfg)
        self.assertIsNone(prepare_gate_guard(turn, cfg, mem, nav, ledger, towers, walls))
        self.assertNotIn(14, ledger.used)

    def test_no_safe_post_waits_without_falling_back_to_mining_or_guard(self):
        data = scout_case()
        def configure(turn, nav, ledger, mem):
            ledger.reserved.update((x, y) for x in range(turn.width) for y in range(turn.height))
        _, _, ledger, mem = self.act(data, configure=configure)
        self.assertEqual(ledger.notes[14]['reason'], 'imp_scout_route_blocked')
        self.assertNotIn('14', ledger.commands)
        self.assertTrue(mem.sabotage.scout)

    def test_expired_budget_still_clears_an_attack_lane(self):
        data = scout_case(tick=80, point=(38, 4))
        robot = unit(300, 'bossRobot', 39, 4, targetTeam='defender', attackRange=3)
        data['teamOur']['summonRobotList'] = [robot]
        def configure(turn, nav, ledger, mem):
            nav.deadline = monotonic() - 1
            ledger.commands['300'] = command('attack', (36, 4))
        _, _, ledger, _ = self.act(data, configure=configure)
        self.assertEqual(ledger.notes[14]['reason'], 'imp_scout_yield')
        self.assertNotIn(pos(ledger.commands['14']['targetPos'][0]), line_cells((39, 4), (36, 4)))

    def test_dead_imp_or_missing_enemy_base_releases_the_assignment(self):
        for missing in ('imp', 'base'):
            data = scout_case()
            mem = planned()
            turn, _, _, _, mem = prepare_scout(data, mem)
            self.assertTrue(sync_imp_scout(turn, mem))
            if missing == 'imp':
                next(r for r in data['teamOur']['roles'] if r['roleType'] == 'imp')['health'] = 0
            else:
                data['teamEnemy']['roles'] = []
            turn, _, _, _, mem = prepare_scout(data, mem)
            self.assertFalse(sync_imp_scout(turn, mem))
            self.assertFalse(mem.sabotage.scout)

    def test_known_turret_coverage_guides_the_safe_post(self):
        data = scout_case()
        data['teamEnemy']['roles'].append(unit(50, 'rocket', 33, 4))
        _, _, _, mem = self.act(data)
        self.assertLessEqual(distance(mem.sabotage.scout['post'], (33, 4)), 4)

    def test_dizzy_scout_holds_its_role_without_an_illegal_move(self):
        data = scout_case(tick=80)
        next(r for r in data['teamOur']['roles'] if r['roleType'] == 'imp')['abnormalState'] = 'dizzy'
        _, _, ledger, mem = self.act(data)
        self.assertNotIn('14', ledger.commands)
        self.assertEqual(ledger.notes[14]['reason'], 'imp_scout_dizzy')
        self.assertEqual(mem.sabotage.scout['hero_id'], 14)
