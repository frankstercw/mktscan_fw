"""
tools_run_tests_offline.py
──────────────────────────────────────────────────────────────────────────────
Run the test suite without pytest installed.

Provides just enough of the pytest API (`approx`, `mark.parametrize`,
`importorskip`, `raises`, `skip`) to execute `tests/test_options_pipeline.py`
unmodified in an environment with no package index access.

This is a convenience for constrained environments — **not** a pytest
replacement. Where pytest is available, use it:

    pytest tests/ -v

Usage:

    python tools_run_tests_offline.py                       # default module
    python tools_run_tests_offline.py tests.test_scrapers   # a specific module

Exits non-zero if any test fails, so it works in CI as a fallback.
"""
import importlib
import os
import sys
import traceback
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── Minimal pytest shim ───────────────────────────────────────────────────────

class _Skip(Exception):
    """Raised to skip a test — mirrors pytest.skip / importorskip."""


class _Approx:
    """Tolerant float comparison, mirroring pytest.approx."""

    def __init__(self, expected, rel=1e-6, abs_=1e-9):
        self.expected, self.rel, self.abs = expected, rel, abs_

    def __eq__(self, actual):
        if self.expected is None or actual is None:
            return actual is self.expected
        return abs(actual - self.expected) <= max(self.abs, self.rel * abs(self.expected))

    def __repr__(self):
        return f"approx({self.expected})"


class _Mark:
    @staticmethod
    def parametrize(argnames, argvalues):
        names = [a.strip() for a in argnames.split(",")]

        def deco(fn):
            fn._parametrize = (names, argvalues)
            return fn
        return deco

    def __getattr__(self, _name):
        # Accept and ignore any other marker (skipif, xfail, ...).
        def deco(fn=None, **_kw):
            return fn if fn is not None else (lambda f: f)
        return deco


class _Raises:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"expected {self.exc.__name__} to be raised")
        return issubclass(exc_type, self.exc)


def _importorskip(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        raise _Skip(f"{name} not installed")


pytest = types.ModuleType("pytest")
pytest.approx = _Approx
pytest.mark = _Mark()
pytest.raises = _Raises
pytest.importorskip = _importorskip
pytest.skip = lambda msg="": (_ for _ in ()).throw(_Skip(msg))
sys.modules["pytest"] = pytest


# ── Runner ────────────────────────────────────────────────────────────────────

def run(module_name: str) -> int:
    """Execute every Test* class in the module. Returns the failure count."""
    module = importlib.import_module(module_name)
    passed = failed = skipped = 0
    failures = []

    for cls_name in sorted(dir(module)):
        if not cls_name.startswith("Test"):
            continue
        cls = getattr(module, cls_name)
        if not isinstance(cls, type):
            continue

        print(f"\n\033[1m{cls_name}\033[0m")
        for meth_name in sorted(vars(cls)):
            if not meth_name.startswith("test_"):
                continue

            method = getattr(cls, meth_name)
            is_static = isinstance(vars(cls)[meth_name], staticmethod)

            cases = [{}]
            if hasattr(method, "_parametrize"):
                names, values = method._parametrize
                cases = [
                    dict(zip(names, v if isinstance(v, (tuple, list)) else (v,)))
                    for v in values
                ]

            for kwargs in cases:
                label = meth_name + (f"[{list(kwargs.values())}]" if kwargs else "")
                try:
                    instance = cls()
                    fn = method if is_static else getattr(instance, meth_name)
                    fn(**kwargs)
                    passed += 1
                    print(f"  \033[32m✓\033[0m {label}")
                except _Skip as e:
                    skipped += 1
                    print(f"  \033[33m⊘\033[0m {label}  ({e})")
                except Exception:
                    failed += 1
                    failures.append((f"{cls_name}::{label}", traceback.format_exc()))
                    print(f"  \033[31m✗\033[0m {label}")

    print("\n" + "=" * 70)
    for name, tb in failures:
        print(f"\n\033[31mFAILED {name}\033[0m\n{tb}")
    print(f"\033[1m{passed} passed, {failed} failed, {skipped} skipped\033[0m")
    return failed


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "tests.test_options_pipeline"
    sys.exit(1 if run(target) else 0)
