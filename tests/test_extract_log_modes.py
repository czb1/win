"""Check strict task/day/phase selection using the standalone CLI."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def record(identity, number, day, phase):
    return dict(run="R", session="S", team="A", record_id=identity, message=identity, round=number,
                day=day, phase=phase, event="diagnostic", category="general", data={})


def mixed_records():
    def row(identity, number, day, phase, **fields):
        base = record(identity, number, day, phase)
        base.update(fields)
        return base
    return [
        row("session", 1, 1, "day", event="session_started", category="runtime", data={"config": {}}),
        row("evo-definition", 2, 1, "day", event="task_description", category="evolution", task_id="E",
            data={"payload_id": "evo-question", "content": "SECRET_EVOLUTION"}),
        row("long-definition", 3, 1, "day", event="received_folkLegends", category="long_context", task_id="L",
            data={"payload_id": "long-question", "content": "SECRET_LONG_CONTEXT"}),
        row("previous-night", 130, 1, "night"),
        row("day-worker-start", 131, 2, "day", event="unit_decision", category="decision", task_id="E",
            data={"role_type": "worker", "reason": "mine_for_sale"}),
        row("evo-ref", 132, 2, "day", event="task_description", category="evolution", task_id="E",
            data={"payload_id": "evo-question"}),
        row("legacy-evo", 132, 2, "day", event="task_retry", task_id="E"),
        row("long-ref", 132, 2, "day", event="received_folkLegends", category="long_context", task_id="L",
            data={"payload_id": "long-question"}),
        row("long-task-event", 132, 2, "day", event="task_clue", category="long_context", task_id="L"),
        row("reasoning", 132, 2, "day", event="received_officialNews", category="reasoning", task_id="N",
            data={"content": "SECRET_REASONING"}),
        row("pioneer", 132, 2, "day", event="unit_decision", category="decision", task_id="E",
            data={"role_type": "pioneer", "command": {"action": "submitAnswer"}}),
        row("mixed-response", 132, 2, "day", event="turn_response", category="response",
            data={"roleCommandMap": {"10": {"action": "move"}, "11": {"action": "acceptTask"}}}),
        row("orphan-payload", 132, 2, "day", event="received_llmResp", category="recovery",
            data={"content": "SECRET_ORPHAN_PAYLOAD"}),
        row("other-task", 133, 2, "day", event="task_description", category="evolution", task_id="OTHER",
            data={"content": "SECRET_OTHER_TASK"}),
        row("mixed-feedback", 133, 2, "day", event="previous_feedback", category="feedback",
            data={"actions": [{"command": {"action": "move"}}, {"command": {"action": "submitAnswer"}}]}),
        row("treasure-feedback", 134, 2, "day", event="previous_feedback", category="feedback",
            data={"lastSummonTreasureResult": 3}),
        row("task-feedback", 135, 2, "day", event="previous_feedback", category="feedback", task_id="E",
            data={"lastSummonTreasureResult": 0}),
        row("prompt-response", 136, 2, "day", event="turn_response", category="response",
            data={"prompt_chars": 100, "roleCommandMap": {}}),
        row("ordinary-response", 137, 2, "day", event="turn_response", category="response",
            data={"roleCommandMap": {"10": {"action": "move"}}, "prompt_chars": 0, "execute_chars": 0}),
        row("ordinary-feedback", 199, 2, "day", event="previous_feedback", category="feedback",
            data={"lastSummonTreasureResult": 0, "actions": [{"command": {"action": "move"}}]}),
        row("day-worker-end", 199, 2, "day", event="unit_decision", category="decision", data={"role_type": "worker"}),
        row("day-snapshot", 200, 2, "day", event="turn_snapshot", category="snapshot", task_id="E"),
        row("night-battle-start", 201, 2, "night", event="unit_decision", category="decision", task_id="E",
            data={"role_type": "warrior"}),
        row("night-evo", 202, 2, "night", event="received_lastCmdResult", category="evolution", task_id="E"),
        row("night-long", 202, 2, "night", event="received_folkLegends", category="long_context", task_id="L"),
        row("night-reasoning", 202, 2, "night", event="received_officialNews", category="reasoning", task_id="N"),
        row("night-pioneer", 203, 2, "night", event="unit_decision", category="decision", data={"role_type": "pioneer"}),
        row("night-response", 203, 2, "night", event="turn_response", category="response",
            data={"roleCommandMap": {"20": {"action": "attack"}}}),
        row("night-battle-end", 260, 2, "night", event="unit_decision", category="decision", data={"role_type": "cannon"}),
        row("night-feedback", 260, 2, "night", event="previous_feedback", category="feedback",
            data={"lastSummonTreasureResult": 0}),
        row("next-day", 261, 3, "day"),
        row("other-session", 132, 2, "day", session="OTHER", category="evolution", event="task_started"),
        row("other-team", 132, 2, "day", team="B", category="long_context"),
        row("other-run", 132, 2, "day", run="OTHER", category="evolution", event="task_description",
            data={"payload_id": "evo-question", "content": "WRONG_RUN_PAYLOAD"}),
    ]


class ExtractionModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.working = Path(self.temporary.name)
        shutil.copyfile(ROOT / "tools/extract_logs.py", self.working / "extract_logs.py")
        (self.working / "match.log").write_text(
            "\n".join("FWLOG " + json.dumps(row) for row in mixed_records()), encoding="utf-8")
        self.command = [sys.executable, "-I", "extract_logs.py", "match.log", "--run", "R", "--session", "S", "--team", "A"]
        self.output_number = 0

    def invoke(self, *args, expected_code=0):
        result = subprocess.run(self.command + list(args), cwd=self.working,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, expected_code, result.stderr)
        return result

    def extract(self, *args):
        self.output_number += 1
        output = self.working / str(self.output_number)
        self.invoke(*args, "--out", output.name)
        rows = [json.loads(line.split("FWLOG ", 1)[1])
                for line in (output / "issue.txt").read_text(encoding="utf-8").splitlines()]
        meta = json.loads((output / "meta.json").read_text(encoding="utf-8"))
        return rows, meta, output

    def test_evolution_only_and_payload_dependency(self):
        rows, meta, output = self.extract("--mode", "evolution", "--from-round", "132", "--to-round", "132", "--context", "0")
        self.assertEqual({row["message"] for row in rows}, {"evo-ref", "legacy-evo", "evo-definition"})
        self.assertEqual(meta["selected_records"], 2)
        self.assertEqual(meta["filters"]["mode"], "evolution")
        self.assertEqual(meta["session_metadata"][0]["record_id"], "session")
        self.assertEqual(meta["missing_payloads"], [])
        self.assertIn("SECRET_EVOLUTION", (output / "tasks.txt").read_text(encoding="utf-8"))
        self.assertNotIn("WRONG_RUN_PAYLOAD", (output / "issue.txt").read_text(encoding="utf-8"))

    def test_long_context_only_uses_explicit_category(self):
        rows, meta, output = self.extract("--mode", "long-context", "--from-round", "132", "--to-round", "132", "--context", "0")
        self.assertEqual({row["message"] for row in rows}, {"long-ref", "long-task-event", "long-definition"})
        self.assertEqual({row["category"] for row in rows}, {"long_context"})
        self.assertEqual(meta["missing_payloads"], [])
        self.assertIn("SECRET_LONG_CONTEXT", (output / "tasks.txt").read_text(encoding="utf-8"))

    def test_non_task_day_has_strict_boundaries(self):
        rows, meta, output = self.extract("--mode", "non-task", "--day", "2", "--phase", "day", "--split")
        self.assertEqual({row["message"] for row in rows},
                         {"day-worker-start", "ordinary-response", "ordinary-feedback", "day-worker-end", "day-snapshot"})
        self.assertTrue(all(row["day"] == 2 and row["phase"] == "day" for row in rows))
        self.assertNotIn("SECRET_", (output / "issue.txt").read_text(encoding="utf-8"))
        self.assertEqual((output / "tasks.txt").read_text(encoding="utf-8"), "")
        self.assertEqual(meta["task_records"], 0)
        self.assertEqual(len((output / "turns.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_non_task_night_has_strict_boundaries(self):
        rows, meta, output = self.extract("--mode", "non-task", "--day", "2", "--phase", "night")
        self.assertEqual({row["message"] for row in rows},
                         {"night-battle-start", "night-response", "night-battle-end", "night-feedback"})
        self.assertTrue(all(row["day"] == 2 and row["phase"] == "night" for row in rows))
        self.assertEqual(meta["selected_records"], 4)
        self.assertEqual((output / "tasks.txt").read_text(encoding="utf-8"), "")

    def test_list_respects_mode_day_and_phase(self):
        for mode, task, count in (("evolution", "E", 1), ("long-context", "L", 1), ("non-task", "-", 4)):
            with self.subTest(mode=mode):
                result = self.invoke("--mode", mode, "--day", "2", "--phase", "night", "--list")
                self.assertEqual(result.stdout.splitlines()[1:], [f"R\tS\tA\t{task}\t{count}"])

    def test_task_id_stays_strict_with_neighbor_context(self):
        rows, _, _ = self.extract("--mode", "evolution", "--task-id", "E", "--day", "2", "--phase", "day",
                                  "--from-round", "132", "--to-round", "132")
        self.assertEqual({row["message"] for row in rows}, {"evo-ref", "legacy-evo", "evo-definition"})

    def test_empty_selection_does_not_create_output(self):
        result = self.invoke("--mode", "evolution", "--day", "3", "--phase", "night", "--out", "empty", expected_code=1)
        self.assertIn("没有匹配", result.stderr)
        self.assertFalse((self.working / "empty").exists())

    def test_invalid_day_is_rejected(self):
        result = self.invoke("--mode", "non-task", "--day", "0", "--phase", "day", expected_code=2)
        self.assertIn("--day must be at least 1", result.stderr)


if __name__ == "__main__":
    unittest.main()
