"""Regressions for wall throughput, aligned geometry and weak-model replies."""
import copy
import json
import sys
from time import monotonic
import unittest

from test_agent import payload, unit, setup_case, ROOT
from agent.brain import Agent
from agent.config import Config
from agent.model import Turn, distance
from agent.navigation import layout, Navigator, DeadlineExceeded
from agent.commands import command
from agent.economy import worker, build, wall_keeps_access, mine
from agent.combat import select_targets
from agent.intelligence import Memory, Intelligence, parse_object, parse_task_reply

sys.path.insert(0, str(ROOT / "tools"))
from fortification_benchmark import simulate_day


class LayoutRegressionTests(unittest.TestCase):
    def test_rectangle_is_anchored_to_entire_station_footprint(self):
        t, c, _, _ = setup_case(payload())
        towers, walls = layout(t, c)
        self.assertEqual(len(walls), 16)
        self.assertEqual(len(set(walls)), len(walls))
        self.assertTrue(all(t.base_distance(p) == 2 for p in walls))
        self.assertTrue(all(p[0] in (1, 6) or p[1] in (8, 13) for p in walls))
        self.assertTrue(all(t.base_distance(p) == 1 for p in towers))
        # Two contiguous rear gate cells.
        self.assertNotIn((1, 10), walls)
        self.assertNotIn((1, 11), walls)

    def test_side_switch_mirrors_the_entire_blueprint(self):
        a = payload(roles=[unit(13, "station", 3, 11)])
        b = payload(roles=[unit(13, "station", 10, 4)])
        b["teamOur"]["type"] = "defender"
        la = layout(Turn(a, Config()), Config())
        lb = layout(Turn(b, Config()), Config())
        for one, two in zip(la, lb):
            self.assertEqual([(14-x, 14-y) for x, y in one], two)

    def test_blueprint_does_not_shift_when_a_site_is_occupied(self):
        p = payload()
        before = layout(Turn(p, Config()), Config())
        p["teamOur"]["roles"].append(unit(60, "wall", *before[1][0]))
        p["teamOur"]["roles"][1]["pos"] = {"x": before[1][1][0], "y": before[1][1][1]}
        self.assertEqual(layout(Turn(p, Config()), Config()), before)

    def test_edge_layout_stays_inside_map(self):
        for x, y in ((0, 1), (13, 1), (0, 14), (13, 14)):
            t = Turn(payload(roles=[unit(13, "station", x, y)]), Config())
            towers, walls = layout(t, Config())
            self.assertTrue(all(t.inside(p) for p in towers + walls))
            self.assertTrue(all(t.base_distance(p) == 2 for p in walls))

    def test_explicit_whitelist_preserves_order_without_duplicate_sites(self):
        t, c, _, _ = setup_case(payload(), layout_mode="explicit",
                               wall_cells=[[5, 5], [5, 5], [6, 5]], weapon_cells=[[2, 2]])
        self.assertEqual(layout(t, c), ([(2, 2)], [(5, 5), (6, 5)]))

    def test_completed_default_walls_keep_direct_fire_lanes(self):
        p = payload(71, roles=[unit(13, "station", 3, 11)])
        t = Turn(p, Config())
        towers, walls = layout(t, Config())
        p["teamOur"]["roles"] += [unit(100+i, "wall", *point) for i, point in enumerate(walls)]
        # Default gatling faces right, railgun down.
        for index, (kind, target) in enumerate((("gatling", (9, 11)), ("railgun", (3, 5)))):
            case = copy.deepcopy(p)
            case["teamOur"]["roles"].append(unit(20, kind, *towers[index]))
            case["robot"]["roles"] = [unit(80, "smallRobot", *target, health=40)]
            turn, _, nav, _ = setup_case(case)
            self.assertTrue(select_targets(turn, turn.weapons[0], {}, nav.deadline))

    def test_complete_wall_ring_keeps_worker_route_to_base(self):
        p = payload(roles=[unit(13, "station", 3, 11), unit(10, "worker", 8, 10)])
        towers, walls = layout(Turn(p, Config()), Config())
        p["teamOur"]["roles"] += [unit(100+i, "wall", *point) for i, point in enumerate(walls)]
        p["teamOur"]["roles"] += [unit(20+i, kind, *point)
                                  for i, (kind, point) in enumerate(zip(Config().loadout, towers))]
        t, _, n, _ = setup_case(p)
        self.assertIsNotNone(n.approach(t.workers[0], t.station.cells))


