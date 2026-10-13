"""Finite decision/state checks for the day-seven worker regressions."""
import copy
import unittest
from unittest.mock import patch

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import use_inventory
from agent.intelligence import Memory
from agent.mining import mine, earn
from agent.model import pos
from agent.navigation import layout
from agent.scouting import ScoutMemory, act_night_scout, prepare_night_scout
from agent.wall_watch import prepare_watch, repair_watch
from agent.worker_jobs import _continue, save_daytime_jobs


def perimeter(tick=80, day=7, worker=(11, 21), packs=3):
    data = payload((day - 1) * 130 + tick, [
        unit(13, 'station', 9, 22, health=4500, level=3),
        unit(12, 'worker', *worker, health=500, backpack=['WallFixer'] * packs),
        unit(11, 'pioneer', 8, 22, health=500)])
    data['mapInfo'].update(width=41, height=32)
    turn, cfg, _, _ = setup_case(data)
    towers, walls = layout(turn, cfg)
    data['teamOur']['roles'] += [unit(20+i, 'rocket', *p, health=2000, level=3)
                               for i, p in enumerate(towers)]
    data['teamOur']['roles'] += [unit(40000+i, 'wall', *p, health=1000)
                               for i, p in enumerate(walls)]
    cfg = Config(llm_enabled=False, layout_mode='explicit',
                 weapon_cells=[list(p) for p in towers], wall_cells=[list(p) for p in walls])
    return data, cfg


def setup(data, cfg):
    return setup_case(data, **vars(cfg))


