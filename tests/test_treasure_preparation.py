"""Evidence-backed purchasing and default day-one-only robot policy."""
import copy
import json
import unittest
from unittest.mock import patch
from run_checks import replay_test

ITEMS = ["AcientTablet", "StarSand", "FlameBreath"]
from test_agent import payload, setup_case, unit
from agent.economy import pioneer, reserve_treasure_gold
from agent.brain import Agent, summon_best_robot
from agent.config import Config
from agent.intelligence import Memory, Intelligence
from agent.model import Turn
from agent.treasure_clues import merge_material_plan
from agent.worker_jobs import resume_daytime_jobs


def material_plan(names=('MoonKey', 'BlueDust'), quantity=1):
    return dict(status='confirmed', items=[dict(name=n, quantity=quantity, day=2,
                quote=f'祭坛需要{quantity}件{n}') for n in names])


def memory(plan=None, **kwargs):
    p = plan or material_plan()
    return Memory(treasure_materials=p, news=[dict(day=2,
                  folkLegends='；'.join(i['quote'] for i in p['items']))], **kwargs)


def case(backpack=(), gold=200, round_no=140):
    data = payload(round_no, [unit(11, 'pioneer', 5, 5, backpack=list(backpack))])
    data['teamOur']['goldNum'] = gold
    data['weaponShopList'] = [dict(name=n, price=30) for n in ('MoonKey', 'BlueDust')]
    data['mapInfo']['zones'] = [dict(neutralType='weaponShop', pos=dict(x=6, y=5))]
    return data


