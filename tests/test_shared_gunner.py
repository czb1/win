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

    def test_all_ready_guns_follow_persistent_rotation(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        for step, expected in enumerate((20, 21, 22, 20)):
            p['roundNo'] = 70 + step
            commands = agent.decide(p)['roleCommandMap']
            self.assertEqual([uid for uid, c in commands.items() if c['action'] == 'attack'],
                             [str(expected)])

    def test_full_miner_yields_the_reserved_post(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.case()
        agent.decide(p)
        p['roundNo'] += 1
        p['teamOur']['roles'][1]['pos'] = {'x': 2, 'y': 11}
        p['teamOur']['roles'][2].update(pos={'x': 4, 'y': 11}, backpack=['copper'] * 100)
        commands = agent.decide(p)['roleCommandMap']
        self.assertEqual(commands['2']['action'], 'move')
        self.assertNotEqual(commands['2']['targetPos'], [{'x': 4, 'y': 11}])

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
