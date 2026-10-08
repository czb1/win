"""Protocol and log-extraction checks without game simulations."""
from contextlib import redirect_stderr
from copy import deepcopy
import http.client
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_agent import payload, unit, ROOT
from test_evolution import task_payload
from test_logging_system import capture, records
from agent.brain import Agent
from agent.config import Config
from agent.commands import EMPTY, Ledger, command
from agent.logging_system import (configure_logging, ContextFilter, WireFormatter,
                                  emit_payload, request_context)
from agent.model import Turn
from agent.server import Server

sys.path.insert(0, str(ROOT / "tools"))
from log_records import events, ReadReport
from extract_logs import main as extract


class TxtDiagnosticsTests(unittest.TestCase):
    def test_snapshot_preserves_new_fields_dead_units_and_visibility(self):
        data = payload(1)
        data["teamOur"]["roles"] += [unit(14, "imp", 8, 8), unit(15, "worker", 9, 8, health=0)]
        data["teamOur"]["roles"][1]["isDriving"] = True
        data["teamOur"]["summonRobotList"] = [unit(81, "smallRobot", 8, 9, targetTeam="defender")]
        data["robot"]["roles"] = deepcopy(data["teamOur"]["summonRobotList"])
        data["mapInfo"]["zones"] = [{"pos": {"x": 5, "y": 5}, "neutralType": "iron", "remain": 4}]
        data["teamEnemy"]["roles"] = [unit(99, "worker", 12, 12)]
        agent = Agent(Config(layout_mode="explicit", llm_enabled=False))
        with capture() as stream:
            agent.decide(data)
            following = deepcopy(data)
            following["roundNo"] = 2
            following["teamEnemy"]["roles"] = []
            agent.decide(following)
        result = records(stream)
        snapshots = [r["data"] for r in result if r["event"] == "turn_snapshot"]
        self.assertEqual(snapshots[0]["mapInfo"], data["mapInfo"])
        self.assertEqual(snapshots[0]["teamOur"], data["teamOur"])
        self.assertEqual(snapshots[1]["teamEnemy"]["roles"], [])
        self.assertEqual(snapshots[1]["enemy_last_seen"][0]["last_seen_round"], 1)
        decision = next(r for r in result if r["event"] == "unit_decision" and r["unit_id"] == 14)
        self.assertEqual(decision["data"]["reason"], "imp_escape")
        self.assertFalse(decision["data"]["conditions"]["safe_exit"])

    def test_feedback_distinguishes_legality_effect_and_skipped_rounds(self):
        data = payload(1)
        data["mapInfo"]["zones"] = [{"pos": {"x": 8, "y": 8}, "neutralType": "iron"},
                                    {"pos": {"x": 1, "y": 5}, "neutralType": "vendor"}]
        data["vendorShopList"] = [{"name": "iron", "price": 3}]
        agent = Agent(Config(layout_mode="explicit", llm_enabled=False))
        with capture() as stream:
            first = agent.decide(data)
            self.assertTrue(any(c["action"] == "move" for c in first["roleCommandMap"].values()))
            data["roundNo"] = 2
            data["lastRoundRoleActionResults"] = {uid: True for uid in first["roleCommandMap"]}
            agent.decide(data)
            data["roundNo"] = 5
            agent.decide(data)
        feedback = [r["data"] for r in records(stream) if r["event"] == "previous_feedback"]
        self.assertTrue(feedback[1]["correlated"])
        moved = [a for a in feedback[1]["actions"] if a["command"]["action"] == "move"]
        self.assertTrue(all(a["legality"] is True and a["effect"] == "target_not_observed" for a in moved))
        self.assertFalse(feedback[2]["correlated"])
        self.assertEqual(feedback[2]["actions"], [])

    def test_cache_keeps_response_and_has_distinct_request_with_same_session(self):
        agent = Agent(Config(layout_mode="explicit", llm_enabled=False))
        with capture() as stream:
            first = agent.decide(payload(1))
            second = agent.decide(payload(1))
        self.assertEqual(first, second)
        responses = [r for r in records(stream) if r["event"] == "turn_response"]
        self.assertEqual(responses[0]["session"], responses[1]["session"])
        self.assertNotEqual(responses[0]["request_id"], responses[1]["request_id"])
        self.assertTrue(responses[1]["data"]["cached"])

    def test_rejection_diagnostics_preserve_validator_behavior(self):
        turn = Turn(payload(roles=[unit(10, "worker", 1, 1)]), Config())
        ledger = Ledger(turn, Config(), [], [])
        self.assertFalse(ledger.add(10, {"action": "move", "targetPos": [{"x": [], "y": 2}]}))
        self.assertFalse(ledger.add(10, command("move", (12, 12))))
        self.assertFalse(ledger.commands)
        self.assertEqual(ledger.gold, turn.gold)
        self.assertIn("invalid_target_position", ledger.rejections[10])
        self.assertIn("move_not_adjacent_or_occupied", ledger.rejections[10])

    def test_mining_reason_includes_route_and_candidate_conditions(self):
        data = payload(1)
        data["mapInfo"]["zones"] = [{"pos": {"x": 8, "y": 8}, "neutralType": "iron"},
                                    {"pos": {"x": 1, "y": 5}, "neutralType": "vendor"}]
        data["vendorShopList"] = [{"name": "iron", "price": 3}]
        with capture() as stream:
            Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(data)
        decisions = [r["data"] for r in records(stream) if r["event"] == "unit_decision"]
        mining = next(d for d in decisions if d["reason"] == "mine_for_sale")
        self.assertGreater(mining["conditions"]["candidate_count"], 0)
        self.assertIn("route_steps", mining["conditions"])
        self.assertTrue(mining["selected_at"])

    def test_metadata_failure_does_not_change_accepted_command_or_rejection(self):
        turn = Turn(payload(roles=[unit(10, "worker", 1, 1)]), Config())
        ledger = Ledger(turn, Config(), [], [])
        with patch.object(ledger, "explain", side_effect=OSError("diagnostic failed")), redirect_stderr(io.StringIO()):
            self.assertTrue(ledger.add(10, command("move", (1, 2))))
            self.assertFalse(ledger.add(999, None))
        self.assertEqual(ledger.commands["10"], command("move", (1, 2)))

    def test_invalid_turn_is_attributed_and_context_is_released(self):
        with capture() as stream:
            with self.assertRaises(ValueError):
                Agent().decide({**payload(70), "mapInfo": {"width": 0, "height": 15}})
            logging.info("outside")
        invalid = next(r for r in records(stream) if r["event"] == "turn_invalid")
        self.assertEqual(invalid["round"], 70)
        self.assertTrue(invalid["request_id"])
        self.assertIsNone(records(stream)[-1]["request_id"])

    def test_http_busy_fallback_has_request_and_response_context(self):
        server = Server(("127.0.0.1", 0), Config(layout_mode="explicit"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server.lock.acquire()
        try:
            with capture() as stream:
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                client.request("POST", "/", body=json.dumps(payload(42)), headers={"Content-Type": "application/json"})
                response = client.getresponse()
                self.assertEqual(json.loads(response.read()), EMPTY)
                client.close()
            busy = next(r for r in records(stream) if r["event"] == "lock_busy")
            sent = next(r for r in records(stream) if r["event"] == "http_response")
            self.assertEqual(busy["round"], 42)
            self.assertEqual(busy["request_id"], sent["request_id"])
        finally:
            server.lock.release()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_chunks_restore_full_unicode_and_report_missing_or_corrupt_data(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(ContextFilter())
        handler.setFormatter(WireFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        text = "详细题目\n" * 12000
        try:
            with request_context():
                emit_payload("task_description", text)
        finally:
            root.removeHandler(handler)
        physical = stream.getvalue().splitlines()
        self.assertGreater(len(physical), 2)
        self.assertTrue(all(len(line.encode()) < 8192 for line in physical))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "match.txt")
            path.write_text("startup\n" + "\n".join("[platform time] " + line for line in physical))
            restored = list(events(path))
            self.assertEqual(restored[0]["data"]["content"], text)
            path.write_text("\n".join(physical[:-1]))
            report = ReadReport()
            self.assertEqual(list(events(path, report, warn=False)), [])
            self.assertEqual(report.incomplete_records, 1)
            objects = [json.loads(line.split("FWLOG ", 1)[1]) for line in physical]
            objects[-1]["fragment"]["sha256"] = "bad"
            path.write_text("\n".join("FWLOG " + json.dumps(r) for r in objects))
            report = ReadReport()
            list(events(path, report, warn=False))
            self.assertGreater(report.invalid_lines, 0)

    def test_required_trace_survives_warning_level_and_output_errors(self):
        root = logging.getLogger()
        previous = root.level
        previous_handlers = list(root.handlers)
        stream = io.StringIO()
        try:
            with patch("sys.stderr", stream):
                configure_logging("WARNING")
                Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(payload(1))
            self.assertIn('"event":"turn_snapshot"', stream.getvalue())
            class BrokenStream:
                def write(self, value):
                    raise OSError("disk full")
                def flush(self):
                    pass
            handler = next(h for h in root.handlers if getattr(h, "game_log_handler", False))
            handler.stream = BrokenStream()
            with redirect_stderr(io.StringIO()) as fallback:
                response = Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(payload(1))
            self.assertIn("roleCommandMap", response)
            self.assertIn("logging_failed", fallback.getvalue())
        finally:
            for handler in list(root.handlers):
                if handler not in previous_handlers:
                    root.removeHandler(handler)
                    handler.close()
            for handler in previous_handlers:
                if handler not in root.handlers:
                    root.addHandler(handler)
            root.setLevel(previous)

    def test_extraction_restores_out_of_window_task_payload_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "match.txt")
            source = [dict(run="R", session="S", team="A", round=1, event="session_started", data={"config": {"round_origin": 0}}),
                      dict(run="R", session="S", team="A", round=2, task_id="S/r2", event="task_description", category="evolution",
                           record_id="definition", data={"payload_id": "question", "content": "完整题目"}),
                      dict(run="R", session="S", team="A", round=50, task_id="S/r2", request_id="request", event="task_description",
                           category="evolution", data={"payload_id": "question"}),
                      dict(run="R", session="S", team="A", round=50, request_id="request", event="turn_snapshot", category="snapshot", data={}),
                      dict(run="R", session="other", team="B", round=50, event="task_description", category="evolution", data={"content": "另一队"})]
            path.write_text("\n".join("time FWLOG " + json.dumps(r, ensure_ascii=False) for r in source))
            output = Path(directory, "issue")
            self.assertEqual(extract([str(path), "--from-round", "50", "--to-round", "50", "--context", "0",
                                      "--session", "S", "--out", str(output), "--split"]), 0)
            issue = list(events(output / "issue.txt"))
            self.assertTrue(any(r.get("included_as") == "payload_dependency" for r in issue))
            self.assertTrue(any(r.get("event") == "session_started" for r in issue))
            self.assertNotIn("另一队", (output / "issue.txt").read_text())
            self.assertIn("完整题目", (output / "tasks.txt").read_text())
            self.assertEqual(len(list(events(output / "turns.jsonl"))), 1)
            self.assertEqual(json.loads((output / "meta.json").read_text())["missing_payloads"], [])

    def test_task_id_filter_exports_whole_task_and_unit_filter_keeps_scene(self):
        agent = Agent(Config(layout_mode="explicit"))
        with capture() as stream:
            agent.decide(task_payload(1, "第一任务"))
            agent.decide(task_payload(2, "第一任务"))
            agent.decide(task_payload(3, ""))
            agent.decide(task_payload(4, "第二任务"))
        result = records(stream)
        task = next(r["task_id"] for r in result if r["event"] == "task_started")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "match.txt")
            path.write_text("\n".join("FWLOG " + json.dumps(r, ensure_ascii=False) for r in result))
            output = Path(directory, "task")
            self.assertEqual(extract([str(path), "--task-id", task, "--context", "0", "--out", str(output)]), 0)
            task_result = list(events(output / "tasks.txt"))
            self.assertTrue(any(r["event"] == "task_started" for r in task_result))
            self.assertFalse(any(isinstance(r.get("data"), dict) and r["data"].get("content") == "第二任务" for r in task_result))
            self.assertEqual(extract([str(path), "--unit-id", "11", "--from-round", "1", "--to-round", "1",
                                      "--context", "0", "--out", str(Path(directory, "unit")), "--split"]), 0)
            self.assertTrue(list(events(Path(directory, "unit/turns.jsonl"))))

    def test_reader_accepts_utf16_txt_and_detects_sequence_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "windows.txt")
            source = [{"run": "R", "sequence": n, "round": n, "event": "sample"} for n in (1, 3)]
            path.write_text("\n".join("FWLOG " + json.dumps(r) for r in source), encoding="utf-16")
            report = ReadReport()
            self.assertEqual(len(list(events(path, report))), 2)
            self.assertEqual(report.summary()["sequence_gaps"], [{"run": "R", "from": 2, "to": 2}])


if __name__ == "__main__":
    unittest.main()
