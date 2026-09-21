"""Deterministic breach repair and replacement-level recovery."""
import unittest

from test_agent import payload, setup_case, unit
from agent.commands import command
from agent.economy import build, supplies, use_inventory, workers, repair_walls
from agent.intelligence import Memory
from agent.model import Turn
from agent.navigation import wall_gaps


class WallRepairTests(unittest.TestCase):
    def batch_case(self):
        p = payload(130, [unit(1, "worker", 12, 4), unit(2, "worker", 12, 8),
                          unit(13, "station", 1, 6, health=1500, level=3),
                          unit(20, "rocket", 2, 5, level=3),
                          unit(30, "wall", 4, 3, health=1000),
                          unit(31, "wall", 4, 7, health=1000)])
        p["mapInfo"]["zones"] = [{"neutralType": k, "pos": {"x": x, "y": y}}
                                   for k, x, y in (("stone", 13, 4), ("copper", 13, 8), ("vendor", 11, 8))]
        p["vendorShopList"] = [{"name": "stone", "price": 1}, {"name": "copper", "price": 10}]
        settings = dict(layout_mode="explicit", loadout=["rocket"], weapon_cells=[[2, 5]],
                        wall_cells=[[4, y] for y in range(3, 8)])
        return p, settings

    def test_one_material_trip_closes_three_gaps_and_releases_worker(self):
        p, settings = self.batch_case()
        mem = Memory()
        stone_actions, wall_actions, after_repair = [], [], []
        first_build_load = None
        for round_no in range(130, 160):
            p["roundNo"] = round_no
            t, c, n, l = setup_case(p, **settings)
            mem.observe(t, c)
            complete = all(any(w.kind == "wall" and w.pos == (4, y) for w in t.ours)
                           for y in range(4, 7))
            workers(t, c, mem, n, l, [(2, 5)], [(4, y) for y in range(3, 8)])
            # The second actor has a reachable, profitable mine throughout.
            self.assertIn("2", l.commands, (round_no, l.commands))
            self.assertNotIn(2, mem.preparation_workers)
            self.assertNotEqual(l.commands["2"]["action"], "build")
            if complete:
                self.assertIsNone(mem.wall_repair_worker)
                self.assertIn("1", l.commands)
                after_repair.append(l.commands["1"]["action"])
            else:
                self.assertEqual(mem.wall_repair_worker, 1)
            for uid, cmd in l.commands.items():
                actor = next(r for r in p["teamOur"]["roles"] if str(r["id"]) == uid)
                action = cmd["action"]
                target = tuple(cmd["targetPos"][0][k] for k in ("x", "y")) if "targetPos" in cmd else None
                if action == "move":
                    self.assertNotIn(target, t.blocked)
                    actor["pos"] = cmd["targetPos"][0]
                elif action == "collect":
                    kind = t.zones[target]
                    actor["backpack"].append(kind)
                    if kind == "stone":
                        self.assertEqual(uid, "1")
                        stone_actions.append(round_no)
                elif action == "build":
                    self.assertEqual((uid, cmd["name"]), ("1", "wall"))
                    if first_build_load is None:
                        first_build_load = actor["backpack"].count("stone")
                    actor["backpack"].remove("stone")
                    p["teamOur"]["roles"].append(unit(100 + round_no, "wall", *target, health=1000))
                    wall_actions.append(round_no)
                elif action == "sell":
                    for _ in range(cmd["num"]):
                        actor["backpack"].remove(cmd["name"])
                    p["teamOur"]["goldNum"] += cmd["num"] * t.prices[cmd["name"]]
                else:
                    self.fail(f"Unexpected replay action: {cmd}")
            mem.last_round, mem.last_commands = round_no, l.commands
            p["lastRoundRoleActionResults"] = {uid: True for uid in l.commands}
        self.assertEqual(len(stone_actions), 3)
        self.assertEqual(first_build_load, 3)
        self.assertEqual(len(wall_actions), 3)
        self.assertLess(max(stone_actions), min(wall_actions))
        self.assertTrue(after_repair)
        self.assertIn("collect", after_repair)

    def test_assigned_collector_does_not_switch_when_other_worker_has_stone(self):
        p, settings = self.batch_case()
        p["teamOur"]["roles"][0]["backpack"] = ["stone"]
        p["teamOur"]["roles"][1]["backpack"] = ["stone"] * 3
        t, c, n, l = setup_case(p, **settings)
        mem = Memory(wall_repair_worker=1)
        workers(t, c, mem, n, l, [(2, 5)], sorted(l.wall_cells))
        self.assertEqual(mem.wall_repair_worker, 1)
        self.assertEqual(l.commands["1"], command("collect", (13, 4)))
        self.assertNotEqual(l.commands["2"]["action"], "build")

    def test_delivery_spends_partial_load_without_returning_to_mine(self):
        p, settings = self.batch_case()
        p["teamOur"]["roles"][0]["backpack"] = ["stone"] * 2
        t, c, n, l = setup_case(p, **settings)
        mem = Memory(wall_repair_worker=1, wall_repair_delivering=True)
        repair_walls(t, c, mem, n, l, t.workers, sorted(l.wall_cells))
        self.assertEqual(l.commands["1"]["action"], "move")
        self.assertLess(l.commands["1"]["targetPos"][0]["x"], 12)

    def test_full_material_backpack_starts_delivery(self):
        p, settings = self.batch_case()
        p["teamOur"]["roles"][0].update(backpack=["stone"] * 2, backPackCapability=2)
        t, c, n, l = setup_case(p, **settings)
        mem = Memory(wall_repair_worker=1)
        repair_walls(t, c, mem, n, l, t.workers, sorted(l.wall_cells))
        self.assertTrue(mem.wall_repair_delivering)
        self.assertEqual(l.commands["1"]["action"], "move")

    def test_finished_repair_releases_worker_even_when_wall_upgrades_remain(self):
        p, settings = self.batch_case()
        p["teamOur"]["roles"] += [unit(100 + y, "wall", 4, y, health=1000) for y in range(4, 7)]
        t, c, n, l = setup_case(p, **settings)
        mem = Memory(wall_repair_worker=1, wall_repair_delivering=True,
                     wall_rebuild_levels={(4, 5): 3})
        workers(t, c, mem, n, l, [(2, 5)], sorted(l.wall_cells))
        self.assertIsNone(mem.wall_repair_worker)
        self.assertEqual(set(l.commands), {"1", "2"})
        self.assertFalse(mem.preparation_workers)

    def test_dead_or_returning_worker_can_be_replaced(self):
        p, settings = self.batch_case()
        t, c, n, l = setup_case(p, **settings)
        mem = Memory(wall_repair_worker=99, wall_repair_delivering=True)
        repair_walls(t, c, mem, n, l, [t.workers[1]], sorted(l.wall_cells))
        self.assertEqual(mem.wall_repair_worker, 2)
        self.assertIn("2", l.commands)

    def test_insufficient_daylight_releases_repair_assignment(self):
        p, settings = self.batch_case()
        p["roundNo"] = 195
        t, c, n, l = setup_case(p, **settings)
        mem = Memory(wall_repair_worker=1)
        self.assertFalse(repair_walls(t, c, mem, n, l, t.workers, sorted(l.wall_cells)))
        self.assertIsNone(mem.wall_repair_worker)
        self.assertFalse(l.commands)

    def test_flank_gap_precedes_unfinished_front_in_default_layout(self):
        p = payload(130, [unit(13, "station", 3, 11),
                          unit(1, "worker", 5, 7, backpack=["stone"]),
                          unit(30, "wall", 6, 8), unit(31, "wall", 4, 8)])
        t, c, n, l = setup_case(p)
        self.assertTrue(build(t, c, Memory(), n, l, t.workers[0],
                              sorted(l.wall_cells), lambda _: "wall"))
        self.assertIn((5, 8), l.build_claims)

    def test_corner_and_multiple_missing_cells_are_gaps_but_gate_is_not(self):
        p = payload(130, [unit(1, "wall", 4, 8, level=2),
                          unit(2, "wall", 6, 10, level=3)])
        sites = [(4, 8), (5, 8), (6, 8), (6, 9), (6, 10), (6, 11)]
        t, c, _, _ = setup_case(p, layout_mode="explicit", wall_cells=sites)
        self.assertEqual(wall_gaps(t, sites), {(5, 8), (6, 8), (6, 9)})
        m = Memory()
        m.observe(t, c)
        self.assertEqual(m.wall_rebuild_levels, {(5, 8): 3, (6, 8): 3, (6, 9): 3})
        self.assertNotIn((5, 9), m.wall_rebuild_levels)

    def repair_case(self, neighbour_level=3):
        p = payload(129, [unit(1, "worker", 5, 7),
                          unit(13, "station", 3, 11, health=1500, level=3),
                          unit(20, "rocket", 5, 10, level=1),
                          unit(30, "wall", 4, 8, health=1000, level=neighbour_level),
                          unit(31, "wall", 5, 8, health=1000, level=neighbour_level),
                          unit(32, "wall", 6, 8, health=1000, level=neighbour_level)])
        p["mapInfo"]["zones"] = [{"neutralType": "weaponShop", "pos": {"x": 4, "y": 7}}]
        p["weaponShopList"] = [{"name": "WallUpgradeVoucher1", "price": 20},
                               {"name": "WallUpgradeVoucher2", "price": 30},
                               {"name": "WeaponUpgradeVoucher1", "price": 50}]
        t, c, _, _ = setup_case(p)
        m = Memory()
        m.observe(t, c)
        m.last_round = 129
        p["roundNo"] = 130
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"] if r["id"] != 31]
        m.observe(Turn(p, c), c)
        return p, c, m

    def test_gap_is_closed_before_carried_weapon_upgrade(self):
        p, c, m = self.repair_case()
        p["teamOur"]["roles"][0]["backpack"] = ["stone", "WeaponUpgradeVoucher1"]
        t, c, n, l = setup_case(p, layout_mode="explicit", loadout=["rocket"],
                              weapon_cells=[[5, 10]], wall_cells=[[4, 8], [5, 8], [6, 8]])
        workers(t, c, m, n, l, [], sorted(l.wall_cells))
        self.assertEqual(l.commands["1"], command("build", (5, 8), name="wall"))

    def test_replacement_buys_and_uses_both_levels_before_weapon_upgrades(self):
        p, c, m = self.repair_case()
        self.assertEqual(m.wall_rebuild_levels[(5, 8)], 3)
        replacement = unit(99, "wall", 5, 8, health=1000)
        p["teamOur"]["roles"].append(replacement)
        for level in (1, 2):
            replacement["level"] = level
            p["roundNo"] += 1
            t, c, n, l = setup_case(p)
            m.observe(t, c)
            name = f"WallUpgradeVoucher{level}"
            self.assertEqual(supplies(t, c, m, n, l, t.workers[0], bulk=True)[0], name)
            p["teamOur"]["roles"][0]["backpack"] = [name, "WeaponUpgradeVoucher1"]
            t, c, n, l = setup_case(p)
            self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=m))
            self.assertEqual(l.commands["1"], command("use", (5, 8), name=name))
            p["teamOur"]["roles"][0]["backpack"] = []
        replacement["level"] = 3
        m.observe(Turn(p, c), c)
        self.assertNotIn((5, 8), m.wall_rebuild_levels)

    def test_level_one_neighbours_do_not_trigger_extra_upgrades(self):
        p, c, m = self.repair_case(neighbour_level=1)
        p["teamOur"]["roles"].append(unit(99, "wall", 5, 8, health=1000))
        m.observe(Turn(p, c), c)
        self.assertFalse(m.wall_rebuild_levels)

    def test_unobserved_destruction_across_round_gap_is_remembered(self):
        p, c, m = self.repair_case()
        p["roundNo"] = 260
        m.observe(Turn(p, c), c)
        self.assertEqual(m.wall_rebuild_levels[(5, 8)], 3)


if __name__ == "__main__":
    unittest.main()
