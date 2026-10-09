"""Parser repairs preserve evidence, whitespace and unrelated computation."""
import ast
import json
from pathlib import Path
import tempfile
import unittest

from test_log_evidence import execute
from test_evolution_v2 import step
from agent.intelligence import Memory
from agent.task_log_repair import repair_context, merge_repair, validate_literal_regex


BAD = r'''import re
def parse_line(line, system):
    match = re.match(r'(\d+) (\w+) module=(\S+)', line)
    if not match:
        return None
    return {'system':system, 'timestamp':int(match[1]), 'is_fault':match[2]=='BAD', 'module':match[3]}
with open('sensor.log') as source:
    rows = [parse_line(line, 'sensor') for line in source]
task_result({'count':sum(row['is_fault'] for row in rows if row is not None)})
'''
GOOD_FUNCTION = r'''def parse_line(line, system):
    match = re.match(r'(\d+)\s+(\w+)\s+module=(\S+)', line)
    if not match:
        return None
    return {'system':system, 'timestamp':int(match[1]), 'is_fault':match[2]=='BAD', 'module':match[3]}
'''


class LogParserRepairTests(unittest.TestCase):
    def test_legacy_parser_is_reference_only_and_dependencies_are_rechecked(self):
        from agent.task_skills import learned_method
        contract = {'input_kind':'logs', 'family':'sample/task#.md', 'example':{'count':0}}
        parser = "def parse_sample_log(path):\n return 1"
        skill = learned_method((6,5), contract, parser=parser)
        mem = Memory(task_point=(6,5), contract=contract, skills=[skill],
                     documents=[{'output':'与上一题完全相同的日志格式'}])
        self.assertIsNone(mem.reusable_parser())
        skill['parser_contract'] = 'structured-v1'
        skill['parser'] = "def parse_sample_log(path):\n return missing_global[path]"
        self.assertIsNone(mem.reusable_parser())
        self.assertFalse(skill['disabled'])

    def test_file_parser_scalar_and_swallowed_name_error_are_attributed(self):
        from agent.task_skills import parser_method
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sample.log').write_text('bad\nnormal\n')
            code = """def parse_sample_log(path):
    count = 0
    try:
        with open(path) as source:
            for line in source:
                count += 1
                missing_counter += 1
    except Exception:
        pass
    return count
rows = parse_sample_log('sample.log')
list(rows)
"""
            raw, result, report = execute(code, directory)
            self.assertNotEqual(result[0], 'ok')
            entry = report['logs']['parsers']['parse_sample_log']
            self.assertEqual(entry['invalid_return'], 'int')
            self.assertIn('UnboundLocalError', entry['error'])
            self.assertEqual(report['logs']['files'][str(Path(directory,'sample.log'))]['read_lines'], 1)
            self.assertIsNone(parser_method(code, report['logs']['parsers']))
            from agent.intelligence import compact_attempt
            compact = compact_attempt({'status':'input_failed','sandbox':raw,'http':report})
            self.assertIn('TypeError', compact['runtimeError'])
            self.assertEqual(repair_context(code, {'logs':report['logs']})['targets'], ['parse_sample_log'])

    def test_failed_reused_version_allows_local_repair_without_changing_aggregate(self):
        from agent.task_skills import learned_method, parser_method
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('1 BAD module=alpha\n2 OK  module=alpha\n')
            old = parser_method(BAD)
            contract = {'input_kind':'logs','family':'sample/task#.md','example':{'count':0}}
            skill = learned_method((6,5), contract, parser=old,
                                   parser_evidence={'parse_line':{'record_contract':'structured-v1'}})
            raw, _, _ = execute(BAD, directory, old)
            mem = Memory(task_text='query',task_point=(6,5),task_started=1,bootstrap_done=True,
                         pending=('cmd',1),running_python=BAD,running_parser=old,running_parser_id=skill['id'],
                         skills=[skill],contract=contract,
                         documents=[{'output':'与上一题完全相同的日志格式'}],
                         inputs={'directory':directory,'files':[{'path':'sensor.log'}]})
            prompt, _, _ = step(mem,2,lastCmdResult=raw)
            self.assertIn('repair.targets', prompt)
            self.assertIsNone(mem.reusable_parser())
            self.assertFalse(skill['disabled'])
            self.assertEqual(mem.log_diagnostics['failed_parser'], skill['id'])
            _, command, _ = step(mem,3,llmResp='PYTHON\n'+GOOD_FUNCTION)
            self.assertTrue(command)
            self.assertIsNone(mem.running_parser)
            raw, _, report = execute(mem.running_python, directory)
            self.assertNotIn('reused_parsers', report)
            self.assertEqual(report['parse_lines']['matched'], 2)
            _, _, ledger = step(mem,4,lastCmdResult=raw)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'count':1})

    def test_signature_binding_both_orders_keywords_and_unknown(self):
        for signature, call in [('line, system', "parse_line(raw, 'sensor')"),
                                ('system, line', "parse_line('sensor', raw)"),
                                ('*, line, system', "parse_line(system='sensor', line=raw)")]:
            with self.subTest(signature=signature), tempfile.TemporaryDirectory() as directory:
                code = f"def parse_line({signature}):\n return None\nraw='2 OK  module=alpha'\n{call}"
                _, _, report = execute(code, directory)
                self.assertEqual(list(report['parse_systems']), ['sensor'])
                self.assertEqual(report['parse_systems']['sensor']['unmatched'], ['2 OK  module=alpha'])
                self.assertEqual(report['logs']['parsers']['parse_line']['unmatched_count'], 1)
        with tempfile.TemporaryDirectory() as directory:
            _, _, report = execute("def parse_line(a, b):\n return None\nparse_line('secret raw', 'sensor')", directory)
            self.assertEqual(list(report['parse_systems']), ['unknown'])
            self.assertEqual(report['parse_lines']['unmatched'], ['<line argument unavailable>'])

    def test_failed_execution_to_compact_prompt_to_partial_repair_and_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('1 BAD module=alpha\n2 OK  module=alpha\n')
            raw, _, report = execute(BAD, directory)
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                         pending=('cmd', 1), running_python=BAD,
                         contract={'input_kind':'logs', 'example':{'count':0}},
                         inputs={'directory':directory, 'files':[{'path':'sensor.log'}]})
            prompt, _, ledger = step(mem, 2, lastCmdResult=raw)
            self.assertTrue(mem.input_blocked)
            self.assertFalse(ledger.commands)
            self.assertIn('repair.targets', prompt)
            context = json.loads(prompt.split('上下文：', 1)[1])
            self.assertNotIn('history', context)
            self.assertNotIn('lastAttempt', context)
            self.assertEqual(context['repair']['targets'], ['parse_line'])
            self.assertEqual(context['repair']['systems']['sensor']['unmatched'], ['2 OK  module=alpha\n'])
            self.assertNotIn('task_result', context['repair']['source'])
            broken = GOOD_FUNCTION.replace(r'(\d+)\s+(\w+)\s+module=(\S+)', '[z-a]')
            retry, command, _ = step(mem, 3, llmResp='PYTHON\n'+broken)
            self.assertFalse(command)
            self.assertIn('正则表达式无效', retry)
            self.assertEqual(mem.repair_python, BAD)
            _, command, _ = step(mem, 4, llmResp='PYTHON\n'+GOOD_FUNCTION)
            self.assertTrue(command)
            # Original aggregation and file reads survive unchanged in the executable AST.
            untouched = lambda code: [ast.dump(n) for n in ast.parse(code).body if not isinstance(n, ast.FunctionDef)]
            self.assertEqual(untouched(mem.running_python), untouched(BAD))
            raw, result, report = execute(mem.running_python, directory)
            self.assertEqual(report['parse_lines']['matched'], 2)
            self.assertEqual(report['parse_systems']['sensor']['normal'], 1)
            _, _, ledger = step(mem, 5, lastCmdResult=raw)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'count':1})
            self.assertFalse(mem.input_blocked)
            self.assertFalse(mem.repair_python)

    def test_patch_cannot_change_signature_or_aggregation(self):
        context = {'targets':['parse_line']}
        for patch in (GOOD_FUNCTION.replace('(line, system)', '(system, line)'),
                      GOOD_FUNCTION+'\ntask_result({"count":999})',
                      GOOD_FUNCTION+'\ndef other():\n return 1',
                      'import json as re\n'+GOOD_FUNCTION):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                merge_repair(BAD, patch, context)
        self.assertEqual(ast.dump(ast.parse(merge_repair(BAD, BAD, context))), ast.dump(ast.parse(BAD)))

    def test_literal_regex_validation_does_not_execute_or_rewrite(self):
        with self.assertRaisesRegex(ValueError, '正则表达式无效'):
            validate_literal_regex("import re\ndef parse_line(line):\n return re.match(r'[z-a]', line)")
        validate_literal_regex("import re\nraise RuntimeError('must not execute')\nre.match(dynamic(), line)")
        validate_literal_regex(r"import re; re.match(r'\\d+', line)")  # Valid literal backslash is not auto-fixed.
        validate_literal_regex("import re\nre.compile('a # [', flags=re.X)")

    def test_exception_targets_and_no_evidence_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            code = 'def parse_sensor_line(line):\n raise ValueError("bad timestamp")\nparse_sensor_line("input")'
            _, _, report = execute(code, directory)
            context = repair_context(code, {'logs':report['logs']})
            self.assertEqual(context['targets'], ['parse_sensor_line'])
            self.assertIn('bad timestamp', context['parsers']['parse_sensor_line']['error'])
        self.assertIsNone(repair_context(BAD, {}))

    def test_learned_reverse_signature_uses_named_current_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('7\n')
            parser = "def parse_line(line, system):\n return {'system':system,'timestamp':int(line),'is_fault':False}"
            _, result, report = execute("rows=task_parse('sensor', 'sensor.log')\ntask_result({'count':len(rows)})", directory, parser)
            self.assertEqual(result[0], 'ok')
            self.assertEqual(report['parse_systems']['sensor']['normal'], 1)
