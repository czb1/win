"""Multi-turn judger fixtures; no external LLM or live task API is required."""
from pathlib import Path
import json
import shlex
import subprocess
import sys
import tempfile
import unittest

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Memory, Intelligence, parse_task_reply, sandbox_result
from agent.task_tools import document_code
from agent.model import Turn


def task_payload(round_no=1, description=""):
    p = payload(round_no, roles=[unit(11, "pioneer", 5, 5)])
    p["teamOur"]["playerTasks"] = [{"taskType": "自进化类1",
        "taskPosition": {"x": 6, "y": 5}, "timeoutRounds": 60,
        "coldDownRounds": 0, "isValid": True, "scoreReward": 10, "goldReward": 10}]
    p["phaseTask"] = description
    return p


class EvolutionTests(unittest.TestCase):
    def test_relative_task_document_is_discovered_under_rotating_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "tmp", "selfEvolutionTask", "3-fixed", "ws_9")
            root.mkdir(parents=True)
            Path(root, "task_3_gamma.md").write_text("CURRENT TASK", encoding="utf-8")
            code = document_code("task_3_gamma.md").replace(
                "root = '/tmp/selfEvolutionTask'", f"root = {str(Path(directory, 'tmp', 'selfEvolutionTask'))!r}")
            result = subprocess.run([sys.executable, "-c", code], cwd=directory,
                                    capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESOLVED_DOCUMENT", result.stdout)
        self.assertIn("CURRENT TASK", result.stdout)

    def test_old_task_document_is_not_injected_into_new_task(self):
        p = task_payload(20, "读取 task_3_gamma.md，获取任务信息")
        mem = Memory(task_text=p["phaseTask"], task_started=10, task_point=(6, 5),
                     bootstrap_done=True, knowledge=[{"point": (6, 5), "path": "/tmp/old/ws_2/spec.md",
                                                     "output": "STALE", "evidence": "old"}])
        t, cfg, _, ledger = setup_case(p)
        prompt, _ = Intelligence(t, cfg, mem).task(ledger)
        self.assertNotIn("/tmp/old/ws_2", prompt)
        self.assertNotIn("previousDocuments", prompt)

    def test_deadline_forces_final_python_or_answer(self):
        p = task_payload(14, "query")
        mem = Memory(task_text="query", task_started=1, task_timeout=15,
                     bootstrap_done=True, pending=("task", 13))
        p["llmResp"] = "READ another.md"
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        self.assertIsNone(mem.python)
        self.assertGreater(mem.task_failures, 0)
        prompt, _ = Intelligence(t, cfg, mem).task(ledger)
        self.assertIn("最后机会", prompt)
        self.assertNotIn("读取文件：READ", prompt)

    def test_full_task_question_is_kept_while_attempt_logs_are_bounded(self):
        question = "完整题目信息：" + "要求" * 1200
        agent = Agent(Config(layout_mode="explicit"))
        p = task_payload(1, question)
        with self.assertLogs("agent.intelligence", "INFO") as captured:
            agent.decide(p)
        self.assertIn(json.dumps(question, ensure_ascii=False), "\n".join(captured.output))

        reply = "PYTHON\n# " + "x" * 3000 + "\nprint(missing)"
        p.update(roundNo=2, llmResp=reply)
        with self.assertLogs("agent.intelligence", "INFO") as captured:
            agent.decide(p)
        line = next(line for line in captured.output if "task_llm " in line)
        self.assertIn("kind=python", line)
        self.assertIn(f"chars={len(reply)}", line)
        self.assertLess(len(line), 500)
        self.assertNotIn("x" * 500, line)

        p.update(roundNo=3, llmResp="", lastCmdResult="[exitCode:1]\n" + "y" * 3000
                 + "\nValueError: useful tail")
        with self.assertLogs("agent.intelligence", "INFO") as captured:
            agent.decide(p)
        line = next(line for line in captured.output if "task_sandbox " in line)
        self.assertIn("ValueError: useful tail", line)
        self.assertLess(len(line), 800)

    def test_rejected_accept_does_not_keep_stale_task_origin(self):
        p = task_payload(2)
        p["lastRoundRoleActionResults"] = {"11": False}
        mem = Memory(accepted_round=1, task_point=(6, 5), task_timeout=10)
        mem.observe(Turn(p, Config()), Config())
        self.assertIsNone(mem.accepted_round)
        self.assertIsNone(mem.task_point)

    def test_skipped_round_does_not_submit_stale_sandbox_output(self):
        p = task_payload(5, "query")
        p["lastCmdResult"] = "[exitCode:0]\nFINAL_ANSWER\n42"
        mem = Memory(task_text="query", pending=("cmd", 2))
        mem.observe(Turn(p, Config()), Config())
        self.assertIsNone(mem.answer)

    def test_accept_execute_submit_and_reuse_observed_solution(self):
        agent = Agent(Config(layout_mode="explicit"))
        p = task_payload()
        first = agent.decide(p)
        self.assertEqual(first["roleCommandMap"]["11"]["action"], "acceptTask")
        p.update(roundNo=2, phaseTask="读取 weather.json，返回北京温度的整数。")
        self.assertTrue(agent.decide(p)["prompt"])
        # Execute only this fixed, locally authored fixture, never live model code.
        code = ("import json\nfrom pathlib import Path\n"
                "data = json.loads(Path('weather.json').read_text())\n"
                "print('FINAL_ANSWER')\nprint(data['北京'])")
        p.update(roundNo=3, llmResp="PYTHON\n" + code)
        response = agent.decide(p)
        self.assertFalse(response["prompt"])
        args = shlex.split(response["executeCmd"])
        self.assertEqual(args[:2], ["python3", "-c"])
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "weather.json").write_text('{"北京": 21, "上海": 25}', encoding="utf-8")
            result = subprocess.run([sys.executable, "-c", args[2]], cwd=directory,
                                    capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        p.update(roundNo=4, llmResp="", lastCmdResult=f"[exitCode:{result.returncode}]\n{result.stdout}")
        response = agent.decide(p)
        self.assertEqual(response["roleCommandMap"]["11"], {"action": "submitAnswer", "taskAnswer": "21"})
        self.assertFalse(response["prompt"])
        mem = next(iter(agent.sessions.values()))
        self.assertEqual(mem.task_started, 1)
        self.assertEqual(mem.task_timeout, 60)
        p.update(roundNo=5, phaseTask="", lastCmdResult="", lastRoundRoleActionResults={"11": True})
        p["teamOur"]["playerTasks"][0].update(isValid=False, coldDownRounds=30)
        agent.decide(p)
        self.assertEqual(len(mem.skills), 1)
        self.assertEqual(mem.skills[0]["python"], code)
        self.assertFalse(mem.skills[0]["verified"])
        p.update(roundNo=36, lastRoundRoleActionResults={})
        p["teamOur"]["playerTasks"][0].update(isValid=True, coldDownRounds=0)
        agent.decide(p)
        p.update(roundNo=37, phaseTask="读取 weather.json，返回上海温度的整数。")
        response = agent.decide(p)
        self.assertIn("previousSolutions", response["prompt"])
        self.assertIn("weather.json", response["prompt"])
        self.assertIn("legal_submission_then_task_disappeared", response["prompt"])
        self.assertNotIn("11", response["roleCommandMap"])
        self.assertFalse(response["executeCmd"])  # Old city/code is never run blindly.

    def test_only_clean_explicit_final_output_is_auto_submitted(self):
        for output in ("[exitCode:1]\nFINAL_ANSWER\n42", "[TIMEOUT]\nFINAL_ANSWER\n42",
                       "[JUDGER_ERROR]\nFINAL_ANSWER\n42", "[exitCode:0]\nFINAL_ANSWER\n42\n[TRUNCATED]",
                       "[exitCode:0]\n42", "[exitCode:0]\ndebug\nFINAL_ANSWER\n42", ""):
            with self.subTest(output=output):
                p = task_payload(3, "query")
                p["lastCmdResult"] = output
                mem = Memory(task_text="query", pending=("cmd", 2), running_python="print(42)")
                t, cfg, _, ledger = setup_case(p)
                mem.observe(t, cfg)
                prompt, command = Intelligence(t, cfg, mem).task(ledger)
                self.assertTrue(prompt)
                self.assertFalse(command)
                self.assertFalse(ledger.commands)
        self.assertEqual(sandbox_result('[exitCode:0]\nFINAL_ANSWER\n{"城市":"北京"}')[-1], '{"城市":"北京"}')

    def test_failure_then_corrected_code_recovers(self):
        agent = Agent(Config(layout_mode="explicit"))
        p = task_payload(1, "读取指定API返回数值")
        agent.decide(p)
        p.update(roundNo=2, llmResp="PYTHON\nprint(missing)")
        self.assertTrue(agent.decide(p)["executeCmd"])
        p.update(roundNo=3, llmResp="", lastCmdResult="[exitCode:1]\nNameError: missing")
        response = agent.decide(p)
        self.assertIn("NameError", response["prompt"])
        p.update(roundNo=4, lastCmdResult="", llmResp="PYTHON\nprint('FINAL_ANSWER'); print(42)")
        self.assertTrue(agent.decide(p)["executeCmd"])
        p.update(roundNo=5, llmResp="", lastCmdResult="[exitCode:0]\nFINAL_ANSWER\n42")
        self.assertEqual(agent.decide(p)["roleCommandMap"]["11"]["taskAnswer"], "42")

    def test_official_deadline_can_exceed_forty_rounds(self):
        agent = Agent(Config(layout_mode="explicit"))
        p = task_payload(1, "等待API信息")
        agent.decide(p)
        p["roundNo"] = 45
        self.assertTrue(agent.decide(p)["prompt"])
        p["roundNo"] = 61
        self.assertFalse(agent.decide(p)["prompt"])

    def test_ready_answer_is_submitted_before_night_return(self):
        agent = Agent(Config(layout_mode="explicit"))
        p = task_payload(70, "answer")
        agent.decide(p)
        mem = next(iter(agent.sessions.values()))
        mem.answer = "42"
        mem.pending = None
        p["roundNo"] = 71
        response = agent.decide(p)
        self.assertEqual(response["roleCommandMap"]["11"]["taskAnswer"], "42")

    def test_wrong_answer_feedback_survives_next_round(self):
        mem = Memory(task_text="query", submitted=(2, "bad"))
        p = task_payload(3, "query")
        p["errors"] = [{"errorCode": 2, "description": "需要JSON对象"}]
        mem.observe(Turn(p, Config()), Config())
        p.update(roundNo=4, errors=[])
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        self.assertIn("需要JSON对象", Intelligence(t, cfg, mem).task(ledger)[0])

    def test_timeout_death_skips_and_wrong_answers_are_not_learned(self):
        for reason in ("timeout", "death", "wrong", "skipped", "illegal", "walked_away"):
            with self.subTest(reason=reason):
                mem = Memory(task_text="query", task_point=(6, 5), task_started=1,
                             task_timeout=60, submitted=(3, "42"), successful_python="print(42)")
                p = task_payload(4)
                p["lastRoundRoleActionResults"] = {"11": True}
                if reason == "timeout":
                    p["errors"] = [{"errorCode": 1}]
                elif reason == "death":
                    p["teamOur"]["roles"] = []
                elif reason == "wrong":
                    p["errors"] = [{"errorCode": 2}]
                elif reason == "skipped":
                    p["roundNo"] = 6
                elif reason == "illegal":
                    p["lastRoundRoleActionResults"]["11"] = False
                else:
                    p["teamOur"]["roles"][0]["pos"] = {"x": 1, "y": 1}
                mem.observe(Turn(p, Config()), Config())
                self.assertFalse(mem.skills)

    def test_weak_model_format_variants(self):
        for text, answer in (("answer: 42", "42"), ("ANSWER：\n42", "42"),
                             ('{"city":"北京"}', {"city": "北京"}), ("[1,2]", [1, 2]), ("42", 42)):
            self.assertEqual(parse_task_reply(text), {"answer": answer})
        self.assertIsNone(parse_task_reply("随便猜一个"))

    def test_large_sandbox_output_keeps_traceback_tail(self):
        p = task_payload(3, "query")
        p["lastCmdResult"] = "[exitCode:1]\n" + "x" * 30000 + "\nValueError: important detail"
        mem = Memory(task_text="query", pending=("cmd", 2))
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        self.assertIn("ValueError: important detail", Intelligence(t, cfg, mem).task(ledger)[0])


if __name__ == "__main__":
    unittest.main()
