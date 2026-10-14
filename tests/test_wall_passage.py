"""Finite requests for the reported inner-entrance stall and supply handoff."""
import copy
import unittest

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.daytime import finish_daytime_work
from agent.intelligence import Memory
from agent.model import Turn, pos, distance
from agent.navigation import layout
from agent.traffic import coordinate_traffic, park_idle_worker, transit_cells
from agent.wall_supply import request_wall_passage, _home, prepare


class WallPassageTests(unittest.TestCase):
    def case(self, tick=49, watcher=(21, 16), blocker=(8, 20), mirror=False, modern=False):
        data = payload(5*130 + tick, [
            unit(13, 'station', 9, 22, health=1500),
            unit(1, 'worker', *watcher, health=500,
                 backpack=['WallFixer']*4 + ['WallUpgradeVoucher1']*15 + ['WallUpgradeVoucher2']*11),
            unit(2, 'worker', *blocker, health=500),
            unit(11, 'pioneer', 8, 22, health=500),
            unit(20, 'rocket', 8, 23, level=3),
            unit(21, 'rocket', 9, 23, level=3),
            unit(22, 'gatling', 9 if modern else 8, 20 if modern else 21, level=1)])
        data['mapInfo'].update(width=41, height=32)
        data['teamOur']['goldNum'] = 0
        t, cfg, _, _ = setup_case(data)
        sites = layout(t, cfg)[1]
        data['teamOur']['roles'] += [unit(100+i, 'wall', *s,
            level=1 if i < 15 else 2 if i < 18 else 3, health=1000) for i, s in enumerate(sites)]
        data['mapInfo']['zones'] = [dict(neutralType=k, pos=dict(x=x, y=y))
            for k, x, y in [('vendor', 20, 16), ('weaponShop', 25, 20)]]
        data['weaponShopList'] = [dict(name=n, price=p) for n, p in [
            ('WallFixer', 15), ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30)]]
        if mirror:
            for role in data['teamOur']['roles']:
                role['pos']['x'] = 40-role['pos']['x'] - (1 if role['roleType']=='station' else 0)
                role['pos']['y'] = 31-role['pos']['y'] + (1 if role['roleType']=='station' else 0)
            for zone in data['mapInfo']['zones']:
                zone['pos']['x'], zone['pos']['y'] = 40-zone['pos']['x'], 31-zone['pos']['y']
        return data

    def setup(self, data, mem=None):
        t, cfg, nav, ledger = setup_case(data, llm_enabled=False)
        mem = mem or Memory(day=6, wall_watch_id=1, gatling_operator_id=2,
                            gunner_id=11, gunner_post=t.units[11].pos)
        if not mem.wall_supply_trip:
            mem.wall_supply_trip = dict(key=(6, 1), phase='deliver', shop=next(
                p for p, k in t.zones.items() if k == 'weaponShop'), pending=None)
        gunner = t.units[11].pos
        independent = (gunner[0], gunner[1] + (-2 if t.station.pos[0] < t.width/2 else 2))
        ledger.operator_posts = {2: independent, 11: gunner}
        ledger.daytime_gunner_post = t.units[11].pos
        return t, cfg, mem, nav, ledger

    def coordinate(self, data, mem=None, returning=()):
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        request_wall_passage(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        coordinate_traffic(t, cfg, mem, nav, ledger, set(returning))
        return t, cfg, mem, nav, ledger

    def advance_moves(self, data, ledger):
        occupied = set(ledger.turn.blocked)
        destinations = set()
        for uid, action in ledger.commands.items():
            if action['action'] != 'move':
                continue
            point = pos(action['targetPos'][0])
            actor = next(r for r in data['teamOur']['roles'] if r['id'] == int(uid))
            self.assertNotIn(point, occupied)
            self.assertNotIn(point, destinations)
            self.assertEqual(distance(pos(actor['pos']), point), 1)
            destinations.add(point)
            actor['pos'] = action['targetPos'][0]
        data['roundNo'] += 1

    def test_incomplete_guns_do_not_allow_idle_inner_entrance_parking(self):
        for mirror in (False, True):
            for modern in (False, True):
                with self.subTest(mirror=mirror, modern=modern):
                    t, cfg, mem, nav, ledger = self.setup(self.case(mirror=mirror, modern=modern))
                    self.assertIn(t.units[2].pos, transit_cells(t, nav, ledger))
                    self.assertTrue(park_idle_worker(t, nav, ledger, t.units[2]))
                    self.assertEqual(ledger.commands['2']['action'], 'move')
                    self.assertNotEqual(pos(ledger.commands['2']['targetPos'][0]), t.units[2].pos)

    def test_final_daytime_pass_parks_idle_worker_before_full_firepower(self):
        t, cfg, mem, nav, ledger = self.setup(self.case())
        finish_daytime_work(t, cfg, mem, nav, ledger, set())
        self.assertEqual(ledger.commands['2']['action'], 'move')
        self.assertEqual(ledger.notes[2]['reason'], 'leave_idle_transit_cell')

    def test_wall_return_exists_without_any_ordinary_work_job(self):
        t, cfg, mem, nav, ledger = self.setup(self.case())
        self.assertFalse(mem.daytime_jobs)
        self.assertIsNone(_home(t, mem, nav, ledger, t.units[1], ledger.wall_cells)[1])
        request_wall_passage(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        self.assertEqual(ledger.traffic_requests[1]['signature'], ('wall_supply_home', 1))
        self.assertTrue(ledger.traffic_requests[1]['advance'])
        self.assertTrue(ledger.traffic_requests[1]['goals'].isdisjoint(transit_cells(t, nav, ledger)))

    def test_requester_advances_while_blocker_departs_through_the_gate(self):
        for mirror in (False, True):
            data = self.case(mirror=mirror)
            t, cfg, mem, nav, ledger = self.coordinate(data)
            self.assertEqual(ledger.notes[1]['reason'], 'approach_wall_passage')
            self.assertEqual(ledger.notes[2]['reason'], 'yield_blocked_route')
            self.assertIn(1, mem.traffic_leases)
            self.assertTrue(mem.traffic_leases[1]['aside'])
            self.advance_moves(data, ledger)
            t, cfg, mem, nav, ledger = self.coordinate(data, mem)
            self.assertEqual(ledger.commands['2']['action'], 'move')
            self.advance_moves(data, ledger)
            t, cfg, mem, nav, ledger = self.coordinate(data, mem)
            self.assertIsNotNone(_home(t, mem, nav, ledger, t.units[1], ledger.wall_cells)[1])
            self.assertIn(1, mem.traffic_leases)
            self.assertIn(2, ledger.traffic_forbidden)
            prepare(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
            self.assertIn('1', ledger.commands)

    def test_blocked_supply_phase_and_shop_survive_coordination(self):
        data = self.case()
        t, cfg, mem, nav, ledger = self.coordinate(data)
        before = copy.deepcopy(mem.wall_supply_trip)
        self.assertEqual(before['phase'], 'deliver')
        self.advance_moves(data, ledger)
        t, cfg, mem, nav, ledger = self.coordinate(data, mem)
        self.assertEqual(mem.wall_supply_trip, before)
        self.assertFalse(mem.daytime_jobs)

    def test_adjacent_paid_wall_use_is_not_blocked_by_inside_return(self):
        data = self.case(watcher=(13, 18))
        t, cfg, mem, nav, ledger = self.coordinate(data)
        self.assertNotIn(1, ledger.used)
        self.assertEqual(ledger.commands['2']['action'], 'move')
        prepare(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        self.assertEqual(ledger.commands['1'], command('use', (12, 19), name='WallUpgradeVoucher1'))
        self.assertEqual(ledger.notes[1]['conditions']['reason_detail'], 'local_delivery_while_waiting_for_passage')

    def test_adjacent_use_also_works_with_a_permanently_blocked_return(self):
        data = self.case(watcher=(13, 18))
        # A building cannot be treated as a teammate that can yield.
        data['teamOur']['roles'][2]['roleType'] = 'wall'
        t, cfg, mem, nav, ledger = self.setup(data)
        request_wall_passage(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        coordinate_traffic(t, cfg, mem, nav, ledger, set())
        prepare(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        self.assertEqual(ledger.commands['1']['action'], 'use')
        self.assertFalse(mem.traffic_leases)
        self.assertNotIn('2', ledger.commands)

    def test_unreceived_or_wrong_tier_coupons_do_not_create_local_work(self):
        data = self.case(watcher=(13, 18))
        data['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher2']
        t, cfg, mem, nav, ledger = self.coordinate(data)
        self.assertFalse(ledger.traffic_requests[1]['local_work'])
        self.assertNotEqual(ledger.commands.get('1', {}).get('action'), 'use')

    def test_early_mining_window_and_night_do_not_publish_supply_return(self):
        for tick in (0, 34, 70, 120):
            t, cfg, mem, nav, ledger = self.setup(self.case(tick=tick))
            request_wall_passage(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
            self.assertFalse(ledger.traffic_requests)
            if not t.is_day:
                self.assertFalse(park_idle_worker(t, nav, ledger, t.units[2]))

    def test_genuine_operator_recall_is_not_idle_parking(self):
        t, cfg, mem, nav, ledger = self.setup(self.case(tick=67))
        ledger.plans[2] = {'reason': 'defence_recall'}
        self.assertFalse(park_idle_worker(t, nav, ledger, t.units[2]))
        finish_daytime_work(t, cfg, mem, nav, ledger, {2})
        self.assertNotIn('2', ledger.commands)

    def test_recalled_yielder_waits_until_the_existing_passage_is_used(self):
        data = self.case(tick=64, watcher=(7, 18))
        t, cfg, mem, nav, ledger = self.coordinate(data)
        self.advance_moves(data, ledger)
        t, cfg, mem, nav, ledger = self.coordinate(data, mem)
        self.advance_moves(data, ledger)
        data['roundNo'] = 5*130 + 67
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        # Retain the actual gun post while the yielder is waiting outside.
        ledger.operator_posts[2] = (8, 20)
        request_wall_passage(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        coordinate_traffic(t, cfg, mem, nav, ledger, {2})
        self.assertIn(2, ledger.traffic_holds)
        self.assertEqual(ledger.notes[2]['reason'], 'keep_wall_passage_clear')
        self.assertNotIn('2', ledger.commands)

    def test_real_occupied_cells_and_other_reservations_remain_blocked(self):
        t, cfg, mem, nav, ledger = self.setup(self.case())
        request_wall_passage(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        ledger.reserved.add((7, 21))
        coordinate_traffic(t, cfg, mem, nav, ledger, set())
        self.assertFalse(ledger.commands)
        self.assertFalse(mem.traffic_leases)
        self.assertFalse(ledger.add(2, command('move', t.units[11].pos)))

    def test_priority_emergency_action_is_not_replaced_by_yield(self):
        t, cfg, mem, nav, ledger = self.setup(self.case())
        ledger.used.add(2)
        request_wall_passage(t, cfg, mem, nav, ledger, t.units[1], ledger.wall_cells)
        coordinate_traffic(t, cfg, mem, nav, ledger, set())
        self.assertNotIn('2', ledger.commands)
        self.assertFalse(mem.traffic_leases)

    def test_passage_is_released_after_requester_reaches_safe_inside_goal(self):
        data = self.case()
        t, cfg, mem, nav, ledger = self.coordinate(data)
        goal = sorted(ledger.traffic_requests[1]['goals'])[0]
        data['teamOur']['roles'][1]['pos'] = dict(zip(('x','y'), goal))
        data['teamOur']['roles'][2]['pos'] = dict(x=6,y=21)
        data['roundNo'] += 1
        t, cfg, mem, nav, ledger = self.coordinate(data, mem)
        self.assertFalse(ledger.traffic_requests)
        self.assertNotIn(1, mem.traffic_leases)

    def test_agent_logged_stall_gets_legal_actions_with_unfinished_gatling(self):
        data = self.case()
        agent = Agent(Config(llm_enabled=False))
        t = Turn(data, agent.cfg)
        _, _, mem, _, _ = self.setup(data)
        agent.sessions[(*t.key, t.station.pos)] = mem
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['2']['action'], 'move')
        self.assertIn(response['roleCommandMap']['1']['action'], ('move', 'use'))
        self.assertEqual(agent.decide(data), response)
        self.assertEqual((mem.wall_watch_id, mem.gatling_operator_id), (1, 2))

    def test_idle_worker_does_not_walk_back_into_entrance_after_parking(self):
        from agent.economy import worker
        data = self.case()
        t, cfg, mem, nav, ledger = self.setup(data)
        worker(t, cfg, mem, nav, ledger, t.units[2], list(ledger.tower_cells),
               list(ledger.wall_cells), False, deadline=35)
        self.advance_moves(data, ledger)
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        worker(t, cfg, mem, nav, ledger, t.units[2], list(ledger.tower_cells),
               list(ledger.wall_cells), False, deadline=35)
        self.advance_moves(data, ledger)
        t, cfg, mem, nav, ledger = self.setup(data, mem)
        parked = t.units[2].pos
        self.assertNotIn(parked, transit_cells(t, nav, ledger))
        worker(t, cfg, mem, nav, ledger, t.units[2], list(ledger.tower_cells),
               list(ledger.wall_cells), False, deadline=35)
        self.assertNotIn('2', ledger.commands)
        self.assertEqual(ledger.notes[2]['reason'], 'off_route_standby')
