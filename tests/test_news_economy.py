"""Dated official-news reasoning changes real mining/sale commands, not prices."""
import json
import unittest

from test_agent import unit, setup_case
from test_mining_runs import mining_case
from agent.intelligence import Memory
from agent.market import merge_signals, observe_prices, cash_floor
from agent.mining import earn, mine
from agent.worker_jobs import _continue


QUOTE = '铁矿明日起停产两天，供应短缺将推高铁矿回收价格。'


def signal(trend='up', name='iron', start=3, end=4, quote=QUOTE):
    return dict(name=name, trend=trend, startDay=start, endDay=end, sourceDay=2, quote=quote)


def case(day=2, tick=20, bag=(), gold=500, price=6):
    p = mining_case((day - 1) * 130 + tick, bag,
                    zones=[('iron', 6, 5), ('copper', 5, 6), ('vendor', 4, 5)])
    p['teamOur']['roles'] += [unit(13, 'station', 1, 9, health=1500, level=2),
                             unit(20, 'rocket', 2, 7, health=500, level=2),
                             unit(21, 'rocket', 3, 7, health=500, level=2),
                             unit(22, 'rocket', 4, 7, health=500, level=2)]
    p['teamOur']['goldNum'] = gold
    p['vendorShopList'] = [{'name': 'iron', 'price': price}, {'name': 'copper', 'price': 7},
                            {'name': 'stone', 'price': 3}]
    p['weaponShopList'] = [{'name': 'WeaponUpgradeVoucher2', 'price': 150},
                           {'name': 'StationUpgradeVoucher2', 'price': 150}]
    return p


def memory(*signals):
    mem = Memory()
    mem.market_prices = {2: {'iron': 6, 'copper': 7, 'stone': 3}}
    news = [{'day': s['sourceDay'], 'officialNews': s['quote']} for s in signals]
    mem.market_signals = merge_signals([], list(signals), news, mem.market_prices, 2)
    return mem


