"""Real worker regressions and weak-model fault isolation."""
import unittest
from collections import deque
from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.economy import workers, supplies, use_inventory
from agent.economy_plan import preparation_start
from agent.intelligence import Memory, Intelligence
from agent.mining import earn
from agent.model import Turn
from agent.recovery import Recovery


class WorkerRecoveryTests(unittest.TestCase):
    def case(self, tick=40, backpack=()):
        p = payload(tick, [unit(1, 'worker', 5, 5, backpack=list(backpack)),
                           unit(10, 'station', 3, 5, health=1000),
                           unit(20, 'rocket', 4, 7, level=3)])
        p['mapInfo']['zones'] = [
            {'neutralType': k, 'pos': {'x': x, 'y': y}}
            for k, x, y in [('vendor', 8, 5), ('weaponShop', 6, 6),
                            ('copper', 7, 5), ('iron', 5, 7)]]
        p['vendorShopList'] = [{'name': 'copper', 'price': 5}, {'name': 'iron', 'price': 3}]
        p['weaponShopList'] = [{'name': 'StationUpgradeVoucher1', 'price': 20}]
        return p

    def test_paid_station_voucher_precedes_sale_trip(self):
        p = self.case(41, backpack=['StationUpgradeVoucher1', 'copper'])
        # Not next to the shop, and the station is within use range.
        p['mapInfo']['zones'][1]['pos'] = {'x': 10, 'y': 10}
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(sale_workers={1})
        workers(t, cfg, mem, nav, ledger, [], [])
        self.assertEqual(ledger.commands['1'], command('use', (3, 5), name='StationUpgradeVoucher1'))

    def test_preparation_buys_affordable_base_when_weapon_too_expensive(self):
        p = self.case()
        p['teamOur']['roles'][2]['level'] = 1
        p['teamOur']['goldNum'] = 25
        p['weaponShopList'].append({'name': 'WeaponUpgradeVoucher1', 'price': 100})
        t, cfg, nav, ledger = setup_case(p)
        self.assertEqual(supplies(t, cfg, Memory(), nav, ledger, t.workers[0])[0],
                         'StationUpgradeVoucher1')
        ledger.gold = 25
        self.assertIsNone(supplies(t, cfg, Memory(), nav, ledger, t.workers[0], reserve=25))
        p['roundNo'] = 10
        t, cfg, nav, ledger = setup_case(p)
        self.assertIsNone(supplies(t, cfg, Memory(), nav, ledger, t.workers[0]))

    def test_base_only_upgrade_reserves_shop_travel(self):
        p = self.case(1)
        p['mapInfo']['zones'][1]['pos'] = {'x': 14, 'y': 14}
        t, cfg, nav, ledger = setup_case(p, economy_rounds=69)
        with_base = preparation_start(t, cfg, Memory(), nav, t.workers, [])
        p['teamOur']['roles'][1]['level'] = 3
        t, cfg, nav, ledger = setup_case(p, economy_rounds=69)
        self.assertLess(with_base, preparation_start(t, cfg, Memory(), nav, t.workers, []))

    def test_sold_worker_can_reach_nearby_ore_without_second_sale(self):
        t, cfg, nav, ledger = setup_case(self.case())
        self.assertTrue(earn(t, cfg, Memory(sold_workers={1}), nav, ledger, t.workers[0]))
        self.assertEqual(ledger.commands['1']['action'], 'move')

    def test_delivery_keeps_target_and_loop_releases_it(self):
        p = self.case(backpack=['StationUpgradeVoucher1', 'WeaponUpgradeVoucher1'])
        p['teamOur']['roles'][0]['pos'] = {'x': 9, 'y': 5}
        p['teamOur']['roles'][2]['level'] = 1
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(upgrade_targets={1: (3, 5)})
        self.assertTrue(use_inventory(t, nav, ledger, t.workers[0], mem=mem))
        self.assertEqual(mem.upgrade_targets[1], (3, 5))
        mem.last_round = t.round - 1
        mem.last_commands = {'1': command('move', (9, 5))}
        mem.movement.trails[1] = deque([(9, 5), (8, 5), (9, 5), (8, 5),
                                       (9, 5), (8, 5), (9, 5)], maxlen=8)
        mem.movement.observe(t, mem)
        self.assertIn(1, mem.movement.looped)
        self.assertNotIn(1, mem.upgrade_targets)
        self.assertTrue(mem.movement.avoids(1, (3, 5)))

    def test_recovery_fallback_and_async_model_choice(self):
        t, cfg, nav, ledger = setup_case(self.case(15))
        mem = Memory()
        mem.recovery.idle[1] = 2
        mem.recovery.recover(t, cfg, mem, nav, ledger, set())
        self.assertIn('1', ledger.commands)  # no waiting for model
        prompt = mem.recovery.prompt(Intelligence(t, cfg, mem))
        self.assertIn('"choice"', prompt)
        self.assertEqual(mem.pending, ('recovery', t.round))
        self.assertEqual(mem.calls, 1)
        p = self.case(16)
        p['llmResp'] = '{"choice":1}'
        mem.last_round = 15
        mem.observe(Turn(p, cfg), cfg)
        self.assertIsNone(mem.pending)
        self.assertEqual(mem.recovery.active[1][0][1], 'collect')
        self.assertIsNone(mem.python)
        self.assertIsNone(mem.answer)

    def test_bad_reply_preserves_rules_and_rejects_commands(self):
        goal = (1, 'collect', (7, 5))
        t, cfg, nav, ledger = setup_case(self.case())
        for reply in [None, {}, {'choice': True}, {'choice': -1}, {'choice': 20},
                      {'choice': '0'}, {'choice': 0, 'python': 'print(1)'}, {'action': 'move'}]:
            recovery = Recovery(active={1: (goal, 48)}, offered=[goal])
            recovery.accept(reply, t)
            self.assertEqual(recovery.active, {1: (goal, 48)})
            self.assertEqual(recovery.offered, [])

    def test_revalidate_removed_mine_and_defence_exclusion(self):
        t, cfg, nav, ledger = setup_case(self.case())
        mem = Memory()
        goal = (1, 'collect', (7, 5))
        mem.recovery.active[1] = (goal, 48)
        mem.recovery.resume(t, cfg, mem, nav, ledger, {1})
        self.assertFalse(ledger.commands)
        mem.recovery.active[1] = (goal, 48)
        t.zones.pop((7, 5))
        mem.recovery.resume(t, cfg, mem, nav, ledger, set())
        self.assertFalse(ledger.commands)
        self.assertFalse(mem.recovery.active)

    def test_no_model_during_task_cooldown_or_exhausted_quota(self):
        t, cfg, nav, ledger = setup_case(self.case())
        mem = Memory()
        mem.recovery.ready = [(1, 'collect', (7, 5)), (1, 'collect', (5, 7))]
        intel = Intelligence(t, cfg, mem)
        mem.calls = cfg.daily_llm_limit
        self.assertEqual(mem.recovery.prompt(intel), '')
        mem.calls = 0
        mem.recovery.next_call = t.round + 1
        self.assertEqual(mem.recovery.prompt(intel), '')
        mem.recovery.next_call = 0
        t.phase_task = 'question'
        self.assertEqual(mem.recovery.prompt(intel), '')

    def test_skipped_or_missing_reply_does_not_apply_model_choice(self):
        for round_no, reply in [(42, '{"choice":0}'), (41, ''), (41, 'nonsense')]:
            p = self.case(round_no)
            p['llmResp'] = reply
            t, cfg, nav, ledger = setup_case(p)
            mem = Memory(day=1, last_round=40, pending=('recovery', 40))
            mem.recovery.offered = [(1, 'collect', (7, 5))]
            mem.observe(t, cfg)
            self.assertIsNone(mem.pending)
            self.assertFalse(mem.recovery.active)

    def test_recovery_does_not_miss_night_or_approach_robots(self):
        goal = (1, 'collect', (7, 5))
        for tick in (68, 70):
            t, cfg, nav, ledger = setup_case(self.case(tick))
            mem = Memory()
            self.assertIsNone(mem.recovery.action(goal, t, cfg, mem, nav, ledger))
        p = self.case(20)
        p['robot']['roles'] = [unit(90, 'robot', 8, 8)]
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        self.assertIsNone(mem.recovery.action(goal, t, cfg, mem, nav, ledger))

    def test_recovery_does_not_preempt_assigned_wall_repair(self):
        t, cfg, nav, ledger = setup_case(self.case(20))
        mem = Memory(wall_repair_worker=1)
        mem.recovery.active[1] = ((1, 'collect', (7, 5)), 28)
        mem.recovery.resume(t, cfg, mem, nav, ledger, set())
        self.assertFalse(ledger.commands)
        self.assertFalse(mem.recovery.active)

    def test_agent_emits_recovery_prompt_on_observed_loop(self):
        p = self.case(45)
        p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 7, 'y': 7}})
        agent = Agent()
        agent.decide(p)
        mem = next(iter(agent.sessions.values()))
        mem.last_commands = {'1': command('move', (5, 5))}
        mem.movement.trails[1] = deque([(5, 5), (6, 5), (5, 5), (6, 5),
                                       (5, 5), (6, 5), (5, 5)], maxlen=8)
        p['roundNo'] = 46
        response = agent.decide(p)
        self.assertTrue(response['prompt'])
        self.assertEqual(mem.pending, ('recovery', 46))
        self.assertIn('1', response['roleCommandMap'])
        self.assertEqual(agent.decide(p), response)
        self.assertEqual(mem.calls, 1)


if __name__ == '__main__':
    unittest.main()
