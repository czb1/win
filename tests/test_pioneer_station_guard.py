"""Reserved base healing and adjacent standby when no gun exists."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.daytime import guard_station_with_pioneer
from agent.intelligence import Memory
from agent.model import distance, neighbours, pos
from agent.spending import prepare_first_day_pioneer


BASE = 'StationUpgradeVoucher1'
BOSS = 'BossRobotSummonOrder'


class PioneerStationGuardTests(unittest.TestCase):
    def data(self, round_no=70, side='challenger', health=1500, backpack=None):
        home, enemy, start = ((9, 22), (30, 10), (11, 21)) if side == 'challenger' else (
            (30, 10), (9, 22), (29, 11))
        uid = 10011 if side == 'challenger' else 20011
        data = payload(round_no, [unit(uid + 2, 'station', *home, health=health),
                                  unit(uid, 'pioneer', *start, health=500,
                                       backpack=[BASE] if backpack is None else backpack)])
        data['teamOur'].update(type=side, goldNum=0)
        data['mapInfo'].update(width=41, height=32)
        data['teamEnemy']['roles'] = [unit(99, 'station', *enemy, health=1500)]
        return data

    def state(self, phase='robots'):
        return dict(phase=phase, station_item=BASE, pending_buy=None, robot_purchased=3)

    def case(self, data):
        return setup_case(data, layout_mode='explicit', llm_enabled=False)

    def test_both_sides_move_to_adjacent_rear_hold_at_500_and_heal_at_499(self):
        for side, post in (('challenger', (8, 23)), ('defender', (32, 8))):
            with self.subTest(side=side):
                data = self.data(side=side)
                _, cfg, _, _ = self.case(data)
                agent = Agent(cfg)
                uid = str(data['teamOur']['roles'][1]['id'])
                result = agent.decide(copy.deepcopy(data))['roleCommandMap']
                mem = next(iter(agent.sessions.values()))
                self.assertEqual(result[uid]['action'], 'move')
                self.assertEqual(mem.pioneer_wait_post, post)
                data['roundNo'] += 1
                data['teamOur']['roles'][1]['pos'] = dict(zip(('x', 'y'), post))
                data['teamOur']['roles'][0]['health'] = 500
                self.assertNotIn(uid, agent.decide(copy.deepcopy(data))['roleCommandMap'])
                data['roundNo'] += 1
                data['teamOur']['roles'][0]['health'] = 499
                result = agent.decide(copy.deepcopy(data))['roleCommandMap']
                turn, _, _, _ = self.case(data)
                self.assertEqual(result[uid], command('use', turn.station.nearest_cell(post), name=BASE))

    def test_finished_summons_return_with_voucher_and_no_guns(self):
        for phase in ('robots', 'station', 'return'):
            with self.subTest(phase=phase):
                data = self.data(round_no=55)
                turn, cfg, nav, ledger = self.case(data)
                mem = Memory(first_day_pioneer=self.state(phase))
                self.assertTrue(prepare_first_day_pioneer(turn, cfg, mem, nav, ledger))
                self.assertEqual(mem.first_day_pioneer['phase'], 'return')
                self.assertEqual(ledger.commands[str(turn.pioneer.id)]['action'], 'move')
                self.assertEqual(ledger.notes[turn.pioneer.id]['reason'], 'pioneer_station_guard')
                self.assertEqual(turn.base_distance(mem.pioneer_wait_post), 1)
                self.assertEqual(turn.pioneer.inventory[BASE], 1)

    def test_night_guard_preserves_orders_and_resumes_summons_in_daylight(self):
        data = self.data(round_no=69, backpack=[BASE, BOSS])
        _, cfg, _, _ = self.case(data)
        agent = Agent(cfg)
        agent.decide(copy.deepcopy(data))
        mem = next(iter(agent.sessions.values()))
        mem.first_day_pioneer = self.state()
        data['roundNo'] = 70
        result = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(result['10011']['action'], 'move')
        self.assertEqual(mem.first_day_pioneer['phase'], 'robots')
        data['roundNo'] = 130
        result = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual((result['10011']['action'], result['10011']['name']), ('use', BOSS))

    def test_emergency_healing_preempts_opening_summons_in_both_phases(self):
        for round_no in (55, 70):
            for level in (1, 2):
                for health in (499, 500):
                    with self.subTest(round_no=round_no, level=level, health=health):
                        name = f'StationUpgradeVoucher{level}'
                        data = self.data(round_no=round_no - 1, backpack=[name, BOSS])
                        data['teamOur']['roles'][0]['level'] = level
                        data['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 23}
                        _, cfg, _, _ = self.case(data)
                        agent = Agent(cfg)
                        agent.decide(copy.deepcopy(data))
                        mem = next(iter(agent.sessions.values()))
                        mem.first_day_pioneer = dict(self.state(), station_item=name)
                        data.update(roundNo=round_no, lastRoundRoleActionResults={'10011': False})
                        data['teamOur']['roles'][0]['health'] = health
                        result = agent.decide(copy.deepcopy(data))['roleCommandMap']
                        action = result.get('10011', {})
                        self.assertEqual(action.get('name') == name, health < 500)
                        if health == 500 and round_no == 55:
                            self.assertEqual((action['action'], action['name']), ('use', BOSS))

    def test_occupied_reserved_and_planned_cells_cannot_be_guard_posts(self):
        data = self.data()
        data['teamOur']['roles'].append(unit(10010, 'worker', 8, 23))
        turn, _, nav, ledger = self.case(data)
        ledger.reserved.add((8, 22))
        ledger.tower_cells.add((9, 23))
        ledger.wall_cells.add((10, 23))
        mem = Memory()
        self.assertTrue(guard_station_with_pioneer(turn, mem, nav, ledger))
        self.assertNotIn(mem.pioneer_wait_post, {(8, 23), (8, 22), (9, 23), (10, 23)})
        self.assertEqual(turn.base_distance(mem.pioneer_wait_post), 1)

    def test_live_threat_overrides_enemy_base_direction(self):
        data = self.data()
        data['robot']['roles'] = [unit(90, 'bossRobot', 4, 23, attackRange=3,
                                       targetTeam='challenger')]
        turn, _, nav, ledger = self.case(data)
        mem = Memory()
        self.assertTrue(guard_station_with_pioneer(turn, mem, nav, ledger))
        self.assertGreater(distance(mem.pioneer_wait_post, (4, 23)), 4)
        self.assertEqual(turn.base_distance(mem.pioneer_wait_post), 1)
        self.assertGreater(distance(pos(ledger.commands['10011']['targetPos'][0]), (4, 23)), 4)

    def test_all_adjacent_cells_blocked_hold_without_illegal_move(self):
        data = self.data()
        turn, _, _, _ = self.case(data)
        ring = {p for c in turn.station.cells for p in neighbours(c)} - turn.station.cells
        data['teamOur']['roles'][1]['pos'] = {'x': 13, 'y': 21}
        data['teamOur']['roles'] += [unit(40000 + i, 'wall', *p) for i, p in enumerate(sorted(ring))]
        turn, _, nav, ledger = self.case(data)
        self.assertTrue(guard_station_with_pioneer(turn, Memory(), nav, ledger))
        self.assertFalse(ledger.commands)
        self.assertIn(turn.pioneer.id, ledger.used)
        self.assertEqual(ledger.notes[turn.pioneer.id]['reason'], 'pioneer_station_guard_blocked')

    def test_guard_holds_and_reserves_its_post_without_an_enemy_base(self):
        data = self.data()
        data['teamEnemy']['roles'] = []
        data['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 23}
        turn, _, nav, ledger = self.case(data)
        self.assertTrue(guard_station_with_pioneer(turn, Memory(), nav, ledger))
        self.assertFalse(ledger.commands)
        self.assertIn(turn.pioneer.pos, ledger.reserved)

    def test_new_gun_releases_guard_and_pioneer_fires(self):
        data = self.data()
        _, cfg, _, _ = self.case(data)
        cfg.loadout = ['rocket'] * 3
        agent = Agent(cfg)
        agent.decide(copy.deepcopy(data))
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 23}
        data['teamOur']['roles'].append(unit(10040, 'rocket', 8, 22, attackRange=10))
        data['robot']['roles'] = [unit(90, 'smallRobot', 14, 23, attackRange=3,
                                       targetTeam='challenger')]
        result = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertTrue(any(c['action'] == 'attack' and c['controllerId'] == '10011'
                            for c in result.values()))

    def test_daytime_active_task_is_not_replaced_by_guard(self):
        data = self.data(round_no=140)
        data['phaseTask'] = 'active task'
        turn, _, nav, ledger = self.case(data)
        self.assertFalse(guard_station_with_pioneer(turn, Memory(), nav, ledger))
        self.assertNotIn(turn.pioneer.id, ledger.used)

    def test_idle_vehicle_cannot_pull_guard_away_from_base(self):
        data = self.data(round_no=140)
        data['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 23}
        data['mapInfo']['zones'] = [dict(neutralType='challengerVehicle', pos={'x': 12, 'y': 20})]
        _, cfg, _, _ = self.case(data)
        agent = Agent(cfg)
        result = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertNotIn('10011', result)
        self.assertEqual(next(iter(agent.sessions.values())).pioneer_wait_post, (8, 23))
