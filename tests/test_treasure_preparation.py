"""Budgeted preparation without a complete plan; no simulated LLM reasoning."""
import unittest
from unittest.mock import patch

from test_agent import payload, setup_case, unit
from agent.economy import pioneer, reserve_treasure_gold
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Memory
from agent.treasure_clues import preparation_items


NEWS = [{'day': 1, 'folkLegends': '西部有一石门，门需三钥'},
        {'day': 3, 'folkLegends': '灰白色石板刻着字；银白色粉末在黑处发光；水晶瓶内封橙红色雾。'}]
ITEMS = ['AcientTablet', 'StarSand', 'FlameBreath']


def case(backpack=(), gold=200, x=5, round_no=391):
    data = payload(round_no, roles=[unit(11, 'pioneer', x, 5, backpack=list(backpack))])
    data['teamOur']['goldNum'] = gold
    data['weaponShopList'] = [{'name': name, 'price': 15} for name in ITEMS]
    data['mapInfo']['zones'] = [{'neutralType': 'weaponShop', 'pos': {'x': 6, 'y': 5}}]
    return data


class TreasurePreparationTests(unittest.TestCase):
    def decide(self, data, mem):
        turn, cfg, nav, ledger = setup_case(data)
        pioneer(turn, cfg, mem, nav, ledger, turn.pioneer)
        return ledger

    def test_null_plan_prepares_one_each_and_stops_without_summoning(self):
        mem = Memory(news=NEWS)
        bag = []
        for name in ITEMS:
            ledger = self.decide(case(bag), mem)
            self.assertEqual(ledger.commands['11'], {'action': 'buy', 'name': name, 'num': 1})
            bag.append(name)
        self.assertEqual(mem.treasure_prep_spent, 45)
        self.assertFalse(self.decide(case(bag), mem).commands)
        self.assertIsNone(mem.treasure)
        self.assertFalse(mem.treasure_attempted)

    def test_reserve_capacity_distance_night_and_active_task(self):
        cases = [case(gold=44), case(round_no=460), case(round_no=459)]
        full = case()
        full['teamOur']['roles'][0]['backPackCapability'] = 2
        cases.append(full)
        active = case()
        active['phaseTask'] = 'active task'
        cases.append(active)
        for data in cases:
            with self.subTest(data=data):
                mem = Memory(news=NEWS)
                self.assertFalse(self.decide(data, mem).commands)
                self.assertEqual(mem.treasure_prep_spent, 0)

    def test_issued_purchase_failure_cannot_exceed_budget(self):
        mem = Memory(news=NEWS)
        self.assertTrue(self.decide(case(), mem).commands)
        # Unchanged inventory after a failed purchase: don't exceed total cap.
        self.assertFalse(self.decide(case(), mem).commands)
        self.assertEqual(mem.treasure_prep_spent, 15)

    def test_normal_task_keeps_priority(self):
        data = case()
        data['teamOur']['playerTasks'] = [dict(isValid=True, coldDownRounds=0,
            taskPosition={'x': 5, 'y': 6}, timeoutRounds=10, scoreReward=100, goldReward=100)]
        mem = Memory(news=NEWS)
        turn, cfg, nav, ledger = setup_case(data)
        self.assertEqual(reserve_treasure_gold(turn, cfg, mem, nav, ledger, turn.pioneer), 0)
        ledger = self.decide(data, mem)
        self.assertEqual(ledger.commands['11']['action'], 'acceptTask')
        self.assertEqual(mem.treasure_prep_spent, 0)

    def test_source_evidence_required_not_model_prose(self):
        shop = dict.fromkeys(ITEMS, 15)
        self.assertEqual(preparation_items(NEWS, shop), ITEMS)
        self.assertEqual(preparation_items(NEWS[1:], shop), [])
        self.assertEqual(preparation_items(NEWS, {'AcientTablet': 15}), [])
        mem = Memory(treasure_clues=[{'kind': 'items', 'meaning': ','.join(ITEMS)}])
        self.assertFalse(self.decide(case(), mem).commands)

    def test_complete_plan_and_attempted_state_disable_preparation(self):
        for flags in ({'treasure_done': True}, {'treasure_attempted': True},
                      {'treasure': {'position': [6, 5], 'items': ['StarSand'],
                                    'startRound': 1, 'endRound': 2}}):
            mem = Memory(news=NEWS, **flags)
            self.assertFalse(self.decide(case(), mem).commands)
            self.assertEqual(mem.treasure_prep_spent, 0)

    def test_distant_shop_with_competing_spending_completes_before_day_five(self):
        # Exercise Agent's real reservation order; competing workers try to
        # consume every unreserved coin. Replay actual movement and purchases.
        agent = Agent(Config(layout_mode='explicit'))
        data = case(gold=95, x=0, round_no=261)
        data['mapInfo']['width'] = 41
        data['mapInfo']['zones'][0]['pos'] = {'x': 20, 'y': 5}
        data['worldNews'] = {'folkLegends': ''.join(n['folkLegends'] for n in NEWS)}
        purchased = []
        worker_calls = []

        def spend(turn, cfg, mem, nav, ledger, *args):
            worker_calls.append(ledger.gold)
            data['teamOur']['goldNum'] -= ledger.gold
            ledger.gold = 0

        with patch('agent.brain.workers', side_effect=spend):
            for r in range(261, 300):
                data['roundNo'] = r
                result = agent.decide(data)
                action = result['roleCommandMap'].get('11', {})
                hero = data['teamOur']['roles'][0]
                if action.get('action') == 'move':
                    hero['pos'] = action['targetPos'][0]
                elif action.get('action') == 'buy':
                    self.assertGreaterEqual(data['teamOur']['goldNum'], 15)
                    purchased.append(action['name'])
                    hero['backpack'].append(action['name'])
                    data['teamOur']['goldNum'] -= 15
                self.assertNotEqual(action.get('action'), 'summonTreasure')
                if len(purchased) == 3:
                    break
        self.assertEqual(purchased, ITEMS)
        self.assertEqual(worker_calls[0], 50)
        self.assertEqual(data['teamOur']['goldNum'], 0)
        self.assertLess(r, 330)

    def test_reserve_grows_with_income_and_releases_for_emergencies(self):
        mem = Memory(news=NEWS)
        for gold in (10, 30, 45, 95):
            turn, cfg, nav, ledger = setup_case(case(gold=gold, x=0))
            self.assertEqual(reserve_treasure_gold(turn, cfg, mem, nav, ledger, turn.pioneer), min(45, gold))
        for kind in ('pioneer', 'wall'):
            data = case()
            if kind == 'pioneer':
                data['teamOur']['roles'][0]['health'] = 100
            else:
                data['teamOur']['roles'].append(unit(33, 'wall', 1, 1, health=400))
            turn, cfg, nav, ledger = setup_case(data)
            self.assertEqual(reserve_treasure_gold(turn, cfg, mem, nav, ledger, turn.pioneer), 0)

    def test_saved_list_survives_news_eviction_and_resumes_next_day(self):
        mem = Memory(news=NEWS)
        self.assertEqual(self.decide(case(x=0), mem).commands['11']['action'], 'move')
        mem.news = []
        self.assertFalse(self.decide(case(round_no=460), mem).commands)
        self.assertEqual(self.decide(case(round_no=521), mem).commands['11']['action'], 'buy')
