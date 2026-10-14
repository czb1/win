"""Gatling obstacle rules and night-post release boundaries; no simulation."""
from copy import deepcopy
import unittest

from test_agent import payload, setup_case, unit
from test_logging_system import capture, records
from agent.brain import Agent
from agent.combat import defend, select_targets
from agent.config import Config


def night_case(round_no=460):
    # Relevant geometry from the reported fourth-night turns, without the full log.
    data = payload(round_no, [
        unit(10010, 'worker', 11, 21, health=500, attackRange=0,
             backpack=['WallFixer'] * 3),
        unit(10011, 'pioneer', 8, 22, health=500, attackRange=0),
        unit(10012, 'worker', 8, 20, health=500, attackRange=0),
        unit(10013, 'station', 9, 22, health=1145, attackRange=0, level=2),
        unit(10020, 'gatling', 8, 21, health=1500, attackRange=5, level=2),
        unit(10040, 'rocket', 8, 23, health=1500, attackRange=15, level=2),
        unit(10041, 'rocket', 9, 23, health=990, attackRange=10),
    ])
    data['mapInfo'].update(width=41, height=32, zones=[
        {'neutralType': 'copper', 'pos': {'x': 13, 'y': 17}, 'remain': 4}])
    data['teamEnemy']['roles'] = [unit(20013, 'station', 30, 10, attackRange=0)]
    data['robot']['roles'] = [unit(30139, 'largeRobot', 23, 25, health=360,
                                   attackRange=3, targetTeam='challenger')]
    return data


