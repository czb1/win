"""Third-night flank reinforcement and downloadable battle diagnostics."""
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.config import Config
from agent.economy import build, exposed_wall, supplies, workers
from agent.intelligence import Memory
from agent.model import Turn


class WallRiskTests(unittest.TestCase):
    def test_rebuild_attacked_flank_before_unattacked_front(self):
        walls = [(9, 7), (9, 8), (6, 4), (6, 12)]
        p = payload(260, [unit(1, "worker", 7, 8, backpack=["stone"]),
                          unit(13, "station", 3, 9), unit(30, "wall", 9, 7),
                          unit(31, "wall", 6, 4)])
        turn, cfg, nav, ledger = setup_case(p, layout_mode="explicit",
                                            wall_cells=[list(w) for w in walls])
        mem = Memory(wall_hits={(6, 12): 3})
        self.assertTrue(build(turn, cfg, mem, nav, ledger, turn.workers[0], walls, lambda _: "wall"))
        self.assertEqual(mem.build_targets[1], (6, 12))

    def test_wall_hit_persists_into_next_day(self):
        mem, cfg = Memory(), Config()
        p = payload(210, [unit(13, "station", 3, 9), unit(30, "wall", 9, 8, health=1000)])
        mem.observe(Turn(p, cfg), cfg)
        mem.last_round = 210
        p["roundNo"] = 211
        p["teamOur"]["roles"][1]["health"] = 750
        mem.observe(Turn(p, cfg), cfg)
        self.assertEqual(mem.wall_hits[(9, 8)], 1)
        mem.last_round = 211
        p["roundNo"] = 260
        mem.observe(Turn(p, cfg), cfg)
        self.assertEqual(mem.wall_hits[(9, 8)], 1)

    def case(self, round_no=260, gun_level=2):
        p = payload(round_no, [unit(1, "worker", 6, 5), unit(13, "station", 3, 9, level=3),
                               unit(20, "rocket", 5, 8, level=gun_level),
                               unit(21, "rocket", 6, 8, level=gun_level),
                               unit(22, "rocket", 7, 8, level=gun_level),
                               unit(30, "wall", 9, 8, health=1000),
                               unit(31, "wall", 7, 4, health=1000),
                               unit(32, "wall", 7, 12, health=1000)])
        p["teamOur"]["goldNum"] = 200
        p["mapInfo"]["zones"] = [{"neutralType": "weaponShop", "pos": {"x": 6, "y": 6}}]
        p["weaponShopList"] = [{"name": "WeaponUpgradeVoucher1", "price": 100},
                               {"name": "WeaponUpgradeVoucher2", "price": 150},
                               {"name": "WallUpgradeVoucher1", "price": 20}]
        return p

    def test_three_sides_upgrade_after_weapon_funding(self):
        p = self.case()
        turn, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[], wall_cells=[[9,8],[7,4],[7,12]])
        mem = Memory()
        self.assertTrue(exposed_wall(turn, turn.ours[-1], mem))
        self.assertEqual(supplies(turn, cfg, mem, nav, ledger, turn.workers[0], bulk=True)[0],
                         "WeaponUpgradeVoucher2")
        p = self.case(gun_level=1)
        turn, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[], wall_cells=[[9,8],[7,4],[7,12]])
        self.assertEqual(supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)[0],
                         "WeaponUpgradeVoucher1")
        p = self.case(gun_level=3)
        turn, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[], wall_cells=[[9,8],[7,4],[7,12]])
        self.assertEqual(supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)[0],
                         "WallUpgradeVoucher1")


