"""Rocket handoff, firing cadence, night income and threat recall."""
import copy
import unittest
from unittest.mock import patch

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.combat import defend
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.economy import finish_preparation, worker
from agent.model import Turn, distance
from agent.navigation import layout, DeadlineExceeded
from agent.economy_plan import preparation_start
from agent.night import shared_crew, crew_pairs, day_return, night_plan, night_earn, safe_trip


def night_case(rno=90, mirror=False, inventory=()):
    width, height = 41, 32

    def flip(p):
        return (width - 1 - p[0], height - 1 - p[1]) if mirror else p

    def make(uid, kind, p, **kwargs):
        p = flip(p)
        if mirror and kind == "station":
            p = p[0] - 1, p[1] + 1
        return unit(uid, kind, *p, **kwargs)

    p = payload(rno, [make(13, "station", (9, 22), health=1500),
                      make(10, "worker", (10, 20), health=220, backpack=list(inventory)),
                      make(11, "pioneer", (11, 21), health=200),
                      make(12, "worker", (10, 23), health=220)])
    p["mapInfo"].update(width=width, height=height)
    t = Turn(p, Config())
    guns, walls = layout(t, Config())
    p["teamOur"]["roles"] += [unit(20+i, "rocket", *point, attackRange=10) for i, point in enumerate(guns)]
    p["teamOur"]["roles"] += [unit(100+i, "wall", *point, health=1000) for i, point in enumerate(walls)]
    p["mapInfo"]["zones"] = [{"neutralType": kind, "pos": dict(zip(("x", "y"), flip(point)))}
                              for kind, point in (("copper", (6, 18)), ("vendor", (6, 17)))]
    p["vendorShopList"] = [{"name": "copper", "price": 5}]
    p["weaponShopList"] = [{"name": "WeaponUpgradeVoucher1", "price": 100}]
    return p


def apply_turn(p, response):
    """Apply worker travel/inventory and explicit three-round cooldown windows."""
    roles = {r["id"]: r for r in p["teamOur"]["roles"]}
    results = {}
    for uid, c in response["roleCommandMap"].items():
        actor = roles[int(uid)]
        action = c["action"]
        if action == "move":
            actor["pos"] = c["targetPos"][0].copy()
        elif action == "collect":
            actor["backpack"].append("copper")
        elif action == "sell":
            for _ in range(c["num"]):
                actor["backpack"].remove(c["name"])
            p["teamOur"]["goldNum"] += 5 * c["num"]
        results[uid] = True
    for uid, actor in roles.items():
        if actor["roleType"] == "rocket":
            actor["cooldown"] = (3 if response["roleCommandMap"].get(str(uid), {}).get("action") == "attack"
                                 else max(0, actor.get("cooldown", 0) - 1))
    p["lastRoundRoleActionResults"] = results


