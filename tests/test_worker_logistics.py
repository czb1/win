"""Regressions for phase changes, construction cargo and partial visibility."""
from collections import deque
from time import monotonic
import unittest

from test_agent import payload, unit, setup_case
from test_day_economy import simulate
from agent.brain import Agent
from agent.commands import command
from agent.combat import assignments, operator_posts, defend
from agent.config import Config
from agent.economy import worker, workers, build, wall_keeps_access
from agent.intelligence import Memory
from agent.mining import earn, sale_inventory
from agent.model import Turn, neighbours
from agent.navigation import Navigator


class LogisticsTests(unittest.TestCase):
    def case(self, tick=40):
        p = payload(tick, [unit(1, "worker", 5, 5, backpack=["stone"] * 10 + ["copper"] * 4)])
        p["mapInfo"]["zones"] = [{"neutralType": k, "pos": {"x": x, "y": y}}
                                   for k, x, y in [("vendor", 4, 5), ("copper", 6, 5), ("stone", 7, 7)]]
        p["vendorShopList"] = [{"name": "copper", "price": 10}, {"name": "stone", "price": 1}]
        return p

    def test_sale_keeps_construction_stone_and_finishes_once(self):
        p = self.case()
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(stone_reserves={1: 10}, sale_workers={1})
        earn(t, cfg, mem, nav, ledger, t.workers[0])
        self.assertEqual(ledger.commands["1"], command("sell", name="copper", num=4))
        p["teamOur"]["roles"][0]["backpack"] = ["stone"] * 10
        self.assertFalse(sale_inventory(Turn(p, cfg), mem, Turn(p, cfg).workers[0]))

    def test_surplus_stone_becomes_saleable_when_blueprint_fills(self):
        t, cfg, nav, ledger = setup_case(self.case())
        mem = Memory(stone_reserves={1: 3})
        self.assertEqual(sale_inventory(t, mem, t.workers[0])["stone"], 7)
        mem.stone_reserves.clear()
        self.assertEqual(sale_inventory(t, mem, t.workers[0])["stone"], 10)

    def test_sell_at_adjacent_vendor_even_if_another_was_selected(self):
        p = self.case()
        p["mapInfo"]["zones"].append({"neutralType": "vendor", "pos": {"x": 10, "y": 5}})
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(sale_workers={1}, sale_targets={1: (10, 5)}, stone_reserves={1: 10})
        earn(t, cfg, mem, nav, ledger, t.workers[0])
        self.assertEqual(ledger.commands["1"], command("sell", name="copper", num=4))

    def test_sale_destination_stays_fixed_during_transport(self):
        p = self.case()
        p["mapInfo"]["zones"][0]["pos"] = {"x": 2, "y": 5}
        p["mapInfo"]["zones"].append({"neutralType": "vendor", "pos": {"x": 10, "y": 5}})
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(sale_workers={1}, sale_targets={1: (10, 5)})
        earn(t, cfg, mem, nav, ledger, t.workers[0])
        self.assertEqual(mem.sale_targets[1], (10, 5))
        self.assertEqual(ledger.commands["1"]["targetPos"][0]["x"], 6)

    def test_full_backpack_uses_reserved_stone_instead_of_selling_it(self):
        p = payload(45, [unit(1, "worker", 5, 5, backpack=["stone"] * 4, backPackCapability=4),
                         unit(20, "rocket", 5, 7)])
        p["mapInfo"]["zones"] = [{"neutralType": k, "pos": {"x": x, "y": y}}
                                   for k, x, y in [("vendor", 4, 5), ("weaponShop", 4, 6)]]
        p["vendorShopList"] = [{"name": "stone", "price": 1}]
        p["weaponShopList"] = [{"name": "WeaponUpgradeVoucher1", "price": 20}]
        t, cfg, nav, ledger = setup_case(p, layout_mode="explicit", wall_cells=[[6, 5]])
        worker(t, cfg, Memory(stone_reserves={1: 4}), nav, ledger, t.workers[0], [], [(6, 5)], True)
        self.assertEqual(ledger.commands["1"], command("build", (6, 5), name="wall"))

    def test_unupgraded_weapons_do_not_cap_walls_at_the_front(self):
        p = self.case(45)
        p["teamOur"]["roles"][0]["backpack"] = ["stone"]
        p["teamOur"]["roles"] += [unit(13, "station", 2, 8)]
        p["teamOur"]["roles"] += [unit(20+i, "rocket", 2+i, 10) for i in range(3)]
        p["teamOur"]["roles"] += [unit(30+i, "wall", 8, 4+i) for i in range(3)]
        walls = [(8, 4), (8, 5), (8, 6), (5, 6)]
        t, cfg, nav, ledger = setup_case(p, layout_mode="explicit", wall_cells=[list(p) for p in walls])
        workers(t, cfg, Memory(), nav, ledger, [], walls)
        self.assertEqual(ledger.commands["1"], command("build", (5, 6), name="wall"))

    def test_completed_material_load_is_spent_without_refilling_after_each_wall(self):
        p = self.case(45)
        p["teamOur"]["roles"][0]["backpack"] = ["stone"] * 9
        walls = [(5, 6), (5, 4), (6, 6), (6, 4), (7, 4), (8, 4), (9, 4), (10, 4), (11, 4), (12, 4)]
        t, cfg, nav, ledger = setup_case(p, layout_mode="explicit", wall_cells=[list(p) for p in walls])
        mem = Memory(preparation_workers={1})
        workers(t, cfg, mem, nav, ledger, [], walls)
        self.assertNotEqual(ledger.commands["1"]["action"], "collect")

    def test_near_flank_wall_precedes_distant_blueprint_index(self):
        p = payload(45, [unit(1, "worker", 6, 8, backpack=["stone"]),
                         unit(13, "station", 2, 8), unit(99, "wall", 9, 8)])
        walls = [(9, 8), (3, 3), (7, 8)]
        t, cfg, nav, ledger = setup_case(p, layout_mode="explicit", wall_cells=[list(p) for p in walls])
        build(t, cfg, Memory(), nav, ledger, t.workers[0], walls, lambda _: "wall")
        self.assertEqual(ledger.commands["1"], command("build", (7, 8), name="wall"))


