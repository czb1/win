"""Host-side answer evidence and sandbox-only log diagnostics."""
import json
import math
import re
from .task_sop import answer_error


def result_object(text, contract):
    """Exactly one finite JSON object; no diagnostics, duplicate keys or tokens."""
    if not contract.get('example') or contract.get('kind') == 'check_token':
        return None
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        def finite(item):
            if isinstance(item, dict):
                return all(finite(v) for v in item.values())
            if isinstance(item, list):
                return all(finite(v) for v in item)
            return not isinstance(item, float) or math.isfinite(item)
        if not isinstance(value, dict) or not finite(value) or answer_error(text, contract):
            return None
        return json.dumps(value, ensure_ascii=False)
    except (ValueError, TypeError):
        return None


def computed_answer(body, contract):
    body = body.strip()
    if body.startswith('FINAL_ANSWER\n'):
        body = body.partition('\n')[2]
    return result_object(body, contract)


def evidence_matches(answer, output, contract):
    supported = computed_answer(output, contract)
    proposed = result_object(answer, contract)
    if supported is None or proposed is None:
        return False
    # Permit a partial submission only if every supplied leaf has evidence.
    def subset(value, source):
        if isinstance(value, dict):
            return isinstance(source, dict) and all(k in source and subset(v, source[k]) for k, v in value.items())
        return type(value) is type(source) and value == source
    return subset(json.loads(proposed), json.loads(supported))


def parsing_rules(documents):
    """Retain explicit rule lines, not record samples or current file paths."""
    lines = []
    for document in documents:
        for line in document.get('output', '').splitlines():
            if (len(line) < 350 and not re.search(r'/tmp/|https?://|\d{4}-\d\d-\d\d', line)
                    and re.search(r'故障|失败|ERROR|5xx|OOM|格式|解析', line)
                    and line not in lines):
                lines.append(line)
    return '\n'.join(lines)[:2000]


# Injected only for log tasks. No model code or local task file runs on the host.
LOG_AUDIT = r'''
import builtins
import io
import functools
from pathlib import Path

log_report = report.setdefault('logs', {'files': {}, 'parsers': {}, 'events': {}, 'errors': []})
def log_error(kind, detail):
    if len(log_report['errors']) < 8:
        log_report['errors'].append({'kind': kind, 'detail': str(detail)[:240]})

class LogReader:
    def __init__(self, stream, path):
        self.stream = stream
        self.entry = log_report['files'].setdefault(path, {'read_lines': 0, 'complete': False})
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self.stream.__exit__(*args)
    def __iter__(self):
        return self
    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line
    def eof(self):
        return self.stream.tell() == os.fstat(self.stream.fileno()).st_size
    def readline(self, *args):
        line = self.stream.readline(*args)
        if line.strip():
            self.entry['read_lines'] += 1
        if not line or self.eof():
            self.entry['complete'] = True
        return line
    def read(self, *args):
        value = self.stream.read(*args)
        self.entry['read_lines'] += sum(bool(line.strip()) for line in value.splitlines())
        if not args or args[0] == -1 or not value or self.eof():
            self.entry['complete'] = True
        return value
    def readlines(self, hint=-1):
        return list(self) if hint <= 0 else self._limited_lines(hint)
    def _limited_lines(self, hint):
        result, size = [], 0
        for line in self:
            result.append(line)
            size += len(line)
            if size >= hint:
                break
        return result

old_open, old_io_open = builtins.open, io.open
def audited_open(original):
    def opened(file, mode='r', *args, **kwargs):
        try:
            is_log = os.fspath(file).endswith('.log') and not any(c in mode for c in 'wax+')
        except TypeError:
            is_log = False
        try:
            stream = original(file, mode, *args, **kwargs)
        except OSError as error:
            if is_log:
                log_error('file', error)
            raise
        return LogReader(stream, os.path.abspath(file)) if is_log else stream
    return opened
builtins.open, io.open = audited_open(old_open), audited_open(old_io_open)

def audited_function(function):
    @functools.wraps(function)
    def observed(*args, **kwargs):
        name = function.__name__
        entry = log_report['parsers'].setdefault(name, {'calls': 0, 'returned_records': 0})
        entry['calls'] += 1
        try:
            value = function(*args, **kwargs)
        except Exception as error:
            log_error('parser', type(error).__name__ + ': ' + str(error))
            raise
        if name == 'parse_line':
            system = str(args[0] if args else kwargs.get('system', 'unknown'))[:80]
            systems = report.setdefault('parse_systems', {})
            counts = systems.setdefault(system, {'total': 0, 'matched': 0, 'failures': 0, 'unmatched': []})
            counts['total'] += 1
            if value is not None and value is not False:
                counts['matched'] += 1
                if isinstance(value, (tuple, list)) and len(value) >= 3:
                    counts['failures'] += bool(value[2])
            else:
                if len(counts['unmatched']) < 3:
                    counts['unmatched'].append(str(args[-1] if args else kwargs.get('line', ''))[:240])
        elif isinstance(value, (list, tuple)):
            entry['returned_records'] += len(value)
        return value
    return observed

def task_events(system, timestamps, gap_minutes=5):
    """Generic adjacent-gap grouping; fault definitions come from current rules."""
    timestamps = sorted(timestamps)
    events = []
    for timestamp in timestamps:
        if not events or (timestamp - events[-1][1]).total_seconds() > gap_minutes * 60:
            events.append([timestamp, timestamp])
        else:
            events[-1][1] = timestamp
    log_report['events'][str(system)[:80]] = {
        'failures': len(timestamps), 'count': len(events),
        'samples': [[str(a), str(b)] for a, b in events[:3]]}
    return events

def audit_merge(function):
    @functools.wraps(function)
    def observed(*args, **kwargs):
        value = function(*args, **kwargs)
        if isinstance(value, (list, tuple)):
            calls = log_report.setdefault('merge_calls', [])
            if len(calls) < 12:
                calls.append({'function': function.__name__, 'events': len(value),
                              'records': len(args[0]) if args and isinstance(args[0], (list, tuple)) else None})
        return value
    return observed

def task_parse(system, path):
    function = learned_scope.get('parse_' + system + '_log')
    if function:
        return function(path)
    function = learned_scope.get('parse_line')
    if function:
        with open(path, encoding='utf-8') as source:
            return [record for line in source if line.strip()
                    for record in [function(system, line)] if record is not None]
    raise ValueError('no verified parser for ' + system)
'''
