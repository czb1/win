import hashlib
import json
import logging
from collections import OrderedDict
from time import monotonic
from .config import Config
from .model import Turn, distance
from .navigation import Navigator, layout, DeadlineExceeded
from .commands import Ledger
from .combat import assignments, defend, emergency_items
from .economy import workers, pioneer, walk, vacate_site, use_inventory, finish_preparation
from .intelligence import Memory, Intelligence

LOG = logging.getLogger(__name__)


class Agent:
    """A long-lived process serves isolated team memories. HTTP serializes decide()."""
    def __init__(self, config=None):
        self.cfg = config or Config()
        self.sessions = OrderedDict()

    def decide(self, data):
        started = monotonic()
        turn = Turn(data, self.cfg)
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = (*turn.key, turn.station.pos if turn.station else None)
        mem = self.sessions.get(key)
        if mem and mem.last_round == turn.round and mem.last_digest == digest:
            return json.loads(json.dumps(mem.last_response))
        if mem is None or turn.round < mem.last_round:
            mem = Memory()
            self.sessions[key] = mem
        self.sessions.move_to_end(key)
        while len(self.sessions) > 8:
            self.sessions.popitem(last=False)
        previous_mines = mem.mine_kinds.copy()
        mem.observe(turn, self.cfg)
        if mem.last_round < 0 or previous_mines != mem.mine_kinds:
            LOG.info("round=%s source=mapInfo.zones mines_received=%s mines=%s", turn.round,
                     len(mem.mine_kinds), json.dumps(
                         [{"type": kind, "pos": list(p)} for p, kind in sorted(mem.mine_kinds.items())],
                         ensure_ascii=False))
        towers, walls = layout(turn, self.cfg)
        ledger = Ledger(turn, self.cfg, towers, walls)
        nav = Navigator(turn, started + self.cfg.decision_seconds)
        intel = Intelligence(turn, self.cfg, mem)
        prompt, execute = "", ""
        try:
            h = turn.pioneer
            within_timeout = bool(h and turn.phase_task and turn.round - mem.task_started <
                                  min(mem.task_timeout, self.cfg.task_max_rounds))
            home = [w.cells for w in turn.weapons] or ([turn.station.cells] if turn.station else [])
            task_return = min((route[0] for cells in home
                               if (route := nav.approach(h, cells)) is not None), default=130) if within_timeout else 0
            danger = bool(h and any(turn.threatens_us(r) and (
                turn.base_distance(r.pos) <= max(self.cfg.task_danger_radius,
                                                 task_return + self.cfg.return_margin + r.attack_range)
                or distance(h.pos, r.pos) <= max(self.cfg.task_danger_radius, r.attack_range + 2))
                for r in turn.robots))
            # First-wave readiness has a hard return deadline. On later nights
            # a task may continue while no wave threatens the pioneer or base.
            first_watch = (turn.day == 1 and bool(home)
                           and turn.day_left <= task_return + self.cfg.return_margin)
            hold_task = within_timeout and not danger and not first_watch and not mem.stop_reason
            if not turn.is_day:
                emergency_items(turn, ledger)
            pairs = assignments(turn, nav, ledger, excluded={h.id} if hold_task else ())
            ledger.return_targets = {hero.id: tower.cells for hero, tower in pairs}
            returning = set()
            for hero, tower in pairs:
                route = nav.approach(hero, [tower.pos], ledger.reserved)
                if route is not None and (not turn.is_day or turn.day_left <= route[0] + self.cfg.return_margin):
                    returning.add(hero.id)
                    LOG.debug("round=%s worker_or_pioneer=%s return_to_tower=%s steps=%s day_left=%s",
                              turn.round, hero.id, tower.id, route[0], turn.day_left)
            if h and turn.phase_task:
                # Submit a ready answer before a return movement can cancel it.
                # LLM/sandbox work holds the pioneer at the task point and gets
                # its own chance before expensive worker connectivity searches.
                if within_timeout and (mem.answer is not None or hold_task):
                    available = min(mem.task_timeout, self.cfg.task_max_rounds) - (turn.round - mem.task_started)
                    prompt, execute = intel.task(ledger, available_rounds=available)
                if hold_task:
                    ledger.used.add(h.id)
                elif not mem.stop_reason:
                    mem.stop_reason = ("defence_threat" if danger else
                                       "first_wave_deadline" if first_watch else "task_deadline")
                    LOG.info("round=%s task_stop=%s", turn.round, mem.stop_reason)
            if not turn.is_day:
                defend(turn, nav, ledger, pairs)
                for hero in turn.heroes:
                    if hero.id not in ledger.used and use_inventory(turn, nav, ledger, hero, local_only=True):
                        continue
                    if hero.id not in ledger.used and hero.id not in {h.id for h, _ in pairs} and turn.station:
                        walk(nav, ledger, hero, turn.station.cells)
            else:
                for hero, tower in pairs:
                    if hero.id in returning and hero.id not in ledger.used:
                        finish_preparation(turn, self.cfg, mem, nav, ledger, hero, tower, walls)
                defend(turn, nav, ledger, [(h, w) for h, w in pairs if h.id in returning])
                workers(turn, self.cfg, mem, nav, ledger, towers, walls, returning)
                if h and h.id not in ledger.used and h.id not in returning:
                    if turn.phase_task:
                        if not hold_task and turn.station:
                            walk(nav, ledger, h, turn.station.cells)
                    else:
                        pioneer(turn, self.cfg, mem, nav, ledger, h)
                        if h.id not in ledger.used:
                            vacate_site(turn, nav, ledger, h, towers + walls)
                if not prompt and not execute:
                    prompt = intel.news()
        except DeadlineExceeded:
            LOG.warning("round=%s budget reached; returning %s validated actions", turn.round, len(ledger.commands))
        response = ledger.response(prompt, execute)
        mem.last_round, mem.last_digest, mem.last_response = turn.round, digest, response
        mem.last_commands = response["roleCommandMap"]
        if turn.is_day:
            for worker in turn.workers:
                LOG.debug("round=%s worker=%s position=%s free_space=%s mining_target=%s command=%s",
                          turn.round, worker.id, worker.pos, worker.space, mem.mine_targets.get(worker.id),
                          response["roleCommandMap"].get(str(worker.id)))
        LOG.info("round=%s day=%s phase=%s commands=%s latency_ms=%.2f", turn.round, turn.day,
                 "day" if turn.is_day else "night", len(ledger.commands), (monotonic()-started)*1000)
        return json.loads(json.dumps(response))
