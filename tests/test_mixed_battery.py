"""Focused requests and combat boundaries for the worker/pioneer battery."""
import copy
import unittest

from test_agent import ROOT, payload, setup_case, unit
from agent.brain import Agent
from agent.combat import select_targets
from agent.config import Config
from agent.economy import build
from agent.intelligence import Memory
from agent.model import Turn, pos
from agent.navigation import layout


def mixed_config(**options):
    return Config(loadout=['rocket', 'rocket', 'gatling'], **options)


def mixed_case(round_no=80, walls=False):
    data = payload(round_no, [unit(13, 'station', 5, 11, health=1500),
        unit(1, 'worker', 4, 9, health=500), unit(2, 'worker', 11, 2, health=500),
        unit(11, 'pioneer', 4, 11, health=500),
        unit(20, 'rocket', 4, 12, level=3, attackRange=20),
        unit(21, 'rocket', 5, 12, level=3, attackRange=20),
        unit(22, 'gatling', 4, 10, level=1, attackRange=7)])
    data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=x, y=y))
                               for x, y in ((2, 9), (12, 2))]
    data['vendorShopList'] = [dict(name='copper', price=3)]
    data['robot']['roles'] = [unit(90, 'largeRobot', 2, 10, health=500,
                                  attackRange=1, targetTeam='challenger')]
    if walls:
        sites = layout(Turn(data, mixed_config()), mixed_config())[1]
        data['teamOur']['roles'] += [unit(100+i, 'wall', *p) for i, p in enumerate(sites)]
    return data


def enemy_walls(data, count=6):
    data['teamEnemy']['roles'] = [unit(50, 'station', 10, 2)]
    data['teamEnemy']['roles'] += [unit(60+y, 'wall', 8, y, health=1000) for y in range(count)]


