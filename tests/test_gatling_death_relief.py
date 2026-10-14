"""Finite observations for scout-safe and boss-wave gatling handoffs."""
import unittest
from unittest.mock import patch

from test_agent import unit
from test_mixed_battery import mixed_case, mixed_config
from agent.brain import Agent
from agent.intelligence import Memory
from agent.model import Turn, distance, pos
from agent.scouting import ScoutMemory


def role(data, uid):
    return next(r for r in data['teamOur']['roles'] if r['id'] == uid)


def first_night():
    data = mixed_case(80)
    data['teamOur']['goldNum'] = 0
    data['teamEnemy']['roles'] = [unit(50, 'station', 10, 2, health=1500)]
    role(data, 2)['pos'] = dict(x=12, y=5)
    return data


def seed(data, scout=False):
    agent = Agent(mixed_config(llm_enabled=False))
    turn = Turn(data, agent.cfg)
    mem = Memory(day=turn.day, last_round=turn.round - 1,
                 gatling_operator_id=1, wall_watch_id=2 if turn.day >= 4 else None,
                 gunner_id=11, gunner_post=(4, 11))
    if scout:
        mem.night_scout = ScoutMemory(key=(1, 50, (10, 2)), worker_id=2, post=(12, 5))
    agent.sessions[(*turn.key, turn.station.pos)] = mem
    return agent, mem


def boss_night(round_no=470):
    data = mixed_case(round_no)
    data['teamOur']['goldNum'] = 0
    role(data, 2)['backpack'] = ['WallFixer'] * 3
    data['robot']['roles'] = [unit(90 + i, 'bossRobot', 2, 10 - i * 2,
        health=800, attackRange=3, targetTeam='challenger') for i in range(2)]
    return data


