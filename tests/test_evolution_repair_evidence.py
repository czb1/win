"""Counterexamples for generic repair evidence, not competition answers."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from test_log_evidence import execute
from test_evolution_v2 import step
import test_weak_model_evolution as transform_fixture
from agent.intelligence import Memory
from agent.task_log_repair import repair_context, merge_repair, FullRepairRequired
from agent.task_transform_feedback import observe, prompt


class RepairEvidenceTests(unittest.TestCase):
    def test_line_evidence_preserves_spaces_and_classification_without_zero_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('1  link-down\n2  okay\n' * 30)
            code = """def parse_sensor_line(line):
    stamp, signal = line.split()
    return {'system':'sensor','timestamp':int(stamp),'signal':signal,'is_fault':False}
with open('sensor.log') as source:
    rows = [parse_sensor_line(line) for line in source]
task_result({'count':sum(row['is_fault'] for row in rows)})
"""
            raw, result, report = execute(code, directory)
            self.assertEqual(result[0], 'ok', raw)
            evidence = report['logs']['parsers']['parse_sensor_line']['samples']['normal']
            self.assertEqual(len(evidence), 2)
            self.assertEqual(evidence[0]['line'], '1  link-down\n')
            self.assertEqual(evidence[0]['parsed']['signal'], 'link-down')
            self.assertFalse(evidence[0]['parsed']['is_fault'])
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                         pending=('cmd', 1), running_python=code,
                         contract={'input_kind':'logs','example':{'count':0}},
                         inputs={'directory':directory,'files':[{'path':'sensor.log'}]})
            _, _, ledger = step(mem, 2, lastCmdResult=raw)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'count':0})

    def test_related_parser_can_be_fixed_and_signature_requests_full_mode(self):
        code = """def parse_worker_line(line):
    return {'system':'worker','timestamp':1,'is_fault':False}
def parse_line(line):
    return parse_worker_line(line)
rows = [parse_line('link-down')]
task_result({'count':sum(row['is_fault'] for row in rows)})
"""
        context = repair_context(code, {'logs':{'parsers':{'parse_line':{'unmatched_count':1}}}})
        self.assertIn('parse_worker_line', context['allowed_functions'])
        patch = "def parse_worker_line(line):\n return {'system':'worker','timestamp':1,'is_fault':line == 'link-down'}"
        merged = merge_repair(code, patch, context)
        with tempfile.TemporaryDirectory() as directory:
            _, result, _ = execute(merged, directory)
            self.assertEqual(json.loads(result[2]), {'count':1})
        with self.assertRaises(FullRepairRequired):
            merge_repair(code, patch.replace('(line)', '(line, extra)'), context)
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     pending=('task',2), repair_python=code,
                     log_diagnostics={'logs':{'parsers':{'parse_line':{'unmatched_count':1}}}},
                     contract={'input_kind':'logs','example':{'count':0}})
        step(mem,3,llmResp='PYTHON\n'+patch.replace('(line)', '(line, extra)'))
        self.assertEqual(mem.repair_mode,'full')

    def checker_example(self, records, message):
        helper = transform_fixture.WeakModelTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, *_ = helper.fixture(root)
            output = [{'ref':r['ref']} for r in records]
            (root/'examples.json').write_text(json.dumps([{'input':records,'expected':output}]))
            (root/'data.json').write_text(json.dumps(records))
            (root/'check_custom').write_text(f'#!{sys.executable}\nprint({message!r})\n')
            status, body, answer = helper.execute("def transform(records):\n return [{'ref':r['ref']} for r in records]", config)
            self.assertIsNone(answer)
            return json.loads(body.partition('TRANSFORM_DIAGNOSTIC ')[2])

    def test_checker_joins_arbitrary_unique_field_and_original_positions(self):
        records = [{'ref':'omega','rank':7,'weight':32}, {'ref':'alpha','rank':7,'weight':91}]
        detail = self.checker_example(records, '[FAIL] $[0]/ref 值不一致：期望 alpha，实际 omega')
        joined = detail['record_context']
        self.assertEqual(joined['identity_field'],'ref')
        self.assertEqual(joined['expected'], {'input_index':1,'record':records[1]})
        self.assertEqual(joined['actual'], {'input_index':0,'record':records[0]})
        self.assertEqual(len(joined['neighbor_inputs']),2)

    def test_checker_does_not_guess_duplicate_or_missing_reference(self):
        for records, message in [
            ([{'ref':'x'},{'ref':'x'}], '[FAIL] $[0]/ref 期望 x，实际 x'),
            ([{'ref':'x'},{'ref':'y'}], '[FAIL] $[0]/ref 期望 unknown，实际 x'),
            ([{'ref':'x'},{'ref':'y'}], '[FAIL] unexpected result')]:
            with self.subTest(message=message):
                detail = self.checker_example(records,message)
                self.assertIn('unavailable',detail['record_context']['alignment'])

    def test_repeated_checker_failure_changes_guidance_but_does_not_block(self):
        mem = Memory()
        attempt = {'python':"def transform(records):\n return sorted(records, key=lambda r:r['rank'])",
                   'diagnosis':{'stage':'checker','checker_feedback':'same mismatch','record_context':{'alignment':'unavailable'}}}
        observe(mem,attempt)
        self.assertFalse(mem.transform_progress['same_checker_failure'])
        observe(mem,attempt)
        mem.transform_attempt = attempt
        text = prompt(mem,8)
        self.assertIn('同一checker反例仍未解决',text)
        self.assertIn('currentExpressions',text)
        self.assertFalse(mem.failed_python)
        observe(mem,{'diagnosis':{'stage':'checker','checker_feedback':'different mismatch'}})
        self.assertFalse(mem.transform_progress['same_checker_failure'])
