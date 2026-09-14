"""Controlled economy replay and static combat timing, NOT a match simulator.

Compare checkouts with --agent-root /path/to/checkout. Uses only stdlib and
does not contact the competition server or consume any LLM quota.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from time import monotonic


def unit(uid, kind, x, y, **kw):
    return {"id": uid, "roleType": kind, "pos": {"x": x, "y": y}, "health": 200,
            "backpack": [], "backPackCapability": 100, "attackRange": 10,
            "attackPower": 100, "level": 3, **kw}


def state(roles):
    return {"roundNo": 1, "mapInfo": {"width": 41, "height": 32, "zones": []},
            "teamOur": {"type": "challenger", "teamId": "benchmark", "goldNum": 0,
                        "roles": roles, "playerTasks": []},
            "teamEnemy": {"roles": []}, "robot": {"roles": []}, "phaseTask": "",
            "worldNews": {}, "llmResp": "", "lastCmdResult": "", "errors": []}


def economy(Agent, Config):
    """70 daytime rounds; controlled ore respawn at two alternating nearby cells.

    Only movement, collection and selling are applied. No news, opponents,
    upgrades, wall construction, robot waves or scoring are simulated.
    """
    hero = unit(1, "worker", 10, 5)
    data = state([hero, unit(13, "station", 2, 13),
                  unit(20, "gatling", 2, 10), unit(30, "railgun", 3, 10),
                  unit(40, "rocket", 4, 10)])
    mine = {"neutralType": "copper", "pos": {"x": 11, "y": 5}}
    data["mapInfo"]["zones"] = [mine, {"neutralType": "vendor", "pos": {"x": 2, "y": 5}}]
    data["vendorShopList"] = [{"name": "copper", "price": 5}]
    agent = Agent(Config(layout_mode="explicit", llm_enabled=False))
    actions = Counter()
    mined = 0
    for r in range(1, 71):
        data["roundNo"] = r
        response = agent.decide(data)
        cmd = response["roleCommandMap"].get("1", {})
        action = cmd.get("action", "idle")
        actions[action] += 1
        if action == "move":
            hero["pos"] = cmd["targetPos"][0].copy()
        elif action == "collect":
            hero["backpack"].append("copper")
            mined += 1
            if mined % 10 == 0:
                mine["pos"]["y"] = 6 if mine["pos"]["y"] == 5 else 5
        elif action == "sell":
            count = cmd["num"]
            data["teamOur"]["goldNum"] += count * 5
            for _ in range(count):
                hero["backpack"].remove("copper")
        elif action != "idle":
            raise AssertionError(f"Unsupported benchmark action: {cmd}")
    return {"rounds": 70, "gold": data["teamOur"]["goldNum"],
            "carried_copper": len(hero["backpack"]), "actions": dict(actions)}


def combat(Agent, Config):
    data = state([unit(1, "worker", 4, 26), unit(2, "worker", 6, 26),
                  unit(3, "pioneer", 8, 26), unit(13, "station", 4, 30),
                  unit(20, "gatling", 5, 25), unit(30, "railgun", 7, 25),
                  unit(40, "rocket", 9, 25)])
    data["robot"]["roles"] = [unit(100+i, "smallRobot", x, y, health=40,
                                        targetTeam="challenger")
                                for i, (x, y) in enumerate((x, y) for x in range(2, 39)
                                                           for y in range(2, 22))]
    agent = Agent(Config(llm_enabled=False))
    durations, counts = [], []
    for r in range(71, 91):
        data["roundNo"] = r  # Bypass duplicate-request caching.
        start = monotonic()
        response = agent.decide(data)
        durations.append((monotonic() - start) * 1000)
        counts.append(sum(c["action"] == "attack" for c in response["roleCommandMap"].values()))
    return {"static_robots": len(data["robot"]["roles"]), "samples": len(durations),
            "worst_ms": round(max(durations), 2), "min_attacking_towers": min(counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    sys.path.insert(0, str(args.agent_root / "SDK/SDK_Python/CoreGeek"))
    from agent.brain import Agent
    from agent.config import Config
    print(json.dumps({"scope": "controlled fixtures; not official engine or win rate",
                      "economy": economy(Agent, Config), "combat": combat(Agent, Config)},
                     indent=2))


if __name__ == "__main__":
    main()
