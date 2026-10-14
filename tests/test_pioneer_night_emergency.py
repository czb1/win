"""Night-only base-voucher rescue and persistent, collision-safe gun handoff."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.combat import relieve_night_pioneer
from agent.config import Config
from agent.intelligence import Memory
from agent.spending import heal_station_with_pioneer


class PioneerNightEmergencyTests(unittest.TestCase):
    def case(self, health=150, round_no=200, level=1):
        data = payload(round_no, [
            unit(13, 'station', 5, 11, level=level, health=1500 * level),
            unit(1, 'worker', 11, 3, health=500),
            unit(2, 'worker', 11, 2, health=500),
            unit(11, 'pioneer', 4, 11, health=health,
                 backpack=[f'StationUpgradeVoucher{level}']),
            unit(20, 'rocket', 4, 12, level=3),
            unit(21, 'rocket', 5, 12, level=3),
            unit(22, 'rocket', 4, 10, level=3),
        ])
        data['teamOur']['goldNum'] = 0
        data['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 12, 'y': 2}}]
        data['robot']['roles'] = [unit(90, 'smallRobot', 8, 10, health=500,
                                     attackRange=1, targetTeam='challenger')]
        return data

    def hero(self, data, uid=11):
        return next(r for r in data['teamOur']['roles'] if r['id'] == uid)

    def advance(self, data, commands):
        occupied = {tuple(r['pos'].values()) for r in data['teamOur']['roles'] if r['health'] > 0}
        destinations = set()
        for uid, cmd in commands.items():
            if cmd['action'] == 'move':
                target = tuple(cmd['targetPos'][0].values())
                self.assertNotIn(target, occupied)
                self.assertNotIn(target, destinations)
                destinations.add(target)
                self.hero(data, int(uid))['pos'] = cmd['targetPos'][0]
        data['roundNo'] += 1

    def test_night_health_boundary_and_both_base_voucher_levels(self):
        for health in (1, 149, 150, 151, 500):
            for round_no in (69, 70, 129, 130, 200):
                for level in (1, 2):
                    with self.subTest(health=health, round_no=round_no, level=level):
                        turn, _, nav, ledger = setup_case(self.case(health, round_no, level))
                        expected = not turn.is_day and health <= 150
                        self.assertEqual(heal_station_with_pioneer(turn, Memory(), nav, ledger), expected)
                        self.assertEqual(ledger.commands.get('11'), command(
                            'use', (5, 11), name=f'StationUpgradeVoucher{level}') if expected else None)

    def test_emergency_still_requires_live_base_correct_voucher_and_free_action(self):
        for condition in ('no_voucher', 'wrong_voucher', 'max_level', 'claimed',
                          'dead_base', 'dead_pioneer', 'already_used'):
            with self.subTest(condition=condition):
                data = self.case()
                if condition == 'no_voucher':
                    self.hero(data)['backpack'] = []
                elif condition == 'wrong_voucher':
                    self.hero(data)['backpack'] = ['StationUpgradeVoucher2']
                elif condition == 'max_level':
                    self.hero(data, 13)['level'] = 3
                elif condition == 'dead_base':
                    self.hero(data, 13)['health'] = 0
                elif condition == 'dead_pioneer':
                    self.hero(data)['health'] = 0
                turn, _, nav, ledger = setup_case(data)
                if condition == 'claimed':
                    ledger.upgrade_claims.add(13)
                elif condition == 'already_used':
                    ledger.used.add(11)
                self.assertFalse(heal_station_with_pioneer(turn, Memory(), nav, ledger))
                self.assertFalse(ledger.commands)

    def test_emergency_walks_into_range_and_does_not_issue_remote_use(self):
        data = self.case()
        self.hero(data)['pos'] = {'x': 8, 'y': 5}
        turn, _, nav, ledger = setup_case(data)
        self.assertTrue(heal_station_with_pioneer(turn, Memory(), nav, ledger))
        self.assertEqual(ledger.commands['11']['action'], 'move')
        target = tuple(ledger.commands['11']['targetPos'][0].values())
        self.assertLess(turn.base_distance(target), turn.base_distance(turn.pioneer.pos))
        self.assertIn(13, ledger.upgrade_claims)

    def test_unreachable_base_does_not_submit_an_illegal_use(self):
        data = self.case()
        self.hero(data)['pos'] = {'x': 1, 'y': 1}
        data['teamEnemy']['roles'] = [unit(100 + x * 3 + y, 'wall', x, y)
                                    for x in range(3) for y in range(3) if (x, y) != (1, 1)]
        turn, _, nav, ledger = setup_case(data)
        self.assertFalse(heal_station_with_pioneer(turn, Memory(), nav, ledger))
        self.assertFalse(ledger.commands)

    def test_base_heal_preempts_medicine_task_and_attacks(self):
        data = self.case(health=50)
        self.hero(data)['backpack'] += ['Medicine', 'Bomb', 'BossRobotSummonOrder']
        data['phaseTask'] = 'unfinished task'
        agent = Agent(Config(llm_enabled=False))
        result = agent.decide(data)
        commands = result['roleCommandMap']
        self.assertEqual(commands['11'], command('use', (5, 11), name='StationUpgradeVoucher1'))
        self.assertFalse(any(c.get('controllerId') == '11' for c in commands.values()))
        self.assertEqual(agent.decide(copy.deepcopy(data)), result)

    def test_free_worker_returns_and_keeps_the_gun_after_voucher_consumption_or_death(self):
        for dead in (False, True):
            with self.subTest(dead=dead):
                data = self.case()
                data['roundNo'] += 20
                agent = Agent(Config(llm_enabled=False))
                commands = agent.decide(data)['roleCommandMap']
                mem = next(iter(agent.sessions.values()))
                worker = mem.pioneer_relief['worker_id']
                self.assertIn(worker, (1, 2))
                self.assertEqual(commands[str(worker)]['action'], 'move')
                self.assertEqual(commands[str(3 - worker)]['action'], 'collect')
                self.advance(data, commands)
                self.hero(data)['backpack'] = []
                self.hero(data, 13).update(level=2, health=3000)
                if dead:
                    self.hero(data)['health'] = 0
                for _ in range(14):
                    commands = agent.decide(data)['roleCommandMap']
                    self.assertEqual(mem.gunner_id, worker)
                    self.assertFalse(any(c.get('controllerId') == '11' for c in commands.values()))
                    if any(c.get('controllerId') == str(worker) for c in commands.values()):
                        break
                    self.advance(data, commands)
                else:
                    self.fail('The replacement worker never reached a firing position')

    def test_nearby_worker_fires_while_pioneer_uses_voucher(self):
        data = self.case()
        self.hero(data, 1)['pos'] = {'x': 3, 'y': 11}
        commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['11']['name'], 'StationUpgradeVoucher1')
        self.assertTrue(any(c.get('controllerId') == '1' for c in commands.values()))
        self.assertNotIn('1', commands)

    def test_relief_does_not_require_a_voucher_or_an_immediate_enemy(self):
        data = self.case()
        self.hero(data)['backpack'] = []
        data['robot']['roles'] = []
        agent = Agent(Config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(commands[str(mem.pioneer_relief['worker_id'])]['action'], 'move')
        self.assertNotEqual(mem.gunner_id, 11)

    def test_no_free_worker_still_uses_the_base_voucher(self):
        data = self.case()
        for uid in (1, 2):
            self.hero(data, uid)['health'] = 0
        agent = Agent(Config(llm_enabled=False))
        commands = agent.decide(data)['roleCommandMap']
        self.assertEqual(commands['11']['name'], 'StationUpgradeVoucher1')
        self.assertFalse(next(iter(agent.sessions.values())).pioneer_relief)

    def test_watch_worker_is_not_taken_from_its_existing_post(self):
        data = self.case(health=151, round_no=460)
        agent = Agent(Config(llm_enabled=False))
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        watcher = mem.wall_watch_id
        self.assertIn(watcher, (1, 2))
        data['roundNo'] += 1
        self.hero(data)['health'] = 150
        agent.decide(data)
        self.assertNotEqual(mem.pioneer_relief['worker_id'], watcher)
        self.assertEqual(mem.wall_watch_id, watcher)

    def test_recovery_above_150_restores_original_pioneer_assignment(self):
        data = self.case()
        agent = Agent(Config(llm_enabled=False))
        agent.decide(data)
        data['roundNo'] += 1
        self.hero(data)['health'] = 151
        commands = agent.decide(data)['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertFalse(mem.pioneer_relief)
        self.assertEqual(mem.gunner_id, 11)
        self.assertTrue(any(c.get('controllerId') == '11' for c in commands.values()))
        self.assertNotIn('11', commands)

    def test_relief_skips_busy_workers_and_retains_the_original_tower(self):
        data = self.case()
        turn, _, nav, ledger = setup_case(data)
        ledger.used.add(1)
        mem = Memory(return_targets={11: 22})
        worker, tower = relieve_night_pioneer(turn, mem, nav, ledger)
        self.assertEqual((worker.id, tower.id), (2, 22))

    def test_non_shared_layout_recalls_worker_to_the_pioneers_tower(self):
        data = self.case(health=151)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] not in (21, 22)]
        self.hero(data, 20).update(roleType='railgun', pos={'x': 3, 'y': 11})
        data['robot']['roles'] = []
        agent = Agent(Config(llm_enabled=False))
        agent.decide(data)
        data['roundNo'] += 1
        self.hero(data)['health'] = 150
        commands = agent.decide(data)['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        worker = mem.pioneer_relief['worker_id']
        self.assertEqual(mem.return_targets[worker], 20)
        self.assertEqual(commands[str(worker)]['action'], 'move')

    def test_mixed_loadout_keeps_its_gatling_operator_and_uses_the_free_worker(self):
        data = self.case(health=151)
        self.hero(data, 22)['roleType'] = 'gatling'
        agent = Agent(Config(llm_enabled=False, loadout=('rocket', 'rocket', 'gatling')))
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        gatling_worker = mem.gatling_operator_id
        data['roundNo'] += 1
        self.hero(data)['health'] = 150
        commands = agent.decide(data)['roleCommandMap']
        self.assertNotEqual(mem.pioneer_relief['worker_id'], gatling_worker)
        self.assertEqual(mem.gatling_operator_id, gatling_worker)
        self.assertEqual(commands[str(mem.pioneer_relief['worker_id'])]['action'], 'move')

    def test_first_observation_prefers_the_worker_without_wall_repair_supplies(self):
        data = self.case(round_no=460)
        self.hero(data, 1).update(pos={'x': 3, 'y': 11}, backpack=['WallFixer'])
        agent = Agent(Config(llm_enabled=False))
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.pioneer_relief['worker_id'], 2)
        self.assertEqual(mem.wall_watch_id, 1)

    def test_first_night_scout_loss_does_not_steal_the_replacement_gunner(self):
        data = self.case(round_no=70)
        data['teamEnemy']['roles'] = [unit(50, 'station', 12, 5, health=1500)]
        agent = Agent(Config(llm_enabled=False))
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        worker = mem.pioneer_relief['worker_id']
        scout = mem.night_scout.worker_id
        self.assertNotEqual(worker, scout)
        self.hero(data, scout)['health'] = 0
        self.hero(data)['backpack'] = []
        data['roundNo'] += 1
        agent.decide(data)
        self.assertEqual(mem.gunner_id, worker)
        self.assertIsNone(mem.night_scout.worker_id)

    def test_dawn_clears_relief_and_healthy_night_does_not_start_it(self):
        for health in (151, 500):
            data = self.case(health=health)
            agent = Agent(Config(llm_enabled=False))
            commands = agent.decide(data)['roleCommandMap']
            mem = next(iter(agent.sessions.values()))
            self.assertFalse(mem.pioneer_relief)
            self.assertEqual(mem.gunner_id, 11)
            self.assertTrue(any(c.get('controllerId') == '11' for c in commands.values()))
        data = self.case(round_no=259)
        agent = Agent(Config(llm_enabled=False))
        agent.decide(data)
        data['roundNo'] = 260
        agent.decide(data)
        self.assertFalse(next(iter(agent.sessions.values())).pioneer_relief)


if __name__ == '__main__':
    unittest.main()