class ConstructionRegressionTests(unittest.TestCase):
    def test_both_workers_build_after_old_day_one_cap(self):
        p = payload(roles=[unit(1, "worker", 3, 3, backpack=["stone"]),
                           unit(2, "worker", 7, 3, backpack=["stone"])]
                          + [unit(20+i, "wall", i, 10) for i in range(6)])
        cfg = Config(layout_mode="explicit", weapon_cells=[], wall_cells=[[3, 4], [7, 4]],
                     llm_enabled=False)
        commands = Agent(cfg).decide(p)["roleCommandMap"]
        self.assertEqual({uid for uid, c in commands.items() if c["action"] == "build"}, {"1", "2"})
        self.assertEqual(len({tuple(c["targetPos"][0].values()) for c in commands.values()}), 2)

    def test_moving_builders_claim_different_work_destinations(self):
        p = payload(roles=[unit(1, "worker", 1, 1), unit(2, "worker", 2, 1)])
        sites = [(7, 7), (9, 7)]
        t, c, n, l = setup_case(p, layout_mode="explicit", wall_cells=list(map(list, sites)))
        for hero in t.workers:
            self.assertTrue(build(t, c, Memory(), n, l, hero, sites, lambda _: "wall"))
        self.assertEqual(set(l.build_claims), set(sites))
        self.assertEqual(len(l.reserved), 2)
        self.assertTrue(all(cmd["action"] == "move" for cmd in l.commands.values()))

    def test_dusk_uses_partial_stone_batch(self):
        p = payload(55, roles=[unit(1, "worker", 2, 2, backpack=["stone"])])
        p["mapInfo"]["zones"] = [{"neutralType": "stone", "pos": {"x": 3, "y": 2}}]
        sites = [(10, y) for y in range(5, 11)]
        t, c, n, l = setup_case(p, layout_mode="explicit", wall_cells=list(map(list, sites)))
        worker(t, c, Memory(), n, l, t.workers[0], [], sites, True)
        self.assertEqual(l.commands["1"]["action"], "move")

    def test_news_prediction_cannot_disable_a_visible_stone_deposit(self):
        p = payload(roles=[unit(1, "worker", 2, 2)])
        p["mapInfo"]["zones"] = [{"neutralType": "stone", "pos": {"x": 3, "y": 2}}]
        t, c, n, l = setup_case(p)
        mem = Memory(outages=[{"name": "stone", "startDay": 1, "endDay": 10}])
        self.assertTrue(mine(t, c, mem, n, l, t.workers[0], want_stone=True))
        self.assertEqual(l.commands["1"]["action"], "collect")

    def test_cached_routes_invalidate_when_occupancy_changes(self):
        p = payload(roles=[unit(1, "worker", 1, 1)])
        t, _, n, _ = setup_case(p)
        first = n.search(t.heroes[0], {(4, 1)})
        t.blocked.add(first[1])
        second = n.search(t.heroes[0], {(4, 1)})
        self.assertNotEqual(first[1], second[1])

    def divider_case(self, holes):
        p = payload(roles=[unit(1, "worker", 2, 2), unit(2, "worker", 2, 4),
                           unit(13, "station", 0, 5)]
                          + [unit(100+y, "wall", 3, y) for y in range(7) if y not in holes])
        p["mapInfo"].update(width=7, height=7, zones=[
            {"neutralType": "vendor", "pos": {"x": 0, "y": 0}},
            {"neutralType": "weaponShop", "pos": {"x": 5, "y": 3}}])
        for r in p["teamOur"]["roles"][:2]:
            r["backpack"] = ["stone"]
        return setup_case(p, layout_mode="explicit", wall_cells=[[3, y] for y in holes])

    def test_wall_must_preserve_shop_even_if_vendor_remains_reachable(self):
        t, _, n, l = self.divider_case([3])
        self.assertFalse(wall_keeps_access(t, n, l, (3, 3)))

    def test_second_build_checks_combined_wall_occupancy(self):
        t, _, n, l = self.divider_case([2, 4])
        self.assertTrue(wall_keeps_access(t, n, l, (3, 2)))
        self.assertTrue(l.add(1, command("build", (3, 2), name="wall")))
        self.assertFalse(wall_keeps_access(t, n, l, (3, 4)))

    def test_connectivity_deadline_restores_turn_obstacles(self):
        t, _, _, l = self.divider_case([3])
        original = t.blocked
        with self.assertRaises(DeadlineExceeded):
            wall_keeps_access(t, Navigator(t, 0), l, (3, 3))
        self.assertIs(t.blocked, original)

    def test_first_day_replay_exceeds_six_walls_with_two_builders(self):
        result = simulate_day(Agent, Config)
        self.assertGreater(result["walls_day1"], 6, result)
        self.assertEqual(result["invalid_actions"], 0, result)
        self.assertEqual(len(result["walls_per_worker"]), 2, result)
        print("\nControlled construction replay:", json.dumps(result))


