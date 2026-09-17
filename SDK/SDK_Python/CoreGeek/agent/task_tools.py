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
