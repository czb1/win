"""Finite first-day snapshots for the fixed worker's ordered task chain."""
import copy
import unittest

from test_agent import setup_case, unit
from test_first_day_wall_block import BOSS, block_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.model import pos
from agent.opening_offense import act_first_day_offense
from agent.robot_assault import act_robots, RobotAssaultMemory


TOWERS = [[8, 23], [9, 23], [8, 21]]
WALLS = [[10, 20], [10, 19]]


def case(tick=25, stock=(), ready=True, gold=433):
    data = block_case(tick)
    worker = data['teamOur']['roles'][1]
    worker.update(pos={'x': 9, 'y': 20}, backpack=list(stock))
    data['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 22}
    if not ready:
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 22]
    data['teamOur']['goldNum'] = gold
    data['vendorShopList'] = [{'name': k, 'price': v} for k, v in
                              (('stone', 5), ('iron', 10), ('copper', 8))]
    data['mapInfo']['zones'] += [
        {'neutralType': 'vendor', 'pos': {'x': 12, 'y': 20}},
        {'neutralType': 'iron', 'pos': {'x': 8, 'y': 20}, 'remain': 10}]
    return data


def cfg(walls=WALLS):
    return Config(llm_enabled=False, layout_mode='explicit',
                  weapon_cells=TOWERS, wall_cells=walls)


def step(data, memory, walls=WALLS):
    turn, config, nav, ledger = setup_case(
        data, llm_enabled=False, layout_mode='explicit', weapon_cells=TOWERS, wall_cells=walls)
    act_robots(turn, RobotAssaultMemory(), nav, ledger)
    worker = act_first_day_offense(turn, config, memory, nav, ledger,
                                   sorted(ledger.tower_cells), sorted(ledger.wall_cells))
    return worker, ledger


