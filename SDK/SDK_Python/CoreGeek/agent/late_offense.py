"""One sixth-night BOSS batch after maintenance and a cash safety buffer."""
from collections import Counter
from dataclasses import replace

from .model import WEAPONS, distance
from .robot_assault import enemy_base, weakest_approach, choose_summon_position
from .wall_supply import requirements, PACK, V1, V2

OFFENSE_DAY = 6
MAX_BOSSES = 5


def purchase_day_allowed(turn, cfg):
    return turn.day <= cfg.robot_purchase_last_day or turn.day == OFFENSE_DAY


def maintenance_budget(turn, mem, ledger, watcher):
    """Net remaining obligations against today's already debited reservations."""
    from .economy import voucher_for, station_voucher_needed
    report = requirements(turn, mem, watcher, ledger.wall_cells)
    demand = Counter({PACK: report['demand'][PACK], V1: report['key_one'],
                      V2: report['key_three']})
    hero = turn.pioneer
    base_item = voucher_for(turn.station) if turn.station else None
    if station_voucher_needed(turn, hero, base_item):
        demand[base_item] = 1
    medicine = sum(h.health <= 165 and not h.inventory['Medicine'] for h in turn.heroes)
    if medicine:
        demand['Medicine'] = medicine
    # Current-round buys have paid ledger gold but are absent from the input
    # backpack until next turn. Debit those obligations exactly once.
    for uid, cmd in ledger.commands.items():
        if cmd['action'] == 'buy':
            name = cmd['name']
            if name != PACK or int(uid) == watcher.id:
                demand[name] = max(0, demand[name] - cmd.get('num', 1))
    if any(n > 0 and turn.shop.get(name, -1) < 0 for name, n in demand.items()):
        return None
    required = sum(n * turn.shop.get(name, 0) for name, n in demand.items())
    required = max(0, required - getattr(ledger, 'watch_gold_reserved', 0))
    # At least half the next day's baseline (12 packs), increasing with real
    # recent use. This is cash only; do not buy a second physical pack reserve.
    recent = max((r.used for r in mem.wall_watch.history), default=0)
    buffer = max(6, recent + 2) * max(0, turn.shop.get(PACK, 15))
    return required, buffer


def evaluate(turn, cfg, mem, nav, ledger, state, reserve):
    """Do not commit funds without defense, a viable lane and enough BOSSes."""
    result = dict(day=turn.day, gold_available=ledger.gold, reserve=reserve)

    def blocked(reason):
        result['blocked'] = reason
        ledger.spending_plan['late_offense'] = result
        ledger.spending_plan['robot_blocked'] = reason
        return None

    if not turn.is_day or turn.tick < cfg.economy_rounds:
        return blocked('late_offense_mining_window')
    if state.get('late_batch_bought'):
        return blocked('late_offense_batch_purchased')
    base = enemy_base(turn)
    watcher = next((h for h in turn.workers if h.id == mem.wall_watch_id), None)
    if not watcher or not turn.station or turn.station.health < 500:
        return blocked('late_offense_defense_unready')
    if len(turn.weapons) < len(cfg.loadout) or any(w.level < 3 for w in turn.weapons):
        return blocked('late_offense_firepower_unready')
    if set(ledger.wall_cells) - {w.pos for w in turn.ours if w.kind == 'wall'}:
        return blocked('late_offense_wall_gap')
    previous = mem.wall_watch.history[-1] if mem.wall_watch.history else None
    if previous and (previous.unmet or previous.lost):
        return blocked('late_offense_recent_defense_failure')
    if base is None:
        return blocked('enemy_base_missing')
    remembered = getattr(getattr(mem, 'robot_assault', None), 'buildings', {}).values()
    known = {u.id: u for u in (*ledger.robot_known_enemies, *remembered, *turn.enemies)
             if u.kind in WEAPONS}
    ledger.late_known_weapons = tuple(known.values())
    # A hidden remembered gatling remains a threat. Do not infer an unarmed
    # lane merely from losing sight of its operator or weapon.
    plan = weakest_approach(turn, ledger, nav.deadline, prefer_rear=True,
        wall_filter=lambda wall: wall is None or wall.health <= 500
                                 or base.health <= 1000 and wall.level == 1,
        spawn_filter=lambda p: not any(w.kind == 'gatling' and distance(w.pos, p) <= w.attack_range
                                       for w in known.values()))
    if plan is None:
        return blocked('late_offense_no_weak_uncovered_entry')
    budget = maintenance_budget(turn, mem, ledger, watcher)
    if budget is None:
        return blocked('late_offense_missing_defense_price')
    bought_packs = ledger.commands.get(str(watcher.id), {})
    bought_packs = (bought_packs.get('num', 1) if bought_packs.get('action') == 'buy'
                    and bought_packs.get('name') == PACK else 0)
    missing_packs = max(0, mem.wall_watch.stock_target(turn) - watcher.inventory[PACK])
    if missing_packs > max(bought_packs, ledger.watch_pack_slots.get(watcher.id, 0)):
        return blocked('late_offense_stock_trip_unready')
    required, buffer = budget
    protected = max(reserve, required) + buffer
    available = max(0, ledger.gold - protected)
    price = turn.shop.get('BossRobotSummonOrder', -1)
    minimum = 3 if base.health <= 1000 else 4
    count = min(MAX_BOSSES, available // price if price > 0 else MAX_BOSSES)
    result.update(maintenance=required, next_day_buffer=buffer, reserve=protected,
                  surplus=available, minimum=minimum, affordable=count,
                  entry=plan.entry, anchor=plan.position)
    if price < 0 or count < minimum:
        return blocked('late_offense_insufficient_batch_budget')
    # Check all placements before paying. Keep one compact group using the
    # same side/anchor during subsequent serialized daylight uses.
    old = (ledger.robot_assault_entry, ledger.robot_summon_anchor,
           ledger.robot_summon_mode, ledger.summon_pending_positions.copy())
    try:
        ledger.robot_assault_entry = plan.entry
        ledger.robot_summon_anchor = plan.position
        ledger.robot_summon_mode = 'late_wave'
        positions = []
        for _ in range(count):
            point = choose_summon_position(turn, ledger, nav.deadline)
            if point is None:
                break
            positions.append(point)
            ledger.summon_pending_positions.add(point)
    finally:
        (ledger.robot_assault_entry, ledger.robot_summon_anchor,
         ledger.robot_summon_mode, ledger.summon_pending_positions) = old
    count = min(count, len(positions))
    if count < minimum:
        return blocked('late_offense_insufficient_spawn_slots')
    result['quantity'] = count
    ledger.spending_plan['late_offense'] = result
    return result


def purchase_fits(turn, cfg, mem, nav, ledger, hero, shop, count, held):
    """Buy + serialized uses + the buyer's actual night post, from the shop."""
    from .mining import return_destination
    from .economy_plan import via
    home, exact = return_destination(turn, nav, ledger, hero, mem)
    if not home:
        return False
    # Charge the endpoint of the very same approach that shopping will use,
    # rather than another tile with an attractive but unrelated return path.
    outward = nav.approach(hero, [shop], ledger.reserved, with_endpoint=True)
    if outward is None:
        return False
    original = turn.blocked
    try:
        turn.blocked = original - {hero.pos}
        back = via(nav, replace(hero, pos=outward[2]), [home], ledger.reserved,
                   final_exact=exact, future_return=True)
    finally:
        turn.blocked = original
    return (back is not None and
            outward[0] + 1 + count + held + back + cfg.return_margin < turn.day_left)
