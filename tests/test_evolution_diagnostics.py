"""Bounded evidence and parser-contract regressions using unrelated task values."""
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

from test_evolution_v2 import step, v2_result
from test_log_evidence import execute
import test_weak_model_evolution as transform_fixture
from agent.intelligence import Memory, compact_attempt, sandbox_failure_fingerprint
from agent.task_evidence import parsing_rules
from agent.task_skills import parser_method


class EvolutionDiagnosticsTests(unittest.TestCase):
    def log_memory(self, directory, code, raw):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     pending=('cmd', 1), running_python=code,
                     contract={'input_kind': 'logs', 'example': {'count': 0}},
                     inputs={'directory': directory, 'files': [{'path': 'sensor.log'}]})
        prompt, _, ledger = step(mem, 2, lastCmdResult=raw)
        return mem, prompt, ledger

    def test_normal_and_fault_are_distinct_and_submission_is_unblocked(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('1 OK\n2 BAD\n3 OK\n')
            code = """def parse_sensor_line(line):
    timestamp, signal = line.split()
    return {'system':'sensor', 'timestamp':int(timestamp), 'is_fault':signal == 'BAD'}
with open('sensor.log') as source:
    rows = [parse_sensor_line(line) for line in source]
task_result({'count':sum(row['is_fault'] for row in rows)})
"""
            raw, result, report = execute(code, directory)
            self.assertEqual(result[0], 'ok')
            counts = report['parse_systems']['sensor']
            self.assertEqual((counts['total'], counts['matched'], counts['normal'], counts['failures']), (3, 3, 2, 1))
            mem, _, ledger = self.log_memory(directory, code, raw)
            self.assertFalse(mem.input_blocked)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'count': 1})
            self.assertIsNotNone(mem.parser_candidate)

    def test_timestamp_none_and_false_do_not_silently_become_normal(self):
        for expression in ('int(line)', 'None', 'False'):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as directory:
                Path(directory, 'sensor.log').write_text('1\n2\n')
                code = f"def parse_sensor_line(line):\n return {expression}\nwith open('sensor.log') as f:\n rows=[parse_sensor_line(line) for line in f]\ntask_result({{'count':1}})"
                raw, _, report = execute(code, directory)
                self.assertEqual(report['parse_lines']['matched'], 0)
                mem, prompt, ledger = self.log_memory(directory, code, raw)
                self.assertFalse(ledger.commands)
                self.assertTrue(mem.input_blocked)
                self.assertIn('只修', prompt)
                self.assertIn('is_fault', prompt)
                # Comment-only retries are refused before another sandbox command.
                mem.pending = ('task', 2)
                _, command, _ = step(mem, 3, llmResp='PYTHON\n# cosmetic\n'+code)
                self.assertFalse(command)
                self.assertIn('同一AST', mem.history[-1]['error'])

    def test_unknown_line_and_swallowed_time_error_remain_blocked(self):
        for line in ('unknown', 'bad BAD'):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as directory:
                Path(directory, 'sensor.log').write_text(line+'\n')
                code = """def parse_sensor_line(line):
    fields = line.split()
    if len(fields) != 2:
        return None
    try:
        return {'system':'sensor', 'timestamp':int(fields[0]), 'is_fault':True}
    except ValueError:
        return None
with open('sensor.log') as f:
    rows=[parse_sensor_line(line) for line in f]
task_result({'count':0})
"""
                raw, _, _ = execute(code, directory)
                mem, _, ledger = self.log_memory(directory, code, raw)
                self.assertTrue(mem.input_blocked)
                self.assertFalse(ledger.commands)

    def test_rules_keep_adjacent_status_definition_without_matching_keywords(self):
        text = '## 故障信号定义\n- sensor: ALERT\n- proxy: HTTP 429\n- worker: EPIPE\n\n2028-01-02 private sample\n读取 /tmp/previous.log'
        rules = parsing_rules([{'output': text}])
        self.assertIn('HTTP 429', rules)
        self.assertIn('EPIPE', rules)
        self.assertNotIn('private', rules)
        self.assertNotIn('/tmp/', rules)

    def test_parser_extraction_reports_reason_and_keeps_parameterized_method(self):
        diagnostics = {}
        self.assertIsNone(parser_method("def parse_sensor_line(line):\n return hidden[line]", diagnostics=diagnostics))
        self.assertIn('global', diagnostics['parser_unavailable'])
        diagnostics = {}
        code = "from datetime import datetime\ndef parse_sensor_line(line, year):\n return {'system':'sensor','timestamp':datetime.strptime(str(year)+' '+line,'%Y %m-%d'),'is_fault':False}"
        self.assertTrue(parser_method(code, diagnostics=diagnostics))
        self.assertFalse(diagnostics)

    def test_discovery_compaction_handles_duration_header_without_losing_failure(self):
        attempt = {'status':'ok','python':'large discovery program',
                   'sandbox':'[exitCode:0]\n[durationMs:123]\nDOCUMENT /tmp/task'}
        self.assertNotIn('python', compact_attempt(attempt))
        self.assertNotIn('sandbox', compact_attempt(attempt))
        failed = dict(attempt, status='input_failed')
        self.assertIn('python', compact_attempt(failed))

    def test_structured_failure_fingerprint_ignores_duration_but_keeps_error_kind(self):
        report = {'logs': {'errors': [{'kind':'parser_contract','detail':'parse_sensor_line'}]}}
        first = sandbox_failure_fingerprint('[exitCode:0]\n[durationMs:10]', report)
        self.assertEqual(first, sandbox_failure_fingerprint('[exitCode:0]\n[durationMs:99]', report))
        report['logs']['errors'][0]['kind'] = 'unread_inputs'
        self.assertNotEqual(first, sandbox_failure_fingerprint('', report))

    def test_current_definitions_supersede_saved_parser(self):
        from agent.task_skills import learned_method
        contract = {'input_kind':'logs','family':'sensor/task#.md','example':{'count':0}}
        code = "def parse_sensor_line(line):\n return ('sensor',line,'BAD' in line)"
        skill = learned_method((6,5), contract, parser=code, rules='故障定义：BAD', parser_evidence={'parse_sensor_line':{'record_contract':'structured-v1'}})
        mem = Memory(task_point=(6,5), contract=contract, skills=[skill],
                     documents=[{'output':'复用解析。\n\n故障定义：WARN'}])
        self.assertIsNone(mem.reusable_parser())
        mem.documents = [{'output':'复用解析。\n\n故障定义：BAD'}]
        self.assertEqual(mem.reusable_parser(), skill)

    def test_parameterized_saved_parser_binds_current_year(self):
        parser = "from datetime import datetime\ndef parse_sensor_line(line, year):\n return {'system':'sensor','timestamp':datetime(year,1,int(line)), 'is_fault':False}"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('2\n')
            code = "rows=task_parse('sensor','sensor.log',year=2031)\ntask_result({'year':rows[0]['timestamp'].year})"
            _, result, report = execute(code, directory, parser_method(parser))
            self.assertEqual(result[0], 'ok')
            self.assertEqual(json.loads(result[2]), {'year':2031})
            self.assertEqual(report['parse_systems']['sensor']['normal'], 1)

    def test_transform_exposes_extra_record_without_assuming_field_names(self):
        helper = transform_fixture.WeakModelTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, *_ = helper.fixture(root)
            records = [{'ref':'a','weight':17}, {'ref':'b','weight':18}, {'ref':'c','weight':25}]
            (root/'examples.json').write_text(json.dumps({'cases':[{'input':records,'expected':{'rows':records[1:]}}]}))
            status, body, answer = helper.execute('def transform(records):\n return {"rows":records}', config)
            self.assertNotEqual(status, 'ok')
            self.assertIsNone(answer)
            detail = json.loads(body.partition('TRANSFORM_DIAGNOSTIC ')[2])
            evidence = detail['failures'][0]['record_evidence']
            self.assertEqual(evidence['extra_records'], [records[0]])
            self.assertEqual(evidence['expected_kept']['numeric_fields']['weight']['min'], 18)
            self.assertFalse((root/'checked').exists())

    def test_checker_failure_has_current_input_boundaries_and_fragment(self):
        helper = transform_fixture.WeakModelTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, *_ = helper.fixture(root)
            records = [{'ref':'b','weight':18}, {'ref':'c','weight':25}]
            (root/'examples.json').write_text(json.dumps([{'input':records,'expected':{'sum':43}}]))
            (root/'data.json').write_text(json.dumps([{'ref':'fresh','weight':-3}]))
            (root/'check_custom').write_text(f'#!{sys.executable}\nprint("[FAIL] $/sum expected 0 actual -3")\nraise SystemExit(1)\n')
            status, body, answer = helper.execute('def transform(records):\n return {"sum":sum(r["weight"] for r in records)}', config)
            self.assertNotEqual(status, 'ok')
            self.assertIsNone(answer)
            detail = json.loads(body.partition('TRANSFORM_DIAGNOSTIC ')[2])
            self.assertEqual(detail['samples_passed'], 1)
            self.assertEqual(detail['output_fragment']['actual'], -3)
            self.assertEqual(detail['input_summary']['numeric_fields']['weight']['min'], -3)
            self.assertIn('unavailable', detail['sample_evidence'][0]['alignment'])
            self.assertEqual(json.loads((root/'report.json').read_text()), {'sum':-3})

    def test_ambiguous_record_identity_is_explicitly_unavailable(self):
        helper = transform_fixture.WeakModelTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, *_ = helper.fixture(root)
            records = [{'x':1,'y':2}, {'x':2,'y':1}]
            (root/'examples.json').write_text(json.dumps([{'input':records,'expected':[{'x':1,'y':1}]}]))
            _, body, _ = helper.execute('def transform(records):\n return [{"x":2,"y":2}]', config)
            detail = json.loads(body.partition('TRANSFORM_DIAGNOSTIC ')[2])
            self.assertIn('unavailable', detail['failures'][0]['record_evidence']['alignment'])

if __name__ == '__main__':
    unittest.main()
