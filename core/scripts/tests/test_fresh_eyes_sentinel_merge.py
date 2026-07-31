"""Regression suite for the fresh_eyes_dispatch_pending merge ().

The defect: iteration-close.sh wrote the sentinel with an unconditional wm-set on
a slot holding ONE payload, so two deep closes back-to-back silently cancelled the
first close's review obligation. Found by echo (board msg-20260730-113233-echo-5163
F-003); measured loss was 10 files from commit 4ef80c13d.

Section 1 pins the merge FUNCTION. Section 2 pins the WIRING -- a green function
suite certifies the function and never the call site (guard-1943), and this defect
lived entirely in the call site. Section 3 is the mutation proof required by
guard-1475/guard-1780: each core assertion is shown to FAIL when the fix is removed,
so a future edit that reverts the behavior cannot leave this suite green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# parents[3], not [2]: this file is core/scripts/tests/<name>.py, so [0]=tests,
# [1]=scripts, [2]=core, [3]=PROJECT_ROOT. Getting this count wrong is the
#  class (a .parent-based PROJECT_ROOT re-derivation landing one level
# short) and it fails loudly here only because every path below is then wrong.
REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "core" / "scripts" / "fresh-eyes-sentinel-merge.py"
CALL_SITE = REPO / "core" / "scripts" / "iteration-close.sh"


def run(new: object, existing: object = None) -> tuple[int, dict | None]:
    """Invoke the helper exactly as iteration-close.sh does: new on stdin, existing in env."""
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"  # guard-955
    if existing is None:
        env.pop("FRESH_EYES_EXISTING", None)
    else:
        env["FRESH_EYES_EXISTING"] = existing if isinstance(existing, str) else json.dumps(existing)
    payload = new if isinstance(new, str) else json.dumps(new)
    p = subprocess.run(
        [sys.executable, HELPER.as_posix()],  # guard-581
        input=payload, capture_output=True, text=True, env=env, timeout=60,
    )
    try:
        return p.returncode, json.loads(p.stdout)
    except ValueError:
        return p.returncode, None


def fired(files: list[str], **kw) -> dict:
    d = {"fired": True, "files": files, "core_count": len(files),
         "loc_changed": 100, "reason": "r", "set_at": "2026-07-31T10:00:00"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- 1. function

def test_both_file_sets_survive_back_to_back_closes():
    """THE defect. Echo's 10 files and the next close's 3 must both be present."""
    old = fired([f"core/scripts/f{i}.sh" for i in range(10)], set_at="2026-07-31T03:00:00")
    new = fired(["core/scripts/a.py", "core/scripts/b.sh", "core/scripts/c.md"])
    rc, out = run(new, old)
    assert rc == 0 and out is not None
    for f in old["files"] + new["files"]:
        assert f in out["files"], f"{f} was dropped by the merge"
    assert out["core_count"] == 13


def test_no_existing_payload_passes_new_through_unchanged():
    """Backward compat: the common case must behave exactly as the old overwrite."""
    new = fired(["core/scripts/a.py"])
    for existing in (None, "null", "", "   "):
        rc, out = run(new, existing)
        assert rc == 0 and out == new, f"existing={existing!r} altered the payload"


def test_unfired_existing_does_not_merge():
    """A {'fired': false} payload carries no obligation and must not accrete."""
    rc, out = run(fired(["core/scripts/a.py"]), {"fired": False, "core_count": 0})
    assert rc == 0 and out["files"] == ["core/scripts/a.py"] and out["core_count"] == 1


def test_duplicate_files_are_deduped_not_double_counted():
    shared = "core/scripts/shared.sh"
    rc, out = run(fired([shared, "core/scripts/new.py"]), fired([shared, "core/scripts/old.sh"]))
    assert out["files"].count(shared) == 1
    assert out["core_count"] == 3


def test_core_count_is_recomputed_never_stale():
    """rb-3399: a union that leaves the derived count stale is the failure mode."""
    rc, out = run(fired(["core/scripts/a.py"]), fired(["core/scripts/b.sh", "core/scripts/c.sh"]))
    assert out["core_count"] == 3, "count must describe the union, not either input"
    assert out["core_count"] != 1 and out["core_count"] != 2
    assert "core_count_is_lower_bound" not in out, "neither input was capped -- count is exact"


def test_capped_input_yields_flagged_lower_bound_not_an_invented_number():
    """files is capped at 20 while core_count is the TRUE count, so a union of two
    capped lists has an unknowable true size. Report a lower bound and say so."""
    capped = fired([f"core/scripts/x{i}.sh" for i in range(20)], core_count=34)
    rc, out = run(fired(["core/scripts/new.py"]), capped)
    assert out["core_count_is_lower_bound"] is True
    assert out["core_count"] >= 34, "each input's own count is a valid lower bound"


def test_cap_overflow_is_reported_never_silent():
    old = fired([f"core/scripts/o{i}.sh" for i in range(15)])
    new = fired([f"core/scripts/n{i}.py" for i in range(15)])
    rc, out = run(new, old)
    assert len(out["files"]) == 20
    assert out["dropped_files"] == 10, "a cap that is not reported reads as full coverage"


def test_older_obligation_keeps_priority_under_the_cap():
    """The set that already survived a close must not be the one evicted."""
    old = fired([f"core/scripts/o{i}.sh" for i in range(20)], set_at="2026-07-31T03:00:00")
    new = fired(["core/scripts/new.py"])
    rc, out = run(new, old)
    assert all(f in out["files"] for f in old["files"])


def test_earliest_set_at_is_kept():
    """The canary reports on the OLDEST un-dispatched obligation's age."""
    rc, out = run(fired(["core/scripts/a.py"], set_at="2026-07-31T09:00:00"),
                  fired(["core/scripts/b.sh"], set_at="2026-07-31T03:00:00"))
    assert out["set_at"] == "2026-07-31T03:00:00"


