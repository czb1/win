"""Raw per-type history: real file capture, submission lifecycle and prompt boundaries."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from test_agent import setup_case
from test_evolution import task_payload
from agent.intelligence import Memory, Intelligence
from agent.model import Turn
from agent.config import Config
from agent.task_history import completed_record, previous_tasks, source_snapshot
from agent.task_prompt import render_prompt
from agent.task_tools import task_document_code


class CompleteHistoryTests(unittest.TestCase):
    def memory(self, kind='自进化类1'):
        return Memory(task_text='原题\r\n"引号"', task_descriptor={'taskType': kind},
                      answer_python="api_key='literal'\nprint(7)\n", answer=' {"值": 7} ')

    def context(self, mem):
        return json.loads(render_prompt(mem, 8, previous_tasks(mem)).split('上下文：\n')[1])

    def test_all_records_and_exact_values_survive_old_limits(self):
        mem = self.memory()
        for i in range(12):
            mem.task_text = str(i) + '题' * 17000
            mem.task_sources = [{'path': '/same.md', 'text': '原文\r\n' * 5000}]
            mem.completed_tasks.setdefault('自进化类1', []).append(completed_record(mem))
        expected = list(mem.completed_tasks['自进化类1'])
        mem.completed_tasks['自进化类2'] = [{'question': 'OTHER_TYPE'}] * 20
        context = self.context(mem)
        self.assertEqual(context['previousTasks'], expected)
        self.assertNotIn('OTHER_TYPE', json.dumps(context))
        self.assertNotIn('referencedTasks', context)
        self.assertNotIn('previousSolutions', context)
        self.assertEqual(context['previousTasks'][0]['python'], mem.answer_python)
        self.assertEqual(context['previousTasks'][0]['answer'], mem.answer)
        mem.task_descriptor = {'taskType': '自进化类2'}
        self.assertEqual(previous_tasks(mem), mem.completed_tasks['自进化类2'])

    def test_snapshot_is_not_mutated_by_next_task(self):
        mem = self.memory()
        mem.task_sources = [{'path': '/task.md', 'text': 'original'}]
        record = completed_record(mem)
        mem.task_sources[0]['text'] = 'changed'
        mem.answer = 'other'
        self.assertEqual(record['documents'][0]['text'], 'original')
        self.assertEqual(record['answer'], ' {"值": 7} ')

    def test_missing_type_never_guesses_or_mixes_history(self):
        mem = self.memory(None)
        mem.completed_tasks['自进化类1'] = [{'question': 'old'}]
        self.assertIsNone(completed_record(mem))
        self.assertEqual(previous_tasks(mem), [])

    def test_same_path_and_title_are_separate_between_types(self):
        mem = self.memory()
        mem.current_task_archive()['/same.md'] = {'title': 'S-1', 'text': 'one'}
        mem.task_descriptor['taskType'] = '自进化类2'
        mem.current_task_archive()['/same.md'] = {'title': 'S-1', 'text': 'two'}
        self.assertEqual(mem.task_archive['自进化类1']['/same.md']['text'], 'one')
        self.assertEqual(mem.current_task_archive()['/same.md']['text'], 'two')

    def test_full_file_capture_preserves_newlines_and_marker_literals(self):
        text = '# 题面\r\n' + ('中文 "quoted"\n' * 2200) + '\nTASK_SOURCE literal\nNEXT_READ literal\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'task.md')
            path.write_bytes(text.encode())
            for name in (str(path), 'task.md'):
                code = task_document_code(name, base=directory)
                run = subprocess.run([sys.executable, '-c', code], cwd=directory, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                raw = '[exitCode:0]\n' + run.stdout
                preview, snapshot = source_snapshot(raw)
                self.assertEqual(snapshot, {'path': str(path), 'text': text})
                self.assertIn('NEXT_READ', preview)
                mem = Memory(task_text='query', task_started=1, task_descriptor={'taskType': '自进化类1'},
                             pending=('cmd', 1), running_tool={'kind': 'discover', 'path': name})
                mem.observe(Turn(task_payload(2, 'query') | {'lastCmdResult': raw}, Config()), Config())
                self.assertEqual(mem.task_sources, [snapshot])
                self.assertNotIn('TASK_SOURCE ', mem.documents[0]['output'])

    def test_truncated_snapshot_not_accepted_as_full_source(self):
        raw = '[exitCode:0]\nDOCUMENT /task.md OFFSET 0\npage\nTASK_SOURCE {"path":\n[TRUNCATED]'
        cleaned, snapshot = source_snapshot(raw)
        self.assertIsNone(snapshot)
        self.assertEqual(cleaned, raw)

    def test_direct_answer_archived_once_only_after_completion(self):
        mem = self.memory()
        mem.task_text = 'query'
        mem.task_point = (6, 5)
        mem.task_started = 1
        mem.answer_python = ''
        turn, cfg, _, ledger = setup_case(task_payload(3, 'query'))
        Intelligence(turn, cfg, mem).submit_answer(ledger, turn.pioneer)
        self.assertFalse(mem.completed_tasks)
        payload = task_payload(4)
        payload['lastRoundRoleActionResults'] = {'11': True}
        mem.observe(Turn(payload, cfg), cfg)
        mem.observe(Turn(payload, cfg), cfg)
        record, = mem.completed_tasks['自进化类1']
        self.assertEqual(record['answer'], ' {"值": 7} ')
        self.assertEqual(record['python'], '')
        self.assertEqual(Memory().completed_tasks, {})

    def test_rejected_submission_not_archived(self):
        mem = self.memory()
        mem.task_point, mem.task_started = (6, 5), 1
        turn, cfg, _, ledger = setup_case(task_payload(3, mem.task_text))
        Intelligence(turn, cfg, mem).submit_answer(ledger, turn.pioneer)
        payload = task_payload(4, mem.task_text)
        payload['errors'] = [{'errorCode': 2}]
        mem.observe(Turn(payload, cfg), cfg)
        self.assertFalse(mem.completed_tasks)
