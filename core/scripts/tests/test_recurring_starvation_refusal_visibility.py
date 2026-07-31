"""test_recurring_starvation_refusal_visibility.py —  outcome-3 pins.

Sibling to test_recurring_starvation_apply.py, which pins the dedup and the
`--max-file` cap over the JSON output. This file pins the HUMAN output, which
is the surface an operator and the aspirations precheck actually read, and
which had a hole the JSON did not:

  the summary block was gated on `if args.apply and not filed:`, so a run that
  filed ANYTHING printed nothing about its refusals.

With the default `--max-file 1` that is reachable on the first two rows — row 1
refused, row 2 filed, loop breaks — and it is the WORST shape, because the
worst-starved row sorts first and is therefore the one most likely to be the
refused one. Measured on the live script before the fix, stdout read:

    2 starved of 2 examined ...
        g-999-AA  120.0h =  50.0x ...
        g-999-BB  120.0h =  40.0x ...
        FILED g-115-9001 for g-999-BB

with no mention anywhere that g-999-AA had been REFUSED. That reads as a clean
capped run rather than a swallowed detection — a detector whose refusals are
invisible is indistinguishable from one that found nothing (guard-1802).

The second hole is the CHANNEL. The reason lived only on stderr while the
count lived on stdout, and the stdout line said "see the WARN line(s) above" —
which points at nothing for any caller that drops stderr (`2>/dev/null`, a
stdout-only capture, a log tail). Same split-channel class as guard-1680.

NOT PINNED HERE, deliberately: the exit code. `main()` returns 0 on every path
and `test_recurring_starvation_apply._run` asserts that explicitly with the
comment "main() must always exit 0 (fail-open sweep)". That is a deliberate
contract, not an oversight, so this fix makes the OUTPUT honest and leaves the
exit code alone.

Only the daemon boundary is stubbed, so the real `_file_unblock` payload
construction, the real loop arithmetic, and the real print block all run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "recurring_starvation_refusal",
    str(SCRIPT_DIR / "recurring-starvation-check.py"),
)
rsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsc)

REFUSAL = '{"error":"goal_duplication_blocked","gate":"goal-duplication-gate"}'

STATS = {"examined": 2, "shelved": 0, "basis_suppressed": 0,
         "unreadable_anchor": 0, "sources_seen": 1}


def _row(goal_id: str, ratio: float) -> dict:
    """One starved row in the exact shape scan() emits."""
    return {
        "goal_id": goal_id, "aspiration_id": "asp-999", "source": "world",
        "title": "Recurring: synthetic sweep", "age_hours": 120.0,
        "anchor_field": "lastAchievedAt", "interval_hours": 6,
        "basis_hours": 24.0, "basis_reason": "interval", "ratio": ratio,
        "declared_ratio": 20.0, "intended_agent": "zeta",
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.setattr(rsc, "_read_active", lambda source: [])
    monkeypatch.setattr(rsc, "_existing_origin_signals", lambda: set())


def _filer(monkeypatch, refuse_suffixes=()):
    """Refuse any goal whose origin_signal ends with a listed suffix."""
    def _fake(asp_id, payload, source=None):
        sig = payload.get("origin_signal", "")
        if any(sig.endswith(s) for s in refuse_suffixes):
            err = RuntimeError("refused")
            err.body = REFUSAL
            raise err
        return {"id": "g-115-9001"}
    monkeypatch.setattr(rsc._rt, "aspirations_add_goal", _fake)


def _run(monkeypatch, capsys, rows, argv_extra):
    """Invoke the REAL main(); return (stdout, stderr)."""
    monkeypatch.setattr(rsc, "scan", lambda mult, breaks=None: (rows, dict(STATS)))
    monkeypatch.setattr(sys, "argv", ["recurring-starvation-check.py"] + argv_extra)
    rc = rsc.main()
    assert rc == 0, "main() must stay fail-open (exit 0) — see module docstring"
    cap = capsys.readouterr()
    return cap.out, cap.err


# ── The partial swallow: a refusal sharing a run with a success ───────────

def test_refusal_is_visible_even_when_another_row_filed(monkeypatch, capsys):
    """THE regression. Row 1 refused, row 2 filed, --max-file 1.

    Before the fix stdout carried the FILED line and nothing else, so the
    refusal of the WORSE-starved row was invisible on the channel operators
    read.
    """
    _filer(monkeypatch, refuse_suffixes=("g-999-AA",))
    out, _ = _run(monkeypatch, capsys,
                  [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)],
                  ["--apply", "--max-file", "1"])

    assert "FILED g-115-9001 for g-999-BB" in out, "the success must still print"
    assert "REFUSED g-999-AA" in out, (
        "the refusal must reach STDOUT even though the run filed something — "
        "this is the swallow the goal exists to close")
    assert "1 attempt(s) REFUSED" in out


def test_refusal_reason_reaches_stdout_not_only_stderr(monkeypatch, capsys):
    """The reason must be readable without stderr (guard-1680 class)."""
    _filer(monkeypatch, refuse_suffixes=("g-999-AA",))
    out, err = _run(monkeypatch, capsys,
                    [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)],
                    ["--apply", "--max-file", "1"])

    assert "goal_duplication_blocked" in out, (
        "a caller that drops stderr must still learn WHY the filing failed")
    # stderr keeps its WARN — this is additive, not a channel move.
    assert "goal_duplication_blocked" in err


def test_stdout_no_longer_points_at_a_channel_it_did_not_write(monkeypatch, capsys):
    """The old text said 'see the WARN line(s) above' — on stdout there were none."""
    _filer(monkeypatch, refuse_suffixes=("g-999-AA", "g-999-BB"))
    out, _ = _run(monkeypatch, capsys,
                  [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)],
                  ["--apply", "--max-file", "2"])

    assert "see the WARN line(s) above" not in out
    assert "reasons above" in out
    assert out.count("REFUSED g-999-") == 2


# ── The all-refused case must keep working ────────────────────────────────

def test_all_refused_still_says_nothing_filed(monkeypatch, capsys):
    _filer(monkeypatch, refuse_suffixes=("g-999-AA", "g-999-BB"))
    out, _ = _run(monkeypatch, capsys,
                  [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)],
                  ["--apply", "--max-file", "2"])

    assert "nothing filed" in out
    assert "all 2 attempt(s) REFUSED" in out
    assert "FILED" not in out.replace("REFUSED", "")


def test_all_refused_omits_the_filed_lines_qualifier(monkeypatch, capsys):
    """The 'do not read the FILED lines above' clause needs FILED lines to exist."""
    _filer(monkeypatch, refuse_suffixes=("g-999-AA",))
    out, _ = _run(monkeypatch, capsys, [_row("g-999-AA", 50.0)],
                  ["--apply", "--max-file", "1"])

    assert "REFUSED g-999-AA" in out
    assert "do not read the FILED lines above" not in out, (
        "pointing at FILED lines that do not exist is the same class of "
        "dangling reference this fix removes")


# ── Clean runs must not grow a spurious refusal line ──────────────────────

def test_clean_run_prints_no_refusal_line(monkeypatch, capsys):
    _filer(monkeypatch, refuse_suffixes=())
    out, _ = _run(monkeypatch, capsys,
                  [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)],
                  ["--apply", "--max-file", "2"])

    assert "REFUSED" not in out
    assert out.count("FILED ") == 2


def test_report_only_run_prints_no_refusal_line(monkeypatch, capsys):
    """Without --apply nothing is attempted, so nothing can be refused."""
    _filer(monkeypatch, refuse_suffixes=("g-999-AA", "g-999-BB"))
    out, _ = _run(monkeypatch, capsys,
                  [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)], [])

    assert "REFUSED" not in out
    assert "nothing filed" not in out


# ── The JSON contract stays backward compatible ───────────────────────────

def test_json_keeps_int_file_failures_and_adds_details(monkeypatch, capsys):
    """`file_failures` stays an int — test_recurring_starvation_apply pins it."""
    _filer(monkeypatch, refuse_suffixes=("g-999-AA",))
    out, _ = _run(monkeypatch, capsys,
                  [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)],
                  ["--apply", "--max-file", "1", "--output", "json"])
    d = json.loads(out)

    assert d["file_failures"] == 1
    assert isinstance(d["file_failures"], int)
    assert d["file_failure_details"] == [
        {"goal_id": "g-999-AA", "reason": REFUSAL}]
    assert len(d["filed"]) == 1


def test_file_unblock_errors_param_is_optional(monkeypatch):
    """Existing call sites pass no `errors` — that must keep working."""
    _filer(monkeypatch, refuse_suffixes=("g-999-AA",))
    assert rsc._file_unblock(_row("g-999-AA", 50.0)) is None
    assert rsc._file_unblock(_row("g-999-BB", 40.0)) == "g-115-9001"


# ── The NON-exception failure paths ───────────────────────────────────────
#
# Found by fresh-eyes on the same iteration that introduced the fix above.
# `_file_unblock` returns None from THREE places; the first version of this
# change recorded a reason from only ONE (the daemon-raised path). Because
# `failed` became `len(failures)`, the other two reported file_failures=0 AND
# printed nothing — the `if failed: ... elif deduped: ...` chain matches no
# branch when both are zero. That is LESS visible than the `failed += 1`
# counter it replaced, i.e. a regression in the exact property being fixed.
# One test per path, plus the structural net.

def _returns(monkeypatch, value):
    """Daemon RETURNS `value` instead of raising."""
    monkeypatch.setattr(rsc._rt, "aspirations_add_goal",
                        lambda asp_id, payload, source=None: value)


def test_unparseable_string_response_is_reported(monkeypatch, capsys):
    _returns(monkeypatch, "<html>502 Bad Gateway</html>")
    out, err = _run(monkeypatch, capsys, [_row("g-999-AA", 50.0)],
                    ["--apply", "--max-file", "1"])

    assert "REFUSED g-999-AA" in out, "an unparseable response must not vanish"
    assert "unparseable response" in out
    assert "502 Bad Gateway" in out, "the actual body is the diagnostic"
    assert "1 attempt(s) REFUSED" in out
    assert "502 Bad Gateway" in err, "stderr keeps its WARN too"


def test_response_without_goal_id_is_reported(monkeypatch, capsys):
    """A 200 that carried no id is the quietest failure of the three."""
    _returns(monkeypatch, {"status": "ok", "warning": "duplicate suppressed"})
    out, _ = _run(monkeypatch, capsys, [_row("g-999-AA", 50.0)],
                  ["--apply", "--max-file", "1"])

    assert "REFUSED g-999-AA" in out
    assert "no goal id" in out
    assert "status" in out and "warning" in out, "surface the keys that WERE present"


def test_unexpected_response_type_is_reported(monkeypatch, capsys):
    _returns(monkeypatch, None)
    out, _ = _run(monkeypatch, capsys, [_row("g-999-AA", 50.0)],
                  ["--apply", "--max-file", "1"])

    assert "REFUSED g-999-AA" in out
    assert "unexpected response type" in out
    assert "NoneType" in out


def test_no_failure_path_can_report_zero(monkeypatch, capsys):
    """The whole point: `nothing filed` must never print with no reason.

    Before the fix this run printed the two starved rows and then stopped —
    no FILED line, no REFUSED line, no `nothing filed` line. Silence.
    """
    _returns(monkeypatch, {"status": "ok"})
    out, _ = _run(monkeypatch, capsys,
                  [_row("g-999-AA", 50.0), _row("g-999-BB", 40.0)],
                  ["--apply", "--max-file", "2"])

    assert "REFUSED" in out
    assert "nothing filed" in out
    assert "all 2 attempt(s) REFUSED" in out


def test_json_counts_non_exception_failures_too(monkeypatch, capsys):
    """`file_failures` regressed to 0 on these paths — pin the count."""
    _returns(monkeypatch, {"status": "ok"})
    out, _ = _run(monkeypatch, capsys, [_row("g-999-AA", 50.0)],
                  ["--apply", "--max-file", "1", "--output", "json"])
    d = json.loads(out)

    assert d["file_failures"] == 1, (
        "a non-exception failure must count — reporting 0 here is the "
        "regression this suite exists to prevent")
    assert d["filed"] == []
    assert d["file_failure_details"][0]["goal_id"] == "g-999-AA"


def test_caller_records_a_reason_even_if_the_callee_forgets(monkeypatch, capsys):
    """Structural net for a FUTURE unrecorded return-None path.

    Simulates exactly the bug that was just fixed: a `_file_unblock` that
    returns None without touching `errors`. The caller must still count it,
    so `failed` can never silently read zero.
    """
    monkeypatch.setattr(rsc, "_file_unblock",
                        lambda row, errors=None: None)
    out, _ = _run(monkeypatch, capsys, [_row("g-999-AA", 50.0)],
                  ["--apply", "--max-file", "1"])

    assert "REFUSED g-999-AA" in out
    assert "unrecorded failure path" in out, (
        "the net must name itself as a net, so a reader is not misled into "
        "thinking the callee produced this diagnosis")
