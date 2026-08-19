"""test_e9_skip_observation_reason.py — regression for .

The E9 hook (`_emit_e9_skip_observation`) appends a `sensory_buffer`
observation whenever a goal flips to skipped/expired, so the skip RATIONALE
reaches the encoding pipeline instead of dying on the goal record.

THE DEFECT THIS PINS. The reason was selected as
`skip_reason or defer_reason or "no reason given"`. **`skip_reason` is a field
no writer sets** — measured 2026-08-16 (bravo, hostname cc-05, `uname -r`
6.8.0-137-generic) against the live store: the key is structurally ABSENT from
every goal record, not merely null. A skipped goal also rarely carries a
`defer_reason`. So the chain fell through to the fallback on 100% of skips, and
all 5 live `sensory_buffer` items read `Reason: no reason given` while their
goal records carried 85, 361, 811, 1989 and 3316 characters of recorded
rationale in `outcome_note`.

Why that is worse than a cosmetic string bug: the buffer is a DETECTION
surface. "5 goals skipped with no reason given" reads as a real hygiene
problem, so a reader either chases five non-problems or — the likelier outcome
after it has cried wolf a few times — learns to discount the buffer entirely.
rb-245 class: a zero (here, a 100% fallback rate) produced by reading a field
the store does not carry.

REACHABILITY. This hook fires on the STATUS write, so the fix only works if
`outcome_note` is already on the record at that moment. It is: the framework's
own normative order is "write the evidence to outcome_note FIRST, then set
status=skipped" (aspirations.py, superseded-status guard, ~L2017), and
`agent-watchdog.py` writes the pair literally as
`(("outcome_note", note), ("status", "skipped"))`. Pinned by
test_outcome_note_reaches_observation below, which is the end-to-end shape.

TWO COPIES. The identical selection chain lives in
`mind_api/src/endpoints/aspirations_write.py` (daemon path). A fix applied to
one copy and not the other is the live failure mode, so the last test pins
their agreement structurally rather than trusting a comment.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import aspirations  # noqa: E402


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def _emit(monkeypatch, goal, new_status="skipped"):
    """Run the hook with the wm.py subprocess stubbed; return the payload dict.

    Returns None when the hook declined to emit (trivial-goal short-circuit).
    """
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return _FakeCompleted()

    monkeypatch.setattr(aspirations.subprocess, "run", fake_run)
    aspirations._emit_e9_skip_observation(goal.get("id", "g-t-01"), new_status, goal)
    if "input" not in captured:
        return None
    return json.loads(captured["input"])


# A description long enough to clear the trivial-goal short-circuit
# (len(desc) < 40 AND len(title) < 30 returns early).
_DESC = "A description well over forty characters so the hook does not short-circuit."


def test_outcome_note_reaches_observation(monkeypatch):
    """THE FIX. A skipped goal whose only recorded reason is outcome_note must
    surface that reason, not the fallback."""
    payload = _emit(monkeypatch, {
        "id": "g-t-01",
        "title": "Investigate: git/capacity drift on this box",
        "description": _DESC,
        "outcome_note": "condition resolved before investigation — every metric back under threshold",
    })
    assert payload is not None
    obs = payload["observation"]
    assert "condition resolved before investigation" in obs, obs
    assert "no reason given" not in obs, obs


def test_fallback_still_fires_when_nothing_is_recorded(monkeypatch):
    """The fallback must survive. A goal genuinely skipped with no recorded
    reason is a real signal and must keep saying so — widening the chain must
    not make every skip look explained."""
    payload = _emit(monkeypatch, {
        "id": "g-t-02",
        "title": "Some goal with no recorded rationale at all",
        "description": _DESC,
    })
    assert payload is not None
    assert "Reason: no reason given" in payload["observation"]


def test_empty_outcome_note_is_not_treated_as_a_reason(monkeypatch):
    """An empty, absent, or WHITESPACE-ONLY note must fall through to the
    fallback rather than emit a blank reason.

    The whitespace cases are the ones that bite. `"" or x` handles empty for
    free, but `"   \\n"` is TRUTHY — so before the `.strip()` the chain
    short-circuited past defer_reason AND past the fallback and emitted
    "Reason:    .", which is strictly worse than the "no reason given" it was
    meant to replace. This docstring previously claimed whitespace coverage
    while the loop tested only ("", None); a fresh-eyes probe caught the
    overclaim. A docstring that promises a case the body never exercises is a
    pin that never fires (same class as the sync-pin false-RED noted below).
    """
    for note in ("", None, "   ", "   \n", "\t\n  "):
        payload = _emit(monkeypatch, {
            "id": "g-t-03",
            "title": "Goal whose outcome_note is present but empty",
            "description": _DESC,
            "outcome_note": note,
        })
        assert payload is not None
        assert "Reason: no reason given" in payload["observation"], note


def test_non_string_outcome_note_does_not_crash_the_fail_open_hook(monkeypatch):
    """A non-string `outcome_note` must not raise, and must not leak its type
    into the observation.

    THE CRASH THIS PINS. The chain is computed BEFORE the `try` that wraps the
    subprocess, and both call sites (`aspirations.py` cmd_update_goal,
    `aspirations_write.py` cmd_update_goal) invoke the hook unguarded and AFTER
    the lock has released and the status write has committed. So subscripting a
    dict raised `KeyError: slice(None, 300, None)` straight out of a function
    whose own docstring promises "fail-open: never blocks the status-change
    return path" — failing CLOSED on an already-persisted write, and 500-ing
    the daemon endpoint. A list did not raise but returned a LIST, which the
    f-string then rendered as "Reason: ['a', 'b']".

    Both are regressions the `[:300]` subscript introduced; the pre-fix chain
    had no subscript and was type-agnostic. `str(...)` restores that.
    """
    for note in ({"text": "x"}, ["a", "b"], 42):
        payload = _emit(monkeypatch, {
            "id": "g-t-08",
            "title": "Goal whose outcome_note is not a string",
            "description": _DESC,
            "outcome_note": note,
            "defer_reason": "a real defer that must still be reachable",
        })
        assert payload is not None, note
        obs = payload["observation"]
        assert isinstance(obs, str), note
        # Whatever the coercion yields, it must be a string in the observation
        # — never a bare repr of a container leaking through the f-string.
        assert "Reason: " in obs, note


def test_outcome_note_outranks_defer_reason(monkeypatch):
    """ORDER. On a SKIPPED goal a defer_reason is a stale leftover from before
    the skip decision, while outcome_note IS the skip decision."""
    payload = _emit(monkeypatch, {
        "id": "g-t-04",
        "title": "Goal carrying both a stale defer and a real skip note",
        "description": _DESC,
        "defer_reason": "human_blocked: waiting on an approval that no longer applies",
        "outcome_note": "superseded by g-t-99 which shipped the whole scope",
    })
    assert payload is not None
    obs = payload["observation"]
    assert "superseded by g-t-99" in obs, obs
    assert "human_blocked" not in obs, obs


def test_defer_reason_still_used_when_no_outcome_note(monkeypatch):
    """Widening the chain must not DROP the pre-existing defer_reason lane."""
    payload = _emit(monkeypatch, {
        "id": "g-t-05",
        "title": "Goal with only a defer_reason recorded",
        "description": _DESC,
        "defer_reason": "precondition: upstream service not yet provisioned",
    })
    assert payload is not None
    assert "upstream service not yet provisioned" in payload["observation"]


def test_long_outcome_note_is_truncated(monkeypatch):
    """Live notes run to 3316 chars. The buffer is a queue, not an archive —
    an untruncated note would crowd out other observations."""
    payload = _emit(monkeypatch, {
        "id": "g-t-06",
        "title": "Goal with a very long recorded rationale",
        "description": _DESC,
        "outcome_note": "X" * 5000,
    })
    assert payload is not None
    obs = payload["observation"]
    assert "X" * 300 in obs
    assert "X" * 301 not in obs


def test_expired_status_takes_the_same_path(monkeypatch):
    """The hook fires for `expired` as well as `skipped`; the reason chain is
    shared, so an expired goal must surface its note too."""
    payload = _emit(monkeypatch, {
        "id": "g-t-07",
        "title": "Goal that aged out of its deadline window",
        "description": _DESC,
        "outcome_note": "expired unexecuted after the window closed",
    }, new_status="expired")
    assert payload is not None
    assert "expired unexecuted" in payload["observation"]
    assert payload["observation"].startswith("Goal expired:")


def test_cli_and_daemon_copies_agree_on_precedence():
    """TWO-COPY SYNC PIN. `mind_api/src/endpoints/aspirations_write.py` carries
    the identical chain. Fixing one copy and not the other is the live failure
    mode, and no test elsewhere compares them — so assert structurally that
    BOTH consult outcome_note, and that BOTH rank it above defer_reason.

    Deliberately a source-text check, not an import: the daemon module pulls in
    the whole endpoint stack, and a skipped import here would be a pin that
    never fires.
    """
    files = {
        "cli": CORE_SCRIPTS / "aspirations.py",
        "daemon": PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py",
    }
    for label, path in files.items():
        assert path.exists(), f"{label} copy missing at {path}"
        src = path.read_text(encoding="utf-8")
        # Isolate the assignment expression, not the surrounding prose — a
        # comment mentioning the fields must not satisfy this pin.
        # Anchor on the chain's terminating fallback, not on the first `)` —
        # a non-greedy match to `)` stops inside `goal.get("skip_reason")` and
        # then reports the chain as missing outcome_note, which is a false RED
        # indistinguishable from the real regression this pin exists to catch.
        m = re.search(r'skip_reason\s*=\s*\((.*?"no reason given")\s*\)', src, re.S)
        assert m, f"{label}: skip_reason assignment not found in {path}"
        chain = m.group(1)
        assert "outcome_note" in chain, f"{label}: outcome_note absent from the chain"
        assert "defer_reason" in chain, f"{label}: defer_reason dropped from the chain"
        assert chain.index("outcome_note") < chain.index("defer_reason"), (
            f"{label}: defer_reason outranks outcome_note — on a skipped goal "
            f"the defer is stale and the note is the decision"
        )