class MovementFeedbackTests(unittest.TestCase):
    def test_collision_avoids_failed_cell_then_expires(self):
        p = payload(11, [unit(1, "worker", 2, 2), unit(2, "worker", 8, 8)])
        mem = Memory(last_round=10, last_commands={"1": command("move", (3, 3))})
        cfg = Config()
        turn = Turn(p, cfg)
        mem.observe(turn, cfg)
        nav = Navigator(turn, monotonic()+3, mem.movement)
        self.assertNotEqual(nav.search(turn.workers[0], {(6, 6)})[1], (3, 3))
        self.assertNotIn((3, 3), mem.movement.blocked(2))
        p["roundNo"] = 15
        mem.observe(Turn(p, cfg), cfg)
        self.assertNotIn((3, 3), mem.movement.blocked(1))

    def test_repeated_collision_extends_cooldown_for_a_long_detour(self):
        p = payload(11, [unit(1, "worker", 2, 2)])
        mem = Memory(last_round=10, last_commands={"1": command("move", (3, 3))})
        for tick in (11, 16, 25):
            p["roundNo"], mem.last_round = tick, tick-1
            mem.observe(Turn(p, Config()), Config())
        self.assertEqual(mem.movement.failures[1, (3, 3)], 41)

    def test_short_movement_cycle_releases_target(self):
        p = payload(9, [unit(1, "worker", 2, 2)])
        mem = Memory(last_round=8, mine_targets={1: (6, 6)},
                     last_commands={"1": command("move", (2, 2))})
        mem.movement.trails[1] = deque([(2, 2), (3, 2), (2, 2), (3, 2), (2, 2), (3, 2), (3, 2)], maxlen=8)
        mem.observe(Turn(p, Config()), Config())
        self.assertTrue(mem.movement.avoids(1, (6, 6)))
        self.assertNotIn(1, mem.mine_targets)

    def test_four_cell_cycle_releases_construction_target(self):
        p = payload(9, [unit(1, "worker", 2, 2)])
        mem = Memory(day=1, last_round=8, build_targets={1: (6, 6)},
                     last_commands={"1": command("move", (2, 2))})
        mem.movement.trails[1] = deque([(3, 2), (3, 3), (2, 3), (2, 2), (3, 2), (3, 3), (2, 3)], maxlen=8)
        mem.observe(Turn(p, Config()), Config())
        self.assertTrue(mem.movement.avoids(1, (6, 6)))
        self.assertNotIn(1, mem.build_targets)

    def test_unseen_weapon_is_remembered_until_its_cell_is_seen_empty(self):
        p = payload(1, [unit(1, "worker", 3, 5)])
        p["teamEnemy"]["roles"] = [unit(99, "rocket", 6, 5)]
        cfg, mem = Config(), Memory()
        mem.observe(Turn(p, cfg), cfg)
        p["teamEnemy"]["roles"] = []
        p["teamOur"]["roles"][0]["pos"] = {"x": 0, "y": 5}
        turn = Turn(p, cfg)
        mem.observe(turn, cfg)
        self.assertIn((6, 5), turn.blocked)
        p["teamOur"]["roles"][0]["pos"] = {"x": 3, "y": 5}
        turn = Turn(p, cfg)
        mem.observe(turn, cfg)
        self.assertNotIn((6, 5), turn.blocked)

    def test_mobile_enemy_and_exhausted_ore_do_not_become_ghost_obstacles(self):
        p = payload(1, [unit(1, "worker", 3, 5)])
        p["teamEnemy"]["roles"] = [unit(99, "worker", 6, 5)]
        p["mapInfo"]["zones"] = [{"neutralType": "stone", "pos": {"x": 6, "y": 6}}]
        mem, cfg = Memory(), Config()
        mem.observe(Turn(p, cfg), cfg)
        p["teamEnemy"]["roles"], p["mapInfo"]["zones"] = [], []
        turn = Turn(p, cfg)
        mem.observe(turn, cfg)
        self.assertNotIn((6, 5), turn.blocked)
        self.assertNotIn((6, 6), turn.blocked)


