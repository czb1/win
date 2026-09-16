"""Regressions for decisions that waste economy turns or defensive damage."""
import unittest
from test_agent import payload, unit, setup_case
from agent.model import distance
from agent.combat import select_targets, defend, threat
from agent.economy import worker
from agent.intelligence import Memory


class TargetingRegressionTests(unittest.TestCase):
    def test_large_robot_at_base_outweighs_small_robot(self):
        p = payload(71, roles=[unit(13, "station", 3, 5, health=1500),
                               unit(20, "rocket", 5, 5)])
        p["robot"]["roles"] = [unit(31, "smallRobot", 7, 4, health=20),
                                unit(32, "smallRobot", 7, 3, health=20),
                                unit(33, "largeRobot", 7, 6, health=500)]
        t, _, nav, _ = setup_case(p)
        self.assertGreater(threat(t, t.robots[-1]), threat(t, t.robots[0]))
        self.assertEqual(select_targets(t, t.weapons[0], {}, nav.deadline), [(7, 6)])

    def test_railgun_spends_energy_on_current_health_not_planned_health(self):
        p = payload(71, roles=[unit(1, "worker", 4, 6), unit(20, "railgun", 5, 5)])
        p["robot"]["roles"] = [unit(31, "smallRobot", 7, 5, health=40),
                                unit(32, "largeRobot", 8, 5, health=500)]
        t, _, n, _ = setup_case(p)
        damage = {31: 30}
        select_targets(t, t.weapons[0], damage, n.deadline)
        self.assertEqual(damage[31], 40)
        self.assertEqual(damage.get(32, 0), 0)

    def test_rocket_can_aim_between_robots(self):
        p = payload(71, roles=[unit(20, "rocket", 5, 4)])
        p["robot"]["roles"] = [unit(30+i, "smallRobot", x, y, health=40)
                                for i, (x, y) in enumerate([(7, 3), (9, 3), (7, 5), (9, 5)])]
        t, _, n, _ = setup_case(p)
        damage = {}
        self.assertEqual(select_targets(t, t.weapons[0], damage, n.deadline), [(8, 4)])
        self.assertEqual(sum(damage.values()), 40)

    def test_rocket_splash_reaches_one_cell_beyond_aim_range(self):
        p = payload(71, roles=[unit(20, "rocket", 5, 5, attackRange=3)])
        p["robot"]["roles"] = [unit(31, "smallRobot", 9, 5, health=40)]
        t, _, n, _ = setup_case(p)
        damage = {}
        targets = select_targets(t, t.weapons[0], damage, n.deadline)
        self.assertTrue(targets)
        self.assertTrue(all(distance(t.weapons[0].pos, target) <= 3 for target in targets))
        self.assertEqual(damage[31], 10)

    def test_only_operator_chooses_ready_weapon_over_cooling_weapon(self):
        p = payload(71, roles=[unit(1, "worker", 5, 5),
                              unit(20, "rocket", 4, 5, cooldown=2),
                              unit(30, "railgun", 6, 5)])
        p["robot"]["roles"] = [unit(40, "smallRobot", 9, 5, health=40)]
        t, _, n, l = setup_case(p)
        defend(t, n, l)
        self.assertIn("30", l.commands)


