"""Observed task files must reach the next prompt without model-written diagnostics."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from test_evolution_v2 import step
from test_task_completion import run_sandbox
from agent.intelligence import Memory, sandbox_result
from agent.task_runtime import runtime_code, runtime_result
from agent.task_tools import file_code


def execute(code, root):
    return run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(runtime_code(code, str(root)))}, root)


def evidence(raw):
    return runtime_result(raw)[1]['diagnostics']


def record(detail, name, kind):
    return next(item for item in detail['files']
                if item['source_path'] and Path(item['source_path']).name == name and item['kind'] == kind)


class TaskAutoDiagnosticsTests(unittest.TestCase):
    def test_normal_exit_without_answer_preserves_input_output_and_next_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            original = '[{"id":17,"amount":155,"note":"完整原始字段","active":true}]\r\n'
            Path(root, 'input.json').write_bytes(original.encode())
            program = ('from pathlib import Path\nimport json\n'
                       'rows=json.loads(Path("input.json").read_text())\n'
                       'Path("result.json").write_text("[]\\n")\n'
                       'print("official validation: expected 155, actual 0")')
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                         pending=('task', 1), documents=[{'kind': 'read', 'path': str(Path(root, 'task.md')),
                                                       'resolved_path': str(Path(root, 'task.md')), 'output': 'current task'}])
            _, command, _ = step(mem, 2, llmResp='PYTHON\n' + program)
            raw = run_sandbox({'executeCmd': command}, root)
            detail = evidence(raw)
            self.assertEqual(detail['reason'], 'no_final_answer')
            source = record(detail, 'input.json', 'before_read')
            self.assertEqual(source['content'], original)
            self.assertEqual(Path(source['snapshot_path']).read_bytes(), original.encode())
            self.assertTrue(source['captured_complete'])
            self.assertTrue(source['shown_complete'])
            self.assertEqual(record(detail, 'result.json', 'after_write')['content'], '[]\n')
            prompt, command, ledger = step(mem, 3, lastCmdResult=raw)
            context = json.loads(prompt.split('上下文：\n')[1])
            self.assertEqual(context['lastAttempt']['sandbox'], raw)
            self.assertIn('完整原始字段', context['lastAttempt']['sandbox'])
            self.assertFalse(mem.input_blocked)
            self.assertFalse(context['lastAttempt']['hasFinalAnswer'])
            self.assertIn('尚未声明最终答案', context['nextAction'])
            self.assertFalse(command)
            self.assertFalse(ledger.commands)
            self.assertEqual(mem.task_stats['sandboxCalls'], 1)

    def test_overwritten_input_keeps_before_and_after_snapshots(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'data').write_bytes(b'original\r\n')
            raw = execute('with open("data") as f: data=f.read()\n'
                          'with open("data", "w") as f: f.write("replacement")', root)
            detail = evidence(raw)
            self.assertEqual(record(detail, 'data', 'before_read')['content'], 'original\r\n')
            self.assertEqual(record(detail, 'data', 'after_write')['content'], 'replacement')

    def test_write_without_read_keeps_original_and_os_open_is_observed(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'data').write_bytes(b'original')
            detail = evidence(execute('import os\nf=os.open("data", os.O_WRONLY|os.O_TRUNC)\n'
                                      'os.write(f,b"new")\nos.close(f)', root))
            self.assertEqual(record(detail, 'data', 'before_write')['content'], 'original')
            self.assertEqual(record(detail, 'data', 'after_write')['content'], 'new')

    def test_exception_preserves_exit_stderr_and_file_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'source').write_text('raw input')
            raw = execute('import sys\nfrom pathlib import Path\nPath("source").read_text()\n'
                          'Path("actual").write_text("actual result")\n'
                          'print("feedback on stderr", file=sys.stderr)\nraise ValueError("task failure")', root)
            self.assertTrue(raw.startswith('[exitCode:1]\n'))
            self.assertEqual(evidence(raw)['reason'], 'nonzero_exit')
            output = next(r for r in evidence(raw)['files'] if r['kind'] == 'execution_output')
            text = Path(output['snapshot_path']).read_text()
            self.assertIn('feedback on stderr', text)
            self.assertIn('ValueError: task failure', text)
            self.assertEqual(record(evidence(raw), 'actual', 'after_write')['content'], 'actual result')

    def test_child_process_file_changes_are_collected_without_rerunning_child(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'existing').write_text('before')
            child = ('from pathlib import Path\nPath("new").mkdir()\n'
                     'Path("new/output").write_text("child output")\n'
                     'Path("existing").write_text("after")\n'
                     'Path("runs").write_text("one execution")')
            raw = execute(f'import subprocess, sys\nsubprocess.run([sys.executable,"-c",{child!r}],check=True)', root)
            detail = evidence(raw)
            self.assertEqual(record(detail, 'output', 'directory_change')['content'], 'child output')
            self.assertEqual(record(detail, 'existing', 'directory_change')['content'], 'after')
            self.assertEqual(Path(root, 'runs').read_text(), 'one execution')

    def test_large_snapshot_can_be_read_exactly_and_chinese_output_stays_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            original = ('中文记录\r\n' * 9000).encode()
            Path(root, 'source').write_bytes(original)
            raw = execute('from pathlib import Path\nPath("source").read_text()\nprint("中文" * 40000)', root)
            self.assertLess(len(raw.encode()), 65536)
            self.assertIn('[TRUNCATED]', raw)
            detail = evidence(raw)
            source = record(detail, 'source', 'before_read')
            self.assertTrue(source['captured_complete'])
            self.assertFalse(source['shown_complete'])
            self.assertNotIn('content', source)
            self.assertEqual(Path(source['snapshot_path']).read_bytes(), original)
            args = shlex.split(source['read_command'])
            page = run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(file_code('read', args[1], int(args[2])))}, root)
            self.assertIn('中文记录', page)
            self.assertIn('NEXT_READ', page)
            output = next(r for r in detail['files'] if r['kind'] == 'execution_output')
            self.assertEqual(Path(output['snapshot_path']).read_text(), '中文' * 40000 + '\n')

    def test_collection_limit_marks_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'large').write_bytes(b'a' * 1049000)
            detail = evidence(execute('from pathlib import Path\nPath("large").read_bytes()', root))
            source = record(detail, 'large', 'before_read')
            self.assertFalse(source['captured_complete'])
            self.assertFalse(source['shown_complete'])
            self.assertEqual(source['source_bytes'], 1049000)
            self.assertEqual(source['snapshot_bytes'], 1048576)
            self.assertIn('partial', source['omitted'])

    def test_success_keeps_answer_exact_and_removes_temporary_snapshots(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'input').write_text('data')
            answer = '  第一行\r\n"第二行"\n'
            raw = execute('from pathlib import Path\nPath("input").read_text()\n'
                          f'task_result({answer!r})', root)
            cleaned, report = runtime_result(raw)
            # The fixture's text transport normalizes CRLF; inspect bytes as well.
            process = subprocess.run([sys.executable, '-c', runtime_code(f'task_result({answer!r})', root)],
                                     cwd=root, capture_output=True)
            self.assertEqual(process.stdout.partition(b'FINAL_ANSWER\n')[2], answer.encode())
            self.assertEqual(sandbox_result(cleaned)[2], answer.replace('\r\n', '\n'))
            self.assertNotIn('diagnostics', report)
            self.assertEqual(list(Path(root).glob('.task-evidence-*')), [])

    def test_outside_directory_symlink_and_fifo_are_not_read_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            Path(outside, 'private').write_text('outside evidence')
            Path(root, 'link').symlink_to(Path(outside, 'private'))
            program = ('from pathlib import Path\nimport os\n'
                       'Path("link").read_text()\nos.mkfifo("fifo")\n'
                       'f=os.open("fifo", os.O_RDONLY|os.O_NONBLOCK)\nos.close(f)')
            detail = evidence(execute(program, root))
            self.assertFalse(any(r['source_path'] and 'private' in r['source_path'] for r in detail['files']))
            source = record(detail, 'fifo', 'before_read')
            self.assertIn('not a regular file', source['omitted'])

    def test_snapshot_failure_does_not_change_solver_result(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'source').write_text('still readable')
            raw = execute('import tempfile\nfrom pathlib import Path\n'
                          'def fail(*args, **kwargs): raise OSError("diagnostic disk failure")\n'
                          'tempfile.mkdtemp=fail\nprint(Path("source").read_text())', root)
            self.assertTrue(raw.startswith('[exitCode:0]\n'))
            self.assertIn('still readable', raw)
            self.assertIn('diagnostic disk failure', json.dumps(evidence(raw)))

    def test_many_files_preserve_manifest_and_flag_limits(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(80):
                Path(root, str(i)).write_text('raw')
            detail = evidence(execute('from pathlib import Path\n'
                                      'for i in range(80): Path(str(i)).read_text()', root))
            self.assertTrue(any('limit' in note for note in detail['notes']))
            manifest = json.loads(Path(detail['manifest_path']).read_text())
            self.assertEqual(manifest['reason'], 'no_final_answer')
            self.assertTrue(manifest['files'])

    def test_cleanup_failure_does_not_block_a_successful_answer(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'input').write_text('data')
            raw = execute('import shutil\nfrom pathlib import Path\nPath("input").read_text()\n'
                          'def fail(*args, **kwargs): raise RuntimeError("cleanup failure")\n'
                          'shutil.rmtree=fail\ntask_result("verified answer")', root)
            self.assertEqual(sandbox_result(runtime_result(raw)[0])[2], 'verified answer')
