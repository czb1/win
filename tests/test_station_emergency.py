"""Paid base healing must remain executable during wall-level recovery."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import use_inventory
from agent.intelligence import Memory
from agent.worker_jobs import resume_daytime_jobs


class StationEmergencyTests(unittest.TestCase):
    settings = dict(layout_mode="explicit", loadout=["rocket"],
                    weapon_cells=[[5, 10]], wall_cells=[[5, 8]], llm_enabled=False)

    def case(self, round_no=391, station_level=1, health=100, worker_pos=(2, 10),
             backpack=None):
        if backpack is None:
            backpack = [f"StationUpgradeVoucher{station_level}"]
        p = payload(round_no, [unit(13, "station", 3, 11, health=health, level=station_level),
                               unit(10, "worker", *worker_pos, health=500, backpack=backpack),
                               unit(20, "rocket", 5, 10, health=2000, level=3),
                               unit(30, "wall", 5, 8, health=1000)])
        p["teamOur"]["goldNum"] = 0
        return p

    def setup(self, p):
        return setup_case(p, **self.settings)

    def test_zero_gold_and_no_wall_voucher_do_not_block_paid_base_healing(self):
        for round_no in (131, 391, 461):
            for station_level in (1, 2):
                for missing in (False, True):
                    with self.subTest(round_no=round_no, level=station_level, missing=missing):
                        p = self.case(round_no, station_level)
                        if missing:
                            p["teamOur"]["roles"].pop()
                        turn, _, nav, ledger = self.setup(p)
                        mem = Memory(wall_rebuild_levels={(5, 8): 3})
                        self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[0],
                                                      local_only=True, urgent_only=True, mem=mem))
                        self.assertEqual(ledger.commands["10"], command(
                            "use", (3, 11), name=f"StationUpgradeVoucher{station_level}"))
                        self.assertEqual(ledger.gold, 0)
                        self.assertEqual(mem.wall_rebuild_levels, {(5, 8): 3})

    def test_critical_base_precedes_an_executable_replacement_wall_upgrade(self):
        for round_no in (131, 391, 450, 459):
            with self.subTest(round_no=round_no):
                p = self.case(round_no, worker_pos=(4, 9),
                              backpack=["StationUpgradeVoucher1", "WallUpgradeVoucher1"])
                turn, _, nav, ledger = self.setup(p)
                mem = Memory(wall_rebuild_levels={(5, 8): 3},
                             upgrade_targets={10: (5, 8)})
                self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[0], mem=mem))
                self.assertEqual(ledger.commands["10"], command(
                    "use", (3, 11), name="StationUpgradeVoucher1"))
                self.assertEqual(ledger.upgrade_claims, {13})
                self.assertEqual(mem.wall_rebuild_levels, {(5, 8): 3})

    def test_noncritical_base_still_waits_for_wall_recovery(self):
        for round_no, health in ((131, 750), (391, 375), (391, 1500)):
            with self.subTest(round_no=round_no, health=health):
                p = self.case(round_no, health=health, worker_pos=(4, 9))
                turn, _, nav, ledger = self.setup(p)
                mem = Memory(wall_rebuild_levels={(5, 8): 3})
                self.assertFalse(use_inventory(turn, nav, ledger, turn.workers[0], mem=mem))
                self.assertFalse(ledger.commands)
                p["teamOur"]["roles"][1]["backpack"].append("WallUpgradeVoucher1")
                turn, _, nav, ledger = self.setup(p)
                self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[0], mem=mem))
                self.assertEqual(ledger.commands["10"], command(
                    "use", (5, 8), name="WallUpgradeVoucher1"))

    def test_emergency_does_not_bypass_voucher_level_or_claim_checks(self):
        for station_level, backpack, claimed in ((1, [], False),
                                                (1, ["StationUpgradeVoucher2"], False),
                                                (2, ["StationUpgradeVoucher1"], False),
                                                (3, ["StationUpgradeVoucher2"], False),
                                                (1, ["StationUpgradeVoucher1"], True)):
            with self.subTest(level=station_level, backpack=backpack, claimed=claimed):
                p = self.case(station_level=station_level, backpack=backpack)
                turn, _, nav, ledger = self.setup(p)
                if claimed:
                    ledger.upgrade_claims.add(13)
                self.assertFalse(use_inventory(turn, nav, ledger, turn.workers[0],
                                               mem=Memory(wall_rebuild_levels={(5, 8): 3})))
                self.assertFalse(ledger.commands)

    def test_unreachable_base_does_not_stall_a_paid_wall_upgrade(self):
        p = self.case(worker_pos=(5, 7),
                      backpack=["StationUpgradeVoucher1", "WallUpgradeVoucher1"])
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"] if r["roleType"] != "rocket"]
        station_cells = {(3, 10), (4, 10), (3, 11), (4, 11)}
        ring = {(x, y) for x in range(2, 6) for y in range(9, 13)} - station_cells
        p["teamEnemy"]["roles"] = [unit(100 + i, "wall", *cell, health=1000)
                                     for i, cell in enumerate(sorted(ring))]
        turn, _, nav, ledger = self.setup(p)
        self.assertIsNone(nav.approach(turn.workers[0], turn.station.cells))
        self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[0],
                                      mem=Memory(wall_rebuild_levels={(5, 8): 3})))
        self.assertEqual(ledger.commands["10"], command(
            "use", (5, 8), name="WallUpgradeVoucher1"))

    def test_infeasible_dusk_base_delivery_does_not_stall_local_wall_work(self):
        p = self.case(459, worker_pos=(5, 7),
                      backpack=["StationUpgradeVoucher1", "WallUpgradeVoucher1"])
        turn, _, nav, ledger = self.setup(p)
        ledger.operator_posts[10] = (5, 7)
        self.assertTrue(use_inventory(turn, nav, ledger, turn.workers[0],
                                      mem=Memory(wall_rebuild_levels={(5, 8): 3})))
        self.assertEqual(ledger.commands["10"], command(
            "use", (5, 8), name="WallUpgradeVoucher1"))

    def test_base_healing_preempts_an_existing_wall_delivery(self):
        p = self.case(worker_pos=(4, 9),
                      backpack=["StationUpgradeVoucher1", "WallUpgradeVoucher1"])
        turn, cfg, nav, ledger = self.setup(p)
        mem = Memory(wall_rebuild_levels={(5, 8): 3}, daytime_jobs={
            10: dict(kind="use", target=(5, 8), name="WallUpgradeVoucher1")})
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, [(5, 10)], [(5, 8)], returning=set())
        self.assertEqual(ledger.commands["10"], command(
            "use", (3, 11), name="StationUpgradeVoucher1"))
        self.assertEqual(mem.wall_rebuild_levels, {(5, 8): 3})

    def test_agent_heals_with_an_observed_destroyed_and_rebuilt_wall(self):
        agent = Agent(Config(**self.settings))
        p = self.case(389, health=1500, backpack=[])
        p["teamOur"]["roles"][-1]["level"] = 3
        agent.decide(copy.deepcopy(p))
        p["roundNo"] = 390
        p["teamOur"]["roles"].pop()
        agent.decide(copy.deepcopy(p))
        p["roundNo"] = 391
        p["teamOur"]["roles"][0]["health"] = 100
        p["teamOur"]["roles"][1]["backpack"] = ["StationUpgradeVoucher1"]
        p["teamOur"]["roles"].append(unit(99, "wall", 5, 8, health=1000))
        response = agent.decide(copy.deepcopy(p))
        self.assertEqual(response["roleCommandMap"].get("10"), command(
            "use", (3, 11), name="StationUpgradeVoucher1"))
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.wall_rebuild_levels, {(5, 8): 3})
        self.assertEqual(agent.decide(copy.deepcopy(p)), response)


if __name__ == "__main__":
    unittest.main()
