#!/usr/bin/env python3
"""Embed the canonical codec and compact log views in the standalone extractor."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = {"LOG CRYPTO": "log_crypto.py", "LOG DISPLAY": "log_display.py"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    path = ROOT / "tools/extract_logs.py"
    content = path.read_text(encoding="utf-8")
    for label, filename in MODULES.items():
        source = (ROOT / "SDK/SDK_Python/CoreGeek/agent" / filename).read_text(encoding="utf-8").rstrip() + "\n"
        start, end = f"# BEGIN EMBEDDED {label}\n", f"# END EMBEDDED {label}\n"
        before, rest = content.split(start, 1)
        actual, after = rest.split(end, 1)
        if args.check and actual != source:
            print(f"Embedded {label.lower()} differs; run python tools/sync_log_crypto.py", file=sys.stderr)
            return 1
        content = before + start + source + end + after
    if not args.check:
        path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
