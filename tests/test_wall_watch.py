"""Wall watch integration: real commands, inventory, geometry and gunner priority."""
import unittest
from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Memory
from agent.wall_watch import select_watch, prepare_watch, repair_watch, geometry
from agent.navigation import layout


class WallWatchTests(unittest.TestCase):
    def case(self, day=4, tick=70, mirror=False, packs=3, damaged=True):
        p = payload((day - 1) * 130 + tick, [unit(13, 'station', 5, 11, health=1500),
            unit(1, 'worker', 4, 11, health=220),
            unit(2, 'worker', 7, 10, health=220, backpack=['WallFixer'] * packs)])
        t, cfg, _, _ = setup_case(p)
        towers, walls = layout(t, cfg)
        p['teamOur']['roles'] += [unit(20+i, 'rocket', *s) for i, s in enumerate(towers)]
        p['teamOur']['roles'] += [unit(100+i, 'wall', *s, health=400 if damaged else 1000)
                                  for i, s in enumerate(walls)]
        p['robot']['roles'] = [unit(90, 'smallRobot', 10, 10, health=500, attackRange=1, targetTeam='challenger')]
        p['mapInfo']['zones'] = [{'neutralType': 'copper', 'pos': {'x': 11, 'y': 2}},
                                 {'neutralType': 'weaponShop', 'pos': {'x': 7, 'y': 11}}]
        p['weaponShopList'] = [{'name': 'WallFixer', 'price': 10}]
        if mirror:
            for role in p['teamOur']['roles'] + p['robot']['roles']:
                role['pos']['x'] = 14-role['pos']['x'] - (1 if role['roleType'] == 'station' else 0)
            for z in p['mapInfo']['zones']:
                z['pos']['x'] = 14-z['pos']['x']
        return p

    def test_fourth_night_repairs_front_while_gunner_fires_in_both_directions(self):
        for mirror in (False, True):
            p = self.case(mirror=mirror)
            result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
            self.assertTrue(any(c['action'] == 'attack' and c['controllerId'] == '1' for c in result.values()))
            self.assertEqual(result['2']['name'], 'WallFixer')
            self.assertEqual(result['2']['targetPos'][0]['x'], 6 if mirror else 8)

    def test_no_watch_before_fourth_day(self):
        p = self.case(day=3)
        t, _, _, _ = setup_case(p)
        mem = Memory()
        self.assertIsNone(select_watch(t, mem, []))

    def test_no_pack_or_healthy_walls_release_miner(self):
        for packs, damaged in ((0, True), (3, False)):
            p = self.case(packs=packs, damaged=damaged)
            p['teamOur']['roles'][2]['pos'] = {'x': 10, 'y': 2}
            result = Agent(Config(llm_enabled=False)).decide(p)['roleCommandMap']
            self.assertEqual(result['2']['action'], 'collect')

    def test_daytime_buys_batch_even_with_healthy_walls(self):
        p = self.case(tick=40, packs=0, damaged=False)
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(gunner_id=1, gunner_post=(4, 11), wall_watch_id=2)
        locked, _ = prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1])
        self.assertTrue(locked)
        self.assertEqual(ledger.commands['2'], {'action': 'buy', 'name': 'WallFixer', 'num': 3})

    def test_early_day_reserves_gold_without_locking_worker(self):
        p = self.case(tick=5, packs=0)
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(gunner_post=(4, 11))
        self.assertEqual(prepare_watch(t, cfg, mem, nav, ledger, t.workers[1], layout(t, cfg)[1]), (False, 30))

    def test_no_money_no_pack_does_not_lock_worker(self):
        p = self.case(tick=50, packs=0)
        p['teamOur']['goldNum'] = 0
        t, cfg, nav, ledger = setup_case(p)
        self.assertEqual(prepare_watch(t, cfg, Memory(), nav, ledger, t.workers[1], layout(t, cfg)[1]), (False, 0))

    def test_inside_routes_never_enter_front_or_flank_boundary(self):
        p = self.case()
        p['teamOur']['roles'][2]['pos'] = {'x': 6, 'y': 10}
        # Only far end of front remains damaged.
        for r in p['teamOur']['roles']:
            if r['roleType'] == 'wall':
                r['health'] = 300 if r['pos'] == {'x': 8, 'y': 8} else 1000
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(gunner_post=(4, 11))
        sites = layout(t, cfg)[1]
        self.assertTrue(repair_watch(t, mem, nav, ledger, t.workers[1], sites))
        cmd = ledger.commands['2']
        self.assertEqual(cmd['action'], 'move')
        point = cmd['targetPos'][0]
        self.assertIn((point['x'], point['y']), geometry(t, sites)[0])

    def test_reserved_gunner_corridor_is_not_entered(self):
        p = self.case()
        t, cfg, nav, ledger = setup_case(p)
        sites = layout(t, cfg)[1]
        ledger.reserved.update(geometry(t, sites)[0] - {t.workers[1].pos})
        # Adjacent front repair needs no movement and therefore cannot block a new tile.
        self.assertTrue(repair_watch(t, Memory(gunner_post=(4, 11)), nav, ledger, t.workers[1], sites))
        self.assertEqual(ledger.commands['2']['action'], 'use')

    def test_dead_gunner_takes_priority_over_repair(self):
        p = self.case()
        agent = Agent(Config(llm_enabled=False))
        agent.decide(p)
        p['teamOur']['roles'][1]['health'] = 0
        p['roundNo'] += 1
        result = agent.decide(p)['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.gunner_id, 2)
        self.assertIsNone(mem.wall_watch_id)
        self.assertEqual(result['2']['action'], 'move')

    def test_higher_level_observed_damage_is_repaired(self):
        p = self.case(damaged=False)
        t, cfg, nav, ledger = setup_case(p)
        wall = next(r for r in p['teamOur']['roles'] if r['roleType'] == 'wall')
        wall.update(level=3, health=2400)
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        select_watch(t, mem, [])
        wall['health'] = 1800
        t, cfg, nav, ledger = setup_case(p)
        select_watch(t, mem, [])
        self.assertTrue(repair_watch(t, mem, nav, ledger, t.workers[1], layout(t, cfg)[1]))

    def test_mining_worker_returns_when_front_takes_damage(self):
        p = self.case(damaged=False)
        p['teamOur']['roles'][2]['pos'] = {'x': 10, 'y': 2}
        agent = Agent(Config(llm_enabled=False))
        self.assertEqual(agent.decide(p)['roleCommandMap']['2']['action'], 'collect')
        front = next(r for r in p['teamOur']['roles'] if r['roleType'] == 'wall' and r['pos']['x'] == 8)
        front['health'] = 500
        repaired = False
        for _ in range(25):
            p['roundNo'] += 1
            command = agent.decide(p)['roleCommandMap'].get('2', {})
            if command.get('action') == 'move':
                p['teamOur']['roles'][2]['pos'] = command['targetPos'][0]
            elif command.get('name') == 'WallFixer':
                self.assertEqual(command['targetPos'][0], front['pos'])
                repaired = True
                break
        self.assertTrue(repaired)

    def test_partial_funds_buy_only_affordable_packages(self):
        p = self.case(tick=40, packs=0)
        p['teamOur']['goldNum'] = 15
        t, cfg, nav, ledger = setup_case(p)
        prepare_watch(t, cfg, Memory(), nav, ledger, t.workers[1], layout(t, cfg)[1])
        self.assertEqual(ledger.commands['2']['num'], 1)
        self.assertEqual(ledger.gold, 5)

    def test_carried_packs_return_before_dusk_without_shopping(self):
        p = self.case(tick=65)
        p['teamOur']['roles'][2]['pos'] = {'x': 6, 'y': 6}
        t, cfg, nav, ledger = setup_case(p)
        locked, reserved = prepare_watch(t, cfg, Memory(gunner_post=(4, 11)), nav, ledger,
                                         t.workers[1], layout(t, cfg)[1])
        self.assertTrue(locked)
        self.assertEqual(reserved, 0)
        self.assertEqual(ledger.commands['2']['action'], 'move')

    def test_day_mining_preserves_pack_slots_but_night_does_not(self):
        from agent.mining import reserve_watch_space
        p = self.case(tick=5, packs=0)
        p['teamOur']['roles'][2].update(backPackCapability=5, backpack=['copper'] * 2)
        t, _, _, _ = setup_case(p)
        mem = Memory(wall_watch_id=2)
        self.assertEqual(reserve_watch_space(t, mem, t.workers[1]).space, 0)
        p['roundNo'] += 70
        t, _, _, _ = setup_case(p)
        self.assertEqual(reserve_watch_space(t, mem, t.workers[1]).space, 3)

    def test_repair_worker_yields_to_gunner_before_any_repair(self):
        from agent.combat import clear_gunner_route
        p = self.case()
        # Put the gunner in the rear approach and watcher on its exact post.
        p['teamOur']['roles'][1]['pos'] = {'x': 2, 'y': 11}
        p['teamOur']['roles'][2]['pos'] = {'x': 4, 'y': 11}
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(gunner_id=1, gunner_post=(4, 11), wall_watch_id=2)
        corridor = clear_gunner_route(t, nav, ledger, mem, [(t.workers[0], t.weapons[0])])
        self.assertIn(2, ledger.used)
        self.assertEqual(ledger.commands['2']['action'], 'move')
        destination = ledger.commands['2']['targetPos'][0]
        self.assertNotIn((destination['x'], destination['y']), corridor)
