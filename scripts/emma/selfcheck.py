#!/usr/bin/env python3
"""Run the first-party test modules without any third-party dependencies.

The build environment for this migration has no virtualenv, so ``pytest`` is not
available while the runtime is being extracted. This runner executes the same
``test_*`` functions using only the standard library, with a minimal stand-in for
the two pytest features those tests use (``raises`` and the ``monkeypatch``
fixture). It is a development aid, not a replacement for the pytest suite:

    PYTHONPATH=src python3 scripts/emma/selfcheck.py
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests" / "emma"

MODULES = (
    "test_cost_policy",
    "test_budget",
    "test_data_policy",
    "test_redaction",
    "test_config",
)


class _Raises:
    """Stand-in for ``pytest.raises``."""

    def __init__(self, expected: type[BaseException]) -> None:
        self._expected = expected
        self.value: BaseException | None = None

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            raise AssertionError(f"expected {self._expected.__name__}, nothing was raised")
        if not issubclass(exc_type, self._expected):
            return False
        self.value = exc
        return True


class _MonkeyPatch:
    """Stand-in for the ``monkeypatch`` fixture, limited to the environment."""

    def __init__(self) -> None:
        self._saved: dict[str, str | None] = {}

    def setenv(self, name: str, value: str) -> None:
        self._saved.setdefault(name, os.environ.get(name))
        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True) -> None:
        self._saved.setdefault(name, os.environ.get(name))
        if name in os.environ:
            del os.environ[name]
        elif raising:
            raise KeyError(name)

    def undo(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._saved.clear()


def _install_pytest_shim() -> None:
    """Provide the small slice of the pytest API the tests use."""
    if "pytest" in sys.modules:
        return
    shim = types.ModuleType("pytest")
    shim.raises = lambda expected, *a, **k: _Raises(expected)  # type: ignore[attr-defined]
    shim.fixture = lambda *a, **k: (lambda fn: fn)  # type: ignore[attr-defined]
    shim.mark = types.SimpleNamespace(skip=lambda *a, **k: None)  # type: ignore[attr-defined]
    sys.modules["pytest"] = shim


def main() -> int:
    _install_pytest_shim()
    sys.path.insert(0, str(REPO_ROOT / "src"))
    sys.path.insert(0, str(TESTS_DIR))

    passed = failed = 0
    failures: list[str] = []

    for module_name in MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            failed += 1
            failures.append(f"{module_name}: import failed\n{traceback.format_exc()}")
            continue
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            test = getattr(module, name)
            if not callable(test):
                continue
            patch = _MonkeyPatch()
            kwargs = {}
            if "monkeypatch" in test.__code__.co_varnames[: test.__code__.co_argcount]:
                kwargs["monkeypatch"] = patch
            try:
                test(**kwargs)
            except Exception:
                failed += 1
                failures.append(f"{module_name}::{name}\n{traceback.format_exc()}")
            else:
                passed += 1
            finally:
                patch.undo()

    for failure in failures:
        print(f"FAIL {failure}")
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
