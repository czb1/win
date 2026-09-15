"""Actual damage footprints, shopping delivery and weak-model task regressions."""
from pathlib import Path
import shlex
import runpy
import subprocess
import sys
import tempfile
import unittest

from test_agent import payload, unit, setup_case, ROOT
from test_evolution import task_payload
from agent.brain import Agent
from agent.commands import command
from agent.combat import select_targets, emergency_items
from agent.config import Config
from agent.economy import worker, use_inventory
from agent.intelligence import Memory, Intelligence, parse_task_reply
from agent.model import Turn, distance
from agent.task_tools import file_code, document_path

sys.path.insert(0, str(ROOT / "tools"))
from progression_benchmark import simulate


class WaveOwnershipTests(unittest.TestCase):
    def case(self, kind, team="challenger", level=1):
        p = payload(71, roles=[unit(1, "worker", 4, 6), unit(20, kind, 5, 5, level=level)])
        p["teamOur"]["type"] = team
        other = "defender" if team == "challenger" else "challenger"
        p["robot"]["roles"] = [unit(31, "smallRobot", 8, 5, targetTeam=other)]
        return p

    def test_never_spend_cooldown_on_opponent_wave(self):
        for team in ("challenger", "defender"):
            for kind in ("gatling", "railgun", "rocket"):
                for level in (1, 2, 3):
                    with self.subTest(team=team, kind=kind, level=level):
                        p = self.case(kind, team, level)
                        self.assertFalse(Agent().decide(p)["roleCommandMap"])

    def test_both_routes_in_range_fire_only_on_our_wave(self):
        for team in ("challenger", "defender"):
            for kind in ("gatling", "railgun", "rocket"):
                p = self.case(kind, team)
                p["robot"]["roles"].append(unit(32, "largeRobot", 8, 9, targetTeam=team))
                t, _, nav, _ = setup_case(p)
                damage = {}
                self.assertTrue(select_targets(t, t.weapons[0], damage, nav.deadline))
                self.assertGreater(damage[32], 0)
                self.assertFalse(damage.get(31, 0))

    def test_direct_fire_does_not_hit_protected_blocker(self):
        for kind in ("gatling", "railgun"):
            p = self.case(kind)
            p["robot"]["roles"].append(unit(32, "largeRobot", 9, 5, targetTeam="challenger"))
            t, _, nav, _ = setup_case(p)
            self.assertEqual(select_targets(t, t.weapons[0], {}, nav.deadline), [])

    def test_rocket_uses_safe_splash_edge_when_waves_touch(self):
        p = self.case("rocket", level=3)
        p["robot"]["roles"].append(unit(32, "largeRobot", 9, 5, targetTeam="challenger"))
        t, _, nav, _ = setup_case(p)
        damage = {}
        shots = select_targets(t, t.weapons[0], damage, nav.deadline)
        self.assertEqual(len(shots), 3)
        self.assertTrue(all(distance(p, (8, 5)) > 1 for p in shots))
        self.assertGreater(damage[32], 0)

    def test_missing_team_uses_nearest_base_not_spawn_corner(self):
        p = payload(71, roles=[unit(13, "station", 1, 12), unit(20, "rocket", 4, 8)])
        p["teamEnemy"]["roles"] = [unit(99, "station", 11, 2)]
        p["robot"]["roles"] = [unit(31, "smallRobot", 10, 3)]
        t, _, nav, _ = setup_case(p)
        self.assertFalse(select_targets(t, t.weapons[0], {}, nav.deadline))
        p["robot"]["roles"][0]["targetTeam"] = "challenger"
        t, _, nav, _ = setup_case(p)
        self.assertTrue(select_targets(t, t.weapons[0], {}, nav.deadline))

    def test_bomb_and_stun_do_not_help_other_team(self):
        for name in ("Bomb", "DizzyWeapon"):
            p = payload(71, roles=[unit(1, "worker", 4, 6, backpack=[name]), unit(13, "station", 2, 7)])
            p["robot"]["roles"] = [unit(31+i, "largeRobot", 6, 6+i, targetTeam="defender") for i in range(2)]
            t, _, _, ledger = setup_case(p)
            emergency_items(t, ledger)
            self.assertFalse(ledger.commands)


