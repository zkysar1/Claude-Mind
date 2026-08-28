"""test_compact_checkpoint_box_identity.py —  regression.

A compaction resume summary once carried a PARTNER's hostname (cc-02 on a cc-05
box) and it reached 5 durable records before anyone ran `hostname`. The class is
not a knowledge gap — the governing rule and signature both existed and were
retrievable — it is a CHOKEPOINT gap: nothing measured reached the moment of the
write, so summary prose was the only box identity present at restore.

These tests pin the two halves of the chokepoint:
  1. precompact-checkpoint stamps a MEASURED machine_id (never inherited prose).
  2. compact-restore surfaces it, on EVERY restore path, fail-open when absent.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

os.environ.setdefault("MIND_AGENT", "echo")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_box_identity_is_measured_not_inherited():
    """The stamp must come from a live syscall, not from any stored string."""
    import socket

    pcc = _load("pcc_bi", "precompact-checkpoint.py")
    ident = pcc._box_identity()

    assert set(ident) == {"machine_id", "platform_uname"}, ident
    # It must equal what THIS box reports right now — the whole point is that a
    # value from another box's narrative cannot survive this comparison.
    expected = os.environ.get("MACHINE_ID", "").strip() or socket.gethostname()
    assert ident["machine_id"] == expected, (
        f"stamp {ident['machine_id']!r} != measured {expected!r} — the stamp is "
        "not being measured on this box (g-115-4550)")
    assert ident["machine_id"] != "unknown"


def test_box_identity_reuses_the_single_resolver():
    """No second gethostname implementation — one answer to 'which box is this'."""
    import _session_telemetry

    pcc = _load("pcc_bi2", "precompact-checkpoint.py")
    assert pcc._box_identity()["machine_id"] == _session_telemetry._machine_id()


def test_box_identity_is_fail_open():
    """An identity failure must never cost a checkpoint write."""
    pcc = _load("pcc_bi3", "precompact-checkpoint.py")
    real = sys.modules.pop("_session_telemetry", None)

    class _Boom:
        def __getattr__(self, n):
            raise RuntimeError("resolver exploded")

    sys.modules["_session_telemetry"] = _Boom()
    try:
        ident = pcc._box_identity()
        assert ident["machine_id"] == "unknown", ident
    finally:
        sys.modules.pop("_session_telemetry", None)
        if real is not None:
            sys.modules["_session_telemetry"] = real


def test_restore_surfaces_the_stamp_and_tolerates_its_absence():
    """The surface prints when stamped, and is silent (not fatal) when not.

    SHAPE ONLY. This exercises a LOCAL COPY of the emitter, so it cannot see
    whether the shipped one runs -- and it did not: the real block referenced
    `checkpoint`, unbound in main(), and the copy below bound it as a
    PARAMETER. The copy passed while production was silently dead
    (guard-3803 / rb-9581). The binding check below is what actually guards it.
    """
    src = (CORE_SCRIPTS / "compact-restore-slots.py").read_text(encoding="utf-8")
    assert "box identity (MEASURED at checkpoint write" in src, (
        "restore no longer surfaces the measured stamp (g-115-4550)")

    def emit(checkpoint):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                _box = checkpoint.get("box_identity") or {}
                if _box.get("machine_id"):
                    print(
                        "box identity (MEASURED at checkpoint write, not from "
                        f"summary prose): machine_id={_box.get('machine_id')} "
                        f"uname={_box.get('platform_uname', 'unknown')}")
            except Exception:
                pass
        return buf.getvalue()

    out = emit({"box_identity": {"machine_id": "cc-03",
                                 "platform_uname": "Linux 6.8.0-137-generic"}})
    assert "machine_id=cc-03" in out and "MEASURED" in out, out

    assert emit({}) == ""                                  # pre-stamp checkpoint
    assert emit({"box_identity": None}) == ""              # null stamp
    assert emit({"box_identity": {"machine_id": ""}}) == ""  # empty stamp


def test_surface_names_are_bound_in_their_enclosing_function():
    """Every name the box-identity block LOADS must be bound where it runs.

    The defect this exists for: the block sat inside main() at line 325 and
    read `checkpoint`, which main() does not assign until line 369. Because
    the name IS assigned somewhere in the function, Python treats it as a
    LOCAL throughout and raises UnboundLocalError at the earlier use -- not
    NameError, and not a lookup of the same-named PARAMETER on
    _describe_discarded_checkpoint(), which was the first (wrong) reading.
    The block's own `except Exception: pass` swallowed it, so the surface was
    dead from the moment it shipped.

    That is why this check is ORDER-AWARE rather than a membership test: a
    set-of-bound-names check passes against the real defect, because the name
    is genuinely bound -- just later. Line order is not scope (rb-9581), and a
    fail-open handler also covers bugs in the code it wraps (guard-3803).
    """
    import ast
    src = (CORE_SCRIPTS / "compact-restore-slots.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            module_names.add(node.name)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in ast.walk(node):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    module_names.add(t.id)

    import builtins
    safe = module_names | set(dir(builtins))

    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert fn is not None, "main() not found in compact-restore-slots.py"

    # Record the EARLIEST line each local name is bound at. Order matters:
    # a name assigned LATER in the same function is UnboundLocalError at an
    # earlier line, not a resolvable global -- which is exactly how the
    # shipped defect read `checkpoint` (assigned ~50 lines below the surface).
    bound = {a.arg: -1 for a in fn.args.args}

    def _bind(name, lineno):
        if name not in bound or lineno < bound[name]:
            bound[name] = lineno

    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            _bind(n.id, n.lineno)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                _bind((a.asname or a.name).split(".")[0], n.lineno)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            _bind(n.name, n.lineno)

    # Isolate the box-identity block: the Try whose body prints the surface.
    target = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Try) and "box identity (MEASURED" in ast.dump(n):
            target = n
            break
    assert target is not None, (
        "box-identity surface block not found inside main() -- if it moved, "
        "move this check with it (g-115-4550)")

    bad = set()
    for n in ast.walk(target):
        if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
            continue
        if n.id in bound:
            # A local: legal only if bound at or before this use.
            if bound[n.id] > n.lineno:
                bad.add(f"{n.id} (local, first bound at line {bound[n.id]}, "
                        f"used at {n.lineno})")
        elif n.id not in safe:
            bad.add(f"{n.id} (not bound anywhere in scope)")

    assert not bad, (
        "box-identity surface loads name(s) unresolvable where it runs: "
        f"{sorted(bad)} -- the surrounding except swallows the "
        "NameError/UnboundLocalError, so this is silent in production "
        "(guard-3803 / rb-9581)")


if __name__ == "__main__":
    for fn in (test_box_identity_is_measured_not_inherited,
               test_box_identity_reuses_the_single_resolver,
               test_box_identity_is_fail_open,
               test_restore_surfaces_the_stamp_and_tolerates_its_absence,
               test_surface_names_are_bound_in_their_enclosing_function):
        fn()
        print(f"  [PASS] {fn.__name__}")
    print("\nAll cases passed.")
