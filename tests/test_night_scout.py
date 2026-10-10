"""Finite observed-state checks for one night observer and role isolation."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import controlled_robot
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.model import distance, pos
from agent.scouting import ScoutMemory, act_night_scout
from agent.robot_assault import RobotAssaultMemory, act_robots
from agent.wall_watch import select_watch
from agent.intelligence import Memory


def scout_case(round_no=71):
    data = payload(round_no, [unit(13, 'station', 3, 11),
                             unit(10, 'worker', 5, 8, health=500),
                             unit(12, 'worker', 2, 10, health=500),
                             unit(11, 'pioneer', 2, 11, health=500)])
    data['teamEnemy']['roles'] = [unit(99, 'station', 10, 4, health=1500)]
    return data


def decide(data, memory=None, home_roles=(), assault=False):
    turn, _, nav, ledger = setup_case(data, layout_mode='explicit')
    memory = memory if memory is not None else ScoutMemory()
    if assault:
        act_robots(turn, RobotAssaultMemory(), nav, ledger)
    scout = act_night_scout(turn, memory, nav, ledger, home_roles=home_roles)
    return scout, ledger, memory, turn


class NightScoutTests(unittest.TestCase):
    def test_one_worker_approaches_and_holds_shared_vision(self):
        data = scout_case()
        scout, ledger, memory, turn = decide(data)
        self.assertEqual(scout.id, 10)
        self.assertEqual(set(ledger.commands), {'10'})
        step = pos(ledger.commands['10']['targetPos'][0])
        self.assertEqual(distance(scout.pos, step), 1)
        self.assertNotIn(step, turn.blocked)
        post = memory.post
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['pos'] = {'x': post[0], 'y': post[1]}
        scout, ledger, memory, turn = decide(data, memory)
        self.assertNotIn('10', ledger.commands)
        self.assertIn(10, ledger.used)
        self.assertEqual(ledger.notes[10]['reason'], 'night_scout_hold')
        self.assertTrue(all(distance(scout.pos, p) <= 4 for p in turn.enemies[0].cells))

    def test_owner_stays_until_death_then_another_worker_takes_over(self):
        data = scout_case()
        _, _, memory, _ = decide(data)
        data['roundNo'] += 1
        data['teamOur']['roles'][2]['pos'] = {'x': 8, 'y': 8}
        scout, _, _, _ = decide(data, memory)
        self.assertEqual(scout.id, 10)
        data['teamOur']['roles'][1]['health'] = 0
        data['roundNo'] += 1
        scout, _, _, _ = decide(data, memory)
        self.assertEqual(scout.id, 12)

    def test_home_worker_is_preserved_when_another_worker_is_available(self):
        scout, _, _, _ = decide(scout_case(), home_roles={10})
        self.assertEqual(scout.id, 12)

    def test_gunner_is_preserved_before_wall_watch_when_both_workers_have_roles(self):
        scout, _, _, _ = decide(scout_case(), home_roles=(10, 12))
        self.assertEqual(scout.id, 12)

    def test_daybreak_or_enemy_base_death_releases_the_observer(self):
        for end in ('day', 'dead', 'missing'):
            with self.subTest(end=end):
                data = scout_case()
                _, _, memory, _ = decide(data)
                if end == 'day':
                    data['roundNo'] = 130
                elif end == 'dead':
                    data['teamEnemy']['roles'][0]['health'] = 0
                else:
                    data['teamEnemy']['roles'] = []
                scout, ledger, memory, _ = decide(data, memory)
                self.assertIsNone(scout)
                self.assertIsNone(memory.worker_id)
                self.assertFalse(ledger.used)

    def test_only_workers_are_eligible_and_dizzy_observer_waits(self):
        data = scout_case()
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['roleType'] != 'worker']
        self.assertIsNone(decide(data)[0])
        data = scout_case()
        data['teamOur']['roles'][1]['abnormalState'] = 'dizzy'
        scout, ledger, _, _ = decide(data)
        self.assertEqual(scout.id, 10)
        self.assertFalse(ledger.commands)
        self.assertIn(10, ledger.used)

    def test_healing_uses_one_action_and_keeps_the_role(self):
        data = scout_case()
        data['teamOur']['roles'][1].update(health=100, backpack=['Medicine'])
        scout, ledger, memory, _ = decide(data)
        self.assertEqual(ledger.commands['10'], command('use', name='Medicine'))
        self.assertEqual(memory.worker_id, scout.id)

    def test_scout_avoids_planned_robot_attack_ray_and_does_not_change_observation(self):
        data = scout_case()
        data['teamOur']['summonRobotList'] = [controlled_robot(x=7, y=4)]
        before = copy.deepcopy(data)
        scout, ledger, memory, turn = decide(data, assault=True)
        self.assertEqual(ledger.commands['30000']['action'], 'attack')
        self.assertEqual(data, before)
        self.assertNotIn(memory.post, {(8, 4), (9, 4), (10, 4)})
        if str(scout.id) in ledger.commands:
            self.assertNotIn(pos(ledger.commands[str(scout.id)]['targetPos'][0]),
                             {(8, 4), (9, 4), (10, 4)})
        self.assertNotIn(memory.post, turn.blocked)

    def test_unknown_robots_constrain_routes_but_owned_robots_do_not_create_danger(self):
        data = scout_case()
        data['teamOur']['roles'][1]['pos'] = {'x': 6, 'y': 10}
        data['robot']['roles'] = [controlled_robot(30001, 9, 4, targetTeam='challenger')]
        scout, ledger, memory, turn = decide(data)
        if ledger.commands.get(str(scout.id), {}).get('action') == 'move':
            step = pos(ledger.commands[str(scout.id)]['targetPos'][0])
            self.assertGreater(distance(step, (9, 4)), 4)
        self.assertFalse((6, 4) in turn.blocked)
        owned = scout_case()
        owned['robot']['roles'] = [controlled_robot(x=7, y=4)]
        owned['teamOur']['summonRobotList'] = copy.deepcopy(owned['robot']['roles'])
        self.assertIsNotNone(decide(owned, assault=True)[2].post)

    def test_unreachable_post_keeps_one_observer_without_invalid_movement(self):
        data = scout_case()
        data['teamOur']['roles'] += [unit(100+i, 'wall', x, y) for i, (x, y) in enumerate(
            [(4, 7), (5, 7), (6, 7), (4, 8), (6, 8), (4, 9), (5, 9), (6, 9)])]
        scout, ledger, _, _ = decide(data)
        self.assertEqual(scout.id, 10)
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.used, {10})
        self.assertEqual(ledger.notes[10]['reason'], 'night_scout_route_blocked')

    def test_agent_preserves_observer_and_other_worker_while_pioneer_fires(self):
        from test_shared_gunner import SharedGunnerTests
        data = SharedGunnerTests().case(80)
        data['teamOur']['roles'].append(unit(11, 'pioneer', 4, 11, health=500))
        data['teamEnemy']['roles'] = [unit(99, 'station', 10, 4)]
        agent = Agent(Config(llm_enabled=False))
        response = agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        uid = mem.night_scout.worker_id
        self.assertIn(uid, (1, 2))
        self.assertEqual(response, agent.decide(copy.deepcopy(data)))
        self.assertFalse(any(c.get('controllerId') == str(uid) for c in response['roleCommandMap'].values()))
        self.assertEqual(mem.gunner_id, 11)
        self.assertIn(response['roleCommandMap'][str(uid)]['action'], ('move', 'use'))

    def test_wall_watch_does_not_take_the_scout(self):
        data = scout_case(460)
        turn, _, _, _ = setup_case(data)
        mem = Memory(wall_watch_id=10)
        self.assertEqual(select_watch(turn, mem, [], excluded={10}).id, 12)

    def test_scout_selection_and_coverage_work_in_four_map_orientations(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = scout_case()
                for team in ('teamOur', 'teamEnemy'):
                    for role in data[team]['roles']:
                        x, y = role['pos']['x'], role['pos']['y']
                        station = role['roleType'] == 'station'
                        role['pos'] = {'x': 14-x-int(station) if flip_x else x,
                                       'y': 14-y+int(station) if flip_y else y}
                scout, ledger, memory, turn = decide(data)
                self.assertIsNotNone(memory.post)
                self.assertTrue(all(distance(memory.post, p) <= 4 for p in turn.enemies[0].cells))
                self.assertEqual(len(ledger.commands), 1)


if __name__ == '__main__':
    unittest.main()
