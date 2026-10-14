"""Source-text transport regressions for previews and READ continuation."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from test_evolution_v2 import step
from agent.intelligence import Memory
from agent.task_inputs import input_context, preview_code
from agent.task_tools import document_code, file_code, parse_file_tool, task_document_code
from agent.task_transform_feedback import metadata


class TaskInputSourceTests(unittest.TestCase):
    def execute(self, code, root):
        result = subprocess.run([sys.executable, '-c', code], cwd=root,
                                capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        # Decode bytes without universal-newline conversion in the test harness.
        return result.stdout.decode('utf-8')

    def preview(self, root, files):
        doc = root / 'task.md'
        text = '读取 ' + '、'.join('`' + name + '`' for name in files) + '。'
        doc.write_text(text, encoding='utf-8')
        output = self.execute(preview_code(repr(str(doc)), repr(text)), root)
        return input_context(output), output

    def test_json_preserves_source_structure_and_real_case_fields(self):
        source = ('\ufeff{\r\n  "meta": "原始字段",\r\n  "cases": [\r\n'
                  '    {"input": [{"key": "\\u0061", "n": 1.2300e+02}], "expected": []},\r\n'
                  '    {"case": "源文件编号", "input": [], "expected": []}\r\n'
                  '  ]\r\n}\t ')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'cases.json'
            path.write_bytes(source.encode('utf-8'))
            context, _ = self.preview(root, ['cases.json'])
            entry = context['files'][0]
            self.assertTrue(entry['complete'])
            self.assertEqual(entry['content'], source)
            self.assertEqual((entry['start'], entry['end']), (0, len(source)))
            self.assertEqual(entry['bytes'], len(source.encode('utf-8')))
            value = json.loads(entry['content'].lstrip('\ufeff'))
            self.assertEqual(set(value), {'meta', 'cases'})
            self.assertNotIn('case', value['cases'][0])
            self.assertEqual(value['cases'][1]['case'], '源文件编号')
            for field in ('sample', 'case_samples', 'preview', 'next_case', 'next_read'):
                self.assertNotIn(field, entry)
            self.assertEqual(path.read_bytes(), source.encode('utf-8'))

    def test_csv_tsv_and_log_preserve_whitespace_and_all_lines(self):
        files = {'rows.csv': '编号,值\r\n1,"a,b"\r\n2,"引号"\r\n\r\n',
                 'rows.tsv': 'a\tb\n1\t 2 \n3\t4\n5\t6\n',
                 'events.log': 'first\rsecond\nthird\r\nfourth\n\n'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                (root / name).write_bytes(text.encode('utf-8'))
            context, _ = self.preview(root, files)
            self.assertEqual(len(context['files']), len(files))
            for entry in context['files']:
                self.assertEqual(entry['content'], files[entry['path']])
                self.assertTrue(entry['complete'])

    def test_json_keeps_duplicate_keys_and_escaped_unicode_as_written(self):
        source = '{"\\ud800": 1, "value": 1e+02, "value": 100.00, "\\u0061": "原文"}'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'lexical.json').write_text(source, encoding='utf-8')
            context, _ = self.preview(root, ['lexical.json'])
            self.assertTrue(context['files'][0]['complete'])
            self.assertEqual(context['files'][0]['content'], source)

    def test_invalid_json_is_still_source_text(self):
        source = '{"rows": [1, 2,\r\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'invalid.json').write_bytes(source.encode('utf-8'))
            context, _ = self.preview(root, ['invalid.json'])
            self.assertEqual(context['files'][0]['content'], source)
            self.assertTrue(context['files'][0]['complete'])
            self.assertNotIn('error', context['files'][0])

    def test_optional_metadata_cannot_block_deep_source_json(self):
        source = '[' * 1500 + '0' + ']' * 1500
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'deep.json').write_text(source, encoding='utf-8')
            context, _ = self.preview(root, ['deep.json'])
            self.assertTrue(context['files'][0]['complete'])
            self.assertEqual(context['files'][0]['content'], source)

    def test_schema_compatibility_observes_complete_source_without_mutating_it(self):
        cases = [{'input': [{'key': 'a', 'rank': 3}], 'expected': ['a']}]
        source = json.dumps({'cases': cases}, indent=2)
        raw = {'files': [{'content': source, 'complete': True, 'type': 'dict', 'case_count': 1}]}
        old = {'files': [{'case_samples': cases}]}
        self.assertEqual(metadata(raw, {}), metadata(old, {}))
        self.assertEqual(raw['files'][0]['content'], source)
        self.assertNotIn('case_samples', raw['files'][0])
        raw['files'][0]['complete'] = False
        self.assertIsNone(metadata(raw, {})['input_schema'])

    def test_invalid_utf8_is_reported_without_replacement_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'invalid.log'
            path.write_bytes(b'original\xffdata')
            context, _ = self.preview(root, ['invalid.log'])
            entry = context['files'][0]
            self.assertFalse(entry['complete'])
            self.assertEqual(entry['content'], '')
            self.assertIn('error', entry)
            self.assertNotIn('\ufffd', json.dumps(context, ensure_ascii=False))
            self.assertEqual(path.read_bytes(), b'original\xffdata')

    def test_current_sized_input_and_cases_both_fit_in_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {'cases.json': '{"cases": [{"input": [], "expected": []}]}' + ' ' * 4379,
                       'input.json': '[{"id": 1, "value": "当前输入"}]' + ' ' * 6920}
            for name, text in sources.items():
                (root / name).write_bytes(text.encode('utf-8'))
            context, _ = self.preview(root, sources)
            for entry in context['files']:
                self.assertTrue(entry['complete'])
                self.assertEqual(entry['content'], sources[entry['path']])

    def test_byte_bounded_preview_continues_at_exact_character_offset(self):
        source = ('中文😀"\\\t\r\n' * 4200) + '末尾没有换行'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'large.log'
            path.write_bytes(source.encode('utf-8'))
            context, output = self.preview(root, ['large.log'])
            self.assertLessEqual(len(output.encode('utf-8')), 24000)
            entry = context['files'][0]
            self.assertFalse(entry['complete'])
            self.assertEqual(entry['content'], source[:entry['end']])
            self.assertGreater(entry['end'], 0)
            action = parse_file_tool(entry['next_read'])
            self.assertEqual(action, {'read': str(path), 'start': entry['end']})
            recovered = entry['content']
            while action:
                start = action['start']
                raw = self.execute(file_code('read', action['read'], start), root)
                body = raw.partition('\n')[2]
                expected = source[start:start + 6000]
                self.assertTrue(body.startswith(expected + '\n'))
                recovered += body[:len(expected)]
                continuation = body[len(expected) + 1:]
                marker = re.fullmatch(r'NEXT_READ (.+)', continuation.strip())
                action = parse_file_tool('READ ' + marker[1]) if marker else None
                if start + len(expected) < len(source):
                    self.assertEqual(action['start'], start + len(expected))
                else:
                    self.assertIsNone(action)
            self.assertEqual(recovered, source)

    def test_budget_keeps_other_file_locations_without_claiming_full_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ['part_%d.log' % i for i in range(6)]
            for name in names:
                (root / name).write_text('中文' * 20000, encoding='utf-8')
            context, output = self.preview(root, names)
            self.assertLessEqual(len(output.encode('utf-8')), 24000)
            self.assertEqual([e['path'] for e in context['files']], names)
            for entry in context['files']:
                self.assertFalse(entry['complete'])
                self.assertEqual(entry['end'], len(entry['content']))
                self.assertEqual(parse_file_tool(entry['next_read'])['start'], entry['end'])

    def test_read_preserves_line_endings_for_absolute_and_relative_paths(self):
        source = ('一\r\n二\r三\n' * 1000) + '尾部\r\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'data.log'
            path.write_bytes(source.encode('utf-8'))
            for requested, base in ((str(path), None), ('data.log', str(root))):
                with self.subTest(requested=requested):
                    output = self.execute(document_code(requested, start=5999, base=base), root)
                    self.assertEqual(output.partition('\n')[2], source[5999:] + '\n')

    def test_discovery_to_prompt_preserves_raw_input_and_outer_metadata(self):
        source = '{\r\n "cases": [{"input": [], "expected": [], "note": "中文\\n引号\\\""}]\r\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cases.json').write_bytes(source.encode('utf-8'))
            doc = root / 'task.md'
            doc.write_text('查看 `cases.json` 并按题意完成操作。', encoding='utf-8')
            raw = self.execute(task_document_code(str(doc)), root)
            task = '请阅读' + str(doc) + '，获取任务信息'
            mem = Memory(task_text=task, task_started=1, bootstrap_done=True,
                         pending=('cmd', 1), running_tool={'kind': 'discover', 'path': str(doc), 'start': 0})
            prompt, command, _ = step(mem, 2, lastCmdResult='[exitCode:0]\n[durationMs:12]\n' + raw)
            self.assertFalse(command)
            context = json.loads(prompt.split('上下文：\n', 1)[1])
            self.assertEqual(context['inputPreview']['files'][0]['content'], source)
            self.assertTrue(context['inputPreview']['files'][0]['complete'])
            self.assertIn('外层路径、大小、类型、数量', prompt)
            self.assertIn('按 next_read 的字符偏移继续 READ', prompt)
            self.assertNotIn('case', json.loads(context['inputPreview']['files'][0]['content'])['cases'][0])


if __name__ == '__main__':
    unittest.main()
