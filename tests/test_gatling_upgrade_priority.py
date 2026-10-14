"""Finite checks for the observed Gatling cap and a single scout shop visit."""
import unittest

import test_scout_defense_upgrade as scout
from agent.brain import Agent
from agent.commands import command
from agent.economy import dusk_resources, supplies, use_inventory, workers
from agent.economy_plan import planned_weapons
from agent.intelligence import Memory
from agent.night_economy import WorkNavigator, _deliver, _purchase
from agent.worker_jobs import resume_daytime_jobs


V1, V2 = scout.V1, scout.V2


def case(tick=165, stock=(), gold=500, worker_pos=(7, 23), level=2):
    data = scout.case(tick, stock=stock, gold=gold, worker_pos=worker_pos)
    data['teamOur']['roles'][-1].update(roleType='gatling', level=level)
    return data


class GatlingUpgradePriorityTests(unittest.TestCase):
    def test_other_weapons_wait_even_when_gatling_upgrade_is_not_feasible(self):
        for reason in ('voucher', 'local', 'claim', 'route'):
            for kind in ('rocket', 'railgun'):
                with self.subTest(reason=reason, kind=kind):
                    data = case(stock=[V1] + ([] if reason == 'voucher' else [V2]))
                    data['teamOur']['roles'][4]['roleType'] = kind
                    turn, _, nav, ledger = scout.setup(data)
                    if reason == 'claim':
                        ledger.upgrade_claims.add(22)
                    if reason == 'route':
                        turn.blocked.update({(x, y) for x in range(7, 10)
                                             for y in range(20, 23)} - {(8, 21)})
                    self.assertFalse(use_inventory(turn, nav, ledger, turn.workers[0],
                                                   local_only=reason == 'local', mem=Memory()))
                    self.assertFalse(ledger.commands)

    def test_second_worker_waits_for_observed_level_three_after_final_use(self):
        data = case(stock=[V2], worker_pos=(7, 21), gold=0)
        data['teamOur']['roles'][2].update(pos=dict(x=7, y=23), backpack=[V1])
        turn, _, nav, ledger = scout.setup(data)
        self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[0], mem=Memory()))
        self.assertEqual(ledger.commands['10'], command('use', (8, 21), name=V2))
        self.assertFalse(use_inventory(turn, nav, ledger, turn.workers[1], mem=Memory()))
        data['roundNo'] += 1
        data['teamOur']['roles'][-1]['level'] = 3
        turn, _, nav, ledger = scout.setup(data)
        self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[1], mem=Memory()))
        self.assertEqual(ledger.commands['12'], command('use', (8, 23), name=V1))

    def test_all_existing_gatlings_must_be_maxed(self):
        data = case(stock=[V1], level=3)
        data['teamOur']['roles'][5].update(roleType='gatling', level=2)
        turn, _, nav, ledger = scout.setup(data)
        self.assertFalse(use_inventory(turn, nav, ledger, turn.workers[0], mem=Memory()))

    def test_normal_shopping_never_substitutes_rocket_vouchers(self):
        for reason in ('gold', 'missing', 'claimed', 'carried', 'old_job'):
            with self.subTest(reason=reason):
                data = case(worker_pos=(14, 19), gold=100 if reason == 'gold' else 500)
                if reason == 'missing':
                    data['weaponShopList'] = [i for i in data['weaponShopList'] if i['name'] != V2]
                if reason == 'carried':
                    data['teamOur']['roles'][2]['backpack'] = [V2]
                turn, cfg, nav, ledger = scout.setup(data)
                if reason == 'claimed':
                    ledger.purchases.add(V2)
                options = {'item_only': V1} if reason == 'old_job' else {}
                self.assertIsNone(supplies(turn, cfg, Memory(), nav, ledger,
                                           turn.workers[0], bulk=True, **options))

    def test_bulk_shopping_only_counts_unfinished_gatlings(self):
        turn, cfg, nav, ledger = scout.setup(case(worker_pos=(14, 19), level=1))
        plan = supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)
        self.assertEqual((plan[0], plan[2]), (V1, 1))

    def test_planned_gatling_also_gates_other_weapon_purchase_demand(self):
        data = case(worker_pos=(14, 19))
        data['teamOur']['roles'].pop()
        turn, cfg, nav, ledger = scout.setup(data)
        mem = Memory()
        planned = planned_weapons(turn, cfg, mem, [tuple(p) for p in scout.offense.TOWERS])
        plan = supplies(turn, cfg, mem, nav, ledger, turn.workers[0],
                        planned=planned, reserve=cfg.weapon_cost, bulk=True)
        self.assertEqual((plan[0], plan[2]), (V1, 1))

    def test_unused_rocket_coupon_does_not_block_buying_gatling_second_tier(self):
        for allocate in (workers, dusk_resources):
            with self.subTest(allocate=allocate.__name__):
                turn, cfg, nav, ledger = scout.setup(
                    case(stock=[V1], worker_pos=(14, 19), gold=150))
                ledger.used.update((11, 12))
                ledger.income_worker_id = 12
                towers = sorted(ledger.tower_cells)
                args = (towers, []) if allocate is workers else (towers,)
                allocate(turn, cfg, Memory(), nav, ledger, *args)
                self.assertEqual(ledger.commands.get('10'), command('buy', name=V2, num=1))

    def test_recovery_cannot_deliver_a_rocket_coupon_early(self):
        for level in (2, 3):
            with self.subTest(level=level):
                turn, cfg, nav, ledger = scout.setup(case(stock=[V1], level=level))
                mem = Memory()
                action = mem.recovery.action((10, 'use', (8, 23)), turn, cfg, mem, nav, ledger)
                self.assertEqual(action, None if level == 2 else command('use', (8, 23), name=V1))

    def test_night_delivery_and_procurement_obey_the_same_gate(self):
        for level in (2, 3):
            with self.subTest(level=level):
                data = case(240, stock=[V1], gold=100, level=level)
                turn, cfg, nav, ledger = scout.setup(data)
                mem = Memory(gatling_operator_id=10)
                state = dict(end_round=295, phase='shop')
                self.assertEqual(_deliver(turn, mem, WorkNavigator(nav, ledger), ledger,
                                          turn.workers[0], state), level == 3)
                data['teamOur']['roles'][1].update(pos=dict(x=14, y=19), backpack=[])
                turn, cfg, nav, ledger = scout.setup(data)
                self.assertEqual(_purchase(turn, cfg, mem, WorkNavigator(nav, ledger), ledger,
                                           turn.workers[0], state), level == 3)
                if level == 3:
                    self.assertEqual(ledger.commands['10'], command('buy', name=V1, num=1))

    def test_old_purchase_job_cannot_spend_the_gatling_budget_on_rockets(self):
        turn, cfg, nav, ledger = scout.setup(case(worker_pos=(14, 19), gold=100))
        mem = Memory(daytime_jobs={10: dict(kind='buy', target=(15, 20), name=V1, quantity=2)})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, scout.offense.TOWERS, [], set())
        self.assertFalse(any(c['action'] == 'buy' for c in ledger.commands.values()))

    def test_scout_with_first_coupon_at_shop_finishes_second_coupon_before_return(self):
        data = case(35, stock=[V1], worker_pos=(14, 19), gold=150, level=1)
        mem = scout.memory()
        _, ledger = scout.step(data, mem)
        self.assertEqual(ledger.commands['10'], command('buy', name=V2, num=1))
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_buy')
        self.assertEqual(ledger.gold, 0)
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'] += [V2]
        data['teamOur']['goldNum'] = 0
        _, ledger = scout.step(data, mem)
        self.assertEqual(ledger.commands['10']['action'], 'move')
        self.assertEqual(mem.first_day_offense['phase'], 'upgrade_use')

    def test_scout_buys_both_tiers_for_gatling_without_prefetching_rockets(self):
        data = case(35, worker_pos=(14, 19), gold=1000, level=1)
        agent = Agent(scout.offense.cfg(walls=[]))
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=V1, num=1))
        mem = next(iter(agent.sessions.values()))
        self.assertTrue(mem.night_scout.cancelled)
        data['roundNo'] += 1
        data['teamEnemy']['roles'].pop()
        data['lastRoundRoleActionResults'] = {'10': True}
        data['teamOur']['roles'][1]['backpack'] = [V1]
        data['teamOur']['goldNum'] = 900
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=V2, num=1))
        data['roundNo'] += 1
        data['teamOur']['roles'][1].update(backpack=[V1, V2], pos=dict(x=7, y=21))
        data['teamOur']['goldNum'] = 750
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('use', (8, 21), name=V1))
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'] = [V2]
        data['teamOur']['roles'][-1]['level'] = 2
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('use', (8, 21), name=V2))
        self.assertFalse(any(c.get('name') in scout.SUMMON_ORDERS for c in response.values()))
