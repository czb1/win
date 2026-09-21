"""Deterministic frontline, night release and once-per-day sale policies."""
import unittest

from test_agent import payload, unit, setup_case
from test_mining_runs import mining_case
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Memory
from agent.mining import earn
from agent.model import Turn
from agent.navigation import layout
from agent.economy import build, wall_keeps_access


class DailySaleTests(unittest.TestCase):
    def test_mixed_batch_then_new_ore_waits_for_next_day(self):
        mem = Memory(day=1)
        for rno, inventory, expected in (
                (38, ['copper', 'iron'], 'sell'),
                (39, ['iron'], 'sell'),
                (40, [], 'collect'),
                (41, ['iron'] * 100, None),
                (130, ['iron'] * 100, 'sell')):
            p = mining_case(rno, inventory, zones=[('iron', 6, 5), ('vendor', 4, 5)])
            p['lastRoundRoleActionResults'] = {'1': True}
            t, cfg, nav, ledger = setup_case(p)
            mem.observe(t, cfg)
            earn(t, cfg, mem, nav, ledger, t.workers[0], deadline=40)
            self.assertEqual(ledger.commands.get('1', {}).get('action'), expected)
            mem.last_round, mem.last_commands = rno, ledger.commands

    def test_sold_worker_does_not_start_another_daytime_mining_trip(self):
        p = mining_case(45, zones=[('iron', 9, 5), ('vendor', 4, 5)])
        t, cfg, nav, ledger = setup_case(p)
        self.assertFalse(earn(t, cfg, Memory(day=1, sold_workers={1}), nav, ledger, t.workers[0]))
        self.assertFalse(ledger.commands)
        p['roundNo'] = 80
        t, cfg, nav, ledger = setup_case(p)
        self.assertTrue(earn(t, cfg, Memory(day=1, sold_workers={1}), nav, ledger, t.workers[0]))
        self.assertEqual(ledger.commands['1']['action'], 'move')

    def test_failed_sale_retries_same_visit(self):
        p = mining_case(39, ['iron'], zones=[('iron', 6, 5), ('vendor', 4, 5)])
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(day=1)
        earn(t, cfg, mem, nav, ledger, t.workers[0], deadline=40)
        mem.last_round, mem.last_commands = 39, ledger.commands
        p.update(roundNo=40, lastRoundRoleActionResults={'1': False})
        t, cfg, nav, ledger = setup_case(p)
        mem.observe(t, cfg)
        earn(t, cfg, mem, nav, ledger, t.workers[0], deadline=40)
        self.assertEqual(ledger.commands['1']['action'], 'sell')

    def test_interrupted_visit_cannot_restart_after_other_work(self):
        mem = Memory(day=1, sold_workers={1}, sale_workers={1}, last_round=45,
                     last_commands={'1': {'action': 'collect', 'targetPos': [{'x': 6, 'y': 5}]}})
        p = mining_case(46, ['iron'] * 100, zones=[('iron', 6, 5), ('vendor', 4, 5)])
        t, cfg, nav, ledger = setup_case(p)
        mem.observe(t, cfg)
        earn(t, cfg, mem, nav, ledger, t.workers[0], force_sale=True)
        self.assertFalse(ledger.commands)


class NightMiningTests(unittest.TestCase):
    def night_case(self, rno=80):
        p = payload(rno, [unit(1, 'worker', 5, 5), unit(20, 'rocket', 5, 6),
                          unit(13, 'station', 3, 11)])
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 6, 'y': 5}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        return p

    def test_clear_night_mines_without_vendor_or_daylight_budget(self):
        for rno in (70, 80, 129):
            result = Agent(Config(llm_enabled=False)).decide(self.night_case(rno))
            self.assertEqual(result['roleCommandMap']['1']['action'], 'collect')

    def test_wave_dead_then_new_wave_switches_back_to_defence(self):
        agent = Agent(Config(llm_enabled=False))
        p = self.night_case()
        robot = unit(90, 'smallRobot', 8, 5, health=40, attackRange=1, targetTeam='challenger')
        p['robot']['roles'] = [robot]
        self.assertEqual(agent.decide(p)['roleCommandMap']['20']['action'], 'attack')
        p['roundNo'] += 1
        robot['health'] = 0
        self.assertEqual(agent.decide(p)['roleCommandMap']['1']['action'], 'collect')
        p['roundNo'] += 1
        robot['health'] = 40
        self.assertEqual(agent.decide(p)['roleCommandMap']['20']['action'], 'attack')

    def test_full_worker_does_not_sell_at_night(self):
        p = self.night_case()
        p['teamOur']['roles'][0]['backpack'] = ['copper'] * 100
        p['mapInfo']['zones'].append({'neutralType': 'vendor', 'pos': {'x': 4, 'y': 5}})
        self.assertNotIn('1', Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap'])

    def test_no_weapon_worker_retreats_when_base_is_threatened(self):
        p = self.night_case()
        p['teamOur']['roles'] = [r for r in p['teamOur']['roles'] if r['id'] != 20]
        p['robot']['roles'] = [unit(90, 'smallRobot', 8, 5, attackRange=1, targetTeam='challenger')]
        commands = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertNotEqual(commands.get('1', {}).get('action'), 'collect')

    def test_far_opponent_wave_does_not_hold_workers(self):
        p = self.night_case()
        p['mapInfo'].update(width=41, height=15)
        p['robot']['roles'] = [unit(90, 'smallRobot', 30, 10, attackRange=1, targetTeam='defender')]
        self.assertEqual(Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']['1']['action'], 'collect')

    def test_unarmed_worker_wont_cross_monster_range_to_mine(self):
        p = payload(90, [unit(1, 'worker', 1, 5)])
        p['mapInfo'].update(width=15, height=11)
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 12, 'y': 5}}]
        p['robot']['roles'] = [unit(90, 'smallRobot', 7, 5, attackRange=3, targetTeam='defender')]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        self.assertFalse(Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap'])


    def test_top_left_base_keeps_night_worker_off_right_side(self):
        p = payload(80, [unit(1, 'worker', 2, 5), unit(13, 'station', 3, 4)])
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 8, 'y': 5}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        commands = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertNotIn('1', commands)

        p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 1, 'y': 5}})
        commands = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'collect')
        self.assertEqual(commands['1']['targetPos'][0], {'x': 1, 'y': 5})

    def test_bottom_right_base_keeps_night_worker_off_left_side(self):
        p = payload(80, [unit(1, 'worker', 12, 10), unit(13, 'station', 10, 11)])
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 7, 'y': 10}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        commands = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertNotIn('1', commands)

        p['mapInfo']['zones'].append({'neutralType': 'copper', 'pos': {'x': 13, 'y': 10}})
        commands = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
        self.assertEqual(commands['1']['action'], 'collect')
        self.assertEqual(commands['1']['targetPos'][0], {'x': 13, 'y': 10})

    def test_top_left_worker_already_right_of_base_retreats_left(self):
        p = payload(80, [unit(1, 'worker', 7, 5), unit(13, 'station', 3, 4)])
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 9, 'y': 5}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        command = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']['1']
        self.assertEqual(command['action'], 'move')
        self.assertLess(command['targetPos'][0]['x'], 7)

    def test_bottom_right_worker_already_left_of_base_retreats_right(self):
        p = payload(80, [unit(1, 'worker', 7, 10), unit(13, 'station', 10, 11)])
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 5, 'y': 10}}]
        p['vendorShopList'] = [{'name': 'copper', 'price': 10}]
        command = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']['1']
        self.assertEqual(command['action'], 'move')
        self.assertGreater(command['targetPos'][0]['x'], 7)


