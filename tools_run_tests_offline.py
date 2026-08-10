"""
Minimal pytest shim + runner.

The sandbox has no PyPI access, so this provides just enough of the pytest API
(approx, mark.parametrize, importorskip, skip) to execute the real test module
unmodified. Not a pytest replacement — only a way to verify the suite here.
"""
import importlib
import sys
import traceback
import types

ROOT = "/sessions/clever-intelligent-goodall/mnt/outputs/mktscan/mktscan-main"
sys.path.insert(0, ROOT)


# ── pytest shim ───────────────────────────────────────────────────────────────
class _Skip(Exception):
    pass


class _Approx:
    def __init__(self, expected, rel=1e-6, abs_=1e-9):
        self.expected, self.rel, self.abs = expected, rel, abs_

    def __eq__(self, actual):
        if self.expected is None or actual is None:
            return actual is self.expected
        tol = max(self.abs, self.rel * abs(self.expected))
        return abs(actual - self.expected) <= tol

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
        def deco(fn=None, **_kw):
            return fn if fn is not None else (lambda f: f)
        return deco


pytest = types.ModuleType("pytest")
pytest.approx = _Approx
pytest.mark = _Mark()
pytest.skip = lambda msg="": (_ for _ in ()).throw(_Skip(msg))


def _importorskip(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        raise _Skip(f"{name} not installed")


pytest.importorskip = _importorskip


class _Raises:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError(f"expected {self.exc.__name__}")
        return issubclass(et, self.exc)


pytest.raises = _Raises
sys.modules["pytest"] = pytest


# ── runner ────────────────────────────────────────────────────────────────────
def run(module_name):
    mod = importlib.import_module(module_name)
    passed = failed = skipped = 0
    failures = []

    for cls_name in sorted(dir(mod)):
        if not cls_name.startswith("Test"):
            continue
        cls = getattr(mod, cls_name)
        if not isinstance(cls, type):
            continue

        print(f"\n\033[1m{cls_name}\033[0m")
        for meth_name in sorted(vars(cls)):
            if not meth_name.startswith("test_"):
                continue
            meth = getattr(cls, meth_name)
            raw = vars(cls)[meth_name]
            is_static = isinstance(raw, staticmethod)

            cases = [((), {})]
            if hasattr(meth, "_parametrize"):
                names, values = meth._parametrize
                cases = []
                for v in values:
                    v = v if isinstance(v, (tuple, list)) else (v,)
                    cases.append(((), dict(zip(names, v))))

            for args, kwargs in cases:
                label = meth_name + (f"[{list(kwargs.values())}]" if kwargs else "")
                try:
                    inst = cls()
                    fn = meth if is_static else getattr(inst, meth_name)
                    fn(*args, **kwargs)
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
    sys.exit(1 if run(sys.argv[1] if len(sys.argv) > 1 else "tests.test_options_pipeline") else 0)
