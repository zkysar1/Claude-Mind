"""Pins iteration-close.sh's DRAINED close ().

The completed-not-closed drain closed a finished row with aspirations-complete-by.sh,
which runs no gate, so every check in do_verify skipped every drained close. It now
closes through `--phase verify --drain --key-finding`: every gate runs, and the steps
written for the session that ran the unit take a drain branch. Why each branch:
core/config/rationale/completed-not-closed-drain.md.

HOW THESE TESTS RUN THE REAL CODE. As in test_iteration_close_candidate_close.py, the
behavioural tests extract the real functions from iteration-close.sh at test time and
source them, with the one writer they call replaced by a stand-in that records its argv,
so no store is read and nothing is written. The wiring the extraction cannot see is
pinned on do_verify's text. The outcome-2 tests run the real close-review gate on a goal
shaped like a drained row, through the subprocess harness of test_goal_close_risk_tier.py.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import BASH  # noqa: E402
from test_goal_close_risk_tier import _A_ON, _ASKED, _run_gate, _tier_lines, _verdict_file  # noqa: E402
from test_iteration_close_forward_precondition import _extract  # noqa: E402
from test_post_status_stamps_are_non_fatal import CLOSE_SH, STATUS_WRITE, _do_verify_body  # noqa: E402

ENTRY = "_refuse_malformed_drain"
RECOVERY = "_print_recovery_instructions"
APPEND = "_append_recent_completion"
FINDING = "the drain's finding"
DRAIN_REMEDY = "Drained close: do not retry it and do not revert it."


def _bash(harness: str, **env) -> subprocess.CompletedProcess:
    """Run a harness with its inputs passed through the environment, never interpolated."""
    return subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60,
                          env={**os.environ, **env})


def _line(stderr: str, label: str) -> str:
    return next((ln.strip() for ln in stderr.splitlines() if ln.strip().startswith(label)), "")


# ─── 1. the entry check ───────────────────────────────────────────────────

def _entry(drain: str, key_finding: str, status: str) -> subprocess.CompletedProcess:
    harness = f"""
set -uo pipefail
DRAIN="$T_DRAIN"; KEY_FINDING="$T_KF"; GOAL_STATUS="$T_STATUS"
{_extract(ENTRY)}
rc=0; {ENTRY} || rc=$?
echo "RC=$rc"
"""
    return _bash(harness, T_DRAIN=drain, T_KF=key_finding, T_STATUS=status)


@pytest.mark.parametrize("drain,key_finding,status", [
    ("", "", "completed"),
    ("", "", "blocked"),
    ("true", FINDING, "completed"),
], ids=["loop-completed", "loop-blocked", "drained"])
def test_the_entry_check_passes_a_loop_close_and_a_wellformed_drained_close(drain, key_finding, status):
    p = _entry(drain, key_finding, status)
    assert "RC=0" in p.stdout and p.stderr == "", p.stdout + p.stderr


@pytest.mark.parametrize("drain,key_finding,status", [
    ("true", "", "completed"),
    ("", FINDING, "completed"),
    ("true", FINDING, "blocked"),
    ("true", FINDING, "skipped"),
], ids=["drain-without-finding", "finding-without-drain", "drained-blocked", "drained-skipped"])
def test_the_entry_check_refuses_a_malformed_drained_call(drain, key_finding, status):
    p = _entry(drain, key_finding, status)
    assert "RC=1" in p.stdout, p.stdout + p.stderr
    assert "--drain and --key-finding go together, with --status completed" in p.stderr
    assert "--phase verify --drain --goal <id> --status completed" in p.stderr


def test_do_verify_runs_the_entry_check_before_its_first_gate_and_the_status_write():
    body = _do_verify_body()
    call = body.find(f"{ENTRY} || exit 2")
    assert call != -1, f"do_verify no longer calls {ENTRY}"
    for later in ("pending-deploys-gate.sh", "domain-suite-gate.py", STATUS_WRITE):
        at = body.find(later)
        assert at != -1, f"do_verify no longer contains {later!r}; re-pin this test"
        assert call < at, f"{ENTRY} must run before {later!r}"


# ─── 2. a refused drained close: the remedy ───────────────────────────────

def _recovery(live: str, rc: int, drain: str = "true", key_finding: str = FINDING) -> str:
    """What the EXIT trap prints after a verify call exits `rc`, the live status reading `live`."""
    harness = f"""
