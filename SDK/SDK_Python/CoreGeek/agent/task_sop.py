"""Source-backed submission hints; legacy deployment utility is not auto-routed.

The host only inspects text and builds commands; execution stays in executeCmd.
"""
import json
from pathlib import PurePosixPath
import re


def answer_contract(documents):
    """Extract source-backed submission hints, never a business task category.

    Examples describe examples, not a closed schema. Validation steps belong
    to the task document and model program, not a host-side task category.
    """
    for document in documents:
        if document.get('kind') not in ('read', 'discover'):
            continue
        text = document.get('output', '').partition('\nTASK_INPUTS ')[0]
        section = re.search(r'(?:^|\n)#{1,4}\s*(?:提交规则|提交格式|提交形式|答案格式|答案输出格式)[^\n]*\n'
                            r'(.*?)(?=\n#{1,4} |\Z)', text, re.S)
        if not section and not re.search(r'提交', text):
            continue
        source = document.get('resolved_path') or document.get('path', '')
        contract = {'source': source, 'submission_text': section[1] if section else text, 'example_only': True}
        for sample in re.findall(r'^[ \t]*```(?:json)?[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*\r?$', section[1] if section else text, re.S | re.M | re.I):
            try:
                contract['example'] = json.loads(sample)
                contract['format'] = 'json'
                break
            except ValueError:
                continue
        if source.startswith('/'):
            name = PurePosixPath(source)
            contract['family'] = name.parent.name + '/' + re.sub(r'\d+', '#', name.name)
            contract['workspace'] = str(name.parent)
        cd = re.search(r'`cd\s+([^`\n]+)`', text)
        if cd and contract.get('workspace'):
            workspace = PurePosixPath(cd[1].strip())
            if not workspace.is_absolute():
                workspace = PurePosixPath(contract['workspace']) / workspace
            if '..' not in workspace.parts and workspace.is_relative_to(PurePosixPath(source).parent):
                contract['workspace'] = str(workspace)
            else:
                contract.pop('workspace', None)
        return contract
    return {}