class FirstDayOffenseTests(unittest.TestCase):
    def test_tick_25_preempts_mining_and_releases_old_work_locks(self):
        data = case(24, stock=['stone', 'iron'])
        memory = Memory(daytime_jobs={10: dict(kind='mine', target=(8, 20), ore='iron')},
                        mine_targets={10: (8, 20)}, build_targets={10: (8, 21)},
                        preparation_workers={10}, return_targets={10: 20},
                        return_posts={10: (7, 23)}, supply_worker=10)
        worker, ledger = step(data, memory)
        self.assertIsNone(worker)
        self.assertFalse(ledger.used)
        data['roundNo'] = 25
        worker, ledger = step(data, memory)
        self.assertEqual(worker.id, 10)
        self.assertEqual(ledger.commands['10']['action'], 'build')
        self.assertEqual(ledger.commands['10']['name'], 'wall')
        self.assertNotIn(10, memory.mine_targets)
        self.assertNotIn(10, memory.return_targets)
        self.assertNotIn(10, memory.preparation_workers)
        self.assertIsNone(memory.supply_worker)
        self.assertEqual(ledger.notes[10]['conditions']['stop_mining_tick'], 25)

    def test_order_is_stone_walls_then_all_ores_then_batch_purchase(self):
        memory = Memory()
        data = case(stock=['stone', 'stone', 'iron', 'iron', 'copper'])
        for tick in (25, 26):
            data['roundNo'] = tick
            _, ledger = step(data, memory)
            built = ledger.commands['10']
            self.assertEqual((built['action'], built['name']), ('build', 'wall'))
            data['teamOur']['roles'].append(unit(100 + tick, 'wall', *pos(built['targetPos'][0])))
            data['teamOur']['roles'][1]['backpack'].remove('stone')
        data['roundNo'] = 27
        _, ledger = step(data, memory)
        self.assertEqual(ledger.commands['10']['action'], 'move')
        self.assertEqual(memory.first_day_offense['phase'], 'sell')
        data['teamOur']['roles'][1]['pos'] = {'x': 11, 'y': 20}
        data['roundNo'] = 28
        _, ledger = step(data, memory)
        self.assertEqual(ledger.commands['10'], command('sell', name='iron', num=2))
        # Failed sale / unchanged inventory must not advance to purchasing.
        data['roundNo'] = 29
        data['lastRoundRoleActionResults'] = {'10': False}
        _, ledger = step(data, memory)
        self.assertEqual(ledger.commands['10'], command('sell', name='iron', num=2))
        data['teamOur']['roles'][1]['backpack'] = ['copper']
        data['roundNo'] = 30
        _, ledger = step(data, memory)
        self.assertEqual(ledger.commands['10'], command('sell', name='copper', num=1))
        data['teamOur']['roles'][1].update(backpack=[], pos={'x': 14, 'y': 20})
        data['roundNo'] = 31
        _, ledger = step(data, memory)
        self.assertEqual(ledger.commands['10'], command('buy', name=BOSS, num=3))
        self.assertEqual(memory.first_day_offense['phase'], 'buy')
        data['teamOur']['roles'][1]['backpack'] = [BOSS] * 3
        data['roundNo'] = 32
        _, ledger = step(data, memory)
        self.assertEqual(memory.first_day_offense['phase'], 'block')
        self.assertEqual(ledger.commands['10']['action'], 'move')
        self.assertIsNotNone(memory.night_scout.wall_entry)

    def test_two_workers_split_purchase_and_construction_before_three_guns(self):
        data = case(25, ready=False)
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        data['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 20}
        response = Agent(cfg(walls=[])).decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=BOSS, num=3))
        self.assertEqual(response['12'], command('build', (8, 21), name='rocket'))

    def test_fixed_tick_25_chain_overrides_a_longer_optional_mining_window(self):
        data = case(24)
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        for role in data['teamOur']['roles']:
            if role['roleType'] == 'rocket':
                role['level'] = 3
        config = cfg(walls=[])
        config.economy_rounds = 60
        agent = Agent(config)
        response = agent.decide(data)['roleCommandMap']
        self.assertFalse(any(c['action'] == 'buy' and c.get('name') == BOSS for c in response.values()))
        data['roundNo'] = 25
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('buy', name=BOSS, num=3))
        self.assertEqual(next(iter(agent.sessions.values())).first_day_offense['worker_id'], 10)

    def test_sale_and_shop_movement_keep_their_accepted_job_context(self):
        for stock, kind, target in ((['iron'], 'sell', (12, 20)),
                                    ([], 'robot_buy', (15, 20))):
            with self.subTest(kind=kind):
                data = case(35, stock=stock)
                agent = Agent(cfg(walls=[]))
                response = agent.decide(data)['roleCommandMap']
                self.assertEqual(response['10']['action'], 'move')
                memory = next(iter(agent.sessions.values()))
                self.assertEqual(memory.daytime_jobs[10]['kind'], kind)
                self.assertEqual(memory.daytime_jobs[10]['target'], target)

    def test_purchase_preserves_missing_gun_gold(self):
        data = case(35, ready=False, gold=144)
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        _, ledger = step(data, Memory(), walls=[])
        self.assertFalse(any(c['action'] == 'buy' for c in ledger.commands.values()))
        self.assertEqual(ledger.notes[10]['reason'], 'first_day_offense_wait_gold')
        self.assertEqual(ledger.notes[10]['conditions']['reserved_construction_gold'], 25)
        data['teamOur']['goldNum'] = 145
        _, ledger = step(data, Memory(), walls=[])
        self.assertEqual(ledger.commands['10'], command('buy', name=BOSS, num=1))
        self.assertEqual(ledger.gold, 25)

    def test_actual_occupation_allows_same_worker_to_use_three_orders_and_hold(self):
        agent = Agent(cfg(walls=[]))
        data = case(25, stock=[BOSS] * 3, ready=False)
        agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        entry = memory.night_scout.wall_entry
        self.assertIsNotNone(entry)
        worker = data['teamOur']['roles'][1]
        worker['pos'] = {'x': entry[0], 'y': entry[1]}
        placements = []
        for tick, stock in ((26, 3), (27, 2), (28, 1)):
            data['roundNo'] = tick
            worker['backpack'] = [BOSS] * stock
            data['lastRoundRoleActionResults'] = {'10': True}
            response = agent.decide(data)
            use = response['roleCommandMap']['10']
            self.assertEqual((use['action'], use['name']), ('use', BOSS))
            placements.append(pos(use['targetPos'][0]))
            self.assertEqual(agent.decide(copy.deepcopy(data)), response)
        self.assertEqual(len(set(placements)), 3)
        self.assertEqual(memory.robot_summon_state['count'], 3)
        worker['backpack'] = []
        data['roundNo'] = 67
        response = agent.decide(data)
        self.assertNotIn('10', response['roleCommandMap'])
        self.assertNotIn(10, memory.return_targets)
        self.assertEqual(memory.first_day_offense['phase'], 'block')
        data['roundNo'] = 70
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['10']['action'], 'move')
        self.assertNotEqual(pos(response['roleCommandMap']['10']['targetPos'][0]), entry)

    def test_failed_buy_retries_without_advancing_or_double_counting(self):
        data = case(35)
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        agent = Agent(cfg(walls=[]))
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['10'], command('buy', name=BOSS, num=3))
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)
        self.assertEqual(memory.robot_summon_state['purchased'], 3)
        data['roundNo'] = 36
        data['lastRoundRoleActionResults'] = {'10': False}
        response = agent.decide(data)
        self.assertNotIn('10', response['roleCommandMap'])
        self.assertEqual(memory.robot_summon_state['purchased'], 0)
        self.assertEqual(memory.first_day_offense['phase'], 'buy')
        data['roundNo'] = 41
        data['lastRoundRoleActionResults'] = {}
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['10'], command('buy', name=BOSS, num=3))
        self.assertEqual(memory.robot_summon_state['purchased'], 3)
        self.assertEqual(memory.first_day_offense['phase'], 'buy')

    def test_worker_stays_fixed_when_the_other_gets_closer_and_replaces_only_after_death(self):
        memory = Memory()
        data = case(35, stock=[BOSS])
        step(data, memory, walls=[])
        data['roundNo'] = 36
        data['teamOur']['roles'][2]['pos'] = {'x': 30, 'y': 13}
        worker, _ = step(data, memory, walls=[])
        self.assertEqual(worker.id, 10)
        data['roundNo'] = 37
        data['teamOur']['roles'][1]['health'] = 0
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        worker, _ = step(data, memory, walls=[])
        self.assertEqual(worker.id, 12)
        self.assertEqual(memory.first_day_offense['worker_id'], 12)
        self.assertEqual(memory.night_scout.worker_id, 12)

    def test_held_stone_without_build_site_is_sold_instead_of_remined(self):
        data = case(35, stock=['stone'])
        data['teamOur']['roles'][1]['pos'] = {'x': 11, 'y': 20}
        worker, ledger = step(data, Memory(), walls=[])
        self.assertEqual(worker.id, 10)
        self.assertEqual(ledger.commands['10'], command('sell', name='stone', num=1))

    def test_no_route_or_money_never_returns_assigned_worker_to_mining(self):
        data = case(35, gold=0)
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        agent = Agent(cfg(walls=[]))
        response = agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        self.assertNotIn('10', response['roleCommandMap'])
        self.assertEqual(memory.first_day_offense['phase'], 'buy')
        data['roundNo'] = 54
        data['mapInfo']['zones'] = [z for z in data['mapInfo']['zones'] if z['neutralType'] != 'weaponShop']
        data['teamEnemy']['roles'] += [unit(300 + x * 32 + y, 'wall', x, y, health=1000)
            for x in range(28, 34) for y in (7, 12)]
        response = agent.decide(data)
        self.assertNotIn('10', response['roleCommandMap'])
        self.assertEqual(memory.first_day_offense['phase'], 'block')
        self.assertEqual(memory.first_day_offense['worker_id'], 10)

    def test_late_purchase_accounts_for_travel_to_the_wall(self):
        data = case(60)
        data['teamOur']['roles'][1]['pos'] = {'x': 14, 'y': 20}
        memory = Memory()
        _, ledger = step(data, memory, walls=[])
        self.assertFalse(any(c['action'] == 'buy' for c in ledger.commands.values()))
        self.assertEqual(memory.first_day_offense['phase'], 'block')
        self.assertEqual(memory.first_day_offense['transition'], 'shop_or_wall_daylight_deadline')

    def test_only_worker_finishes_guns_and_day_two_releases_the_role(self):
        data = case(35, ready=False)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 12]
        memory = Memory()
        worker, _ = step(data, memory, walls=[])
        self.assertIsNone(worker)
        self.assertFalse(memory.first_day_offense)
        data = case(35, stock=[BOSS])
        step(data, memory, walls=[])
        data['roundNo'] = 130
        worker, ledger = step(data, memory, walls=[])
        self.assertIsNone(worker)
        self.assertFalse(memory.first_day_offense)
        self.assertIsNone(ledger.first_day_offense_worker_id)

    def test_unarmed_face_survives_mirrors_and_both_factions(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = case(35, stock=[BOSS] * 3)
                data['teamEnemy']['roles'].append(unit(20040, 'rocket', 30, 11, health=1000))
                for team in ('teamOur', 'teamEnemy'):
                    for role in data[team]['roles']:
                        x, y = pos(role['pos'])
                        station = role['roleType'] == 'station'
                        role['pos'] = {'x': 40 - x - int(station) if flip_x else x,
                                       'y': 31 - y + int(station) if flip_y else y}
                        role['id'] += 10000 if flip_x else 0
                data['mapInfo']['zones'] = []
                memory = Memory()
                worker, ledger = step(data, memory, walls=[])
                self.assertEqual(worker.id, 10010 if flip_x else 10)
                self.assertEqual(memory.night_scout.wall_entry[1], 24 if flip_y else 7)
                self.assertEqual(ledger.commands[str(worker.id)]['action'], 'move')