class FrontOnlyTests(unittest.TestCase):
    def test_twelve_walls_keep_front_first_and_rear_open_in_each_corner(self):
        for x, y in ((3, 11), (10, 4), (3, 4), (10, 11)):
            cfg = Config()
            t = Turn(payload(roles=[unit(13, 'station', x, y)]), cfg)
            _, walls = layout(t, cfg)
            self.assertEqual(len(walls), 12)
            self.assertEqual({p[0] for p in walls[:6]}, {x + 3 if x < 7 else x - 2})
            self.assertEqual(len(set(walls)), 12)
            self.assertEqual(sorted(sum(p[1] == y0 for p in walls[6:])
                                    for y0 in {p[1] for p in walls[6:]}), [3, 3])
            rear_x = x - 2 if x < 7 else x + 3
            self.assertNotIn(rear_x, {p[0] for p in walls})

    def test_wall_preserves_short_access_until_gun_is_built(self):
        roles = [unit(1, 'worker', 1, 1)] + [unit(100+y, 'wall', 3, y)
                  for y in range(15) if y not in (2, 10)]
        p = payload(45, roles)
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit',
                                         weapon_cells=[[5, 1]], wall_cells=[[3, 2]])
        self.assertFalse(wall_keeps_access(t, nav, ledger, (3, 2)))
        p['teamOur']['roles'].append(unit(20, 'rocket', 5, 1))
        t, cfg, nav, ledger = setup_case(p, layout_mode='explicit',
                                         weapon_cells=[[5, 1]], wall_cells=[[3, 2]])
        self.assertTrue(wall_keeps_access(t, nav, ledger, (3, 2)))

    def test_nearby_isolated_site_cannot_override_connected_extension(self):
        p = payload(45, [unit(13, 'station', 3, 11),
                         unit(1, 'worker', 5, 8, backpack=['stone']),
                         unit(90, 'wall', 6, 11)])
        t, cfg, nav, ledger = setup_case(p)
        self.assertTrue(build(t, cfg, Memory(), nav, ledger, t.workers[0],
                              layout(t, cfg)[1], lambda _: 'wall'))
        target = next(iter(ledger.build_claims))
        self.assertEqual(abs(target[0]-6) + abs(target[1]-11), 1)

    def test_moving_builder_does_not_seed_a_second_wall(self):
        p = payload(45, [unit(13, 'station', 3, 11),
                         unit(1, 'worker', 0, 0, backpack=['stone']),
                         unit(2, 'worker', 0, 2, backpack=['stone'])])
        t, cfg, nav, ledger = setup_case(p)
        walls = layout(t, cfg)[1]
        self.assertTrue(build(t, cfg, Memory(), nav, ledger, t.workers[0], walls, lambda _: 'wall'))
        self.assertEqual(ledger.commands['1']['action'], 'move')
        self.assertFalse(build(t, cfg, Memory(), nav, ledger, t.workers[1], walls, lambda _: 'wall'))

    def test_second_builder_can_extend_same_turn_actual_build(self):
        p = payload(45, [unit(13, 'station', 3, 11),
                         unit(1, 'worker', 5, 8, backpack=['stone']),
                         unit(2, 'worker', 5, 9, backpack=['stone'])])
        t, cfg, nav, ledger = setup_case(p)
        walls = layout(t, cfg)[1]
        for hero in t.workers:
            self.assertTrue(build(t, cfg, Memory(), nav, ledger, hero, walls, lambda _: 'wall'))
        self.assertTrue(all(c['action'] == 'build' for c in ledger.commands.values()))
        a, b = ledger.build_claims
        self.assertEqual(abs(a[0]-b[0]) + abs(a[1]-b[1]), 1)


if __name__ == '__main__':
    unittest.main()
