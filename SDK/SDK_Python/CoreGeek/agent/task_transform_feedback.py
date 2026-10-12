"""Evidence-only transform feedback; never infer business rules or field renames."""
import json
from .task_history import references
from .task_log_repair import repair_expressions


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
    return {'source': contract.get('family'), 'source_document': contract.get('source'), 'input_schema': schema(records) if records else None,
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
            'evidence': skill.get('evidence'),
            'source_document': previous.get('source_document'),
            'reuse_boundary': '旧题通过仅证明旧任务结果通过；新增字段、明细顺序及过滤要求必须按当前题目重新验证。',
            'output_change': {'previous': previous.get('output_schema'), 'current': current.get('output_schema')}
                             if not exact else None,
            'metadata': {k: v for k, v in previous.items()
                         if k not in ('source', 'source_document') and (exact or k != 'output_schema')},
            'python': skill['transform']}))
    candidates.sort(key=lambda item: item[:2], reverse=True)
    return [{k: v for k, v in item[2].items() if v is not None} for item in candidates[:2]]


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
    previous = mem.transform_progress
    mem.transform_progress = {}
    if detail.get('stage') == 'checker':
        feedback = detail.get('checker_feedback', '')
        repeated = bool(feedback) and feedback == previous.get('checker_feedback')
        mem.transform_progress = {'checker_feedback': feedback, 'same_checker_failure': repeated,
                                  'repeat_count': previous.get('repeat_count', 1) + 1 if repeated else 1}
        return
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
    result = []
    for failure in (detail or {}).get('failures', []):
        evidence = failure.get('record_evidence', {})
        item = {'case': failure['case'], 'classification': failure.get('classification'),
                'difference': failure.get('difference')}
        for key in ('alignment', 'identity_field', 'missing_count', 'extra_count', 'contrasts', 'order_pairs'):
            if key in evidence and evidence[key] not in (None, [], {}):
                item[key] = evidence[key]
        result.append(item)
    return result