class GatlingObstacleTests(unittest.TestCase):
    def test_buildings_characters_and_neutrals_do_not_occlude_gatling(self):
        obstacles = [(side, kind) for side in ('teamOur', 'teamEnemy')
                     for kind in ('station', 'wall', 'gatling', 'worker', 'pioneer', 'imp')]
        obstacles += [('mapInfo', kind) for kind in ('stone', 'vendor', 'challengerTaskPoint1')]
        for side, kind in obstacles:
            with self.subTest(side=side, kind=kind):
                data = payload(80, [unit(1, 'worker', 4, 5),
                                    unit(20, 'gatling', 5, 5, attackRange=5, level=2)])
                if side == 'mapInfo':
                    data[side]['zones'].append({'neutralType': kind, 'pos': {'x': 7, 'y': 5}})
                else:
                    data[side]['roles'].append(unit(70, kind, 7, 5))
                data['robot']['roles'] = [unit(90, 'largeRobot', 9, 5, health=500,
                                               targetTeam='challenger')]
                turn, _, nav, ledger = setup_case(data)
                before, damage, report = turn.blocked.copy(), {}, {}
                targets = select_targets(turn, turn.units[20], damage, nav.deadline,
                                         controller=turn.units[1], diagnostics=report)
                self.assertEqual(targets, [(9, 5)] * 2)
                self.assertEqual(damage, {90: 50})
                self.assertEqual(report['reason'], 'ready_to_fire')
                self.assertEqual(report['clear_paths'], 1)
                self.assertEqual(report['blocker_counts'], {})
                self.assertEqual(turn.blocked, before)
                self.assertIn((7, 5), turn.blocked)
                defend(turn, nav, ledger, [(turn.units[1], turn.units[20])], {1: (4, 5)})
                self.assertEqual(ledger.commands['20']['action'], 'attack')

    def test_first_robot_still_absorbs_gatling_shots(self):
        data = payload(80, [unit(1, 'worker', 4, 5),
                            unit(20, 'gatling', 5, 5, attackRange=5, level=2),
                            unit(70, 'wall', 6, 5)])
        data['robot']['roles'] = [unit(90, 'smallRobot', 7, 5, health=10, targetTeam='challenger'),
                                  unit(91, 'largeRobot', 9, 5, health=500, targetTeam='challenger')]
        turn, _, nav, _ = setup_case(data)
        damage = {}
        self.assertTrue(select_targets(turn, turn.units[20], damage, nav.deadline))
        self.assertEqual(damage[90], 10)
        self.assertEqual(damage.get(91, 0), 0)

    def test_protected_robot_still_prevents_friendly_fire(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                data = payload(80, [unit(1, 'worker', 4, 5), unit(20, 'gatling', 5, 5, attackRange=5)])
                protector = unit(90, 'smallRobot', 7, 5, health=40,
                                 targetTeam='challenger' if owned else 'defender')
                data['robot']['roles'] = [protector, unit(91, 'largeRobot', 9, 5, health=500,
                                                         targetTeam='challenger')]
                if owned:
                    data['teamOur']['summonRobotList'] = [deepcopy(protector)]
                turn, _, nav, _ = setup_case(data)
                damage, report = {}, {}
                self.assertEqual(select_targets(turn, turn.units[20], damage, nav.deadline,
                                                diagnostics=report), [])
                self.assertEqual(report['reason'], 'protected_robot_in_path')
                self.assertEqual(damage, {})


class NightPostTests(unittest.TestCase):
    def decide(self, agent, data):
        with capture() as stream:
            commands = agent.decide(data)['roleCommandMap']
        decision = next(record['data'] for record in records(stream)
                        if record['event'] == 'unit_decision' and record['unit_id'] == 10012)
        return commands, decision

    def test_night_start_holds_post_while_wave_is_still_outside_range(self):
        for round_no in (70, 200, 460, 850):
            with self.subTest(round_no=round_no):
                agent = Agent(Config(llm_enabled=False))
                commands, decision = self.decide(agent, night_case(round_no))
                self.assertNotIn('10012', commands)
                self.assertNotIn('10020', commands)
                self.assertEqual(decision['reason'], 'operator_standby')
                self.assertEqual(decision['planning']['hold_reason'], 'incoming_wave')
                self.assertEqual(next(iter(agent.sessions.values())).return_targets[10012], 10020)

    def test_reported_base_geometry_now_fires_without_moving_the_worker(self):
        data = night_case(471)
        data['robot']['roles'][0]['pos'] = {'x': 11, 'y': 21}
        commands, decision = self.decide(Agent(Config(llm_enabled=False)), data)
        self.assertEqual(commands['10020'], {'action': 'attack', 'controllerId': '10012',
                                             'targetPos': [{'x': 11, 'y': 21}] * 2})
        self.assertNotIn('10012', commands)
        self.assertEqual(decision['reason'], 'operating_weapon')

    def test_clear_wave_allows_mining_and_a_new_distant_wave_recalls_immediately(self):
        data, agent = night_case(), Agent(Config(llm_enabled=False))
        commands, _ = self.decide(agent, data)
        self.assertNotIn('10012', commands)
        wave = data['robot']['roles']
        data['roundNo'] += 1
        data['robot']['roles'] = []
        commands, decision = self.decide(agent, data)
        self.assertEqual(commands['10012']['action'], 'move')
        self.assertEqual(decision['reason'], 'mine_for_later')
        data['teamOur']['roles'][2]['pos'] = commands['10012']['targetPos'][0]
        data['roundNo'] += 1
        data['robot']['roles'] = wave
        _, decision = self.decide(agent, data)
        self.assertIn(decision['reason'], ('operator_standby', 'operator_repositioning'))
        self.assertEqual(decision['planning']['hold_reason'], 'incoming_wave')
        self.assertEqual(next(iter(agent.sessions.values())).gatling_operator_id, 10012)

    def test_nearby_enemy_keeps_worker_at_post_after_robot_wave_clears(self):
        for kind in ('worker', 'pioneer', 'imp', 'gatling'):
            with self.subTest(kind=kind):
                data = night_case()
                data['robot']['roles'] = []
                data['teamEnemy']['roles'].append(unit(20014, kind, 13, 20, attackRange=0))
                commands, decision = self.decide(Agent(Config(llm_enabled=False)), data)
                self.assertNotIn('10012', commands)
                self.assertEqual(decision['planning']['hold_reason'], 'nearby_enemy')

    def test_own_opponent_bound_and_dead_robots_do_not_pin_the_worker(self):
        data = night_case()
        data['robot']['roles'] = [
            unit(30139, 'largeRobot', 23, 25, health=0, targetTeam='challenger'),
            unit(31139, 'largeRobot', 26, 8, health=360, targetTeam='defender'),
            unit(39999, 'largeRobot', 23, 25, health=360, targetTeam='challenger'),
        ]
        data['teamOur']['summonRobotList'] = [deepcopy(data['robot']['roles'][-1])]
        data['teamEnemy']['roles'].append(unit(20014, 'imp', 13, 20, health=0, attackRange=0))
        commands, decision = self.decide(Agent(Config(llm_enabled=False)), data)
        self.assertEqual(commands['10012']['action'], 'move')
        self.assertEqual(decision['reason'], 'mine_for_later')

    def test_daytime_worker_actions_are_not_locked_by_a_distant_wave(self):
        data = night_case(420)
        data['teamOur']['roles'][2]['pos'] = {'x': 12, 'y': 18}
        commands, decision = self.decide(Agent(Config(llm_enabled=False)), data)
        self.assertNotIn('10020', commands)
        self.assertNotIn('hold_reason', decision.get('planning', {}))
        self.assertIn(commands['10012']['action'], ('move', 'collect'))
