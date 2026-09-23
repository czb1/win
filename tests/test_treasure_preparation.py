"""Budgeted preparation without a complete plan; no simulated LLM reasoning."""
import unittest

from test_agent import payload, setup_case, unit
from agent.economy import pioneer
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
        cases = [case(gold=144), case(x=0), case(round_no=460), case(round_no=459)]
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