class DayMaintenanceTests(unittest.TestCase):
    def case(self, round_no=520, gap=False, backpack=(), gold=53):
        roles = [unit(1, "worker", 8, 9, backpack=list(backpack)),
                 unit(13, "station", 3, 9, level=3, health=1130),
                 unit(20, "rocket", 5, 8, level=3),
                 unit(21, "rocket", 6, 8, level=3),
                 unit(22, "rocket", 7, 8, level=3),
                 unit(30, "wall", 9, 8, level=2, health=75),
                 unit(32, "wall", 9, 10, level=2, health=240)]
        if not gap:
            roles.append(unit(31, "wall", 9, 9, health=1000))
        p = payload(round_no, roles)
        p["teamOur"]["goldNum"] = gold
        p["mapInfo"]["zones"] = [
            {"neutralType": "weaponShop", "pos": {"x": 7, "y": 9}},
            {"neutralType": "vendor", "pos": {"x": 5, "y": 5}},
            {"neutralType": "copper", "pos": {"x": 11, "y": 5}},
            {"neutralType": "stone", "pos": {"x": 7, "y": 10}}]
        p["vendorShopList"] = [{"name": "copper", "price": 10}]
        p["weaponShopList"] = [{"name": "WallFixer", "price": 10},
                               {"name": "WallUpgradeVoucher2", "price": 30}]
        return setup_case(p, layout_mode="explicit", wall_cells=[[9,8],[9,9],[9,10]],
                          weapon_cells=[[5,8],[6,8],[7,8]])

    def decide(self, case):
        turn, cfg, nav, ledger = case
        workers(turn, cfg, Memory(initial_walls_complete=True), nav, ledger, list(ledger.tower_cells),
                sorted(ledger.wall_cells))
        return ledger.commands.get("1", {})

    def test_single_survivor_closes_breach_at_dawn(self):
        c = self.decide(self.case(gap=True, backpack=["stone"]))
        self.assertEqual((c["action"], c["name"]), ("build", "wall"))
        self.assertEqual(c["targetPos"], [{"x":9, "y":9}])

    def test_breach_without_material_starts_stone_collection_at_dawn(self):
        c = self.decide(self.case(gap=True))
        self.assertEqual(c["action"], "collect")
        self.assertEqual(c["targetPos"], [{"x":7, "y":10}])

    def test_after_breach_closed_buys_upgrade_before_repairs(self):
        c = self.decide(self.case())
        self.assertEqual((c["action"], c["name"], c["num"]), ("buy", "WallUpgradeVoucher2", 1))

    def test_repair_budget_is_sufficient_with_only_twenty_gold(self):
        c = self.decide(self.case(gold=20))
        self.assertEqual((c["name"], c["num"]), ("WallFixer", 2))

    def test_carried_upgrade_takes_precedence_over_repair(self):
        c = self.decide(self.case(backpack=["WallFixer", "WallUpgradeVoucher2"]))
        self.assertEqual((c["action"], c["name"]), ("use", "WallUpgradeVoucher2"))
        self.assertEqual(c["targetPos"], [{"x":9, "y":8}])

    def test_remaining_wall_is_repaired_after_first_wall_heals(self):
        turn, cfg, nav, ledger = self.case(backpack=["WallFixer", "WallFixer"])
        from dataclasses import replace
        turn.ours = tuple(replace(w, level=3, health=2000 if w.id == 30 else w.health) if w.kind == "wall" else w for w in turn.ours)
        c = self.decide((turn, cfg, nav, ledger))
        self.assertEqual((c["action"], c["name"]), ("use", "WallFixer"))
        self.assertEqual(c["targetPos"], [{"x":9, "y":10}])

    def test_other_purchases_cannot_spend_repair_reserve(self):
        turn, cfg, nav, ledger = self.case(gold=35)
        from dataclasses import replace
        turn.ours = tuple(replace(w, level=2) if w.id == 20 else w for w in turn.ours)
        turn.shop["WeaponUpgradeVoucher2"] = 30
        self.assertIsNone(supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True))

    def test_gap_precedes_both_carried_upgrade_and_repair(self):
        c = self.decide(self.case(gap=True, backpack=["stone", "WallFixer", "WallUpgradeVoucher2"]))
        self.assertEqual((c["action"], c["name"]), ("build", "wall"))

    def test_lower_level_wall_upgrade_precedes_higher_level(self):
        case = self.case(backpack=["WallUpgradeVoucher2"])
        case[0].shop["WallUpgradeVoucher1"] = 20
        c = self.decide(case)
        self.assertEqual((c["action"], c["name"]), ("buy", "WallUpgradeVoucher1"))

    def test_upgraded_full_health_walls_do_not_consume_repairs(self):
        from dataclasses import replace
        from agent.economy import maintain_walls
        turn, cfg, nav, ledger = self.case(backpack=["WallFixer"] * 100)
        turn.ours = tuple(replace(w, level=3, health=2000) if w.kind == "wall" else w for w in turn.ours)
        maintain_walls(turn, cfg, Memory(), nav, ledger, turn.workers, list(ledger.wall_cells))
        self.assertFalse(ledger.commands)

    def test_late_upgrade_purchase_falls_back_to_carried_repair(self):
        c = self.decide(self.case(round_no=588, backpack=["WallFixer", "WallFixer"]))
        self.assertEqual((c["action"], c["name"]), ("use", "WallFixer"))

    def test_healthy_low_level_front_wall_is_still_upgraded(self):
        from dataclasses import replace
        case = self.case()
        turn = case[0]
        turn.ours = tuple(replace(w, health=1500) if w.kind == "wall" else w for w in turn.ours)
        c = self.decide(case)
        self.assertEqual((c["action"], c["name"]), ("buy", "WallUpgradeVoucher2"))

    def test_first_day_does_not_enter_emergency_maintenance(self):
        from agent.economy import maintain_walls
        turn, cfg, nav, ledger = self.case(round_no=0)
        maintain_walls(turn, cfg, Memory(), nav, ledger, turn.workers, list(ledger.wall_cells))
        self.assertFalse(ledger.commands)


class BattleDiagnosticsTests(unittest.TestCase):
    def test_third_night_reports_wall_levels_cooldown_and_operator(self):
        p = payload(337, [unit(13, "station", 3, 9, health=1410),
                          unit(1, "worker", 5, 7), unit(20, "rocket", 5, 8, cooldown=2),
                          unit(30, "wall", 9, 8, level=2, health=700)])
        p["robot"]["roles"] = [unit(50, "smallRobot", 10, 8, targetTeam="challenger")]
        with self.assertLogs("agent.brain", "INFO") as captured:
            Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(p)
        line = next(line for line in captured.output if "battle_state=" in line)
        self.assertIn('"baseHealth":1410', line)
        self.assertIn('"level":2', line)
        self.assertIn('"reason":"cooldown"', line)
        self.assertIn('"operator":1', line)


if __name__ == "__main__":
    unittest.main()