class OperatorReturnTests(unittest.TestCase):
    def test_distant_return_recalls_idle_gatekeeper_before_its_own_deadline(self):
        p = payload(60, [unit(1, "worker", 0, 4), unit(2, "worker", 3, 3),
                         unit(20, "rocket", 3, 2), unit(21, "rocket", 6, 3)])
        p["mapInfo"].update(width=7, height=7)
        p["teamOur"]["roles"] += [unit(100+y, "wall", 3, y) for y in (0, 1, 4, 5, 6)]
        for task in (False, True):
            with self.subTest(pioneer_on_task=task):
                if task:
                    p["teamOur"]["roles"].append(unit(3, "pioneer", 1, 2))
                    p["phaseTask"] = "返回42"
                cfg = Config(llm_enabled=task, layout_mode="explicit")
                agent, turn = Agent(cfg), Turn(p, cfg)
                mem = Memory(day=1, return_targets={1: 21}, return_posts={1: (5, 3)})
                agent.sessions[(*turn.key, None)] = mem
                result = agent.decide(p)
                self.assertEqual(result["roleCommandMap"]["2"]["action"], "move")
                self.assertNotEqual(result["roleCommandMap"]["2"]["targetPos"][0], {"x": 3, "y": 3})
                self.assertIn(2, mem.return_targets)
                if task:
                    self.assertNotIn(3, mem.return_targets)
                    self.assertTrue(result["prompt"])

    def test_shared_nearest_cell_does_not_count_as_two_operator_posts(self):
        p = payload(60, [unit(1, "worker", 3, 2), unit(2, "pioneer", 3, 5),
                         unit(20, "rocket", 5, 3), unit(21, "rocket", 5, 5)])
        # The first gun has only (4, 4). The second can also use that cell,
        # but the complete crew must give it the distinct post (4, 5).
        occupied = (set(neighbours((5, 3))) | set(neighbours((5, 5)))) - {
            (4, 4), (4, 5), (5, 3), (5, 5)}
        p["teamOur"]["roles"] += [unit(100+i, "wall", *q) for i, q in enumerate(sorted(occupied))]
        t, _, nav, ledger = setup_case(p)
        pairs = list(zip(t.heroes, t.weapons))
        posts = operator_posts(t, nav, pairs)
        self.assertEqual(posts[1][0], (4, 4))
        self.assertEqual(posts[2][0], (4, 5))
        defend(t, nav, ledger, pairs, {uid: q for uid, (q, _) in posts.items()})
        self.assertEqual(ledger.commands["2"], command("move", (4, 5)))

    def test_operator_waits_beside_gate_until_teammate_passes(self):
        p = payload(62, [unit(1, "worker", 2, 3), unit(2, "pioneer", 1, 4),
                         unit(20, "rocket", 3, 2), unit(21, "rocket", 6, 3)])
        p["mapInfo"].update(width=7, height=7)
        p["teamOur"]["roles"] += [unit(100+y, "wall", 3, y) for y in (0, 1, 4, 5, 6)]
        t, _, nav, ledger = setup_case(p)
        pairs = list(zip(t.heroes, t.weapons))
        defend(t, nav, ledger, pairs, {1: (3, 3), 2: (5, 3)})
        self.assertNotEqual(ledger.commands.get("1"), command("move", (3, 3)))
        self.assertEqual(ledger.commands["2"]["action"], "move")
        # Once the second operator is inside, the gate operator may stand down.
        p["teamOur"]["roles"][1]["pos"] = {"x": 5, "y": 3}
        t, _, nav, ledger = setup_case(p)
        defend(t, nav, ledger, list(zip(t.heroes, t.weapons)), {1: (3, 3), 2: (5, 3)})
        self.assertEqual(ledger.commands["1"], command("move", (3, 3)))

    def test_recall_keeps_assignment_then_resets_at_dawn(self):
        p = payload(65, [unit(1, "worker", 2, 2), unit(2, "pioneer", 10, 10),
                         unit(20, "rocket", 11, 11), unit(21, "rocket", 3, 3)])
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(day=1, return_targets={1: 20, 2: 21}, return_posts={1: (10, 11)})
        self.assertEqual({h.id: w.id for h, w in assignments(t, nav, ledger, fixed=mem.return_targets)},
                         {1: 20, 2: 21})
        p["roundNo"] = 130
        mem.observe(Turn(p, cfg), cfg)
        self.assertFalse(mem.return_targets)
        self.assertFalse(mem.return_posts)

    def test_late_wall_must_preserve_time_to_the_reserved_post(self):
        p = payload(66, [unit(1, "worker", 2, 2, backpack=["stone"]), unit(20, "rocket", 5, 2)])
        t, _, nav, ledger = setup_case(p)
        ledger.return_pairs = [(t.heroes[0], t.weapons[0])]
        ledger.operator_posts = {1: (4, 2)}
        original = t.blocked.copy()
        self.assertFalse(wall_keeps_access(t, nav, ledger, (3, 2)))
        self.assertEqual(t.blocked, original)


