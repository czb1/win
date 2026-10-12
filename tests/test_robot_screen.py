"""Focused first-night experiment checks; not an official engine simulation."""
import copy
import unittest
from test_agent import payload, unit, setup_case
from agent.brain import Agent, observe_robot_summons
from agent.config import Config
from agent.intelligence import Memory
from agent.robot_screen import (ray_cells, defend_screen, prepare_screen, summon_position,
                                screen_cells, ORDER, VOUCHER)


def case(round_no=1, gold=75):
    data = payload(round_no, [unit(13, 'station', 6, 8, health=1500),
                             unit(10, 'worker', 1, 2), unit(12, 'worker', 2, 1)])
    data['teamOur']['goldNum'] = gold
    data['teamEnemy']['roles'] = [unit(99, 'station', 12, 13)]
    data['mapInfo']['zones'] = [{'neutralType': 'weaponShop', 'pos': {'x': 1, 'y': 1}}]
    data['weaponShopList'] = [{'name': ORDER, 'price': 15}, {'name': VOUCHER, 'price': 100}]
    return data


def robot(uid, x, y, ours=True, **kw):
    return unit(uid, 'smallRobot' if ours else 'largeRobot', x, y,
                health=40 if ours else 500, attackRange=3, attackPower=5 if ours else 20,
                **kw)


