"""Regressions for wasted trips, forgotten nearby ore and the first wave."""
import copy
import unittest

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.commands import command
from agent.economy import finish_preparation
from agent.intelligence import Memory
from agent.mining import earn, mine
from agent.model import Turn


def mining_case(rno=10, inventory=(), zones=None, workers=None):
    p = payload(rno, workers or [unit(1, "worker", 5, 5, health=220, backpack=list(inventory))])
    p["mapInfo"]["zones"] = [{"neutralType": k, "pos": {"x": x, "y": y}}
                              for k, x, y in (zones or [("iron", 6, 5), ("copper", 14, 5), ("vendor", 4, 7)])]
    p["vendorShopList"] = [{"name": "iron", "price": 6}, {"name": "copper", "price": 10}]
    return p


class MiningRunTests(unittest.TestCase):
    def test_nearby_iron_beats_a_long_walk_to_expensive_copper(self):
        t, cfg, nav, ledger = setup_case(mining_case())
        mine(t, cfg, Memory(), nav, ledger, t.workers[0], deadline=40)
        self.assertEqual(ledger.commands["1"], command("collect", (6, 5)))

    def test_small_price_change_does_not_interrupt_current_deposit(self):
        p = mining_case(zones=[("iron", 6, 5), ("copper", 5, 6), ("vendor", 4, 7)])
        p["vendorShopList"][1]["price"] = 7
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(mine_targets={1: (6, 5)}, mine_collected={(6, 5): 9})
        mine(t, cfg, mem, nav, ledger, t.workers[0])
        self.assertEqual(ledger.commands["1"], command("collect", (6, 5)))

    def test_real_price_collapse_can_release_a_mining_commitment(self):
        p = mining_case(zones=[("iron", 6, 5), ("copper", 5, 6), ("vendor", 4, 7)])
        p["vendorShopList"][0]["price"] = 1
        t, cfg, nav, ledger = setup_case(p)
        mine(t, cfg, Memory(mine_targets={1: (6, 5)}), nav, ledger, t.workers[0])
        self.assertEqual(ledger.commands["1"], command("collect", (5, 6)))

    def test_two_workers_split_equivalent_deposits(self):
        p = mining_case(zones=[("copper", 8, 4), ("copper", 8, 6), ("vendor", 3, 5)],
                        workers=[unit(1, "worker", 5, 4), unit(2, "worker", 5, 6)])
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory()
        for h in t.workers:
            mine(t, cfg, mem, nav, ledger, h)
        self.assertEqual(len(set(mem.mine_targets.values())), 2)

    def test_distant_worker_does_not_chase_another_workers_last_ore(self):
        p = mining_case(zones=[("copper", 8, 4), ("iron", 5, 7), ("vendor", 3, 5)],
                        workers=[unit(1, "worker", 7, 4), unit(2, "worker", 5, 6)])
        t, cfg, nav, ledger = setup_case(p)
        mem = Memory(mine_targets={1: (8, 4)}, mine_collected={(8, 4): 9})
        mine(t, cfg, mem, nav, ledger, t.workers[1])
        self.assertEqual(ledger.commands["2"], command("collect", (5, 7)))

    def test_fourteen_ore_does_not_trigger_an_extra_sale_trip(self):
        t, cfg, nav, ledger = setup_case(mining_case(inventory=["iron"] * 14))
        earn(t, cfg, Memory(), nav, ledger, t.workers[0], deadline=40)
        self.assertEqual(ledger.commands["1"]["action"], "collect")

    def test_full_backpack_still_returns_to_sell(self):
        t, cfg, nav, ledger = setup_case(mining_case(inventory=["iron"] * 100))
        mem = Memory()
        earn(t, cfg, mem, nav, ledger, t.workers[0], deadline=40)
        self.assertEqual(ledger.commands["1"]["action"], "move")
        self.assertIn(1, mem.sale_workers)

    def test_mixed_load_reserves_one_sale_action_per_kind(self):
        p = mining_case(38, ["copper", "iron"],
                        zones=[("iron", 6, 5), ("vendor", 4, 5)])
        t, cfg, nav, ledger = setup_case(p)
        earn(t, cfg, Memory(), nav, ledger, t.workers[0], deadline=40)
        self.assertEqual(ledger.commands["1"], command("sell", name="copper", num=1))

    def test_no_new_remote_mine_trip_without_time_to_cash_out(self):
        p = mining_case(38, zones=[("copper", 14, 5), ("vendor", 4, 5)])
        t, cfg, nav, ledger = setup_case(p)
        self.assertFalse(mine(t, cfg, Memory(), nav, ledger, t.workers[0], deadline=40))
        self.assertFalse(ledger.commands)

    def test_unverified_model_outage_does_not_override_observed_prices(self):
        p = mining_case()
        results = []
        for outages in ([], [{"name": "iron", "startDay": 1, "endDay": 10}]):
            t, cfg, nav, ledger = setup_case(p)
            mine(t, cfg, Memory(outages=outages), nav, ledger, t.workers[0])
            results.append(ledger.commands)
        self.assertEqual(*results)

    def test_disappeared_deposit_releases_target_and_estimate(self):
        t, cfg, _, _ = setup_case(mining_case(zones=[("copper", 14, 5), ("vendor", 4, 5)]))
        mem = Memory(day=t.day, mine_targets={1: (6, 5)}, mine_kinds={(6, 5): "iron"},
                     mine_collected={(6, 5): 9})
        mem.observe(t, cfg)
        self.assertFalse(mem.mine_targets)
        self.assertFalse(mem.mine_collected)

    def test_failed_collection_is_retried_after_cooldown(self):
        p = mining_case()
        t, cfg, _, _ = setup_case(p)
        mem = Memory(day=1, last_round=9, mine_targets={1: (6, 5)}, mine_kinds={(6, 5): "iron"},
                     last_commands={"1": command("collect", (6, 5))})
        p["lastRoundRoleActionResults"] = {"1": False}
        mem.observe(Turn(p, cfg), cfg)
        self.assertNotIn(1, mem.mine_targets)
        self.assertIn((6, 5), mem.collect_failures)
        p.update(roundNo=15, lastRoundRoleActionResults={})
        mem.observe(Turn(p, cfg), cfg)
        self.assertNotIn((6, 5), mem.collect_failures)

    def test_large_visible_mine_set_still_produces_both_worker_actions(self):
        p = mining_case(workers=[unit(1, "worker", 5, 5), unit(2, "worker", 5, 7)])
        p["mapInfo"].update(width=100, height=100)
        p["mapInfo"]["zones"] += [{"neutralType": "copper", "pos": {"x": x, "y": y}}
                                  for x in range(20, 95, 5) for y in range(20, 95, 5)]
        result = Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(p)
        self.assertEqual(set(result["roleCommandMap"]), {"1", "2"})

    def test_far_second_vendor_does_not_shut_down_local_mining(self):
        p = mining_case(30)
        p["mapInfo"].update(width=100, height=100)
        p["mapInfo"]["zones"].append({"neutralType": "vendor", "pos": {"x": 90, "y": 90}})
        p["teamOur"]["roles"].append(unit(13, "station", 3, 9))
        t, cfg, nav, ledger = setup_case(p)
        self.assertTrue(mine(t, cfg, Memory(), nav, ledger, t.workers[0], deadline=40))
        self.assertEqual(ledger.commands["1"], command("collect", (6, 5)))