class LogisticsReplayTests(unittest.TestCase):
    def test_remote_ore_keeps_all_operators_and_front_wall_ready(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result = simulate(Agent, Config, case="remote_ore", mirror=mirror)
                self.assertEqual(result["invalid_actions"], 0)
                self.assertEqual(result["checkpoints"]["69"]["operators_ready"], 3)
                self.assertEqual(result["checkpoints"]["69"]["front_walls"], 6)

    def test_rebuild_front_for_three_days_without_stranding_operators(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result = simulate(Agent, Config, mirror=mirror, days=3, damage_walls=True, trace=True)
                self.assertEqual(result["invalid_actions"], 0)
                self.assertEqual(result["destroyed_walls"], 4)
                nights = [result["checkpoints"][str(r)] for r in (69, 199, 329)]
                self.assertTrue(all(n["operators_ready"] == 3 and n["front_walls"] == 6 for n in nights))
                self.assertTrue(all(n["walls"] == 6 for n in nights))
                self.assertGreaterEqual(nights[2]["walls"], nights[1]["walls"])
                self.assertEqual(nights[2]["weapon_levels"], [3, 3, 3])
                visits, previous_sales = {}, {}
                for row in result["trace"]:
                    for h in row["workers"]:
                        if (h["command"] or {}).get("action") == "sell":
                            self.assertLess(row["round"] % 130, 70)
                            key = (row["round"] // 130, h["id"])
                            if previous_sales.get(h["id"]) != row["round"] - 1:
                                visits[key] = visits.get(key, 0) + 1
                            previous_sales[h["id"]] = row["round"]
                self.assertTrue(visits)
                self.assertTrue(all(n == 1 for n in visits.values()), visits)


if __name__ == "__main__":
    unittest.main()
