"""Minimal test runner (pytest isn't available in the build sandbox). Usage: python -m tests.run [module ...]"""
import asyncio
import importlib
import inspect
import pkgutil
import sys
import traceback

import tests


def main(argv):
    mods = argv or [m.name for m in pkgutil.iter_modules(tests.__path__) if m.name.startswith("test_")]
    failed = passed = 0
    for name in mods:
        mod = importlib.import_module(f"tests.{name}")
        for fname, fn in inspect.getmembers(mod, inspect.isfunction):
            if not fname.startswith("test_") or fn.__module__ != mod.__name__:
                continue
            try:
                if inspect.iscoroutinefunction(fn):
                    asyncio.run(fn())
                else:
                    fn()
                passed += 1
                print(f"  ok   {name}.{fname}")
            except Exception:
                failed += 1
                print(f"  FAIL {name}.{fname}")
                traceback.print_exc(limit=3)
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