class FirstWaveTests(unittest.TestCase):
    def test_explicit_one_based_compatibility(self):
        self.assertTrue(Turn(payload(70), Config(round_origin=1)).is_day)
        self.assertFalse(Turn(payload(71), Config(round_origin=1)).is_day)

    def test_wave_seventy_can_fire_immediately(self):
        p = payload(70, [unit(1, "worker", 4, 6), unit(20, "rocket", 5, 5)])
        p["robot"]["roles"] = [unit(90, "smallRobot", 7, 5, health=40, targetTeam="challenger")]
        result = Agent(Config(llm_enabled=False)).decide(p)
        self.assertEqual(result["roleCommandMap"]["20"]["action"], "attack")

    def test_first_wave_recall_does_not_wait_for_robots_or_llm(self):
        p = payload(63, [unit(11, "pioneer", 12, 5), unit(20, "rocket", 5, 5),
                         unit(13, "station", 3, 7)])
        p["phaseTask"] = "slow unresolved task"
        agent = Agent(Config(layout_mode="explicit"))
        result = agent.decide(p)
        self.assertEqual(result["roleCommandMap"]["11"]["action"], "move")
        self.assertFalse(result["prompt"])
        self.assertEqual(next(iter(agent.sessions.values())).stop_reason, "first_wave_deadline")

    def test_last_local_wall_finishes_only_with_time_to_return(self):
        p = payload(64, [unit(1, "worker", 9, 9, backpack=["stone"]),
                         unit(20, "rocket", 7, 9), unit(13, "station", 4, 9)])
        t, cfg, nav, ledger = setup_case(p, layout_mode="explicit", wall_cells=[[8, 10]])
        finish_preparation(t, cfg, Memory(), nav, ledger, t.workers[0], t.weapons[0], [(8, 10)])
        self.assertEqual(ledger.commands["1"], command("build", (8, 10), name="wall"))
        late = copy.deepcopy(p)
        late["roundNo"] = 69
        t, cfg, nav, ledger = setup_case(late, layout_mode="explicit", wall_cells=[[8, 10]])
        self.assertFalse(finish_preparation(t, cfg, Memory(), nav, ledger, t.workers[0], t.weapons[0], [(8, 10)]))


if __name__ == "__main__":
    unittest.main()
