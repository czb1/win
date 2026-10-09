"""Check the downloaded extractor without any repository modules available."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StandaloneExtractionTests(unittest.TestCase):
    def test_single_script_lists_and_extracts_downloaded_logs(self):
        source = [
            dict(run="R", session="S", team="A", round=1, event="session_started", data={}),
            dict(run="R", session="S", team="A", round=2, task_id="S/r2",
                 event="task_description", category="evolution", record_id="definition",
                 data={"payload_id": "question", "content": "完整题目"}),
            dict(run="R", session="S", team="A", round=50, task_id="S/r2",
                 request_id="request", event="task_description", category="evolution",
                 data={"payload_id": "question"}),
            dict(run="R", session="S", team="A", round=50, request_id="request",
                 event="turn_snapshot", category="snapshot", data={}),
            dict(run="R", session="S", team="A", round=50, request_id="request",
                 event="turn_response", data={}),
        ]
        text = "startup\n" + "\n".join("[platform time] FWLOG " + json.dumps(record, ensure_ascii=False)
                                          for record in source)
        for encoding in ("utf-8-sig", "utf-16"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                working = Path(directory)
                script = working / "extract_logs.py"
                shutil.copyfile(ROOT / "tools/extract_logs.py", script)
                log = working / "比赛 日志.log"
                log.write_text(text, encoding=encoding)

                def run(*args):
                    # -I excludes the repository, PYTHONPATH and user packages.
                    result = subprocess.run([sys.executable, "-I", str(script), log.name, *args],
                                            cwd=working, capture_output=True, text=True,
                                            encoding="utf-8", timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return result

                listed = run("--list")
                self.assertIn("S/r2", listed.stdout)
                self.assertFalse((working / "issue").exists())
                run("--from-round", "50", "--to-round", "50", "--context", "0",
                    "--session", "S", "--split", "--out", "issue")

                output = working / "issue"
                issue = [json.loads(line.split("FWLOG ", 1)[1])
                         for line in (output / "issue.txt").read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(record.get("included_as") == "session_metadata" for record in issue))
                self.assertTrue(any(record.get("included_as") == "payload_dependency" for record in issue))
                self.assertIn("完整题目", (output / "tasks.txt").read_text(encoding="utf-8"))
                turns = (output / "turns.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(turns), 1)
                self.assertEqual(json.loads(turns[0])["event"], "turn_snapshot")
                meta = json.loads((output / "meta.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["selected_records"], 3)
                self.assertEqual(meta["missing_payloads"], [])
                self.assertEqual(meta["missing_session_metadata"], [])


if __name__ == "__main__":
    unittest.main()
