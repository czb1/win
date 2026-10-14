"""Bounded source-text previews, read only in executeCmd without reshaping data."""
import json
import re


def preview_code(path_expression, text_expression):
    return (f"PREVIEW_DOCUMENT = {path_expression}\nPREVIEW_TEXT = {text_expression}\n"
            + _PREVIEW)


def input_context(body):
    match = re.search(r"(?m)^TASK_INPUTS (.+)$", body)
    if not match:
        return {}
    try:
        value = json.loads(match[1])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


_PREVIEW = r'''
import json
import os
from pathlib import Path
import re
import shlex

# Count encoded transport bytes, including escaped source newlines and quotes.
# Leave room for the document, metadata and platform headers below 64KB.
PREVIEW_BYTES = 24000

def wire_size(context):
    return len(('TASK_INPUTS ' + json.dumps(context, ensure_ascii=False) + '\n').encode('utf-8'))

def source_page(entry, path, text, complete):
    entry.update(content=text, start=0, end=len(text), complete=complete)
    if complete:
        entry.pop('next_read', None)
    else:
        entry['next_read'] = 'READ ' + shlex.quote(str(path)) + ' ' + str(len(text))

def json_metadata(entry, text):
    # Compatibility metadata for existing opt-in tools, outside the raw content.
    # Never inject metadata into source objects or serialize them as a sample.
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return
    entry['type'] = type(value).__name__
    sequence = value.get('cases') if isinstance(value, dict) else value
    entry['records'] = len(sequence) if isinstance(sequence, list) else None
    if isinstance(sequence, list) and sequence and all(
            isinstance(c, dict) and 'input' in c and len(set(c) & {'expected', 'output'}) == 1
            for c in sequence):
        entry['case_count'] = len(sequence)

def preview_inputs():
    base = Path(PREVIEW_DOCUMENT).resolve().parent
    context = {'directory': str(base), 'entries': [], 'files': [], 'preview_only': True,
               'scope': 'content 是源文件原文或连续前缀，外层字段是元数据；complete=false 时按 next_read 补读。文件发现有数量上限。'}
    with os.scandir(base) as entries:
        for index, entry in enumerate(entries):
            if index >= 60:
                context['entries_truncated'] = True
                break
            if not entry.name.startswith('.'):
                context['entries'].append(entry.name + ('/' if entry.is_dir() else ''))
    context['entries'].sort()
    # Inspect only paths explicitly named in this document, under its directory.
    references = re.findall(r'(?<![A-Za-z0-9_./-])[A-Za-z0-9_./*-]+\.(?:json|log|csv|tsv)(?![A-Za-z0-9_./-])', PREVIEW_TEXT)
    directories = re.findall(r'(?<![A-Za-z0-9_./-])[A-Za-z0-9_./-]+/(?=[`\s（(])', PREVIEW_TEXT)
    candidates = []
    for name in dict.fromkeys(references + directories):
        if len(candidates) >= 12:
            break
        path = Path(name)
        if '..' in path.parts:
            continue
        path = path if path.is_absolute() else base / path
        if not path.resolve().is_relative_to(base):
            continue
        if '*' in name:
            if path.parent.is_dir():
                candidates.extend(sorted(path.parent.glob(path.name))[:3])
        elif path.is_dir():
            candidates.extend(sorted(p for p in path.iterdir()
                                     if p.suffix in ('.json', '.log', '.csv', '.tsv'))[:3])
        else:
            candidates.append(path)
    pages = []
    for path in dict.fromkeys(candidates):
        if len(context['files']) >= 6:
            break
        if not path.is_file() or not path.resolve().is_relative_to(base):
            continue
        entry = {'path': str(path.relative_to(base)), 'bytes': path.stat().st_size}
        source_page(entry, path, '', False)
        text, complete = '', False
        try:
            with path.open(encoding='utf-8', newline='') as source:
                text = source.read(65537)
                complete = not source.read(1)
            if path.suffix == '.json' and complete:
                json_metadata(entry, text)
        except (OSError, UnicodeError) as error:
            entry['error'] = str(error)[:300]
        context['files'].append(entry)
        pages.append((path, text, complete))
    # Reserve all file locations before spending the budget on source text.
    while context['entries'] and wire_size(context) > PREVIEW_BYTES:
        context['entries'].pop()
        context['entries_truncated'] = True
    while context['files'] and wire_size(context) > PREVIEW_BYTES:
        context['files'].pop()
        pages.pop()
        context['files_truncated'] = True
    for entry, (path, text, complete) in zip(context['files'], pages):
        source_page(entry, path, text, complete)
        if wire_size(context) <= PREVIEW_BYTES:
            continue
        lower, upper = 0, len(text)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            source_page(entry, path, text[:middle], False)
            if wire_size(context) <= PREVIEW_BYTES:
                lower = middle
            else:
                upper = middle - 1
        source_page(entry, path, text[:lower], False)
    print('TASK_INPUTS ' + json.dumps(context, ensure_ascii=False))

try:
    preview_inputs()
except (OSError, ValueError, UnicodeError) as error:
    print('TASK_INPUTS ' + json.dumps({'error': str(error)[:300]}, ensure_ascii=False))
'''
