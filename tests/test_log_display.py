"""Visible log output stays compact while recovery keeps its original evidence."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_agent import ROOT
from test_log_encryption import encrypted_logging, PRIVATE
from agent import logging_system as logs
from agent.log_display import compact_record, HIDDEN_FIELDS

sys.path.insert(0, str(ROOT / "tools"))
import extract_logs
from query_logs import readable


class LogDisplayTests(unittest.TestCase):
    def test_business_payload_and_exception_survive_without_mutating_source(self):
        source = dict.fromkeys(HIDDEN_FIELDS, "internal")
        source.update(round=0, unit_id=0, event="turn_snapshot", category="snapshot",
                      task_id="S/r1", task_type="自进化类1", message="turn_snapshot",
                      data={"level": 1, "timestamp": None, "valid": False, "count": 0, "items": []},
                      exception="ValueError: first\nsecond", side=None)
        before = deepcopy(source)
        result = compact_record(source)
        self.assertFalse(HIDDEN_FIELDS & result.keys())
        self.assertNotIn("task_id", result)
        self.assertNotIn("task_type", result)
        self.assertNotIn("message", result)
        self.assertNotIn("side", result)
        self.assertEqual(result["round"], 0)
        self.assertEqual(result["unit_id"], 0)
        self.assertEqual(result["data"], source["data"])
        self.assertEqual(result["exception"], source["exception"])
        self.assertEqual(source, before)

    def test_task_scope_is_kept_only_on_related_records(self):
        examples = [
            ("task_outcome", "evolution", {}, True),
            ("received_folkLegends", "long_context", {}, True),
            ("received_officialNews", "reasoning", {}, True),
            ("task_schedule", "general", {}, True),
            ("unit_decision", "decision", {"role_type": "pioneer"}, True),
            ("unit_decision", "decision", {"role_type": "worker"}, False),
            ("turn_response", "response", {"prompt_chars": 10}, True),
            ("turn_response", "response", {"prompt_chars": 0, "execute_chars": 0}, False),
            ("unit_decision", "decision", {"command": {"action": "summonTreasure"}}, True),
            ("previous_feedback", "feedback", {"actions": []}, True),
            ("turn_snapshot", "snapshot", {}, False),
        ]
        for event, category, data, expected in examples:
            with self.subTest(event=event, data=data):
                result = compact_record(dict(event=event, category=category, data=data,
                                             task_id="S/r1", task_type="自进化类1"))
                self.assertEqual("task_id" in result, expected)
                self.assertEqual("task_type" in result, expected)

    def test_empty_task_fields_and_unused_decision_fields_are_omitted(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                result = compact_record(dict(event="task_state", category="evolution",
                                             task_id=value, task_type=value, data={}))
                self.assertNotIn("task_id", result)
                self.assertNotIn("task_type", result)
                self.assertNotIn("data", result)
        result = compact_record(dict(event="unit_decision", data={"role_type": "worker",
            "reason": "mine_for_sale", "build_target": None, "rejections": {},
            "budget_reached": False, "free_space": 0}))
        self.assertEqual(result["data"], {"role_type": "worker", "reason": "mine_for_sale",
                                         "budget_reached": False, "free_space": 0})

    def test_feedback_correlation_and_session_configuration_remain_in_archive(self):
        feedback = dict(event="previous_feedback", data={"previous_request_id": "internal",
                                                         "correlated": False, "feedback_for_round": 0})
        self.assertEqual(compact_record(feedback)["data"], {"correlated": False, "feedback_for_round": 0})
        self.assertEqual(feedback["data"]["previous_request_id"], "internal")
        metadata = dict(event="session_started", data={"config": {"round_origin": 0},
                                                       "version": None, "reason": "round_rewind"})
        self.assertEqual(compact_record(metadata)["data"], {"reason": "round_rewind"})
        self.assertIn("config", metadata["data"])

    def test_plaintext_stream_and_game_file_are_compact_but_archive_is_searchable(self):
        root = logging.getLogger()
        handlers, level, mode = list(root.handlers), root.level, logs._encrypted_logging
        stream = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as directory, patch("sys.stderr", stream):
                logs.configure_logging(log_dir=directory, plaintext_logs=True)
                with logs.request_context():
                    logs.update_context(round=0, session="display-session", task_id="S/r1", task_type="自进化类1")
                    logs.emit_event("unit_decision", {"role_type": "worker", "reason": "mine_for_sale"},
                                    "decision", unit_id=0)
                visible = json.loads(stream.getvalue().removeprefix("FWLOG "))
                self.assertFalse(HIDDEN_FIELDS & visible.keys())
                self.assertNotIn("task_id", visible)
                self.assertNotIn("task_type", visible)
                self.assertEqual(visible["data"]["reason"], "mine_for_sale")
                self.assertEqual(stream.getvalue(), Path(directory, "game.log").read_text())
                stored = json.loads(Path(directory, "events.jsonl").read_text())
                self.assertEqual(stored["session"], "display-session")
                self.assertEqual(stored["level"], "INFO")
                self.assertTrue(stored["request_id"])
        finally:
            for handler in list(root.handlers):
                if handler not in handlers:
                    root.removeHandler(handler)
                    handler.close()
            for handler in handlers:
                if handler not in root.handlers:
                    root.addHandler(handler)
            root.setLevel(level)
            logs._encrypted_logging = mode

    def test_compact_unicode_fragments_restore_and_deduplicate(self):
        text = "完整题目\n" * 2000
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "task_description", (), None)
        record.category = "evolution"
        record.data = {"content": text}
        record.task_id = "S/r1"
        logs.ContextFilter().filter(record)
        lines = logs.WireFormatter(compact=True).format(record).splitlines()
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line.encode("utf-8")) < 8192 for line in lines))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "match.log")
            path.write_text("\n".join(list(reversed(lines)) * 2), encoding="utf-8")
            report = extract_logs.ReadReport()
            result = list(extract_logs.events(path, report, warn=False))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["data"]["content"], text)
        self.assertFalse(HIDDEN_FIELDS & result[0].keys())
        self.assertEqual(report.duplicate_records, 1)
        self.assertEqual(report.invalid_lines, 0)

    def test_query_filters_before_compacting_and_keeps_data_and_exceptions(self):
        record = dict(round=0, event="failed", category="runtime", session="S", level="ERROR",
                      task_id=None, task_type=None, data={"error": "failure", "attempt": 0},
                      exception="ValueError: first\nsecond")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "events.jsonl")
            path.write_text(json.dumps(record), encoding="utf-8")
            command = [sys.executable, str(ROOT / "tools/query_logs.py"), str(path), "--session", "S", "--level", "ERROR"]
            for extra in ([], ["--json"]):
                process = subprocess.run(command + extra, capture_output=True, text=True, timeout=10)
                self.assertEqual(process.returncode, 0, process.stderr)
                if extra:
                    result = json.loads(process.stdout)
                    self.assertFalse(HIDDEN_FIELDS & result.keys())
                    self.assertEqual(result["data"], record["data"])
                    self.assertEqual(result["exception"], record["exception"])
                else:
                    self.assertIn("round=0", process.stdout)
                    self.assertIn('"error":"failure"', process.stdout)
                    self.assertIn("ValueError: first\nsecond", process.stdout)
                    self.assertNotIn("None", process.stdout)
                    self.assertNotIn("task_id", process.stdout)
                    self.assertNotIn("task_type", process.stdout)
        self.assertEqual(readable({"event": "startup"}), "startup")

    def test_encrypted_export_hides_metadata_after_filtering_and_payload_recovery(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with encrypted_logging(stream), logs.request_context():
                logs.update_context(session="compact-example", team="A", round=1)
                logs.emit_event("session_started", {"config": {"round_origin": 0}}, "runtime")
                for number in (2, 50):
                    logs.update_context(round=number, task_id="compact-example/r2", task_type="自进化类1")
                    logs.emit_payload("task_description", "完整题目", "evolution")
            path, output = Path(directory, "match.log"), Path(directory, "out")
            path.write_text(stream.getvalue(), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                status = extract_logs.main([str(path), "--private-key", str(PRIVATE), "--session", "compact-example",
                    "--mode", "evolution", "--from-round", "50", "--to-round", "50", "--context", "0", "--out", str(output)])
            self.assertEqual(status, 0)
            for filename in ("issue.txt", "tasks.txt"):
                result = list(extract_logs.events(output / filename))
                self.assertEqual(len(result), 2)
                self.assertTrue(all(not HIDDEN_FIELDS & item.keys() for item in result))
                self.assertTrue(all(item["task_id"] == "compact-example/r2" for item in result))
                self.assertTrue(any(item.get("data", {}).get("content") == "完整题目" for item in result))
            meta = json.loads((output / "meta.json").read_text())
            self.assertEqual(meta["missing_payloads"], [])
            self.assertEqual(meta["session_metadata"][0]["data"]["config"], {"round_origin": 0})


if __name__ == "__main__":
    unittest.main()