def sample_evidence(detail):
    """Keep observed contrasts once, without repeating sample output order."""
    result = []
    for evidence in (detail or {}).get('sample_evidence', []):
        item = {k: evidence[k] for k in ('case', 'alignment', 'identity_field', 'contrasts', 'contrast_search_truncated')
                if k in evidence and evidence[k] not in (None, [], {})}
        contrasts = evidence.get('contrasts', [])
        fields = {k for pair in contrasts for k in pair.get('different_fields', [])} if isinstance(contrasts, list) else set()
        for key in ('expected_kept', 'expected_dropped'):
            summary = evidence.get(key, {})
            if summary:
                item[key] = {k: summary[k] for k in ('count',) if k in summary}
                numeric = {k: v for k, v in summary.get('numeric_fields', {}).items() if k in fields}
                if numeric:
                    item[key]['numeric_fields'] = numeric
        if item:
            result.append(item)
    return result


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
               'referencedTasks': references(mem.documents, mem.task_archive)[0],
               'documents': [{**d, 'output': d.get('output', '').partition('\nTASK_INPUTS ')[0]}
                             for d in mem.documents if d.get('kind') != 'read'],
               'inputPreview': {**mem.inputs, 'files': []},
               'verifiedMethods': [] if attempt else history_methods(mem.skills, metadata(mem.inputs, mem.contract))}
    if detail:
        context['lastAttempt']['diagnosis'] = compact(detail)
    if detail and detail.get('stage') == 'checker':
        context['lastAttempt']['diagnosis']['sample_evidence'] = sample_evidence(detail)
        context['currentExpressions'] = repair_expressions(base.get('python', ''))
        context['progress'] = {k: v for k, v in context['progress'].items() if k != 'checker_feedback'}
        if context['progress'].get('same_checker_failure'):
            context['nextAction'] = '同一checker反例仍未解决；这不证明其他输出完全相同。' + context['nextAction']
        record_context = detail.get('record_context', {})
        if record_context.get('expected') and record_context.get('actual'):
            context['nextAction'] += '结合record_context中的已关联记录核对原始输入与位置。'
        else:
            context['nextAction'] += '未定位到唯一输入记录；结合output_fragment、样例对照和输入摘要核查，不能把聚合差异直接归因于某条记录。'
    if use_best:
        context['repairBase']['diagnosis'] = compact(base_detail)
        context['lastAttempt']['diagnosis'] = {k: detail[k] for k in
            ('stage', 'case_count', 'failed_count', 'quality', 'read_command') if k in detail}
    if attempt and not detail:
        context['lastAttempt']['sandbox'] = attempt.get('sandbox', '')
        context['lastAttempt']['runtimeError'] = attempt.get('runtimeError', '')
    if rejected.get('reason') == 'failed_ast':
        if 'currentExpressions' not in context:
            context['rejectedCandidate']['unchangedExpressions'] = repair_expressions(rejected.get('python', ''))
        context['nextAction'] = ('本次代码与已失败代码的AST相同；注释修改无效。'
                                 '对照原反例修改执行表达式。' + context['nextAction'])
    reconsider = rejected.get('reason') == 'failed_ast' or context['progress'].get('same_checker_failure')
    if reconsider:
        context['nextAction'] = ('重新比较规则候选：在完整函数前用2～3行代码注释简述旧条件、冲突证据和本次修改；'
                                 '检查等值与范围、边界是否包含及组合条件是否遗漏，不预设修改方向。'
                                 '仅改注释、变量名或移动语句不能解决原反例。' + context['nextAction'])
    failed = {f['case'] for f in (base_detail or {}).get('failures', []) if 'input' in f}
    for entry in mem.inputs.get('files', []):
        clean = dict(entry)
        if 'case_samples' in clean:
            clean.pop('sample', None)
            clean['case_samples'] = [c for c in clean['case_samples'] if c.get('case') not in failed]
        context['inputPreview']['files'].append(clean)
    context['additionalReads'] = [d for d in mem.documents if d.get('kind') == 'read']
    # Put concise evidence before code; keep full counterexamples once below it.
    # Prune only presentation metadata. Never remove empty/false/zero task data.
    for section in (context['lastAttempt'].get('diagnosis'), context['repairBase'].get('diagnosis')):
        if section:
            for failure in section.get('failures', []):
                evidence = failure.get('record_evidence', {})
                for key in ('contrasts', 'order_pairs'):
                    evidence.pop(key, None)  # the focus section already carries these
    if not base.get('python'):
        context.pop('repairBase', None)
    order = ('stage', 'nextAction', 'remainingRounds', 'focus', 'lastAttempt', 'currentExpressions', 'progress',
             'rejections', 'rejectedCandidate', 'repairBase', 'verifiedMethods',
             'documents', 'referencedTasks', 'inputPreview', 'additionalReads')
    context = {key: context[key] for key in order
               if key in context and context[key] not in (None, '', [], {})}
    guidance = [
        '完成标准：必须通过本题官方checker并取得真实TOKEN才可提交。公开样例通过不代表完成；checker失败必须修正规则、重新生成结果并验证，不得修改checker、猜TOKEN或按差额改答案。',
        '最新官方反例优先：新代码必须解决该反例，不能用样例通过否定反例。当前题新增要求优先于历史题；旧代码通过不证明全部规则已经实现。',
        '多种规则都符合样例时仍是候选，结合引用题面和官方反馈区分；自行构造的预期不是规则证据。',
        '第一行 PYTHON，后面只定义转换函数和必要辅助函数；框架负责样例验证、写文件、checker及真实token提交。',
        '按nextAction修复，返回名为 transform、接收 records 参数的完整函数；必要补读使用 READ 路径 偏移（样例按case编号，其他按NEXT_READ）。',
        '分别核对保留条件、排序条件、分组与输出。条件须解释保留和删除的对照，排序须核对同值次序。',
        '允许利用数值差异定位可疑条件并提出通用规则候选，再由checker验证；禁止直接加减结果、硬编码记录或答案。',
        '样例恰好保留若干条不证明数量上限，字段恰好同序不证明排序依据；样例对照仅表示观测，不能证明唯一规则。',
        '代码注释仅简述依据，不展开长篇试探；不要求额外分析回复。']
    if context.get('focus'):
        guidance.append('focus是差异证据；顺序索引为投影后的遍历位置，不直接证明业务排序规则。关联不明确时查看完整反例。')
    if context.get('verifiedMethods'):
        guidance.append('历史方法仅作候选参考；先核对reuse_boundary和output_change，旧代码实现不等于当前规则已验证。')
    if context.get('progress', {}).get('same_sample_outputs'):
        guidance.append('已测样例输出重复；请按证据改变规则，不只改写法。这不证明全输入等价。')
    return '\n'.join(guidance) + '\n上下文：' + json.dumps(context, ensure_ascii=False)