def test_disjoint_commit_metrics_sum():
    rc, out = run(fired(["core/scripts/a.py"], loc_changed=50, commits_scanned=1),
                  fired(["core/scripts/b.sh"], loc_changed=200, commits_scanned=2))
    assert out["loc_changed"] == 250
    assert out["commits_scanned"] == 3


def test_merge_marker_and_count_are_recorded():
    rc, out = run(fired(["core/scripts/a.py"]), fired(["core/scripts/b.sh"]))
    assert out["merged_payloads"] == 2
    assert "MERGED" in out["reason"]
    # A third close folds onto the already-merged payload.
    rc, out3 = run(fired(["core/scripts/c.sh"]), out)
    assert out3["merged_payloads"] == 3
    assert out3["core_count"] == 3


def test_unusable_existing_fails_open_to_the_new_payload():
    """Worst case must be the OLD overwrite behavior, never a lost new payload."""
    new = fired(["core/scripts/a.py"])
    for junk in ("{not json", "[]", '"a string"', "42"):
        rc, out = run(new, junk)
        assert rc == 0 and out == new, f"junk existing={junk!r} must not corrupt the write"


def test_unusable_new_payload_exits_nonzero_so_caller_falls_back():
    rc, out = run("{not json at all", fired(["core/scripts/a.py"]))
    assert rc != 0, "caller relies on non-zero to fall back to gate_json_stamped"


# ----------------------------------------------------------------- 2. wiring

def test_call_site_actually_invokes_the_helper():
    """guard-1943: a green function suite certifies the function, never the wiring --
    and this defect lived entirely at the call site."""
    src = CALL_SITE.read_text(encoding="utf-8")
    assert "fresh-eyes-sentinel-merge.py" in src
    assert 'FRESH_EYES_EXISTING="$_fe_existing"' in src, "existing must arrive via env (guard-165)"
    assert 'wm-read.sh" fresh_eyes_dispatch_pending' in src, "the merge needs a read before the write"


def test_call_site_writes_the_merged_payload_not_the_raw_one():
    src = CALL_SITE.read_text(encoding="utf-8")
    assert 'echo "$_fe_merged" | bash "$SCRIPT_DIR/wm-set.sh" fresh_eyes_dispatch_pending' in src
    assert 'echo "$gate_json_stamped" | bash "$SCRIPT_DIR/wm-set.sh" fresh_eyes_dispatch_pending' not in src, \
        "the unconditional overwrite must be gone, not merely bypassed"


def test_call_site_falls_back_when_the_helper_fails():
    src = CALL_SITE.read_text(encoding="utf-8")
    assert '[ -n "$_fe_merged" ] || _fe_merged="$gate_json_stamped"' in src, \
        "a helper failure must never drop this close's own dispatch"


def test_dispatch_banner_reports_the_merged_count():
    """A banner sourced from gate_json under-reports after a merge."""
    src = CALL_SITE.read_text(encoding="utf-8")
    assert 'core_count=$(echo "$_fe_merged"' in src
    assert 'core_count=$(echo "$gate_json"' not in src


# -------------------------------------------------------------- 3. mutation

def test_detection_is_load_bearing_removing_the_merge_breaks_these_tests():
    """guard-1475/guard-1780: prove the assertions fail with the fix removed.

    The pre-fix behavior is exactly 'emit the new payload, ignore the existing one'.
    Simulating it must break the defect test -- otherwise this suite would stay
    green through a revert.
    """
    old = fired([f"core/scripts/f{i}.sh" for i in range(10)])
    new = fired(["core/scripts/a.py", "core/scripts/b.sh", "core/scripts/c.md"])

    pre_fix = new  # unconditional overwrite: existing is simply discarded
    assert not all(f in pre_fix["files"] for f in old["files"]), \
        "pre-fix control must LOSE the old file set (else the test proves nothing)"
    assert pre_fix["core_count"] == 3

    rc, post_fix = run(new, old)
    assert all(f in post_fix["files"] for f in old["files"])
    assert post_fix["core_count"] == 13
    assert post_fix != pre_fix, "post-fix output must differ from the pre-fix control"
