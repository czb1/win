"""Small deterministic file tools executed only by the official task sandbox."""
import json
import re
import shlex


def parse_file_tool(text):
    match = re.fullmatch(r"(READ|LIST)\s*[:：]?\s+(.+)", text.strip(), re.I | re.S)
    if not match:
        return None
    try:
        args = shlex.split(match[2])
    except ValueError:
        return None
    kind = match[1].lower()
    start = 0
    if kind == "read" and len(args) == 2 and args[-1].isdigit():
        start = int(args.pop())
    if len(args) != 1 or not 0 <= start <= 1000000 or len(args[0]) > 500 or "\x00" in args[0]:
        return None
    return {kind: args[0], "start": start}


def document_path(task):
    # Only explicit local documentation paths from this task; no guessed API.
    for match in re.finditer(r"(?<![\w/])[A-Za-z0-9_./-]+\.(?:md|txt|rst)(?![\w/])", task):
        path = match[0]
        prefix = task[max(0, match.start() - 8):match.start()]
        if ":" not in prefix and "//" not in path and ".." not in path.split("/"):
            return path
    return None


def document_code(path, start=0):
    """Read an explicit document, locating a relative task file once.

    Task sandboxes use per-task working directories that can change between
    visits to the same task point.  A bare filename from phaseTask therefore
    cannot safely inherit yesterday's absolute /tmp path.  Keep discovery
    deterministic and bounded, and prefer the current working directory when
    the judger already placed the file there.
    """
    if not isinstance(path, str) or not path or len(path) > 500 or "\x00" in path:
        raise ValueError("READ requires one path")
    if type(start) is not int or not 0 <= start <= 1000000:
        raise ValueError("invalid file offset/path")
    if path.startswith("/"):
        return file_code("read", path, start)
    config = repr(json.dumps({"path": path, "start": start}, ensure_ascii=False))
    return ("import os, json\n"
            f"cfg = json.loads({config})\n"
            "requested = cfg['path']\n"
            "matches = []\n"
            "if os.path.isfile(requested):\n"
            "    matches.append(os.path.abspath(requested))\n"
            "root = '/tmp/selfEvolutionTask'\n"
            "wanted = os.path.basename(requested)\n"
            "suffix = requested.replace('\\\\', '/').strip('/')\n"
            "if not matches and os.path.isdir(root):\n"
            "    seen = 0\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))[:40]\n"
            "        seen += len(dirs) + len(files)\n"
            "        if wanted in files:\n"
            "            candidate = os.path.join(base, wanted)\n"
            "            if candidate.replace('\\\\', '/').endswith(suffix):\n"
            "                matches.append(candidate)\n"
            "        if seen >= 2000 or len(matches) >= 20:\n"
            "            break\n"
            "if len(matches) != 1:\n"
            "    raise FileNotFoundError('expected one task document for %r, found %d' % (requested, len(matches)))\n"
            "resolved = matches[0]\n"
            "print('RESOLVED_DOCUMENT', resolved, 'OFFSET', cfg['start'])\n"
            "with open(resolved, encoding='utf-8', errors='replace') as f:\n"
            "    f.read(cfg['start'])\n"
            "    page = f.read(6000)\n"
            "    print(page)\n"
            "    if f.read(1):\n"
            "        print('NEXT_READ', repr(resolved), cfg['start'] + len(page))\n")


def file_code(kind, path, start=0):
    """Bound output and read sequentially, so large documents do not time out."""
    if kind not in ("read", "list") or not isinstance(path, str) or not path or len(path) > 500:
        raise ValueError("READ/LIST requires one path")
    if type(start) is not int or not 0 <= start <= 1000000 or "\x00" in path:
        raise ValueError("invalid file offset/path")
    config = repr(json.dumps({"path": path, "start": start}, ensure_ascii=False))
    if kind == "list":
        return ("import os, json\n"
                f"p = json.loads({config})['path']\n"
                "print('DIRECTORY', p)\n"
                "with os.scandir(p) as entries:\n"
                "    for i, entry in enumerate(entries):\n"
                "        if i >= 80:\n"
                "            print('More entries omitted; LIST a specific subdirectory.'); break\n"
                "        if not entry.name.startswith('.'):\n"
                "            print(entry.name + ('/' if entry.is_dir(follow_symlinks=False) else ''))\n")
    return ("import json\n"
            f"cfg = json.loads({config})\n"
            "print('DOCUMENT', cfg['path'], 'OFFSET', cfg['start'])\n"
            "with open(cfg['path'], encoding='utf-8', errors='replace') as f:\n"
            "    f.read(cfg['start'])\n"
            "    page = f.read(6000)\n"
            "    print(page)\n"
            "    if f.read(1):\n"
            "        print('NEXT_READ', repr(cfg['path']), cfg['start'] + len(page))\n")
