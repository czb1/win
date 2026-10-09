"""Night ownership and daytime congestion: bounded state transitions only."""
import unittest

from test_agent import payload, setup_case, unit
from test_mixed_battery import mixed_case
from agent.brain import Agent
from agent.combat import fixed_gatling_crew
from agent.config import Config
from agent.daytime import park_idle_pioneer
from agent.economy_plan import via
from agent.intelligence import Memory
from agent.model import Turn, pos
from agent.navigation import layout
from agent.wall_watch import prepare_watch


def gate_case(round_no=393):
    """The reported narrow aisle, without team identity or uploaded log data."""
    data = payload(round_no, [unit(13, 'station', 9, 22, health=1500),
        unit(1, 'worker', 11, 22, health=500,
             backpack=['WallUpgradeVoucher2'] * 7 + ['stone'] * 3),
        unit(2, 'worker', 7, 18, health=500, backpack=['copper']),
        unit(11, 'pioneer', 7, 21, health=500),
        unit(20, 'rocket', 8, 23, health=2000, level=3),
        unit(21, 'rocket', 9, 23, health=2000, level=3),
        unit(22, 'gatling', 8, 21, health=2000, level=3)])
    walls = [(12, y, 2) for y in (22, 21, 23, 20, 24, 19)]
    walls += [(11, 19, 2)] + [(x, 19, 1) for x in (10, 9, 8, 7)]
    walls += [(7, 20, 1)] + [(x, 24, 1) for x in (11, 10, 9)]
    data['teamOur']['roles'] += [unit(100+i, 'wall', x, y, health=1000, level=level)
                               for i, (x, y, level) in enumerate(walls)]
    data['teamOur']['goldNum'] = 334
    data['mapInfo'].update(width=41, height=32, zones=[
        dict(neutralType=kind, pos=dict(x=x, y=y)) for kind, x, y in
        [('weaponShop', 25, 20), ('vendor', 20, 16), ('copper', 1, 15),
         ('stone', 6, 16), ('iron', 13, 20), ('copper', 15, 18)]])
    data['weaponShopList'] = [dict(name=name, price=price) for name, price in
                             [('WallFixer', 15), ('WallUpgradeVoucher1', 20),
                              ('WallUpgradeVoucher2', 30)]]
    data['vendorShopList'] = [dict(name='copper', price=8)]
    return data


def seeded_agent(data, **kwargs):
    agent = Agent(Config(llm_enabled=False))
    turn = Turn(data, agent.cfg)
    mem = Memory(day=turn.day, last_round=turn.round-1,
                 gatling_operator_id=2, wall_watch_id=1, **kwargs)
    agent.sessions[(*turn.key, turn.station.pos)] = mem
    return agent, mem


class FixedWorkerRolesTests(unittest.TestCase):
    def test_living_gunner_keeps_job_after_positions_gold_and_day_change(self):
        data = mixed_case(390)
        data['robot']['roles'] = []
        agent = Agent(Config(llm_enabled=False))
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (1, 2))
        for round_no, gold in ((391, 0), (460, 999), (520, 10)):
            data['roundNo'] = round_no
            data['teamOur']['goldNum'] = gold
            data['teamOur']['roles'][1]['pos'] = dict(x=11, y=2)
            data['teamOur']['roles'][2]['pos'] = dict(x=4, y=9)
            agent.decide(data)
            self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (1, 2))

    def test_initial_assignment_keeps_pack_carrier_for_repairs(self):
        data = mixed_case(390)
        data['teamOur']['roles'][1]['backpack'] = ['WallFixer'] * 3
        turn, cfg, nav, ledger = setup_case(data)
        mem = Memory()
        fixed_gatling_crew(turn, mem, nav, ledger)
        self.assertEqual(mem.gatling_operator_id, 2)

    def test_destroyed_gatling_does_not_swap_owners_and_rebuilt_gun_reuses_owner(self):
        data = gate_case()
        agent, mem = seeded_agent(data)
        for round_no, health in ((393, 0), (394, 2000)):
            data['roundNo'] = round_no
            next(r for r in data['teamOur']['roles'] if r['id'] == 22)['health'] = health
            agent.decide(data)
            self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (2, 1))

    def test_gunner_death_promotes_survivor_and_revived_owner_does_not_steal_job(self):
        data = gate_case(470)
        agent, mem = seeded_agent(data)
        worker = next(r for r in data['teamOur']['roles'] if r['id'] == 2)
        worker['health'] = 0
        agent.decide(data)
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (1, None))
        data['roundNo'] = 520
        worker['health'] = 500
        agent.decide(data)
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (1, 2))

    def test_watcher_death_does_not_reassign_living_gunner(self):
        data = gate_case(470)
        agent, mem = seeded_agent(data)
        next(r for r in data['teamOur']['roles'] if r['id'] == 1)['health'] = 0
        agent.decide(data)
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (2, None))

    def test_nonshared_battery_also_preserves_fixed_worker(self):
        data = gate_case()
        next(r for r in data['teamOur']['roles'] if r['id'] == 20)['pos'] = dict(x=4, y=26)
        agent, mem = seeded_agent(data)
        agent.decide(data)
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (2, 1))

    def test_gate_blockage_does_not_cancel_sale_or_exchange_jobs(self):
        data = gate_case()
        agent, mem = seeded_agent(data, sale_workers={2}, sale_targets={2: (20, 16)},
                                  daytime_jobs={2: dict(kind='sell', target=(20, 16))})
        result = agent.decide(data)['roleCommandMap']
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (2, 1))
        self.assertEqual(mem.daytime_jobs[2], dict(kind='sell', target=(20, 16)))
        self.assertEqual(result['2']['action'], 'move')
        self.assertEqual(result['11']['action'], 'move')
        self.assertNotEqual(pos(result['11']['targetPos'][0]), (7, 21))
        # Observe successful moves, then verify the inside worker can work
        # again; this checks scheduling only, with no simulated combat/economy.
        data['roundNo'] += 1
        for role in data['teamOur']['roles']:
            cmd = result.get(str(role['id']), {})
            if cmd.get('action') == 'move':
                role['pos'] = cmd['targetPos'][0]
        result = agent.decide(data)['roleCommandMap']
        self.assertIn('1', result)
        self.assertEqual(mem.daytime_jobs[2]['kind'], 'sell')
        self.assertEqual((mem.gatling_operator_id, mem.wall_watch_id), (2, 1))

    def test_fixed_gunner_can_collect_in_early_daylight(self):
        data = mixed_case(391)
        data['robot']['roles'] = []
        data['teamOur']['goldNum'] = 0
        data['teamOur']['roles'][2]['backpack'] = []
        agent, mem = seeded_agent(data)
        result = agent.decide(data)['roleCommandMap']
        self.assertEqual(result['2']['action'], 'collect')
        self.assertEqual(mem.gatling_operator_id, 2)
        self.assertNotIn(2, mem.return_targets)

    def test_recall_deadline_still_overrides_sale_for_fixed_gunner(self):
        data = gate_case(459)
        next(r for r in data['teamOur']['roles'] if r['id'] == 11)['pos'] = dict(x=6, y=23)
        agent, mem = seeded_agent(data, sale_workers={2},
                                  daytime_jobs={2: dict(kind='sell', target=(20, 16))})
        agent.decide(data)
        self.assertEqual(mem.gatling_operator_id, 2)
        self.assertEqual(mem.return_targets.get(2), 22)
        self.assertNotEqual(mem.daytime_jobs.get(2, {}).get('kind'), 'sell')