class MixedBatteryTests(unittest.TestCase):
    def test_explicit_mixed_configuration_builds_two_rockets_and_one_gatling(self):
        for cfg in (mixed_config(),):
            self.assertEqual(cfg.loadout, ['rocket', 'rocket', 'gatling'])
            data = payload(10, [unit(13, 'station', 5, 11), unit(1, 'worker', 4, 11)])
            sites = layout(Turn(data, cfg), cfg)[0]
            # Each independent request leaves exactly one unbuilt weapon site.
            for index, expected in enumerate(('rocket', 'rocket', 'gatling')):
                case = copy.deepcopy(data)
                case['teamOur']['roles'][1]['pos'] = dict(zip(('x', 'y'), (4, 9) if index == 2 else (4, 11)))
                case['teamOur']['roles'] += [unit(20+i, kind, *site)
                    for i, (kind, site) in enumerate(zip(cfg.loadout, sites)) if i != index]
                turn, _, nav, ledger = setup_case(case)
                self.assertTrue(build(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                                      sites, lambda i: cfg.loadout[i]))
                self.assertEqual(ledger.commands['1']['name'], expected)
                self.assertEqual(pos(ledger.commands['1']['targetPos'][0]), sites[index])

    def test_pioneer_and_worker_fire_separate_weapons_on_every_night(self):
        for round_no in (80, 210, 340, 470):
            data = mixed_case(round_no)
            commands = Agent(mixed_config(llm_enabled=False)).decide(data)['roleCommandMap']
            self.assertEqual(commands['22']['controllerId'], '1')
            rockets = [c for uid, c in commands.items() if uid in ('20', '21')]
            self.assertEqual(len(rockets), 1)
            self.assertEqual(rockets[0]['controllerId'], '11')
            self.assertNotIn('1', commands)
            self.assertNotIn('11', commands)
            if round_no < 460:
                self.assertEqual(commands['2']['action'], 'collect')

    def test_two_rocket_rotation_preserves_controller_and_real_cooldown(self):
        data, agent = mixed_case(), Agent(mixed_config(llm_enabled=False))
        for offset, expected in enumerate(('20', '21', '20', '21')):
            data['roundNo'] = 80 + offset
            commands = agent.decide(data)['roleCommandMap']
            self.assertEqual([uid for uid in ('20', '21') if uid in commands], [expected])
            self.assertEqual(commands[expected]['controllerId'], '11')
        data['roundNo'] += 1
        data['teamOur']['roles'][4]['cooldown'] = 3
        commands = agent.decide(data)['roleCommandMap']
        self.assertNotIn('20', commands)
        self.assertEqual(commands['21']['controllerId'], '11')
        data['roundNo'] += 1
        data['teamOur']['roles'][5]['cooldown'] = 2
        commands = agent.decide(data)['roleCommandMap']
        self.assertFalse(any(uid in commands for uid in ('20', '21')))
        self.assertNotIn('11', commands)
        self.assertEqual(commands['22']['controllerId'], '1')

    def test_clear_wave_releases_worker_while_level_one_gatling_does_not_block_siege(self):
        data = mixed_case()
        data['robot']['roles'] = []
        enemy_walls(data)
        commands = Agent(mixed_config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['20']['controllerId'], '11')
        self.assertEqual(len(commands['20']['targetPos']), 3)
        self.assertTrue(all(p['x'] == 8 for p in commands['20']['targetPos']))
        self.assertNotIn('22', commands)
        self.assertIn(commands['1']['action'], ('move', 'collect'))
        self.assertEqual(commands['2']['action'], 'collect')

    def test_released_gatling_worker_mines_instead_of_becoming_wall_watcher(self):
        data = mixed_case(470, walls=True)
        data['robot']['roles'] = []
        enemy_walls(data)
        agent = Agent(mixed_config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        self.assertIn(commands['1']['action'], ('move', 'collect'))
        self.assertEqual(next(iter(agent.sessions.values())).wall_watch_id, 2)
        self.assertEqual(commands['20']['controllerId'], '11')

    def test_worker_returns_when_enemy_reappears_after_mining_move(self):
        data, agent = mixed_case(), Agent(mixed_config(llm_enabled=False))
        self.assertEqual(agent.decide(data)['roleCommandMap']['22']['controllerId'], '1')
        robot = data['robot']['roles'][0]
        data['roundNo'] += 1
        robot['health'] = 0
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'move')
        data['teamOur']['roles'][1]['pos'] = commands['1']['targetPos'][0]
        data['roundNo'] += 1
        robot['health'] = 500
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'move')
        self.assertEqual(next(iter(agent.sessions.values())).gatling_operator_id, 1)
        data['teamOur']['roles'][1]['pos'] = commands['1']['targetPos'][0]
        data['roundNo'] += 1
        self.assertEqual(agent.decide(data)['roleCommandMap']['22']['controllerId'], '1')

    def test_dead_gatling_worker_is_replaced_without_taking_pioneer(self):
        data, agent = mixed_case(), Agent(mixed_config(llm_enabled=False))
        agent.decide(data)
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['health'] = 0
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(next(iter(agent.sessions.values())).gatling_operator_id, 2)
        self.assertEqual(commands['2']['action'], 'move')
        self.assertEqual(commands['21']['controllerId'], '11')

    def test_healing_gatling_worker_does_not_recall_other_miner(self):
        data, agent = mixed_case(), Agent(mixed_config(llm_enabled=False))
        agent.decide(data)
        data['roundNo'] += 1
        data['teamOur']['roles'][1].update(health=50, backpack=['Medicine'])
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['1'], dict(action='use', name='Medicine'))
        self.assertNotIn('22', commands)
        self.assertEqual(commands['2']['action'], 'collect')
        self.assertEqual(next(iter(agent.sessions.values())).gatling_operator_id, 1)

    def test_dead_pioneer_never_makes_gatling_worker_control_a_rocket(self):
        data = mixed_case()
        data['teamOur']['roles'][3]['health'] = 0
        commands = Agent(mixed_config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['22']['controllerId'], '1')
        self.assertFalse(any(uid in commands for uid in ('20', '21')))

    def test_no_enemy_blockade_even_with_active_task_on_third_night(self):
        data = mixed_case(340)
        data['teamOur']['roles'][3]['pos'] = dict(x=8, y=5)
        data['teamEnemy']['roles'] = [unit(50, 'rocket', 10, 5)]
        data['phaseTask'] = 'unfinished task'
        agent = Agent(mixed_config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['11']['action'], 'move')
        self.assertLess(commands['11']['targetPos'][0]['x'], 8)
        self.assertEqual(next(iter(agent.sessions.values())).gatling_operator_id, 1)
        # The worker may yield the return corridor before it can fire again.
        self.assertTrue(commands.get('22', {}).get('controllerId') == '1'
                        or commands.get('1', {}).get('action') == 'move')

    def test_full_perimeter_has_two_distinct_control_posts_in_four_corners(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            data = mixed_case(walls=True)
            for group in (data['teamOur']['roles'], data['robot']['roles'], data['mapInfo']['zones']):
                for role in group:
                    point = role['pos']
                    if flip_x:
                        point['x'] = (13 if role.get('roleType') == 'station' else 14) - point['x']
                    if flip_y:
                        point['y'] = (15 if role.get('roleType') == 'station' else 14) - point['y']
            commands = Agent(mixed_config(llm_enabled=False)).decide(data)['roleCommandMap']
            self.assertEqual(commands['22']['controllerId'], '1')
            self.assertEqual(commands['20']['controllerId'], '11')
            self.assertNotIn('1', commands)
            self.assertNotIn('11', commands)

    def test_siege_target_count_is_legal_when_only_one_wall_remains(self):
        data = mixed_case()
        data['robot']['roles'] = []
        enemy_walls(data, count=1)
        commands = Agent(mixed_config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['20']['targetPos'], [dict(x=8, y=0)] * 3)

    def test_local_enemy_prevents_wall_siege(self):
        data = mixed_case()
        enemy_walls(data)
        commands = Agent(mixed_config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertTrue(all(p == dict(x=2, y=10) for p in commands['20']['targetPos']))
        self.assertNotIn('1', commands)

    def test_day_never_fires_gatling_or_rockets(self):
        commands = Agent(mixed_config(llm_enabled=False)).decide(mixed_case(69))['roleCommandMap']
        self.assertFalse(any(c['action'] == 'attack' for c in commands.values()))

    def test_remote_opponent_wave_does_not_keep_gatling_worker_on_post(self):
        data = mixed_case()
        data['mapInfo'].update(width=41, height=32)
        data['robot']['roles'] = [unit(90, 'bossRobot', 35, 25, health=800,
                                      attackRange=3, targetTeam='defender')]
        enemy_walls(data)
        cfg = mixed_config(llm_enabled=False, layout_mode='explicit',
                     weapon_cells=[[4, 12], [5, 12], [4, 10]])
        commands = Agent(cfg).decide(data)['roleCommandMap']
        self.assertIn(commands['1']['action'], ('move', 'collect'))
        self.assertNotIn('22', commands)
        self.assertEqual(commands['20']['controllerId'], '11')


class GatlingTargetTests(unittest.TestCase):
    def case(self, level=1):
        data = payload(80, [unit(13, 'station', 1, 8), unit(1, 'worker', 4, 6),
                            unit(22, 'gatling', 5, 5, level=level, attackRange=7)])
        return data

    def targets(self, data, damage=None):
        turn, _, nav, _ = setup_case(data)
        damage = {} if damage is None else damage
        return select_targets(turn, turn.weapons[0], damage, nav.deadline), damage

    def test_highest_current_health_precedes_distance_and_threat_score(self):
        data = self.case()
        data['robot']['roles'] = [unit(90, 'smallRobot', 5, 2, health=40, targetTeam='challenger'),
                                  unit(91, 'smallRobot', 12, 5, health=800, targetTeam='challenger')]
        targets, damage = self.targets(data)
        self.assertEqual(targets, [(12, 5)])
        self.assertEqual(damage, {91: 25})

    def test_level_three_concentrates_legal_bullets_on_highest_health_enemy(self):
        data = self.case(3)
        data['robot']['roles'] = [unit(90, 'bossRobot', 5, 2, health=800),
                                  unit(91, 'bossRobot', 12, 5, health=900)]
        targets, damage = self.targets(data)
        self.assertEqual(targets, [(12, 5)] * 3)
        self.assertEqual(damage, {91: 75})

    def test_all_multi_targets_stay_in_one_ninety_degree_cone(self):
        data = self.case(3)
        data['robot']['roles'] = [unit(90, 'smallRobot', 5, 2, health=20),
                                  unit(91, 'smallRobot', 12, 5, health=25),
                                  unit(92, 'smallRobot', 0, 5, health=15)]
        targets, _ = self.targets(data)
        self.assertEqual(len(targets), 3)
        self.assertEqual(targets[0], (12, 5))
        vectors = [(x-5, y-5) for x, y in targets]
        self.assertTrue(all(ax*bx + ay*by >= 0 for ax, ay in vectors for bx, by in vectors))

    def test_blocking_robot_health_is_ranked_instead_of_hidden_aim_point(self):
        data = self.case()
        data['robot']['roles'] = [unit(90, 'bossRobot', 12, 5, health=800),
                                  unit(91, 'smallRobot', 8, 5, health=40),
                                  unit(92, 'middleRobot', 5, 2, health=60)]
        targets, damage = self.targets(data)
        self.assertEqual(targets, [(5, 2)])
        self.assertEqual(damage, {92: 25})

    def test_protected_robot_in_bullet_path_is_never_shot(self):
        data = self.case()
        data['robot']['roles'] = [unit(90, 'bossRobot', 12, 5, health=800, targetTeam='challenger'),
                                  unit(91, 'smallRobot', 8, 5, health=40, targetTeam='defender'),
                                  unit(92, 'middleRobot', 5, 2, health=60, targetTeam='challenger')]
        targets, damage = self.targets(data)
        self.assertEqual(targets, [(5, 2)])
        self.assertNotIn(91, damage)

    def test_high_health_behind_wall_or_out_of_range_does_not_override_legal_target(self):
        data = self.case()
        data['teamOur']['roles'].append(unit(70, 'wall', 8, 5))
        data['robot']['roles'] = [unit(90, 'bossRobot', 12, 5, health=800),
                                  unit(91, 'bossRobot', 14, 6, health=900),
                                  unit(92, 'middleRobot', 5, 2, health=60)]
        self.assertEqual(self.targets(data)[0], [(5, 2)])

    def test_dead_enemy_and_enemy_already_covered_by_planned_damage_are_skipped(self):
        data = self.case()
        data['robot']['roles'] = [unit(90, 'bossRobot', 12, 5, health=800),
                                  unit(91, 'bossRobot', 8, 6, health=0),
                                  unit(92, 'middleRobot', 5, 2, health=60)]
        self.assertEqual(self.targets(data, {90: 800})[0], [(5, 2)])


if __name__ == '__main__':
    unittest.main()