set -uo pipefail
GOAL_ID="g-999-1"; GOAL_STATUS="completed"; SOURCE="world"; OUTCOME="deep"
SUMMARY=""; OVERRIDE_UNCOMMITTED=""; OVERRIDE_MISSING_ARTIFACT=""; OVERRIDE_DOMAIN_SUITE=""
OVERRIDE_CLOSURE_EVIDENCE=""; OUTCOME_NOTE_FILE=""; _CURRENT_PHASE="verify"
DRAIN="$T_DRAIN"; KEY_FINDING="$T_KF"
_probe_goal_status() {{ printf '%s' "$T_LIVE"; }}
{_extract(RECOVERY)}
{RECOVERY} {rc}
"""
    return _bash(harness, T_DRAIN=drain, T_KF=key_finding, T_LIVE=live).stderr


def test_a_refused_drained_close_is_counted_and_held_never_retried_or_reverted():
    stderr = _recovery("in-progress", 1)
    assert "the status write did NOT land" in stderr and DRAIN_REMEDY in stderr, stderr
    assert "A CLOSE REVIEW refusal needs nothing more" in stderr
    assert "completed-not-closed-slate.sh --hold g-999-1 --reason" in stderr
    assert not _line(stderr, "Retry:") and "Revert (mark pending)" not in stderr, stderr


def test_control_a_refused_loop_close_still_gets_its_retry_and_its_revert():
    stderr = _recovery("in-progress", 1, drain="", key_finding="")
    retry = _line(stderr, "Retry:")
    assert DRAIN_REMEDY not in stderr and "Revert (mark pending)" in stderr, stderr
    assert retry and "--drain" not in retry, retry


@pytest.mark.parametrize("live,says", [
    ("completed", "ALREADY on the record"),      # interrupted after the write: the row closed
    ("", "asserting neither direction"),         # unread: probe first
], ids=["landed", "unreadable"])
def test_a_drained_close_is_counted_as_refused_only_when_the_write_did_not_land(live, says):
    stderr = _recovery(live, 1)
    assert says in stderr and DRAIN_REMEDY not in stderr, stderr


@pytest.mark.parametrize("drain,key_finding,suffix", [
    ("true", "", '--drain --key-finding "<one line>"'),
    ("", FINDING, f'--drain --key-finding "{FINDING}"'),
], ids=["drain-without-finding", "finding-without-drain"])
def test_a_malformed_drained_call_gets_the_corrected_retry(drain, key_finding, suffix):
    # rc 2 is the entry check: nothing ran, so the remedy is the same call made whole.
    stderr = _recovery("in-progress", 2, drain=drain, key_finding=key_finding)
    retry = _line(stderr, "Retry:")
    assert DRAIN_REMEDY not in stderr and retry.endswith(suffix), stderr


# ─── 3. the recent_completions row ────────────────────────────────────────

def _append(tmp_path: Path, key_finding: str | None, writer_rc: int = 0):
    """Source the real writer with team-state-update.sh replaced by an argv recorder."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    argv = tmp_path / "argv"
    (scripts / "team-state-update.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{argv.as_posix()}"\n'
        f"exit {writer_rc}\n", encoding="utf-8")
    call = APPEND if key_finding is None else f'{APPEND} "$T_KF"'
    harness = f"""
set -euo pipefail
SCRIPT_DIR="{scripts.as_posix()}"
GOAL_ID="g-999-1"; AGENT="alpha"; NOW_ISO="2026-10-06T19:00:00"
{_extract(APPEND)}
{call}
echo "AFTER"
"""
    p = _bash(harness, T_KF=key_finding or "")
    return p, (argv.read_text(encoding="utf-8").splitlines() if argv.exists() else [])