class WeakModelRegressionTests(unittest.TestCase):
    def test_plain_code_avoids_json_string_escaping(self):
        code = 'data = {"name": "中文"}\nprint(data["name"])'
        self.assertEqual(parse_task_reply("PYTHON\n" + code), {"python": code})
        self.assertEqual(parse_task_reply("```python\n" + code + "\n```"), {"python": code})

    def test_answer_body_preserves_json(self):
        self.assertEqual(parse_task_reply('ANSWER\n{"city":"北京","temperature":20}'),
                         {"answer": '{"city":"北京","temperature":20}'})

    def test_one_json_object_in_commentary_is_recovered(self):
        self.assertEqual(parse_object('结果如下：\n```json\n{"answer":"42"}\n```'),
                         {"answer": "42"})
        self.assertIsNone(parse_object('{"answer":"a"}\n{"answer":"b"}'))
        self.assertIsNone(parse_object('[{"answer":"a"}]'))
        self.assertIsNone(parse_object('{"broken": {"answer":"a"}'))

    def observe_reply(self, mem, text, round_no=2):
        p = payload(round_no, roles=[unit(11, "pioneer", 5, 5)])
        p["phaseTask"] = "return a value"
        p["llmResp"] = text
        mem.task_text = p["phaseTask"]
        mem.pending = ("task", round_no - 1)
        t, c, _, l = setup_case(p)
        mem.observe(t, c)
        return t, c, l

    def test_repeated_candidate_is_not_resubmitted_forever(self):
        mem = Memory()
        for r in range(2, 7):
            t, c, l = self.observe_reply(mem, "ANSWER\nwrong", r)
            Intelligence(t, c, mem).task(l)
            if r < 4:
                self.assertIn("11", l.commands)
            else:
                self.assertNotIn("11", l.commands)
        self.assertEqual(mem.task_failures, 3)
        self.assertEqual(Intelligence(t, c, mem).task(l), ("", ""))

    def test_ambiguous_and_invalid_python_replies_count_as_no_progress(self):
        for reply in ('{"python":"print(1)","answer":"1"}', "PYTHON\nfor", "nonsense"):
            mem = Memory()
            self.observe_reply(mem, reply)
            self.assertEqual(mem.task_failures, 1)
            self.assertIsNone(mem.python)
            self.assertIsNone(mem.answer)

    def test_new_task_resets_failure_budget(self):
        mem = Memory(task_text="old", task_failures=3, proposal_counts={"x": 9})
        p = payload()
        p["phaseTask"] = "new"
        mem.observe(Turn(p, Config()), Config())
        self.assertEqual(mem.task_failures, 0)
        self.assertFalse(mem.proposal_counts)

    def test_three_malformed_replies_return_pioneer_toward_base(self):
        cfg = Config(layout_mode="explicit")
        p = payload(roles=[unit(13, "station", 2, 12), unit(11, "pioneer", 10, 3)])
        p["phaseTask"] = "return a value"
        agent = Agent(cfg)
        for r in range(1, 5):
            p["roundNo"] = r
            p["llmResp"] = "nonsense"
            response = agent.decide(p)
        self.assertFalse(response["prompt"])
        self.assertFalse(response["executeCmd"])
        self.assertEqual(response["roleCommandMap"]["11"]["action"], "move")


if __name__ == "__main__":
    unittest.main()
