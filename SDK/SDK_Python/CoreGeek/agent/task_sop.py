"""Task contracts and a narrow deployment SOP for the official offline sandbox.

The host only inspects text and builds commands. Even the deterministic repair
and checker run in executeCmd, never in the HTTP process.
"""
import json
from pathlib import PurePosixPath
import re


def answer_contract(documents):
    """Only submission examples, never API response examples, define the shape."""
    for document in documents:
        if document.get("kind") not in ("read", "discover"):
            continue
        text = document.get("output", "")
        section = re.search(r"(?:^|\n)#{1,4}\s*(?:提交规则|提交格式|答案格式|答案输出格式)[^\n]*\n"
                            r"(.*?)(?=\n#{1,4} |\Z)", text, re.S)
        if not section:
            continue
        samples = re.findall(r"```(?:json)?\s*\n(.*?)\n```", section[1], re.S)
        for sample in samples:
            try:
                example = json.loads(sample)
            except ValueError:
                continue
            if not isinstance(example, dict) or not example:
                continue
            contract = {"example": example, "source": document.get("resolved_path", document.get("path", ""))}
            # This family is established by the real task in log091601.txt.
            # Rebind its workspace from this task's document on every visit.
            if set(example) == {"token"} and "./check" in text and "spec.md" in text and "TOKEN:" in text:
                contract["kind"] = "check_token"
                cd = re.search(r"`cd\s+([^`\n]+)`", text)
                source = document.get("resolved_path", "")
                if cd and source.startswith("/"):
                    parent = PurePosixPath(source).parent
                    workspace = PurePosixPath(cd[1].strip())
                    if not workspace.is_absolute():
                        workspace = parent / workspace
                    if ".." not in workspace.parts and workspace.is_relative_to(parent):
                        contract["workspace"] = str(workspace)
            return contract
    return {}


def answer_error(answer, contract):
    """Permit partial field sets for credit, but reject extras and wrong shapes."""
    if not contract:
        return ""
    try:
        value = json.loads(answer)
    except ValueError:
        return "提交规则要求 JSON 对象；文档正文、解释和代码都不是答案。"

    def compatible(actual, example):
        if isinstance(example, dict):
            return (isinstance(actual, dict) and bool(actual) and not actual.keys() - example.keys()
                    and all(compatible(v, example[k]) for k, v in actual.items()))
        if isinstance(example, list):
            return isinstance(actual, list)  # An example does not specify list length/items.
        if example is None:
            return True
        if type(example) in (int, float):
            return type(actual) in (int, float)
        return type(actual) is type(example)

    if not compatible(value, contract["example"]):
        return "答案的键或类型不符合提交格式；仅提交已求得的规定字段，禁止额外包装 task/result。"
    return ""


def engineering_code(workspace, repair=True):
    """Generate a repeatable spec-driven repair, followed by authoritative check."""
    return f"WORKSPACE = {workspace!r}\nREPAIR = {repair!r}\n" + _ENGINEERING_SCRIPT


_ENGINEERING_SCRIPT = r'''
import json
import os
from pathlib import Path
import re
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
try:
    with tempfile.TemporaryFile() as output:
        result = subprocess.run([str(base / 'check')], cwd=base, stdout=output,
                                stderr=subprocess.STDOUT, timeout=8)
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