def test_the_row_carries_the_finding_through_backslashes_quotes_and_newlines(tmp_path):
    p, argv = _append(tmp_path, 'C:\\new\\path "quoted"\nsecond line')
    assert "AFTER" in p.stdout, p.stderr
    assert argv[:5] == ["--field", "recent_completions", "--operation", "append", "--value"], argv
    assert json.loads(argv[5]) == {
        "goal_id": "g-999-1", "completed_by": "alpha", "completed_at": "2026-10-06T19:00:00",
        "key_finding": 'C:\\new\\path "quoted" second line'}


@pytest.mark.parametrize("key_finding", [None, ""], ids=["no-argument", "empty"])
def test_a_row_with_no_finding_reads_completed(tmp_path, key_finding):
    _p, argv = _append(tmp_path, key_finding)
    assert json.loads(argv[5])["key_finding"] == "completed"


def test_a_failed_row_write_warns_and_never_aborts_the_close(tmp_path):
    p, _argv = _append(tmp_path, FINDING, writer_rc=1)
    assert "AFTER" in p.stdout, "a failed team-state write aborted the caller under set -e"
    assert "WARN: team-state-update recent_completions failed for g-999-1" in p.stderr


def test_a_loop_close_still_writes_its_row_from_its_summary():
    # Sliced at the next top-level function, as _do_verify_body slices do_verify:
    # _extract would stop at a column-0 brace inside do_state_update's embedded Python.
    after = CLOSE_SH.read_text(encoding="utf-8").split("\ndo_state_update() {", 1)[1]
    nxt = re.search(r"\n[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", after)
    assert nxt, "no function follows do_state_update; re-pin this test"
    assert f'{APPEND} "${{SUMMARY:-completed}}"' in after[: nxt.start()]


# ─── 4. do_verify's wiring ────────────────────────────────────────────────

# Every gate call found in do_verify is checked; these are the ones that must be found.
GATES = {"pending-deploys-gate.sh", "domain-suite-gate.py", "closure-evidence-gate.py",
         "close-review-gate.py"}
_GATE_CALL = re.compile(r"\b(?:bash|python3)\b.*?([A-Za-z0-9_-]*gate[A-Za-z0-9_-]*\.(?:py|sh))")
_HEAD = re.compile(r"(if|elif|else|while|for|until|case)\b")
_BODY_INDENT = 4   # do_verify's own statements. Shallower lines are text inside a string.


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _enclosing_heads(lines: list[str], idx: int) -> list[str]:
    """Every control-flow head around lines[idx], innermost first. An elif or else adds the
    `if` that opens its chain, because that branch runs on the `if`'s condition too."""
    heads, level = [], _indent(lines[idx])
    for j in range(idx - 1, -1, -1):
        s, ind = lines[j].strip(), _indent(lines[j])
        if not s or s.startswith("#") or ind >= level or ind < _BODY_INDENT or not _HEAD.match(s):
            continue
        heads.append(s)
        if s.startswith(("elif", "else")):
            heads.append(next((lines[k].strip() for k in range(j - 1, -1, -1)
                               if _indent(lines[k]) == ind and lines[k].strip().startswith("if ")), ""))
        level = ind
    return heads


def _at(lines: list[str], text: str) -> int:
    hits = [i for i, ln in enumerate(lines) if text in ln and not ln.strip().startswith("#")]
    assert len(hits) == 1, f"expected one do_verify line containing {text!r}, found {len(hits)}"
    return hits[0]


def test_every_gate_runs_on_a_drained_close():
    """The point of the change. A drain condition around any gate call would reopen the
    way around the gates that the complete-by close was."""
    lines = _do_verify_body().splitlines()
    seen = set()
    for i, ln in enumerate(lines):
        s = ln.strip()
        m = _GATE_CALL.search(s)
        if not m or s.startswith(("#", "echo")):
            continue
        seen.add(m.group(1))
        conditions = [s, *_enclosing_heads(lines, i)]
        assert not [c for c in conditions if "DRAIN" in c], (m.group(1), conditions)
    assert GATES <= seen, f"do_verify no longer calls {sorted(GATES - seen)}; re-pin this test"