class SuppliesTests(unittest.TestCase):
    def test_legacy_sdk_entry_uses_actual_agent(self):
        entry = runpy.run_path(str(ROOT / "SDK" / "main3.py"), run_name="_sdk_test")
        response = entry["callback"](payload())
        self.assertEqual(set(response), {"roleCommandMap", "prompt", "executeCmd"})
        self.assertTrue(response["roleCommandMap"])

    def case(self, **kw):
        p = payload(roles=[unit(1, "worker", 5, 5, backpack=["Medicine"], **kw),
                           unit(20, "rocket", 4, 5)])
        p["teamOur"]["goldNum"] = 200
        p["mapInfo"]["zones"] = [{"neutralType": "weaponShop", "pos": {"x": 6, "y": 5}},
                                  {"neutralType": "vendor", "pos": {"x": 5, "y": 6}},
                                  {"neutralType": "stone", "pos": {"x": 6, "y": 6}}]
        p["weaponShopList"] = [{"name": "WeaponUpgradeVoucher1", "price": 100},
                               {"name": "Medicine", "price": 10}]
        p["vendorShopList"] = [{"name": "stone", "price": 1}, {"name": "copper", "price": 5}]
        return p

    def decide_worker(self, p, mem=None):
        t, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[], wall_cells=[[7, 7]])
        worker(t, cfg, mem or Memory(), nav, ledger, t.workers[0], [], [(7, 7)], True)
        return ledger.commands.get("1", {})

    def test_upgrade_is_not_starved_by_unfinished_walls(self):
        self.assertEqual(self.decide_worker(self.case()), {"action": "buy", "name": "WeaponUpgradeVoucher1", "num": 1})

    def test_full_backpack_sells_before_buying(self):
        p = self.case(backPackCapability=3)
        p["teamOur"]["roles"][0]["backpack"] = ["Medicine", "copper", "copper"]
        self.assertEqual(self.decide_worker(p), {"action": "sell", "name": "copper", "num": 2})

    def test_wounded_worker_buys_then_uses_medicine(self):
        p = self.case(health=80)
        p["teamOur"]["roles"][0]["backpack"] = []
        self.assertEqual(self.decide_worker(p)["name"], "Medicine")
        p["teamOur"]["roles"][0]["backpack"] = ["Medicine"]
        self.assertEqual(self.decide_worker(p), {"action": "use", "name": "Medicine"})

    def test_unrelated_stale_voucher_cannot_block_weapon_upgrade(self):
        p = self.case()
        p["teamOur"]["roles"][0]["backpack"].append("StationUpgradeVoucher2")
        self.assertEqual(self.decide_worker(p)["name"], "WeaponUpgradeVoucher1")

    def test_buy_failure_backs_off_and_still_works(self):
        p = self.case()
        p.update(roundNo=2, lastRoundRoleActionResults={"1": False})
        mem = Memory(last_round=1, last_commands={"1": command("buy", name="WeaponUpgradeVoucher1", num=1)})
        mem.observe(Turn(p, Config()), Config())
        self.assertNotEqual(self.decide_worker(p, mem)["action"], "buy")

    def test_two_carriers_do_not_upgrade_same_building(self):
        p = self.case()
        p["teamOur"]["roles"][0]["backpack"] = ["WeaponUpgradeVoucher1"]
        p["teamOur"]["roles"].append(unit(2, "worker", 3, 5, backpack=["WeaponUpgradeVoucher1"]))
        t, _, nav, ledger = setup_case(p)
        self.assertTrue(use_inventory(t, nav, ledger, t.workers[0]))
        self.assertFalse(use_inventory(t, nav, ledger, t.workers[1]))
        self.assertFalse(ledger.add(2, command("use", (4, 5), name="WeaponUpgradeVoucher1")))

    def test_no_shop_trip_without_time_to_deliver(self):
        p = self.case()
        p["roundNo"] = 69
        self.assertNotEqual(self.decide_worker(p)["action"], "buy")

    def test_station_and_damaged_wall_vouchers_have_real_purchase_paths(self):
        for building, name in (("station", "StationUpgradeVoucher1"), ("wall", "WallUpgradeVoucher1")):
            p = self.case()
            p["teamOur"]["roles"][1]["level"] = 3
            p["teamOur"]["roles"].append(unit(30, building, 3, 7, health=400 if building == "wall" else 1500))
            p["weaponShopList"] = [{"name": name, "price": 20}]
            self.assertEqual(self.decide_worker(p)["name"], name)

    def test_save_for_weapon_instead_of_buying_cheap_wall_voucher(self):
        p = self.case()
        p["teamOur"]["goldNum"] = 100
        p["teamOur"]["roles"][1]["level"] = 2
        p["teamOur"]["roles"].append(unit(30, "wall", 3, 7, health=1000))
        p["weaponShopList"] = [{"name": "WeaponUpgradeVoucher2", "price": 150},
                               {"name": "WallUpgradeVoucher1", "price": 20}]
        self.assertNotEqual(self.decide_worker(p)["action"], "buy")

    def test_both_workers_seek_income_before_preparation(self):
        p = payload(roles=[unit(1, "worker", 5, 5), unit(2, "worker", 5, 7)])
        p["teamOur"]["goldNum"] = 0
        p["mapInfo"]["zones"] = [{"neutralType": k, "pos": {"x": x, "y": y}} for k, x, y in
                                   (("stone", 6, 5), ("copper", 6, 7), ("vendor", 4, 7))]
        p["vendorShopList"] = [{"name": "stone", "price": 1}, {"name": "copper", "price": 10}]
        result = Agent(Config(layout_mode="explicit", wall_cells=[[8, 5]], llm_enabled=False)).decide(p)
        self.assertEqual(result["roleCommandMap"]["1"], command("move", (5, 6)))
        self.assertEqual(result["roleCommandMap"]["2"], command("collect", (6, 7)))

    def test_repair_carrier_walks_back_and_uses_fixer(self):
        p = self.case()
        p["teamOur"]["roles"][0]["backpack"] = ["Medicine", "WallFixer"]
        p["teamOur"]["roles"].append(unit(30, "wall", 2, 7, health=100))
        for _ in range(10):
            cmd = self.decide_worker(p)
            if cmd["action"] == "use":
                break
            self.assertEqual(cmd["action"], "move")
            p["teamOur"]["roles"][0]["pos"] = cmd["targetPos"][0]
        self.assertEqual(cmd, command("use", (2, 7), name="WallFixer"))

    def test_multi_day_collect_sell_buy_deliver_upgrade(self):
        result = simulate(Agent, Config)
        self.assertEqual(result["invalid_actions"], 0)
        self.assertEqual(result["weapon_levels"], [3, 3, 3])
        self.assertEqual(result["station_level"], 3)
        self.assertEqual(result["purchases"].get("WallUpgradeVoucher2", 0), 0)
        self.assertGreater(result["purchases"]["Medicine"], 0)
        self.assertLess(result["first_rounds"]["used_WeaponUpgradeVoucher2"], 70)
        self.assertGreater(result["gold"], 2660)
        self.assertLess(result["worst_ms"], 1000)


