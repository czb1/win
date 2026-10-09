"""Focused v2.0 command, visibility, channel and safety boundary checks."""
import copy
from time import monotonic
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, distance, neighbours, pos
from agent.sabotage import act_imps


def ore(point, kind="copper", remain=10):
    return {"neutralType": kind, "pos": {"x": point[0], "y": point[1]}, "remain": remain}


def imp_payload(round_no=10, point=(27, 12), mirror=False):
    base, mine = (4, 27), (28, 12)
    if mirror:
        point, mine = (40 - point[0], 31 - point[1]), (40 - mine[0], 31 - mine[1])
        base = (35, 5)  # Mirror the 2x2 station footprint, then use its top-left anchor.
    data = payload(round_no, [unit(13, "station", *base),
                              unit(14, "imp", *point, backPackCapability=0, attackRange=0)])
    data["mapInfo"].update(width=41, height=32, zones=[ore(mine)])
    if mirror:
        data["teamOur"]["type"] = "defender"
    return data


def imp_case(data, mem=None):
    turn, cfg, nav, ledger = setup_case(data, llm_enabled=False, layout_mode="explicit")
    mem = mem or Memory()
    mem.observe(turn, cfg)
    nav.memory = mem.movement
    return turn, cfg, nav, ledger, mem


def act(data, mem=None):
    turn, _, nav, ledger, mem = imp_case(data, mem)
    act_imps(turn, mem.sabotage, nav, ledger)
    return turn, ledger, mem


