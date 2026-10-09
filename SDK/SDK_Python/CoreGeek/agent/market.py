"""Official-news signals guide bounded inventory timing; live prices settle trades."""
from .model import ORES, distance


STOCK_LIMIT = 10  # At most one deposit's output per worker waits for tomorrow.


def observe_prices(turn, mem):
    mem.market_prices[turn.day] = {k: p for k, p in turn.prices.items() if k in ORES and p > 0}
    mem.market_prices = {d: p for d, p in mem.market_prices.items() if d >= turn.day - 2}
    mem.market_signals = [s for s in mem.market_signals if s['endDay'] >= turn.day]
    mem.market_cashouts = {key for key in mem.market_cashouts if key[0] == turn.day}


def merge_signals(previous, proposed, news, prices, day):
    """Retain dated evidence across unrelated replies; never invent a price."""
    signals = [s for s in previous if s['endDay'] >= day]
    if not isinstance(proposed, list):
        return signals
    for signal in proposed[:20]:
        if not isinstance(signal, dict):
            continue
        name, trend = signal.get('name'), signal.get('trend')
        start, end, source = (signal.get(k) for k in ('startDay', 'endDay', 'sourceDay'))
        quote = signal.get('quote')
        if (name not in ORES or trend not in ('up', 'down')
                or any(type(v) is not int for v in (start, end, source))
                or not 1 <= source <= day <= 10 or not source <= start <= end <= 10 or end < day
                or not isinstance(quote, str) or not 4 <= len(quote.strip()) <= 1000):
            continue
        quote = quote.strip()
        if not (any(n.get('day') == source and quote in n.get('officialNews', '') for n in news)
                or any(s['sourceDay'] == source and s['quote'] == quote for s in previous)):
            continue
        key = name, start, end
        matching = [s for s in signals if (s['name'], s['startDay'], s['endDay']) == key]
        old = matching[0] if matching else None
        if matching and max(s['sourceDay'] for s in matching) > source:
            continue
        prior = [d for d in prices if d < start and name in prices[d]]
        reference = prices[max(prior)][name] if prior else None
        entry = dict(name=name, trend=trend, startDay=start, endDay=end,
                     sourceDay=source, quote=quote,
                     referencePrice=old['referencePrice'] if old else reference)
        # Same-source contradictory conclusions stay ambiguous; a newer official
        # source may replace the old window, but list ordering cannot decide it.
        signals = [s for s in signals if (s['name'], s['startDay'], s['endDay']) != key
                   or s['sourceDay'] == source and s['trend'] != trend]
        signals.append(entry)
    return signals[-20:]


def signal_for(turn, mem, name):
    # Only tomorrow or the current event is actionable. Conflicting windows
    # are kept as evidence but cannot authorize speculative inventory changes.
    relevant = [s for s in mem.market_signals if s['name'] == name
                and s['startDay'] <= turn.day + 1 and s['endDay'] >= turn.day]
    if not relevant or len({s['trend'] for s in relevant}) != 1:
        return None
    return max(relevant, key=lambda s: (s['sourceDay'], s['startDay']))


def cash_floor(turn, cfg):
    # Fund missing weapons plus the next applicable core/weapon upgrade.
    upgrade_prices = [turn.shop.get('StationUpgradeVoucher' + str(turn.station.level), 0)] \
        if turn.station and turn.station.level < 3 else []
    upgrade_prices += [turn.shop.get('WeaponUpgradeVoucher' + str(w.level), 0)
                       for w in turn.weapons if w.level < 3]
    next_upgrade = min((p for p in upgrade_prices if p > 0), default=0)
    return max(cfg.weapon_cost, next_upgrade) + max(0, len(cfg.loadout) - len(turn.weapons)) * cfg.weapon_cost


def stock_candidates(turn, cfg, mem, ledger, hero):
    if (not mem.market_signals or not cfg.llm_enabled or turn.day == 1 or not turn.station
            or hero.kind != 'worker' or hero.id in (mem.wall_watch_id, mem.wall_repair_worker)
            or not hero.space or ledger.gold < cash_floor(turn, cfg)
            or turn.station.health < 500 or hero.health <= 165
            or any(w.kind == 'wall' and w.health < 500 for w in turn.ours)
            or any(turn.threatens_us(r) and (turn.base_distance(r.pos) <= max(6, r.attack_range)
                   or distance(hero.pos, r.pos) <= r.attack_range + 2) for r in turn.robots)):
        return set()
    candidates = set()
    for name in ORES:
        signal = signal_for(turn, mem, name)
        if (signal and signal['trend'] == 'up' and signal['startDay'] == turn.day + 1
                and signal['referencePrice'] is not None
                and 0 < turn.prices.get(name, 0) <= signal['referencePrice']):
            candidates.add(name)
    return candidates


def hold_inventory(turn, cfg, mem, ledger, hero, counts):
    held, room = {}, min(STOCK_LIMIT, hero.capacity // 4)
    for name in sorted(stock_candidates(turn, cfg, mem, ledger, hero)):
        amount = min(room, counts.get(name, 0))
        if amount:
            held[name] = amount
            room -= amount
    return held


def preferred_stock(turn, cfg, mem, ledger, hero):
    candidates = stock_candidates(turn, cfg, mem, ledger, hero)
    if sum(hero.inventory[k] for k in candidates) >= min(STOCK_LIMIT, hero.capacity // 4):
        return set()
    return candidates


def cashout_ores(turn, cfg, mem, hero, counts):
    if not cfg.llm_enabled:
        return set()
    result = set()
    for name in counts:
        if (turn.day, hero.id, name) in mem.market_cashouts:
            continue
        signal = signal_for(turn, mem, name)
        if not signal or signal['referencePrice'] is None:
            continue
        price, reference = turn.prices.get(name, 0), signal['referencePrice']
        if ((signal['trend'] == 'up' and price > reference)
                or (signal['trend'] == 'down' and signal['startDay'] == turn.day + 1
                    and price >= reference)):
            result.add(name)
    return result
