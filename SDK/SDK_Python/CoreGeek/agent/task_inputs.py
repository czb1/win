"""Bounded input previews, generated on the host and read only in executeCmd."""
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


def zero_without_coverage(answer, contract, report):
    """Zero is valid only after the current log records were actually parsed."""
    if contract.get('input_kind') != 'logs':
        return False
    try:
        value = json.loads(answer)
    except (ValueError, TypeError):
        return False
    numbers = []

    def visit(item):
        if type(item) in (int, float):
            numbers.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    coverage = report.get('parse_lines', {})
    return bool(numbers) and not any(numbers) and not coverage.get('matched', 0)


_PREVIEW = r'''
import json
import os
from pathlib import Path
import re

def preview_inputs():
    base = Path(PREVIEW_DOCUMENT).resolve().parent
    context = {'directory': str(base), 'entries': [], 'files': []}
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
    for path in dict.fromkeys(candidates):
        if len(context['files']) >= 6:
            break
        if not path.is_file() or not path.resolve().is_relative_to(base):
            continue
        entry = {'path': str(path.relative_to(base)), 'bytes': path.stat().st_size}
        if path.suffix == '.json':
            with path.open(encoding='utf-8', errors='replace') as source:
                text = source.read(65537)
            if len(text) > 65536:
                entry['preview'] = text[:600]
                entry['next_read'] = str(path)
            else:
                try:
                    value = json.loads(text)
                    entry['type'] = type(value).__name__
                    entry['keys'] = list(value)[:20] if isinstance(value, dict) else []
                    sequence = value.get('cases') if isinstance(value, dict) else value
                    entry['records'] = len(sequence) if isinstance(sequence, list) else None
                    sample = sequence[:1] if isinstance(sequence, list) else value
                    encoded = json.dumps(sample, ensure_ascii=False)
                    entry['sample'] = encoded[:900]
                    if len(encoded) > 900 or (isinstance(sequence, list) and len(sequence) > 1):
                        entry['next_read'] = str(path)
                except ValueError as error:
                    entry['error'] = str(error)[:160]
        else:
            with path.open(encoding='utf-8', errors='replace') as source:
                entry['sample'] = [source.readline(220).rstrip('\n') for _ in range(3)]
        context['files'].append(entry)
    print('TASK_INPUTS ' + json.dumps(context, ensure_ascii=False))

try:
    preview_inputs()
except (OSError, ValueError) as error:
    print('TASK_INPUTS ' + json.dumps({'error': str(error)[:300]}, ensure_ascii=False))
'''
