#!/usr/bin/env python3
"""Quiet offline validation. Full coverage by default; quick is iteration only."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import sys
from time import monotonic
import unittest

ROOT = Path(__file__).resolve().parents[1]


def replay_test(method):
    """Mark a simulation without changing standard unittest discovery."""
    method.is_replay = True
    return method


def select_tests(suite, quick=False):
    selected = unittest.TestSuite()
    omitted = 0
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            child, count = select_tests(test, quick)
            selected.addTests(child)
            omitted += count
        elif quick and getattr(getattr(test, test._testMethodName), "is_replay", False):
            omitted += 1
        else:
            selected.addTest(test)
    return selected, omitted


def run_units(suite, log):
    # Preserve all diagnostics on disk, including prints from successful replays.
    with redirect_stdout(log), redirect_stderr(log):
        return unittest.TextTestRunner(stream=log, verbosity=2).run(suite)


def failure_excerpt(path):
    with path.open(encoding="utf-8") as log:
        from collections import deque
        return "".join(deque(log, maxlen=60))[-6000:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="Omit marked replays; never a final acceptance check")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "artifacts/validation")
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    mode = "quick" if args.quick else "full"
    log_path = args.log_dir / (mode + ".log")
    started = monotonic()
    loader = unittest.TestLoader()
    suite, omitted = select_tests(loader.discover(str(ROOT / "tests")), args.quick)
    with log_path.open("w", encoding="utf-8") as log:
        result = run_units(suite, log)
        success = result.wasSuccessful() and result.testsRun > 0
        benchmark = "not run"
        if success and not args.quick:
            # Construction and progression are already asserted by the suite.
            # Keep the independent static-combat/economy benchmark from CI.
            log.flush()
            process = subprocess.run(
                [sys.executable, str(ROOT / "tools/strategy_benchmark.py")],
                cwd=ROOT, stdout=log, stderr=log, check=False,
            )
            success = process.returncode == 0
            benchmark = "PASS" if success else "FAIL"
    status = "PASS" if success else "FAIL"
    print(f"{status} {mode}: tests={result.testsRun}, failures={len(result.failures)}, "
          f"errors={len(result.errors)}, skipped={len(result.skipped)}, "
          f"replays_omitted={omitted}, benchmark={benchmark}, "
          f"seconds={monotonic() - started:.2f}")
    print(f"Log: {log_path}")
    if args.quick:
        print("Iteration only: run without --quick before final delivery.")
    if not success:
        print(failure_excerpt(log_path))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
