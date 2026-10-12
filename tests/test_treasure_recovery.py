"""Official coordinates and evidence-based recovery, without fixture-specific policy."""
import json
import unittest
from test_agent import payload
from agent.config import Config
from agent.intelligence import Memory, Intelligence
from agent.model import Turn
from agent.treasure_clues import resolve_location, retry_allowed, treasure_active


QUOTE = '旧图原点(8,9)，向东三格，向北两格，次日开放。'
NEWS = [{'day': 1, 'folkLegends': QUOTE}]


def location(mode='relative'):
    result = dict(status='confirmed', mode=mode, day=1, quote=QUOTE)
    if mode == 'relative':
        result.update(base=[8, 9], offsets=[dict(direction='east', steps=3), dict(direction='north', steps=2)])
    else:
        result['position'] = [11, 11]
    return result


def plan():
    return dict(location=location(), position=[11, 11], items=['StarSand'],
                startRound=10, endRound=200, confidence=.99, evidence=[QUOTE])


class TreasureRecoveryTests(unittest.TestCase):
    def test_coordinates_and_boundaries(self):
        inside = lambda p: 0 <= p[0] < 41 and 0 <= p[1] < 32
        self.assertEqual(resolve_location(location(), NEWS, inside), [11, 11])
        self.assertEqual(resolve_location(location('direct'), NEWS, inside), [11, 11])
        for direction, expected in [('east', [10, 9]), ('west', [6, 9]), ('north', [8, 11]), ('south', [8, 7])]:
            loc = location(); loc['offsets'] = [dict(direction=direction, steps=2)]
            self.assertEqual(resolve_location(loc, NEWS, inside), expected)
        for change in [dict(status='unknown'), dict(status='conflict'), dict(base=[True, 2]),
                       dict(quote='invented'), dict(offsets=[]), dict(base=[40, 31]),
                       dict(offsets=[dict(direction='up', steps=1)]), dict(offsets=[dict(direction='north', steps=True)])]:
            self.assertIsNone(resolve_location({**location(), **change}, NEWS, inside))

    def test_correlated_failure_revises_and_reuses_existing_news_budget(self):
        data = payload(12)
        data['weaponShopList'] = [{'name': 'StarSand', 'price': 10}]
        data['lastSummonTreasureResult'] = 2
        data['lastRoundRoleActionResults'] = {'11': True}
        mem = Memory(news=NEWS.copy(), treasure={**plan(), 'position': [11, 7]},
                     treasure_attempted=True, last_round=11,
                     last_commands={'11': dict(action='summonTreasure', targetPos=[dict(x=11,y=7)], item=['StarSand'])})
        mem.observe(Turn(data, Config()), Config())
        self.assertEqual(mem.treasure_failures[-1]['result'], 2)
        self.assertFalse(treasure_active(Turn(data, Config()), mem))
        prompt = Intelligence(Turn(data, Config()), Config(), mem).news()
        self.assertIn('\"lastTreasureFailure\": {', prompt)
        self.assertIn('北Y增加', prompt)
        self.assertEqual(mem.calls, 1)
        self.assertFalse(mem.news_dirty)
        data.update(roundNo=13, lastSummonTreasureResult=0, llmResp=json.dumps({'treasure': {**plan(), 'position': [11, 7]}}))
        mem.last_round = 12; mem.last_commands = {}
        mem.observe(Turn(data, Config()), Config())
        self.assertEqual(mem.treasure['position'], [11, 11])
        self.assertFalse(mem.treasure_attempted)
        self.assertTrue(treasure_active(Turn(data, Config()), mem))
        self.assertEqual(Intelligence(Turn(data, Config()), Config(), mem).news(), '')

    def test_no_cosmetic_retry_or_oscillation(self):
        p = plan()
        failure = dict(round=15, result=2, position=p['position'], items=p['items'])
        for change in [{}, dict(endRound=300), dict(startRound=20), dict(confidence=1), dict(evidence=['different'])]:
            self.assertFalse(retry_allowed({**p, **change}, [failure], NEWS))
        corrected = {**p, 'position': [12, 11]}
        self.assertTrue(retry_allowed(corrected, [failure], NEWS))
        second = {**failure, 'position': corrected['position']}
        self.assertFalse(retry_allowed(p, [failure, second], NEWS))
        self.assertFalse(retry_allowed(p, [{**failure, 'result': 3}], NEWS))
        self.assertTrue(retry_allowed({**p, 'items': ['IronWhistle']}, [{**failure, 'result': 3}], NEWS))
        self.assertFalse(retry_allowed(corrected, [{**failure, 'result': 0}], NEWS))

    def test_future_opening_requires_source_and_new_start(self):
        p = plan(); failure = dict(round=15, result=2, position=p['position'], items=p['items'])
        p.update(startRound=130, opening=dict(round=130, day=1, quote=QUOTE))
        self.assertTrue(retry_allowed(p, [failure], NEWS))
        p['opening']['quote'] = 'invented'
        self.assertFalse(retry_allowed(p, [failure], NEWS))

    def test_uncorrelated_feedback_cannot_finish_or_review(self):
        for code in range(5):
            data = payload(20); data['lastSummonTreasureResult'] = code
            mem = Memory(last_round=18, treasure_attempted=True,
                         last_commands={'11': dict(action='summonTreasure', targetPos=[dict(x=3,y=4)], item=['StarSand'])})
            mem.observe(Turn(data, Config()), Config())
            self.assertFalse(mem.treasure_done)
            self.assertFalse(mem.treasure_failures)

    def test_quota_and_active_task_do_not_force_review(self):
        for kwargs in [dict(calls=3), {}]:
            data=payload(20)
            if not kwargs: data['phaseTask']='busy'
            mem=Memory(news=NEWS.copy(), news_dirty=True, **kwargs)
            self.assertEqual(Intelligence(Turn(data, Config()), Config(), mem).news(), '')


    def test_invalid_location_pauses_old_plan(self):
        for loc in [None, {**location(), 'status': 'conflict'}, {**location(), 'quote': 'invented'}]:
            data = payload(12)
            data['weaponShopList'] = [{'name': 'StarSand', 'price': 10}]
            data['llmResp'] = json.dumps({'treasure': {**plan(), 'location': loc}})
            mem = Memory(news=NEWS.copy(), treasure=plan(), pending=('news', 11))
            mem.observe(Turn(data, Config()), Config())
            self.assertIsNone(mem.treasure)
            self.assertFalse(treasure_active(Turn(data, Config()), mem))

    def test_material_correction_requires_confirmed_sourced_materials(self):
        for confirmed in [False, True]:
            data = payload(20)
            data['weaponShopList'] = [{'name': name, 'price': 10} for name in ['StarSand', 'IronWhistle']]
            candidate = {**plan(), 'items': ['IronWhistle']}
            reply = {'treasure': candidate}
            if confirmed:
                reply['treasureMaterials'] = dict(status='confirmed', items=[dict(name='IronWhistle', quantity=1, day=1, quote=QUOTE)])
            data['llmResp'] = json.dumps(reply)
            mem = Memory(news=NEWS.copy(), treasure=plan(), treasure_attempted=True,
                         treasure_failures=[dict(round=15, result=3, position=[11,11], items=['StarSand'])],
                         pending=('news',19))
            mem.observe(Turn(data, Config()), Config())
            self.assertEqual(mem.treasure_attempted, not confirmed)

    def test_corrected_plan_summons_then_stops_on_success_or_empty(self):
        from test_agent import setup_case, unit
        from agent.economy import pioneer
        for code in [1,4]:
            data=payload(20, roles=[unit(11, 'pioneer', 10,11,backpack=['StarSand'])])
            turn,cfg,nav,ledger=setup_case(data)
            mem=Memory(treasure=plan(), news=NEWS.copy(),
                       treasure_failures=[dict(round=15,result=2,position=[11,7],items=['StarSand'])])
            pioneer(turn,cfg,mem,nav,ledger,turn.pioneer)
            self.assertEqual(ledger.commands['11']['targetPos'], [{'x':11,'y':11}])
            self.assertTrue(mem.treasure_attempted)
            mem.last_commands=ledger.commands; mem.last_round=20
            data.update(roundNo=21, lastSummonTreasureResult=code)
            mem.observe(Turn(data,cfg),cfg)
            self.assertTrue(mem.treasure_done)
            self.assertFalse(treasure_active(Turn(data,cfg),mem))
