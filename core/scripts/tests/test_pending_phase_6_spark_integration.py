"""test_pending_phase_6_spark_integration.py —  integration coverage.

Closes the coverage gap named in g-115-2914: the non-recurring
pending_phase_6_spark sentinel write had NO test that EXECUTES the real bash
block. The sibling test_pending_phase_6_spark_sentinel.py covers source-grep +
a Python *reproduction* of the payload shape — neither runs the actual
production sentinel-write code for the non-recurring path.

Post-g-115-2848 reality (the write MOVED to do_state_update):
  g-115-2848 relocated the non-recurring sentinel WRITE from do_verify to
  `do_state_update` (commits 1ee17f40 + a03dd553), landing via git-push lag
  after this test was first authored. The move is a deliberate SPLIT, not a
  regression:
    - the stdout imperative "invoke Skill(aspirations-spark) BEFORE the
      state-update phase" STAYS in do_verify (Phase 5) — it must fire before
      state-update (Phase 8), so its timing is preserved;
    - only the WM sentinel moved to do_state_update, because a deep
      non-recurring verify run WITHOUT --outcome wrote no sentinel
      (g-115-2839); do_state_update always has --outcome.
  This test asserts BOTH halves of that split (see
  test_split_imperative_in_do_verify_sentinel_in_do_state_update).

Strategy — EXTRACT-AND-RUN, not hand-reproduction:
  The test slices the REAL sentinel block out of iteration-close.sh's
  do_state_update at test time (bracketed by stable anchors) and executes those
  exact bash lines with a stub wm-set.sh AND a stub `_probe_is_recurring`. The
  production block computes recurrence via the `_probe_is_recurring` FUNCTION
  (it reads the goal record), so the harness supplies a stub returning the value
  under test — the real block cannot be driven by an env var alone. Extraction
  (vs a copied snippet) means the test executes the real production lines and
  cannot drift from the script.

Why a stub wm-set.sh instead of a full end-to-end subprocess:
  A full `iteration-close.sh --phase state-update` subprocess is infeasible as a
  hermetic test on this box — do_state_update calls daemon-routed helpers and
  wm-set.sh is daemon-only (rt_call POST /v1/wm/set, no CLI fallback). A real run
  needs a live daemon + local backend (a daemon_integration-marked test),
  hazardous on this live own-cloud box (guard-955 / rb-2983 S3-key collision).
  Stubbing wm-set.sh at the `$SCRIPT_DIR/wm-set.sh` call boundary keeps the test
  hermetic while driving the real production block.

Cross-refs:
  - g-115-2914 (this test — Idea, integration-path coverage)
  - g-115-2848 (moved the sentinel write to do_state_update — commits 1ee17f40/a03dd553)
  - g-115-2839 (the gap the move fixes: verify-without-outcome wrote no sentinel)
  - g-115-2416 (the original non-recurring block, formerly in do_verify)
  - g-115-1174 (source-grep sibling: test_pending_phase_6_spark_sentinel.py)
  - core/scripts/iteration-close.sh do_state_update (the block under test)
  - .claude/skills/aspirations/SKILL.md Phase -0.5c.2 (the consumer)
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ITERATION_CLOSE_SH = CORE_SCRIPTS / "iteration-close.sh"

# Resolve bash absolutely (bin-first, clean-PATH-safe) — same import the
# sibling iteration-close tests use.
from _bash_helpers import BASH  # rb-1472

# Stable anchors bracketing the do_state_update sentinel block. `_su_is_recurring`
# is unique to this block (su = state-update); the End comment closes it. If
# either drifts, extraction fails loudly (asserted in _extract_sentinel_block)
# rather than silently testing nothing.
_START_ANCHOR = "local _su_is_recurring"
_END_ANCHOR = "End Phase-6 spark sentinel"

_WMSET_MARK = 'wm-set.sh" pending_phase_6_spark'


def _extract_sentinel_block() -> str:
    """Slice the REAL do_state_update sentinel block out of iteration-close.sh.

    Returns the bash lines from the `local _su_is_recurring` declaration through
    the `if ... fi` (inclusive of the start line, exclusive of the End comment).
    Executing these exact lines — rather than a hand-copied reproduction — is
    what makes this an integration test of the production code.
    """
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8").splitlines()
    start = end = None
    for i, line in enumerate(src):
        if _START_ANCHOR in line and start is None:
            start = i
        elif _END_ANCHOR in line and start is not None:
            end = i
            break
    assert start is not None, (
        f"START anchor {_START_ANCHOR!r} not found in {ITERATION_CLOSE_SH} — "
        "the do_state_update sentinel block was moved/renamed; update the anchor"
    )
    assert end is not None, (
        f"END anchor {_END_ANCHOR!r} not found after START in {ITERATION_CLOSE_SH}"
    )
    block = "\n".join(src[start:end])  # inclusive of the `local` decl line
    assert _WMSET_MARK in block, (
        "extracted block missing the wm-set pending_phase_6_spark invocation — "
        "anchors drifted or the write was deleted"
    )
    assert "_probe_is_recurring" in block, (
        "extracted block no longer calls _probe_is_recurring — the recurrence "
        "gate changed; review the harness stub"
    )
    return block


def _run_sentinel_block(outcome: str, stub_recurring: str,
                        goal_id: str = "g-TEST-01", source: str = "world",
                        summary: str = "a deep framework close"):
    """Execute the extracted do_state_update block with stubbed wm-set.sh +
    _probe_is_recurring (no daemon).

    stub_recurring is the value the stubbed _probe_is_recurring returns
    ("true"/"false") — the production block derives recurrence from that
    function, not an env var.

    Returns (returncode, stdout, stderr, wm_call) where wm_call is
    {"argv": <str>, "stdin": <str>} when the block invoked wm-set.sh, else None.
    """
    td = Path(tempfile.mkdtemp(prefix="p6spark-"))
    try:
        argv_file = td / "wm_argv"
        stdin_file = td / "wm_stdin"

        # Stub wm-set.sh: capture argv + stdin, never reach a daemon. Invoked
        # by the block as `bash "$SCRIPT_DIR/wm-set.sh" pending_phase_6_spark`.
        stub = td / "wm-set.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s" "$*" > {json.dumps(str(argv_file))}\n'
            f'cat > {json.dumps(str(stdin_file))}\n'
        )
        _chmod_x(stub)

        # python3 shim → the test interpreter, so the block's inline
        # `python3 -c` timestamp construction resolves deterministically.
        py3 = td / "python3"
        py3.write_text(
            "#!/usr/bin/env bash\n"
            f'exec {json.dumps(sys.executable)} "$@"\n'
        )
        _chmod_x(py3)

        # The production block calls the `_probe_is_recurring` FUNCTION; supply a
        # stub returning the recurrence value under test.
        prog = (
            f"_probe_is_recurring() {{ printf '%s' {json.dumps(stub_recurring)}; }}\n"
            "_sentinel_block() {\n"
            + _extract_sentinel_block()
            + "\n}\n_sentinel_block\n"
        )

        env = dict(os.environ)
        env["PATH"] = str(td) + os.pathsep + env.get("PATH", "")
        env["SCRIPT_DIR"] = str(td)
        env["OUTCOME"] = outcome
        env["GOAL_ID"] = goal_id
        env["SOURCE"] = source
        env["SUMMARY"] = summary

        r = subprocess.run(
            [BASH, "-c", prog], env=env,
            capture_output=True, text=True, timeout=30,
        )
        wm_call = None
        if argv_file.exists():
            wm_call = {
                "argv": argv_file.read_text(encoding="utf-8"),
                "stdin": stdin_file.read_text(encoding="utf-8") if stdin_file.exists() else "",
            }
        return r.returncode, r.stdout, r.stderr, wm_call
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _chmod_x(p: Path) -> None:
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ─── extraction smoke ──────────────────────────────────────────────────────


def test_extract_block_smoke():
    """The anchors resolve and the extracted block carries the real write."""
    block = _extract_sentinel_block()
    assert 'if [[ "$OUTCOME" == "deep"' in block, \
        "extracted block missing the OUTCOME==deep condition gate"
    assert _WMSET_MARK in block


# ─── behavioral: the block executed under real condition variables ─────────


def test_deep_nonrecurring_writes_full_sentinel():
    """(deep, non-recurring) — the real do_state_update block writes the sentinel.

    Asserts, from the ACTUAL production code path:
      1. wm-set.sh was invoked with the `pending_phase_6_spark` slot name.
      2. the piped payload is valid JSON carrying the 6 consumer-required
         fields (goal_id, outcome, source, summary, expires_at, set_at).
      3. field values round-trip (outcome=deep, goal_id/source preserved).
      4. expires_at parses and is after set_at (the consumer's staleness gate
         depends on a future expiry).

    NOTE: unlike the pre-g-115-2848 do_verify block, this block emits NO stdout
    imperative — that stayed in do_verify (see
    test_split_imperative_in_do_verify_sentinel_in_do_state_update).
    """
    rc, out, err, wm = _run_sentinel_block("deep", "false",
                                           goal_id="g-115-2914", source="world")
    assert rc == 0, f"block exited non-zero: {rc}; stderr={err!r}"
    assert wm is not None, f"deep non-recurring close did NOT write the sentinel; stderr={err!r}"
    assert wm["argv"].strip() == "pending_phase_6_spark", \
        f"wm-set targeted wrong slot: {wm['argv']!r}"

    payload = json.loads(wm["stdin"])  # raises if the real python built bad JSON
    for field in ("goal_id", "outcome", "source", "summary", "expires_at", "set_at"):
        assert field in payload, f"sentinel payload missing required field: {field}"
    assert payload["outcome"] == "deep"
    assert payload["goal_id"] == "g-115-2914"
    assert payload["source"] == "world"

    expires_at = datetime.fromisoformat(payload["expires_at"])
    set_at = datetime.fromisoformat(payload["set_at"])
    assert expires_at > set_at, \
        f"expires_at ({expires_at}) must be after set_at ({set_at})"


def test_routine_nonrecurring_skips():
    """(routine, non-recurring) — spark is deep-only, so NO write."""
    rc, out, err, wm = _run_sentinel_block("routine", "false")
    assert rc == 0, f"block exited non-zero: {rc}; stderr={err!r}"
    assert wm is None, "routine close wrongly wrote the pending_phase_6_spark sentinel"


def test_deep_recurring_skips():
    """(deep, recurring) — recurring-close.sh owns the sentinel for recurring
    closes (it writes the POST-FLIP outcome this phase cannot know), so the
    do_state_update block MUST skip when _probe_is_recurring returns true.
    Drives the real recurrence gate via the stubbed _probe_is_recurring."""
    rc, out, err, wm = _run_sentinel_block("deep", "true")
    assert rc == 0, f"block exited non-zero: {rc}; stderr={err!r}"
    assert wm is None, \
        "recurring close wrongly wrote the sentinel from do_state_update " \
        "(double-write with recurring-close.sh)"


# ─── source-location pins (regression guards on the production script) ─────


def test_iteration_close_has_nonrecurring_sentinel_block():
    """iteration-close.sh must still contain the non-recurring sentinel write
    in do_state_update, gated on OUTCOME==deep && !recurring."""
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    assert 'if [[ "$OUTCOME" == "deep" && "$_su_is_recurring" != "true" ]]' in src, \
        "do_state_update sentinel condition gate missing/changed — g-115-2848 regression"
    assert _WMSET_MARK in src, \
        "iteration-close.sh missing the pending_phase_6_spark wm-set invocation"
    assert '"set_at"' in src, \
        "iteration-close.sh sentinel payload missing set_at (consumption-based " \
        "dedup key — g-115-1404)"


def test_split_imperative_in_do_verify_sentinel_in_do_state_update():
    """Assert the  SPLIT: the sentinel WRITE lives in do_state_update,
    while the stdout imperative stays in do_verify.

    This is the load-bearing architectural pin. The imperative "invoke
    Skill(aspirations-spark) BEFORE the state-update phase" MUST fire at
    verify-close (Phase 5), before state-update (Phase 8) — so it must be in
    do_verify. The sentinel (consumed next-iteration, phase-agnostic) moved to
    do_state_update because a deep non-recurring verify without --outcome wrote
    no sentinel (g-115-2839). If a future edit collapses the split — moving the
    imperative into do_state_update (too late) or the sentinel back into
    do_verify (re-opening the no-outcome gap) — this fails and forces a
    conscious decision.
    """
    lines = ITERATION_CLOSE_SH.read_text(encoding="utf-8").splitlines()

    def _first(anchor: str):
        for i, ln in enumerate(lines):
            if anchor in ln:
                return i
        return None

    do_verify_i = _first("do_verify() {")
    do_state_i = _first("do_state_update() {")
    wmset_i = _first(_WMSET_MARK)
    imperative_i = _first("BEFORE the state-update phase")

    assert do_verify_i is not None, "do_verify() not found"
    assert do_state_i is not None, "do_state_update() not found"
    assert wmset_i is not None, "pending_phase_6_spark write not found"
    assert imperative_i is not None, "'BEFORE the state-update phase' imperative not found"

    # sentinel WRITE is in do_state_update (after do_state_update())
    assert wmset_i > do_state_i, (
        f"sentinel write (line {wmset_i + 1}) must be inside do_state_update "
        f"(starts line {do_state_i + 1}) — see docstring for the g-115-2848 split"
    )
    # imperative STAYS in do_verify (between do_verify() and do_state_update())
    assert do_verify_i < imperative_i < do_state_i, (
        f"the 'BEFORE the state-update phase' imperative (line {imperative_i + 1}) "
        f"must stay in do_verify ({do_verify_i + 1}..{do_state_i + 1}); moving it "
        "into do_state_update fires it too late (fast-path regression)"
    )


if __name__ == "__main__":
    # Direct invocation without pytest for quick smoke testing.
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = total = 0
    for fn in fns:
        total += 1
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {fn.__name__}: {e}")
            traceback.print_exc()
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)
