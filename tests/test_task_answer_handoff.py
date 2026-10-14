"""Successful execution must hand evidence back without implying a wrong answer."""
import json
from pathlib import Path
import tempfile
import unittest

from test_evolution_v2 import step
from test_task_completion import run_sandbox
from agent.intelligence import Memory
from agent.task_prompt import render_prompt


def context(prompt):
    return json.loads(prompt.split('上下文：\n', 1)[1])


class TaskAnswerHandoffTests(unittest.TestCase):
    def test_failure_diagnostics_reach_next_prompt_in_full(self):
        records = [{'key': i, 'detail': 'x' * 100, 'note': '原始字段'} for i in range(150)]
        diagnostics = ('输入原文:\n' + json.dumps(records, ensure_ascii=False)
                       + '\n实际结果:\n  {"observed": 8}\n预期结果:\n  {"expected": 7}\n')
        code = ('from pathlib import Path\nimport sys\n'
                'print(Path("diagnostics.txt").read_text(encoding="utf-8"), end="")\n'
                'print("validation mismatch: expected 7, observed 8", file=sys.stderr)\n')
        for raises in (False, True):
            with self.subTest(raises=raises), tempfile.TemporaryDirectory() as root:
                Path(root, 'diagnostics.txt').write_text(diagnostics, encoding='utf-8')
                program = (code + ('raise ValueError("validation mismatch")\n' if raises else '')).rstrip('\n')
                mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                             pending=('task', 1))
                _, command, _ = step(mem, 2, llmResp='PYTHON\n' + program)
                raw = run_sandbox({'executeCmd': command}, root)
                self.assertGreater(len(raw), 16000)
                self.assertLess(len(raw.encode('utf-8')), 65536)
                prompt, command, ledger = step(mem, 3, lastCmdResult=raw)
                state = context(prompt)
                self.assertEqual(state['lastAttempt']['sandbox'], raw)
                self.assertIn(diagnostics, state['lastAttempt']['sandbox'])
                self.assertEqual(state['lastAttempt']['python'], program)
                self.assertFalse(state['lastAttempt']['hasFinalAnswer'])
                self.assertEqual(mem.input_blocked, raises)
                self.assertEqual(mem.task_stats['sandboxCalls'], 1)
                self.assertFalse(command)
                self.assertFalse(ledger.commands)

    def test_supplemental_read_preserves_full_latest_python_diagnostics(self):
        raw = '[exitCode:0]\n' + 'source evidence\n' * 1400 + 'validation mismatch\n'
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     pending=('cmd', 1), running_python='print("diagnostics")')
        step(mem, 2, lastCmdResult=raw)
        mem.pending, mem.running_tool = ('cmd', 2), {'kind': 'read', 'path': '/task/data.txt', 'start': 0}
        prompt, _, _ = step(mem, 3, lastCmdResult='[exitCode:0]\nsupplemental evidence')
        state = context(prompt)
        self.assertEqual(state['lastAttempt']['sandbox'], raw)
        self.assertEqual(state['lastToolResult']['sandbox'], '[exitCode:0]\nsupplemental evidence')

    def test_first_stdout_result_prompts_handoff_then_submits_exact_answer(self):
        answer = '  第一行\n第二行\n'
        code = f'print({answer!r}, end="")'
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('task', 1))
        with tempfile.TemporaryDirectory() as root:
            _, command, _ = step(mem, 2, llmResp='PYTHON\n' + code)
            prompt, command, ledger = step(mem, 3, lastCmdResult=run_sandbox({'executeCmd': command}, root))
        state = context(prompt)
        self.assertFalse(command)
        self.assertFalse(ledger.commands)
        self.assertFalse(state['lastAttempt']['hasFinalAnswer'])
        self.assertEqual(state['lastAttempt']['sameOutputCount'], 1)
        self.assertEqual(state['lastAttempt']['python'], code)
        self.assertIn('尚未声明最终答案', state['nextAction'])
        self.assertIn('直接返回 ANSWER', state['nextAction'])
        self.assertEqual(state['localRejections'], [])
        prompt, command, ledger = step(mem, 4, llmResp='ANSWER\n' + answer)
        self.assertFalse(prompt)
        self.assertFalse(command)
        self.assertEqual(ledger.commands['11']['taskAnswer'], answer)
        self.assertEqual(mem.task_stats['sandboxCalls'], 1)
        self.assertEqual(mem.task_stats['submissions'], 1)

    def test_exploration_can_continue_and_task_result_submits_without_another_model_call(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('task', 1))
        with tempfile.TemporaryDirectory() as root:
            _, command, _ = step(mem, 2, llmResp='PYTHON\nprint("input inspected")')
            prompt, _, ledger = step(mem, 3, lastCmdResult=run_sandbox({'executeCmd': command}, root))
            self.assertFalse(ledger.commands)
            self.assertIn('若仍缺数据或验证', context(prompt)['nextAction'])
            self.assertIn('PYTHON', context(prompt)['allowedActions'])
            _, command, _ = step(mem, 4, llmResp='PYTHON\ntask_result(str(sum([2, 5])))')
            prompt, command, ledger = step(mem, 5, lastCmdResult=run_sandbox({'executeCmd': command}, root))
        self.assertFalse(prompt)
        self.assertFalse(command)
        self.assertTrue(mem.last_attempt['hasFinalAnswer'])
        self.assertEqual(ledger.commands['11']['taskAnswer'], '7')

    def test_repeated_program_limit_redirects_to_existing_output(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('task', 1))
        with tempfile.TemporaryDirectory() as root:
            for round_no in (2, 4):
                _, command, _ = step(mem, round_no, llmResp='PYTHON\nprint(7)')
                prompt, _, ledger = step(mem, round_no + 1,
                                         lastCmdResult=run_sandbox({'executeCmd': command}, root))
                self.assertFalse(ledger.commands)
                self.assertEqual(context(prompt)['localRejections'], [])
        prompt, command, ledger = step(mem, 6, llmResp='PYTHON\n# changed comment\nprint(7)')
        self.assertFalse(command)
        self.assertFalse(ledger.commands)
        self.assertEqual(mem.task_stats['sandboxCalls'], 2)
        self.assertEqual(mem.task_stats['rejections'], 1)
        self.assertIn('尚未声明最终答案', context(prompt)['nextAction'])
        self.assertIn('已有成功输出', context(prompt)['localRejections'][-1])
        self.assertIn('直接返回 ANSWER', context(prompt)['localRejections'][-1])
        _, _, ledger = step(mem, 7, llmResp='ANSWER\n7')
        self.assertEqual(ledger.commands['11']['taskAnswer'], '7')

    def test_failed_execution_after_success_does_not_prompt_answer_handoff(self):
        for raw in ('[exitCode:1]\nValueError: incomplete', '[TIMEOUT]\npartial',
                    '[JUDGER_ERROR]\nunavailable', '[exitCode:0]\npartial\n[TRUNCATED]'):
            with self.subTest(raw=raw):
                mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                             pending=('cmd', 1), running_python='print(7)')
                step(mem, 2, lastCmdResult='[exitCode:0]\n7')
                for round_no in (3, 4):
                    mem.pending, mem.running_python = ('cmd', round_no - 1), 'raise ValueError("incomplete")'
                    prompt, _, ledger = step(mem, round_no, lastCmdResult=raw)
                    self.assertFalse(ledger.commands)
                    self.assertTrue(mem.input_blocked)
                    self.assertNotIn('尚未声明最终答案', context(prompt)['nextAction'])
                    self.assertIn('执行未成功', context(prompt)['localRejections'][-1])

    def test_supplemental_read_keeps_successful_python_handoff(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     pending=('cmd', 1), running_python='print(7)')
        step(mem, 2, lastCmdResult='[exitCode:0]\n7')
        mem.pending, mem.running_tool = ('cmd', 2), {'kind': 'read', 'path': '/task/data.txt', 'start': 0}
        prompt, _, _ = step(mem, 3, lastCmdResult='[exitCode:0]\nsupplemental evidence')
        state = context(prompt)
        self.assertEqual(state['lastAttempt']['python'], 'print(7)')
        self.assertEqual(state['lastToolResult']['tool'], 'read')
        self.assertIn('尚未声明最终答案', state['nextAction'])

    def test_read_only_does_not_count_as_successful_solver_execution(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('cmd', 1),
                     running_tool={'kind': 'read', 'path': '/task/data.txt', 'start': 0})
        prompt, _, ledger = step(mem, 2, lastCmdResult='[exitCode:0]\nsource data')
        self.assertFalse(ledger.commands)
        self.assertNotIn('尚未声明最终答案', context(prompt)['nextAction'])

    def test_official_rejection_is_preserved_after_unmarked_success(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('task', 1))
        step(mem, 2, llmResp='ANSWER\n7')
        step(mem, 3, errors=[{'errorCode': 2, 'description': 'required condition missing'}])
        with tempfile.TemporaryDirectory() as root:
            _, command, _ = step(mem, 4, llmResp='PYTHON\nprint(7)')
            prompt, _, ledger = step(mem, 5, lastCmdResult=run_sandbox({'executeCmd': command}, root))
        self.assertFalse(ledger.commands)
        self.assertIn('required condition missing', context(prompt)['submissionFeedback'])
        self.assertIn('官方反馈', context(prompt)['nextAction'])
        prompt, _, ledger = step(mem, 6, llmResp='ANSWER\n7')
        self.assertFalse(ledger.commands)
        self.assertIn('该答案已判错', context(prompt)['localRejections'][-1])

    def test_repeated_explicit_rejected_answer_still_requires_repair(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('task', 1))
        step(mem, 2, llmResp='ANSWER\n7')
        step(mem, 3, errors=[{'errorCode': 2, 'description': 'required condition missing'}])
        for round_no in (4, 5):
            mem.pending, mem.running_python = ('cmd', round_no - 1), 'task_result("7")'
            prompt, _, ledger = step(mem, round_no, lastCmdResult='[exitCode:0]\nFINAL_ANSWER\n7')
            self.assertFalse(ledger.commands)
            self.assertTrue(mem.last_attempt['hasFinalAnswer'])
            self.assertNotIn('尚未声明最终答案', context(prompt)['nextAction'])
            self.assertIn('答案已被判错', context(prompt)['localRejections'][-1])

    def test_deadline_still_prioritizes_answer_only(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     pending=('cmd', 1), running_python='print(7)')
        step(mem, 2, lastCmdResult='[exitCode:0]\n7')
        state = context(render_prompt(mem, 2, []))
        self.assertEqual(state['allowedActions'], ['ANSWER'])
        self.assertIn('最后机会', state['nextAction'])


if __name__ == '__main__':
    unittest.main()
