#!/usr/bin/env python3
"""Minimal pytest-free runner for tests/test_core.py (stdlib only)."""

import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tests.test_core as t  # noqa: E402


def main():
    tests = [(name, fn) for name, fn in vars(t).items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in sorted(tests):
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
