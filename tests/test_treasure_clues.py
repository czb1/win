"""Evidence-backed partial memory; fixtures do not measure real LLM accuracy."""
import copy
import json
import unittest

from test_agent import payload
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Intelligence, Memory
from agent.model import Turn
from agent.treasure_clues import ITEM_DESCRIPTIONS, merge_clues


def clue(kind, day, quote, meaning):
    return dict(kind=kind, day=day, quote=quote, meaning=meaning)


class TreasureClueTests(unittest.TestCase):
    def test_five_day_partial_evidence_survives_null_noise_and_task_reset(self):
        news = ['西部有一石门，门需三钥', '村里组织巡逻队，今晚开始值夜',
                '灰白色石板刻着文字，银白粉末暗处发光，晶石瓶封着橙红色雾',
                '矿区有人半夜偷矿', '三道封印缺一不可，必须同时解开']
        mem, cfg = Memory(), Config()
        for day, text in enumerate(news, 1):
            data = payload((day - 1) * 130 + 1)
            data['weaponShopList'] = [{'name': k, 'price': 15} for k in ITEM_DESCRIPTIONS]
            data['worldNews'] = {'folkLegends': text}
            turn = Turn(data, cfg)
            mem.observe(turn, cfg)
            prompt = Intelligence(turn, cfg, mem).news()
            self.assertIn('itemDescriptions', prompt)
            self.assertIn('AcientTablet', prompt)
            if day >= 4:
                self.assertIn('用品候选为三件', prompt)
            hints = ([clue('location', day, text, '西部，精确坐标未知')] if day == 1 else
                     [clue('items', day, text, '用品候选为三件：AcientTablet、StarSand、FlameBreath')]
                     if day == 3 else [])
            data.update(roundNo=data['roundNo'] + 1,
                        llmResp=json.dumps({'treasureClues': hints, 'treasure': None}))
            mem.observe(Turn(data, cfg), cfg)
            self.assertIsNone(mem.treasure)
        self.assertEqual(len(mem.treasure_clues), 2)
        data.update(roundNo=523, phaseTask='a self evolution task', llmResp='')
        mem.observe(Turn(data, cfg), cfg)
        self.assertEqual(len(mem.treasure_clues), 2)
        self.assertEqual(Memory().treasure_clues, [])

    def test_forged_quotes_invalid_types_and_oversized_hints_rejected(self):
        news = [{'day': 1, 'folkLegends': '西部有一石门，门需三钥'}]
        good = clue('location', 1, '西部有一石门', '西部')
        bad = [clue('location', 2, good['quote'], '西部'),
               clue('location', 1, '石门在坐标(1,2)', '精确坐标'),
               clue('items', True, good['quote'], '错误天数'),
               clue('items', 1, good['quote'], 'x' * 301), None, {'kind': []}]
        kept, rejected = merge_clues([], [good, *bad], news)
        self.assertEqual(kept, [good])
        self.assertEqual(rejected, len(bad))
        self.assertEqual(merge_clues(kept, None, news)[0], kept)
        self.assertEqual(merge_clues(kept, {}, news), (kept, 1))

    def test_bounds_dedup_and_conflicting_sources(self):
        hints = [clue('items', 1, '线索正文%02d' % i, '候选') for i in range(20)]
        text = '；'.join(c['quote'] for c in hints)
        news = [{'day': 1, 'folkLegends': text}]
        kept, rejected = merge_clues([], hints, news)
        self.assertEqual(len(kept), 6)
        self.assertEqual(rejected, 2)
        self.assertEqual(merge_clues(kept, kept, news)[0], kept)
        opposite = [clue('location', 1, '石门位于西侧', '西侧'),
                    clue('location', 2, '旧地图错误，石门位于东侧', '东侧')]
        sources = [{'day': c['day'], 'folkLegends': c['quote']} for c in opposite]
        kept, _ = merge_clues([], opposite, sources)
        self.assertEqual(kept, opposite)

    def test_partial_hints_do_not_change_pioneer_commands_or_quota(self):
        with_hints, without_hints = Agent(), Agent()
        quote = '灰白色石板上刻着文字'
        for r in range(1, 6):
            data = payload(r)
            data['worldNews'] = {'folkLegends': quote}
            data['weaponShopList'] = [{'name': 'AcientTablet', 'price': 15}]
            response = {'treasure': None, 'oreOutages': []}
            data['llmResp'] = json.dumps(response)
            baseline = without_hints.decide(copy.deepcopy(data))
            response['treasureClues'] = [clue('items', 1, quote, 'AcientTablet')]
            data['llmResp'] = json.dumps(response)
            actual = with_hints.decide(data)
            self.assertEqual(actual['roleCommandMap'], baseline['roleCommandMap'])
            self.assertEqual(actual['executeCmd'], baseline['executeCmd'])
        self.assertEqual(next(iter(with_hints.sessions.values())).calls,
                         next(iter(without_hints.sessions.values())).calls)

    def test_later_complete_plan_uses_existing_validation_and_old_format_works(self):
        for extra in ({}, {'treasureClues': []}):
            data = payload(2)
            data['weaponShopList'] = [{'name': 'StarSand', 'price': 15}]
            plan = dict(position=[4, 4], items=['StarSand'], startRound=10,
                        endRound=20, confidence=.99, evidence=['synthetic complete fixture'])
            data['llmResp'] = json.dumps({'treasure': plan, **extra})
            mem = Memory(pending=('news', 1))
            mem.observe(Turn(data, Config()), Config())
            self.assertEqual(mem.treasure, plan)
            self.assertFalse(mem.treasure_attempted)


if __name__ == '__main__':
    unittest.main()
