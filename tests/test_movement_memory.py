"""Destination cooldowns follow one work task, not unrelated accepted moves."""
import unittest

from test_agent import payload, unit
from agent.commands import command
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn


BOSS = 'BossRobotSummonOrder'
SHOP = (12, 12)
BUILD = (3, 8)
MATCH_TRAIL = [(4, 5), (5, 4), (6, 3), (7, 2),
               (6, 3), (5, 4), (4, 5), (5, 4)]


class MovementTaskContinuityTests(unittest.TestCase):
    def observe(self, mem, tick, position, job, expected=None):
        data = payload(tick, [unit(1, 'worker', *position, health=500)])
        mem.last_round = tick - 1
        mem.last_commands = {'1': command('move', expected or position)}
        mem.daytime_jobs = {1: job}
        turn = Turn(data, Config())
        mem.movement.observe(turn, mem)
        return turn

    def job(self, kind='robot_buy', target=SHOP, quantity=3):
        return dict(kind=kind, target=target,
                    name=BOSS if kind == 'robot_buy' else 'rocket', quantity=quantity)

    def test_buy_build_buy_switches_do_not_cool_down_the_shop(self):
        mem = Memory(build_targets={1: BUILD})
        for tick, position in enumerate(MATCH_TRAIL, 47):
            job = self.job('build', BUILD) if 51 <= tick <= 53 else self.job()
            self.observe(mem, tick, position, job)
        self.assertFalse(mem.movement.looped)
        self.assertFalse(mem.movement.avoids(1, SHOP))
        self.assertFalse(mem.movement.avoids(1, BUILD))

    def test_same_task_loop_still_cools_down_its_destination(self):
        mem = Memory()
        for tick, position in enumerate(MATCH_TRAIL, 47):
            self.observe(mem, tick, position, self.job())
        self.assertIn(1, mem.movement.looped)
        self.assertTrue(mem.movement.avoids(1, SHOP))
        self.assertEqual(mem.movement.targets[1, SHOP], 62)

    def test_changed_destination_starts_a_new_trail(self):
        mem = Memory()
        for tick, position in enumerate(MATCH_TRAIL, 47):
            self.observe(mem, tick, position, self.job(target=SHOP if tick < 51 else (11, 12)))
        self.assertFalse(mem.movement.targets)

    def test_quantity_change_does_not_hide_a_same_destination_loop(self):
        mem = Memory()
        for tick, position in enumerate(MATCH_TRAIL, 47):
            self.observe(mem, tick, position, self.job(quantity=3 if tick < 51 else 2))
        self.assertTrue(mem.movement.avoids(1, SHOP))

    def test_task_switch_preserves_real_step_collision_feedback(self):
        mem = Memory()
        for tick in range(47, 55):
            job = self.job('build', BUILD) if tick % 2 else self.job()
            self.observe(mem, tick, (4, 5), job, expected=(5, 4))
        self.assertIn((5, 4), mem.movement.blocked(1))
        self.assertEqual(mem.movement.failure_counts[1, (5, 4)][0], 8)
        self.assertFalse(mem.movement.avoids(1, SHOP))

