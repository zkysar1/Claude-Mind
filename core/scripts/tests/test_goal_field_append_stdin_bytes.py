""" outcome 2: goal-field-append sends a composed value to the store
wrapper as UTF-8 BYTES, so Windows text mode cannot add a CR to each newline.

Measured on ZDS 2026-09-28: through the store wrapper, a value with 30
newlines written on text-mode stdin was stored with 30 CRs. Text mode rewrites
'\\n' only on Windows and dev is POSIX, so these tests pin the call SHAPE
(bytes on stdin) rather than reproduce the platform behaviour.
"""
import ast
import importlib.util
import os
import pathlib
import subprocess
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# guard-955: pin the backend BEFORE importing anything that resolves one.
os.environ["STORAGE_BACKEND"] = "local"

_SPEC = importlib.util.spec_from_file_location(
    "goal_field_append", _SCRIPTS / "goal-field-append.py")
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_stdin_value_goes_as_utf8_bytes_and_the_reply_comes_back_as_text(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(
            argv, 0, b'{"ok": true}\r\n', "warn — x\r\n".encode("utf-8"))

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    value = "line one\nline two — dash\n" * 15
    res = mod._run(["x.sh"], input=value)
    assert seen["input"] == value.encode("utf-8")
    assert not seen.get("text") and "encoding" not in seen
    assert res.returncode == 0
    # Decoded the way text mode would decode it: universal newlines, so the
    # callers' JSON parsing and stderr checks see what they saw before.
    assert res.stdout == '{"ok": true}\n' and res.stderr == "warn — x\n"


def test_a_call_without_stdin_is_unchanged(monkeypatch):
    """The read path (read_goal) sends no input and keeps plain text mode."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "out\n", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    res = mod._run(["x.sh"])
    assert seen.get("text") is True and "input" not in seen
    assert res.stdout == "out\n"


def test_no_stdin_write_bypasses_run():
    """A subprocess.run elsewhere with a text-mode input= would bring the CRs back."""
    tree = ast.parse((_SCRIPTS / "goal-field-append.py").read_text(encoding="utf-8"))
    parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    owners = []
    for node in ast.walk(tree):
        f = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(f, ast.Attribute) and f.attr == "run"
                and isinstance(f.value, ast.Name) and f.value.id == "subprocess"
                and any(k.arg == "input" for k in node.keywords)):
            p = parent.get(node)
            while p is not None and not isinstance(p, ast.FunctionDef):
                p = parent.get(p)
            owners.append(p.name if p else "<module>")
    assert owners and set(owners) == {"_run"}, owners
