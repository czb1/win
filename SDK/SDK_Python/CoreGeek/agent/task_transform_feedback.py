"""Evidence-only transform feedback; never infer business rules or field renames."""
import json


def diagnosis(raw):
    for line in str(raw).splitlines():
        if line.startswith('TRANSFORM_DIAGNOSTIC '):
            try:
                value = json.loads(line.partition(' ')[2])
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
    return None


def schema(value):
    if isinstance(value, dict):
        return {'object': {k: schema(v) for k, v in sorted(value.items())}}
    if isinstance(value, list):
        variants = {json.dumps(schema(v), sort_keys=True) for v in value}
        return {'array': [json.loads(v) for v in sorted(variants)]}
    return type(value).__name__


def metadata(inputs, contract):
    records, outputs = [], []
    for entry in inputs.get('files', []):
        for case in entry.get('case_samples', []):
            if isinstance(case.get('input'), list):
                records.extend(case['input'])
            outputs.append(case.get('expected', case.get('output')))
        if entry.get('type') == 'list' and not entry.get('case_count'):
            try:
                preview = json.loads(entry.get('sample', ''))
                if isinstance(preview, list):
                    records.extend(preview)
            except (ValueError, TypeError):
                pass
    return {'source': contract.get('family'), 'input_schema': schema(records) if records else None,
            'output_schema': schema(outputs) if outputs else None,
            'schema_evidence': 'observed_preview_only'}


def history_methods(skills, current):
    candidates = []
    for skill in skills:
        previous = skill.get('transform_metadata', {})
        if (not skill.get('transform') or skill.get('disabled') or 'rounds' not in skill
                or skill.get('evidence') != 'legal_submission_then_task_disappeared'
                or previous.get('source') != current.get('source')
                or not current.get('input_schema')
                or previous.get('input_schema') != current['input_schema']):
            continue
        exact = previous.get('output_schema') == current.get('output_schema')
        candidates.append((exact, skill.get('successes', 0), {
            'id': skill.get('id'), 'source': previous['source'],
            'compatibility': 'observed_schema_match' if exact else 'output_adaptation_required',
            'evidence': skill.get('evidence'), 'metadata': previous, 'python': skill['transform']}))
    candidates.sort(key=lambda item: item[:2], reverse=True)
    return [item[2] for item in candidates[:2]]


def dominates(a, b):
    """Sample-distance dominance is a repair hint, never proof of correct rules."""
    if not a or not b or not a.get('sample_fingerprint') or a.get('sample_fingerprint') != b.get('sample_fingerprint'):
        return False
    qa, qb = a.get('quality'), b.get('quality')
    if not qa or not qb or len(qa) != len(qb):
        return False
    if any(len(x) != len(y) for x, y in zip(qa, qb)):
        return False
    pairs = [(x, y) for ra, rb in zip(qa, qb) for x, y in zip(ra, rb)]
    return all(x <= y for x, y in pairs) and any(x < y for x, y in pairs)


def observe(mem, attempt):
    detail = attempt.get('diagnosis') or {}
    mem.transform_progress = {}
    if detail.get('stage') != 'samples' or not detail.get('sample_fingerprint'):
        return
    sample = detail['sample_fingerprint']
    key = (sample, detail.get('output_fingerprint'))
    repeated = key in mem.transform_outputs
    mem.transform_outputs.add(key)
    prior = mem.transform_best.get('diagnosis') or {}
    if not prior or prior.get('sample_fingerprint') != sample:
        mem.transform_best = dict(attempt)
        relation = 'first'
    elif dominates(detail, prior):
        mem.transform_best = dict(attempt)
        relation = 'improved'
    elif dominates(prior, detail):
        relation = 'regressed'
    else:
        relation = 'equal_or_incomparable'
    mem.transform_progress = {'comparison': relation, 'same_sample_outputs': repeated}


def compact(detail):
    if not detail:
        return detail
    result = dict(detail)
    failures = []
    for original in detail.get('failures', []):
        item = dict(original)
        evidence = dict(item.get('record_evidence', {}))
        for key in ('expected_kept', 'expected_dropped', 'input_order'):
            evidence.pop(key, None)
        if item.get('classification') != 'order_only':
            evidence.pop('expected_order', None)
            evidence.pop('actual_order', None)
        if evidence:
            item['record_evidence'] = evidence
        failures.append(item)
    if 'failures' in detail:
        result['failures'] = failures
    return result


