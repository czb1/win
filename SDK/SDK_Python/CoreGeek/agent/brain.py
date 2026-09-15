import hashlib
import json
import logging
from collections import OrderedDict
from time import monotonic
from .config import Config
from .model import Turn
from .navigation import Navigator, layout, DeadlineExceeded
from .commands import Ledger, command
from .combat import assignments, defend, emergency_items
from .economy import worker, pioneer, walk
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
        mem.observe(turn, self.cfg)
        towers, walls = layout(turn, self.cfg)
        ledger = Ledger(turn, self.cfg, towers, walls)
        nav = Navigator(turn, started + self.cfg.decision_seconds)
        intel = Intelligence(turn, self.cfg, mem)
        prompt, execute = "", ""
        try:
            if not turn.is_day:
                emergency_items(turn, ledger)
            pairs = assignments(turn, nav, ledger)
            returning = set()
            for hero, tower in pairs:
                route = nav.approach(hero, [tower.pos], ledger.reserved)
                length = route[0] if route else 130
                if not turn.is_day or turn.day_left <= length + self.cfg.return_margin:
                    returning.add(hero.id)
            if not turn.is_day:
                defend(turn, nav, ledger, pairs)
                for hero in turn.heroes:
                    if hero.id not in ledger.used and hero.id not in {h.id for h, _ in pairs} and turn.station:
                        walk(nav, ledger, hero, turn.station.cells)
            else:
                defend(turn, nav, ledger, [(h, w) for h, w in pairs if h.id in returning])
                for hero in turn.workers:
                    if hero.id not in ledger.used and hero.id not in returning:
                        worker(turn, self.cfg, mem, nav, ledger, hero, towers, walls, True)
                h = turn.pioneer
                if h and h.id not in ledger.used and h.id not in returning:
                    if turn.phase_task:
                        elapsed = turn.round - mem.task_started
                        if elapsed < min(mem.task_timeout, self.cfg.task_max_rounds) and mem.task_failures < 3:
                            prompt, execute = intel.task(ledger)
                        elif turn.station:
                            walk(nav, ledger, h, turn.station.cells)
                    else:
                        pioneer(turn, self.cfg, mem, nav, ledger, h)
                if not prompt and not execute:
                    prompt = intel.news()
        except DeadlineExceeded:
            LOG.warning("round=%s budget reached; returning %s validated actions", turn.round, len(ledger.commands))
        response = ledger.response(prompt, execute)
        mem.last_round, mem.last_digest, mem.last_response = turn.round, digest, response
        mem.last_commands = response["roleCommandMap"]
        LOG.info("round=%s day=%s phase=%s commands=%s latency_ms=%.2f", turn.round, turn.day,
                 "day" if turn.is_day else "night", len(ledger.commands), (monotonic()-started)*1000)
        return json.loads(json.dumps(response))