class ImpCommandTests(unittest.TestCase):
    def test_imp_is_a_character_but_not_a_production_or_defence_hero(self):
        turn, _, _ = act(imp_payload())
        self.assertEqual([h.id for h in turn.imps], [14])
        self.assertEqual([h.id for h in turn.characters], [14])
        self.assertEqual(turn.heroes, [])

    def test_enemy_half_and_destroy_are_mirrored(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                data = imp_payload(mirror=mirror)
                turn, ledger, _ = act(data)
                self.assertEqual(ledger.commands["14"]["action"], "destroy")
                self.assertTrue(turn.enemy_mine(pos(ledger.commands["14"]["targetPos"][0])))

    def test_actual_base_side_overrides_team_label(self):
        data = imp_payload()
        data["teamOur"]["type"] = "defender"
        turn, ledger, _ = act(data)
        self.assertTrue(turn.enemy_mine((28, 12)))
        self.assertEqual(ledger.commands["14"]["action"], "destroy")

    def test_home_and_diagonal_mines_rejected_by_final_ledger(self):
        for mine in ((7, 24), (0, 0), (40, 31)):
            data = imp_payload(point=next(p for p in neighbours(mine)
                                          if 0 <= p[0] < 41 and 0 <= p[1] < 32))
            data["mapInfo"]["zones"] = [ore(mine)]
            turn, _, _, ledger, _ = imp_case(data)
            with self.subTest(mine=mine):
                self.assertFalse(turn.enemy_mine(mine))
                self.assertFalse(ledger.add(14, command("destroy", mine)))

    def test_rectangular_half_is_not_x_or_base_distance(self):
        data = imp_payload()
        data["mapInfo"]["zones"] = [ore(p) for p in ((8, 2), (30, 28), (28, 12))]
        turn = Turn(data, Config())
        self.assertTrue(turn.enemy_mine((8, 2)))
        self.assertFalse(turn.enemy_mine((30, 28)))
        self.assertTrue(turn.enemy_mine((28, 12)))

    def test_no_base_or_ambiguous_base_does_not_authorize_destruction(self):
        for base in (None, unit(13, "station", 4, 5)):
            data = imp_payload()
            if base is None:
                data["teamOur"]["roles"] = data["teamOur"]["roles"][1:]
            else:
                data["mapInfo"].update(width=11, height=11)
                data["teamOur"]["roles"] = [base, unit(14, "imp", 7, 2)]
                data["mapInfo"]["zones"] = [ore((8, 2))]
            turn, ledger, _ = act(data)
            self.assertFalse(any(c["action"] == "destroy" for c in ledger.commands.values()))

    def test_destroy_requires_imp_adjacent_one_live_ore_target(self):
        cases = [command("destroy"), command("destroy", (40, 0)), command("destroy", (27, 12)),
                 {"action": "destroy", "targetPos": [{"x": 28, "y": 12}, {"x": 29, "y": 12}]}]
        for cmd in cases:
            _, _, _, ledger, _ = imp_case(imp_payload())
            self.assertFalse(ledger.add(14, cmd))
        data = imp_payload()
        data["teamOur"]["roles"][1]["roleType"] = "worker"
        _, _, _, ledger, _ = imp_case(data)
        self.assertFalse(ledger.add(14, command("destroy", (28, 12))))
        for remain in (0, -1):
            data = imp_payload()
            data["mapInfo"]["zones"][0]["remain"] = remain
            _, _, _, ledger, _ = imp_case(data)
            self.assertFalse(ledger.add(14, command("destroy", (28, 12))))

    def test_imp_cannot_collect_build_trade_or_use_items(self):
        for action in ("collect", "build", "sell", "buy", "use", "acceptTask", "submitAnswer"):
            _, _, _, ledger, _ = imp_case(imp_payload())
            with self.subTest(action=action):
                self.assertFalse(ledger.add(14, command(action, (28, 12), name="copper", num=1)))

    def test_imp_move_respects_occupancy_and_reservations_and_one_action(self):
        _, _, _, ledger, _ = imp_case(imp_payload())
        self.assertFalse(ledger.add(14, command("move", (28, 12))))
        ledger.reserved.add((26, 12))
        self.assertFalse(ledger.add(14, command("move", (26, 12))))
        self.assertTrue(ledger.add(14, command("destroy", (28, 12))))
        self.assertFalse(ledger.add(14, command("move", (27, 13))))

    def test_catch_accepts_only_adjacent_living_enemy_imp(self):
        for kind, health, point, allowed in (("imp", 200, (28, 13), True),
                                              ("worker", 200, (28, 13), False),
                                              ("imp", 0, (28, 13), False),
                                              ("imp", 200, (29, 13), False)):
            data = imp_payload()
            data["teamEnemy"]["roles"] = [unit(90, kind, *point, health=health)]
            _, _, _, ledger, _ = imp_case(data)
            self.assertEqual(ledger.add(14, command("catch", point)), allowed)


class ImpStrategyTests(unittest.TestCase):
    def test_goes_to_enemy_mine_and_ignores_nearby_friendly_mine(self):
        data = imp_payload(point=(5, 23))
        data["mapInfo"]["zones"].append(ore((6, 23), "stone"))
        turn, ledger, mem = act(data)
        self.assertEqual(mem.sabotage.targets[14][0], (28, 12))
        self.assertEqual(ledger.commands["14"]["action"], "move")
        destination = pos(ledger.commands["14"]["targetPos"][0])
        self.assertNotIn(destination, turn.blocked)
        self.assertEqual(distance(destination, turn.imps[0].pos), 1)

    def test_no_enemy_mine_never_falls_back_to_destroying_home_ore(self):
        data = imp_payload(point=(6, 23))
        data["mapInfo"]["zones"] = [ore((7, 23))]
        _, ledger, mem = act(data)
        self.assertNotIn("14", ledger.commands)
        self.assertNotIn(14, mem.sabotage.targets)

    def test_more_remaining_ore_wins_equal_safe_routes(self):
        data = imp_payload()
        data["mapInfo"]["zones"] = [ore((28, 12), remain=1), ore((28, 13), remain=9)]
        _, ledger, _ = act(data)
        self.assertEqual(pos(ledger.commands["14"]["targetPos"][0]), (28, 13))

    def test_keeps_same_mine_during_four_consecutive_legal_destroy_turns(self):
        data, mem = imp_payload(), Memory()
        for count in range(4):
            with self.subTest(count=count):
                turn, ledger, mem = act(data, mem)
                self.assertEqual(ledger.commands["14"], command("destroy", (28, 12)))
                self.assertEqual(mem.sabotage.progress.get(14, 0), count)
                mem.last_round, mem.last_commands = turn.round, ledger.commands.copy()
                data["roundNo"] += 1
                data["lastRoundRoleActionResults"] = {"14": True}
                data["mapInfo"]["zones"].append(ore((26, 13), remain=10))
        data["mapInfo"]["zones"] = [ore((26, 13))]
        _, ledger, mem = act(data, mem)
        self.assertEqual(ledger.commands["14"], command("destroy", (26, 13)))
        self.assertEqual(mem.sabotage.progress.get(14, 0), 0)

    def test_channel_progress_resets_on_illegal_missing_move_gap_and_ore_change(self):
        changes = ("illegal", "absent", "move", "gap", "ore", "position")
        for change in changes:
            data = imp_payload()
            turn, ledger, mem = act(data)
            mem.last_round, mem.last_commands = turn.round, ledger.commands.copy()
            data["roundNo"] += 1
            data["lastRoundRoleActionResults"] = {14: True}
            mem.sabotage.progress[14] = 2
            if change == "illegal":
                data["lastRoundRoleActionResults"] = {14: False}
            elif change == "absent":
                mem.last_commands.clear()
            elif change == "move":
                mem.last_commands["14"] = command("move", (27, 13))
            elif change == "gap":
                data["roundNo"] += 1
            elif change == "ore":
                data["mapInfo"]["zones"][0]["neutralType"] = "iron"
            else:
                data["teamOur"]["roles"][1]["pos"]["y"] = 13
            _, _, _, _, mem = imp_case(data, mem)
            with self.subTest(change=change):
                self.assertEqual(mem.sabotage.progress.get(14, 0), 0)

    def test_dead_imp_cleanup_and_respawn(self):
        data = imp_payload()
        turn, ledger, mem = act(data)
        mem.last_round, mem.last_commands = turn.round, ledger.commands.copy()
        data["roundNo"] += 1
        data["teamOur"]["roles"][1]["health"] = 0
        _, ledger, mem = act(data, mem)
        self.assertNotIn("14", ledger.commands)
        self.assertEqual(mem.sabotage.targets, {})
        self.assertEqual(mem.sabotage.progress, {})
        data["roundNo"] = 130
        data["teamOur"]["roles"][1]["health"] = 500
        _, ledger, mem = act(data, mem)
        self.assertEqual(ledger.commands["14"]["action"], "destroy")

    def test_all_enemy_character_types_trigger_safe_escape(self):
        for kind in ("worker", "pioneer", "imp"):
            data = imp_payload()
            data["teamEnemy"]["roles"] = [unit(90, kind, 29, 12)]
            turn, ledger, _ = act(data)
            with self.subTest(kind=kind):
                self.assertEqual(ledger.commands["14"]["action"], "move")
                destination = pos(ledger.commands["14"]["targetPos"][0])
                self.assertGreater(distance(destination, (29, 12)), 2)
                self.assertNotIn(destination, turn.blocked)

    def test_driving_enemy_has_larger_capture_envelope(self):
        for driving, expected in ((False, "destroy"), (True, None)):
            data = imp_payload()
            data["teamEnemy"]["roles"] = [unit(90, "worker", 33, 12, isDriving=driving)]
            _, ledger, _ = act(data)
            self.assertEqual(ledger.commands.get("14", {}).get("action"), expected)

    def test_enemy_can_reach_channel_before_completion_so_imp_does_not_start(self):
        data = imp_payload()
        data["teamEnemy"]["roles"] = [unit(90, "pioneer", 32, 12)]
        _, ledger, _ = act(data)
        self.assertNotEqual(ledger.commands.get("14", {}).get("action"), "destroy")

    def test_recent_enemy_retained_across_fog_and_dead_enemy_removed(self):
        data = imp_payload()
        data["teamEnemy"]["roles"] = [unit(90, "pioneer", 33, 12)]
        turn, ledger, mem = act(data)
        mem.last_round, mem.last_commands = turn.round, ledger.commands.copy()
        data["roundNo"] += 1
        data["teamEnemy"]["roles"] = []
        _, ledger, mem = act(data, mem)
        self.assertIn(90, mem.sabotage.enemies)
        self.assertNotEqual(ledger.commands.get("14", {}).get("action"), "destroy")
        data["teamEnemy"]["roles"] = [unit(90, "pioneer", 33, 12, health=0)]
        _, ledger, mem = act(data, mem)
        self.assertNotIn(90, mem.sabotage.enemies)
        self.assertEqual(ledger.commands["14"]["action"], "destroy")

    def test_dusk_fog_remembers_prior_driving_reach_after_car_disappears(self):
        data = imp_payload(69, point=(29, 12))
        data["teamEnemy"]["roles"] = [unit(90, "worker", 33, 12, isDriving=True)]
        turn, ledger, mem = act(data)
        mem.last_round, mem.last_commands = turn.round, ledger.commands.copy()
        data["roundNo"] = 70
        data["teamEnemy"]["roles"] = []
        _, ledger, _ = act(data, mem)
        self.assertEqual(ledger.commands["14"]["action"], "move")
        self.assertEqual(ledger.notes[14]["reason"], "imp_escape")
        self.assertGreater(distance(pos(ledger.commands["14"]["targetPos"][0]), (33, 12)), 4)

    def test_robot_range_defaults_and_all_target_teams_are_avoided(self):
        for kind in ("smallRobot", "middleRobot", "largeRobot", "bossRobot"):
            for target_team in ("challenger", "defender", ""):
                data = imp_payload(70)
                robot = unit(90, kind, 23, 12, targetTeam=target_team)
                robot.pop("attackRange")
                robot.pop("attackPower")
                data["robot"]["roles"] = [robot]
                turn, ledger, _ = act(data)
                with self.subTest(kind=kind, target_team=target_team):
                    self.assertEqual(ledger.commands["14"]["action"], "move")
                    destination = pos(ledger.commands["14"]["targetPos"][0])
                    self.assertGreater(distance(destination, (23, 12)), 4)
                    self.assertNotIn(destination, turn.blocked)

    def test_stunned_robot_is_not_assumed_safe_for_four_turn_channel(self):
        data = imp_payload(70)
        data["robot"]["roles"] = [unit(90, "smallRobot", 23, 12, attackRange=3, abnormalState="dizzy")]
        _, ledger, _ = act(data)
        self.assertEqual(ledger.commands["14"]["action"], "move")

    def test_robot_can_approach_during_channel_so_imp_does_not_start(self):
        data = imp_payload(70)
        data["robot"]["roles"] = [unit(90, "largeRobot", 21, 12, attackRange=3)]
        _, ledger, _ = act(data)
        self.assertNotEqual(ledger.commands.get("14", {}).get("action"), "destroy")

    def test_route_avoids_enemy_and_robot_envelopes(self):
        data = imp_payload(70, point=(12, 12))
        data["robot"]["roles"] = [unit(90, "smallRobot", 18, 12, attackRange=3)]
        turn, ledger, _ = act(data)
        destination = pos(ledger.commands["14"]["targetPos"][0])
        self.assertEqual(ledger.commands["14"]["action"], "move")
        self.assertGreater(distance(destination, (18, 12)), 4)
        self.assertNotIn(destination, turn.blocked)

    def test_survival_preempts_channel_and_budget_expiry(self):
        data = imp_payload(70)
        data["teamEnemy"]["roles"] = [unit(90, "worker", 29, 12)]
        turn, _, nav, ledger, mem = imp_case(data)
        mem.sabotage.targets[14] = (28, 12), "copper"
        mem.sabotage.progress[14] = 3
        nav.deadline = monotonic() - 1
        act_imps(turn, mem.sabotage, nav, ledger)
        self.assertEqual(ledger.commands["14"]["action"], "move")
        self.assertNotIn(14, mem.sabotage.progress)

    def test_escape_respects_reserved_destination(self):
        data = imp_payload()
        data["teamEnemy"]["roles"] = [unit(90, "worker", 29, 12)]
        turn, _, nav, ledger, mem = imp_case(data)
        ledger.reserved.add((26, 11))
        act_imps(turn, mem.sabotage, nav, ledger)
        self.assertNotEqual(pos(ledger.commands["14"]["targetPos"][0]), (26, 11))

    def test_escape_routes_around_wall_instead_of_waiting_in_danger(self):
        data = imp_payload(70)
        data["teamOur"]["roles"] += [unit(100 + i, "wall", 28, y) for i, y in enumerate((11, 13))]
        data["robot"]["roles"] = [unit(90, "smallRobot", 23, 12, attackRange=3)]
        turn, ledger, _ = act(data)
        self.assertEqual(ledger.commands["14"]["action"], "move")
        destination = pos(ledger.commands["14"]["targetPos"][0])
        self.assertNotIn(destination, turn.blocked)
        self.assertIn(destination, ((27, 11), (27, 13)))
        self.assertFalse(ledger.notes[14]["conditions"]["safe_exit"])

    def test_budget_expired_keeps_local_escape_even_without_one_step_safe_exit(self):
        data = imp_payload(70)
        data["robot"]["roles"] = [unit(90, "smallRobot", 25, 12, attackRange=3)]
        turn, _, nav, ledger, mem = imp_case(data)
        nav.deadline = monotonic() - 1
        act_imps(turn, mem.sabotage, nav, ledger)
        self.assertEqual(ledger.commands["14"]["action"], "move")
        self.assertGreater(distance(pos(ledger.commands["14"]["targetPos"][0]), (25, 12)), 2)

    def test_trapped_imp_does_not_send_illegal_move_or_destroy(self):
        data = imp_payload()
        data["teamOur"]["roles"] += [unit(100 + i, "wall", *p)
                                       for i, p in enumerate(neighbours((27, 12))) if p != (28, 12)]
        data["teamEnemy"]["roles"] = [unit(90, "worker", 29, 12)]
        _, ledger, _ = act(data)
        self.assertNotIn("14", ledger.commands)
        self.assertEqual(ledger.notes[14]["reason"], "imp_no_safe_escape")

    def test_no_new_channel_in_last_three_daylight_turns(self):
        for round_no in (66, 67, 68, 69):
            _, ledger, _ = act(imp_payload(round_no))
            self.assertEqual(ledger.commands.get("14", {}).get("action") == "destroy", round_no == 66)

    def test_existing_channel_can_complete_before_dusk(self):
        data = imp_payload(66)
        turn, ledger, mem = act(data)
        mem.last_round, mem.last_commands = turn.round, ledger.commands.copy()
        data["roundNo"] = 67
        data["lastRoundRoleActionResults"] = {14: True}
        _, ledger, _ = act(data, mem)
        self.assertEqual(ledger.commands["14"]["action"], "destroy")

    def test_no_reachable_safe_enemy_mine_never_issues_destroy(self):
        data = imp_payload(point=(27, 12))
        data["mapInfo"]["zones"] = [ore((38, 2))]
        data["teamOur"]["roles"] += [unit(100 + i, "wall", *p)
                                       for i, p in enumerate(neighbours((27, 12)))]
        _, ledger, _ = act(data)
        self.assertNotIn("14", ledger.commands)


class ImpIntegrationTests(unittest.TestCase):
    def test_agent_dispatches_imp_and_duplicate_request_is_idempotent(self):
        agent, data = Agent(Config(llm_enabled=False)), imp_payload()
        response = agent.decide(data)
        self.assertEqual(response["roleCommandMap"]["14"]["action"], "destroy")
        self.assertEqual(agent.decide(copy.deepcopy(data)), response)
        self.assertEqual(len(agent.sessions), 1)

    def test_round_rewind_and_side_switch_do_not_reuse_imp_targets(self):
        agent = Agent(Config(llm_enabled=False))
        first = imp_payload(10)
        self.assertEqual(agent.decide(first)["roleCommandMap"]["14"]["action"], "destroy")
        first["roundNo"] = 0
        first["mapInfo"]["zones"] = []
        self.assertNotIn("14", agent.decide(first)["roleCommandMap"])
        mirrored = imp_payload(0, mirror=True)
        self.assertEqual(pos(agent.decide(mirrored)["roleCommandMap"]["14"]["targetPos"][0]), (12, 19))

    def test_worker_and_pioneer_crew_stays_separate(self):
        data = imp_payload(70)
        data["teamOur"]["roles"] += [unit(10, "worker", 3, 26), unit(11, "pioneer", 9, 22),
                                       unit(12, "worker", 3, 25), unit(20, "rocket", 3, 27)]
        data["robot"]["roles"] = [unit(90, "smallRobot", 9, 27, targetTeam="challenger", attackRange=3)]
        # This checks independent sabotage in an explicit layout with no gate.
        # The default perimeter now recalls the imp to guard its sole opening.
        cfg = Config(llm_enabled=False, layout_mode='explicit', weapon_cells=[[3, 27]])
        result = Agent(cfg).decide(data)["roleCommandMap"]
        original = copy.deepcopy(data)
        original["teamOur"]["roles"] = [r for r in original["teamOur"]["roles"] if r["roleType"] != "imp"]
        baseline = Agent(cfg).decide(original)["roleCommandMap"]
        self.assertEqual({uid: cmd for uid, cmd in result.items() if uid != "14"}, baseline)
        self.assertEqual(result["14"]["action"], "destroy")


if __name__ == "__main__":
    unittest.main()
