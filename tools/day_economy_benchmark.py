"""Controlled opening economy with finite deposits, shopping and construction.

Starts with 75 gold and no weapons. Records actual level/crew readiness at dusk.
Default: sample-map economy and seeded map-wide finite mine respawns.
The controlled profile retains the historical optimistic regression fixtures.
No robot combat, opponent mining, task rewards or survival claims.
"""
import argparse
from collections import Counter
import copy
from itertools import permutations
import json
import random
from statistics import median
from pathlib import Path
import sys
from time import monotonic


def role(uid, kind, x, y, **kw):
    return {"id": uid, "roleType": kind, "pos": {"x": x, "y": y}, "health": 220,
            "level": 1, "backpack": [], "backPackCapability": 100,
            "attackRange": 8, "attackPower": 40, "cooldown": 0, **kw}


def simulate(Agent, Config, case="near", mirror=False, days=1, trace=False, damage_walls=False,
             profile="sample", seed=0, mines_per_kind=2):
    if profile not in ("sample", "random", "controlled"):
        raise ValueError("unknown economy profile")
    if days < 1:
        raise ValueError("days must be positive")
    if profile != "controlled" and case != "near":
        raise ValueError("case variations require profile=controlled")
    if not isinstance(mines_per_kind, int) or not 1 <= mines_per_kind <= 20:
        raise ValueError("mines_per_kind must be between 1 and 20")
    if profile != "random" and mines_per_kind != 2:
        raise ValueError("mine count overrides require profile=random")
    rng = random.Random(seed)
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
    neutral_zones = [("vendor", vendor), ("weaponShop", shop)]
    forbidden = set()
    if profile != "controlled":
        # The checked-in request is a night snapshot, not a captured opening.
        # Reuse only its geometry and price tables; reset the opening economy.
        sample = json.loads((Path(__file__).resolve().parents[1] / "examples/request.json").read_text())
        roles = [make(13, "station", (10, 24), health=1500),
                 make(1, "worker", (9, 22)), make(2, "worker", (9, 24)),
                 make(3, "pioneer", (9, 23), health=200)]
        wall_sites = {flip((x, y)) for x in range(8, 14) for y in range(21, 27)
                      if (x in (8, 13) or y in (21, 26)) and (x, y) not in ((8, 23), (8, 24))}
        weapon_sites = {flip((x, y)) for x in range(9, 13) for y in range(22, 26)
                        if x in (9, 12) or y in (22, 25)}
        front = {flip((13, y)) for y in range(21, 27)}
        neutral_zones = [(z["neutralType"], flip(point(z))) for z in sample["mapInfo"]["zones"]
                         if z["neutralType"] not in prices]
        vendor = next(p for k, p in neutral_zones if k == "vendor")
        shop = next(p for k, p in neutral_zones if k == "weaponShop")
        prices = {v["name"]: v["price"] for v in sample["vendorShopList"] if v["name"] in prices}
        active = {flip(point(z)): [z["neutralType"], 10] for z in sample["mapInfo"]["zones"]
                  if z["neutralType"] in prices}
        # Exclude both entire inferred building rings, including the bases.
        forbidden = {flip((x, y)) for bx, by in ((10, 24), (30, 10))
                     for x in range(bx-2, bx+4) for y in range(by-3, by+3)}
    neutral_cells = {p for _, p in neutral_zones}

    def respawn(kind, old=None):
        occupied_now = set().union(*(cells(r) for r in roles))
        blocked = forbidden | neutral_cells | set(active) | occupied_now
        candidates = [flip((x, y)) for x in range(width) for y in range(height)
                      if flip((x, y)) not in blocked and flip((x, y)) != old]
        if not candidates:
            raise ValueError("no free cell for mine respawn")
        active[rng.choice(candidates)] = [kind, 10]

    if profile == "random":
        kinds = [kind for kind in sorted(prices) for _ in range(mines_per_kind)]
        active.clear()
        for kind in kinds:
            respawn(kind)
    initial_mines = [{"kind": k, "pos": list(p), "remaining": n} for p, (k, n) in active.items()]
    respawns = []
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
    # Keep historical controlled fixture indexing; new profiles model all 70 daylight ticks.
    for rno in range(1 if profile == "controlled" else 0, days * 130):
        state["roundNo"] = rno
        if damage_walls and rno >= 130 and rno % 130 == 0:
            victims = [r for r in roles if r["roleType"] == "wall" and point(r) in front][:2]
            for victim in victims:
                roles.remove(victim)
            destroyed_walls += len(victims)
        state["mapInfo"]["zones"] = [{"neutralType": kind, "pos": dict(zip(("x", "y"), p))}
                                      for kind, p in neutral_zones
                                      + [(k, p) for p, (k, _) in active.items()]]
        occupied = set(active) | neutral_cells | set().union(*(cells(r) for r in roles))
        started = monotonic()
        commands = agent.decide(copy.deepcopy(state))["roleCommandMap"]
        worst = max(worst, (monotonic()-started)*1000)
        by_id = {str(r["id"]): r for r in roles}
        if trace:
            history.append({"round": rno, "gold": state["teamOur"]["goldNum"],
                            "mines": [{"kind": k, "pos": list(p), "remaining": n}
                                      for p, (k, n) in active.items()],
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
                if profile == "controlled":
                    indices[kind] += 1
                    active[deposits[kind][indices[kind] % len(deposits[kind])]] = [kind, 10]
                else:
                    before = set(active)
                    respawn(kind, p)
                    new = next(iter(set(active) - before))
                    respawns.append({"available_round": rno + 1, "kind": kind,
                                     "old": list(p), "new": list(new)})
        state["lastRoundRoleActionResults"] = results
        if rno % 130 in (39, 40, 69, 70):
            guns = [r for r in roles if r["roleType"] in ("rocket", "gatling", "railgun")]
            heroes = [r for r in roles if r["roleType"] in ("worker", "pioneer")]
            crew = max((sum(near(point(h), point(w)) for h, w in zip(hs, guns))
                        for hs in permutations(heroes, len(guns))), default=0)
            checkpoints[str(rno)] = {"gold": state["teamOur"]["goldNum"], "income": income,
                "initial_gold": 75, "task_income": 0,
                "mine_counts": dict(Counter(k for k, _ in active.values())),
                "remaining_minerals": sum(n for _, n in active.values()),
                "inventory": dict(Counter(n for h in heroes for n in h["backpack"])),
                "spent": spent, "weapon_levels": sorted(r["level"] for r in guns), "operators_ready": crew,
                "walls": sum(r["roleType"] == "wall" for r in roles),
                "front_walls": sum(r["roleType"] == "wall" and point(r) in front for r in roles),
                "carried_vouchers": sum("UpgradeVoucher" in n for h in heroes for n in h["backpack"]),
                "carried_ore_value": sum(prices.get(n, 0) for h in heroes for n in h["backpack"])}
    return {"profile": profile, "seed": seed, "prices": prices,
            "checkpoint_timing": "after actions; round 69 is before first night",
            "initial_mines": initial_mines, "respawns": respawns,
            "limitations": ["no combat or opponent mining", "no task rewards or news price changes",
                            "sample geometry is not an observed opening", "building rings inferred from demo"],
            "case": case, "mirror": mirror, "days": days, "checkpoints": checkpoints,
            "early_worker_actions": dict(early_actions), "actions": dict(actions),
            "purchases": dict(purchases), "first": first, "invalid_actions": invalid,
            "worker_actions_before_70": dict(worker_actions),
            "mined_before_70": dict(mined_by_kind), "worker_reversals_before_70": reversals,
            "daily_worker_actions": {d: dict(c) for d, c in daily_worker_actions.items()},
            "destroyed_walls": destroyed_walls,
            **({"trace": history} if trace else {}), "worst_ms": round(worst, 2)}


def summarize(results):
    """Compare pre-night balances, not final balances after a combat-free night."""
    rows = []
    for result in results:
        dusk = result["checkpoints"]["69"]
        rows.append({"profile": result["profile"], "seed": result["seed"], "mirror": result["mirror"],
                     "gold": dusk["gold"], "income": dusk["income"], "spent": dusk["spent"],
                     "weapon_levels": dusk["weapon_levels"],
                     "upgraded_weapons": sum(level >= 2 for level in dusk["weapon_levels"]),
                     "operators_ready": dusk["operators_ready"], "walls": dusk["walls"],
                     "carried_ore_value": dusk["carried_ore_value"],
                     "invalid_actions": result["invalid_actions"]})
    return {"first_night": rows,
            "distribution": {key: {"min": min(row[key] for row in rows),
                                   "median": median(row[key] for row in rows),
                                   "max": max(row[key] for row in rows)}
                             for key in ("gold", "income", "upgraded_weapons")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--case", choices=("near", "far_shop", "remote_ore", "local_ore"), default="near")
    parser.add_argument("--profile", choices=("sample", "random", "controlled"), default="sample")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--seeds", type=int, nargs="+", help="Run reproducible seed matrix and report dusk distribution")
    parser.add_argument("--both-sides", action="store_true", help="Include mirrored geometry for each seed")
    parser.add_argument("--mines-per-kind", type=int, default=2, help="Random profile only; default matches sample count")
    parser.add_argument("--output", type=Path, help="Write JSON results including assumptions and checkpoints")
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
    if args.days < 1 or not 1 <= args.mines_per_kind <= 20:
        parser.error("days must be positive; mines-per-kind must be between 1 and 20")
    if args.profile != "controlled" and args.case != "near":
        parser.error("--case requires --profile controlled")
    if args.profile != "random" and args.mines_per_kind != 2:
        parser.error("--mines-per-kind requires --profile random")
    results = [simulate(Agent, Config, args.case, mirrored, args.days, args.trace,
                        args.damage_walls, profile=args.profile, seed=seed,
                        mines_per_kind=args.mines_per_kind)
               for seed in (args.seeds or [args.seed])
               for mirrored in ((False, True) if args.both_sides else (args.mirror,))]
    result = {**summarize(results), "runs": results} if args.seeds or args.both_sides else results[0]
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
