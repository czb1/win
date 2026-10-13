"""Session-local source pages, independent of successful executable recipes."""
from pathlib import PurePosixPath
import re
import shlex
from .task_tools import document_paths

# Resource bounds, never business rules. Missing pages remain explicitly readable.
MAX_DOCUMENTS = 64
MAX_PAGES = 16
PROMPT_CHARS = 12000


def remember(archive, record):
    path = record.get('resolved_path') or record.get('path', '')
    if not path.startswith('/') or PurePosixPath(path).suffix.lower() not in ('.md', '.txt', '.rst'):
        return
    raw = record.get('output', '').partition('\nTASK_INPUTS ')[0]
    if not raw.startswith(('DOCUMENT ', 'RESOLVED_DOCUMENT ')):
        return
    body = raw.partition('\n')[2]
    continuation = re.search(r'(?m)^NEXT_READ .+$', body)
    text = body[:continuation.start()] if continuation else body
    entry = archive.setdefault(path, {'path': path, 'pages': {}, 'title': ''})
    entry['is_task'] = entry.get('is_task', False) or record.get('kind') in ('discover', 'reference')
    start = record.get('start', 0)
    if start == 0:
        entry['pages'].clear()  # Refresh a source that has changed in the sandbox.
        title = re.search(r'(?m)^#\s+(.+)$', text)
        entry['title'] = title[1] if title else ''
    entry['pages'][start] = {'text': text, 'next_read': continuation[0] if continuation else None}
    while len(entry['pages']) > MAX_PAGES:
        del entry['pages'][max(entry['pages'])]
    while len(archive) > MAX_DOCUMENTS:
        del archive[next(iter(archive))]


def references(documents, archive):
    """Resolve explicit paths/titles/identifiers, never recursively expand history."""
    if not documents:
        return [], []
    entry = documents[0]
    source = entry.get('resolved_path') or entry.get('path', '')
    root = PurePosixPath(source).parent
    text = '\n'.join(d.get('output', '').partition('\nTASK_INPUTS ')[0]
                     for d in documents if (d.get('resolved_path') or d.get('path')) == source)
    chosen, missing = set(), []
    for name in document_paths(text):
        path = str(PurePosixPath(name) if name.startswith('/') else root / name)
        if path == source:
            continue
        explicit_history = re.search(r'(?:前题|上一题|历史题|引用|previous task|prior task)[^。\n]{0,100}' + re.escape(name), text, re.I)
        if not explicit_history and not archive.get(path, {}).get('is_task'):
            continue  # Current API/spec/data files are not historical tasks.
        if path in archive:
            chosen.add(path)
        else:
            missing.append(path)
    # Match identifiers actually seen in historical titles, not a fixed task series.
    aliases = {}
    for path, item in archive.items():
        if path == source:
            continue
        if not item.get('is_task'):
            continue
        title = item.get('title', '')
        names = [title] if title else []
        names += re.findall(r'(?<![A-Za-z0-9])(?:[A-Za-z]+[-_]\d+|[A-Za-z]+\d+)(?![A-Za-z0-9])', title)
        for alias in names:
            if re.search(r'(?<![A-Za-z0-9_-])' + re.escape(alias) + r'(?![A-Za-z0-9_-])', text):
                aliases.setdefault(alias, set()).add(path)
    unresolved = []
    for alias, candidates in aliases.items():
        local = {p for p in candidates if PurePosixPath(p).parent == root}
        candidates = local or candidates
        if len(candidates) == 1:
            chosen.update(candidates)
        else:
            unresolved.append({'reference': alias, 'status': 'ambiguous', 'candidates': sorted(candidates)})
    result, budget = [], PROMPT_CHARS
    for path in sorted(chosen):
        item = archive[path]
        pages, next_read, expected = [], None, 0
        for offset, page in sorted(item['pages'].items()):
            if offset != expected:
                break
            value = page['text']
            shown = value[:max(0, budget)]
            pages.append(shown)
            budget -= len(shown)
            if len(shown) < len(value):
                next_read = 'READ ' + shlex.quote(path) + ' ' + str(offset + len(shown))
                break
            next_read = page['next_read']
            if not next_read:
                break
            expected = int(shlex.split(next_read)[-1])
        if not pages:
            next_read = 'READ ' + shlex.quote(path) + ' 0'
        result.append({'source': path, 'title': item['title'], 'text': ''.join(pages),
                       'complete': not bool(next_read), 'next_read': next_read,
                       'scope': '历史题面原文；旧路径和输出格式不是本题执行目标。旧代码通过不证明所有规则已验证。'})
    result += unresolved
    result += [{'source': p, 'status': 'not_read', 'next_read': 'READ ' + shlex.quote(p) + ' 0'} for p in missing]
    return result, missing


def redact(text):
    """Remove explicit credential literals from historical prompt material."""
    text = re.sub(r'(?i)(Bearer\s+)[^\s\x22\x27`]+', r'\1<读取当前凭据>', str(text))
    return re.sub(r'''(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[\x22\x27]?\s*[:=]\s*[\x22\x27]?)[^\s\x22\x27`,}]+''',
                  r'\1<读取当前凭据>', text)


def solution_record(mem):
    """A source/code pair, promoted only after the existing completion signal."""
    import hashlib
    from .task_skills import shape
    pages = [{k: redact(v) if k == 'output' else v for k, v in d.items()}
             for d in mem.documents if d.get('kind') != 'list']
    code = redact(mem.answer_python)
    record = {'id': hashlib.sha256((mem.task_text + code).encode()).hexdigest()[:16],
            'point': mem.task_point, 'shape': shape(mem.contract.get('example')),
            'family': mem.contract.get('family'), 'source': mem.contract.get('source'),
            'question': redact(mem.task_text), 'documents': pages,
            'python': code, 'evidence': 'legal_submission_then_task_disappeared',
            'verification': mem.last_attempt.get('http', {}).get('checker', {}).get('passed', False),
            'successes': 1, 'failures': 0, 'disabled': False,
            'interfaces': list(mem.method_calls),
            'scope': '旧题提交后观察到任务完成，不证明当前题算法正确。重新读取当前参数和凭据；必须重新查询，不要直接执行旧代码。'}
    if mem.recipe_candidate:
        from .task_skills import learned_method
        legacy = learned_method(mem.task_point, mem.contract, mem.recipe_candidate, mem.method_calls)
        record.update(id=legacy['id'], recipe=legacy['recipe'], python=legacy['recipe']['python'])
    return record


def solution_hints(mem, referenced):
    sources = {r.get('source') for r in referenced}
    candidates = []
    for item in mem.skills:
        if item.get('disabled'):
            continue
        explicit = item.get('source') in sources
        same_stream = item.get('point') == mem.task_point
        if explicit or same_stream:
            candidates.append((explicit, same_stream, item))
    candidates.sort(key=lambda x: x[:2], reverse=True)
    result, budget = [], 16000
    for _, _, item in candidates[:3]:
        value = {k: item[k] for k in ('id', 'question', 'documents', 'python', 'recipe', 'interfaces', 'evidence', 'verification', 'scope') if k in item}
        import json
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) <= budget:
            result.append(value)
            budget -= len(encoded)
        else:
            result.append({'id': item['id'], 'source': item.get('source'),
                           'omitted': '历史方法超过上下文预算；先补读被引用题面，不使用残缺程序。'})
    return result
