"""Adjacent interception boundaries and one-turn work continuity, without replays."""
import copy
import unittest
from unittest.mock import patch

from test_agent import payload, setup_case, unit
from test_gate_guard import gate_case
from agent.brain import Agent
from agent.combat import catch_nearby_imp, IMP_CATCH_INTERVAL
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, neighbours
from agent.worker_jobs import resume_daytime_jobs, save_daytime_jobs


class ImpCatchTests(unittest.TestCase):
    def case(self, enemy=(5, 4), roles=None, round_no=10):
        data = payload(round_no, roles if roles is not None else [unit(1, 'worker', 4, 4)])
        data['teamEnemy']['roles'] = [unit(99, 'imp', *enemy, health=500)]
        return data

    def setup(self, data):
        return setup_case(data, layout_mode='explicit', llm_enabled=False)

    def collect(self, turn, ledger, hero=None, want_stone=False):
        hero = hero or turn.workers[0]
        self.assertTrue(ledger.add(hero.id, command('collect', (4, 5))))
        ledger.mine_claims[hero.id] = (4, 5)
        ledger.remember_work(hero, 'mine', (4, 5), ore='copper', want_stone=want_stone,
                             stockpile=False, deadline=40, stone_goal=None)

    def test_all_eight_neighbours_can_be_caught_without_movement(self):
        for point in neighbours((4, 4)):
            with self.subTest(point=point):
                turn, _, _, ledger = self.setup(self.case(point))
                mem = Memory()
                self.assertTrue(catch_nearby_imp(turn, mem, ledger))
                self.assertEqual(ledger.commands, {'1': command('catch', point)})
                self.assertEqual(ledger.gold, turn.gold)  # No speculative +20 reward.
                self.assertEqual(ledger.reserved, set())

    def test_distance_two_does_not_generate_catch_or_pursuit(self):
        turn, _, _, ledger = self.setup(self.case((6, 4)))
        mem = Memory()
        self.assertFalse(catch_nearby_imp(turn, mem, ledger))
        self.assertFalse(ledger.commands)
        self.assertFalse(mem.imp_catch_attempted)

    def test_dead_friendly_and_other_enemy_types_are_not_targets(self):
        data = self.case()
        data['teamOur']['roles'].append(unit(14, 'imp', 3, 4))
        for kind, health in (('imp', 0), ('worker', 500), ('rocket', 1000)):
            with self.subTest(kind=kind, health=health):
                data['teamEnemy']['roles'] = [unit(99, kind, 5, 4, health=health)]
                turn, _, _, ledger = self.setup(data)
                self.assertFalse(catch_nearby_imp(turn, Memory(), ledger))
                self.assertFalse(ledger.commands)

    def test_idle_hero_is_preferred_over_a_working_miner(self):
        data = self.case(roles=[unit(1, 'worker', 4, 4), unit(11, 'pioneer', 5, 3)])
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=4, y=5))]
        turn, _, _, ledger = self.setup(data)
        self.collect(turn, ledger)
        self.assertTrue(catch_nearby_imp(turn, Memory(), ledger))
        self.assertEqual(ledger.commands['1']['action'], 'collect')
        self.assertEqual(ledger.commands['11'], command('catch', (5, 4)))

    def test_two_nearby_miners_submit_only_one_catch(self):
        data = self.case(roles=[unit(2, 'worker', 3, 4), unit(1, 'worker', 4, 4)], enemy=(4, 3))
        turn, _, _, ledger = self.setup(data)
        self.assertTrue(catch_nearby_imp(turn, Memory(), ledger))
        self.assertEqual(ledger.commands, {'1': command('catch', (4, 3))})

    def test_failed_catch_is_not_retried_during_continuous_contact(self):
        data, mem = self.case(), Memory()
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=4, y=5))]
        turn, _, _, ledger = self.setup(data)
        self.collect(turn, ledger)
        self.assertTrue(catch_nearby_imp(turn, mem, ledger))
        data['lastRoundRoleActionResults'] = {'1': False}
        for round_no in (11, 10 + IMP_CATCH_INTERVAL, 40):
            data['roundNo'] = round_no
            turn, _, _, ledger = self.setup(data)
            self.collect(turn, ledger)
            self.assertFalse(catch_nearby_imp(turn, mem, ledger))
            self.assertEqual(ledger.commands['1'], command('collect', (4, 5)))

    def test_fleeing_target_clears_contact_and_does_not_change_work_move(self):
        data, mem = self.case(), Memory()
        turn, _, _, ledger = self.setup(data)
        self.assertTrue(catch_nearby_imp(turn, mem, ledger))
        data['roundNo'] += 1
        data['teamEnemy']['roles'][0]['pos'] = dict(x=6, y=4)
        turn, _, _, ledger = self.setup(data)
        self.assertTrue(ledger.add(1, command('move', (3, 3))))
        self.assertFalse(catch_nearby_imp(turn, mem, ledger))
        self.assertEqual(ledger.commands['1'], command('move', (3, 3)))
        self.assertFalse(mem.imp_catch_attempted)

    def test_invisible_target_is_not_caught_using_old_position(self):
        data, mem = self.case(), Memory()
        turn, _, _, ledger = self.setup(data)
        self.assertTrue(catch_nearby_imp(turn, mem, ledger))
        data['roundNo'] += 1
        data['teamEnemy']['roles'] = []
        turn, _, _, ledger = self.setup(data)
        self.assertFalse(catch_nearby_imp(turn, mem, ledger))
        self.assertFalse(ledger.commands)
        self.assertFalse(mem.imp_catch_attempted)

    def test_new_contact_still_respects_team_interval(self):
        data, mem = self.case(), Memory()
        turn, _, _, ledger = self.setup(data)
        self.assertTrue(catch_nearby_imp(turn, mem, ledger))
        data['roundNo'] += 1
        data['teamEnemy']['roles'][0]['pos'] = dict(x=8, y=4)
        turn, _, _, ledger = self.setup(data)
        self.assertFalse(catch_nearby_imp(turn, mem, ledger))
        data['teamEnemy']['roles'][0]['pos'] = dict(x=5, y=4)
        for round_no, expected in ((12, False), (10 + IMP_CATCH_INTERVAL, True)):
            data['roundNo'] = round_no
            turn, _, _, ledger = self.setup(data)
            self.assertEqual(catch_nearby_imp(turn, mem, ledger), expected)

    def test_build_trade_use_and_movement_are_never_replaced(self):
        for action in ('build', 'buy', 'sell', 'use', 'move', 'submitAnswer', 'summonTreasure'):
            with self.subTest(action=action):
                turn, _, _, ledger = self.setup(self.case())
                original = dict(action=action, name='Medicine')
                ledger.commands['1'] = original.copy()
                ledger.used.add(1)
                self.assertFalse(catch_nearby_imp(turn, Memory(), ledger))
                self.assertEqual(ledger.commands['1'], original)

    def test_wall_material_collection_is_not_interrupted(self):
        data = self.case()
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=4, y=5))]
        turn, _, _, ledger = self.setup(data)
        self.collect(turn, ledger, want_stone=True)
        self.assertFalse(catch_nearby_imp(turn, Memory(), ledger))
        self.assertEqual(ledger.commands['1']['action'], 'collect')

    def test_recall_task_lock_and_low_health_take_priority(self):
        for mode in ('return_target', 'recall_plan', 'held', 'injured', 'active_task'):
            with self.subTest(mode=mode):
                data = self.case(roles=[unit(1, 'pioneer', 4, 4)])
                if mode == 'injured':
                    data['teamOur']['roles'][0]['health'] = 165
                if mode == 'active_task':
                    data['phaseTask'] = 'pending sandbox work'
                turn, _, _, ledger = self.setup(data)
                mem = Memory()
                if mode == 'return_target':
                    mem.return_targets[1] = 20
                if mode == 'recall_plan':
                    ledger.plans[1] = dict(reason='defence_recall')
                if mode == 'held':
                    ledger.used.add(1)
                self.assertFalse(catch_nearby_imp(turn, mem, ledger))
                self.assertFalse(ledger.commands)

    def test_our_imp_keeps_its_survival_action(self):
        turn, _, _, ledger = self.setup(self.case(roles=[unit(14, 'imp', 4, 4)]))
        self.assertTrue(ledger.add(14, command('move', (3, 3))))
        self.assertFalse(catch_nearby_imp(turn, Memory(), ledger))
        self.assertEqual(ledger.commands['14'], command('move', (3, 3)))

    def test_rejected_replacement_preserves_collect_command_and_lock(self):
        data = self.case()
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=4, y=5))]
        turn, _, _, ledger = self.setup(data)
        self.collect(turn, ledger)
        mem = Memory()
        with patch.object(ledger, 'add', return_value=False):
            self.assertFalse(catch_nearby_imp(turn, mem, ledger))
        self.assertEqual(ledger.commands['1'], command('collect', (4, 5)))
        self.assertIn(1, ledger.used)
        self.assertFalse(mem.imp_catch_attempted)
        self.assertEqual(mem.imp_catch_next_round, 0)

    def test_mining_job_survives_catch_and_resumes_same_mine(self):
        data = self.case(roles=[unit(13, 'station', 1, 12, health=1500),
                                unit(1, 'worker', 4, 4, health=500)])
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=4, y=5), remain=10)]
        data['vendorShopList'] = [dict(name='copper', price=3)]
        turn, cfg, _, ledger = self.setup(data)
        mem = Memory(day=1, preparation_tick=40, mine_targets={1: (4, 5)})
        mem.observe(turn, cfg)
        self.collect(turn, ledger)
        mem.mine_targets[1] = (4, 5)  # Set when the real miner accepts this job.
        job = copy.deepcopy(ledger.work_jobs[1])
        self.assertTrue(catch_nearby_imp(turn, mem, ledger))
        save_daytime_jobs(turn, mem, ledger)
        self.assertEqual(mem.daytime_jobs[1], job)
        self.assertEqual(mem.mine_targets[1], (4, 5))
        mem.last_round, mem.last_commands = turn.round, ledger.commands.copy()
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'1': False}
        data['teamEnemy']['roles'][0]['pos'] = dict(x=8, y=4)
        turn, cfg, nav, ledger = self.setup(data)
        mem.observe(turn, cfg)
        self.assertFalse(mem.mine_collected)  # Failed catch is never collection.
        self.assertFalse(mem.collect_failures)
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, [], [], set())
        self.assertEqual(ledger.commands['1'], command('collect', (4, 5)))
        self.assertFalse(catch_nearby_imp(turn, mem, ledger))

    def test_agent_catches_adjacent_imp_and_caches_same_turn(self):
        data = self.case()
        agent = Agent(Config(layout_mode='explicit', llm_enabled=False))
        expected = {'1': command('catch', (5, 4))}
        self.assertEqual(agent.decide(data)['roleCommandMap'], expected)
        self.assertEqual(agent.decide(data)['roleCommandMap'], expected)

    def test_agent_miner_spends_one_turn_then_resumes_even_if_target_stays(self):
        data = self.case(roles=[unit(13, 'station', 5, 11, health=1500),
                                unit(1, 'worker', 4, 4, health=500)])
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=4, y=5), remain=10)]
        data['vendorShopList'] = [dict(name='copper', price=3)]
        agent = Agent(Config(layout_mode='explicit', llm_enabled=False))
        self.assertEqual(agent.decide(data)['roleCommandMap']['1'], command('catch', (5, 4)))
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'1': False}
        self.assertEqual(agent.decide(data)['roleCommandMap']['1'], command('collect', (4, 5)))

    def test_default_night_keeps_rocket_fire_while_nearby_worker_catches(self):
        data = gate_case(worker=(11, 2))
        data['teamEnemy']['roles'] = [unit(99, 'imp', 10, 2, health=500)]
        data['robot']['roles'] = [unit(90, 'largeRobot', 8, 10, health=500,
                                      targetTeam='challenger', attackRange=3)]
        commands = Agent(Config(llm_enabled=False)).decide(data)['roleCommandMap']
        self.assertEqual(commands['1'], command('catch', (10, 2)))
        shots = [cmd for cmd in commands.values() if cmd['action'] == 'attack']
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]['controllerId'], '11')
        self.assertNotIn('11', commands)


if __name__ == '__main__':
    unittest.main()
