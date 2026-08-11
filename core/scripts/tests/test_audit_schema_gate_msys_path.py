""" — audit-schema-gate.sh was inert at every $WORLD_DIR call site on Windows.

Two independent defects are pinned here, and they need DIFFERENT test shapes:

1. PATH RESOLUTION. A caller interpolates "$WORLD_DIR/store.jsonl" into argv.
   On Git Bash that string is MSYS-flavored ("/c/..."), and Windows Python reads
   the leading "/" as absolute-on-the-current-drive, so `Path(...).is_file()`
   returns False for a file that plainly exists. The gate then took its
   fail-open branch and verified NOTHING — silently, at every call site.
   `_platform.sh` cygpath-converts the path ENV VARS, which is why this looked
   fixed; but argv is interpolated by the CALLER before the callee runs, and
   `MSYS_NO_PATHCONV=1` (exported by that same file) specifically stops MSYS
   from rewriting argv. So the callee must normalize its own argument.

2. NO POSITIVE CONTROL. The gate had no test that fed it input it MUST reject.
   A fail-open gate that has never been observed to block is indistinguishable
   from one that CANNOT block — which is exactly the state defect 1 put it in
   for an unknown length of time. The positive controls below are the standing
   answer to that, and they assert on the PAYLOAD, not merely the exit code
   (guard-1627: a positive control read only through its exit status can be
   satisfied by a gate failing for the wrong reason).

WHY THE WINDOWS BRANCH IS TESTED BY INJECTION. The branch that matters executes
only when os.name == "nt", and this fleet's dev and staging boxes are POSIX —
so a platform-gated test would be skipped on every box where a regression is
most likely to be introduced, and would run only where it is least likely to be
noticed. That asymmetry IS the bug (the goal's own words: "dev and staging are
POSIX, prod is Windows — so this gate verifies clean at every upstream stage and
is dead only at the destination"). The `is_windows` / `exists` seams on
normalize_msys_path exist for this and are load-bearing.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# This file lives at core/scripts/tests/ — the scripts dir is ONE parent up.
SCRIPTS = Path(__file__).resolve().parents[1]
GATE_PY = SCRIPTS / "audit-schema-gate.py"

sys.path.insert(0, str(SCRIPTS))
from _path_helpers import normalize_msys_path  # noqa: E402


def _load_gate():
    """Import audit-schema-gate.py under a module name (its filename is hyphenated)."""
    spec = importlib.util.spec_from_file_location("audit_schema_gate_mod", GATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_gate(*args):
    """Run the gate as a subprocess; return (returncode, parsed stdout JSON)."""
    proc = subprocess.run(
        [sys.executable, str(GATE_PY), *args],
        capture_output=True, text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


# ---------------------------------------------------------------- defect 2:
# POSITIVE CONTROLS — the gate must be observed BLOCKING, not merely passing.

def test_positive_control_absent_field_blocks(tmp_path):
    """Feed the gate a field that is not in the schema. It MUST block.

    This is the control the gate never had. Assert the payload, not just rc:
    a gate that exits 1 for an unrelated reason would pass an rc-only check.
    """
    f = tmp_path / "store.jsonl"
    f.write_text(json.dumps({"id": "x", "utilization": {"times_active": 3}}) + "\n")

    rc, out = _run_gate("--jsonl-path", str(f), "--field-names", "times_triggered")

    assert rc == 1, "gate did not block on an absent field — it cannot block"
    assert out["would_block"] is True
    assert out["fields_missing"] == ["times_triggered"]
    assert out["fields_found"] == []
    assert out["records_sampled"] == 1, "blocked without actually reading a record"


def test_positive_control_always_null_field_blocks(tmp_path):
    """An always-null field must block too — the rb-245 shape that started this.

    `times_triggered` existed as a key and was never written. Treating a
    present-but-null key as "found" is what produced the original false
    "98% zero utilization" conclusion, so this pins _get_dotted's documented
    `cur is None -> False` semantic through the gate's public surface.
    """
    f = tmp_path / "store.jsonl"
    f.write_text(json.dumps({"id": "x", "utilization": {"times_active": None}}) + "\n")

    rc, out = _run_gate("--jsonl-path", str(f), "--field-names", "utilization.times_active")

    assert rc == 1
    assert out["would_block"] is True
    assert out["fields_missing"] == ["utilization.times_active"]


def test_negative_control_present_field_passes(tmp_path):
    """The complement: a real dotted field passes. Without this, a gate that
    blocked unconditionally would satisfy the positive controls above."""
    f = tmp_path / "store.jsonl"
    f.write_text(json.dumps({"id": "x", "utilization": {"times_active": 3}}) + "\n")

    rc, out = _run_gate("--jsonl-path", str(f), "--field-names", "utilization.times_active")

    assert rc == 0
    assert out["would_block"] is False
    assert out["fields_found"] == ["utilization.times_active"]
    assert out["records_sampled"] == 1


def test_missing_file_still_fails_open(tmp_path):
    """Fail-open on a genuinely missing file is DELIBERATE and must survive.

    The fix must not convert defect 1 into a fail-closed gate that wedges
    every caller whose store does not exist yet.
    """
    rc, out = _run_gate(
        "--jsonl-path", str(tmp_path / "nope.jsonl"), "--field-names", "anything"
    )
    assert rc == 0
    assert out["would_block"] is False
    assert "fail-open" in out["reason"]


# ---------------------------------------------------------------- defect 1:
# PATH NORMALIZATION — unit-level, with the host injected.

def test_msys_path_converted_on_windows_host(tmp_path):
    """/c/W/x -> C:/W/x when the converted form exists. The core fix."""
    seen = {}

    def fake_exists(p):
        seen["asked"] = p
        return True

    out = normalize_msys_path("/c/World/store.jsonl", is_windows=True, exists=fake_exists)
    assert out == "C:/World/store.jsonl"
    assert seen["asked"] == "C:/World/store.jsonl"


def test_msys_path_untouched_when_converted_form_absent():
    """A REAL Windows /c/... path (meaning C:\\c\\...) must never be clobbered.

    This is the safety property that makes the conversion unconditionally safe
    to apply: it only fires when it demonstrably resolves to something.
    """
    out = normalize_msys_path("/c/genuine", is_windows=True, exists=lambda p: False)
    assert out == "/c/genuine"


def test_posix_host_never_rewrites():
    """On POSIX, /c/foo is an ordinary absolute path. Rewriting it here would
    turn this defense into a new bug — so the host gate is not optional."""
    called = []
    out = normalize_msys_path(
        "/c/World/store.jsonl", is_windows=False, exists=lambda p: called.append(p) or True
    )
    assert out == "/c/World/store.jsonl"
    assert called == [], "existence was probed on a POSIX host — host gate ran too late"


@pytest.mark.parametrize(
    "value",
    [
        "/home/user/store.jsonl",       # multi-char first segment
        "/cygdrive/c/World/store.jsonl",  # cygwin form — out of measured scope
        "C:/World/store.jsonl",          # already Windows
        "relative/store.jsonl",          # not absolute
        "",                              # degenerate
    ],
)
def test_non_msys_shapes_pass_through_unchanged(value):
    """Only a single-letter first segment is the MSYS drive shape."""
    assert normalize_msys_path(value, is_windows=True, exists=lambda p: True) == value


def test_drive_letter_is_upcased():
    assert normalize_msys_path("/d/x", is_windows=True, exists=lambda p: True) == "D:/x"


def test_bare_drive_root():
    assert normalize_msys_path("/c", is_windows=True, exists=lambda p: True) == "C:/"


# ---------------------------------------------------------------- wiring:
# the gate must actually ROUTE its argv through the normalizer.

def test_gate_routes_argv_through_normalizer(tmp_path, monkeypatch):
    """Mutation-proofing for the wiring itself.

    The unit tests above prove the normalizer is correct; this proves the gate
    CALLS it. Without this, reverting the one-line call site in the gate would
    leave every test above green while the gate stayed exactly as inert as it
    was in prod — the failure mode this whole file exists to prevent.
    """
    f = tmp_path / "store.jsonl"
    f.write_text(json.dumps({"a": 1}) + "\n")

    mod = _load_gate()
    calls = []

    def spy(value, **kw):
        calls.append(value)
        return value

    monkeypatch.setattr(mod, "normalize_msys_path", spy)
    monkeypatch.setattr(sys, "argv", [
        "audit-schema-gate.py", "--jsonl-path", str(f), "--field-names", "a",
    ])
    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 0
    assert calls == [str(f)], "gate did not pass its --jsonl-path through normalize_msys_path"
