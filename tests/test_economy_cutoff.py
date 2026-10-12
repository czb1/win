"""Ordinary daytime development must wait for the daily farming cutoff."""
import unittest

from test_agent import payload, setup_case, unit
from agent.commands import command
from agent.brain import Agent
from agent.economy import build, dusk_resources, supplies, use_inventory, workers
from agent.intelligence import Memory
from agent.worker_jobs import resume_daytime_jobs
from agent.wall_watch import prepare_watch
from test_daylight_spending import case as watch_case, setup as setup_watch


def cutoff_case(day, tick, level=3, origin=0, cutoff=35, backpack=()):
    data = payload((day - 1) * 130 + tick + origin, [
        unit(13, 'station', 0, 3, health=4500, level=3),
        unit(1, 'worker', 4, 4, health=500, backpack=list(backpack)),
        unit(2, 'worker', 7, 7, health=500),
        unit(20, 'rocket', 3, 5, health=2000, level=level),
        unit(30, 'wall', 5, 4, health=1000, level=1),
    ])
    data['teamOur']['goldNum'] = 600
    data['mapInfo']['zones'] = [
        dict(neutralType='weaponShop', pos=dict(x=4, y=3)),
        dict(neutralType='vendor', pos=dict(x=8, y=6)),
        dict(neutralType='copper', pos=dict(x=5, y=5), remain=10),
        dict(neutralType='copper', pos=dict(x=8, y=7), remain=10),
    ]
    data['vendorShopList'] = [dict(name='copper', price=5)]
    data['weaponShopList'] = [dict(name=name, price=price) for name, price in (
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallFixer', 15))]
    return setup_case(data, layout_mode='explicit', loadout=['rocket'],
                      weapon_cells=[[3, 5]], wall_cells=[[5, 4]],
                      round_origin=origin, economy_rounds=cutoff)


class EconomyCutoffTests(unittest.TestCase):
    def test_sweep_waits_for_cutoff_at_every_weapon_level_and_day(self):
        for day in (2, 3, 4, 10):
            for origin in (0, 1):
                for level in (1, 2, 3):
                    for tick in (34, 35):
                        with self.subTest(day=day, origin=origin, level=level, tick=tick):
                            turn, cfg, nav, ledger = cutoff_case(day, tick, level, origin)
                            dusk_resources(turn, cfg, Memory(), nav, ledger, [(3, 5)])
                            self.assertEqual(bool(ledger.commands), tick == 35)

    def test_paid_wall_delivery_also_waits_for_cutoff(self):
        for day in (2, 4, 10):
            for tick in (34, 35):
                with self.subTest(day=day, tick=tick):
                    turn, cfg, nav, ledger = cutoff_case(day, tick, backpack=['WallUpgradeVoucher1'])
                    accepted = use_inventory(turn, nav, ledger, turn.workers[0], mem=Memory())
                    self.assertEqual(accepted, tick == 35)
                    if accepted:
                        self.assertEqual(ledger.commands['1'], command('use', (5, 4), name='WallUpgradeVoucher1'))

    def test_new_purchase_cannot_skip_farming_when_shop_is_adjacent(self):
        for day in (2, 3, 4, 10):
            for tick in (34, 35):
                with self.subTest(day=day, tick=tick):
                    turn, cfg, nav, ledger = cutoff_case(day, tick)
                    plan = supplies(turn, cfg, Memory(), nav, ledger, turn.workers[0], bulk=True)
                    self.assertEqual(plan is not None, tick == 35)

    def test_previous_preparation_state_does_not_take_worker_before_cutoff(self):
        for day in (2, 3, 4, 10):
            with self.subTest(day=day):
                turn, cfg, nav, ledger = cutoff_case(day, 34)
                mem = Memory(preparation_tick=0, preparation_workers={1}, sold_workers={1}, supply_worker=1)
                workers(turn, cfg, mem, nav, ledger, [(3, 5)], [(5, 4)])
                self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))

    def test_old_purchase_job_releases_until_the_daily_cutoff(self):
        for day in (2, 4, 10):
            with self.subTest(day=day):
                turn, cfg, nav, ledger = cutoff_case(day, 34)
                mem = Memory(daytime_jobs={1: dict(kind='buy', target=(4, 3),
                              name='WallUpgradeVoucher1', quantity=1)}, supply_worker=1)
                resume_daytime_jobs(turn, cfg, mem, nav, ledger, [(3, 5)], [(5, 4)], set())
                workers(turn, cfg, mem, nav, ledger, [(3, 5)], [(5, 4)])
                self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))

    def test_configured_cutoff_still_controls_the_boundary(self):
        for tick in (39, 40):
            turn, cfg, nav, ledger = cutoff_case(6, tick, cutoff=40)
            dusk_resources(turn, cfg, Memory(), nav, ledger, [(3, 5)])
            self.assertEqual(bool(ledger.commands), tick == 40)

    def test_new_construction_waits_even_on_the_first_day(self):
        for day in (1, 2, 4, 10):
            for tick in (34, 35):
                with self.subTest(day=day, tick=tick):
                    data = payload((day - 1) * 130 + tick, [
                        unit(13, 'station', 0, 3, health=4500, level=3),
                        unit(1, 'worker', 4, 4, health=500, backpack=['stone'])])
                    turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit',
                        weapon_cells=[], wall_cells=[[5, 4]])
                    accepted = build(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                                     [(5, 4)], lambda _: 'wall')
                    self.assertEqual(accepted, tick == 35)

    def test_full_firepower_does_not_send_watcher_shopping_before_cutoff(self):
        for tick in (34, 35):
            data, cfg = watch_case(tick=tick)
            data['teamOur']['roles'][1]['pos'] = dict(x=6, y=10)
            turn, cfg, nav, ledger = setup_watch(data, cfg)
            locked, _ = prepare_watch(turn, cfg, Memory(wall_watch_id=1), nav, ledger,
                                      turn.workers[0], list(ledger.wall_cells))
            self.assertEqual(locked, tick == 35)
            self.assertEqual('1' in ledger.commands, tick == 35)

    def test_agent_keeps_both_workers_producing_before_cutoff(self):
        for day in (2, 4, 10):
            with self.subTest(day=day):
                turn, cfg, _, _ = cutoff_case(day, 34)
                cfg.llm_enabled = False
                commands = Agent(cfg).decide(turn.raw)['roleCommandMap']
                self.assertEqual(commands['1'], command('collect', (5, 5)))
                self.assertEqual(commands['2'], command('collect', (8, 7)))

    def test_new_day_restores_farming_even_after_an_upgrade(self):
        turn, cfg, _, _ = cutoff_case(3, 35, backpack=['WallUpgradeVoucher1'])
        cfg.llm_enabled = False
        agent = Agent(cfg)
        self.assertEqual(agent.decide(turn.raw)['roleCommandMap']['1']['action'], 'use')
        following, _, _, _ = cutoff_case(4, 0, backpack=['WallUpgradeVoucher1'])
        commands = agent.decide(following.raw)['roleCommandMap']
        self.assertEqual(commands['1'], command('collect', (5, 5)))
        self.assertEqual(commands['2'], command('collect', (8, 7)))


if __name__ == '__main__':
    unittest.main()