class RobotScreenTests(unittest.TestCase):
    def test_budget_buys_five_small_not_boss_or_structures(self):
        data = case()
        response = Agent(Config(robot_screen_experiment=True, llm_enabled=False)).decide(data)
        buys = [v for v in response['roleCommandMap'].values() if v['action'] == 'buy']
        self.assertEqual(buys, [{'action': 'buy', 'name': ORDER, 'num': 5}])
        self.assertFalse(any(v['action'] == 'build' for v in response['roleCommandMap'].values()))

    def test_zero_budget_does_not_buy(self):
        response = Agent(Config(robot_screen_experiment=True, llm_enabled=False)).decide(case(gold=0))
        self.assertFalse(any(v['action'] in ('buy', 'build') for v in response['roleCommandMap'].values()))

    def test_rich_budget_caps_at_eight_and_duplicate_input_is_cached(self):
        agent = Agent(Config(robot_screen_experiment=True, llm_enabled=False))
        data = case(gold=999)
        first = agent.decide(data)
        self.assertEqual(first, agent.decide(copy.deepcopy(data)))
        self.assertEqual(next(v['num'] for v in first['roleCommandMap'].values() if v['action'] == 'buy'), 8)

    def test_rejected_purchase_can_retry_without_phantom_stock(self):
        data = case()
        agent = Agent(Config(robot_screen_experiment=True, llm_enabled=False))
        first = agent.decide(data)
        buyer = next(k for k,v in first['roleCommandMap'].items() if v['action'] == 'buy')
        data['roundNo'] = 2
        data['lastRoundRoleActionResults'] = {buyer: False}
        second = agent.decide(data)
        self.assertTrue(any(v['action'] == 'buy' and v['num'] == 5 for v in second['roleCommandMap'].values()))

    def test_summons_near_own_base_outside_build_zone(self):
        data = case()
        data['teamOur']['roles'][1]['backpack'] = [ORDER]
        turn, cfg, nav, ledger = setup_case(data)
        mem = Memory(); state = observe_robot_summons(turn, mem)
        prepare_screen(turn, cfg, mem, nav, ledger, state)
        point = next(iter(state['positions']))
        self.assertTrue(turn.summon_position_legal(point))
        self.assertEqual(turn.base_distance(point), 3)
        self.assertEqual(ledger.commands['10']['action'], 'use')

    def test_rejected_summon_releases_position_and_daily_slot(self):
        data = case()
        turn, _, _, _ = setup_case(data)
        mem = Memory(); state = observe_robot_summons(turn, mem)
        state.update(count=1, positions={(3, 3)}, pending=(1, 10, (3, 3)))
        data['roundNo'] = 2; data['lastRoundRoleActionResults'] = {'10': False}
        turn, _, _, _ = setup_case(data)
        state = observe_robot_summons(turn, mem)
        self.assertEqual(state['count'], 0)
        self.assertFalse(state['positions'])

    def test_full_purchase_cap_still_allows_one_base_upgrade(self):
        turn, cfg, nav, ledger = setup_case(case(gold=100))
        mem=Memory(); state=observe_robot_summons(turn, mem)
        state['purchased']=8
        prepare_screen(turn,cfg,mem,nav,ledger,state)
        self.assertEqual(ledger.commands['10']['name'], VOUCHER)
        self.assertTrue(state['station_purchased'])

    def test_no_late_purchase_without_summon_time(self):
        turn,cfg,nav,ledger=setup_case(case(round_no=69))
        mem=Memory();state=observe_robot_summons(turn,mem)
        prepare_screen(turn,cfg,mem,nav,ledger,state)
        self.assertFalse(ledger.commands)

    def test_current_shield_is_not_vacated_to_chase_other_enemy(self):
        data=case(round_no=70)
        data['teamOur']['summonRobotList']=[robot(300,5,7)]
        data['robot']['roles']=[robot(400,3,7,False),robot(401,10,8,False)]
        turn,cfg,nav,ledger=setup_case(data)
        defend_screen(turn,Memory(),nav,ledger)
        self.assertNotIn('300',ledger.commands)
        self.assertEqual(ledger.notes[300]['reason'],'robot_screen_hold')

    def test_spawned_screen_moves_home_and_never_attacks(self):
        data=case(round_no=70)
        data['teamOur']['summonRobotList']=[robot(300,3,7),robot(301,10,8)]
        turn,cfg,nav,ledger=setup_case(data)
        defend_screen(turn,Memory(),nav,ledger)
        targets=[]
        for uid,cmd in ledger.commands.items():
            self.assertEqual(cmd['action'],'move')
            target=cmd['targetPos'][0];point=(target['x'],target['y'])
            targets.append(point)
            self.assertLess(turn.base_distance(point),3)
        self.assertEqual(len(targets),2)
        self.assertEqual(len(set(targets)),2)

    def test_dizzy_robot_holds(self):
        data=case(round_no=70)
        data['teamOur']['summonRobotList']=[robot(300,3,7,abnormalState='dizzy')]
        turn,cfg,nav,ledger=setup_case(data)
        defend_screen(turn,Memory(),nav,ledger)
        self.assertFalse(ledger.commands)

    def test_corner_touch_not_assumed_protected(self):
        self.assertNotIn((1,0),ray_cells((0,0),(2,2),{(2,2)}))
        self.assertIn((1,1),ray_cells((0,0),(2,2),{(2,2)}))

    def test_all_four_base_cells_and_mirrored_spawn(self):
        data=case()
        turn,_,_,_=setup_case(data)
        self.assertEqual(len(screen_cells(turn)),12)
        pos=summon_position(turn,{'positions':set()})
        self.assertEqual(turn.base_distance(pos),3)
        data['teamOur']['roles'][0]['pos']={'x':10,'y':5}
        turn,_,_,_=setup_case(data)
        self.assertEqual(turn.base_distance(summon_position(turn,{'positions':set()})),3)

    def test_experiment_branch_runtime_default(self):
        from main3 import competition_config
        self.assertTrue(competition_config().robot_screen_experiment)
        self.assertFalse(Config().robot_screen_experiment)

    def test_second_day_returns_to_standard_strategy(self):
        data = case(round_no=130)
        experimental = Agent(Config(robot_screen_experiment=True, llm_enabled=False)).decide(data)
        standard = Agent(Config(robot_screen_experiment=False, llm_enabled=False)).decide(data)
        self.assertEqual(experimental, standard)

    def test_one_step_to_intercept_a_live_attack_line(self):
        data = case(round_no=70)
        data['teamOur']['summonRobotList'] = [robot(300, 5, 6)]
        data['robot']['roles'] = [robot(400, 3, 7, False)]
        turn, cfg, nav, ledger = setup_case(data)
        defend_screen(turn, Memory(), nav, ledger)
        action = ledger.commands['300']
        self.assertEqual(action['action'], 'move')
        target = action['targetPos'][0]
        self.assertEqual((target['x'], target['y']), (5, 7))