class NightEconomyTests(unittest.TestCase):
    def test_default_and_mirrored_clear_night_preserves_both_worker_mining(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                agent = Agent(Config(llm_enabled=False))
                result = agent.decide(night_case(mirror=mirror))
                mem = next(iter(agent.sessions.values()))
                self.assertEqual((mem.night_mode, mem.night_miner), ("shared", 10))
                self.assertEqual(result["roleCommandMap"]["10"]["action"], "move")
                self.assertNotIn("11", result["roleCommandMap"])
                self.assertEqual(result["roleCommandMap"]["12"]["action"], "move")
                self.assertFalse(any(c["action"] in ("build", "buy") for c in result["roleCommandMap"].values()))

    def test_three_separate_day_posts_prepare_shared_operator_even_with_task(self):
        p = night_case(rno=50)
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        crew = shared_crew(t, cfg, mem, nav, ledger, layout(t, cfg)[1])
        pairs, posts = day_return(t, mem, nav, ledger, crew, excluded={11})
        self.assertEqual({h.id for h, _ in pairs}, {10, 12})
        self.assertEqual(posts[10][0], (10, 20))
        self.assertEqual(crew["post"], (11, 21))
        full_pairs, full_posts = day_return(t, mem, nav, ledger, crew)
        self.assertEqual(len(set(p[0] for p in full_posts.values())), 3)
        self.assertEqual(len(full_pairs), 3)

    def test_nearby_shared_crew_can_finish_local_flank_before_return(self):
        p = night_case(rno=65, inventory=["stone"])
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"] if r["pos"] != {"x": 10, "y": 19}]
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        walls = layout(t, cfg)[1]
        crew = shared_crew(t, cfg, mem, nav, ledger, walls)
        pairs, posts = day_return(t, mem, nav, ledger, crew)
        ledger.return_pairs = pairs
        ledger.operator_posts = {uid: p for uid, (p, _) in posts.items()}
        hero, tower = next((h, w) for h, w in pairs if h.id == 10)
        self.assertTrue(finish_preparation(t, cfg, mem, nav, ledger, hero, tower, walls))
        self.assertEqual(ledger.commands["10"], command("build", (10, 19), name="wall"))

    def test_day_handoff_yields_when_operators_occupy_each_others_routes(self):
        p = night_case(rno=60)
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(day=1)
        crew = shared_crew(t, cfg, mem, nav, ledger, layout(t, cfg)[1])
        pairs, posts = crew_pairs(t, crew)
        mem.return_targets = {h.id: w.id for h, w in pairs}
        mem.return_posts = posts.copy()
        for role in p["teamOur"]["roles"]:
            if role["id"] == 11:
                role["pos"] = dict(zip(("x", "y"), crew["solo_post"]))
            elif role["id"] == crew["operator"]:
                role["pos"] = dict(zip(("x", "y"), crew["miner_post"]))
            elif role["id"] == crew["miner"]:
                role["pos"] = {"x": 6, "y": 18}
        turn = Turn(p, cfg)
        agent = Agent(cfg)
        agent.sessions[(*turn.key, turn.station.pos)] = mem
        result = agent.decide(p)
        self.assertEqual(result["roleCommandMap"].get("11", {}).get("action"), "move")

    def test_courier_stones_do_not_stop_builder_from_collecting_breach_material(self):
        p = night_case(rno=170)
        p["teamOur"]["roles"][1]["pos"] = {"x": 6, "y": 19}
        p["teamOur"]["roles"][3]["backpack"] = ["stone"] * 10 + ["WeaponUpgradeVoucher2"] * 3
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"]
                                  if r["pos"] not in ({"x": 12, "y": 21}, {"x": 12, "y": 22})]
        p["mapInfo"]["zones"][0]["neutralType"] = "stone"
        p["mapInfo"]["zones"].append({"neutralType": "copper", "pos": {"x": 5, "y": 20}})
        p["vendorShopList"].append({"name": "stone", "price": 1})
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(supply_worker=12, wall_hits={(12, 21): 1, (12, 22): 1})
        guns, walls = layout(t, cfg)
        shared_crew(t, cfg, mem, nav, ledger, walls)
        worker(t, cfg, mem, nav, ledger, t.workers[0], guns, walls, True, shopping=False)
        self.assertEqual(ledger.commands["10"], command("collect", (6, 18)))

    def test_shared_guns_keep_four_round_firing_cadence(self):
        p = night_case()
        p["robot"]["roles"] = [unit(900, "largeRobot", 18, 22, health=500, targetTeam="challenger")]
        mem = Memory()
        fired = {20: [], 21: [], 22: []}
        for rno in range(90, 102):
            p["roundNo"] = rno
            t, cfg, nav, ledger = setup_case(p)
            crew = shared_crew(t, cfg, mem, nav, ledger, layout(t, cfg)[1])
            pairs, posts = crew_pairs(t, crew, shared=True)
            defend(t, nav, ledger, pairs, posts)
            attacks = [(int(uid), c) for uid, c in ledger.commands.items() if c["action"] == "attack"]
            self.assertLessEqual(sum(c["controllerId"] == "11" for _, c in attacks), 1)
            for uid, _ in attacks:
                fired[uid].append(rno)
            apply_turn(p, ledger.response())
        self.assertTrue(all(len(rounds) == 3 for rounds in fired.values()), fired)
        self.assertTrue(all(b - a == 4 for rounds in fired.values() for a, b in zip(rounds, rounds[1:])), fired)

    def test_two_operators_fire_while_third_worker_collects_and_sells(self):
        p = payload(90, [unit(13, "station", 3, 9, health=1500),
                         unit(10, "worker", 4, 4, health=220),
                         unit(11, "pioneer", 4, 6, health=200),
                         unit(12, "worker", 7, 6, health=220),
                         unit(20, "rocket", 5, 5, attackRange=10),
                         unit(21, "rocket", 5, 7, attackRange=10),
                         unit(22, "rocket", 7, 7, attackRange=10)])
        p["mapInfo"]["zones"] = [{"neutralType": k, "pos": {"x": x, "y": y}}
                                  for k, x, y in (("copper", 3, 4), ("vendor", 3, 3))]
        p["vendorShopList"] = [{"name": "copper", "price": 5}]
        p["robot"]["roles"] = [unit(900, "largeRobot", 14, 7, health=500,
                                      attackRange=3, targetTeam="challenger")]
        agent = Agent(Config(layout_mode="explicit", llm_enabled=False))
        counts = {20: 0, 21: 0, 22: 0}
        collected, sold = 0, 0
        for rno in range(90, 102):
            p["roundNo"] = rno
            result = agent.decide(copy.deepcopy(p))
            for uid, c in result["roleCommandMap"].items():
                if c["action"] == "attack":
                    self.assertNotEqual(c["controllerId"], "10")
                    counts[int(uid)] += 1
                elif uid == "10" and c["action"] == "collect":
                    collected += 1
                elif uid == "10" and c["action"] == "sell":
                    sold += c["num"]
            apply_turn(p, result)
        self.assertEqual(counts, {20: 3, 21: 3, 22: 3})
        self.assertGreaterEqual(collected, 8)
        self.assertGreaterEqual(sold, 5)

    def test_income_budget_failure_preserves_full_combat_response(self):
        p = night_case()
        p["robot"]["roles"] = [unit(900, "largeRobot", 19, 22, health=500,
                                      attackRange=3, targetTeam="challenger")]
        agent = Agent(Config(llm_enabled=False))
        with patch("agent.brain.night_earn", side_effect=DeadlineExceeded):
            result = agent.decide(p)
        self.assertEqual(next(iter(agent.sessions.values())).night_reason, "income_budget")
        self.assertEqual(sum(c["action"] == "attack" for c in result["roleCommandMap"].values()), 3)

    def test_affordable_base_delivery_is_included_in_preparation_budget(self):
        p = night_case(rno=1)
        p["mapInfo"]["zones"].append({"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}})
        p["weaponShopList"] += [{"name": "StationUpgradeVoucher1", "price": 100},
                                 {"name": "StationUpgradeVoucher2", "price": 150}]
        cutoffs = []
        for gold in (0, 1000):
            p["teamOur"]["goldNum"] = gold
            t, cfg, nav, ledger = setup_case(p)
            mem = Memory()
            shared_crew(t, cfg, mem, nav, ledger, layout(t, cfg)[1])
            cutoffs.append(preparation_start(t, cfg, mem, nav, t.workers, [w.pos for w in t.weapons]))
        self.assertEqual(cutoffs[0] - cutoffs[1], 4)

    def test_near_wave_restores_three_shots_and_recalls_miner(self):
        p = night_case()
        p["robot"]["roles"] = [unit(900, "largeRobot", 13, 22, health=500, attackRange=3, targetTeam="challenger")]
        agent = Agent(Config(llm_enabled=False))
        result = agent.decide(p)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual((mem.night_mode, mem.night_reason), ("full", "threat_recall"))
        attacks = [c for c in result["roleCommandMap"].values() if c["action"] == "attack"]
        self.assertEqual(len(attacks), 3)
        self.assertEqual(len({c["controllerId"] for c in attacks}), 3)

    def test_outbound_miner_returns_before_near_wave(self):
        p = night_case()
        agent = Agent(Config(llm_enabled=False))
        result = agent.decide(p)
        apply_turn(p, result)
        before = (p["teamOur"]["roles"][1]["pos"]["x"], p["teamOur"]["roles"][1]["pos"]["y"])
        p["roundNo"] += 1
        p["robot"]["roles"] = [unit(900, "largeRobot", 13, 22, health=500, attackRange=3, targetTeam="challenger")]
        result = agent.decide(p)
        move = result["roleCommandMap"]["10"]
        self.assertEqual(move["action"], "move")
        target = tuple(move["targetPos"][0][k] for k in ("x", "y"))
        self.assertLess(distance(target, (10, 20)), distance(before, (10, 20)))

    def test_night_income_continues_into_dawn(self):
        p = night_case(rno=122)
        agent = Agent(Config(llm_enabled=False))
        collected = 0
        for rno in range(122, 132):
            p["roundNo"] = rno
            result = agent.decide(copy.deepcopy(p))
            collected += int(result["roleCommandMap"].get("10", {}).get("action") == "collect")
            apply_turn(p, result)
        self.assertGreaterEqual(collected, 4)
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.night_alarm_until, 0)

    def test_full_load_sells_at_night(self):
        p = night_case(inventory=["copper"] * 100)
        agent = Agent(Config(llm_enabled=False))
        sold = None
        for rno in range(90, 103):
            p["roundNo"] = rno
            result = agent.decide(copy.deepcopy(p))
            c = result["roleCommandMap"].get("10", {})
            if c.get("action") == "sell":
                sold = c
                break
            apply_turn(p, result)
        self.assertEqual(sold, command("sell", name="copper", num=100))

    def test_base_damage_or_wall_breach_holds_recall_even_after_clear(self):
        for damage in ("base", "wall"):
            with self.subTest(damage=damage):
                p = night_case()
                agent = Agent(Config(llm_enabled=False))
                apply_turn(p, agent.decide(p))
                p["roundNo"] += 1
                if damage == "base":
                    p["teamOur"]["roles"][0]["health"] -= 5
                else:
                    p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"] if r["id"] != 100]
                result = agent.decide(p)
                mem = next(iter(agent.sessions.values()))
                self.assertEqual(mem.night_mode, "full")
                self.assertGreater(mem.night_alarm_until, p["roundNo"])
                self.assertNotEqual(result["roleCommandMap"].get("10", {}).get("action"), "collect")

    def test_busy_injured_missing_or_nonrocket_crew_does_not_release_miner(self):
        for case in ("busy", "injured", "missing", "mixed", "disabled"):
            with self.subTest(case=case):
                p = night_case(rno=220)
                if case == "busy":
                    p["phaseTask"] = "继续求解任务"
                elif case == "injured":
                    p["teamOur"]["roles"][2]["health"] = 150
                elif case == "missing":
                    p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"] if r["id"] != 11]
                elif case == "mixed":
                    next(r for r in p["teamOur"]["roles"] if r["id"] == 20)["roleType"] = "gatling"
                agent = Agent(Config(llm_enabled=False, night_economy_enabled=case != "disabled"))
                result = agent.decide(p)
                mem = next(iter(agent.sessions.values()))
                self.assertIsNone(mem.night_miner)
                self.assertFalse(any(c["action"] == "collect" for c in result["roleCommandMap"].values()))

    def test_trip_checks_prospective_collect_cell_and_stationary_guards(self):
        p = night_case()
        p["robot"]["roles"] = [unit(900, "largeRobot", 24, 22, health=500, attackRange=3, targetTeam="challenger")]
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        hero = next(h for h in t.workers if h.id == 10)
        self.assertTrue(safe_trip(t, cfg, mem, nav, hero, (10, 20)))
        self.assertFalse(safe_trip(t, cfg, mem, nav, hero, (10, 20), 12, (1, 1)))
        # A guard in the only entrance cannot be removed from the return search.
        guard = next(r for r in p["teamOur"]["roles"] if r["id"] == 11)
        guard["pos"] = {"x": 10, "y": 20}
        miner = next(r for r in p["teamOur"]["roles"] if r["id"] == 10)
        miner["pos"] = {"x": 11, "y": 21}
        t, cfg, nav, ledger = setup_case(p)
        self.assertFalse(safe_trip(t, cfg, mem, nav, t.workers[0], (8, 21)))

    def test_unfinished_handoff_and_no_safe_income_keep_full_crew(self):
        p = night_case()
        p["teamOur"]["roles"][2]["pos"] = {"x": 9, "y": 20}
        agent = Agent(Config(llm_enabled=False))
        agent.decide(p)
        self.assertEqual(next(iter(agent.sessions.values())).night_reason, "handoff_not_ready")
        p = night_case()
        p["vendorShopList"] = []
        agent = Agent(Config(llm_enabled=False))
        agent.decide(p)
        self.assertEqual(next(iter(agent.sessions.values())).night_mode, "full")


if __name__ == "__main__":
    unittest.main()
