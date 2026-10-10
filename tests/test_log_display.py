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
from agent.log_crypto import load_log_key
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

    def test_all_plaintext_sinks_store_the_same_compact_record(self):
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
                self.assertEqual(stored, visible)
                self.assertFalse(HIDDEN_FIELDS & stored.keys())
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
        self.assertTrue(all(not HIDDEN_FIELDS & json.loads(line.removeprefix("FWLOG ")).keys() for line in lines))
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
                status = extract_logs.main([str(path), "--private-key", str(PRIVATE), "--session", "scene-1",
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

    def test_encrypted_source_is_compact_even_with_a_direct_decryptor(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with encrypted_logging(stream, directory), logs.request_context():
                logs.update_context(round=0, team="A", session="native-session", task_id="S/r1", task_type="自进化类1")
                logs.emit_event("turn_snapshot", {"level": 0, "timestamp": "business value", "valid": False}, "snapshot")
                logs.emit_event("task_outcome", {"completed": False}, "evolution")
                logging.error("request failed")
            decryptor = extract_logs.LogDecryptor([load_log_key(PRIVATE, True)])
            for text in (stream.getvalue(), Path(directory, "game.log").read_text(),
                         Path(directory, "events.jsonl").read_text()):
                result = [decryptor.open(json.loads(line.removeprefix("FWENC "))) for line in text.splitlines()]
                self.assertTrue(all(not HIDDEN_FIELDS & record.keys() for record in result))
                self.assertNotIn("task_id", result[0])
                self.assertEqual(result[0]["data"], {"level": 0, "timestamp": "business value", "valid": False})
                self.assertEqual(result[1]["task_id"], "S/r1")
                self.assertEqual(result[2]["event"], "error")
                self.assertEqual(result[2]["message"], "request failed")

    def test_split_exports_and_level_queries_are_compact(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with encrypted_logging(stream), logs.request_context():
                logs.update_context(team="A", round=3, session="native-session")
                logs.emit_event("session_started", {"config": {"round_origin": 0}}, "runtime")
                logs.emit_event("turn_started", {}, "runtime")
                logs.emit_event("turn_snapshot", {"valid": False}, "snapshot")
                logs.emit_event("unit_decision", {"role_type": "worker", "reason": "mine"}, "decision", unit_id=0)
                logs.emit_event("previous_feedback", {"feedback_for_round": 2, "correlated": False}, "feedback")
                logging.warning("budget warning")
                logs.emit_event("turn_response", {"roleCommandMap": {}, "cached": False}, "response")
            path, output = Path(directory, "match.log"), Path(directory, "out")
            path.write_text(stream.getvalue(), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(extract_logs.main([str(path), "--private-key", str(PRIVATE), "--split", "--out", str(output)]), 0)
            for name in ("turns", "decisions", "feedback", "errors"):
                result = [json.loads(line) for line in (output / (name + ".jsonl")).read_text().splitlines()]
                self.assertTrue(result, name)
                self.assertTrue(all(not HIDDEN_FIELDS & record.keys() for record in result), name)
            self.assertEqual(json.loads((output / "meta.json").read_text())["requests_without_response"], [])
            process = subprocess.run([sys.executable, str(ROOT / "tools/query_logs.py"), str(path),
                                      "--private-key", str(PRIVATE), "--session", "scene-1", "--level", "WARNING", "--json"],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["message"], "budget warning")
            process = subprocess.run([sys.executable, str(ROOT / "tools/query_logs.py"), str(path),
                                      "--private-key", str(PRIVATE), "--contains", "turn_snapshot", "--json"],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["data"], {"valid": False})

    def test_compact_epochs_deduplicate_and_report_missing_nonce(self):
        from agent.log_crypto import LogEncryptor, packet_lines
        key = load_log_key(PRIVATE, True)
        writer = LogEncryptor(key)
        first = writer.seal(b'{"round":1,"event":"turn_started","team":"A"}')
        writer.seal(b'{"round":1,"event":"turn_snapshot","team":"A"}')
        last = writer.seal(b'{"round":1,"event":"turn_response","team":"A"}')
        other = LogEncryptor(key).seal(b'{"round":1,"event":"turn_started","team":"A"}')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "match.log")
            path.write_text("\n".join(line for packet in (first, last, first, other) for line in packet_lines(packet)))
            report = extract_logs.ReadReport()
            result = list(extract_logs.events(path, report, decryptor=extract_logs.LogDecryptor([key])))
        self.assertEqual(len(result), 3)
        self.assertEqual(report.duplicate_records, 1)
        self.assertEqual(len({extract_logs.scope(record)[0] for record in result}), 2)
        gap = report.summary()["sequence_gaps"]
        self.assertEqual([(item["from"], item["to"]) for item in gap], [(2, 2)])
        self.assertTrue(all(not HIDDEN_FIELDS & record.keys() for record in result))

    def test_scene_boundaries_keep_payloads_and_repeated_round_requests_separate(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with encrypted_logging(stream):
                for scene in ("first", "second"):
                    with logs.request_context():
                        logs.update_context(team="A", session=scene, round=1, task_id=scene + "/r1")
                        logs.emit_event("session_started", {"reason": "round_rewind", "config": {"scene": scene}}, "runtime")
                        logs.emit_event("turn_started", {}, "runtime")
                        logs.emit_payload("task_description", "same question", "evolution")
                        logs.emit_event("turn_response", {}, "response")
                    with logs.request_context():
                        logs.update_context(team="A", session=scene, round=50, task_id=scene + "/r1")
                        logs.emit_event("turn_started", {}, "runtime")
                        logs.emit_payload("task_description", "same question", "evolution")
                        logs.emit_event("turn_response", {}, "response")
                    with logs.request_context():
                        logs.update_context(team="A", session=scene, round=50, task_id=scene + "/r1")
                        logs.emit_event("turn_snapshot", {}, "snapshot")
                        logs.emit_event("cache_hit", {}, "runtime")
                        logs.emit_event("turn_response", {}, "response")
            path, output = Path(directory, "match.log"), Path(directory, "out")
            path.write_text(stream.getvalue(), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(extract_logs.main([str(path), "--private-key", str(PRIVATE), "--session", "scene-2",
                    "--from-round", "50", "--to-round", "50", "--context", "0", "--out", str(output)]), 0)
            result = list(extract_logs.events(output / "issue.txt"))
            questions = [record for record in result if record.get("event") == "task_description"]
            self.assertEqual(len(questions), 2)
            self.assertTrue(all(record["task_id"] == "second/r1" for record in questions))
            self.assertTrue(any(record.get("data", {}).get("content") == "same question" for record in questions))
            meta = json.loads((output / "meta.json").read_text())
            self.assertEqual(meta["missing_payloads"], [])
            self.assertEqual(meta["requests_without_response"], [])
            self.assertEqual(meta["session_metadata"][0]["data"]["config"], {"scene": "second"})
            raw = list(extract_logs.events(path, decryptor=extract_logs.LogDecryptor([load_log_key(PRIVATE, True)])))
            responses = [record for record in raw if extract_logs.record_value(record, "session") == "scene-2"
                         and record.get("round") == 50 and record.get("event") == "turn_response"]
            self.assertNotEqual(extract_logs.record_value(responses[0], "request_id"),
                                extract_logs.record_value(responses[1], "request_id"))


if __name__ == "__main__":
    unittest.main()