def test_a_drained_close_tells_the_domain_suite_gate_so():
    body = _do_verify_body()
    forward = body.find('[[ -n "$DRAIN" ]] && _dsg_args+=(--drain)')
    call = body.find('domain-suite-gate.py")" "${_dsg_args[@]}"')
    assert forward != -1 and call != -1 and forward < call


def test_a_drained_close_writes_no_checkpoint():
    """The checkpoint is the drainer's own iteration's. do_verify writes it three times:
    intent_state complete, intent_state committed, and the final refresh."""
    lines = _do_verify_body().splitlines()
    writes = [i for i, ln in enumerate(lines) if not ln.strip().startswith("#")
              and (re.match(r"\s*(_checkpoint_update|_checkpoint_refresh)\s", ln)
                   or re.search(r"loop-state-save\.sh\"?\s+(update|init)", ln))]
    assert len(writes) == 3, [lines[i].strip() for i in writes]
    for i in writes:
        heads = _enclosing_heads(lines, i)
        assert any('-z "$DRAIN"' in h for h in heads), (lines[i].strip(), heads)


def test_a_drained_close_overrides_the_uncommitted_work_gate_whatever_its_outcome():
    lines = _do_verify_body().splitlines()
    i = _at(lines, 'OVERRIDE_UNCOMMITTED="auto: drained close')
    head = _enclosing_heads(lines, i)[0]
    # The FIRST branch of the chain, so a deep drained close never takes the deep branch's
    # reason, which names a state-update phase that does not run for this goal.
    assert head.startswith("if ") and '-n "$DRAIN"' in head and "OUTCOME" not in head, head
    assert i < _at(lines, STATUS_WRITE)


def test_a_drained_close_writes_its_finding_after_the_status_write():
    lines = _do_verify_body().splitlines()
    status = _at(lines, STATUS_WRITE)
    for text in ('"$GOAL_ID" key_finding "$KEY_FINDING"', f'{APPEND} "$KEY_FINDING"'):
        i = _at(lines, text)
        assert i > status and '-n "$DRAIN"' in _enclosing_heads(lines, i)[0], text


def test_a_drained_close_names_its_next_step_before_the_reducers_imperative():
    lines = _do_verify_body().splitlines()
    drain = _at(lines, "NEXT (drain):")
    worker = _at(lines, "NEXT (worker Body):")
    assert _enclosing_heads(lines, drain)[0] == 'if [[ -n "$DRAIN" ]]; then'
    # The worker and spark lines are later branches of the SAME chain.
    assert _enclosing_heads(lines, worker)[1] == 'if [[ -n "$DRAIN" ]]; then'
    assert drain < worker < _at(lines, "NEXT: Phase 6 spark REQUIRED")


# ─── 5. outcome 2: check A on a drained close ─────────────────────────────

def _drained_row(**kw):
    """A row the drain closes: tier 2 (HIGH, not recurring), claimed and executed by a
    session that has ended, its review requested. The drainer is a Mind session, which
    carries no BODY_ROLE, so check A judges it."""
    sid = "dddddddd-4444-4444-8444-dddddddddddd"
    row = {"goal_id": "g-999-01", "title": "t", "description": "d", "priority": "HIGH",
           "participants": ["agent"], "status": "in-progress", "claimed_by": "nobody",
           "executed_by": "nobody", "claimed_by_sid": sid, "executed_by_sid": sid,
           "outcome_note": "done", "review_requested": _ASKED}
    row.update(kw)
    return row


def test_check_A_refuses_a_drained_tier2_close_with_no_verdict(tmp_path):
    r = _run_gate(_drained_row(), tmp_path, _A_ON, env_drop=("BODY_ROLE",))
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line["tier"], line["request"]) == ("block", 2, "open")


def test_check_A_releases_a_drained_close_that_an_APPROVE_answered(tmp_path):
    """The positive control: the same row, with a peer's APPROVE written after the ask."""
    _verdict_file(tmp_path, "g-999-01", "APPROVE", "2026-10-06T10:00:00")
    r = _run_gate(_drained_row(), tmp_path, _A_ON, env_drop=("BODY_ROLE",))
    assert r.returncode == 0, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line["reviewer"], line["answered"]) == ("pass", "peer-mind", True)
