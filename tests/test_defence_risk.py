"""Third-night flank reinforcement and downloadable battle diagnostics."""
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.config import Config
from agent.economy import build, exposed_wall, supplies
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
        turn, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[])
        mem = Memory()
        self.assertTrue(exposed_wall(turn, turn.ours[-1], mem))
        self.assertEqual(supplies(turn, cfg, mem, nav, ledger, turn.workers[0], bulk=True)[0],
                         "WeaponUpgradeVoucher2")
        p = self.case(gun_level=1)
        turn, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[])
        self.assertEqual(supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)[0],
                         "WeaponUpgradeVoucher1")
        p = self.case(gun_level=3)
        turn, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[])
        self.assertEqual(supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)[0],
                         "WallUpgradeVoucher1")


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
