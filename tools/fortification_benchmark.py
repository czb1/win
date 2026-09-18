"""Controlled first-day construction replay, not the official game engine.

Models movement occupancy, one-stone collection, ten-use deposits and building.
No robot combat, task rewards, upgrades, prices or survival/win-rate simulation.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from time import monotonic


def role(uid, kind, point, **kw):
    return {"id": uid, "roleType": kind, "pos": {"x": point[0], "y": point[1]},
            "health": 200, "level": 1, "backpack": [], "backPackCapability": 100,
            "attackRange": 8, "attackPower": 40, "cooldown": 0, **kw}


def simulate_day(agent_class, config_class, mirrored=False):
    cfg = config_class(llm_enabled=False)
    agent = agent_class(cfg)
    # Deposits respawn in a fixed cycle outside both versions' building rings.
    deposits = [(10, 12), (10, 17), (11, 12), (11, 17)]
    active = {deposits[0]: 10, deposits[1]: 10}
    next_deposit = 2
    p = {"roundNo": 1, "mapInfo": {"width": 24, "height": 20, "zones": []},
         "teamOur": {"type": "challenger", "teamId": "construction-replay", "goldNum": 75,
                     "roles": [role(13, "station", (5, 15)), role(10, "worker", (4, 14)),
                               role(12, "worker", (4, 12)), role(11, "pioneer", (7, 14))],
                     "playerTasks": []},
         "teamEnemy": {"roles": []}, "robot": {"roles": []}, "phaseTask": "",
         "worldNews": {}, "llmResp": "", "lastCmdResult": "", "errors": []}
    def mirror(point):
        return (23 - point[0], 19 - point[1]) if mirrored else point

    if mirrored:
        deposits = list(map(mirror, deposits))
        active = {mirror(point): count for point, count in active.items()}
        for r in p["teamOur"]["roles"]:
            x, y = mirror((r["pos"]["x"], r["pos"]["y"]))
            if r["roleType"] == "station":
                x, y = x - 1, y + 1
            r["pos"] = {"x": x, "y": y}
        p["teamOur"]["type"] = "defender"
    front_x = 8
    front = {mirror((front_x, y)) for y in range(12, 18)}
    # Independent expected geometry, with rear access kept open.
    expected = front | {mirror((x, y)) for x in range(5, front_x) for y in (12, 17)}
    disconnected_builds = 0
    front_complete_round = None
    first_wall = None
    walls_by_round, actions, invalid, worst = {}, Counter(), 0, 0.0
    builders = Counter()
    for round_no in range(1, 71):
        p["roundNo"] = round_no
        p["mapInfo"]["zones"] = [{"neutralType": "stone", "pos": {"x": x, "y": y}} for x, y in active]
        roles = p["teamOur"]["roles"]
        by_id = {str(r["id"]): r for r in roles}
        occupied = set(active)
        for r in roles:
            x, y = r["pos"]["x"], r["pos"]["y"]
            occupied.add((x, y))
            if r["roleType"] == "station":
                occupied.update({(x + 1, y), (x, y - 1), (x + 1, y - 1)})
        started = monotonic()
        commands = agent.decide(p)["roleCommandMap"]
        worst = max(worst, monotonic() - started)
        destinations = Counter(tuple(c["targetPos"][0][k] for k in ("x", "y"))
                               for c in commands.values() if c["action"] in ("move", "build"))
        results, collected = {}, Counter()
        for uid, cmd in commands.items():
            r = by_id[uid]
            action = cmd["action"]
            actions[action] += 1
            target = cmd.get("targetPos", [{}])[0]
            target = (target.get("x"), target.get("y"))
            origin = (r["pos"]["x"], r["pos"]["y"])
            adjacent = target != origin and None not in target and max(
                abs(target[0] - origin[0]), abs(target[1] - origin[1])) <= 1
            legal = False
            if action == "move":
                legal = (adjacent and 0 <= target[0] < 24 and 0 <= target[1] < 20
                         and target not in occupied and destinations[target] == 1)
                if legal:
                    r["pos"] = {"x": target[0], "y": target[1]}
            elif action == "collect":
                legal = adjacent and target in active and len(r["backpack"]) < r["backPackCapability"]
                if legal:
                    r["backpack"].append("stone")
                    collected[target] += 1
            elif action == "build":
                name = cmd["name"]
                x, y = mirror(target)
                # Explicitly model the demo-inferred ring, independent of layout().
                base_distance = max(max(5 - x, 0, x - 6), max(14 - y, 0, y - 15))
                in_build_area = base_distance == (2 if name == "wall" else 1)
                legal = (adjacent and target not in occupied and destinations[target] == 1
                         and r["roleType"] == "worker"
                         and in_build_area)
                if name == "wall":
                    legal = legal and r["backpack"].count("stone") >= cfg.wall_stones
                    if legal:
                        for _ in range(cfg.wall_stones):
                            r["backpack"].remove("stone")
                        existing = {(w["pos"]["x"], w["pos"]["y"]) for w in roles if w["roleType"] == "wall"}
                        if existing and not any(abs(target[0]-q[0]) + abs(target[1]-q[1]) == 1 for q in existing):
                            disconnected_builds += 1
                        builders[uid] += 1
                        if first_wall is None:
                            first_wall = target
                else:
                    legal = (legal and name in ("gatling", "railgun", "rocket")
                             and p["teamOur"]["goldNum"] >= cfg.weapon_cost
                             and sum(x["roleType"] in ("gatling", "railgun", "rocket") for x in roles) < 3)
                    if legal:
                        p["teamOur"]["goldNum"] -= cfg.weapon_cost
                if legal:
                    roles.append(role(1000 + len(roles), name, target))
            if not legal:
                invalid += 1
            results[uid] = legal
        for point, amount in collected.items():
            active[point] -= amount
            if active[point] <= 0:
                del active[point]
                while deposits[next_deposit % len(deposits)] in active:
                    next_deposit += 1
                active[deposits[next_deposit % len(deposits)]] = 10
                next_deposit += 1
        p["lastRoundRoleActionResults"] = results
        walls_by_round[round_no] = sum(r["roleType"] == "wall" for r in roles)
        built = {(r["pos"]["x"], r["pos"]["y"]) for r in roles if r["roleType"] == "wall"}
        if front <= built and front_complete_round is None:
            front_complete_round = round_no
    return {"walls_day1": walls_by_round[70], "disconnected_builds": disconnected_builds,
            "first_wall_position": first_wall, "front_complete_round": front_complete_round,
            "front_missing": sorted(front - built), "blueprint_missing": sorted(expected - built),
            "first_wall_round": next((r for r, n in walls_by_round.items() if n), None),
            "walls_round_30": walls_by_round[30], "walls_round_50": walls_by_round[50],
            "walls_per_worker": dict(builders), "invalid_actions": invalid,
            "actions": dict(actions), "worst_decision_ms": round(worst * 1000, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mirror", action="store_true", help="Replay the right-side base with left-facing defence")
    args = parser.parse_args()
    sys.path.insert(0, str(args.agent_root / "SDK/SDK_Python/CoreGeek"))
    from agent.brain import Agent
    from agent.config import Config
    print(json.dumps(simulate_day(Agent, Config, args.mirror), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
