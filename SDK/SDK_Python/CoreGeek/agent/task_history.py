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