class NewsEconomyTests(unittest.TestCase):
    def sale(self, data, mem, **kwargs):
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit')
        earn(turn, cfg, mem, nav, ledger, turn.workers[0], **kwargs)
        return ledger.commands.get('1', {}), ledger

    def test_news_response_reaches_stock_policy_with_quote_and_live_reference(self):
        data = case(bag=['iron'] * 14)
        data['worldNews'] = {'officialNews': QUOTE}
        data['llmResp'] = json.dumps({'marketSignals': [signal()]})
        turn, cfg, _, _ = setup_case(data)
        mem = Memory(pending=('news', turn.round - 1))
        mem.observe(turn, cfg)
        self.assertEqual(mem.market_signals[0]['referencePrice'], 6)
        action, _ = self.sale(data, mem, force_sale=True)
        self.assertEqual(action, {'action': 'sell', 'name': 'iron', 'num': 4})
        # An unrelated reply or absent field must not erase the future event.
        mem.pending = ('news', turn.round)
        data.update(roundNo=turn.round + 1, llmResp='{"treasure": null}')
        turn, cfg, _, _ = setup_case(data)
        mem.observe(turn, cfg)
        self.assertEqual(len(mem.market_signals), 1)

    def test_only_official_exact_source_and_bounded_dates_authorize_signal(self):
        for updates in ({'quote': '没有出现在新闻里的内容'}, {'sourceDay': True}, {'name': 'gold'},
                        {'trend': 'maybe'}, {'endDay': 11}, {'startDay': 1}, {'startDay': 5, 'endDay': 4}):
            candidate = dict(signal(), **updates)
            self.assertFalse(merge_signals([], [candidate], [{'day': 2, 'officialNews': QUOTE}], {}, 2))
        self.assertFalse(merge_signals([], [signal()], [{'day': 2, 'folkLegends': QUOTE}], {}, 2))

    def test_nearby_pre_shortage_mining_changes_without_inventing_price(self):
        outputs = []
        for mem in (Memory(), memory(signal())):
            turn, cfg, nav, ledger = setup_case(case(), layout_mode='explicit')
            mine(turn, cfg, mem, nav, ledger, turn.workers[0])
            outputs.append(ledger.commands['1']['targetPos'][0])
            self.assertEqual(turn.prices['iron'], 6)
        self.assertEqual(outputs, [{'x': 5, 'y': 6}, {'x': 6, 'y': 5}])
        # Once ten iron are held, ordinary yield ranking takes over again.
        turn, cfg, nav, ledger = setup_case(case(bag=['iron'] * 10), layout_mode='explicit')
        mine(turn, cfg, memory(signal()), nav, ledger, turn.workers[0])
        self.assertEqual(ledger.commands['1']['targetPos'][0], {'x': 5, 'y': 6})

    def test_actual_price_rise_sells_saved_ore_even_after_daily_sale(self):
        mem = memory(signal())
        mem.sold_workers.add(1)
        action, _ = self.sale(case(day=3, bag=['iron'] * 10, price=12), mem)
        self.assertEqual(action, {'action': 'sell', 'name': 'iron', 'num': 10})
        self.assertEqual(action['num'] * 12 - action['num'] * 6, 60)

    def test_no_rise_releases_stock_without_faking_profit(self):
        mem = memory(signal())
        action, _ = self.sale(case(day=3, bag=['iron'] * 10), mem, force_sale=True)
        self.assertEqual(action['num'], 10)
        self.assertFalse(self.sale(case(day=3, bag=['iron'] * 10), mem)[1].notes[1]['conditions'].get('news_cashout', False))

    def test_fall_tomorrow_sells_small_load_before_further_mining(self):
        mem = memory(signal('down', quote='铁矿明日恢复供应，两天内回收价格将回落。'))
        action, ledger = self.sale(case(bag=['iron'] * 3), mem)
        self.assertEqual(action, {'action': 'sell', 'name': 'iron', 'num': 3})
        self.assertTrue(ledger.notes[1]['conditions']['news_cashout'])

    def test_existing_mining_job_yields_to_high_price_cashout(self):
        turn, cfg, nav, ledger = setup_case(case(day=3, bag=['iron'] * 10, price=12), layout_mode='explicit')
        job = dict(kind='mine', target=(5, 6), ore='copper', want_stone=False, stockpile=False, deadline=40)
        ok, _ = _continue(turn, cfg, memory(signal()), nav, ledger, turn.workers[0], job, [], [])
        self.assertTrue(ok)
        self.assertEqual(ledger.commands['1']['action'], 'sell')

    def test_liquidity_full_backpack_and_emergency_release_held_stock(self):
        for change in ('gold', 'bag', 'base', 'worker', 'robot', 'day1'):
            data = case(bag=['iron'] * 10)
            if change == 'gold': data['teamOur']['goldNum'] = 149
            if change == 'bag': data['teamOur']['roles'][0]['backPackCapability'] = 10
            if change == 'base': data['teamOur']['roles'][1]['health'] = 499
            if change == 'worker': data['teamOur']['roles'][0]['health'] = 100
            if change == 'robot': data['robot']['roles'] = [unit(90, 'smallRobot', 7, 8, attackRange=1, targetTeam='challenger')]
            if change == 'day1': data['roundNo'] = 20
            action, _ = self.sale(data, memory(signal()), force_sale=True)
            self.assertEqual(action.get('num'), 10, change)
        turn, cfg, _, _ = setup_case(case())
        self.assertEqual(cash_floor(turn, cfg), 150)

    def test_stone_for_construction_is_never_speculated_or_sold(self):
        mem = memory(signal(name='stone'))
        mem.stone_reserves[1] = 5
        action, _ = self.sale(case(bag=['stone'] * 20), mem, force_sale=True)
        self.assertEqual(action, {'action': 'sell', 'name': 'stone', 'num': 5})

    def test_conflicting_stale_or_distant_events_do_not_lock_inventory(self):
        for mem in (memory(signal(), signal('down')), memory(signal(), signal('down', start=2)),
                    memory(signal(start=4)), memory(signal())):
            data = case(bag=['iron'] * 10)
            if len(mem.market_signals) == 1 and mem.market_signals[0]['startDay'] == 3:
                data['roundNo'] = 4 * 130 + 20
            action, _ = self.sale(data, mem, force_sale=True)
            self.assertEqual(action.get('num'), 10)
        turn, _, _, _ = setup_case(case(day=5))
        observe_prices(turn, mem)
        self.assertFalse(mem.market_signals)

    def test_unrelated_reply_and_invalid_proposal_preserve_valid_signal(self):
        mem = memory(signal())
        for proposed in (None, [], 'invalid', [{'name': 'iron'}]):
            self.assertEqual(merge_signals(mem.market_signals, proposed, [], mem.market_prices, 2), mem.market_signals)

    def test_end_to_end_two_day_sale_earns_more_for_identical_ore(self):
        # Same input ore and observed prices: baseline sells everything at six;
        # news-aware policy sells surplus now and ten units at twelve tomorrow.
        proceeds = []
        for mem in (Memory(), memory(signal())):
            bag = ['iron'] * 14
            gold = 0
            for day, price in ((2, 6), (3, 12)):
                action, _ = self.sale(case(day=day, bag=bag, price=price), mem, force_sale=True)
                if action.get('action') == 'sell':
                    gold += action['num'] * price
                    for _ in range(action['num']): bag.remove(action['name'])
            proceeds.append(gold)
            self.assertFalse(bag)
        self.assertEqual(proceeds, [84, 144])

    def test_news_cashout_does_not_create_one_ore_sell_loop(self):
        mem = memory(signal())
        action, _ = self.sale(case(day=3, bag=['iron'] * 10, price=12), mem)
        self.assertEqual(action['action'], 'sell')
        mem.sale_workers.clear()  # A completed sale followed by new collection.
        action, _ = self.sale(case(day=3, bag=['iron'], price=12), mem)
        self.assertEqual(action['action'], 'collect')
        self.assertEqual(mem.market_cashouts, {(3, 1, 'iron')})
        turn, _, _, _ = setup_case(case(day=4, bag=['iron'], price=12))
        observe_prices(turn, mem)
        self.assertFalse(mem.market_cashouts)
