"""Observed targeting transitions, without simulating enemy moves or damage."""
import copy
import unittest
from unittest.mock import patch

from test_agent import payload, setup_case, unit
from test_mixed_battery import mixed_case
from agent.brain import Agent
from agent.combat import defend, select_targets
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn


class GatlingFocusTests(unittest.TestCase):
    def case(self, level=1):
        data = payload(80, [unit(13, 'station', 1, 8), unit(1, 'worker', 4, 6),
                            unit(22, 'gatling', 5, 5, level=level, attackRange=7)])
        data['robot']['roles'] = [
            unit(90, 'bossRobot', 12, 5, health=100, targetTeam='challenger'),
            unit(91, 'largeRobot', 5, 2, health=80, targetTeam='challenger')]
        return data

    def fire(self, data, mem, damage=None):
        turn, cfg, nav, ledger = setup_case(data)
        mem.observe(turn, cfg)
        defend(turn, nav, ledger, [(turn.units[1], turn.units[22])], damage=damage, mem=mem)
        return ledger

    def test_focus_survives_lower_health_movement_and_a_stronger_arrival(self):
        data, mem = self.case(), Memory()
        first = self.fire(data, mem)
        self.assertEqual(first.commands['22']['targetPos'], [dict(x=12, y=5)])
        self.assertEqual(mem.gatling_targets, {22: 90})
        data['roundNo'] += 1
        data['robot']['roles'][0].update(health=10, pos=dict(x=12, y=6))
        data['robot']['roles'].append(unit(92, 'bossRobot', 6, 11, health=800,
                                          targetTeam='challenger'))
        second = self.fire(data, mem)
        self.assertEqual(second.commands['22']['targetPos'], [dict(x=12, y=6)])
        self.assertEqual(mem.gatling_targets, {22: 90})
        # Submitted lethal damage is not proof of death; observed survival wins.
        data['roundNo'] += 1
        self.assertEqual(self.fire(data, mem).commands['22']['targetPos'], [dict(x=12, y=6)])
        data['roundNo'] += 1
        data['robot']['roles'].pop(0)
        third = self.fire(data, mem)
        self.assertEqual(third.commands['22']['targetPos'], [dict(x=6, y=11)])
        self.assertEqual(mem.gatling_targets, {22: 92})

    def test_spare_bullets_switch_only_after_focus_has_enough_planned_damage(self):
        data = self.case(level=3)
        data['robot']['roles'][0]['health'] = 30
        mem = Memory(gatling_targets={22: 90})
        damage = {}
        ledger = self.fire(data, mem, damage)
        self.assertEqual(ledger.commands['22']['targetPos'],
                         [dict(x=12, y=5), dict(x=12, y=5), dict(x=5, y=2)])
        self.assertEqual(damage, {90: 30, 91: 25})
        self.assertEqual(mem.gatling_targets, {22: 90})

    def test_other_weapons_planned_kill_frees_bullets_without_confirming_death(self):
        data, mem = self.case(), Memory(gatling_targets={22: 90})
        ledger = self.fire(data, mem, {90: 100})
        self.assertEqual(ledger.commands['22']['targetPos'], [dict(x=5, y=2)])
        self.assertEqual(mem.gatling_targets, {22: 90})
        data['roundNo'] += 1
        data['robot']['roles'][0]['health'] = 10
        self.assertEqual(self.fire(data, mem).commands['22']['targetPos'], [dict(x=12, y=5)])

    def test_focus_keeps_spare_bullets_inside_the_ninety_degree_cone(self):
        data, mem = self.case(level=3), Memory(gatling_targets={22: 90})
        data['robot']['roles'][0]['health'] = 10
        data['robot']['roles'][1]['pos'] = dict(x=0, y=5)
        damage = {}
        ledger = self.fire(data, mem, damage)
        self.assertEqual(ledger.commands['22']['targetPos'], [dict(x=12, y=5)] * 3)
        self.assertEqual(damage, {90: 10})

    def test_planned_dead_blocker_still_prevents_hitting_the_old_focus(self):
        data, mem = self.case(), Memory(gatling_targets={22: 90})
        data['robot']['roles'].append(unit(92, 'smallRobot', 8, 5, health=40,
                                          targetTeam='challenger'))
        damage = {92: 40}
        ledger = self.fire(data, mem, damage)
        self.assertEqual(ledger.commands['22']['targetPos'], [dict(x=5, y=2)])
        self.assertNotIn(90, damage)
        self.assertEqual(mem.gatling_targets, {22: 91})

    def test_focus_validity_ignores_static_obstacles_but_respects_robot_collision(self):
        for reason in ('dead', 'missing', 'out_of_range', 'opponent_bound', 'ours',
                       'wall', 'character', 'robot', 'protected_robot'):
            with self.subTest(reason=reason):
                data, mem = self.case(), Memory(gatling_targets={22: 90})
                robot = data['robot']['roles'][0]
                if reason == 'dead':
                    robot['health'] = 0
                elif reason == 'missing':
                    data['robot']['roles'].pop(0)
                elif reason == 'out_of_range':
                    robot['pos'] = dict(x=14, y=5)
                elif reason == 'opponent_bound':
                    robot['targetTeam'] = 'defender'
                elif reason == 'ours':
                    data['teamOur']['summonRobotList'] = [copy.deepcopy(robot)]
                elif reason in ('wall', 'character'):
                    kind = 'wall' if reason == 'wall' else 'worker'
                    data['teamOur']['roles'].append(unit(70, kind, 8, 5))
                    robot['health'] = 10
                else:
                    team = 'defender' if reason == 'protected_robot' else 'challenger'
                    data['robot']['roles'].append(unit(92, 'smallRobot', 8, 5,
                                                      health=40, targetTeam=team))
                ledger = self.fire(data, mem)
                if reason in ('wall', 'character'):
                    self.assertEqual(ledger.commands['22']['targetPos'], [dict(x=12, y=5)])
                    self.assertEqual(mem.gatling_targets, {22: 90})
                else:
                    self.assertEqual(ledger.commands['22']['targetPos'], [dict(x=5, y=2)])
                    self.assertEqual(mem.gatling_targets, {22: 91})

    def test_no_legal_shot_clears_obstructed_focus(self):
        data, mem = self.case(), Memory(gatling_targets={22: 90})
        data['robot']['roles'].pop()
        data['robot']['roles'].append(unit(92, 'smallRobot', 8, 5, health=40, targetTeam='defender'))
        self.assertNotIn('22', self.fire(data, mem).commands)
        self.assertEqual(mem.gatling_targets, {})

    def test_daylight_and_destroyed_gun_clear_focus_even_without_a_shot(self):
        for reason in ('daylight', 'destroyed', 'replaced'):
            with self.subTest(reason=reason):
                data, mem = self.case(), Memory(gatling_targets={22: 90})
                if reason == 'daylight':
                    data['roundNo'] = 130
                elif reason == 'destroyed':
                    data['teamOur']['roles'].pop()
                else:
                    data['teamOur']['roles'][-1]['roleType'] = 'rocket'
                mem.observe(Turn(data, Config()), Config())
                self.assertEqual(mem.gatling_targets, {})

    def test_preview_and_rejected_command_do_not_acquire_focus(self):
        data, mem = self.case(), Memory()
        turn, _, nav, _ = setup_case(data)
        report = {}
        select_targets(turn, turn.units[22], {}, nav.deadline, diagnostics=report)
        self.assertEqual(report['focus_target_id'], 90)
        self.assertEqual(mem.gatling_targets, {})
        with patch('agent.commands.Ledger.add', return_value=False):
            ledger = self.fire(data, mem)
        self.assertEqual(ledger.weapon_diagnostics[22]['reason'], 'attack_rejected')
        self.assertEqual(mem.gatling_targets, {})

    def test_all_covered_focus_stays_pending_observation(self):
        data, mem = self.case(), Memory(gatling_targets={22: 90})
        self.assertNotIn('22', self.fire(data, mem, {90: 100, 91: 80}).commands)
        self.assertEqual(mem.gatling_targets, {22: 90})

    def test_gun_locks_are_independent(self):
        data = self.case()
        data['teamOur']['roles'] += [unit(2, 'worker', 4, 3),
                                     unit(23, 'gatling', 5, 4, attackRange=7)]
        mem = Memory(gatling_targets={22: 90, 23: 91})
        turn, _, nav, ledger = setup_case(data)
        defend(turn, nav, ledger, [(turn.units[1], turn.units[22]),
                                  (turn.units[2], turn.units[23])], mem=mem)
        self.assertEqual(ledger.commands['22']['targetPos'], [dict(x=12, y=5)])
        self.assertEqual(ledger.commands['23']['targetPos'], [dict(x=5, y=2)])
        self.assertEqual(mem.gatling_targets, {22: 90, 23: 91})

    def test_agent_shared_and_ordinary_defence_keep_focus_across_requests(self):
        for loadout in (['rocket', 'rocket', 'gatling'], ['gatling']):
            with self.subTest(loadout=loadout):
                data = mixed_case()
                if loadout == ['gatling']:
                    data['teamOur']['roles'] = [r for r in data['teamOur']['roles']
                                               if r['roleType'] != 'rocket']
                data['robot']['roles'].append(unit(91, 'largeRobot', 2, 11, health=400,
                                                  attackRange=1, targetTeam='challenger'))
                agent = Agent(Config(llm_enabled=False, loadout=loadout))
                first = agent.decide(data)['roleCommandMap']
                self.assertEqual(first['22']['targetPos'], [dict(x=2, y=10)])
                data['roundNo'] += 1
                data['robot']['roles'][0]['health'] = 25
                second = agent.decide(data)['roleCommandMap']
                self.assertEqual(second['22']['targetPos'], [dict(x=2, y=10)])
                mem = next(iter(agent.sessions.values()))
                self.assertEqual(mem.gatling_targets, {22: 90})


if __name__ == '__main__':
    unittest.main()
