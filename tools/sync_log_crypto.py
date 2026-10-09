#!/usr/bin/env python3
"""Embed the canonical standard-library codec in the downloadable extractor."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
START = "# BEGIN EMBEDDED LOG CRYPTO\n"
END = "# END EMBEDDED LOG CRYPTO\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    source = (ROOT / "SDK/SDK_Python/CoreGeek/agent/log_crypto.py").read_text(encoding="utf-8").rstrip() + "\n"
    path = ROOT / "tools/extract_logs.py"
    content = path.read_text(encoding="utf-8")
    before, rest = content.split(START, 1)
    actual, after = rest.split(END, 1)
    if args.check:
        if actual != source:
            print("Embedded crypto differs; run python tools/sync_log_crypto.py", file=sys.stderr)
            return 1
    else:
        path.write_text(before + START + source + END + after, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
