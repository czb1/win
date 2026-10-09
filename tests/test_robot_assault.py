"""Fast regression checks for fully upgraded defenses and robot assaults."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent, defenses_maxed, observe_robot_summons, summon_best_robot
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.model import distance, pos
from agent.navigation import DeadlineExceeded, layout
from agent.robot_assault import (RobotAssaultMemory, act_robots,
                                 choose_summon_position, enemy_entries, weakest_approach)


WALLS = [(6, 9), (6, 10), (7, 11)]
TOWERS = [(2, 9), (3, 9), (2, 8)]


def fortified_payload(round_no=260, backpack=None):
    roles = [unit(13, "station", 3, 11),
             unit(1, "worker", 4, 8, backpack=backpack or []),
             unit(2, "pioneer", 4, 7)]
    roles += [unit(20 + i, kind, *p, level=3)
              for i, (kind, p) in enumerate(zip(("rocket", "rocket", "gatling"), TOWERS))]
    roles += [unit(30 + i, "wall", *p, level=3, health=2000)
              for i, p in enumerate(WALLS)]
    data = payload(round_no, roles)
    data["teamEnemy"]["roles"] = [unit(99, "station", 10, 4)]
    return data


def fortified_case(data=None):
    return setup_case(data or fortified_payload(), layout_mode="explicit",
                      wall_cells=[list(p) for p in WALLS],
                      weapon_cells=[list(p) for p in TOWERS])


def controlled_robot(uid=30000, x=8, y=4, **fields):
    # RobotRole omits building levels, backpack, range and power in v2.0.
    return {"id": uid, "roleType": "bossRobot", "pos": {"x": x, "y": y},
            "health": 800, "abnormalState": "", "targetTeam": "defender", **fields}


def assault_payload(round_no=70, robot=None):
    data = payload(round_no, [unit(13, "station", 3, 11)])
    data["teamEnemy"]["roles"] = [unit(99, "station", 10, 4)]
    data["teamOur"]["summonRobotList"] = [robot or controlled_robot()]
    return data


def placement_payload():
    data = assault_payload()
    data["teamOur"]["summonRobotList"] = []
    return data


def enemy_ring(data, *, gap=None, weak=None, weak_level=1, weak_hp=1500):
    # The 2x2 base at (10,4) has a complete distance-two defensive perimeter.
    cells = [(x, y) for x in range(8, 14) for y in range(1, 7)
             if x in (8, 13) or y in (1, 6)]
    data["teamEnemy"]["roles"] += [
        unit(100 + i, "wall", *p, level=weak_level if p == weak else 3,
             health=weak_hp if p == weak else 2000)
        for i, p in enumerate(cells) if p != gap]
    return data


class DefenseGateTests(unittest.TestCase):
    def ready(self, data):
        turn, cfg, _, _ = fortified_case(data)
        towers, walls = layout(turn, cfg)
        return defenses_maxed(turn, cfg, towers, walls)

    def test_all_planned_walls_and_turrets_maxed_enable_robot(self):
        # The user's gate names walls and turrets; the base may remain level 1.
        self.assertTrue(self.ready(fortified_payload()))

    def test_any_unfinished_wall_or_turret_keeps_gate_closed(self):
        for uid in (20, 21, 22, 30, 31, 32):
            with self.subTest(uid=uid):
                data = fortified_payload()
                next(r for r in data["teamOur"]["roles"] if r["id"] == uid)["level"] = 2
                self.assertFalse(self.ready(data))

    def test_missing_or_destroyed_planned_defense_keeps_gate_closed(self):
        for uid in (20, 30):
            for destroyed in (False, True):
                with self.subTest(uid=uid, destroyed=destroyed):
                    data = fortified_payload()
                    roles = data["teamOur"]["roles"]
                    if destroyed:
                        next(r for r in roles if r["id"] == uid)["health"] = 0
                    else:
                        data["teamOur"]["roles"] = [r for r in roles if r["id"] != uid]
                    self.assertFalse(self.ready(data))

    def test_unplanned_unfinished_wall_also_keeps_gate_closed(self):
        data = fortified_payload()
        data["teamOur"]["roles"].append(unit(40, "wall", 7, 9, level=1))
        self.assertFalse(self.ready(data))

    def test_extra_maxed_turret_does_not_replace_missing_planned_turret(self):
        data = fortified_payload()
        data["teamOur"]["roles"] = [r for r in data["teamOur"]["roles"] if r["id"] != 20]
        data["teamOur"]["roles"].append(unit(23, "rocket", 1, 5, level=3))
        self.assertFalse(self.ready(data))

    def test_issued_upgrade_does_not_count_before_observed_level_changes(self):
        data = fortified_payload(backpack=["WeaponUpgradeVoucher2"])
        tower = next(r for r in data["teamOur"]["roles"] if r["id"] == 21)
        tower["level"] = 2
        turn, cfg, _, ledger = fortified_case(data)
        self.assertTrue(ledger.add(1, command("use", (3, 9), name="WeaponUpgradeVoucher2")))
        towers, walls = layout(turn, cfg)
        self.assertFalse(defenses_maxed(turn, cfg, towers, walls))


class RobotOwnershipTests(unittest.TestCase):
    def test_only_private_control_list_grants_commands(self):
        data = assault_payload(robot=controlled_robot(targetTeam="challenger"))
        data["robot"]["roles"] = [controlled_robot(targetTeam="challenger"),
                                  controlled_robot(31000, 9, 6)]
        turn, _, _, ledger = setup_case(data, layout_mode="explicit")
        self.assertEqual([r.id for r in turn.summon_robots], [30000])
        self.assertIn(30000, turn.units)
        self.assertNotIn(31000, turn.units)
        self.assertIn((8, 4), turn.blocked)
        self.assertFalse(turn.threatens_us(turn.summon_robots[0]))
        self.assertFalse(ledger.add(31000, command("move", (8, 6))))

    def test_public_target_team_never_grants_ownership(self):
        data = assault_payload()
        data["teamOur"]["summonRobotList"] = []
        data["robot"]["roles"] = [controlled_robot(targetTeam="defender")]
        turn, _, _, ledger = setup_case(data, layout_mode="explicit")
        self.assertFalse(turn.summon_robots)
        self.assertFalse(ledger.add(30000, command("attack", (10, 4))))

    def test_dead_controlled_robot_is_not_an_actor_or_obstacle(self):
        data = assault_payload(robot=controlled_robot(health=0))
        turn, _, _, ledger = setup_case(data, layout_mode="explicit")
        self.assertFalse(turn.summon_robots)
        self.assertNotIn((8, 4), turn.blocked)
        self.assertFalse(ledger.add(30000, command("move", (7, 4))))


class BestRobotSummoningTests(unittest.TestCase):
    def shop_data(self, gold=120, backpack=None, round_no=260):
        data = fortified_payload(round_no, backpack)
        data["teamOur"]["goldNum"] = gold
        data["mapInfo"]["zones"] = [{"neutralType": "weaponShop", "pos": {"x": 4, "y": 9}}]
        data["weaponShopList"] = [
            {"name": "SmallRobotSummonOrder", "price": 15},
            {"name": "MiddleRobotSummonOrder", "price": 20},
            {"name": "LargeRobotSummonOrder", "price": 70},
            {"name": "BossRobotSummonOrder", "price": 120}]
        return data

    def summon(self, data, mem=None, excluded=(), gold=None):
        turn, cfg, nav, ledger = fortified_case(data)
        if gold is not None:
            ledger.gold = gold
        mem = mem or Memory()
        towers, walls = layout(turn, cfg)
        result = summon_best_robot(turn, cfg, mem, nav, ledger, towers, walls,
                                   excluded=excluded)
        return result, turn, mem, ledger

    def test_buy_only_boss_when_all_tiers_are_affordable(self):
        result, _, _, ledger = self.summon(self.shop_data())
        self.assertTrue(result)
        self.assertEqual(ledger.commands["1"],
                         {"action": "buy", "name": "BossRobotSummonOrder", "num": 1})
        self.assertEqual(ledger.gold, 0)

    def test_buy_large_robot_when_boss_is_unaffordable(self):
        result, _, _, ledger = self.summon(self.shop_data(gold=119))
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1'], command('buy', name='LargeRobotSummonOrder', num=1))
        self.assertEqual(ledger.gold, 49)

    def test_all_tier_price_boundaries_and_insufficient_money(self):
        for gold,name,price in ((120,'BossRobotSummonOrder',120),
                                (70,'LargeRobotSummonOrder',70),
                                (69,'MiddleRobotSummonOrder',20),
                                (20,'MiddleRobotSummonOrder',20),
                                (19,'SmallRobotSummonOrder',15),
                                (15,'SmallRobotSummonOrder',15),
                                (14,None,0)):
            with self.subTest(gold=gold):
                result,_,_,ledger=self.summon(self.shop_data(gold=gold))
                if name:
                    self.assertTrue(result)
                    self.assertEqual(ledger.commands['1'],command('buy',name=name,num=1))
                else:
                    self.assertFalse(result)
                    self.assertFalse(ledger.commands)
                self.assertEqual(ledger.gold,gold-price)

    def test_missing_boss_shop_listing_falls_back_to_large_robot(self):
        data = self.shop_data()
        data["weaponShopList"] = [r for r in data["weaponShopList"]
                                  if r["name"] != "BossRobotSummonOrder"]
        result, _, _, ledger = self.summon(data)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['name'], 'LargeRobotSummonOrder')

    def test_critical_hero_medical_budget_precedes_boss_purchase(self):
        data = self.shop_data()
        next(r for r in data["teamOur"]["roles"] if r["id"] == 1)["health"] = 50
        data["weaponShopList"].append({"name": "Medicine", "price": 10})
        result, _, _, ledger = self.summon(data)
        self.assertTrue(result)
        self.assertNotIn('1', ledger.commands)
        self.assertEqual(ledger.notes[2]['conditions']['item'], 'LargeRobotSummonOrder')
        self.assertGreaterEqual(ledger.gold, 10)

    def test_purchase_respects_gold_already_reserved_by_defense(self):
        result, _, _, ledger = self.summon(self.shop_data(gold=200), gold=119)
        self.assertTrue(result)
        self.assertEqual(ledger.commands['1']['name'], 'LargeRobotSummonOrder')
        self.assertEqual(ledger.gold, 49)

    def test_use_existing_boss_before_buying_or_using_lower_tier(self):
        data = self.shop_data(backpack=["SmallRobotSummonOrder", "BossRobotSummonOrder"])
        result, turn, _, ledger = self.summon(data)
        self.assertTrue(result)
        action = ledger.commands["1"]
        self.assertEqual((action["action"], action["name"]), ("use", "BossRobotSummonOrder"))
        self.assertEqual(len(action["targetPos"]), 1)
        self.assertTrue(turn.inside(pos(action["targetPos"][0])))
        self.assertEqual(ledger.gold, 120)

    def test_unfinished_defense_prevents_using_owned_boss(self):
        data = self.shop_data(backpack=["BossRobotSummonOrder"])
        next(r for r in data["teamOur"]["roles"] if r["id"] == 32)["level"] = 2
        result, _, _, ledger = self.summon(data)
        self.assertFalse(result)
        self.assertFalse(ledger.commands)

    def test_full_backpacks_and_protected_actors_prevent_purchase(self):
        data = self.shop_data()
        for actor in data["teamOur"]["roles"]:
            if actor["roleType"] in ("worker", "pioneer"):
                actor.update(backPackCapability=1, backpack=["stone"])
        result, _, _, ledger = self.summon(data)
        self.assertFalse(result)
        self.assertFalse(ledger.commands)
        result, _, _, ledger = self.summon(self.shop_data(), excluded={1, 2})
        self.assertFalse(result)
        self.assertFalse(ledger.commands)

    def test_daytime_only_and_daily_limit_do_not_spend_gold(self):
        result, _, _, ledger = self.summon(self.shop_data(round_no=330))
        self.assertFalse(result)
        self.assertFalse(ledger.commands)
        mem = Memory()
        mem.robot_summon_state = {"day": 3, "count": 10, "positions": set(),
                                  "pending": None, "observed_round": 260}
        result, _, _, ledger = self.summon(self.shop_data(), mem=mem)
        self.assertFalse(result)
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.gold, 120)

    def test_failed_use_releases_pending_position_and_daily_quota(self):
        data = self.shop_data(backpack=["BossRobotSummonOrder"])
        result, _, mem, ledger = self.summon(data)
        self.assertTrue(result)
        site = pos(ledger.commands["1"]["targetPos"][0])
        self.assertEqual(mem.robot_summon_state["count"], 1)
        self.assertIn(site, mem.robot_summon_state["positions"])
        followup = copy.deepcopy(data)
        followup["roundNo"] += 1
        followup["lastRoundRoleActionResults"] = {"1": False}
        turn, _, _, _ = fortified_case(followup)
        observe_robot_summons(turn, mem)
        self.assertEqual(mem.robot_summon_state["count"], 0)
        self.assertNotIn(site, mem.robot_summon_state["positions"])

    def test_new_day_resets_daily_quota(self):
        mem = Memory()
        mem.robot_summon_state = {"day": 3, "count": 10, "positions": {(7, 4)},
                                  "pending": None, "observed_round": 389}
        data = self.shop_data(round_no=390)
        turn, _, _, _ = fortified_case(data)
        observe_robot_summons(turn, mem)
        self.assertEqual(mem.robot_summon_state["day"], 4)
        self.assertEqual(mem.robot_summon_state["count"], 0)
        self.assertFalse(mem.robot_summon_state["positions"])

    def test_identical_cached_turn_does_not_use_another_daily_slot(self):
        data = self.shop_data(backpack=["BossRobotSummonOrder"])
        cfg = Config(layout_mode="explicit", llm_enabled=False,
                     wall_cells=[list(p) for p in WALLS],
                     weapon_cells=[list(p) for p in TOWERS])
        agent = Agent(cfg)
        response = agent.decide(data)
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(memory.robot_summon_state["count"], 1)


class AssaultPositionTests(unittest.TestCase):
    def plan(self, data):
        turn, _, _, ledger = setup_case(data, layout_mode="explicit")
        return weakest_approach(turn, ledger), turn, ledger

    def test_open_gap_precedes_every_existing_wall(self):
        data = enemy_ring(placement_payload(), gap=(8, 4), weak=(8, 5), weak_hp=1)
        plan, _, _ = self.plan(data)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.entry, (8, 4))
        self.assertIsNone(plan.wall)

    def test_lower_wall_level_precedes_lower_wall_health(self):
        data = enemy_ring(placement_payload(), weak=(8, 4), weak_level=1, weak_hp=1000)
        next(r for r in data["teamEnemy"]["roles"]
             if r["roleType"] == "wall" and pos(r["pos"]) == (8, 5))["health"] = 1
        plan, _, _ = self.plan(data)
        self.assertEqual(plan.wall.pos, (8, 4))
        self.assertEqual(plan.wall.level, 1)

    def test_health_breaks_tie_between_equal_wall_levels(self):
        data = enemy_ring(placement_payload(), weak=(8, 4), weak_level=3, weak_hp=20)
        plan, _, _ = self.plan(data)
        self.assertEqual(plan.wall.pos, (8, 4))

    def test_spawn_is_outside_both_base_regions_and_not_occupied_or_reserved(self):
        data = enemy_ring(placement_payload(), gap=(8, 4))
        turn, _, _, ledger = setup_case(data, layout_mode="explicit")
        first = choose_summon_position(turn, ledger)
        self.assertIsNotNone(first)
        self.assertTrue(turn.inside(first))
        self.assertNotIn(first, turn.blocked)
        bases = [r for r in (*turn.ours, *turn.enemies) if r.kind == "station"]
        for base in bases:
            self.assertGreater(min(distance(first, p) for p in base.cells), 2)
        ledger.summon_pending_positions = {first}
        second = choose_summon_position(turn, ledger)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_no_legal_spawn_returns_none(self):
        data = enemy_ring(placement_payload(), gap=(8, 4))
        data["mapInfo"]["zones"] = [
            {"neutralType": "vendor", "pos": {"x": x, "y": y}}
            for x in range(15) for y in range(15)]
        turn, _, _, ledger = setup_case(data, layout_mode="explicit")
        self.assertIsNone(choose_summon_position(turn, ledger))

    def test_mirrored_enemy_base_preserves_wall_weakness_priority(self):
        data = enemy_ring(placement_payload(), weak=(8, 4))
        data["teamOur"]["type"] = "defender"
        for team in ("teamOur", "teamEnemy"):
            for role in data[team]["roles"]:
                x, y = pos(role["pos"])
                role["pos"] = {"x": 13 - x if role["roleType"] == "station" else 14 - x,
                               "y": 15 - y if role["roleType"] == "station" else 14 - y}
        data["teamOur"]["summonRobotList"] = []
        plan, _, _ = self.plan(data)
        self.assertEqual(plan.wall.pos, (6, 10))


class SummonOrderLegalityTests(unittest.TestCase):
    def case(self, data=None):
        return fortified_case(data or fortified_payload(backpack=["BossRobotSummonOrder"]))

    def test_order_requires_exactly_one_position(self):
        for targets in ([], [{"x": 7, "y": 4}, {"x": 7, "y": 5}]):
            with self.subTest(targets=targets):
                _, _, _, ledger = self.case()
                self.assertFalse(ledger.add(1, {"action": "use", "name": "BossRobotSummonOrder",
                                                "targetPos": targets}))

    def test_outside_map_and_either_base_construction_region_are_illegal(self):
        for target in ((15, 4), (-1, 4), (3, 11), (6, 9), (10, 4), (8, 4)):
            with self.subTest(target=target):
                _, _, _, ledger = self.case()
                self.assertFalse(ledger.add(1, command("use", target,
                                                       name="BossRobotSummonOrder")))

    def test_npc_and_previously_pending_position_are_illegal(self):
        data = fortified_payload(backpack=["BossRobotSummonOrder"])
        data["mapInfo"]["zones"] = [{"neutralType": "vendor", "pos": {"x": 7, "y": 4}}]
        _, _, _, ledger = self.case(data)
        self.assertFalse(ledger.add(1, command("use", (7, 4), name="BossRobotSummonOrder")))
        _, _, _, ledger = self.case()
        ledger.summon_pending_positions = {(7, 4)}
        self.assertFalse(ledger.add(1, command("use", (7, 4), name="BossRobotSummonOrder")))

    def test_dynamic_occupancy_is_legal_for_protocol_adjacent_spawning(self):
        data = fortified_payload(backpack=["BossRobotSummonOrder"])
        data["teamEnemy"]["roles"].append(unit(90, "worker", 7, 4))
        _, _, _, ledger = self.case(data)
        self.assertTrue(ledger.add(1, command("use", (7, 4), name="BossRobotSummonOrder")))

    def test_two_same_turn_orders_cannot_claim_same_spawn(self):
        data = fortified_payload(backpack=["BossRobotSummonOrder"])
        next(r for r in data["teamOur"]["roles"] if r["id"] == 2)["backpack"] = ["BossRobotSummonOrder"]
        _, _, _, ledger = self.case(data)
        self.assertTrue(ledger.add(1, command("use", (7, 4), name="BossRobotSummonOrder")))
        self.assertFalse(ledger.add(2, command("use", (7, 4), name="BossRobotSummonOrder")))


class RobotCommandTests(unittest.TestCase):
    def case(self, **fields):
        return setup_case(assault_payload(robot=controlled_robot(**fields)),
                          layout_mode="explicit")

    def test_robot_attacks_single_enemy_target_without_controller(self):
        _, _, _, ledger = self.case()
        self.assertTrue(ledger.add(30000, command("attack", (10, 4))))
        self.assertEqual(ledger.commands["30000"],
                         {"action": "attack", "targetPos": [{"x": 10, "y": 4}]})
        self.assertFalse(ledger.add(30000, command("move", (7, 4))))

    def test_protocol_range_fallback_accepts_three_but_rejects_four(self):
        for robot_x, accepted in ((7, True), (6, False)):
            with self.subTest(robot_x=robot_x):
                _, _, _, ledger = self.case(x=robot_x)
                self.assertEqual(ledger.add(30000, command("attack", (10, 4))), accepted)

    def test_robot_rejects_empty_or_multiple_attack_targets(self):
        for targets in ([], [{"x": 10, "y": 4}, {"x": 10, "y": 3}]):
            with self.subTest(targets=targets):
                _, _, _, ledger = self.case()
                self.assertFalse(ledger.add(30000, {"action": "attack", "targetPos": targets}))

    def test_robot_attack_requires_enemy_building_or_hero(self):
        for target in ((8, 5), (8, 4)):
            with self.subTest(target=target):
                _, _, _, ledger = self.case()
                self.assertFalse(ledger.add(30000, command("attack", target)))
        data = assault_payload()
        data["teamOur"]["roles"].append(unit(45, "wall", 9, 4))
        _, _, _, ledger = setup_case(data, layout_mode="explicit")
        self.assertFalse(ledger.add(30000, command("attack", (9, 4))))

    def test_robot_move_is_one_free_step_and_actions_are_restricted(self):
        for target, accepted in (((7, 4), True), ((6, 4), False), ((10, 4), False)):
            with self.subTest(target=target):
                _, _, _, ledger = self.case()
                self.assertEqual(ledger.add(30000, command("move", target)), accepted)
        for action in ("buy", "use", "build", "collect"):
            with self.subTest(action=action):
                _, _, _, ledger = self.case()
                self.assertFalse(ledger.add(30000, command(action, (7, 4),
                                                          name="BossRobotSummonOrder")))

    def test_daytime_or_dizzy_robot_produces_no_attack_strategy(self):
        for round_no, abnormal in ((1, ""), (70, "dizzy")):
            with self.subTest(round_no=round_no, abnormal=abnormal):
                data = assault_payload(round_no, controlled_robot(abnormalState=abnormal))
                turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
                act_robots(turn, RobotAssaultMemory(), nav, ledger)
                self.assertNotIn("30000", ledger.commands)

    def test_breach_selected_wall_then_keep_base_as_next_target(self):
        data = enemy_ring(assault_payload(robot=controlled_robot(x=7)), weak=(8, 4))
        memory = RobotAssaultMemory()
        turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
        act_robots(turn, memory, nav, ledger)
        self.assertEqual(ledger.commands["30000"], command("attack", (8, 4)))
        followup = copy.deepcopy(data)
        followup["roundNo"] += 1
        followup["teamEnemy"]["roles"] = [
            r for r in followup["teamEnemy"]["roles"]
            if not (r["roleType"] == "wall" and pos(r["pos"]) == (8, 4))]
        turn, _, nav, ledger = setup_case(followup, layout_mode="explicit")
        act_robots(turn, memory, nav, ledger)
        self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))

    def test_robot_without_in_range_target_advances_one_step(self):
        data = enemy_ring(assault_payload(robot=controlled_robot(x=4, y=4)), weak=(8, 4))
        turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
        act_robots(turn, RobotAssaultMemory(), nav, ledger)
        action = ledger.commands["30000"]
        self.assertEqual(action["action"], "move")
        destination = pos(action["targetPos"][0])
        self.assertEqual(distance(destination, (4, 4)), 1)
        self.assertNotIn(destination, turn.blocked)
        self.assertGreater(destination[0], 4)

    def test_static_npc_in_a_missing_wall_cell_is_not_an_open_entrance(self):
        for neutral_type in ("weaponShop", "defenderTaskPoint1"):
            with self.subTest(neutral_type=neutral_type):
                data = assault_payload(robot=controlled_robot(x=7))
                initial, _, _, _ = setup_case(data, layout_mode="explicit")
                base = next(u for u in initial.enemies if u.kind == "station")
                data["teamEnemy"]["roles"] += [
                    unit(100 + i, "wall", *p, level=1 if p == (8, 5) else 3,
                         health=1000 if p == (8, 5) else 2000)
                    for i, p in enumerate(enemy_entries(initial, base)) if p != (8, 4)]
                data["mapInfo"]["zones"] = [
                    {"neutralType": neutral_type, "pos": {"x": 8, "y": 4}}]
                turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
                self.assertEqual(weakest_approach(turn, ledger).wall.pos, (8, 5))
                act_robots(turn, RobotAssaultMemory(), nav, ledger)
                self.assertEqual(ledger.commands["30000"], command("attack", (8, 5)))

    def test_expired_decision_budget_stops_even_a_direct_in_range_attack(self):
        turn, _, nav, ledger = setup_case(assault_payload(), layout_mode="explicit")
        nav.deadline = 0
        with self.assertRaises(DeadlineExceeded):
            act_robots(turn, RobotAssaultMemory(), nav, ledger)
        self.assertFalse(ledger.commands)

    def test_equal_weakness_targets_wall_nearest_robot(self):
        data = enemy_ring(assault_payload(robot=controlled_robot(x=14)), weak=(8, 4),
                          weak_level=1, weak_hp=1000)
        right_wall = next(r for r in data["teamEnemy"]["roles"]
                          if r["roleType"] == "wall" and pos(r["pos"]) == (13, 4))
        right_wall.update(level=1, health=1000)
        turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
        act_robots(turn, RobotAssaultMemory(), nav, ledger)
        self.assertEqual(ledger.commands["30000"], command("attack", (13, 4)))


if __name__ == "__main__":
    unittest.main()

