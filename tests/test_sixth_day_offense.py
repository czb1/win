"""Fast purchase, maintenance, deployment and base-voucher boundaries."""
import copy
import unittest

from test_agent import unit
from test_daylight_spending import case, setup
from agent.brain import summon_best_robot, Agent
from agent.commands import command
from agent.economy import supplies, buy_supply, station_voucher_needed
from agent.intelligence import Memory
from agent.late_offense import maintenance_budget, purchase_day_allowed
from agent.model import pos, distance
from agent.robot_assault import RobotAssaultMemory, enemy_entries
from agent.wall_supply import shopping_list, requirements, PACK, V1, V2
from agent.wall_watch_state import NightRecord

BOSS = 'BossRobotSummonOrder'
BASE = 'StationUpgradeVoucher1'


class SixthDayOffenseTests(unittest.TestCase):
    def data(self, gold=1000, tick=40, packs=9):
        data, cfg = case(tick=tick, gold=gold, packs=packs)
        cfg.robot_purchase_last_day = 1
        data['teamOur']['roles'][2]['pos'] = dict(x=6, y=10)
        data['teamOur']['roles'][3]['backpack'] = [BASE]
        return data, cfg

    def buy(self, data, cfg, mem=None, reserve=0):
        turn, cfg, nav, ledger = setup(data, cfg)
        mem = mem or Memory(wall_watch_id=1)
        accepted = summon_best_robot(turn, cfg, mem, nav, ledger,
                                     ledger.tower_cells, ledger.wall_cells, reserve=reserve)
        return accepted, turn, cfg, nav, ledger, mem

    def test_default_purchase_window_only_adds_day_six(self):
        data, cfg = self.data()
        for day in range(1, 11):
            data['roundNo'] = (day - 1) * 130 + 40
            turn, cfg, _, _ = setup(data, cfg)
            self.assertEqual(purchase_day_allowed(turn, cfg), day in (1, 6))

    def test_agent_buys_then_summons_without_changing_wall_worker(self):
        data, cfg = self.data()
        agent = Agent(cfg)
        commands = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(commands['2'], command('buy', name=BOSS, num=5))
        self.assertEqual(next(iter(agent.sessions.values())).wall_watch_id, 1)
        data['roundNo'] += 1
        data['teamOur']['roles'][2]['backpack'] = [BOSS] * 5
        data['teamOur']['goldNum'] = 400
        data['lastRoundRoleActionResults'] = {'2': True}
        commands = agent.decide(copy.deepcopy(data))['roleCommandMap']
        self.assertEqual(commands['2']['action'], 'use')
        self.assertEqual(commands['2']['name'], BOSS)
        self.assertEqual(next(iter(agent.sessions.values())).wall_watch_id, 1)

    def test_maxed_base_needs_no_further_base_voucher(self):
        data, cfg = self.data()
        data['teamOur']['roles'][0].update(level=3, health=4500)
        data['teamOur']['roles'][3]['backpack'] = []
        accepted, turn, _, _, _, _ = self.buy(data, cfg)
        self.assertTrue(accepted)
        self.assertFalse(station_voucher_needed(turn, turn.pioneer, None))

    def test_opening_retry_uses_current_base_tier_after_old_voucher_was_consumed(self):
        from test_first_day_pioneer_shopping import FirstDayPioneerShoppingTests
        fixture = FirstDayPioneerShoppingTests()
        data = fixture.data(gold=1000)
        data['teamOur']['roles'][0].update(level=2, health=3000)
        data['weaponShopList'].append(dict(name='StationUpgradeVoucher2', price=150))
        mem = Memory(first_day_pioneer=dict(phase='shop', station_item=BASE,
                     pending_buy=(data['roundNo'] - 1, BASE, 1, 0)))
        _, _, ledger = fixture.prepare(data, mem)
        self.assertFalse(any(c['action'] == 'buy' and c['name'] == BASE
                             for c in ledger.commands.values()))

    def test_four_or_five_bosses_keep_maintenance_buffer(self):
        for gold, quantity in ((570, 4), (690, 5), (2000, 5)):
            data, cfg = self.data(gold=gold)
            accepted, _, _, _, ledger, _ = self.buy(data, cfg)
            self.assertTrue(accepted)
            self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=quantity))
            self.assertGreaterEqual(ledger.gold, 90)
            self.assertNotIn('1', ledger.commands)

    def test_mirrored_base_uses_same_batch_budget_and_front_priority(self):
        data, cfg = self.data(gold=690)
        width, height = data['mapInfo']['width'], data['mapInfo']['height']
        for team in ('teamOur', 'teamEnemy'):
            for role in data[team]['roles']:
                x, y = role['pos']['x'], role['pos']['y']
                base = role['roleType'] == 'station'
                role['pos'] = dict(x=width - 1 - x - int(base),
                                   y=height - 1 - y + int(base))
        for zone in data['mapInfo']['zones']:
            zone['pos'] = dict(x=width - 1 - zone['pos']['x'],
                               y=height - 1 - zone['pos']['y'])
        cfg.wall_cells = [[width - 1 - x, height - 1 - y] for x, y in cfg.wall_cells]
        cfg.weapon_cells = [[width - 1 - x, height - 1 - y] for x, y in cfg.weapon_cells]
        front = next(r for r in data['teamOur']['roles'] if r['roleType'] == 'wall'
                     and pos(r['pos']) == (11, 10))
        front.update(level=2, health=1500)
        accepted, _, _, _, ledger, _ = self.buy(data, cfg)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2']['num'], 4)
        self.assertEqual(ledger.spending_plan['late_offense']['maintenance'], 30)

    def test_daily_capacity_and_failed_buy_do_not_invent_a_batch(self):
        data, cfg = self.data()
        mem = Memory(wall_watch_id=1)
        mem.robot_summon_state = dict(day=6, count=8, positions=set(), pending=None,
                                     observed_round=-1, buyer=None)
        self.assertFalse(self.buy(data, cfg, mem)[0])
        mem.robot_summon_state = None
        self.assertTrue(self.buy(data, cfg, mem)[0])
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'2': False}
        # Retry is a fresh submission; the rejected one releases its five slots.
        self.assertTrue(self.buy(data, cfg, mem)[0])
        self.assertEqual(mem.robot_summon_state['purchased'], 5)

    def test_not_enough_for_four_does_not_send_small_batches(self):
        data, cfg = self.data(gold=569)
        accepted, _, _, _, ledger, _ = self.buy(data, cfg)
        self.assertFalse(accepted)
        self.assertFalse(ledger.purchases)
        self.assertEqual(ledger.spending_plan['robot_blocked'], 'late_offense_insufficient_batch_budget')

    def test_low_enemy_base_allows_three_and_uses_current_price(self):
        data, cfg = self.data(gold=480)
        data['teamEnemy']['roles'][0]['health'] = 800
        next(x for x in data['weaponShopList'] if x['name'] == BOSS)['price'] = 130
        accepted, _, _, _, ledger, _ = self.buy(data, cfg)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2']['num'], 3)
        self.assertEqual(ledger.gold, 90)

    def test_unpaid_front_upgrade_reduces_attack_batch(self):
        data, cfg = self.data(gold=690)
        next(r for r in data['teamOur']['roles'] if r['roleType'] == 'wall'
             and pos(r['pos']) == (8, 5)).update(level=2, health=1500)
        accepted, _, _, _, ledger, _ = self.buy(data, cfg)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2']['num'], 4)
        self.assertEqual(ledger.spending_plan['late_offense']['maintenance'], 30)

    def test_stock_trip_must_be_feasible_before_offense(self):
        data, cfg = self.data(packs=1)
        accepted, _, _, _, ledger, _ = self.buy(data, cfg)
        self.assertFalse(accepted)
        self.assertEqual(ledger.spending_plan['robot_blocked'], 'late_offense_stock_trip_unready')

    def test_reservations_and_same_turn_buys_are_not_charged_twice(self):
        data, cfg = self.data(packs=1)
        turn, cfg, nav, ledger = setup(data, cfg)
        mem = Memory(wall_watch_id=1)
        ledger.watch_gold_reserved = 120
        ledger.gold -= 120
        self.assertEqual(maintenance_budget(turn, mem, ledger, turn.workers[0]), (0, 90))
        ledger.watch_gold_reserved = 0
        # At the shop, an accepted purchase is debited now, arrives next turn.
        data['teamOur']['roles'][1]['pos'] = dict(x=6, y=10)
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertTrue(ledger.add(1, command('buy', name=PACK, num=8)))
        self.assertEqual(maintenance_budget(turn, mem, ledger, turn.workers[0]), (0, 90))

    def test_recent_consumption_increases_buffer_and_failure_vetoes_attack(self):
        data, cfg = self.data(packs=10)
        mem = Memory(wall_watch_id=1)
        record = NightRecord(5, 10, 70, {}, used=9)
        mem.wall_watch.history.append(record)
        accepted, _, _, _, ledger, _ = self.buy(data, cfg, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.spending_plan['late_offense']['next_day_buffer'], 165)
        for field in ('unmet', 'lost'):
            mem.robot_summon_state = None
            setattr(record, field, {100})
            accepted, _, _, _, ledger, _ = self.buy(data, cfg, mem)
            self.assertFalse(accepted)
            self.assertEqual(ledger.spending_plan['robot_blocked'], 'late_offense_recent_defense_failure')
            setattr(record, field, set())

    def test_mining_window_and_return_deadline_prevent_purchase(self):
        for tick in (34, 66, 69, 70):
            data, cfg = self.data(tick=tick)
            accepted, _, _, _, ledger, _ = self.buy(data, cfg)
            self.assertFalse(accepted)
            self.assertFalse(ledger.purchases)

    def test_return_budget_shrinks_five_to_four_before_canceling(self):
        data, cfg = self.data(tick=58)
        accepted, _, _, _, ledger, _ = self.buy(data, cfg)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=4))
        data['roundNo'] += 1
        self.assertFalse(self.buy(data, cfg)[0])

    def test_first_day_abort_does_not_disqualify_sixth_day(self):
        data, cfg = self.data()
        mem = Memory(wall_watch_id=1)
        mem.night_scout.cancelled = True
        mem.night_scout.cancel_reason = 'enemy_gatling_spotted'
        self.assertTrue(self.buy(data, cfg, mem)[0])
        self.assertTrue(mem.night_scout.cancelled)

    def test_remembered_gatling_coverage_and_strong_ring_veto_purchase(self):
        data, cfg = self.data()
        turn, _, _, _ = setup(data, cfg)
        mem = Memory(wall_watch_id=1)
        remembered = unit(999, 'gatling', 14, 5, attackRange=30)
        # Use the model parser, retaining a weapon which has lost visibility.
        data_visible = copy.deepcopy(data)
        data_visible['teamEnemy']['roles'].append(remembered)
        visible_turn, _, _, _ = setup(data_visible, cfg)
        mem.robot_assault = RobotAssaultMemory(buildings={999: next(
            u for u in visible_turn.enemies if u.id == 999)})
        self.assertFalse(self.buy(data, cfg, mem)[0])
        for i, point in enumerate(enemy_entries(turn, turn.enemies[0])):
            data['teamEnemy']['roles'].append(unit(800 + i, 'wall', *point, level=3, health=2000))
        self.assertFalse(self.buy(data, cfg)[0])

    def test_buy_confirmation_then_five_unique_clustered_uses_and_no_rebuy(self):
        data, cfg = self.data()
        accepted, _, _, _, ledger, mem = self.buy(data, cfg)
        self.assertTrue(accepted)
        data['teamOur']['roles'][2]['backpack'] = [BOSS] * 5
        data['teamOur']['goldNum'] = 400
        positions = []
        for i in range(5):
            data['roundNo'] += 1
            data['lastRoundRoleActionResults'] = {'2': True}
            accepted, _, _, _, ledger, mem = self.buy(data, cfg, mem)
            self.assertTrue(accepted)
            self.assertEqual(ledger.commands['2']['action'], 'use')
            positions.append(pos(ledger.commands['2']['targetPos'][0]))
            data['teamOur']['roles'][2]['backpack'].pop()
        self.assertEqual(len(set(positions)), 5)
        self.assertTrue(all(distance(p, mem.robot_summon_state['anchor']) <= 2 for p in positions))
        data['roundNo'] += 1
        data['teamOur']['goldNum'] = 2000
        self.assertFalse(self.buy(data, cfg, mem)[0])

    def test_purchase_without_receipt_releases_batch_for_retry(self):
        data, cfg = self.data()
        _, _, _, _, _, mem = self.buy(data, cfg)
        data['roundNo'] += 1
        data['lastRoundRoleActionResults'] = {'2': True}
        accepted, _, _, _, ledger, mem = self.buy(data, cfg, mem)
        self.assertTrue(accepted)
        self.assertEqual(ledger.commands['2'], command('buy', name=BOSS, num=5))
        self.assertEqual(mem.robot_summon_state['purchased'], 5)

    def test_dead_carrier_with_received_stock_does_not_trigger_another_batch(self):
        data, cfg = self.data()
        _, _, _, _, _, mem = self.buy(data, cfg)
        data['roundNo'] += 1
        data['teamOur']['roles'][2].update(health=0, backpack=[BOSS] * 5)
        data['lastRoundRoleActionResults'] = {'2': True}
        accepted, _, _, _, ledger, mem = self.buy(data, cfg, mem)
        self.assertFalse(accepted)
        self.assertTrue(mem.robot_summon_state['late_batch_bought'])
        self.assertFalse(ledger.purchases)

    def test_front_reaches_three_and_damaged_side_stops_at_two(self):
        data, cfg = self.data()
        walls = [r for r in data['teamOur']['roles'] if r['roleType'] == 'wall']
        front = next(r for r in walls if pos(r['pos']) == (8, 5))
        front.update(level=1, health=1000)
        side = next(r for r in walls if pos(r['pos']) == (3, 5))
        side.update(level=1, health=900)
        turn, _, _, ledger = setup(data, cfg)
        mem = Memory(wall_watch_id=1)
        report = shopping_list(turn, mem, turn.workers[0], ledger.wall_cells, 1000, 100)
        self.assertEqual(report['order'], {V1: 2, V2: 1})
        self.assertEqual(report['demand'][V2], 1)
        self.assertEqual(shopping_list(turn, mem, turn.workers[0], ledger.wall_cells, 20, 100)['order'], {V1: 1})
        data['roundNo'] = 780 + 40
        turn, _, _, ledger = setup(data, cfg)
        self.assertFalse(shopping_list(turn, mem, turn.workers[0], ledger.wall_cells, 1000, 100)['deferred'])

    def test_base_voucher_guard_rejects_duplicates_obsolete_tier_and_batch(self):
        data, cfg = self.data()
        for level, stock, item, count, allowed in (
                (1, [BASE], BASE, 1, False), (2, [], BASE, 1, False),
                (3, [], 'StationUpgradeVoucher2', 1, False),
                (1, [], BASE, 2, False), (1, [], BASE, 1, True),
                (2, [], 'StationUpgradeVoucher2', 1, True)):
            data['teamOur']['roles'][0]['level'] = level
            data['teamOur']['roles'][3].update(backpack=stock, pos=dict(x=6, y=10))
            data['weaponShopList'].append(dict(name='StationUpgradeVoucher2', price=150))
            turn, cfg, nav, ledger = setup(data, cfg)
            self.assertEqual(buy_supply(turn, ledger, turn.pioneer, (item, (0, None), count)), allowed)

    def test_general_supplies_do_not_prefetch_base_tier_two_while_one_is_held(self):
        data, cfg = self.data()
        data['teamOur']['roles'][3]['pos'] = dict(x=6, y=10)
        data['weaponShopList'].append(dict(name='StationUpgradeVoucher2', price=150))
        turn, cfg, nav, ledger = setup(data, cfg)
        self.assertIsNone(supplies(turn, cfg, Memory(), nav, ledger, turn.pioneer,
                                   bulk=True, item_only='StationUpgradeVoucher2'))
