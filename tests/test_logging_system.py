"""Focused log attribution and query checks; no game simulations."""
from contextlib import contextmanager
import io
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_agent import payload, ROOT
from test_evolution import task_payload
from agent.brain import Agent
from agent.config import Config
from agent.logging_system import ContextFilter, JsonFormatter, turn_context
from agent.model import Turn


@contextmanager
def capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
        handler.close()


def records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class LoggingSystemTests(unittest.TestCase):
    def test_boundaries_use_configured_round_origin_and_context_is_reset(self):
        with capture() as stream:
            for origin in (0, 1):
                for offset in (0, 69, 70, 129, 130):
                    turn = Turn(payload(origin + offset), Config(round_origin=origin))
                    with turn_context(turn):
                        logging.info("boundary")
            logging.info("system")
        result = records(stream)
        expected = [(1, "day", 1), (1, "day", 70), (1, "night", 1),
                    (1, "night", 60), (2, "day", 1)] * 2
        self.assertEqual([(r['day'], r['phase'], r['phase_round']) for r in result[:-1]], expected)
        self.assertIsNone(result[-1]['day'])

    def test_task_start_end_and_new_task_keep_correct_identity(self):
        agent = Agent(Config(layout_mode="explicit"))
        with capture() as stream:
            agent.decide(task_payload(69, "first"))
            agent.decide(task_payload(70, "first"))
            agent.decide(task_payload(71, ""))
            agent.decide(task_payload(72, "second"))
        result = records(stream)
        starts = [r for r in result if r['event'] == 'task_started']
        ended = next(r for r in result if r['event'] == 'task_ended')
        outcome = next(r for r in result if r['event'] == 'task_outcome')
        self.assertEqual(ended['task_id'], starts[0]['task_id'])
        self.assertEqual(outcome['task_id'], starts[0]['task_id'])
        self.assertNotEqual(starts[0]['task_id'], starts[1]['task_id'])
        self.assertEqual(starts[0]['task_type'], '自进化类1')
        self.assertTrue(any(r['phase'] == 'night' and r['task_id'] == starts[0]['task_id'] for r in result))

    def test_treasure_chain_has_separate_category_and_session(self):
        agent = Agent(Config(layout_mode="explicit"))
        with capture() as stream:
            first = task_payload(1, "contains [TREASURE_TRACE] but is evolution")
            first["worldNews"] = {"folkLegends": "民间传闻：寻找宝藏线索"}
            agent.decide(first)
            other = task_payload(1, "another team")
            other['teamOur']['teamId'] = 'another'
            agent.decide(other)
            agent.decide(task_payload(0, "new game"))
        result = records(stream)
        questions = [r for r in result if r['event'] == 'task_question']
        self.assertEqual({r['category'] for r in questions}, {'evolution'})
        self.assertEqual(len({r['session'] for r in questions}), 3)
        traces = [r for r in result if r['category'] == 'long_context']
        self.assertTrue(traces)
        self.assertTrue(all(r['task_id'].endswith('/long-context') for r in traces))

    def test_exception_json_is_one_line_and_does_not_leak_context(self):
        with capture() as stream:
            try:
                with turn_context(Turn(payload(70), Config())):
                    raise ValueError('line1\nline2')
            except ValueError:
                logging.exception('outside')
        result = records(stream)
        self.assertEqual(len(result), 1)
        self.assertIn('ValueError', result[0]['exception'])
        self.assertIsNone(result[0]['day'])

    def test_rejected_accept_is_attributed_to_original_attempt(self):
        agent = Agent(Config(layout_mode="explicit"))
        with capture() as stream:
            agent.decide(task_payload(1))
            agent.decide(task_payload(2))
        result = records(stream)
        accepted = [r for r in result if r['event'] == 'task_accept']
        rejected = next(r for r in result if r['event'] == 'task_accept_rejected')
        self.assertEqual(rejected['task_id'], accepted[0]['task_id'])
        self.assertTrue(accepted[0]['task_id'].endswith('/r1'))
        if len(accepted) > 1:
            self.assertNotEqual(accepted[0]['task_id'], accepted[1]['task_id'])

    def test_replay_persists_searchable_logs_without_changing_json_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(ROOT / 'tools/replay.py'),
                                      str(ROOT / 'examples/request.json'), '--log-level', 'INFO',
                                      '--log-dir', directory, '--plaintext-logs'], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn('roleCommandMap', json.loads(process.stdout))
            text = Path(directory, 'game.log').read_text(encoding='utf-8')
            self.assertIn('第1天黑夜', text)
            data = [json.loads(line) for line in Path(directory, 'events.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(data)
            self.assertTrue(all(r['day'] == 1 and r['phase'] == 'night' for r in data))

    def test_cli_combines_filters_and_lists_tasks_skipping_damaged_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'events.jsonl')
            data = [dict(day=2, phase='night', category='evolution', task_id='abc/r140',
                         team='A', session='abc', level='INFO', message='task_request'),
                    dict(day=2, phase='day', category='long_context', task_id='abc/long-context',
                         team='A', session='abc', level='INFO', message='news')]
            path.write_text('\n'.join(json.dumps(r) for r in data) + '\nbroken\n', encoding='utf-8')
            command = [sys.executable, str(ROOT / 'tools/query_logs.py'), str(path)]
            process = subprocess.run(command + ['--day', '2', '--phase', 'night', '--category', 'evolution',
                                                '--task-id', 'abc/r140', '--json'], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout), data[0])
            self.assertIn('跳过无效日志', process.stderr)
            listing = subprocess.run(command + ['--list'], capture_output=True, text=True)
            self.assertEqual(listing.returncode, 0)
            self.assertIn('abc/r140\t1', listing.stdout)
            self.assertIn('abc/long-context\t1', listing.stdout)

