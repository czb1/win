"""Encrypted sinks, corruption recovery and a downloaded single-file workflow."""
import base64
from contextlib import contextmanager, redirect_stderr
from copy import deepcopy
import io
import json
import logging
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SDK/SDK_Python/CoreGeek"))
sys.path.insert(0, str(ROOT / "tools"))
from agent import logging_system as logging_impl
from agent.log_crypto import LogEncryptor, load_log_key, packet_lines, LogCryptoError
import extract_logs as extractor

FIXTURES = ROOT / "tests/fixtures/log_crypto"
PRIVATE = FIXTURES / "rsa-test-key.json"
PUBLIC = FIXTURES / "rsa-test-public.json"


@contextmanager
def encrypted_logging(stream, directory=None):
    root = logging.getLogger()
    previous_handlers, previous_level = list(root.handlers), root.level
    previous_mode = logging_impl._encrypted_logging
    try:
        with patch("sys.stderr", stream):
            logging_impl.configure_logging("INFO", directory, PUBLIC)
            yield
    finally:
        for handler in list(root.handlers):
            if handler not in previous_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in previous_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(previous_level)
        logging_impl._encrypted_logging = previous_mode


def seal_records(records, writer, wire=True):
    return [line for record in records for line in packet_lines(
        writer.seal(json.dumps(record, ensure_ascii=False).encode()), wire)]


class LogEncryptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = load_log_key(PRIVATE, True)

    def read(self, path, report=None):
        return list(extractor.events(path, report, warn=False, decryptor=extractor.LogDecryptor([self.key])))

    def test_all_three_sinks_and_exceptions_hide_original_text(self):
        secret = "秘密原文不应出现在比赛日志中"
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with encrypted_logging(stream, directory):
                logging.warning(secret)
                try:
                    raise ValueError(secret)
                except ValueError:
                    logging.exception("failure")
            root = Path(directory)
            (root / "stderr.log").write_text(stream.getvalue())
            results = []
            for name in ("stderr.log", "game.log", "events.jsonl"):
                text = (root / name).read_text()
                self.assertNotIn(secret, text)
                self.assertNotIn("ValueError", text)
                results.append(self.read(root / name))
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0], results[2])
            self.assertIn(secret, results[0][0]["message"])
            self.assertIn(secret, results[0][1]["exception"])
            # Each sink reuses the same complete packet, not a freshly encrypted copy.
            self.assertEqual(stream.getvalue(), (root / "game.log").read_text())

    def test_encryption_failure_does_not_leak_or_fail_the_caller(self):
        stream = io.StringIO()
        with encrypted_logging(stream):
            with patch.object(LogEncryptor, "seal", side_effect=ValueError("SECRET-DATA")):
                logging.error("SECRET-DATA")
        self.assertIn("logging_failed", stream.getvalue())
        self.assertNotIn("SECRET-DATA", stream.getvalue())

    def test_invalid_public_key_creates_no_plaintext_outputs(self):
        root = logging.getLogger()
        previous = list(root.handlers)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "logs"
            with self.assertRaises(LogCryptoError):
                logging_impl.configure_logging("INFO", output, PRIVATE)
            self.assertFalse(output.exists())
            self.assertEqual(root.handlers, previous)

    def test_out_of_order_fragments_and_distinct_encryption_epochs(self):
        records = [{"run": str(i), "record_id": str(i), "round": 9,
                    "data": {"content": secrets.token_hex(20000)}} for i in range(2)]
        batches = [seal_records([record], LogEncryptor(self.key)) for record in records]
        self.assertTrue(all(len(batch) > 1 for batch in batches))
        self.assertTrue(all(len(line.encode()) < 8192 for batch in batches for line in batch))
        interleaved = [line for pair in zip(*batches) for line in pair]
        interleaved += [line for batch in batches for line in batch[len(min(batches, key=len)):]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "match.log"
            path.write_text("\n".join("[platform timestamp] " + line for line in reversed(interleaved)))
            restored = self.read(path)
            self.assertEqual(sorted(restored, key=lambda r: r["run"]), records)
            path.write_text("\n".join(batches[0][1:]))
            report = extractor.ReadReport()
            self.assertEqual(self.read(path, report), [])
            self.assertEqual(report.incomplete_records, 1)

    def test_tampered_packet_is_reported_and_good_records_survive(self):
        writer = LogEncryptor(self.key)
        good = {"run": "R", "record_id": "ok", "round": 1, "message": "good"}
        bad = writer.seal(b'{"round":2,"message":"SECRET"}')
        data = bytearray(base64.b64decode(bad["ciphertext"]))
        data[0] ^= 1
        bad["ciphertext"] = base64.b64encode(data).decode()
        lines = seal_records([good], writer) + ["FWENC " + json.dumps(bad)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "match.log"
            path.write_text("\n".join(lines))
            report = extractor.ReadReport()
            self.assertEqual(self.read(path, report), [good])
            self.assertEqual(report.crypto_errors, 1)
            self.assertEqual(report.encrypted_records, 1)
            self.assertIn("authentication failed", report.problems[0]["reason"])
            with redirect_stderr(io.StringIO()):
                self.assertEqual(extractor.main([str(path), "--private-key", str(PRIVATE), "--out", str(Path(directory) / "out")]), 2)
            meta = json.loads((Path(directory) / "out/meta.json").read_text())
            self.assertEqual(meta["read_report"]["crypto_errors"], 1)

    def test_rotation_preserves_encrypted_reader_compatibility(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with encrypted_logging(stream, directory):
                handler = next(h for h in logging.getLogger().handlers if getattr(h, "baseFilename", "").endswith("game.log"))
                handler.maxBytes = 1
                logging.warning("first record")
                logging.warning("second record")
            root = Path(directory)
            self.assertEqual(self.read(root / "game.log")[0]["message"], "second record")
            self.assertEqual(self.read(root / "game.log.1")[0]["message"], "first record")

    def test_replay_response_is_identical_and_default_logging_requires_public_key(self):
        command = [sys.executable, str(ROOT / "tools/replay.py"), str(ROOT / "examples/request.json"), "--log-level", "INFO"]
        plain = subprocess.run(command + ["--plaintext-logs"], capture_output=True, text=True, timeout=10)
        encrypted = subprocess.run(command + ["--log-public-key", str(PUBLIC)], capture_output=True, text=True, timeout=10)
        self.assertEqual(encrypted.returncode, 0, encrypted.stderr)
        self.assertEqual(json.loads(plain.stdout), json.loads(encrypted.stdout))
        self.assertIn("FWENC ", encrypted.stderr)
        self.assertNotIn("turn_snapshot", encrypted.stderr)
        missing = subprocess.run(command + ["--log-public-key", "missing-public.json"], capture_output=True, text=True, timeout=10)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")

    def test_legacy_message_can_mention_encryption_markers_without_a_key(self):
        record = {"run": "legacy", "record_id": "old", "round": 1,
                  "message": "Use FWENC {JSON} to encrypt a FWLOG record", "data": {}}
        raw = json.dumps(record)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.log"
            for line in (raw, "FWLOG " + raw, "[platform time] FWLOG " + raw):
                path.write_text(line)
                report = extractor.ReadReport()
                self.assertEqual(list(extractor.events(path, report, warn=False)), [record])
                self.assertEqual(report.crypto_errors, 0)

    def test_downloaded_script_decrypts_and_preserves_all_modes(self):
        base = dict(run="R", session="S", team="A", day=2, phase="night")
        records = [
            dict(base, round=1, record_id="metadata", event="session_started", data={}),
            dict(base, round=2, record_id="question", task_id="S/r2", event="task_description", category="evolution",
                 data={"payload_id": "Q", "content": "完整题目"}),
            dict(base, round=50, record_id="reference", task_id="S/r2", event="task_description", category="evolution", data={"payload_id": "Q"}),
            dict(base, round=50, record_id="snapshot", event="turn_snapshot", category="snapshot", data={}),
            dict(base, round=50, record_id="decision", event="unit_decision", category="decision", unit_id="worker", data={"role_type": "worker"}),
            dict(base, round=50, record_id="treasure", event="task_question", category="long_context", task_id="S/long-context", data={"content": "累计传闻"}),
            dict(base, round=51, record_id="legacy", event="turn_snapshot", category="snapshot", data={}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "tools/extract_logs.py", root / "extract_logs.py")
            shutil.copyfile(PRIVATE, root / "local-key.json")
            plain = ["FWLOG " + json.dumps(r, ensure_ascii=False) for r in records]
            encrypted = seal_records(records[:-1], LogEncryptor(self.key)) + plain[-1:]
            for encoding in ("utf-8-sig", "utf-16"):
                (root / "plain.log").write_text("\n".join(plain), encoding=encoding)
                (root / "encrypted.log").write_text("\n".join("[timestamp] " + line for line in encrypted), encoding=encoding)
                def run(log, *args):
                    return subprocess.run([sys.executable, "-I", "extract_logs.py", log, *args], cwd=root,
                                          capture_output=True, text=True, encoding="utf-8", timeout=15)
                for mode in ("all", "evolution", "long-context", "non-task"):
                    args = ["--mode", mode, "--day", "2", "--phase", "night", "--from-round", "50", "--to-round", "51", "--context", "0", "--split"]
                    for log, out, keys in (("plain.log", "plain", []), ("encrypted.log", "encrypted", ["--private-key", "local-key.json"])):
                        result = run(log, *args, "--out", out, *keys)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    for name in ("issue.txt", "tasks.txt", "turns.jsonl", "decisions.jsonl", "feedback.jsonl", "errors.jsonl"):
                        def normalized(folder):
                            data = (root / folder / name).read_text().splitlines()
                            parsed = [json.loads(line.removeprefix("FWLOG ")) for line in data]
                            return [r for r in parsed if r.get("event") != "log_integrity"]
                        self.assertEqual(normalized("plain"), normalized("encrypted"), (encoding, mode, name))
                listed = run("encrypted.log", "--private-key", "local-key.json", "--list")
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertIn("S/r2", listed.stdout)
                missing = run("encrypted.log", "--out", "no-key")
                self.assertEqual(missing.returncode, 2)
                self.assertNotIn("Traceback", missing.stderr)
                self.assertFalse((root / "no-key").exists())

    def test_downloaded_script_can_generate_keys_without_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "tools/extract_logs.py", root / "extract_logs.py")
            result = subprocess.run([sys.executable, "-I", "extract_logs.py", "--generate-keys", "local.json",
                                     "--public-key-out", "public.json", "--key-bits", "2048"], cwd=root,
                                    capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            key = load_log_key(root / "local.json", True)
            public = load_log_key(root / "public.json")
            self.assertEqual(key["n"], public["n"])
            packet = LogEncryptor(public).seal(b'{"generated":true}')
            self.assertEqual(extractor.LogDecryptor([key]).open(packet), {"generated": True})
            shutil.copyfile(PRIVATE, root / "wrong-key.json")
            (root / "match.log").write_text("\n".join(packet_lines(packet)))
            wrong = subprocess.run([sys.executable, "-I", "extract_logs.py", "match.log", "--private-key",
                                    "wrong-key.json", "--out", "rejected"], cwd=root, capture_output=True,
                                   text=True, encoding="utf-8", timeout=10)
            self.assertEqual(wrong.returncode, 2)
            self.assertNotIn("Traceback", wrong.stderr)
            self.assertFalse((root / "rejected").exists())