class EconomyRegressionTests(unittest.TestCase):
    def trade_case(self, backpack, gold=0, has_mine=True, tower_sites=None):
        p = payload(roles=[unit(1, "worker", 5, 5, backpack=backpack)])
        p["teamOur"]["goldNum"] = gold
        p["mapInfo"]["zones"] = [{"neutralType": "vendor", "pos": {"x": 5, "y": 6}}]
        if has_mine:
            p["mapInfo"]["zones"].append({"neutralType": "copper", "pos": {"x": 6, "y": 5}})
        p["vendorShopList"] = [{"name": "copper", "price": 5}]
        t, c, n, l = setup_case(p, layout_mode="explicit", weapon_cells=tower_sites or [])
        worker(t, c, Memory(), n, l, t.workers[0], list(map(tuple, tower_sites or [])), [], False)
        return l.commands["1"]

    def test_short_batch_can_fund_missing_tower(self):
        self.assertEqual(self.trade_case(["copper"] * 5, tower_sites=[[9, 9]])["action"], "sell")

    def test_short_batch_that_cannot_fund_tower_keeps_mining(self):
        self.assertEqual(self.trade_case(["copper"], tower_sites=[[9, 9]])["action"], "collect")

    def test_depleted_mines_allow_partial_batch_sale(self):
        self.assertEqual(self.trade_case(["copper"], has_mine=False)["action"], "sell")

    def test_full_batch_sells_even_without_construction(self):
        self.assertEqual(self.trade_case(["copper"] * 40)["action"], "sell")

    def test_zero_gold_does_not_force_one_ore_sale_when_towers_are_complete(self):
        p = payload(roles=[unit(1, "worker", 5, 5, backpack=["copper"]),
                           unit(20, "gatling", 2, 2), unit(30, "railgun", 3, 2),
                           unit(40, "rocket", 4, 2)])
        p["teamOur"]["goldNum"] = 0
        p["mapInfo"]["zones"] = [{"neutralType": "copper", "pos": {"x": 6, "y": 5}},
                                  {"neutralType": "vendor", "pos": {"x": 5, "y": 6}}]
        p["vendorShopList"] = [{"name": "copper", "price": 5}]
        t, c, n, l = setup_case(p, layout_mode="explicit")
        worker(t, c, Memory(), n, l, t.workers[0], [], [], False)
        self.assertEqual(l.commands["1"]["action"], "collect")

    def test_builder_collects_batch_before_leaving_mine(self):
        p = payload(roles=[unit(1, "worker", 2, 2, backpack=["stone"])])
        p["teamOur"]["goldNum"] = 0
        p["mapInfo"]["zones"] = [{"neutralType": "stone", "pos": {"x": 3, "y": 2}}]
        sites = [[10, y] for y in range(5, 11)]
        t, c, n, l = setup_case(p, layout_mode="explicit", wall_cells=sites)
        worker(t, c, Memory(), n, l, t.workers[0], [], list(map(tuple, sites)), True)
        self.assertEqual(l.commands["1"]["action"], "collect")

    def test_failed_wall_sites_do_not_keep_worker_mining_stone(self):
        p = payload(roles=[unit(1, "worker", 2, 2)])
        p["teamOur"]["goldNum"] = 0
        p["mapInfo"]["zones"] = [{"neutralType": "stone", "pos": {"x": 3, "y": 2}},
                                  {"neutralType": "copper", "pos": {"x": 2, "y": 3}},
                                  {"neutralType": "vendor", "pos": {"x": 4, "y": 3}}]
        p["vendorShopList"] = [{"name": "copper", "price": 10}, {"name": "stone", "price": 1}]
        t, c, n, l = setup_case(p, layout_mode="explicit", wall_cells=[[10, 5]])
        worker(t, c, Memory(build_failures={(10, 5): 20}), n, l, t.workers[0], [], [(10, 5)], True)
        self.assertEqual(l.commands["1"].get("targetPos"), [{"x": 2, "y": 3}])

    def test_builder_leaves_mine_when_batch_is_ready(self):
        p = payload(roles=[unit(1, "worker", 2, 2, backpack=["stone"] * 6)])
        p["teamOur"]["goldNum"] = 0
        p["mapInfo"]["zones"] = [{"neutralType": "stone", "pos": {"x": 3, "y": 2}}]
        sites = [[10, y] for y in range(5, 11)]
        t, c, n, l = setup_case(p, layout_mode="explicit", wall_cells=sites)
        worker(t, c, Memory(), n, l, t.workers[0], [], list(map(tuple, sites)), True)
        self.assertEqual(l.commands["1"]["action"], "move")


if __name__ == "__main__":
    unittest.main()