def answer_error(answer, contract):
    if not contract or contract.get('format') != 'json':
        return ''
    try:
        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        value = json.loads(answer, object_pairs_hook=object_pairs,
                   parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        return '提交要求 JSON；请提供合法 JSON，不能包含解释、重复键或非有限数值。'
    return ''


def engineering_code(workspace, repair=True, checker='check', spec_required=True):
    """Generate a repeatable spec-driven repair, followed by authoritative check."""
    if not re.fullmatch(r'check(?:_[A-Za-z0-9_-]+)?', checker):
        raise ValueError('unsupported checker name')
    return (f"WORKSPACE = {workspace!r}\nREPAIR = {repair!r}\n"
            f"CHECKER = {checker!r}\nSPEC_REQUIRED = {spec_required!r}\n" + _ENGINEERING_SCRIPT)


_ENGINEERING_SCRIPT = r'''
import json
import errno
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

base = Path(WORKSPACE).resolve(strict=True)

def local(name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('unsupported path: ' + name)
    target = base / path
    if target.is_symlink() or not target.resolve().is_relative_to(base):
        raise ValueError('path leaves current workspace: ' + name)
    if target.resolve() in (base, base / 'check', base / 'spec.md'):
        raise ValueError('cannot change task specification/checker: ' + name)
    return target

spec_path = base / 'spec.md'
spec = ''
if SPEC_REQUIRED:
    with spec_path.open(encoding='utf-8') as source:
        spec = source.read(16001)
issue = ''
try:
    if len(spec) > 16000:
        raise ValueError('spec too long for fixed SOP; read remaining requirements')
    if REPAIR:
        operations = []
        config = None
        for raw in spec.splitlines():
            line = raw.strip()
            if not line or line.startswith('# '):
                continue
            if line in ('## 目录要求', '## 脚本要求'):
                config = None
                continue
            header = re.fullmatch(r'## 配置文件\s+`?([^`\s]+)`?', line)
            if header:
                config = local(header[1])
                continue
            directory = re.fullmatch(r'-\s+`?([^`\s]+/)`?\s*必须存在[，,]\s*权限为\s*`?([0-7]{3})`?', line)
            setting = re.fullmatch(r'-\s+第\s*(\d+)\s*行[：:]\s*`([^`\r\n]*)`', line)
            script = re.fullmatch(r'-\s+`?([^`\s]+)`?\s*必须存在且可执行[（(]权限\s*`?([0-7]{3})`?[）)]', line)
            if directory:
                operations.append(('directory', local(directory[1]), int(directory[2], 8)))
            elif setting and config is not None and 1 <= int(setting[1]) <= 10000:
                operations.append(('line', config, (int(setting[1]), setting[2])))
            elif script:
                operations.append(('mode', local(script[1]), int(script[2], 8)))
            else:
                raise ValueError('unsupported spec requirement: ' + line)
        if not operations:
            raise ValueError('no supported requirements')
        # Validate the entire spec and all inputs before changing any files.
        configs = {}
        for kind, target, value in operations:
            if kind in ('line', 'mode') and not target.is_file():
                raise ValueError('required file missing; contents must not be invented: ' + str(target))
            if kind == 'directory' and target.exists() and not target.is_dir():
                raise ValueError('directory path occupied: ' + str(target))
            if kind == 'line':
                if target not in configs:
                    with target.open(encoding='utf-8') as source:
                        content = source.read(65537)
                    if len(content) > 65536:
                        raise ValueError('config too long: ' + str(target))
                    configs[target] = content.splitlines()
                index, content = value
                lines = configs[target]
                if len(lines) < index:
                    lines.extend([''] * (index - len(lines)))
                lines[index - 1] = content
        for target, lines in configs.items():
            target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        for kind, target, value in operations:
            if kind == 'directory':
                target.mkdir(parents=True, exist_ok=True)
            if kind in ('directory', 'mode'):
                target.chmod(value)
except (ValueError, OSError) as error:
    issue = str(error)

# Capture output separately so diagnostics can never masquerade as an answer.
# A file bounds host memory even if the checker produces excessive output.
def run_check(output):
    checker = base / CHECKER
    try:
        return subprocess.run([str(checker)], cwd=base, stdout=output,
                              stderr=subprocess.STDOUT, timeout=8)
    except OSError as error:
        # ENOENT can mean a missing shebang interpreter, not a missing file.
        # Only fall back for a declared shell script; never rewrite the checker
        # or retry a checker that actually ran and returned a failure.
        if error.errno not in (errno.ENOENT, errno.ENOEXEC, errno.EACCES) or not checker.is_file():
            raise
        with checker.open('rb') as source:
            first = source.readline(256).decode('utf-8', errors='replace').strip()
        words = shlex.split(first[2:]) if first.startswith('#!') else []
        if words and Path(words[0]).name == 'env':
            words = words[1:]
        shell = Path(words[0]).name if words else ''
        interpreter = shutil.which(shell) if shell in ('sh', 'bash') else None
        if not interpreter or any(not arg.startswith('-') for arg in words[1:]):
            raise
        return subprocess.run([interpreter, *words[1:], str(checker)], cwd=base,
                              stdout=output, stderr=subprocess.STDOUT, timeout=8)

try:
    with tempfile.TemporaryFile() as output:
        result = run_check(output)
        output.seek(0)
        raw = output.read(16001)
    text = raw.decode('utf-8', errors='replace')
    tokens = re.findall(r'^TOKEN:[ \t]*(\S+)[ \t]*$', text, re.M)
    if result.returncode == 0 and len(raw) <= 16000 and len(tokens) == 1:
        print('FINAL_ANSWER')
        print(json.dumps({'token': tokens[0]}, ensure_ascii=False))
    else:
        print('SOP_DIAGNOSTIC', issue or 'checker did not produce one successful token')
        print('SPEC', str(spec_path))
        print(spec[:16000])
        print('CHECK_EXIT', result.returncode)
        print(text[:16000])
        raise SystemExit(1)
except (OSError, subprocess.TimeoutExpired) as error:
    print('SOP_DIAGNOSTIC', issue, str(error))
    print('SPEC', str(spec_path))
    print(spec[:16000])
    raise SystemExit(1)
'''