class TreasurePreparationTests(unittest.TestCase):
    def decide(self, data, mem):
        t, c, n, l = setup_case(data)
        pioneer(t, c, mem, n, l, t.pioneer)
        return l

    def test_dynamic_names_counts_and_evidence(self):
        for count in (2, 4):
            p = material_plan(tuple(f'NewItem{i}' for i in range(count)), 2)
            m = memory(p)
            accepted, status = merge_material_plan(None, p, m.news, [],
                                                   {i['name']: 30 for i in p['items']})
            self.assertEqual(status, 'confirmed')
            self.assertEqual(accepted, p)
        p = material_plan(quantity=2)
        m = memory(p)
        l = self.decide(case(['MoonKey']), m)
        self.assertEqual(l.commands['11'], dict(action='buy', name='MoonKey', num=1))
        l = self.decide(case(['MoonKey'] * 2), m)
        self.assertEqual(l.commands['11'], dict(action='buy', name='BlueDust', num=2))
        self.assertFalse(self.decide(case(['MoonKey', 'BlueDust'] * 2), m).commands)
        self.assertFalse(m.treasure_attempted)

    def test_missing_malformed_conflict_and_recovery(self):
        p = material_plan(); m = memory(p); shop = dict(MoonKey=30, BlueDust=30)
        for bad in (None, [], {'status': 'confirmed', 'items': [{}]},
                    material_plan(('NotInShop',)), material_plan(quantity=True)):
            self.assertEqual(merge_material_plan(p, bad, m.news, [], shop)[0], p)
        bad = copy.deepcopy(p); bad['items'][0]['quote'] = '伪造的原文'
        self.assertEqual(merge_material_plan(p, bad, m.news, [], shop)[0], p)
        conflict, _ = merge_material_plan(p, dict(status='conflict'), m.news, [], shop)
        m.treasure_materials = conflict
        self.assertFalse(self.decide(case(), m).commands)
        m.treasure_materials, _ = merge_material_plan(conflict, p, m.news, [], shop)
        self.assertEqual(self.decide(case(), m).commands['11']['action'], 'buy')
        self.assertFalse(self.decide(case(), Memory(news=m.news)).commands)

    def test_news_response_populates_plan_without_an_extra_call(self):
        data = case(); p = material_plan(); m = memory(p)
        m.treasure_materials = None
        m.news_dirty = True
        t = Turn(data, Config())
        m.day = t.day
        prompt = Intelligence(t, Config(), m).news()
        self.assertIn('treasureMaterials', prompt)
        calls = m.calls
        data.update(roundNo=141, llmResp=json.dumps(dict(treasureMaterials=p, treasure=None)))
        m.observe(Turn(data, Config()), Config())
        self.assertEqual(m.treasure_materials, p)
        self.assertEqual(m.calls, calls)

    def test_preparation_before_new_task_then_release_after_stocked(self):
        data = case()
        data['teamOur']['playerTasks'] = [dict(isValid=True, coldDownRounds=0,
            taskPosition=dict(x=5,y=6), timeoutRounds=10, scoreReward=100, goldReward=100)]
        m = memory(); m.news = []
        self.assertEqual(self.decide(data, m).commands['11']['action'], 'buy')
        data['teamOur']['roles'][0]['backpack'] = ['MoonKey', 'BlueDust']
        self.assertEqual(self.decide(data, m).commands['11']['action'], 'acceptTask')

    def test_budget_capacity_emergency_and_failure_cooldown(self):
        m = memory()
        for gold in (10, 60, 200):
            t,c,n,l = setup_case(case(gold=gold))
            self.assertEqual(reserve_treasure_gold(t,c,m,n,l,t.pioneer), min(60,gold))
        for state in ('night', 'first_day', 'full', 'active', 'hurt', 'failed', 'done'):
            data = case(); m = memory()
            if state == 'night': data['roundNo'] = 200
            if state == 'first_day': data['roundNo'] = 10
            if state == 'full': data['teamOur']['roles'][0]['backPackCapability'] = 1
            if state == 'active': data['phaseTask'] = 'active task'
            if state == 'hurt': data['teamOur']['roles'][0]['health'] = 100
            if state == 'failed': m.buy_failures[(11,'MoonKey')] = 145
            if state == 'done': m.treasure_done = True
            self.assertFalse(self.decide(data,m).commands, state)

    def test_complete_trip_deadline_and_early_material_purchase(self):
        m=memory(); m.treasure=dict(position=[13,13],items=['MoonKey'],startRound=140,endRound=142)
        self.assertFalse(self.decide(case(),m).commands)
        m.treasure.update(startRound=270,endRound=290)
        self.assertEqual(self.decide(case(),m).commands['11']['action'],'buy')

    def test_reservation_protects_materials_from_ordinary_workers(self):
        agent=Agent(Config(layout_mode='explicit'))
        data=case(); agent.decide(data)
        m=next(iter(agent.sessions.values())); m.treasure_materials=material_plan()
        data['roundNo']+=1
        def spend(turn,cfg,mem,nav,ledger,*args):
            self.assertEqual(ledger.gold,140)
            ledger.gold=0
        with patch('agent.brain.workers',side_effect=spend):
            result=agent.decide(data)
        self.assertEqual(result['roleCommandMap']['11'],dict(action='buy',name='MoonKey',num=1))

    def test_no_new_robot_purchase_or_old_trip_after_day_one(self):
        for day in range(2,11):
            data=case(round_no=(day-1)*130+10)
            data['teamOur']['roles'].append(unit(10,'worker',5,4))
            data['teamEnemy']['roles']=[unit(99,'station',12,12)]
            data['weaponShopList'] += [dict(name='BossRobotSummonOrder',price=120)]
            t,c,n,l=setup_case(data)
            m=Memory(daytime_jobs={10:dict(kind='robot_buy',target=(6,5),name='BossRobotSummonOrder',quantity=1)})
            self.assertFalse(summon_best_robot(t,c,m,n,l,[],[]))
            self.assertEqual(l.gold,200)
            resume_daytime_jobs(t,c,m,n,l,[],[],set())
            self.assertNotIn(10,m.daytime_jobs)
            self.assertFalse(any(x.get('name')=='BossRobotSummonOrder' for x in l.commands.values()))

    def test_conflicting_complete_plan_cannot_override_materials(self):
        data=case(); m=memory(); m.day=2; m.pending=('news',139)
        data['llmResp']=json.dumps(dict(treasure=dict(position=[7,7],items=['MoonKey'],
            startRound=145,endRound=160,confidence=.99,evidence=['fixture'])))
        m.observe(Turn(data,Config()),Config())
        self.assertIsNone(m.treasure)

    def test_carried_orders_still_used_by_worker_but_not_treasure_pioneer(self):
        from test_robot_assault import fortified_payload, fortified_case, TOWERS, WALLS
        data=fortified_payload(260, ['BossRobotSummonOrder'])
        t,c,n,l=fortified_case(data); c.robot_purchase_last_day=1
        self.assertTrue(summon_best_robot(t,c,Memory(),n,l,TOWERS,WALLS))
        self.assertEqual(l.commands['1']['action'],'use')
        data=case(['BossRobotSummonOrder']); data['teamEnemy']['roles']=[unit(99,'station',12,12)]
        t,c,n,l=setup_case(data)
        self.assertFalse(summon_best_robot(t,c,memory(),n,l,[],[]))
        self.assertNotIn('11',l.commands)

    @replay_test
    def test_distant_shop_with_competing_spending_completes_before_day_five(self):
        # Exercise Agent's real reservation order; competing workers try to
        # consume every unreserved coin. Replay actual movement and purchases.
        agent = Agent(Config(layout_mode='explicit'))
        data = case(gold=95, round_no=261)
        data['teamOur']['roles'][0]['pos']['x'] = 0
        data['mapInfo']['width'] = 41
        data['mapInfo']['zones'][0]['pos'] = {'x': 20, 'y': 5}
        data['weaponShopList'] = [dict(name=n, price=15) for n in ITEMS]
        agent.decide(data)
        next(iter(agent.sessions.values())).treasure_materials = material_plan(ITEMS)
        data['roundNo'] += 1
        purchased = []
        worker_calls = []

        def spend(turn, cfg, mem, nav, ledger, *args):
            worker_calls.append(ledger.gold)
            data['teamOur']['goldNum'] -= ledger.gold
            ledger.gold = 0

        with patch('agent.brain.workers', side_effect=spend):
            for r in range(262, 300):
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
