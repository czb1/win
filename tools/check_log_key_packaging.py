#!/usr/bin/env python3
"""Reject FWLOG private keys inside the uploaded application directory."""
import argparse
import json
from pathlib import Path
import sys


def private_keys(root):
    for path in Path(root).rglob("*.json"):
        try:
            if path.stat().st_size > 16384:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("format") == "fwlog-rsa-v1" and value.get("kind") == "private":
            yield path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    found = list(private_keys(args.root))
    if found:
        print("Refusing to package FWLOG private key files: " + ", ".join(str(p) for p in found), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
