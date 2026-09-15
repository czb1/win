#!/usr/bin/env python3
"""Compatibility entry: every SDK launcher uses the same competition agent."""
from pathlib import Path
import runpy
import sys

_entry_dir = Path(__file__).resolve().parent / "SDK_Python" / "CoreGeek"
sys.path.insert(0, str(_entry_dir))
_entry = runpy.run_path(str(_entry_dir / "main3.py"), run_name="_coregeek_entry")
callback = _entry["callback"]
main = _entry["main"]

if __name__ == "__main__":
    main()
