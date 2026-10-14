"""Build model execution wrappers; execution stays in the official sandbox."""
import json
import re


def command_output(raw):
    """Remove only the optional platform duration header; keep status/output strict."""
    header, sep, body = raw.partition("\n")
    first, newline, rest = body.partition("\n")
    if re.fullmatch(r"\[durationMs:[0-9]+\]", first):
        body = rest if newline else ""
    return header + sep + body


def runtime_code(code, directory=None, rounding=None, log_task=False, parser=None, contract=None):
    return (f"TASK_CODE = {code!r}\nTASK_DIRECTORY = {directory!r}\n"
            + _RUNTIME)


def runtime_result(raw):
    """Remove wrapper metadata before applying the strict final-answer parser."""
    if len(raw.encode('utf-8')) > 65536:
        return raw, {}
    raw = command_output(raw)
    header, _, body = raw.partition("\n")
    first, sep, output = body.partition("\n")
    if not sep or not first.startswith("TASK_RUNTIME "):
        return raw, {}
    try:
        report = json.loads(first[len("TASK_RUNTIME "):])
    except ValueError:
        return raw, {}
    if not isinstance(report, dict):
        return raw, {}
    return header + "\n" + output, report


_RUNTIME = r'''
import contextlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import time
import traceback
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit, parse_qs, urlencode

report = {'http_successes': 0, 'http_errors': [], 'http_calls': [], 'json_shapes': []}
request_errors = {}

def remember_call(method, url, headers=None, params=None):
    address = urlsplit(str(url))
    if address.scheme not in ('http', 'https') or not address.hostname:
        return
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    authorization = headers.get('authorization', '')
    # Only protocol names survive; no credentials, query values or response data.
    auth = ('Bearer' if authorization.lower().startswith('bearer ') else
            'Basic' if authorization.lower().startswith('basic ') else
            'X-API-Key' if 'x-api-key' in headers else 'none')
    keys = set(parse_qs(address.query))
    if isinstance(params, dict):
        keys.update(str(k) for k in params)
    endpoint = address.scheme + '://' + address.hostname
    if address.port:
        endpoint += ':' + str(address.port)
    call = {'method': str(method).upper(), 'endpoint': endpoint + address.path,
            'auth': auth, 'parameters': sorted(keys)}
    if call not in report['http_calls'] and len(report['http_calls']) < 12:
        report['http_calls'].append(call)


def failure(url, detail):
    # Full URL is used only as an internal retry identity, never emitted.
    request_errors[str(url)] = {'path': urlsplit(str(url)).path[:240], 'error': str(detail)[:1200]}

def recovered(url):
    request_errors.pop(str(url), None)

def payload_error(url, value):
    # Business payloads can legitimately contain success=false/status=failed.
    # Keep structure as evidence; do not turn domain data into transport errors.
    return

def json_shape(value, depth=0):
    """Return bounded structural metadata without scalar response values."""
    if depth >= 4:
        return {'type': type(value).__name__}
    if isinstance(value, dict):
        raw_keys = list(value.keys())[:20]
        shape = {'type': 'dict', 'keys': [str(k)[:120] for k in raw_keys]}
        children = {}
        for key in raw_keys:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                children[str(key)[:120]] = json_shape(child, depth + 1)
        if children:
            shape['children'] = children
        return shape
    if isinstance(value, list):
        shape = {'type': 'list', 'length': len(value)}
        item_types = []
        for item in value[:8]:
            name = type(item).__name__
            if name not in item_types:
                item_types.append(name)
        if item_types:
            shape['item_types'] = item_types
        if value:
            shape['item'] = json_shape(value[0], depth + 1)
        return shape
    return {'type': type(value).__name__}

def remember_json_shape(url, value):
    path = urlsplit(str(url)).path[:240]
    entry = {'path': path, 'shape': json_shape(value)}
    if entry not in report['json_shapes'] and len(report['json_shapes']) < 4:
        report['json_shapes'].append(entry)

original_urlopen = urllib.request.urlopen
def checked_urlopen(url, *args, **kwargs):
    address = getattr(url, 'full_url', url)
    if len(args) < 2:
        timeout = kwargs.get('timeout', 8)
        kwargs['timeout'] = 8 if timeout is None else min(float(timeout), 8)
    try:
        response = original_urlopen(url, *args, **kwargs)
    except urllib.error.HTTPError as error:
        # Leave the response body available to the caller for diagnosis.
        failure(address, 'HTTP ' + str(error.code))
        raise
    except (OSError, urllib.error.URLError) as error:
        failure(address, type(error).__name__)
        raise
    if str(address).startswith(('http://', 'https://')):
        report['http_successes'] += 1
        recovered(address)
        remember_call(getattr(url, 'get_method', lambda: 'GET')(), address,
                      dict(url.header_items()) if hasattr(url, 'header_items') else {})
    return response
urllib.request.urlopen = checked_urlopen

# requests is present in the official sandbox, but is not a host dependency.
try:
    import requests
except ImportError:
    pass
else:
    original_request = requests.sessions.Session.request
    original_json = requests.models.Response.json

    def checked_request(session, method, url, *args, **kwargs):
        timeout = kwargs.get('timeout', 8)
        if timeout is None:
            timeout = 8
        kwargs['timeout'] = (tuple(min(float(x), 8) for x in timeout)
                             if isinstance(timeout, tuple) else min(float(timeout), 8))
        identity_url = str(url)
        params = kwargs.get('params')
        if params:
            identity_url += ('&' if '?' in identity_url else '?') + (urlencode(params, doseq=True) if not isinstance(params, str) else params)
        try:
            response = original_request(session, method, url, *args, **kwargs)
        except requests.exceptions.RequestException as error:
            failure(identity_url, type(error).__name__)
            raise
        if not 200 <= response.status_code < 300:
            failure(identity_url, 'HTTP %s: %s' % (response.status_code, response.text[:1000]))
        else:
            report['http_successes'] += 1
            recovered(identity_url)
            remember_call(method, url, kwargs.get('headers') or getattr(session, 'headers', {}),
                          kwargs.get('params'))
        response._task_identity_url = identity_url
        return response

    def checked_json(response, *args, **kwargs):
        try:
            value = original_json(response, *args, **kwargs)
        except ValueError:
            failure(getattr(response, '_task_identity_url', response.url), 'invalid JSON response')
            raise
        remember_json_shape(response.url, value)
        payload_error(response.url, value)
        return value

    requests.sessions.Session.request = checked_request
    requests.models.Response.json = checked_json


class TaskDiagnostics:
    """Best-effort file evidence, not a business checker or security boundary."""
    def __init__(self, directory):
        self.active = self.busy = False
        self.root = self.saved = None
        self.records, self.notes = [], []
        self.reads, self.writes = set(), set()
        self.spent = self.total = self.inline = 0
        self.before = {}
        try:
            if not directory:
                self.note('taskDirectory unknown; automatic file collection unavailable')
                return
            self.root = os.path.realpath(directory)
            if not os.path.isdir(self.root):
                self.root = None
                self.note('taskDirectory is not a directory')
                return
            self.before = self.inventory()
            sys.addaudithook(self.observe)
            self.active = True
        except Exception as error:
            self.note(type(error).__name__ + ': ' + str(error))

    def note(self, text):
        text = str(text)[:400]
        if text not in self.notes and len(self.notes) < 12:
            self.notes.append(text)

    def available(self):
        if self.spent >= 0.75:
            self.note('collection time limit reached; evidence is incomplete')
            return False
        return True

    def local(self, name):
        if not isinstance(name, (str, bytes, os.PathLike)) or not self.root:
            return None
        path = os.path.realpath(os.fsdecode(name))
        if os.path.commonpath([self.root, path]) != self.root:
            return None
        parts = os.path.relpath(path, self.root).split(os.sep)
        if '__pycache__' in parts or any(p.startswith('.task-evidence-') for p in parts):
            return None
        return path

    def inventory(self):
        """Stat at most 256 entries, without following directory symlinks."""
        result, pending, count = {}, [self.root], 0
        started = time.monotonic()
        try:
            while pending:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        count += 1
                        if count > 256 or time.monotonic() - started > 0.15 or not self.available():
                            self.note('directory scan limit reached; change inventory is incomplete')
                            return result
                        if entry.name == '__pycache__' or entry.name.startswith('.task-evidence-'):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                pending.append(entry.path)
                            elif stat.S_ISREG(info.st_mode):
                                result[entry.path] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
                        except OSError as error:
                            self.note(str(error))
        except OSError as error:
            self.note(str(error))
        finally:
            self.spent += time.monotonic() - started
        return result

    def save(self, data, entry, complete):
        if not self.saved:
            self.saved = tempfile.mkdtemp(prefix='.task-evidence-', dir=self.root)
        path = os.path.join(self.saved, '%03d.txt' % len(self.records))
        with open(path, 'wb') as target:
            target.write(data)
        self.total += len(data)
        entry.update(snapshot_path=path, snapshot_bytes=len(data), captured_complete=complete,
                     shown_complete=False, read_command='READ ' + shlex.quote(path) + ' 0')
        try:
            text = data.decode('utf-8')
            if '\x00' in text:
                raise ValueError('binary content')
            entry['encoding'] = 'utf-8'
            size = len(json.dumps(text, ensure_ascii=False).encode('utf-8'))
            if entry['kind'] != 'execution_output' and complete and self.inline + size <= 12000:
                entry.update(content=text, shown_complete=True)
                self.inline += size
        except (UnicodeError, ValueError):
            entry['encoding'] = 'binary; snapshot preserves bytes, READ is text only'

    def capture(self, path, kind):
        entry = {'source_path': path, 'kind': kind, 'captured_complete': False, 'shown_complete': False}
        if len(self.records) >= 64:
            self.note('64 snapshot limit reached; evidence is incomplete')
            return
        self.records.append(entry)
        if not self.available() or self.total >= 4194304:
            entry['omitted'] = 'collection time or total byte limit reached'
            return
        started = time.monotonic()
        try:
            # Never wait on a FIFO/device or follow a final-component symlink.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    entry['omitted'] = 'not a regular file'
                    return
                entry['source_bytes'] = info.st_size
                limit = min(1048576, 4194304 - self.total)
                data = source.read(limit + 1)
                after = os.fstat(source.fileno())
            complete = len(data) <= limit and (info.st_size, info.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            self.save(data[:limit], entry, complete)
            if not complete:
                entry['omitted'] = 'file exceeds byte limit or changed during capture; snapshot is partial'
        except Exception as error:
            entry['error'] = type(error).__name__ + ': ' + str(error)[:400]
        finally:
            self.spent += time.monotonic() - started

    def observe(self, event, args):
        if not self.active or self.busy or event != 'open':
            return
        self.busy = True
        try:
            path = self.local(args[0])
            if not path:
                return
            flags = args[2]
            reading = not flags & os.O_WRONLY
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if path not in self.reads and path not in self.writes and len(self.reads | self.writes) >= 64:
                self.note('64 tracked file limit reached; evidence is incomplete')
                return
            if reading and path not in self.reads:
                self.reads.add(path)
                self.capture(path, 'before_read')
            if writing and path not in self.writes:
                if path not in self.reads and os.path.isfile(path):
                    self.capture(path, 'before_write')
                self.writes.add(path)
        except Exception as error:
            # Audit hooks must never make an otherwise valid task open fail.
            self.note(type(error).__name__ + ': ' + str(error))
        finally:
            self.busy = False

    def finish(self, output, reason):
        self.active = False
        if self.root:
            for path in sorted(self.writes):
                self.capture(path, 'after_write')
            after = self.inventory()
            for path in sorted(set(after) | set(self.before)):
                if path not in self.writes and after.get(path) != self.before.get(path):
                    self.capture(path, 'directory_change')
            entry = {'source_path': None, 'kind': 'execution_output',
                     'captured_complete': False, 'shown_complete': False}
            self.records.append(entry)
            started = time.monotonic()
            try:
                if self.available() and self.total < 4194304:
                    output.flush()
                    source = output.buffer
                    source.seek(0)
                    entry['source_bytes'] = os.fstat(source.fileno()).st_size
                    limit = min(1048576, 4194304 - self.total)
                    data = source.read(limit + 1)
                    self.save(data[:limit], entry, len(data) <= limit)
                    if len(data) > limit:
                        entry['omitted'] = 'execution output exceeds byte limit; snapshot is partial'
                else:
                    entry['omitted'] = 'collection time or total byte limit reached'
            except Exception as error:
                entry['error'] = type(error).__name__ + ': ' + str(error)[:400]
            finally:
                self.spent += time.monotonic() - started
        detail = {'reason': reason, 'directory': self.root, 'files': self.records, 'notes': self.notes,
                  'scope': 'Observed task file opens and bounded directory changes; not hidden expected answers or all subprocess reads.'}
        try:
            if self.root:
                if not self.saved:
                    self.saved = tempfile.mkdtemp(prefix='.task-evidence-', dir=self.root)
                manifest = os.path.join(self.saved, 'manifest.json')
                with open(manifest, 'w', encoding='utf-8') as target:
                    json.dump(detail, target, ensure_ascii=False, indent=2)
                detail.update(manifest_path=manifest, read_command='READ ' + shlex.quote(manifest) + ' 0')
        except Exception as error:
            self.note('manifest unavailable: ' + str(error))
        # Keep the entry point usable even when many long paths or fields exist.
        view = {**detail, 'files': [dict(entry) for entry in self.records]}
        if len(json.dumps(view, ensure_ascii=False).encode('utf-8')) > 16000:
            for entry in view['files']:
                entry.pop('content', None)
                entry['shown_complete'] = False
        while len(json.dumps(view, ensure_ascii=False).encode('utf-8')) > 16000 and view['files']:
            view['files'].pop()
            view['files_omitted_from_display'] = view.get('files_omitted_from_display', 0) + 1
        return view

    def cleanup(self):
        self.active = False
        if self.saved:
            try:
                shutil.rmtree(self.saved)
            except Exception:
                pass


answer_value = []
def task_result(text):
    if not isinstance(text, str):
        raise TypeError("task_result 需要最终提交文本；JSON 答案请先调用 json.dumps")
    answer_value[:] = [text]

exit_code = 0
diagnostics = None
with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as output:
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            if TASK_DIRECTORY:
                os.chdir(TASK_DIRECTORY)
            diagnostics = TaskDiagnostics(TASK_DIRECTORY)
            scope = {'__name__': '__main__', 'task_result': task_result}
            exec(compile(TASK_CODE, '<task>', 'exec'), scope)
        except SystemExit as error:
            exit_code = error.code if isinstance(error.code, int) else (1 if error.code else 0)
        except BaseException:
            traceback.print_exc()
            exit_code = 1
    if diagnostics:
        diagnostics.active = False
    output.seek(0)
    body = output.read(60001)
    if diagnostics and (exit_code or not answer_value):
        try:
            report['diagnostics'] = diagnostics.finish(output, 'nonzero_exit' if exit_code else 'no_final_answer')
        except Exception as error:
            report['diagnostics'] = {'error': type(error).__name__ + ': ' + str(error)[:400]}
    elif diagnostics:
        diagnostics.cleanup()
report['http_errors'] = list(request_errors.values())[:4]
if answer_value:
    report['stdout'] = body[:6000]
metadata = 'TASK_RUNTIME ' + json.dumps(report, ensure_ascii=False) + '\n'
# Reserve space for the output/answer and the official status/duration headers.
if len(metadata.encode('utf-8')) > 32000:
    report = {'http_successes': report['http_successes'], 'metadata_truncated': True,
              'diagnostics': report.get('diagnostics', {}),
              'note': 'HTTP metadata exceeds display budget; not a complete HTTP report.'}
    metadata = 'TASK_RUNTIME ' + json.dumps(report, ensure_ascii=False) + '\n'
if answer_value and not exit_code and len(body) <= 60000:
    rendered = 'FINAL_ANSWER\n' + answer_value[0]
else:
    rendered = body[:60000]
room = 60000 - len(metadata.encode('utf-8'))
encoded = rendered.encode('utf-8')
if len(encoded) > room or len(body) > 60000:
    rendered = encoded[:max(0, room - 14)].decode('utf-8', errors='ignore') + '\n[TRUNCATED]\n'
sys.stdout.write(metadata + rendered)
# HTTP diagnostics do not decide whether a handled response is valid task data.
raise SystemExit(exit_code)
'''
