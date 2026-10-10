"""Executable surplus purchases, night stock, and role-aware delivery bounds."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.config import Config
from agent.economy import buy_supply, supplies, use_inventory, workers, station_purchase_allowed
from agent.economy_plan import delivery_destination
from agent.intelligence import Memory
from agent.model import pos
from agent.spending import defenses_ready, plan_day_spending
from agent.wall_health import needs_day_repair
from agent.wall_watch import prepare_watch
from agent.worker_jobs import resume_daytime_jobs


def case(tick=10, gold=581, packs=1):
    walls = [(x, y) for x in range(3, 9) for y in range(3, 9)
             if (x in (3, 8) or y in (3, 8)) and (x, y) != (8, 6)]
    roles = [unit(13, 'station', 5, 6, health=1500),
             unit(1, 'worker', 4, 6, health=500, backpack=['WallFixer'] * packs),
             unit(2, 'worker', 7, 9, health=500),
             unit(3, 'pioneer', 4, 9, health=500),
             unit(20, 'rocket', 7, 4, level=3, health=2000)]
    roles += [unit(100+i, 'wall', *p, level=3 if p[0] == 8 else 2,
                   health=2000 if p[0] == 8 else 1500) for i, p in enumerate(walls)]
    data = payload(650+tick, roles)
    data['mapInfo'].update(width=20, height=16,
        zones=[{'neutralType': 'weaponShop', 'pos': {'x': 6, 'y': 11}}])
    data['teamEnemy']['roles'] = [unit(99, 'station', 15, 4, health=1500)]
    data['teamOur']['goldNum'] = gold
    data['weaponShopList'] = [{'name': name, 'price': price} for name, price in (
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30), ('WallFixer', 15),
        ('StationUpgradeVoucher1', 100), ('StationUpgradeVoucher2', 150),
        ('SmallRobotSummonOrder', 15), ('MiddleRobotSummonOrder', 20),
        ('LargeRobotSummonOrder', 70), ('BossRobotSummonOrder', 120))]
    cfg = Config(layout_mode='explicit', loadout=['rocket'], weapon_cells=[[7, 4]],
                 wall_cells=[list(p) for p in walls], llm_enabled=False)
    return data, cfg


def setup(data, cfg):
    return setup_case(data, **{k: getattr(cfg, k) for k in
                      ('layout_mode', 'loadout', 'weapon_cells', 'wall_cells', 'llm_enabled')})


class DaylightSpendingTests(unittest.TestCase):
    def test_spare_fixer_does_not_exclude_worker_from_voucher_purchase(self):
        data, cfg = case()
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] not in (2, 3)]
        data['teamOur']['roles'][1]['pos'] = {'x': 6, 'y': 10}
        turn, cfg, nav, ledger = setup(data, cfg)
        workers(turn, cfg, Memory(), nav, ledger, [(7, 4)], list(ledger.wall_cells))
        if ledger.commands['1']['action'] == 'move':
            data['teamOur']['roles'][1]['pos'] = ledger.commands['1']['targetPos'][0]
            data['roundNo'] += 1
            turn,cfg,nav,ledger=setup(data,cfg)
            workers(turn,cfg,Memory(),nav,ledger,[(7,4)],list(ledger.wall_cells))
        self.assertEqual(ledger.commands['1']['action'],'buy')
        self.assertEqual(ledger.commands['1']['name'],'WallUpgradeVoucher2')
        self.assertGreater(ledger.commands['1']['num'],1)

    def test_foreign_delivery_is_not_charged_to_new_buyers_route(self):
        data = payload(650+55, [unit(13, 'station', 0, 6, health=1500),
            unit(1, 'worker', 4, 6, health=500),
            unit(2, 'worker', 20, 2, health=500, backpack=['WallUpgradeVoucher2']),
            unit(20, 'rocket', 3, 6, level=3),
            unit(30, 'wall', 20, 3, level=2, health=1300),
            unit(31, 'wall', 5, 5, level=2, health=1500),
            unit(32, 'wall', 25, 5, level=3, health=2000)])
        data['mapInfo'].update(width=30, zones=[{'neutralType':'weaponShop','pos':{'x':4,'y':7}}])
        data['weaponShopList'] = [{'name':'WallUpgradeVoucher2','price':30}]
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', loadout=['rocket'],
            weapon_cells=[[3,6]], wall_cells=[[20,3],[5,5],[25,5]])
        plan = supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)
        self.assertEqual(plan, ('WallUpgradeVoucher2', (0, None), 1))

    def test_nearby_equal_stage_wall_can_be_bought_before_tick_sixty(self):
        data = payload(650+55, [unit(13,'station',0,6,health=1500),
            unit(1,'worker',4,6,health=500),unit(20,'rocket',3,6,level=3),
            unit(30,'wall',20,3,level=2,health=1300),
            unit(31,'wall',5,5,level=2,health=1500),
            unit(32,'wall',25,5,level=3,health=2000)])
        data['mapInfo'].update(width=30,zones=[{'neutralType':'weaponShop','pos':{'x':4,'y':7}}])
        data['weaponShopList']=[{'name':'WallUpgradeVoucher2','price':30}]
        turn,cfg,nav,ledger=setup_case(data,layout_mode='explicit',loadout=['rocket'],
            weapon_cells=[[3,6]],wall_cells=[[20,3],[5,5],[25,5]])
        self.assertEqual(supplies(turn,cfg,Memory(),nav,ledger,turn.workers[0],bulk=True)[2],1)

    def test_courier_returns_to_inner_wall_but_operator_keeps_exact_post(self):
        data,cfg=case()
        turn,cfg,nav,ledger=setup(data,cfg)
        goals,exact=delivery_destination(turn,nav,ledger,turn.workers[1])
        self.assertTrue(exact)
        self.assertTrue(all(3<x<8 and 3<y<8 for x,y in goals))
        ledger.operator_posts[2]=(7,5)
        self.assertEqual(delivery_destination(turn,nav,ledger,turn.workers[1]), ([(7,5)],True))

    def test_stock_trip_starts_early_and_includes_daylight_repairs(self):
        data,cfg=case(packs=1)
        data['teamOur']['roles'][1]['pos']={'x':6,'y':10}
        for p,hp in (((8,4),1440),((8,5),1545)):
            next(r for r in data['teamOur']['roles'] if r['roleType']=='wall' and pos(r['pos'])==p)['health']=hp
        turn,cfg,nav,ledger=setup(data,cfg)
        mem=Memory(wall_watch_id=1)
        locked,funds=prepare_watch(turn,cfg,mem,nav,ledger,turn.workers[0],list(ledger.wall_cells))
        self.assertTrue(locked)
        self.assertEqual(funds,0)
        self.assertEqual(ledger.commands['1'],command('buy',name='WallFixer',num=10))
        self.assertEqual(ledger.gold,431)

    def test_fully_stocked_watcher_can_work_before_actual_recall_deadline(self):
        data,cfg=case(tick=40,packs=9)
        turn,cfg,nav,ledger=setup(data,cfg)
        locked,funds=prepare_watch(turn,cfg,Memory(wall_watch_id=1),nav,ledger,
                                    turn.workers[0],list(ledger.wall_cells))
        self.assertEqual((locked,funds),(False,0))
        self.assertFalse(ledger.commands)

    def test_day_repair_uses_ratio_and_missing_hp(self):
        data,cfg=case()
        for hp,expected in ((1745,False),(1675,False),(1600,False),(1545,True),(1440,True)):
            next(r for r in data['teamOur']['roles'] if r['roleType']=='wall' and pos(r['pos'])==(8,5))['health']=hp
            turn,_,_,_=setup(data,cfg)
            wall=next(w for w in turn.ours if w.pos==(8,5))
            self.assertEqual(needs_day_repair(wall,turn),expected)

    def test_pre_wave_repairs_do_not_consume_night_safety_stock(self):
        data,cfg=case(packs=9)
        data['teamOur']['roles'][1]['pos']={'x':7,'y':5}
        next(r for r in data['teamOur']['roles'] if r['roleType']=='wall' and pos(r['pos'])==(8,5))['health']=1440
        turn,cfg,nav,ledger=setup(data,cfg)
        self.assertFalse(use_inventory(turn,nav,ledger,turn.workers[0],mem=Memory(wall_watch_id=1)))
        data['teamOur']['roles'][1]['backpack'].append('WallFixer')
        turn,cfg,nav,ledger=setup(data,cfg)
        self.assertTrue(use_inventory(turn,nav,ledger,turn.workers[0],mem=Memory(wall_watch_id=1)))
        self.assertEqual(ledger.commands['1'],command('use',(8,5),name='WallFixer'))

    def test_healthy_base_is_eligible_with_unfinished_side_wall_levels(self):
        data,cfg=case()
        turn,cfg,nav,ledger=setup(data,cfg)
        self.assertTrue(station_purchase_allowed(turn,turn.station,Memory()))
        plan=supplies(turn,cfg,Memory(),nav,ledger,turn.workers[1],item_only='StationUpgradeVoucher1')
        self.assertEqual(plan[0],'StationUpgradeVoucher1')

    def test_defense_diagnostic_tracks_front_levels_and_breaches(self):
        data,cfg=case()
        turn,cfg,nav,ledger=setup(data,cfg)
        self.assertTrue(defenses_ready(turn,cfg,[(7,4)],list(ledger.wall_cells)))
        for change in ('front','missing','rebuild'):
            p=copy.deepcopy(data);mem=Memory()
            front=next(r for r in p['teamOur']['roles'] if r['roleType']=='wall' and pos(r['pos'])==(8,5))
            if change=='front':front['level']=2
            if change=='missing':p['teamOur']['roles'].remove(front)
            if change=='rebuild':mem.wall_rebuild_levels[(8,5)]=3
            t,c,n,l=setup(p,cfg)
            self.assertFalse(defenses_ready(t,c,[(7,4)],list(l.wall_cells),mem,l))

    def test_defense_diagnostic_tracks_missing_night_stock(self):
        data,cfg=case(packs=1)
        turn,cfg,nav,ledger=setup(data,cfg);mem=Memory(wall_watch_id=1)
        self.assertFalse(defenses_ready(turn,cfg,[(7,4)],list(ledger.wall_cells),mem,ledger))
        ledger.watch_pack_slots[1]=8
        self.assertTrue(defenses_ready(turn,cfg,[(7,4)],list(ledger.wall_cells),mem,ledger))

    def test_planner_deduplicates_batch_reservations_and_protects_base_budget(self):
        data,cfg=case()
        turn,cfg,nav,ledger=setup(data,cfg)
        actors,reserve=plan_day_spending(turn,cfg,Memory(),nav,ledger,[(7,4)])
        self.assertTrue(actors)
        self.assertLessEqual(reserve,581)
        self.assertEqual(ledger.spending_plan['purchases']['StationUpgradeVoucher1'],100)
        self.assertGreater(ledger.spending_plan['purchases']['WallUpgradeVoucher2'],0)
        self.assertEqual(ledger.gold,581)

    def test_two_workers_do_not_purchase_the_same_wall_batch(self):
        data,cfg=case()
        data['teamOur']['roles'][1]['pos']={'x':6,'y':10}
        data['teamOur']['roles'][2]['pos']={'x':5,'y':10}
        turn,cfg,nav,ledger=setup(data,cfg);mem=Memory()
        first=supplies(turn,cfg,mem,nav,ledger,turn.workers[0],bulk=True,
                       item_only='WallUpgradeVoucher2')
        self.assertTrue(buy_supply(turn,ledger,turn.workers[0],first))
        remaining=ledger.gold
        self.assertIsNone(supplies(turn,cfg,mem,nav,ledger,turn.workers[1],bulk=True,
                                   item_only='WallUpgradeVoucher2'))
        self.assertEqual(ledger.gold,remaining)

    def test_base_delivery_also_respects_operator_post_before_tick_sixty(self):
        data,cfg=case(tick=54)
        turn,cfg,nav,ledger=setup(data,cfg)
        ledger.operator_posts[2]=(19,0)
        self.assertIsNone(supplies(turn,cfg,Memory(),nav,ledger,turn.workers[1],
                                   item_only='StationUpgradeVoucher1'))

    def test_paid_upgrade_restores_damaged_wall_before_spending_fixer(self):
        data,cfg=case()
        data['teamOur']['roles'][2].update(pos={'x':7,'y':5},
            backpack=['WallFixer','WallUpgradeVoucher2'])
        wall=next(r for r in data['teamOur']['roles']
                  if r['roleType']=='wall' and pos(r['pos'])==(8,5))
        wall.update(level=2,health=850)
        turn,cfg,nav,ledger=setup(data,cfg)
        self.assertTrue(use_inventory(turn,nav,ledger,turn.workers[1],mem=Memory()))
        self.assertEqual(ledger.commands['2'],command('use',(8,5),name='WallUpgradeVoucher2'))

    def test_robot_uses_only_unreserved_money_without_operator_return_deadline(self):
        from test_robot_assault import BestRobotSummoningTests,fortified_case,WALLS,TOWERS
        data=BestRobotSummoningTests().shop_data(gold=200)
        turn,cfg,nav,ledger=fortified_case(data)
        self.assertTrue(summon_best_robot(turn,cfg,Memory(),nav,ledger,TOWERS,WALLS,reserve=130))
        self.assertEqual(ledger.commands['1']['name'],'LargeRobotSummonOrder')
        self.assertEqual(ledger.gold,130)
        data['roundNo']=329
        turn,cfg,nav,ledger=fortified_case(data)
        self.assertTrue(summon_best_robot(turn,cfg,Memory(),nav,ledger,TOWERS,WALLS))
        self.assertEqual(ledger.commands['1'], command('buy', name='BossRobotSummonOrder', num=1))
        self.assertEqual(ledger.gold,80)

    def test_medicine_budget_is_preserved_and_only_counted_once(self):
        from test_robot_assault import BestRobotSummoningTests,fortified_case,WALLS,TOWERS
        for hp in (50,150):
            with self.subTest(hp=hp):
                data=BestRobotSummoningTests().shop_data(gold=130)
                data['teamOur']['roles'][0]['level']=3
                data['teamOur']['roles'][1]['health']=hp
                data['weaponShopList'].append({'name':'Medicine','price':10})
                turn,cfg,nav,ledger=fortified_case(data);mem=Memory()
                actors,reserve=plan_day_spending(turn,cfg,mem,nav,ledger,TOWERS)
                self.assertEqual(reserve,10)
                self.assertTrue(summon_best_robot(turn,cfg,mem,nav,ledger,TOWERS,WALLS,
                                                 excluded=actors,reserve=reserve))
                self.assertEqual(ledger.notes[2]['conditions']['item'],'BossRobotSummonOrder')
                self.assertEqual(ledger.gold,10)

    def test_walking_medicine_buyer_has_already_reserved_emergency_money(self):
        from test_robot_assault import BestRobotSummoningTests,fortified_case,WALLS,TOWERS
        data=BestRobotSummoningTests().shop_data(gold=130)
        data['teamOur']['roles'][1].update(pos={'x':0,'y':0},health=50)
        data['weaponShopList'].append({'name':'Medicine','price':10})
        turn,cfg,nav,ledger=fortified_case(data);mem=Memory()
        plan=supplies(turn,cfg,mem,nav,ledger,turn.workers[0],urgent_only=True)
        self.assertTrue(buy_supply(turn,ledger,turn.workers[0],plan))
        self.assertEqual(ledger.gold,120)
        self.assertTrue(summon_best_robot(turn,cfg,mem,nav,ledger,TOWERS,WALLS))
        self.assertEqual(ledger.notes[2]['conditions']['item'],'BossRobotSummonOrder')
        self.assertEqual(ledger.gold,0)

    def test_robot_purchase_continues_same_item_and_shop(self):
        from test_robot_assault import BestRobotSummoningTests,fortified_case,WALLS,TOWERS
        data=BestRobotSummoningTests().shop_data()
        data['teamOur']['roles'][1]['pos']={'x':0,'y':0}
        turn,cfg,nav,ledger=fortified_case(data)
        mem=Memory(daytime_jobs={1:dict(kind='robot_buy',target=(4,9),name='MiddleRobotSummonOrder',quantity=1)})
        resume_daytime_jobs(turn,cfg,mem,nav,ledger,TOWERS,WALLS,set())
        self.assertEqual(ledger.work_jobs[1]['name'],'MiddleRobotSummonOrder')
        self.assertEqual(ledger.work_jobs[1]['target'],(4,9))
        self.assertEqual(ledger.gold,100)

    def test_robot_continuation_rechecks_new_defensive_purchase_budget(self):
        data,cfg=case(gold=100)
        turn,cfg,nav,ledger=setup(data,cfg)
        mem=Memory(daytime_jobs={2:dict(kind='robot_buy',target=(6,11),
                                      name='MiddleRobotSummonOrder',quantity=1)})
        resume_daytime_jobs(turn,cfg,mem,nav,ledger,[(7,4)],list(ledger.wall_cells),set())
        self.assertNotIn('2',ledger.commands)
        self.assertNotIn(2,mem.daytime_jobs)
        self.assertEqual(ledger.gold,100)

    def test_agent_reports_spending_rejections_without_changing_cached_turn(self):
        data,cfg=case(tick=69)
        agent=Agent(cfg);response=agent.decide(copy.deepcopy(data))
        self.assertEqual(agent.decide(copy.deepcopy(data)),response)
        turn,cfg,nav,ledger=setup(data,cfg)
        self.assertIsNone(supplies(turn,cfg,Memory(),nav,ledger,turn.workers[1],bulk=True))
        self.assertTrue(any(r['reason']=='delivery_or_return_deadline'
                            for r in ledger.supply_reports[2]['reasons']))
