"""Repair evidence survives renaming, mixed failures and prompt compaction."""
import copy
import json
from pathlib import Path
import tempfile
import sys
import unittest

import test_weak_model_evolution as weak
from agent.intelligence import Memory
from agent.task_skills import learned_method
from agent.task_transform_feedback import diagnosis, history_methods, metadata, prompt


class RepairContextTests(unittest.TestCase):
    fixture = weak.WeakModelTests.fixture
    execute = weak.WeakModelTests.execute

    def test_mixed_failure_keeps_nearest_contrast_and_order_pair(self):
        # Vary both names and values: no business field or numeric cutoff is assumed.
        for identity, field, low in [('ref', 'weight', 17), ('ticket', 'load', -4)]:
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cfg, *_ = self.fixture(root)
                records = [{identity:'a', 'class':'q', field:low},
                           {identity:'b', 'class':'q', field:low+1},
                           {identity:'c', 'class':'q', field:low+30}]
                (root/'data.json').write_text(json.dumps(records))
                expected = {'nested': {'rows': [{identity:'b'}, {identity:'c'}]}}
                (root/'examples.json').write_text(json.dumps([{'input':records, 'expected':expected}]))
                code = f"def transform(records): return {{'nested':{{'rows':[{{'{identity}':r['{identity}']}} for r in reversed(records)]}}}}"
                _, body, _ = self.execute(code, cfg)
                detail = diagnosis(body)
                evidence = detail['failures'][0]['record_evidence']
                self.assertEqual(evidence['extra_count'], 1)
                self.assertEqual(evidence['contrasts'][0]['contrast'], records[1])
                pair = evidence['order_pairs'][0]
                self.assertEqual(pair['expected_before']['record'], records[1])
                self.assertEqual(pair['expected_after']['record'], records[2])
                self.assertEqual(pair['expected_before']['input_index'], 1)
                mem = Memory()
                mem.transform_attempt = {'python':code, 'diagnosis':detail}
                original = copy.deepcopy(detail)
                context = json.loads(prompt(mem, 8).split('上下文：')[1])
                self.assertIn('contrasts', context['focus'][0])
                self.assertIn('order_pairs', context['focus'][0])
                self.assertLess(list(context).index('focus'), list(context).index('repairBase'))
                self.assertEqual(context['lastAttempt']['diagnosis']['failures'][0]['expected'], expected)
                self.assertNotIn('order_pairs', context['lastAttempt']['diagnosis']['failures'][0]['record_evidence'])
                self.assertEqual(detail, original)  # presentation must not mutate retained evidence

    def test_identity_candidates_must_agree_on_order_not_only_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg, *_ = self.fixture(root)
            records = [{'ref':'a','other':'x'}, {'ref':'b','other':'y'}]
            expected = [{'ref':'a','other':'y'}, {'ref':'b','other':'x'}]
            (root/'examples.json').write_text(json.dumps([{'input':records,'expected':expected}]))
            _, body, _ = self.execute('def transform(records): return records', cfg)
            evidence = diagnosis(body)['failures'][0]['record_evidence']
            self.assertIn('unavailable', evidence['alignment'])
            self.assertNotIn('order_pairs', evidence)

    def test_prompt_prunes_only_metadata_and_keeps_error_and_empty_task_values(self):
        mem = Memory(documents=[{'kind':'discover','output':'CURRENT REQUIREMENTS'}],
                     inputs={'files':[{'case_samples':[{'case':0,'input':[], 'expected':{'rows':[], 'ok':False, 'n':0}}]}]})
        mem.transform_attempt = {'python':'def transform(records): return records',
                                 'diagnosis':{'stage':'execution', 'traceback':'ValueError: exact root cause'}}
        text = prompt(mem, 8)
        context = json.loads(text.split('上下文：')[1])
        for name in ('verifiedMethods','rejections','rejectedCandidate','additionalReads','progress','focus'):
            self.assertNotIn(name, context)
        self.assertNotIn('AST', text)
        self.assertNotIn('same_sample_outputs', text)
        self.assertEqual(context['lastAttempt']['diagnosis']['traceback'], 'ValueError: exact root cause')
        self.assertEqual(context['inputPreview']['files'][0]['case_samples'][0]['expected'],
                         {'rows':[], 'ok':False, 'n':0})
        self.assertEqual(text.count('def transform(records)'), 1)
        self.assertIn('CURRENT REQUIREMENTS', text)

    def test_first_checker_failure_exposes_original_feedback_records_and_expressions(self):
        mem = Memory()
        joined = {'expected':{'record':{'ref':'a'}}, 'actual':{'record':{'ref':'b'}}}
        mem.transform_attempt = {'python':"def transform(records): return sorted(records, key=lambda r:r['weight'])",
                                 'diagnosis':{'stage':'checker','checker_feedback':'[FAIL] exact message',
                                              'record_context':joined, 'sample_evidence':[{'case':0, 'alignment':'unavailable'}]}}
        context = json.loads(prompt(mem, 7).split('上下文：')[1])
        self.assertEqual(context['lastAttempt']['diagnosis']['record_context'], joined)
        self.assertEqual(context['lastAttempt']['diagnosis']['checker_feedback'], '[FAIL] exact message')
        self.assertTrue(context['currentExpressions'])
        self.assertEqual(context['lastAttempt']['diagnosis']['sample_evidence'],
                         [{'case':0, 'alignment':'unavailable'}])
        self.assertLess(list(context).index('lastAttempt'), list(context).index('repairBase'))

    def test_passing_samples_keep_contrasts_through_checker_repair(self):
        for identity, quantity, cutoff in [('ref', 'weight', 17), ('ticket', 'load', -4)]:
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cfg, contract, docs, preview = self.fixture(root)
                records = [{identity:'off', 'active':False, quantity:cutoff+30},
                           {identity:'low', 'active':True, quantity:cutoff},
                           {identity:'edge', 'active':True, quantity:cutoff+1},
                           {identity:'high', 'active':True, quantity:cutoff+30}]
                def result(rows):
                    return {'total':sum(r[quantity] for r in rows),
                            'rows':[{identity:r[identity]} for r in rows]}
                (root/'examples.json').write_text(json.dumps([
                    {'input':records, 'expected':result(records[2:])}]))
                current = [{identity:'extra', 'active':True, quantity:cutoff-5}, records[-1]]
                (root/'data.json').write_text(json.dumps(current))
                (root/'check_custom').write_text(
                    f'#!{sys.executable}\nimport json\nfrom pathlib import Path\n'
                    "v=json.loads(Path('report.json').read_text())\n"
                    f"print('TOKEN: verified' if v == {result(current[1:])!r} else '[FAIL] $/total incorrect')\n")
                code = (f"def transform(records):\n rows=[r for r in records if r['active'] and r[{quantity!r}] != {cutoff}]\n"
                        f" return {{'total':sum(r[{quantity!r}] for r in rows),'rows':[{{{identity!r}:r[{identity!r}]}} for r in rows]}}")
                _, body, answer = self.execute(code, cfg)
                self.assertIsNone(answer)
                detail = diagnosis(body)
                self.assertEqual(detail['samples_passed'], 1)
                evidence = detail['sample_evidence'][0]
                self.assertEqual(evidence['case'], 0)
                pair = next(p for p in evidence['contrasts'] if p['record'] == records[1])
                self.assertEqual(pair['contrast'], records[2])
                self.assertEqual(pair['different_fields'], [quantity])
                mem = Memory(contract=contract, documents=docs, inputs=preview)
                mem.transform_attempt = {'python':code, 'diagnosis':detail}
                mem.transform_progress = {'same_checker_failure':True, 'repeat_count':2}
                original = copy.deepcopy(detail)
                text = prompt(mem, 7)
                context = json.loads(text.split('上下文：')[1])
                self.assertEqual(context['lastAttempt']['diagnosis']['sample_evidence'][0]['contrasts'], evidence['contrasts'])
                self.assertIn('重新比较规则候选', context['nextAction'])
                self.assertIn('未定位到唯一输入记录', context['nextAction'])
                self.assertNotIn('record_context中的完整输入', text)
                self.assertEqual(detail, original)
                # Changing the general rule is allowed; only the checker grants a token.
                _, _, answer = self.execute(code.replace(f'!= {cutoff}', f'> {cutoff}'), cfg)
                self.assertEqual(json.loads(answer), {'token':'verified'})

    def test_checker_sample_identity_ambiguity_does_not_invent_contrasts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg, *_ = self.fixture(root)
            records = [{'ref':'same', 'kind':'same'}, {'ref':'same', 'kind':'same'}]
            (root/'examples.json').write_text(json.dumps([{'input':records, 'expected':records[:1]}]))
            _, body, answer = self.execute('def transform(records): return records[:1]', cfg)
            self.assertIsNone(answer)
            evidence = diagnosis(body)['sample_evidence'][0]
            self.assertIn('unavailable', evidence['alignment'])
            self.assertNotIn('contrasts', evidence)

    def test_history_keeps_source_and_does_not_claim_new_output_is_verified(self):
        before = {'files':[{'case_samples':[{'input':[{'ref':'a','weight':3}], 'expected':{'total':3}}]}]}
        after = {'files':[{'case_samples':[{'input':[{'ref':'a','weight':3}], 'expected':{'rows':[{'ref':'a'}]}}]}]}
        contract = {'family':'family/task#.md', 'source':'/workspace/task1.md', 'example':{'token':''}}
        previous = metadata(before, contract)
        skill = learned_method((1,1), contract, transform='def transform(records): return {}', transform_metadata=previous)
        skill['rounds'] = 4
        current = metadata(after, {**contract, 'source':'/workspace/task2.md'})
        result = history_methods([skill], current)[0]
        self.assertEqual(result['source_document'], '/workspace/task1.md')
        self.assertEqual(result['compatibility'], 'output_adaptation_required')
        self.assertIn('重新验证', result['reuse_boundary'])
        self.assertNotEqual(result['output_change']['previous'], result['output_change']['current'])
        mem = Memory(skills=[skill], inputs=after, contract=contract)
        context = json.loads(prompt(mem, 10).split('上下文：')[1])
        self.assertLess(list(context['verifiedMethods'][0]).index('reuse_boundary'),
                        list(context['verifiedMethods'][0]).index('python'))