class WorkerProductivityTests(unittest.TestCase):
    def test_later_days_clear_even_a_stale_scout_without_reserving_worker(self):
        for day in (2, 4, 7, 10):
            for tick in (55, 69, 70, 129):
                with self.subTest(day=day, tick=tick):
                    data, cfg = perimeter(tick, day)
                    data['teamEnemy']['roles'] = [unit(99, 'station', 32, 8, health=1500)]
                    turn, _, nav, ledger = setup(data, cfg)
                    memory = ScoutMemory(key=(1, 99, (32, 8)), worker_id=12,
                                         post=(29, 6), wall_entry=(29, 7))
                    fn = prepare_night_scout if turn.is_day else act_night_scout
                    self.assertIsNone(fn(turn, memory, nav, ledger))
                    self.assertEqual(memory, ScoutMemory())
                    self.assertFalse(ledger.used)
                    self.assertFalse(ledger.commands)

    def test_agent_releases_old_scout_and_later_night_collects(self):
        data, cfg = perimeter(tick=69, day=2, worker=(11, 18), packs=0)
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=10, y=17), remain=10)]
        data['vendorShopList'] = [dict(name='copper', price=8)]
        data['teamEnemy']['roles'] = [unit(99, 'station', 32, 8, health=1500)]
        agent = Agent(cfg)
        agent.decide(data)
        memory = next(iter(agent.sessions.values()))
        memory.night_scout = ScoutMemory(key=(1, 99, (32, 8)), worker_id=12,
                                        post=(29, 6), wall_entry=(29, 7))
        data['roundNo'] += 1
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(memory.night_scout, ScoutMemory())
        self.assertEqual(response['12'], command('collect', (10, 17)))

    def test_unchanged_walls_do_not_make_worker_ping_pong(self):
        data, cfg = perimeter()
        for wall in data['teamOur']['roles']:
            if wall['roleType'] == 'wall' and pos(wall['pos']) in ((12, 19), (12, 22)):
                wall['health'] = 400
        data['robot']['roles'] = [unit(90+i, 'bossRobot', x, y, attackPower=40,
                                      attackRange=3, targetTeam='challenger')
                                 for i, (x, y) in enumerate(((13, 20), (13, 21), (14, 20),
                                                            (14, 21), (15, 20), (15, 21)))]
        memory = Memory(wall_watch_id=12)
        for _ in range(4):
            turn, _, nav, ledger = setup(data, cfg)
            self.assertTrue(repair_watch(turn, memory, nav, ledger, turn.units[12], ledger.wall_cells))
            self.assertNotIn('12', ledger.commands)
            self.assertEqual(ledger.notes[12]['conditions']['target'], (12, 22))
            self.assertIn(12, ledger.used)
            data['roundNo'] += 1

    def test_urgent_wall_preempts_a_retained_preposition(self):
        data, cfg = perimeter()
        for wall in data['teamOur']['roles']:
            if wall['roleType'] == 'wall' and pos(wall['pos']) == (12, 22):
                wall['health'] = 400
        data['robot']['roles'] = [unit(90, 'bossRobot', 14, 21, attackPower=40,
                                      attackRange=3, targetTeam='challenger')]
        memory = Memory(wall_watch_id=12)
        turn, _, nav, ledger = setup(data, cfg)
        repair_watch(turn, memory, nav, ledger, turn.units[12], ledger.wall_cells)
        old = memory.wall_watch.patrol_target
        for wall in data['teamOur']['roles']:
            if wall['roleType'] == 'wall' and pos(wall['pos']) == (12, 19):
                wall['health'] = 40
                target = wall['id']
        data['roundNo'] += 1
        turn, _, nav, ledger = setup(data, cfg)
        repair_watch(turn, memory, nav, ledger, turn.units[12], ledger.wall_cells)
        self.assertNotEqual(old, target)
        self.assertEqual(memory.wall_watch.patrol_target, target)
        self.assertEqual(ledger.commands['12']['action'], 'move')

    def test_empty_watch_releases_safe_worker_but_holds_under_attack(self):
        for threatened in (False, True):
            data, cfg = perimeter(packs=0)
            if threatened:
                data['robot']['roles'] = [unit(90, 'bossRobot', 14, 21, attackRange=3,
                                              targetTeam='challenger')]
            turn, _, nav, ledger = setup(data, cfg)
            locked = repair_watch(turn, Memory(wall_watch_id=12), nav, ledger,
                                  turn.units[12], ledger.wall_cells)
            self.assertEqual(locked, threatened)
            self.assertEqual(12 in ledger.used, threatened)
            self.assertFalse(ledger.commands)

    def test_occupied_patrol_post_is_replanned_without_entering_teammate(self):
        data, cfg = perimeter()
        wall = next(w for w in data['teamOur']['roles']
                    if w['roleType'] == 'wall' and pos(w['pos']) == (12, 22))
        wall['health'] = 400
        memory = Memory(wall_watch_id=12)
        turn, _, nav, ledger = setup(data, cfg)
        repair_watch(turn, memory, nav, ledger, turn.units[12], ledger.wall_cells)
        post = memory.wall_watch.patrol_post
        data['teamOur']['roles'][1]['pos'] = dict(x=10, y=23)
        data['teamOur']['roles'].append(unit(15, 'worker', *post, health=500))
        data['roundNo'] += 1
        turn, _, nav, ledger = setup(data, cfg)
        self.assertTrue(repair_watch(turn, memory, nav, ledger, turn.units[12], ledger.wall_cells))
        self.assertEqual(memory.wall_watch.patrol_target, wall['id'])
        self.assertNotEqual(memory.wall_watch.patrol_post, post)
        self.assertNotEqual(pos(ledger.commands['12']['targetPos'][0]), post)

    def test_observed_healing_and_daybreak_clear_patrol(self):
        data, cfg = perimeter()
        wall = next(w for w in data['teamOur']['roles']
                    if w['roleType'] == 'wall' and pos(w['pos']) == (12, 22))
        wall['health'] = 400
        memory = Memory(wall_watch_id=12)
        turn, _, nav, ledger = setup(data, cfg)
        memory.wall_watch.observe(turn, memory)
        repair_watch(turn, memory, nav, ledger, turn.units[12], ledger.wall_cells)
        self.assertIsNotNone(memory.wall_watch.patrol_target)
        data['roundNo'] += 1
        wall['health'] = 1000
        turn, _, _, _ = setup(data, cfg)
        memory.wall_watch.observe(turn, memory)
        self.assertIsNone(memory.wall_watch.patrol_target)
        memory.wall_watch.patrol_target, memory.wall_watch.patrol_post = wall['id'], (11, 21)
        data['roundNo'] = 7 * 130
        turn, _, _, _ = setup(data, cfg)
        memory.wall_watch.observe(turn, memory)
        self.assertIsNone(memory.wall_watch.patrol_target)
        self.assertIsNone(memory.wall_watch.patrol_post)

    def mining_case(self, tick=25):
        data, cfg = perimeter(tick=tick, worker=(16, 12), packs=0)
        data['teamOur']['roles'][1]['backpack'] = ['iron'] * 3 + ['copper'] * 10
        data['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y), remain=10)
                                  for k, x, y in [('copper', 1, 20), ('vendor', 20, 16)]]
        data['vendorShopList'] = [dict(name=k, price=p) for k, p in [('copper', 8), ('iron', 5)]]
        return data, cfg

    def test_far_mine_is_rejected_when_today_sale_and_return_do_not_fit(self):
        data, cfg = self.mining_case()
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertFalse(mine(turn, cfg, Memory(), nav, ledger, turn.units[12],
                              stockpile=True, cashout_today=True))
        self.assertFalse(ledger.commands)
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertTrue(mine(turn, cfg, Memory(), nav, ledger, turn.units[12], stockpile=True))
        self.assertEqual(ledger.work_jobs[12]['run']['mode'], 'carry_for_later')

    def test_carry_run_is_not_cancelled_by_a_later_sale_phase(self):
        data, cfg = self.mining_case()
        memory = Memory()
        turn, cfg, nav, ledger = setup(data, cfg)
        mine(turn, cfg, memory, nav, ledger, turn.units[12], stockpile=True)
        save_daytime_jobs(turn, memory, ledger)
        job = copy.deepcopy(memory.daytime_jobs[12])
        data['teamOur']['roles'][1]['pos'] = ledger.commands['12']['targetPos'][0]
        data['roundNo'] += 12
        turn, cfg, nav, ledger = setup(data, cfg)
        with patch('agent.economy.batch_sale_ready', return_value=True):
            accepted, _ = _continue(turn, cfg, memory, nav, ledger, turn.units[12],
                                    job, cfg.weapon_cells, ledger.wall_cells)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['12']['action'], 'move')
        self.assertEqual(ledger.work_jobs[12]['run'], job['run'])

    def test_planned_collection_goal_does_not_grow_each_turn(self):
        data, cfg = self.mining_case()
        data['teamOur']['roles'][1]['pos'] = dict(x=2, y=19)
        turn, cfg, nav, ledger = setup(data, cfg)
        memory = Memory()
        mine(turn, cfg, memory, nav, ledger, turn.units[12], stockpile=True)
        job = ledger.work_jobs[12]
        goal = job['run']['collect_goal']
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['backpack'].append('copper')
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertTrue(_continue(turn, cfg, memory, nav, ledger, turn.units[12], job,
                                  cfg.weapon_cells, ledger.wall_cells)[0])
        self.assertEqual(ledger.work_jobs[12]['run']['collect_goal'], goal)
        self.assertEqual(ledger.notes[12]['conditions']['expected_units'], 9)

    def test_today_run_continues_at_sale_phase_without_abandoning_remaining_yield(self):
        data, cfg = self.mining_case(tick=5)
        data['mapInfo']['zones'][0]['pos'] = dict(x=7, y=16)
        memory = Memory()
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertTrue(mine(turn, cfg, memory, nav, ledger, turn.units[12],
                             stockpile=True, cashout_today=True))
        job = ledger.work_jobs[12]
        self.assertEqual(job['run']['mode'], 'sell_today')
        data['roundNo'] += 30
        data['teamOur']['roles'][1]['pos'] = dict(x=8, y=15)
        data['teamOur']['roles'][1]['backpack'] += ['copper'] * 8
        turn, cfg, nav, ledger = setup(data, cfg)
        with patch('agent.economy.batch_sale_ready', return_value=True):
            self.assertTrue(_continue(turn, cfg, memory, nav, ledger, turn.units[12], job,
                                      ledger.tower_cells, ledger.wall_cells)[0])
        self.assertEqual(ledger.commands['12']['action'], 'collect')
        self.assertEqual(ledger.work_jobs[12]['run'], job['run'])

    def test_today_run_sells_at_accepted_vendor_when_collection_is_complete(self):
        data, cfg = self.mining_case(tick=5)
        turn, cfg, nav, ledger = setup(data, cfg)
        memory = Memory()
        mine(turn, cfg, memory, nav, ledger, turn.units[12], stockpile=True, cashout_today=True)
        job = ledger.work_jobs[12]
        data['roundNo'] += 35
        data['teamOur']['roles'][1]['pos'] = dict(x=19, y=15)
        data['teamOur']['roles'][1]['backpack'] += ['copper'] * 10
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertTrue(_continue(turn, cfg, memory, nav, ledger, turn.units[12], job,
                                  ledger.tower_cells, ledger.wall_cells)[0])
        self.assertEqual(ledger.commands['12'], command('sell', name='copper', num=20))
        self.assertEqual(ledger.work_jobs[12]['target'], job['run']['vendor'])

    def test_today_run_requires_a_positive_sale_price(self):
        data, cfg = self.mining_case(tick=5)
        data['vendorShopList'][0]['price'] = 0
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertFalse(mine(turn, cfg, Memory(), nav, ledger, turn.units[12],
                              stockpile=True, cashout_today=True))
        self.assertFalse(ledger.commands)
        self.assertTrue(mine(turn, cfg, Memory(), nav, ledger, turn.units[12], stockpile=True))

    def shopping_case(self, tick=33, worker=(24, 19)):
        data, cfg = perimeter(tick=tick, worker=worker, packs=0)
        data['teamOur']['goldNum'] = 55
        data['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1'] * 2
        for wall in data['teamOur']['roles']:
            if wall['roleType'] == 'wall' and pos(wall['pos']) not in ((12, 21), (12, 22)):
                wall.update(level=2, health=1500)
        data['mapInfo']['zones'] = [dict(neutralType='weaponShop', pos=dict(x=25, y=20))]
        data['weaponShopList'] = [dict(name='WallFixer', price=15),
                                  dict(name='WallUpgradeVoucher1', price=20)]
        return data, cfg

    def test_safety_packs_finish_at_same_shop_before_paid_delivery(self):
        data, cfg = self.shopping_case(tick=35)
        memory = Memory(wall_watch_id=12, daytime_jobs={12: dict(
            kind='use', target=(12, 21), name='WallUpgradeVoucher1')})
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertEqual(prepare_watch(turn, cfg, memory, nav, ledger, turn.units[12], ledger.wall_cells),
                         (True, 0))
        self.assertEqual(ledger.commands['12'], command('buy', name='WallFixer', num=3))
        self.assertEqual(ledger.gold, 10)
        data['roundNo'] += 1
        data['teamOur']['goldNum'] = 200
        data['teamOur']['roles'][1]['backpack'] += ['WallFixer'] * 3
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertEqual(prepare_watch(turn, cfg, memory, nav, ledger, turn.units[12], ledger.wall_cells),
                         (True, 0))
        self.assertEqual(ledger.commands['12'], command('buy', name='WallFixer', num=9))
        data['roundNo'] += 1
        data['teamOur']['goldNum'] = 65
        data['teamOur']['roles'][1]['backpack'] += ['WallFixer'] * 9
        turn, cfg, nav, ledger = setup(data, cfg)
        prepare_watch(turn, cfg, memory, nav, ledger, turn.units[12], ledger.wall_cells)
        self.assertEqual(memory.wall_supply_trip['phase'], 'deliver')
        self.assertEqual(ledger.commands['12']['action'], 'move')

    def test_shared_shop_order_reserves_safety_stock_once(self):
        data, cfg = self.shopping_case(tick=35)
        data['teamOur']['goldNum'] = 100
        data['teamOur']['roles'][1]['backpack'] = []
        job = dict(kind='buy', target=(25, 20), name='WallUpgradeVoucher1', quantity=2)
        memory = Memory(wall_watch_id=12, daytime_jobs={12: job})
        turn, cfg, nav, ledger = setup(data, cfg)
        locked, funds = prepare_watch(turn, cfg, memory, nav, ledger, turn.units[12], ledger.wall_cells)
        self.assertTrue(locked)
        self.assertEqual(ledger.commands['12'], command('buy', name='WallFixer', num=4))
        self.assertEqual(funds, 40)
        ledger.gold -= funds
        self.assertEqual(ledger.gold, 0)
        data['roundNo'] += 1
        data['teamOur']['goldNum'] = 40
        data['teamOur']['roles'][1]['backpack'] = ['WallFixer'] * 4
        turn, cfg, nav, ledger = setup(data, cfg)
        locked, funds = prepare_watch(turn, cfg, memory, nav, ledger, turn.units[12], ledger.wall_cells)
        self.assertTrue(locked)
        self.assertEqual(funds, 0)
        self.assertEqual(ledger.commands['12'], command('buy', name='WallUpgradeVoucher1', num=2))
        self.assertEqual(ledger.gold, 0)

    def test_daytime_wall_delivery_uses_outside_but_still_requires_return_time(self):
        for tick, accepted, mirror in ((40, True, False), (69, False, False),
                                       (40, True, True), (69, False, True)):
            data, cfg = self.shopping_case(tick=tick, worker=(13, 20))
            target = (12, 21)
            if mirror:
                for role in data['teamOur']['roles']:
                    role['pos']['x'] = 40 - role['pos']['x'] - (role['roleType'] == 'station')
                for zone in data['mapInfo']['zones']:
                    zone['pos']['x'] = 40 - zone['pos']['x']
                cfg.wall_cells = [[40-x, y] for x, y in cfg.wall_cells]
                cfg.weapon_cells = [[40-x, y] for x, y in cfg.weapon_cells]
                target = (28, 21)
            turn, _, nav, ledger = setup(data, cfg)
            memory = Memory(wall_watch_id=12)
            self.assertEqual(use_inventory(turn, nav, ledger, turn.units[12], mem=memory,
                                           target_only=target), accepted)
            if accepted:
                self.assertEqual(ledger.commands['12'],
                                 command('use', target, name='WallUpgradeVoucher1'))


if __name__ == '__main__':
    unittest.main()
