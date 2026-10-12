"""Robot shopping must leave daylight for the uses, including opening staging."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import BestRobotSummoningTests, fortified_case, TOWERS, WALLS
from agent.brain import Agent, summon_best_robot
from agent.commands import command
from agent.intelligence import Memory
from agent.model import SUMMON_ORDERS
from agent.scouting import ScoutMemory, prepare_night_scout
from agent.spending import plan_day_spending
from agent.worker_jobs import resume_daytime_jobs, save_daytime_jobs


BOSS = 'BossRobotSummonOrder'
OPENING_TOWERS = [(31, 8), (32, 8), (32, 10)]


def opening_data(round_no=59, ready=False, at_shop=False, backpack=()):
    # The reported match completed its last rocket at 61 and started a
    # nine-step shopping trip at 67. Keep its geometry, without task payloads.
    roles = [unit(20013, 'station', 30, 10, health=1500),
             unit(20010, 'worker', 31, 7, health=500),
             unit(20012, 'worker', *((25, 19) if at_shop else (29, 11)),
                  health=500, backpack=list(backpack))]
    roles += [unit(20040 + i, 'rocket', *p, health=1000)
              for i, p in enumerate(OPENING_TOWERS[:3 if ready else 2])]
    data = payload(round_no, roles)
    data['mapInfo'].update(width=41, height=32,
        zones=[{'neutralType': 'weaponShop', 'pos': {'x': 25, 'y': 20}}])
    data['teamOur']['goldNum'] = 394 if ready else 419
    data['teamEnemy']['roles'] = [unit(10013, 'station', 9, 22, health=1500)]
    data['weaponShopList'] = [{'name': BOSS, 'price': 120}]
    return data


def opening_setup(data, **options):
    return setup_case(data, layout_mode='explicit', loadout=['rocket'] * 3,
                      weapon_cells=[list(p) for p in OPENING_TOWERS], wall_cells=[],
                      **options)


class RobotDaylightOrderTests(unittest.TestCase):
    def case(self, tick=65, gold=600, backpack=()):
        data = BestRobotSummoningTests().shop_data(round_no=260 + tick, gold=gold,
                                                   backpack=list(backpack))
        return data

    def test_purchase_batch_includes_one_buy_and_one_action_per_use(self):
        for tick, quantity in ((65, 4), (67, 2), (68, 1), (69, 0)):
            with self.subTest(tick=tick):
                turn, cfg, nav, ledger = fortified_case(self.case(tick))
                result = summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS)
                self.assertEqual(result, bool(quantity))
                self.assertEqual(ledger.gold, 600 - 120 * quantity)
                if quantity:
                    self.assertEqual(ledger.commands['1'], command('buy', name=BOSS, num=quantity))
                else:
                    self.assertFalse(ledger.commands)
                    self.assertEqual(ledger.spending_plan['robot_blocked'], 'daylight_summon_deadline')

    def test_route_steps_reduce_the_batch_and_can_prevent_starting(self):
        for tick, expected in ((64, 2), (66, 0)):
            with self.subTest(tick=tick):
                data = self.case(tick)
                data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 5}
                turn, cfg, nav, ledger = fortified_case(data)
                result = summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS,
                                           excluded={2})
                self.assertEqual(result, bool(expected))
                if expected:
                    self.assertEqual(ledger.commands['1']['action'], 'move')
                    self.assertEqual(ledger.work_jobs[1]['quantity'], expected)
                    note = ledger.notes[1]['conditions']
                    self.assertEqual(note['route_steps'], 3)
                    self.assertLessEqual(note['required_daylight_actions'], turn.day_left)
                    self.assertFalse(note['purchase_submitted'])
                else:
                    self.assertFalse(ledger.commands)
                    self.assertEqual(ledger.gold, 600)

    def test_night_neither_buys_nor_uses_any_order_tier(self):
        for day in (1, 3):
            for name in SUMMON_ORDERS:
                with self.subTest(day=day, name=name):
                    data = self.case()
                    data['roundNo'] = (day - 1) * 130 + 70
                    data['teamOur']['roles'][1]['backpack'] = [name]
                    turn, cfg, nav, ledger = fortified_case(data)
                    self.assertFalse(summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS))
                    self.assertFalse(ledger.commands)
                    self.assertEqual(ledger.gold, 600)

    def test_default_day_two_policy_keeps_owned_uses_but_stops_new_orders(self):
        for stock, expected in (([], False), ([BOSS], True)):
            with self.subTest(stock=stock):
                turn, cfg, nav, ledger = opening_setup(opening_data(130, ready=True, at_shop=True,
                                                                    backpack=stock))
                result = summon_best_robot(turn, cfg, Memory(), nav, ledger, OPENING_TOWERS, [])
                self.assertEqual(result, expected)
                self.assertEqual(ledger.gold, 394)
                if stock:
                    self.assertEqual(ledger.commands['20012']['action'], 'use')
                else:
                    self.assertFalse(ledger.commands)
                    self.assertEqual(ledger.spending_plan['robot_blocked'], 'purchase_days_finished')

    def test_existing_busy_stock_reserves_daylight_and_daily_slots(self):
        data = self.case(67)
        data['teamOur']['roles'][2]['backpack'] = [BOSS]
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory()
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS, excluded={2}))
        self.assertEqual(ledger.commands['1'], command('buy', name=BOSS, num=1))
        data['roundNo'] = 295
        data['teamOur']['roles'][2]['backpack'] = [BOSS] * 2
        turn, cfg, nav, ledger = fortified_case(data)
        mem.robot_summon_state = dict(day=3, count=7, positions=set(), pending=None, observed_round=260)
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS, excluded={2}))
        self.assertEqual(ledger.commands['1'], command('buy', name=BOSS, num=1))

    def test_batch_never_exceeds_ten_even_with_zero_price(self):
        data = self.case(35, gold=2400)
        for price in (120, 0):
            with self.subTest(price=price):
                data['weaponShopList'][-1]['price'] = price
                turn, cfg, nav, ledger = fortified_case(data)
                self.assertTrue(summon_best_robot(turn, cfg, Memory(), nav, ledger, TOWERS, WALLS))
                self.assertEqual(ledger.commands['1'], command('buy', name=BOSS, num=10))

    def test_delayed_purchase_job_is_released_without_spending(self):
        data = self.case(64)
        data['teamOur']['roles'][1]['pos'] = {'x': 0, 'y': 5}
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory()
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, TOWERS, WALLS, excluded={2}))
        save_daytime_jobs(turn, mem, ledger)
        data['roundNo'] = 328
        turn, cfg, nav, ledger = fortified_case(data)
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, WALLS, set())
        self.assertNotIn(1, mem.daytime_jobs)
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.gold, 600)

    def test_paid_order_preempts_an_ordinary_sale_job(self):
        data = self.case(67, gold=0, backpack=[BOSS, 'iron'])
        data['mapInfo']['zones'].append({'neutralType': 'vendor', 'pos': {'x': 5, 'y': 8}})
        data['vendorShopList'] = [{'name': 'iron', 'price': 8}]
        turn, cfg, nav, ledger = fortified_case(data)
        mem = Memory(daytime_jobs={1: dict(kind='sell', target=(5, 8))})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, TOWERS, WALLS, set())
        self.assertEqual(ledger.commands['1']['action'], 'use')
        self.assertEqual(ledger.commands['1']['name'], BOSS)
        self.assertNotIn(1, mem.daytime_jobs)

    def test_scout_does_not_take_a_worker_holding_paid_orders(self):
        turn, cfg, nav, ledger = opening_setup(opening_data(68, ready=True, at_shop=True, backpack=[BOSS]))
        self.assertIsNone(prepare_night_scout(turn, ScoutMemory(), nav, ledger, excluded={20010}))
        self.assertNotIn(20012, ledger.used)

    def test_opening_stages_one_worker_and_keeps_the_last_gun_builder(self):
        data = opening_data()
        turn, cfg, _, _ = opening_setup(data, llm_enabled=False)
        agent = Agent(cfg)
        response = agent.decide(data)['roleCommandMap']
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(response['20012']['action'], 'move')
        self.assertEqual(mem.daytime_jobs[20012]['kind'], 'robot_buy')
        self.assertEqual(mem.daytime_jobs[20010]['kind'], 'build')
        self.assertEqual(mem.daytime_jobs[20010]['target'], (32, 10))
        self.assertFalse(any(c['action'] == 'buy' for c in response.values()))
        self.assertEqual(turn.gold, 419)

    def test_staging_keeps_the_existing_gun_builder_and_pioneer_task(self):
        data = opening_data(50)
        data['teamOur']['roles'].append(unit(20011, 'pioneer', 28, 17))
        data['phaseTask'] = 'active task'
        turn, cfg, nav, ledger = opening_setup(data)
        mem = Memory(daytime_jobs={20012: dict(kind='build', target=(32, 10), name='rocket')})
        actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, OPENING_TOWERS)
        self.assertEqual(actors, {20012})
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, OPENING_TOWERS, [],
                                         excluded=actors, reserve=reserve, stage_only=True))
        self.assertNotIn(20011, ledger.used)
        self.assertNotIn(20012, ledger.used)
        self.assertEqual(ledger.work_jobs[20010]['kind'], 'robot_buy')
        self.assertGreaterEqual(ledger.gold, cfg.weapon_cost)

    def test_last_gun_does_not_reassign_an_existing_robot_buyer(self):
        turn, cfg, nav, ledger = opening_setup(opening_data(50))
        mem = Memory(daytime_jobs={20010: dict(kind='robot_buy', target=(25, 20),
                                             name=BOSS, quantity=3)})
        actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, OPENING_TOWERS)
        self.assertEqual(actors, {20012})
        self.assertTrue(summon_best_robot(turn, cfg, mem, nav, ledger, OPENING_TOWERS, [],
                                         excluded=actors, reserve=reserve, stage_only=True))
        self.assertEqual(ledger.work_jobs[20010]['kind'], 'robot_buy')

    def test_only_remaining_worker_can_resume_necessary_construction(self):
        data = opening_data(50)
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 20012]
        turn, cfg, nav, ledger = opening_setup(data)
        mem = Memory(daytime_jobs={20010: dict(kind='robot_buy', target=(25, 20),
                                             name=BOSS, quantity=3)})
        actors, reserve = plan_day_spending(turn, cfg, mem, nav, ledger, OPENING_TOWERS)
        self.assertEqual(actors, {20010})
        self.assertFalse(summon_best_robot(turn, cfg, mem, nav, ledger, OPENING_TOWERS, [],
                                          excluded=actors, reserve=reserve, stage_only=True))

    def test_staged_buyer_at_shop_waits_for_observed_gun_completion(self):
        data = opening_data(60, at_shop=True)
        turn, cfg, nav, ledger = opening_setup(data)
        mem = Memory(daytime_jobs={20012: dict(kind='robot_buy', target=(25, 20), name=BOSS, quantity=2)})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, OPENING_TOWERS, [], set())
        self.assertNotIn('20012', ledger.commands)
        self.assertIn(20012, ledger.used)
        self.assertEqual(ledger.work_jobs[20012]['kind'], 'robot_buy')
        self.assertEqual(turn.gold, 419)
        self.assertEqual(ledger.gold, 179)

    def test_single_worker_is_kept_for_construction_instead_of_staging(self):
        data = opening_data()
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['id'] != 20012]
        _, cfg, _, _ = opening_setup(data, llm_enabled=False)
        agent = Agent(cfg)
        agent.decide(data)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.daytime_jobs[20010]['kind'], 'build')

    def test_staging_releases_the_buyer_if_guns_are_still_missing_at_recall(self):
        turn, cfg, nav, ledger = opening_setup(opening_data(67, at_shop=True))
        mem = Memory(daytime_jobs={20012: dict(kind='robot_buy', target=(25, 20), name=BOSS, quantity=2)})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, OPENING_TOWERS, [], set())
        self.assertNotIn(20012, mem.daytime_jobs)
        self.assertNotIn(20012, ledger.used)
        self.assertEqual(ledger.gold, 419)

    def test_staged_buyer_buys_and_uses_the_whole_batch_before_first_night(self):
        data = opening_data(67, ready=True, at_shop=True)
        _, cfg, _, _ = opening_setup(data, llm_enabled=False)
        agent = Agent(cfg)
        response = agent.decide(data)
        self.assertEqual(response['roleCommandMap']['20012'], command('buy', name=BOSS, num=2))
        following = copy.deepcopy(data)
        following['teamOur']['goldNum'] = 154
        buyer = next(r for r in following['teamOur']['roles'] if r['id'] == 20012)
        for tick, stock in ((68, [BOSS, BOSS]), (69, [BOSS])):
            following['roundNo'] = tick
            following['lastRoundRoleActionResults'] = {'20012': True}
            buyer['backpack'] = stock
            response = agent.decide(following)
            self.assertEqual(response['roleCommandMap']['20012']['action'], 'use')
            self.assertEqual(response['roleCommandMap']['20012']['name'], BOSS)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.robot_summon_state['count'], 2)
        self.assertEqual(len(mem.robot_summon_state['positions']), 2)
        following['roundNo'] = 70
        buyer['backpack'] = []
        response = agent.decide(following)
        self.assertFalse(any(c.get('name') in SUMMON_ORDERS for c in response['roleCommandMap'].values()))