class DaytimeGateTests(unittest.TestCase):
    def test_temporary_blockage_unlocks_early_watcher_but_not_late_recall(self):
        for tick, expected in ((3, False), (65, True)):
            data = gate_case(390 + tick)
            turn, cfg, nav, ledger = setup_case(data)
            mem = Memory(wall_watch_id=2, gunner_post=(8, 22))
            locked, funds = prepare_watch(turn, cfg, mem, nav, ledger, turn.units[2],
                                           layout(turn, cfg)[1])
            self.assertEqual(locked, expected)
            self.assertEqual(funds, 0)
            self.assertFalse(ledger.commands)

    def test_real_wall_obstruction_is_not_treated_as_a_friendly_delay(self):
        data = gate_case()
        next(r for r in data['teamOur']['roles'] if r['id'] == 11)['pos'] = dict(x=6, y=23)
        data['teamOur']['roles'].append(unit(300, 'wall', 7, 21, health=1000))
        turn, cfg, nav, ledger = setup_case(data)
        locked, _ = prepare_watch(turn, cfg, Memory(wall_watch_id=2, gunner_post=(8, 22)), nav, ledger,
                                  turn.units[2], layout(turn, cfg)[1])
        self.assertTrue(locked)
        self.assertEqual(ledger.daytime_waits[2], 'no_home_route')

    def test_future_return_estimate_does_not_authorize_an_occupied_outward_leg(self):
        data = gate_case()
        turn, cfg, nav, ledger = setup_case(data)
        groups = [[(20, 16)], [(7, 21)]]
        before = turn.blocked.copy()
        self.assertIsNone(via(nav, turn.units[2], groups, final_exact=True))
        self.assertIsNotNone(via(nav, turn.units[2], groups, final_exact=True, future_return=True))
        self.assertIsNone(via(nav, turn.units[1], groups, final_exact=True, future_return=True))
        self.assertEqual(turn.blocked, before)

    def test_idle_pioneer_parks_outside_and_keeps_the_destination_in_both_orientations(self):
        for mirror in (False, True):
            data = gate_case()
            if mirror:
                for group in (data['teamOur']['roles'], data['mapInfo']['zones']):
                    for role in group:
                        role['pos']['x'] = (39 if role.get('roleType') == 'station' else 40) - role['pos']['x']
            turn, cfg, nav, ledger = setup_case(data)
            mem = Memory()
            self.assertTrue(park_idle_pioneer(turn, cfg, mem, nav, ledger, turn.pioneer))
            post = mem.pioneer_wait_post
            self.assertTrue(post[0] > 33 if mirror else post[0] < 7)
            self.assertNotIn(post, ledger.wall_cells | ledger.tower_cells)
            next(r for r in data['teamOur']['roles'] if r['id'] == 11)['pos'] = dict(x=post[0], y=post[1])
            turn, cfg, nav, ledger = setup_case(data)
            self.assertTrue(park_idle_pioneer(turn, cfg, mem, nav, ledger, turn.pioneer))
            self.assertEqual(mem.pioneer_wait_post, post)
            self.assertFalse(ledger.commands)

    def test_outside_wait_never_overrides_task_recall_night_or_an_action(self):
        for case in ('task', 'recall', 'night', 'used'):
            data = gate_case(470 if case == 'night' else 393)
            if case == 'task':
                data['phaseTask'] = 'ongoing task'
            turn, cfg, nav, ledger = setup_case(data)
            mem = Memory(return_targets={11: 20} if case == 'recall' else {})
            if case == 'used':
                ledger.used.add(11)
            self.assertFalse(park_idle_pioneer(turn, cfg, mem, nav, ledger, turn.pioneer))
            self.assertFalse(ledger.commands)


if __name__ == '__main__':
    unittest.main()
