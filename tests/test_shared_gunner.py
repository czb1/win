"""Actual Agent requests: stationary rotation, night mining and death handoff."""
import unittest
from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.model import Turn, distance
from agent.navigation import layout


class SharedGunnerTests(unittest.TestCase):
    def case(self, round_no=70):
        # Independently specified rear corner of the 2x2 base at (5, 11).
        p = payload(round_no, [unit(13, 'station', 5, 11, health=1500),
            unit(1, 'worker', 4, 11, health=220), unit(2, 'worker', 11, 2, health=220),
            unit(20, 'rocket', 4, 12), unit(21, 'rocket', 5, 12), unit(22, 'rocket', 4, 10)])
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 12, 'y': 2}}]
        p['robot']['roles'] = [unit(90, 'smallRobot', 8, 10, health=500, attackRange=1,
                                    targetTeam='challenger')]
        return p

    def siege_case(self, round_no=70):
        p = self.case(round_no)
        p['robot']['roles'] = []
        for tower in p['teamOur']['roles'][3:]:
            tower.update(level=3, attackRange=20, cooldown=0)
        p['teamEnemy']['roles'] = [unit(50, 'station', 10, 2)]
        # The x=8 edge faces our station; the other cells are enemy flanks.
        p['teamEnemy']['roles'] += [unit(60 + y, 'wall', 8, y, health=1000)
                                    for y in range(6)]
        p['teamEnemy']['roles'] += [unit(70, 'wall', 9, 1, health=1000),
                                    unit(71, 'wall', 9, 4, health=1000),
                                    unit(72, 'wall', 10, 4, health=1000)]
        return p

    def test_four_corners_share_reachable_post_and_original_walls(self):
        for x, y in ((3, 11), (10, 4), (3, 4), (10, 11)):
            p = payload(roles=[unit(13, 'station', x, y), unit(1, 'worker', 7, 7)])
            t, cfg, _, _ = setup_case(p)
            sites, walls = layout(t, cfg)
            p['teamOur']['roles'] += [unit(20+i, 'rocket', *s) for i, s in enumerate(sites)]
            p['teamOur']['roles'] += [unit(100+i, 'wall', *s) for i, s in enumerate(walls)]
            t, _, nav, _ = setup_case(p)
            posts = [(a, b) for a in range(15) for b in range(15)
                     if (a, b) not in t.blocked and all(distance((a, b), s) == 1 for s in sites)]
            self.assertTrue(any(nav.search(t.workers[0], {s}) for s in posts))
            self.assertEqual(len(walls), 12)
            self.assertTrue(all(t.base_distance(s) == 1 for s in sites))
            self.assertTrue(all(s[0] <= x if x < 7 else s[0] >= x for s in sites))

    def test_rotation_uses_one_worker_and_miner_keeps_collecting(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        for step, expected in enumerate((20, 21, 22, 20, 21, 22)):
            p['roundNo'] = 70 + step
            for tower in p['teamOur']['roles'][3:]:
                tower['cooldown'] = 0 if tower['id'] == expected else 1
            result = agent.decide(p)['roleCommandMap']
            shots = [(uid, c) for uid, c in result.items() if c['action'] == 'attack']
            self.assertEqual([(uid, c['controllerId']) for uid, c in shots], [(str(expected), '1')])
            self.assertNotIn('1', result)
            self.assertEqual(result['2']['action'], 'collect')
            self.assertEqual(agent.decide(p)['roleCommandMap'], result)

    def test_real_cooldown_and_missing_tower_are_respected(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        for tower in p['teamOur']['roles'][3:]:
            tower['cooldown'] = 3
        result = agent.decide(p)['roleCommandMap']
        self.assertFalse(any(c['action'] == 'attack' for c in result.values()))
        self.assertNotIn('1', result)
        p['roundNo'] += 1
        p['teamOur']['roles'][3]['health'] = 0
        p['teamOur']['roles'][4]['cooldown'] = 0
        self.assertEqual(agent.decide(p)['roleCommandMap']['21']['controllerId'], '1')

    def test_dead_gunner_is_replaced_and_revived_worker_does_not_steal_post(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        agent.decide(p)
        p['teamOur']['roles'][1]['health'] = 0
        p['roundNo'] += 1
        result = agent.decide(p)['roleCommandMap']
        self.assertEqual(result['2']['action'], 'move')
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.gunner_id, 2)
        for _ in range(15):
            if result.get('2', {}).get('action') == 'move':
                p['teamOur']['roles'][2]['pos'] = result['2']['targetPos'][0]
            p['roundNo'] += 1
            result = agent.decide(p)['roleCommandMap']
            if any(c['action'] == 'attack' for c in result.values()):
                break
        self.assertTrue(any(c.get('controllerId') == '2' for c in result.values()))
        p['teamOur']['roles'][1].update(health=220, pos={'x': 11, 'y': 2})
        p['roundNo'] += 1
        result = agent.decide(p)['roleCommandMap']
        self.assertEqual(mem.gunner_id, 2)
        self.assertEqual(result['1']['action'], 'collect')

    def test_medicine_does_not_replace_living_gunner(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        agent.decide(p)
        p['roundNo'] += 1
        p['teamOur']['roles'][1].update(health=50, backpack=['Medicine'])
        result = agent.decide(p)['roleCommandMap']
        self.assertEqual(result['1']['action'], 'use')
        self.assertEqual(result['2']['action'], 'collect')
        self.assertEqual(next(iter(agent.sessions.values())).gunner_id, 1)

    def test_miner_still_avoids_personal_danger(self):
        p = self.case()
        p['robot']['roles'].append(unit(91, 'smallRobot', 12, 3, attackRange=1))
        result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertNotEqual(result.get('2', {}).get('action'), 'collect')

    def test_no_targets_gunner_stays_and_two_dead_workers_do_not_attack(self):
        p = self.case()
        p['robot']['roles'] = []
        agent = Agent(Config(llm_enabled=False))
        result = agent.decide(p)['roleCommandMap']
        self.assertNotIn('1', result)
        self.assertEqual(result['2']['action'], 'collect')
        p['roundNo'] += 1
        for h in p['teamOur']['roles'][1:3]:
            h['health'] = 0
        self.assertFalse(any(c['action'] == 'attack' for c in agent.decide(p)['roleCommandMap'].values()))

    def test_level_three_gunner_sieges_enemy_front_wall_at_night(self):
        p = self.siege_case()
        result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        shots = [c for c in result.values() if c['action'] == 'attack']
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]['controllerId'], '1')
        self.assertEqual(len(shots[0]['targetPos']), 3)
        self.assertTrue(all(point['x'] == 8 for point in shots[0]['targetPos']))
        self.assertNotIn('1', result)

    def test_enemy_wall_siege_is_never_controlled_during_day(self):
        p = self.siege_case(round_no=69)
        result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertFalse(any(c['action'] == 'attack' for c in result.values()))

    def test_local_threat_keeps_gunner_on_robot_defence(self):
        p = self.siege_case()
        p['robot']['roles'] = [unit(90, 'smallRobot', 6, 11, health=500,
                                    attackRange=1, targetTeam='challenger')]
        result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        shots = [c for c in result.values() if c['action'] == 'attack']
        self.assertEqual(len(shots), 1)
        self.assertEqual(len(shots[0]['targetPos']), 3)
        self.assertTrue(all(point == {'x': 6, 'y': 11} for point in shots[0]['targetPos']))

    def test_all_ready_guns_follow_persistent_rotation(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        for step, expected in enumerate((20, 21, 22, 20)):
            p['roundNo'] = 70 + step
            commands = agent.decide(p)['roleCommandMap']
            self.assertEqual([uid for uid, c in commands.items() if c['action'] == 'attack'],
                             [str(expected)])

    def test_worker_already_on_post_takes_over_blocked_gunner(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        agent.decide(p)
        p['roundNo'] += 1
        p['teamOur']['roles'][1]['pos'] = {'x': 2, 'y': 11}
        p['teamOur']['roles'][2].update(pos={'x': 4, 'y': 11}, backpack=['copper'] * 100)
        commands = agent.decide(p)['roleCommandMap']
        self.assertTrue(any(c.get('controllerId') == '2' for c in commands.values()))
        self.assertEqual(next(iter(agent.sessions.values())).gunner_id, 2)

    def test_idle_pioneer_yields_post_before_first_night(self):
        p = self.case(67)
        p['teamOur']['roles'][1]['pos'] = {'x': 2, 'y': 11}
        p['teamOur']['roles'].append(unit(3, 'pioneer', 4, 11))
        agent = Agent(Config(llm_enabled=False))
        for round_no in (67, 68, 69, 70):
            p['roundNo'] = round_no
            commands = agent.decide(p)['roleCommandMap']
            for uid, c in commands.items():
                if c['action'] == 'move':
                    next(h for h in p['teamOur']['roles'] if h['id'] == int(uid))['pos'] = c['targetPos'][0]
        self.assertTrue(any(c.get('controllerId') == '1' for c in commands.values()))

    def test_recall_does_not_take_stone_carrier_from_unfinished_walls(self):
        p = self.case(60)
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 3
        p['teamOur']['roles'][2]['pos'] = {'x': 2, 'y': 11}
        agent = Agent(Config(llm_enabled=False))
        agent.decide(p)
        self.assertEqual(next(iter(agent.sessions.values())).gunner_id, 2)

    def test_dusk_arrival_has_priority_over_freeing_stone_carrier(self):
        p = self.case(64)
        p['teamOur']['roles'][1]['backpack'] = ['stone']
        agent = Agent(Config(llm_enabled=False))
        agent.decide(p)
        self.assertEqual(next(iter(agent.sessions.values())).gunner_id, 1)


    def blocked_match(self):
        p = payload(467, [unit(13, 'station', 30, 10, health=1500),
            unit(20012, 'worker', 29, 8), unit(20010, 'worker', 36, 16),
            unit(20011, 'pioneer', 30, 11),
            unit(20040, 'rocket', 32, 10, level=3, attackRange=2147483647),
            unit(20041, 'rocket', 32, 8, level=3, attackRange=2147483647),
            unit(20042, 'rocket', 31, 8, level=3, attackRange=2147483647)])
        p['mapInfo'].update(width=40, height=24)
        walls = [(28, y) for y in range(7, 13)] + [(x, y) for x in (29, 30, 31) for y in (7, 12)]
        p['teamOur']['roles'] += [unit(100+i, 'wall', *point) for i, point in enumerate(walls)]
        p['robot']['roles'] = [unit(31138, 'largeRobot', 24, 8, health=500, targetTeam='challenger')]
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 36, 'y': 17}}]
        return p

    def advance_moves(self, p, commands):
        occupied = {tuple(h['pos'].values()) for h in p['teamOur']['roles'] if h['health'] > 0}
        destinations = set()
        for uid, c in commands.items():
            if c['action'] == 'move':
                dest = tuple(c['targetPos'][0].values())
                self.assertNotIn(dest, occupied)
                self.assertNotIn(dest, destinations)
                destinations.add(dest)
                next(h for h in p['teamOur']['roles'] if h['id'] == int(uid))['pos'] = c['targetPos'][0]
        p['roundNo'] += 1

    def test_match_choke_pioneer_takes_over_and_fires(self):
        p = self.blocked_match()
        agent = Agent(Config(llm_enabled=False))
        for _ in range(8):
            commands = agent.decide(p)['roleCommandMap']
            if any(c.get('controllerId') == '20011' for c in commands.values()):
                break
            self.advance_moves(p, commands)
        else:
            self.fail('living blocked gunner left all three ready rockets idle')

    def test_idle_ally_clears_return_corridor_without_collision(self):
        from agent.combat import clear_gunner_route
        from agent.intelligence import Memory
        p = self.blocked_match()
        turn, cfg, nav, ledger = setup_case(p)
        mem = Memory(gunner_id=20012, gunner_post=(32, 9))
        gunner = next(h for h in turn.heroes if h.id == 20012)
        corridor = clear_gunner_route(turn, nav, ledger, mem, [(gunner, turn.weapons[0])])
        self.assertIn((30, 11), corridor)
        self.assertEqual(ledger.commands['20011']['action'], 'move')
        self.assertNotIn(tuple(ledger.commands['20011']['targetPos'][0].values()), corridor)
        self.advance_moves(p, ledger.commands)

    def test_sole_pioneer_can_operate_when_workers_dead(self):
        p = self.case()
        for h in p['teamOur']['roles'][1:3]:
            h['health'] = 0
        p['teamOur']['roles'].append(unit(3, 'pioneer', 4, 11))
        commands = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertTrue(any(c.get('controllerId') == '3' for c in commands.values()))

    def test_fire_adjacent_gun_before_reaching_common_post(self):
        p = self.case()
        p['teamOur']['roles'][1]['pos'] = {'x': 3, 'y': 9}
        commands = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertEqual(commands['22']['controllerId'], '1')

    def test_repeated_failed_moves_recall_miner(self):
        p = self.case()
        p['teamOur']['roles'][1]['pos'] = {'x': 1, 'y': 11}
        agent = Agent(Config(llm_enabled=False))
        for r in range(70, 73):
            p['roundNo'] = r
            commands = agent.decide(p)['roleCommandMap']
        self.assertEqual(next(iter(agent.sessions.values())).gunner_id, 2)
        self.assertEqual(commands['2']['action'], 'move')
