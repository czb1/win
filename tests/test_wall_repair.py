"""Deterministic breach repair and replacement-level recovery."""
import unittest

from test_agent import payload, setup_case, unit
from agent.commands import command
from agent.economy import build, supplies, use_inventory, worker
from agent.intelligence import Memory
from agent.model import Turn
from agent.navigation import wall_gaps


class WallRepairTests(unittest.TestCase):
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
        worker(t, c, m, n, l, t.workers[0], [], sorted(l.wall_cells), True)
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
