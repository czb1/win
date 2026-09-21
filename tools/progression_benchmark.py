"""Controlled mine/sell/buy/use replay; no combat, real LLM or scoring engine."""
import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import sys
from time import monotonic


def role(uid, kind, x, y, **kw):
    return {"id": uid, "roleType": kind, "pos": {"x": x, "y": y}, "health": 1000,
            "level": 1, "backpack": [], "backPackCapability": 100,
            "attackRange": 8, "attackPower": 40, "cooldown": 0, **kw}


def simulate(agent_class, config_class, days=5):
    wall_sites = [[6, y] for y in range(8, 14)]
    cfg = config_class(layout_mode="explicit", weapon_cells=[], wall_cells=wall_sites, llm_enabled=False)
    agent = agent_class(cfg)
    roles = [role(1, "worker", 8, 7, health=100), role(2, "worker", 8, 8, health=220),
             role(13, "station", 3, 11, health=1500)]
    roles += [role(20+i, "rocket", 5, y) for i, y in enumerate((9, 11, 13))]
    shop = {"Medicine": 10, "WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150,
            "StationUpgradeVoucher1": 100, "StationUpgradeVoucher2": 150,
            "WallUpgradeVoucher1": 20, "WallUpgradeVoucher2": 30, "WallFixer": 10}
    zones = [{"neutralType": kind, "pos": {"x": x, "y": y}} for kind, x, y in
             (("weaponShop", 10, 5), ("vendor", 9, 7), ("copper", 11, 7), ("stone", 8, 10))]
    data = {"roundNo": 1, "mapInfo": {"width": 15, "height": 15, "zones": zones},
            "teamOur": {"type": "challenger", "teamId": "progression", "goldNum": 0,
                        "roles": roles, "playerTasks": []},
            "teamEnemy": {"roles": []}, "robot": {"roles": []}, "phaseTask": "",
            "worldNews": {}, "llmResp": "", "lastCmdResult": "", "errors": [],
            "weaponShopList": [{"name": k, "price": v} for k, v in shop.items()],
            "vendorShopList": [{"name": "copper", "price": 10}, {"name": "stone", "price": 1}]}
    actions, purchases, upgrades = Counter(), Counter(), Counter()
    first, invalid, worst = {}, 0, 0
    stock = []

    def point(r):
        return r["pos"]["x"], r["pos"]["y"]

    def near(a, b):
        return a != b and max(abs(a[0]-b[0]), abs(a[1]-b[1])) <= 1

    def cells(r):
        x, y = point(r)
        return {(x, y), (x+1, y), (x, y-1), (x+1, y-1)} if r["roleType"] == "station" else {(x, y)}

    for rno in range(1, 130 * days + 1):
        data["roundNo"] = rno
        before = copy.deepcopy(roles)
        by_id = {str(r["id"]): r for r in roles}
        occupied = set().union(*(cells(r) for r in before), {point(z) for z in zones})
        started = monotonic()
        commands = agent.decide(data)["roleCommandMap"]
        worst = max(worst, (monotonic() - started) * 1000)
        results = {}
        for uid, cmd in commands.items():
            actor, action = by_id[uid], cmd["action"]
            actions[action] += 1
            target = tuple(cmd["targetPos"][0][k] for k in ("x", "y")) if cmd.get("targetPos") else None
            name = cmd.get("name")
            legal = False
            if action == "move":
                legal = near(point(actor), target) and target not in occupied and all(0 <= x < 15 for x in target)
                if legal:
                    actor["pos"] = dict(zip(("x", "y"), target))
                    occupied.add(target)
            elif action == "collect":
                deposit = next((z["neutralType"] for z in zones if point(z) == target), None)
                legal = (near(point(actor), target) and deposit in ("stone", "copper")
                         and actor["roleType"] == "worker" and len(actor["backpack"]) < actor["backPackCapability"])
                if legal:
                    actor["backpack"].append(deposit)
            elif action in ("sell", "buy"):
                zone = "vendor" if action == "sell" else "weaponShop"
                num = cmd.get("num", 1)
                legal = any(z["neutralType"] == zone and near(point(actor), point(z)) for z in zones)
                if action == "sell":
                    legal &= name in ("stone", "copper") and actor["backpack"].count(name) >= num
                    if legal:
                        for _ in range(num):
                            actor["backpack"].remove(name)
                        data["teamOur"]["goldNum"] += num * (10 if name == "copper" else 1)
                else:
                    legal &= (name in shop and shop[name] * num <= data["teamOur"]["goldNum"]
                              and len(actor["backpack"]) + num <= actor["backPackCapability"])
                    if legal:
                        data["teamOur"]["goldNum"] -= shop[name] * num
                        actor["backpack"] += [name] * num
                        purchases[name] += num
                        first.setdefault(name, rno)
            elif action == "build":
                legal = ((rno - 1) % 130 < 70 and name == "wall" and target not in occupied
                         and list(target) in wall_sites and "stone" in actor["backpack"] and near(point(actor), target))
                if legal:
                    actor["backpack"].remove("stone")
                    roles.append(role(1000+len(roles), "wall", *target))
                    occupied.add(target)
            elif action == "use":
                legal = name in actor["backpack"]
                if name == "Medicine":
                    if legal:
                        actor["health"] = 220
                elif "UpgradeVoucher" in str(name):
                    building = next((r for r in roles if point(r) == target), None)
                    kinds = ("rocket",) if name.startswith("Weapon") else ("station",) if name.startswith("Station") else ("wall",)
                    legal &= bool(building and building["roleType"] in kinds and building["level"] < 3
                                  and name.endswith(str(building["level"])) and any(near(point(actor), p) for p in cells(building)))
                    if legal:
                        building["level"] += 1
                        upgrades[name] += 1
                        first.setdefault("used_" + name, rno)
                else:
                    legal = False
                if legal:
                    actor["backpack"].remove(name)
            if not legal:
                invalid += 1
            results[uid] = bool(legal)
        walls = [r for r in roles if r["roleType"] == "wall"]
        if len(walls) == len(wall_sites):
            first.setdefault("walls_complete", rno)
            if all(r["level"] == 3 for r in walls):
                first.setdefault("walls_level3", rno)
        if all(r["level"] == 3 for r in roles if r["roleType"] == "rocket"):
            first.setdefault("guns_level3", rno)
        if (rno - 69) % 130 == 0:
            heroes = [r for r in roles if r["roleType"] in ("worker", "pioneer")]
            stock.append(sum(h["backpack"].count("WallFixer") for h in heroes))
        data["lastRoundRoleActionResults"] = results
    return {"scope": "controlled infinite deposits and fixed prices; no battle or win rate",
            "night_stock": stock, "days": days, "gold": data["teamOur"]["goldNum"], "invalid_actions": invalid,
            "weapon_levels": [r["level"] for r in roles if r["roleType"] == "rocket"],
            "station_level": next(r["level"] for r in roles if r["roleType"] == "station"),
            "walls": sum(r["roleType"] == "wall" for r in roles), "first_rounds": first,
            "purchases": dict(purchases), "upgrades": dict(upgrades), "actions": dict(actions),
            "worst_ms": round(worst, 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--days", type=int, default=5)
    args = parser.parse_args()
    sys.path.insert(0, str(args.agent_root / "SDK/SDK_Python/CoreGeek"))
    from agent.brain import Agent
    from agent.config import Config
    print(json.dumps(simulate(Agent, Config, args.days), ensure_ascii=False, indent=2))