def focus(detail):
    return [{'case': f['case'], 'classification': f.get('classification'),
             'difference': f.get('difference'),
             **{k: f.get('record_evidence', {})[k] for k in ('missing_count', 'extra_count')
                if k in f.get('record_evidence', {})}}
            for f in (detail or {}).get('failures', [])]


def repair_direction(detail, remaining):
    stage = (detail or {}).get('stage')
    if stage == 'samples':
        action = '程序已运行，但公开样例不匹配。依据具体反例修正规则；允许同时修复多个有证据的问题。'
    elif stage == 'checker':
        action = '公开样例已通过，正式checker未通过。依据原始反馈核对规则与边界，不按差额修改答案。'
    elif stage:
        action = '执行或输入处理失败。依据原始异常及traceback修复对应步骤。'
    else:
        action = '依据题目和完整公开样例确定规则，再输出完整函数。'
    if remaining > 4:
        action += '证据充分时直接修复；关键证据缺失时才用READ及诊断读取入口补充。'
    else:
        action += '临近截止，使用现有证据直接给出修复代码。'
    return action


def prompt(mem, remaining):
    attempt = mem.transform_attempt or (mem.last_attempt if mem.last_attempt.get('tool') == 'transform' else {})
    detail = attempt.get('diagnosis')
    best = getattr(mem, 'transform_best', {})
    use_best = dominates(best.get('diagnosis'), detail)
    base = best if use_best else attempt
    base_detail = base.get('diagnosis')
    rejected = getattr(mem, 'rejected_transform', {})
    context = {'remainingRounds': remaining, 'stage': detail.get('stage') if detail else 'initial',
               'nextAction': repair_direction(detail, remaining),
               'progress': getattr(mem, 'transform_progress', {}),
               'focus': focus(base_detail),
               'repairBase': {'source': 'better_observed_candidate' if use_best else 'last_attempt',
                              'python': base.get('python', ''),
                              'note': '仅为公开样例上的修复起点，不证明业务规则正确，不限制修改范围。'},
               'lastAttempt': {k: attempt[k] for k in ('status',) if k in attempt},
               'rejections': [rejected['reason']] if rejected else [],
               'rejectedCandidate': {k: rejected[k] for k in ('reason', 'executed') if k in rejected},
               'documents': [{**d, 'output': d.get('output', '').partition('\nTASK_INPUTS ')[0]}
                             for d in mem.documents if d.get('kind') != 'read'],
               'inputPreview': {**mem.inputs, 'files': []},
               'verifiedMethods': [] if attempt else history_methods(mem.skills, metadata(mem.inputs, mem.contract))}
    if detail:
        context['lastAttempt']['diagnosis'] = compact(detail)
    if use_best:
        context['repairBase']['diagnosis'] = compact(base_detail)
        context['lastAttempt']['diagnosis'] = {k: detail[k] for k in
            ('stage', 'case_count', 'failed_count', 'quality', 'read_command') if k in detail}
    if attempt and not detail:
        context['lastAttempt']['sandbox'] = attempt.get('sandbox', '')
        context['lastAttempt']['runtimeError'] = attempt.get('runtimeError', '')
    failed = {f['case'] for f in (base_detail or {}).get('failures', []) if 'input' in f}
    for entry in mem.inputs.get('files', []):
        clean = dict(entry)
        if 'case_samples' in clean:
            clean.pop('sample', None)
            clean['case_samples'] = [c for c in clean['case_samples'] if c.get('case') not in failed]
        context['inputPreview']['files'].append(clean)
    context['additionalReads'] = [d for d in mem.documents if d.get('kind') == 'read']
    return ("第一行 PYTHON，后面只定义 transform(records) 和必要辅助函数。框架负责样例验证、写文件、checker及真实token提交。\n"
            "先根据题目和样例确定规则，再输出完整函数；核对每条推断已落实到代码，并用反例检查矛盾。不要在代码注释中展开长篇试探。\n"
            "不得硬编码样例答案或特殊回合；字段和条件必须来自本题证据。无法可靠关联记录时不要猜过滤。\n"
            "按nextAction执行。focus索引指向诊断中的完整反例；混合错误可能同时涉及成员、数值和排序。\n"
            "repairBase只提供一份修复代码。same_sample_outputs=true表示已测样例输出重复，不代表程序在所有输入上等价。请改变有证据的规则，不只改写法。\n"
            "rejectedCandidate表示同一失败AST未再执行。verifiedMethods仅为历史参考，仍须通过本题验证。\n"
            "READ 路径 偏移：样例偏移为case编号，其他文件按读取入口和NEXT_READ继续。\n"
            "上下文：" + json.dumps(context, ensure_ascii=False))
