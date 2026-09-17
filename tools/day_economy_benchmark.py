"""Controlled opening economy with finite deposits, shopping and construction.

Starts with 75 gold and no weapons. Records actual level/crew readiness at dusk.
No robot combat, task rewards, hidden-map generation or survival claims.
"""
import argparse
from collections import Counter
import copy
from itertools import combinations
import json
from pathlib import Path
import sys
from time import monotonic


def role(uid, kind, x, y, **kw):
    return {"id": uid, "roleType": kind, "pos": {"x": x, "y": y}, "health": 220,
            "level": 1, "backpack": [], "backPackCapability": 100,
            "attackRange": 8, "attackPower": 40, "cooldown": 0, **kw}


def simulate(Agent, Config, case="near", mirror=False, days=1, trace=False, damage_walls=False):
    width, height = 41, 32
    def flip(p):
        return (width-1-p[0], height-1-p[1]) if mirror else p

    def point(r):
        return r["pos"]["x"], r["pos"]["y"]

    def near(a, b):
        return a != b and max(abs(a[0]-b[0]), abs(a[1]-b[1])) == 1

    def cells(r):
        x, y = point(r)
        return {(x, y), (x+1, y), (x, y-1), (x+1, y-1)} if r["roleType"] == "station" else {(x, y)}

    def make(uid, kind, p, **kw):
        x, y = flip(p)
        if mirror and kind == "station":
            x, y = x-1, y+1
        return role(uid, kind, x, y, **kw)

    roles = [make(13, "station", (5, 15), health=1500), make(1, "worker", (4, 13)),
             make(2, "worker", (4, 15)), make(3, "pioneer", (4, 14), health=200)]
    # Geometry follows the documented inferred base ring, independently of layout().
    wall_sites = {flip((x, y)) for x in range(3, 9) for y in range(12, 18)
                  if (x in (3, 8) or y in (12, 17)) and (x, y) not in ((3, 14), (3, 15))}
    weapon_sites = {flip((x, y)) for x in range(4, 8) for y in range(13, 17)
                    if x in (4, 7) or y in (13, 16)}
    front = {flip((8, y)) for y in range(12, 18)}
    deposits = {"copper": [(14, 12), (14, 16), (16, 12), (16, 16)],
                "stone": [(10, 11), (10, 18), (11, 11), (11, 18)]}
    if case == "remote_ore":
        deposits["copper"] = [(30, 12), (30, 16), (32, 12), (32, 16)]
        deposits["iron"] = [(14, 13), (14, 17), (16, 13), (16, 17)]
    if case == "local_ore":
        deposits["iron"] = [(2, 11), (2, 17), (1, 13), (1, 16)]
    deposits = {k: list(map(flip, ps)) for k, ps in deposits.items()}
    active = {ps[0]: [k, 10] for k, ps in deposits.items()}
    indices = {k: 0 for k in deposits}
    vendor = flip((12, 14))
    shop = flip((24, 18) if case == "far_shop" else (12, 18))
    prices = {"copper": 10, "iron": 6, "stone": 1}
    products = {"WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150,
                "StationUpgradeVoucher1": 100, "StationUpgradeVoucher2": 150,
                "WallUpgradeVoucher1": 20, "WallUpgradeVoucher2": 30,
                "Medicine": 10, "WallFixer": 10}
    cfg, actions, purchases = Config(llm_enabled=False), Counter(), Counter()
    agent = Agent(cfg)
    state = {"roundNo": 1, "mapInfo": {"width": width, "height": height, "zones": []},
             "teamOur": {"type": "defender" if mirror else "challenger", "teamId": "opening",
                         "goldNum": 75, "roles": roles, "playerTasks": []},
             "teamEnemy": {"roles": []}, "robot": {"roles": []}, "phaseTask": "", "worldNews": {},
             "llmResp": "", "lastCmdResult": "", "errors": [],
             "weaponShopList": [{"name": n, "price": p} for n, p in products.items()],
             "vendorShopList": [{"name": n, "price": p} for n, p in prices.items()]}
    invalid, income, spent, worst = 0, 0, 0, 0
    checkpoints, early_actions, first = {}, Counter(), {}
    worker_actions, mined_by_kind = Counter(), Counter()
    daily_worker_actions = {}
    destroyed_walls = 0
    next_build_id = 1000 + len(roles)
    history, previous, reversals = [], {}, 0
    for rno in range(1, days * 130):
        state["roundNo"] = rno
        if damage_walls and rno >= 130 and rno % 130 == 0:
            victims = [r for r in roles if r["roleType"] == "wall" and point(r) in front][:2]
            for victim in victims:
                roles.remove(victim)
            destroyed_walls += len(victims)
        state["mapInfo"]["zones"] = [{"neutralType": kind, "pos": dict(zip(("x", "y"), p))}
                                      for kind, p in [("vendor", vendor), ("weaponShop", shop)]
                                      + [(k, p) for p, (k, _) in active.items()]]
        occupied = set(active) | {vendor, shop} | set().union(*(cells(r) for r in roles))
        started = monotonic()
        commands = agent.decide(copy.deepcopy(state))["roleCommandMap"]
        worst = max(worst, (monotonic()-started)*1000)
        by_id = {str(r["id"]): r for r in roles}
        if trace:
            history.append({"round": rno, "gold": state["teamOur"]["goldNum"],
                            "workers": [{"id": h["id"], "pos": h["pos"].copy(),
                                         "inventory": dict(Counter(h["backpack"])),
                                         "command": commands.get(str(h["id"]))}
                                        for h in roles if h["roleType"] == "worker"]})
        results, collected = {}, Counter()
        for uid, cmd in commands.items():
            actor, action = by_id[uid], cmd["action"]
            actions[action] += 1
            if rno <= 40 and actor["roleType"] == "worker":
                early_actions[action] += 1
            if rno % 130 < 70 and actor["roleType"] == "worker":
                daily_worker_actions.setdefault(str(rno // 130 + 1), Counter())[action] += 1
            target = tuple(cmd["targetPos"][0][k] for k in ("x", "y")) if cmd.get("targetPos") else None
            name, num = cmd.get("name"), cmd.get("num", 1)
            if rno < 70 and actor["roleType"] == "worker":
                worker_actions[action] += 1
                if action == "move":
                    reversals += int(previous.get(uid) == target)
                    previous[uid] = point(actor)
                else:
                    previous.pop(uid, None)
            legal = False
            if action == "move":
                legal = near(point(actor), target) and target not in occupied and 0 <= target[0] < width and 0 <= target[1] < height
                if legal:
                    actor["pos"] = dict(zip(("x", "y"), target))
                    occupied.add(target)
            elif action == "collect":
                legal = (actor["roleType"] == "worker" and near(point(actor), target)
                         and target in active and len(actor["backpack"]) < actor["backPackCapability"])
                if legal:
                    actor["backpack"].append(active[target][0])
                    collected[target] += 1
                    if rno < 70:
                        mined_by_kind[active[target][0]] += 1
            elif action == "sell":
                legal = near(point(actor), vendor) and name in prices and actor["backpack"].count(name) >= num
                if legal:
                    for _ in range(num):
                        actor["backpack"].remove(name)
                    amount = prices[name] * num
                    income += amount
                    state["teamOur"]["goldNum"] += amount
            elif action == "buy":
                legal = (near(point(actor), shop) and name in products
                         and products[name]*num <= state["teamOur"]["goldNum"]
                         and len(actor["backpack"])+num <= actor["backPackCapability"])
                if legal:
                    amount = products[name]*num
                    spent += amount
                    state["teamOur"]["goldNum"] -= amount
                    actor["backpack"] += [name]*num
                    purchases[name] += num
            elif action == "build":
                legal = (rno % 130 < 70 and actor["roleType"] == "worker" and near(point(actor), target)
                         and target not in occupied)
                if name == "wall":
                    legal &= target in wall_sites and actor["backpack"].count("stone") >= cfg.wall_stones
                    if legal:
                        for _ in range(cfg.wall_stones):
                            actor["backpack"].remove("stone")
                else:
                    legal &= (name in ("rocket", "railgun", "gatling") and target in weapon_sites
                              and state["teamOur"]["goldNum"] >= cfg.weapon_cost
                              and sum(r["roleType"] in ("rocket", "railgun", "gatling") for r in roles) < 3)
                    if legal:
                        spent += cfg.weapon_cost
                        state["teamOur"]["goldNum"] -= cfg.weapon_cost
                if legal:
                    roles.append(role(next_build_id, name, *target, health=1000))
                    next_build_id += 1
                    occupied.add(target)
            elif action == "use":
                legal = name in actor["backpack"]
                if name == "Medicine":
                    if legal:
                        actor["health"] = 220 if actor["roleType"] == "worker" else 200
                else:
                    building = next((r for r in roles if point(r) == target), None)
                    legal &= bool(building and any(near(point(actor), c) for c in cells(building)))
                    if "UpgradeVoucher" in str(name):
                        kinds = ("rocket", "gatling", "railgun") if name.startswith("Weapon") else ("station",) if name.startswith("Station") else ("wall",)
                        legal &= bool(building and building["roleType"] in kinds and building["level"] < 3 and name.endswith(str(building["level"])))
                        if legal:
                            building["level"] += 1
                    elif name == "WallFixer":
                        legal &= bool(building and building["roleType"] == "wall")
                    else:
                        legal = False
                    if legal:
                        building["health"] = 1500 if building["roleType"] == "station" else 1000
                if legal:
                    actor["backpack"].remove(name)
            if legal:
                first.setdefault(action + ("_"+name if name else ""), rno)
            else:
                invalid += 1
            results[uid] = bool(legal)
        for p, n in collected.items():
            active[p][1] -= n
            if active[p][1] <= 0:
                kind = active.pop(p)[0]
                indices[kind] += 1
                active[deposits[kind][indices[kind] % len(deposits[kind])]] = [kind, 10]
        state["lastRoundRoleActionResults"] = results
        if rno % 130 in (39, 40, 69, 70):
            guns = [r for r in roles if r["roleType"] in ("rocket", "gatling", "railgun")]
            heroes = [r for r in roles if r["roleType"] in ("worker", "pioneer")]
            coverage = [{w["id"] for w in guns if near(point(h), point(w))} for h in heroes]
            ready = set().union(*coverage) if coverage else set()
            all_guns = {w["id"] for w in guns}
            crew = next((count for count in range(1, len(heroes) + 1)
                         if any(set().union(*(coverage[i] for i in selected)) >= all_guns
                                for selected in combinations(range(len(heroes)), count))), 0)
            checkpoints[str(rno)] = {"gold": state["teamOur"]["goldNum"], "income": income,
                "spent": spent, "weapon_levels": sorted(r["level"] for r in guns),
                "operators_ready": len(ready), "operators_used": crew,
                "walls": sum(r["roleType"] == "wall" for r in roles),
                "front_walls": sum(r["roleType"] == "wall" and point(r) in front for r in roles),
                "carried_vouchers": sum("UpgradeVoucher" in n for h in heroes for n in h["backpack"]),
                "carried_ore_value": sum(prices.get(n, 0) for h in heroes for n in h["backpack"])}
    return {"case": case, "mirror": mirror, "days": days, "checkpoints": checkpoints,
            "early_worker_actions": dict(early_actions), "actions": dict(actions),
            "purchases": dict(purchases), "first": first, "invalid_actions": invalid,
            "worker_actions_before_70": dict(worker_actions),
            "mined_before_70": dict(mined_by_kind), "worker_reversals_before_70": reversals,
            "daily_worker_actions": {d: dict(c) for d, c in daily_worker_actions.items()},
            "destroyed_walls": destroyed_walls,
            **({"trace": history} if trace else {}), "worst_ms": round(worst, 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--case", choices=("near", "far_shop", "remote_ore", "local_ore"), default="near")
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--fixed-40", action="store_true", help="Control: disable adaptive preparation deadline")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--damage-walls", action="store_true", help="Remove two front walls at later dawns; no combat simulation")
    args = parser.parse_args()
    sys.path.insert(0, str(args.agent_root / "SDK/SDK_Python/CoreGeek"))
    from agent.brain import Agent
    from agent.config import Config
    if args.fixed_40:
        import agent.economy
        agent.economy.preparation_start = lambda *a: 40
    print(json.dumps(simulate(Agent, Config, args.case, args.mirror, args.days, args.trace,
                              args.damage_walls), ensure_ascii=False, indent=2))
