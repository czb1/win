"""Sample-derived economy and finite, map-wide mineral regression checks."""
from collections import Counter
import json
from statistics import median
import unittest
import sys

from test_agent import ROOT
from agent.brain import Agent
from agent.config import Config
sys.path.insert(0, str(ROOT / "tools"))
from day_economy_benchmark import simulate, summarize
from run_checks import replay_test


class EconomyCalibrationTests(unittest.TestCase):
    @replay_test
    def test_sample_and_random_first_night_matrix(self):
        results = []
        for profile, seeds in (("sample", (0,)), ("random", (0, 7, 19))):
            for seed in seeds:
                for mirror in (False, True):
                    with self.subTest(profile=profile, seed=seed, mirror=mirror):
                        result = simulate(Agent, Config, profile=profile, seed=seed, mirror=mirror, trace=True)
                        results.append(result)
                        self.assertEqual(result["invalid_actions"], 0)
                        self.assertEqual(result["prices"], {"stone": 1, "iron": 3, "copper": 5})
                        self.assertEqual(Counter(m["kind"] for m in result["initial_mines"]),
                                         {"stone": 2, "iron": 2, "copper": 2})
                        self.assertEqual([r["round"] for r in result["trace"]], list(range(130)))
                        for checkpoint in result["checkpoints"].values():
                            self.assertEqual(checkpoint["gold"], 75 + checkpoint["income"] - checkpoint["spent"])
                            self.assertGreaterEqual(checkpoint["gold"], 0)
                        dusk = result["checkpoints"]["69"]
                        self.assertEqual(len(dusk["weapon_levels"]), 3)
                        self.assertEqual(dusk["shared_guns_ready"], 3)
                        # Spending must fund actual upgrades, not phantom starting assets.
                        self.assertGreaterEqual(dusk["spent"], 75 + 100 * sum(l >= 2 for l in dusk["weapon_levels"]))
                        for event in result["respawns"]:
                            x, y = event["new"]
                            self.assertTrue(0 <= x < 41 and 0 <= y < 32)
                            self.assertNotEqual(event["old"], event["new"])
                            if mirror:
                                x, y = 40-x, 31-y
                            self.assertFalse(any(bx-2 <= x < bx+4 and by-3 <= y < by+3
                                                 for bx, by in ((10, 24), (30, 10))))
                        for row in result["trace"]:
                            self.assertEqual(len({tuple(m["pos"]) for m in row["mines"]}), 6)
                            self.assertTrue(all(1 <= m["remaining"] <= 10 for m in row["mines"]))
        # This is a calibration envelope, not a rule capping legal tower upgrades.
        self.assertLessEqual(median(sum(l >= 2 for l in r["checkpoints"]["69"]["weapon_levels"])
                                    for r in results), 1)
        report = {**summarize(results), "runs": results}
        output = ROOT / "artifacts/calibration/first-night.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report["distribution"], ensure_ascii=False))

    def test_last_mineral_is_shared_and_respawn_only_visible_next_round(self):
        class Collectors:
            def __init__(self, cfg):
                pass

            def decide(self, state):
                rno = state["roundNo"]
                commands = {}
                if rno < 4:
                    for uid in (1, 2):
                        actor = next(r for r in state["teamOur"]["roles"] if r["id"] == uid)
                        commands[str(uid)] = {"action": "move", "targetPos": [
                            {"x": actor["pos"]["x"]-1, "y": 23 if uid == 1 else 24}]}
                elif rno <= 13:
                    for uid in ((1, 2) if rno == 13 else (1,)):
                        commands[str(uid)] = {"action": "collect", "targetPos": [{"x": 4, "y": 24}]}
                return {"roleCommandMap": commands}

        result = simulate(Collectors, Config, trace=True)
        self.assertEqual(result["invalid_actions"], 0)
        self.assertEqual(result["mined_before_70"], {"stone": 11})
        self.assertEqual(len(result["respawns"]), 1)
        event = result["respawns"][0]
        self.assertEqual(event["available_round"], 14)
        self.assertEqual(next(m["remaining"] for m in result["trace"][13]["mines"] if m["pos"] == [4, 24]), 1)
        self.assertNotIn([4, 24], [m["pos"] for m in result["trace"][14]["mines"]])
        self.assertEqual(next(m["remaining"] for m in result["trace"][14]["mines"] if m["pos"] == event["new"]), 10)
        again = simulate(Collectors, Config, trace=True)
        self.assertEqual(result["trace"], again["trace"])
        self.assertEqual(result["respawns"], again["respawns"])

    def test_seed_and_resource_density_are_explicit(self):
        class Idle:
            def __init__(self, cfg):
                pass

            def decide(self, state):
                return {"roleCommandMap": {}}

        first = simulate(Idle, Config, profile="random", seed=3, mines_per_kind=4)
        again = simulate(Idle, Config, profile="random", seed=3, mines_per_kind=4)
        other = simulate(Idle, Config, profile="random", seed=4, mines_per_kind=4)
        mirrored = simulate(Idle, Config, profile="random", seed=3, mirror=True, mines_per_kind=4)
        self.assertEqual(len(first["initial_mines"]), 12)
        self.assertEqual(first["initial_mines"], again["initial_mines"])
        self.assertNotEqual(first["initial_mines"], other["initial_mines"])
        self.assertEqual([(m["kind"], [40-m["pos"][0], 31-m["pos"][1]]) for m in first["initial_mines"]],
                         [(m["kind"], m["pos"]) for m in mirrored["initial_mines"]])
        self.assertEqual(first["checkpoints"]["69"]["gold"], 75)
        self.assertEqual(first["checkpoints"]["69"]["weapon_levels"], [])
        for kwargs in ({"days": 0}, {"profile": "unknown"}, {"case": "far_shop"},
                       {"profile": "random", "mines_per_kind": 0}):
            with self.assertRaises(ValueError):
                simulate(Idle, Config, **kwargs)

