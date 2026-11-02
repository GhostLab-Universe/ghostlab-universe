"""Export minimized prefixes as standalone runnable regression guards.

:func:`export_guard_py` writes a single pytest file that bootstraps the
repository ``src`` directory, rebuilds the named adapter, replays the
minimized prefix verbatim (with the exact :mod:`minimize` replay semantics:
``ToolError`` outcomes are part of the trace, so replay continues past
them), and asserts the replay shows NO violation. A guard exported from a
still-violating prefix therefore FAILS until the underlying behavior is
repaired; a guard exported from a clean prefix passes.

The guard imports only the repository plus the standard library, takes no
model calls, and carries no secrets: the fixture, prefix and dotted
references are embedded as plain data.
"""

import importlib
import json
from copy import deepcopy
from pathlib import Path

import ghostlab
from ..envs.adapters import split_adapter_ref
from .minimize import check_actions

_GUARD_TEMPLATE = '''"""Generated regression guard: minimized prefix must not violate.

Adapter: {adapter_ref}
Check: {check_fn}
Exported by ghostlab.experiments.regress.export_guard_py. Standalone: run
with ``python -m pytest <this file>``; it bootstraps the repository ``src``.
"""

import importlib
import sys

sys.path.insert(0, {src_literal})

from ghostlab.experiments.minimize import replay_actions

ADAPTER_REF = {adapter_literal}
CHECK_FN = {check_literal}
FIXTURE = {fixture_literal}
PREFIX = {prefix_literal}


def _load_adapter(ref):
    module_name, class_name = ref.split(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls()


def _load_check(dotted):
    module_name, name = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_name), name)


def test_minimized_prefix_has_no_violation():
    adapter = _load_adapter(ADAPTER_REF)
    evidence = replay_actions(adapter, FIXTURE, PREFIX)
    violated = _load_check(CHECK_FN)(evidence)
    assert not violated, (
        "guard failed: the minimized prefix still violates "
        f"{{CHECK_FN}} on {{ADAPTER_REF}}")
'''


def _load_dotted(dotted: str, what: str):
    if type(dotted) is not str or "." not in dotted:
        raise ValueError(f"{what} must be a dotted 'module.Symbol' path")
    module_name, name = dotted.rsplit(".", 1)
    if not module_name or not name:
        raise ValueError(f"{what} must be a dotted 'module.Symbol' path")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ValueError(f"{what} module {module_name!r} cannot be"
                         f" imported: {error}") from error
    target = getattr(module, name, None)
    if target is None:
        raise ValueError(f"{what} {dotted!r} does not exist")
    return target


def export_guard_py(new_adapter_ref_hint: str, fixture: dict,
                    prefix_actions: list, check_fn_dotted: str,
                    path) -> str:
    """Write a standalone pytest guard replaying ``prefix_actions``.

    ``new_adapter_ref_hint`` is ``'module:Class'`` resolved inside the guard
    (validated now: it must import to a class with reset/invoke/evidence).
    ``check_fn_dotted`` is ``'module.function'`` called as
    ``check_fn(evidence) -> bool`` (validated now: it must import to a
    callable). Returns the guard path as a string.
    """
    module_name, class_name = split_adapter_ref(new_adapter_ref_hint)
    try:
        adapter_module = importlib.import_module(module_name)
    except ImportError as error:
        raise ValueError(f"adapter module {module_name!r} cannot be"
                         f" imported: {error}") from error
    cls = getattr(adapter_module, class_name, None)
    if not isinstance(cls, type):
        raise ValueError(f"adapter {new_adapter_ref_hint!r} is not a class")
    missing = [name for name in ("reset", "invoke", "evidence")
               if not callable(getattr(cls, name, None))]
    if missing:
        raise ValueError(f"adapter {new_adapter_ref_hint!r} misses:"
                         f" {', '.join(missing)}")
    check = _load_dotted(check_fn_dotted, "check_fn")
    if not callable(check):
        raise ValueError(f"check_fn {check_fn_dotted!r} is not callable")
    if type(fixture) is not dict:
        raise ValueError("fixture must be an object")
    prefix = check_actions(prefix_actions)
    try:
        json.dumps(fixture)
        json.dumps(prefix)
    except (TypeError, ValueError) as error:
        raise ValueError(f"fixture/prefix must be JSON-serializable:"
                         f" {error}") from error
    try:
        ghostlab_path = Path(ghostlab.__file__).resolve()
    except (AttributeError, OSError) as error:
        raise ValueError(f"cannot locate the ghostlab package: {error}")
    src_dir = str(ghostlab_path.parent.parent)
    guard = _GUARD_TEMPLATE.format(
        adapter_ref=new_adapter_ref_hint, check_fn=check_fn_dotted,
        src_literal=repr(src_dir),
        adapter_literal=repr(new_adapter_ref_hint),
        check_literal=repr(check_fn_dotted),
        fixture_literal=repr(deepcopy(fixture)),
        prefix_literal=repr(prefix))
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(guard, encoding="utf-8")
    return str(out)