class GatlingDeathReliefTests(unittest.TestCase):
    def advance_moves(self, data, commands):
        occupied = {pos(r['pos']) for r in data['teamOur']['roles'] if r['health'] > 0}
        destinations = set()
        for uid, cmd in commands.items():
            if cmd['action'] != 'move':
                continue
            target = pos(cmd['targetPos'][0])
            self.assertNotIn(target, occupied)
            self.assertNotIn(target, destinations)
            destinations.add(target)
            role(data, int(uid))['pos'] = cmd['targetPos'][0]
        data['roundNo'] += 1

    def assert_pioneer_gatling(self, commands):
        self.assertEqual(commands['22']['controllerId'], '11')
        self.assertFalse(any(uid in commands for uid in ('20', '21')))

    def test_first_night_death_preserves_scout_and_transfers_fire(self):
        data = first_night()
        agent, mem = seed(data, scout=True)
        self.assertEqual(agent.decide(data)['roleCommandMap']['22']['controllerId'], '1')
        role(data, 1)['health'] = 0
        data['roundNo'] += 1
        for _ in range(2):
            commands = agent.decide(data)['roleCommandMap']
            self.assert_pioneer_gatling(commands)
            self.assertEqual(mem.night_scout.worker_id, 2)
            self.assertNotIn(2, mem.return_targets)
            self.assertIsNone(mem.gatling_operator_id)
            self.advance_moves(data, commands)

    def test_first_night_pioneer_moves_then_fires_without_rocket_rotation(self):
        data = first_night()
        role(data, 1)['health'] = 0
        role(data, 11)['pos'] = dict(x=5, y=13)
        agent, mem = seed(data, scout=True)
        fired = False
        post = None
        for _ in range(6):
            commands = agent.decide(data)['roleCommandMap']
            self.assertFalse(any(uid in commands for uid in ('20', '21')))
            if post is not None:
                self.assertEqual(mem.return_posts[11], post)
            post = mem.return_posts[11]
            if '22' in commands:
                self.assert_pioneer_gatling(commands)
                fired = True
                break
            self.assertEqual(commands['11']['action'], 'move')
            self.assertEqual(mem.night_scout.worker_id, 2)
            self.advance_moves(data, commands)
        self.assertTrue(fired)

    def test_base_voucher_preempts_handoff_and_firing_resumes_after_observed_heal(self):
        for level in (1, 2):
            with self.subTest(level=level):
                data = first_night()
                role(data, 1)['health'] = 0
                role(data, 13).update(health=499, level=level)
                name = f'StationUpgradeVoucher{level}'
                role(data, 11)['backpack'] = [name, 'Medicine']
                agent, mem = seed(data, scout=True)
                commands = agent.decide(data)['roleCommandMap']
                self.assertEqual(commands['11']['action'], 'use')
                self.assertEqual(commands['11']['name'], name)
                self.assertNotIn('22', commands)
                self.assertEqual(mem.night_scout.worker_id, 2)
                role(data, 13).update(health=1500 * (level + 1), level=level + 1)
                role(data, 11)['backpack'] = ['Medicine']
                data['roundNo'] += 1
                self.assert_pioneer_gatling(agent.decide(data)['roleCommandMap'])

    def test_base_heal_can_interrupt_an_existing_gatling_handoff(self):
        data = first_night()
        role(data, 1)['health'] = 0
        role(data, 11)['backpack'] = ['StationUpgradeVoucher1']
        agent, _ = seed(data, scout=True)
        self.assert_pioneer_gatling(agent.decide(data)['roleCommandMap'])
        role(data, 13)['health'] = 499
        data['roundNo'] += 1
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['11']['name'], 'StationUpgradeVoucher1')
        self.assertNotIn('22', commands)

    def test_remote_pioneer_finishes_base_heal_before_firing(self):
        data = first_night()
        role(data, 1)['health'] = 0
        role(data, 13)['health'] = 499
        role(data, 11).update(pos=dict(x=5, y=13), backpack=['StationUpgradeVoucher1'])
        agent, _ = seed(data, scout=True)
        used = False
        for _ in range(6):
            commands = agent.decide(data)['roleCommandMap']
            self.assertNotIn('22', commands)
            if commands['11']['action'] == 'use':
                self.assertEqual(commands['11']['name'], 'StationUpgradeVoucher1')
                used = True
                break
            self.assertEqual(commands['11']['action'], 'move')
            self.advance_moves(data, commands)
        self.assertTrue(used)

    def test_existing_base_and_pioneer_health_thresholds_are_preserved(self):
        for base_health, health, expected in ((499, 500, True), (500, 500, False),
                                               (1500, 150, True), (1500, 151, False)):
            with self.subTest(base_health=base_health, health=health):
                data = first_night()
                role(data, 1)['health'] = 0
                role(data, 13)['health'] = base_health
                role(data, 11).update(health=health, backpack=['StationUpgradeVoucher1'])
                agent, _ = seed(data, scout=True)
                commands = agent.decide(data)['roleCommandMap']
                if expected:
                    self.assertEqual(commands['11']['name'], 'StationUpgradeVoucher1')
                    self.assertNotIn('22', commands)
                else:
                    self.assert_pioneer_gatling(commands)

    def test_invalid_or_absent_base_voucher_does_not_prevent_takeover(self):
        for level, backpack in ((1, []), (1, ['StationUpgradeVoucher2']),
                                 (3, ['StationUpgradeVoucher1', 'StationUpgradeVoucher2'])):
            with self.subTest(level=level, backpack=backpack):
                data = first_night()
                role(data, 1)['health'] = 0
                role(data, 13).update(health=499, level=level)
                role(data, 11)['backpack'] = backpack
                agent, _ = seed(data, scout=True)
                self.assert_pioneer_gatling(agent.decide(data)['roleCommandMap'])

    def test_idle_worker_still_replaces_dead_gunner(self):
        data = mixed_case()
        role(data, 1)['health'] = 0
        agent, mem = seed(data)
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(mem.gatling_operator_id, 2)
        self.assertFalse(mem.gatling_relief)
        self.assertEqual(commands['20']['controllerId'], '11')

    def test_living_gunner_healing_and_destroyed_gun_do_not_take_pioneer(self):
        for case in ('healing', 'destroyed'):
            with self.subTest(case=case):
                data = first_night()
                if case == 'healing':
                    role(data, 1).update(health=50, backpack=['Medicine'])
                else:
                    role(data, 22)['health'] = 0
                agent, mem = seed(data, scout=True)
                commands = agent.decide(data)['roleCommandMap']
                self.assertEqual(commands['20']['controllerId'], '11')
                self.assertFalse(mem.gatling_relief)

    def test_dawn_releases_temporary_pioneer_and_its_recall_target(self):
        data = first_night()
        role(data, 1)['health'] = 0
        agent, mem = seed(data, scout=True)
        self.assert_pioneer_gatling(agent.decide(data)['roleCommandMap'])
        self.assertEqual(mem.return_targets[11], 22)
        data['roundNo'] = 130
        data['robot']['roles'] = []
        role(data, 1)['health'] = 500
        agent.decide(data)
        self.assertFalse(mem.gatling_relief)
        self.assertEqual(mem.gatling_operator_id, 1)
        self.assertNotIn(11, mem.return_targets)
        self.assertEqual(mem.gunner_id, 11)

    def test_fourth_night_repairer_is_recalled_even_when_already_mining(self):
        for mining in (False, True):
            with self.subTest(mining=mining):
                data = boss_night()
                role(data, 1)['health'] = 0
                agent, mem = seed(data)
                if mining:
                    role(data, 2)['backpack'] = []
                    mem.night_work[2] = dict(day=4, end_round=600, phase='mine', target=(12, 2))
                with patch('agent.night_economy.rockets_sufficient', return_value=True):
                    commands = agent.decide(data)['roleCommandMap']
                self.assertEqual(mem.gatling_operator_id, 2)
                self.assertIsNone(mem.wall_watch_id)
                self.assertEqual(mem.gatling_relief['reason'], 'boss_wave')
                self.assertNotIn(2, mem.night_work)
                self.assertEqual(commands['2']['action'], 'move')
                self.assertLess(distance(pos(commands['2']['targetPos'][0]), (4, 10)),
                                distance(pos(role(data, 2)['pos']), (4, 10)))
                self.assertTrue(any(commands.get(str(uid), {}).get('controllerId') == '11'
                                    for uid in (20, 21)))

    def test_boss_handoff_fires_and_stays_until_all_threats_clear(self):
        data = boss_night()
        role(data, 1)['health'] = 0
        role(data, 2)['pos'] = dict(x=4, y=9)
        agent, mem = seed(data)
        with patch('agent.night_economy.rockets_sufficient', return_value=True):
            for change in ('two', 'one', 'small', 'clear'):
                if change == 'one':
                    data['robot']['roles'][1]['health'] = 0
                elif change == 'small':
                    data['robot']['roles'][0]['roleType'] = 'smallRobot'
                elif change == 'clear':
                    data['robot']['roles'] = []
                commands = agent.decide(data)['roleCommandMap']
                if change != 'clear':
                    self.assertEqual(commands['22']['controllerId'], '2')
                    self.assertEqual(mem.gatling_relief['reason'], 'boss_wave')
                    self.assertNotIn(2, mem.night_work)
                else:
                    self.assertFalse(mem.gatling_relief)
                    self.assertIn(2, mem.night_work)
                data['roundNo'] += 1

    def test_boss_lock_respects_day_number_count_type_and_destination(self):
        for case in ('third_night', 'one_boss', 'large_robots', 'friendly', 'other_team', 'alive_gunner'):
            with self.subTest(case=case):
                data = boss_night(340 if case == 'third_night' else 470)
                role(data, 1)['health'] = 500 if case == 'alive_gunner' else 0
                robots = data['robot']['roles']
                if case == 'one_boss':
                    robots[1]['health'] = 0
                elif case == 'large_robots':
                    for robot in robots:
                        robot['roleType'] = 'largeRobot'
                elif case == 'friendly':
                    data['teamOur']['summonRobotList'] = [robots[1].copy()]
                elif case == 'other_team':
                    robots[1]['targetTeam'] = 'defender'
                agent, mem = seed(data)
                agent.decide(data)
                self.assertFalse(mem.gatling_relief)

    def test_boss_handoff_survives_dawn_without_revived_owner_stealing_gun(self):
        data = boss_night()
        role(data, 1)['health'] = 0
        agent, mem = seed(data)
        agent.decide(data)
        self.assertEqual(mem.gatling_relief['reason'], 'boss_wave')
        data['roundNo'] = 520
        data['robot']['roles'] = []
        role(data, 1)['health'] = 500
        agent.decide(data)
        self.assertFalse(mem.gatling_relief)
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (2, 1))


if __name__ == '__main__':
    unittest.main()