class TaskReliabilityTests(unittest.TestCase):
    def run_fixed_tool(self, code, directory):
        # Only repository-authored tool code is executed in these fixtures.
        result = subprocess.run([sys.executable, "-c", code], cwd=directory,
                                capture_output=True, text=True, timeout=3)
        return f"[exitCode:{result.returncode}]\n{result.stdout}{result.stderr}"

    def test_document_bootstrap_read_query_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "api.md").write_text("Read values.csv; return the integer in the second column for Shanghai.")
            Path(directory, "values.csv").write_text("Beijing,21\nShanghai,25\n")
            p = task_payload(1, "阅读 api.md，查询 Shanghai 的温度，答案为整数。")
            agent = Agent(Config(layout_mode="explicit"))
            first = agent.decide(p)
            self.assertFalse(first["prompt"])
            self.assertEqual(shlex.split(first["executeCmd"])[-1], file_code("read", "api.md"))
            p.update(roundNo=2, lastCmdResult=self.run_fixed_tool(file_code("read", "api.md"), directory))
            self.assertIn("values.csv", agent.decide(p)["prompt"])
            p.update(roundNo=3, lastCmdResult="", llmResp="READ values.csv")
            self.assertTrue(agent.decide(p)["executeCmd"])
            p.update(roundNo=4, llmResp="", lastCmdResult=self.run_fixed_tool(file_code("read", "values.csv"), directory))
            self.assertIn("Shanghai,25", agent.decide(p)["prompt"])
            p.update(roundNo=5, lastCmdResult="", llmResp="ANSWER\n25")
            self.assertEqual(agent.decide(p)["roleCommandMap"]["11"]["taskAnswer"], "25")

    def test_read_pagination_does_not_lose_long_line_middle(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "doc.txt").write_text("x" * 6100 + "IMPORTANT" + "y" * 6000)
            first = self.run_fixed_tool(file_code("read", "doc.txt"), directory)
            self.assertIn("NEXT_READ 'doc.txt' 6000", first)
            second = self.run_fixed_tool(file_code("read", "doc.txt", 6000), directory)
            self.assertIn("IMPORTANT", second)
            self.assertLess(len(second), 6500)

    def test_document_paths_and_tool_formats(self):
        self.assertEqual(document_path("阅读 `/app/docs/api.md` 后查询"), "/app/docs/api.md")
        self.assertIsNone(document_path("阅读 https://example.com/docs/api.md"))
        for reply in ("READ api.md 6000", "READ\napi.md 6000"):
            self.assertEqual(parse_task_reply(reply), {"read": "api.md", "start": 6000})
        self.assertEqual(parse_task_reply("代码如下：\n```python\nprint(42)\n```"), {"python": "print(42)"})
        self.assertEqual(parse_task_reply("```text\nREAD api.md\n```"), {"read": "api.md", "start": 0})

    def test_last_failed_code_survives_repeated_bad_format(self):
        p = task_payload(2, "query")
        p["lastCmdResult"] = "[exitCode:1]\nNameError: missing_api"
        mem = Memory(task_text="query", pending=("cmd", 1), running_python="missing_api('Shanghai')")
        mem.observe(Turn(p, Config()), Config())
        for r in range(3, 10):
            mem.pending = ("task", r - 1)
            p.update(roundNo=r, llmResp="格式不正确", lastCmdResult="")
            t, cfg, _, ledger = setup_case(p)
            mem.observe(t, cfg)
            prompt, _ = Intelligence(t, cfg, mem).task(ledger)
        self.assertIn("missing_api('Shanghai')", prompt)
        self.assertIn("NameError", prompt)

    def test_night_without_own_threat_does_not_cancel_task(self):
        p = task_payload(199, "query")
        p["teamOur"]["roles"] += [unit(20, "rocket", 1, 1), unit(1, "worker", 1, 2), unit(13, "station", 2, 3)]
        p["robot"]["roles"] = [unit(90, "largeRobot", 7, 5, targetTeam="defender")]
        agent = Agent(Config(layout_mode="explicit"))
        agent.decide(p)
        p.update(roundNo=200, llmResp="LIST .")
        response = agent.decide(p)
        self.assertTrue(response["executeCmd"])
        self.assertNotIn("11", response["roleCommandMap"])
        self.assertNotIn("20", response["roleCommandMap"])

    def test_actual_threat_stops_task_and_logs_reason(self):
        p = task_payload(199, "query")
        p["teamOur"]["roles"] += [unit(20, "rocket", 1, 1), unit(13, "station", 2, 3)]
        agent = Agent(Config(layout_mode="explicit"))
        agent.decide(p)
        p.update(roundNo=200, llmResp="LIST .")
        p["robot"]["roles"] = [unit(90, "largeRobot", 7, 5, targetTeam="challenger")]
        response = agent.decide(p)
        self.assertFalse(response["executeCmd"])
        self.assertEqual(response["roleCommandMap"]["11"]["action"], "move")
        self.assertEqual(next(iter(agent.sessions.values())).stop_reason, "defence_threat")

    def test_conflicting_file_and_answer_is_rejected(self):
        p = task_payload(2, "query")
        p["llmResp"] = '{"read":"api.md","answer":"42"}'
        mem = Memory(task_text="query", pending=("task", 1))
        mem.observe(Turn(p, Config()), Config())
        self.assertIsNone(mem.answer)
        self.assertIsNone(mem.python)
        self.assertEqual(mem.task_failures, 1)


if __name__ == "__main__":
    unittest.main()
