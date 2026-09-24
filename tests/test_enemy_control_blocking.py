"""Focused movement, scheduling and occupancy checks; no game simulation."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.combat import block_enemy_controls
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, distance, neighbours, pos


def battery(data):
    # (10, 5) is the only cell touching all three guns.
    data['teamEnemy']['roles'] = [unit(50, 'rocket', 9, 4),
                                 unit(51, 'gatling', 11, 4),
                                 unit(52, 'railgun', 10, 6)]
    return data


def sole_post(round_no=1):
    data = payload(round_no, [unit(11, 'pioneer', 8, 5)])
    data['teamEnemy']['roles'] = [unit(50, 'rocket', 10, 5)]
    data['teamEnemy']['roles'] += [unit(60 + i, 'wall', *p)
        for i, p in enumerate(neighbours((10, 5))) if p != (9, 5)]
    return data


class EnemyControlBlockingTests(unittest.TestCase):
    def test_idle_pioneer_moves_to_shared_post_in_both_orientations(self):
        for mirrored in (False, True):
            data = battery(payload(1, [unit(11, 'pioneer', 9, 5)]))
            target = (10, 5)
            if mirrored:
                data['teamOur']['type'] = 'defender'
                for team in ('teamOur', 'teamEnemy'):
                    for role in data[team]['roles']:
                        role['pos']['x'] = 14 - role['pos']['x']
                target = (4, 5)
            commands = Agent(Config(llm_enabled=False, layout_mode='explicit')).decide(data)['roleCommandMap']
            self.assertEqual(commands['11'], {'action': 'move', 'targetPos': [dict(zip(('x', 'y'), target))]})
            self.assertTrue(all(distance(target, pos(w['pos'])) == 1 for w in data['teamEnemy']['roles']))

    def test_arrival_holds_day_and_night_without_self_move(self):
        for round_no in (1, 69, 70, 129):
            data = battery(payload(round_no, [unit(11, 'pioneer', 10, 5),
                                               unit(13, 'station', 3, 11)]))
            commands = Agent(Config(llm_enabled=False, layout_mode='explicit')).decide(data)['roleCommandMap']
            self.assertNotIn('11', commands)
            turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
            self.assertTrue(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))
            self.assertIn(11, ledger.used)
            self.assertIn((10, 5), ledger.reserved)

    def test_unique_control_cell_precedes_nearer_shared_post(self):
        data = sole_post()
        data['teamOur']['roles'][0]['pos'] = {'x': 4, 'y': 5}
        data['teamEnemy']['roles'] += [unit(80, 'rocket', 3, 4), unit(81, 'rocket', 5, 4)]
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        self.assertTrue(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))
        self.assertEqual(pos(ledger.commands['11']['targetPos'][0])[0], 5)

    def test_dusk_departure_uses_route_length_and_margin(self):
        for round_no, expected in ((62, False), (63, True)):
            data = sole_post(round_no)
            data['teamOur']['roles'][0]['pos'] = {'x': 7, 'y': 5}
            turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
            self.assertEqual(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer,
                                                  dusk_only=True), expected)
            self.assertEqual(bool(ledger.commands), expected)

    def test_early_task_keeps_priority_but_dusk_departs_before_new_task(self):
        for round_no, expected in ((1, 'acceptTask'), (63, 'move')):
            data = sole_post(round_no)
            data['teamOur']['roles'][0]['pos'] = {'x': 7, 'y': 5}
            data['teamOur']['playerTasks'] = [dict(isValid=True, coldDownRounds=0,
                taskPosition={'x': 6, 'y': 5}, timeoutRounds=2, scoreReward=10, goldReward=10)]
            result = Agent(Config(layout_mode='explicit', task_min_rounds=1)).decide(data)
            self.assertEqual(result['roleCommandMap']['11']['action'], expected)
            if expected == 'move':
                self.assertEqual(pos(result['roleCommandMap']['11']['targetPos'][0])[0], 8)

    def test_third_day_skips_task_that_would_delay_enemy_post(self):
        for round_no, expected in ((280, 6), (310, 8)):
            data = sole_post(round_no)
            data['teamOur']['roles'][0]['pos'] = {'x': 7, 'y': 5}
            data['teamOur']['playerTasks'] = [dict(isValid=True, coldDownRounds=0,
                taskPosition={'x': 5, 'y': 5}, timeoutRounds=15,
                scoreReward=10, goldReward=10)]
            command = Agent(Config(layout_mode='explicit', task_min_rounds=1)).decide(data)['roleCommandMap']['11']
            self.assertEqual(command['action'], 'move')
            self.assertEqual(pos(command['targetPos'][0])[0], expected)

    def test_active_task_is_not_interrupted_at_dusk_or_night(self):
        for round_no in (69, 70):
            data = sole_post(round_no)
            data['phaseTask'] = 'Return the integer answer to the current task.'
            result = Agent(Config(layout_mode='explicit')).decide(data)
            self.assertNotEqual(result['roleCommandMap'].get('11', {}).get('action'), 'move')
            turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
            self.assertFalse(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))

    def test_medicine_keeps_priority(self):
        for round_no in (1, 69, 70):
            data = sole_post(round_no)
            data['teamOur']['roles'][0].update(health=50, backpack=['Medicine'])
            commands = Agent(Config(llm_enabled=False, layout_mode='explicit')).decide(data)['roleCommandMap']
            self.assertEqual(commands['11'], {'action': 'use', 'name': 'Medicine'})

    def test_free_pioneer_goes_to_enemy_post_instead_of_home_at_night(self):
        data = sole_post(70)
        data['teamOur']['roles'].append(unit(13, 'station', 3, 11))
        commands = Agent(Config(llm_enabled=False, layout_mode='explicit')).decide(data)['roleCommandMap']
        self.assertEqual(commands['11'], {'action': 'move', 'targetPos': [{'x': 9, 'y': 5}]})

    def test_occupied_or_reserved_only_post_is_not_entered(self):
        for blocker in ('enemy', 'ally', 'robot', 'reservation', 'construction', 'operator'):
            data = sole_post()
            if blocker == 'enemy':
                data['teamEnemy']['roles'].append(unit(90, 'worker', 9, 5))
            elif blocker == 'ally':
                data['teamOur']['roles'].append(unit(12, 'worker', 9, 5))
            elif blocker == 'robot':
                data['robot']['roles'].append(unit(90, 'smallRobot', 9, 5))
            turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
            if blocker == 'reservation':
                ledger.reserved.add((9, 5))
            elif blocker == 'construction':
                ledger.wall_cells.add((9, 5))
            elif blocker == 'operator':
                ledger.operator_posts[12] = (9, 5)
            with self.subTest(blocker=blocker):
                self.assertFalse(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))
                self.assertFalse(ledger.commands)

    def test_unreachable_posts_do_not_issue_moves(self):
        data = battery(payload(1, [unit(11, 'pioneer', 2, 2)]))
        data['teamEnemy']['roles'] += [unit(100 + i, 'wall', *p)
                                      for i, p in enumerate(neighbours((2, 2)))]
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        self.assertFalse(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))
        self.assertFalse(ledger.used)

    def test_occupied_shared_post_falls_back_to_an_open_control_cell(self):
        data = battery(payload(1, [unit(11, 'pioneer', 8, 5)]))
        data['teamEnemy']['roles'].append(unit(90, 'worker', 10, 5))
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        self.assertTrue(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))
        destination = pos(ledger.commands['11']['targetPos'][0])
        self.assertNotIn(destination, turn.blocked)
        self.assertTrue(any(distance(destination, w.pos) == 1 for w in turn.enemies[:3]))

    def test_missing_or_destroyed_towers_keep_original_night_return(self):
        for destroyed in (False, True):
            data = payload(70, [unit(11, 'pioneer', 8, 5), unit(13, 'station', 3, 11)])
            agent = Agent(Config(llm_enabled=False, layout_mode='explicit'))
            expected = agent.decide(data)
            if destroyed:
                battery(data)
                for tower in data['teamEnemy']['roles']:
                    tower['health'] = 0
            self.assertEqual(Agent(agent.cfg).decide(data), expected)

    def test_known_tower_survives_fog_but_is_released_when_visibly_gone(self):
        data = sole_post()
        data['teamOur']['roles'][0]['pos'] = {'x': 1, 'y': 5}
        cfg, mem = Config(layout_mode='explicit'), Memory()
        mem.movement.observe(Turn(data, cfg), mem)
        data['teamEnemy']['roles'] = []
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        mem.movement.observe(turn, mem)
        nav.memory = mem.movement
        self.assertTrue(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))
        data['teamOur']['roles'][0]['pos'] = {'x': 8, 'y': 5}
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        mem.movement.observe(turn, mem)
        nav.memory = mem.movement
        self.assertFalse(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))

    def test_worker_and_already_used_pioneer_are_never_redirected(self):
        data = sole_post()
        data['teamOur']['roles'].append(unit(12, 'worker', 7, 5))
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        self.assertFalse(block_enemy_controls(turn, cfg, nav, ledger, turn.workers[0]))
        ledger.used.add(11)
        self.assertFalse(block_enemy_controls(turn, cfg, nav, ledger, turn.pioneer))
        self.assertFalse(ledger.commands)

    def test_worker_mining_and_shared_gunner_commands_are_unchanged(self):
        data = payload(330, [unit(13, 'station', 5, 11, health=1500),
            unit(1, 'worker', 4, 11, health=220), unit(2, 'worker', 11, 2, health=220),
            unit(11, 'pioneer', 8, 5), unit(20, 'rocket', 4, 12),
            unit(21, 'rocket', 5, 12), unit(22, 'rocket', 4, 10)])
        data['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 12, 'y': 2}}]
        data['robot']['roles'] = [unit(90, 'smallRobot', 8, 10, health=500,
                                     attackRange=1, targetTeam='challenger')]
        cfg = Config(llm_enabled=False)
        baseline = Agent(cfg).decide(data)['roleCommandMap']
        changed = Agent(cfg).decide(battery(copy.deepcopy(data)))['roleCommandMap']
        self.assertEqual({k: v for k, v in changed.items() if k != '11'},
                         {k: v for k, v in baseline.items() if k != '11'})
        self.assertEqual(changed['2']['action'], 'collect')
        self.assertEqual(changed['20']['controllerId'], '1')
        self.assertEqual(changed['11']['action'], 'move')

    def test_pioneer_needed_for_home_defence_is_not_redirected(self):
        for round_no in (69, 70):
            data = battery(payload(round_no, [unit(13, 'station', 3, 11),
                unit(11, 'pioneer', 8, 5), unit(20, 'rocket', 3, 12)]))
            cfg = Config(llm_enabled=False, layout_mode='explicit', weapon_cells=[[3, 12]])
            changed = Agent(cfg).decide(data)
            data['teamEnemy']['roles'] = []
            baseline = Agent(cfg).decide(data)
            self.assertEqual(changed['roleCommandMap'], baseline['roleCommandMap'])
