"""Staleness guard on goal-selector's cross-session completion window ().

WHY THIS EXISTS. `load_recent_class_completions` builds the "recent completions"
window by tail-reading <agent>/journal.jsonl `goals_completed`. Nothing in
core/scripts or mind_api writes that field (every match targets a different
store: session telemetry, handoff.yaml, or the loop_state int counter), so the
field stopped being populated and the window silently fossilised.

Every pre-existing fallback keys on the window being UNREADABLE (no AGENT_DIR,
missing journal, index error, read error) or EMPTY. None keyed on it being OLD,
so a window filled months ago was indistinguishable from a fresh one and was
returned as "recent" to three scorer criteria: per_goal_saturation (a
RAPID-REPEAT suppressor that charges -5.0 for a months-old completion),
class_balance_bonus, and context_coherence.

Measured on two boxes before the fix: alpha/cc-04 walked back 194 of 384 journal
entries to fill window_size=20, newest contributor 15.7 days old and oldest 50.7;
zeta/cc-02 measured 82 days. Neither produced any signal.

THE LOAD-BEARING TEST is test_fresh_window_is_not_flagged paired with
test_stale_window_falls_back: a guard that fires on everything is exactly as
broken as one that never fires, and only the pair distinguishes them. Deleting
the guard makes test_stale_window_falls_back fail; making it unconditional makes
test_fresh_window_is_not_flagged fail.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SELECTOR = Path(__file__).resolve().parents[1] / "goal-selector.py"


def _load():
    """Import goal-selector.py, whose hyphenated name blocks a plain import.

    Purges `_paths` from sys.modules first. goal-selector does
    `from _paths import ...` at module scope and _paths computes WORLD_DIR /
    AGENT_DIR ONCE at ITS import, so a cached copy pins whatever environment
    existed when some earlier test imported it.

    That is not hypothetical: test_class_balance_cross_session sandboxes via
    MIND_WORLD/MIND_AGENT_DIR and correctly restores those env vars in a
    finally -- but leaves _paths cached pointing at its tmp dirs, which it then
    deletes. Measured: after that file runs, sys.modules["_paths"].WORLD_DIR is
    a removed /tmp/cbcs_world_* path. A fresh exec_module here inherits it, so
    all 21 of these tests pass solo and fail in-suite purely on file order.

    Re-importing under restored env repairs the entry rather than polluting it,
    so this is safe for whatever runs after us.
    """
    for name in ("_paths", "goal_selector_under_test"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("goal_selector_under_test", SELECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _window_age_days — the clock half
# ---------------------------------------------------------------------------

def test_age_of_today_is_under_a_day():
    m = _load()
    today = dt.datetime.now().strftime("%Y-%m-%d")
    age = m._window_age_days(today)
    assert age is not None and 0.0 <= age < 1.5


def test_age_of_a_known_old_date_matches_the_calendar():
    """Pin the arithmetic against a computed offset, not a hardcoded date.

    A literal date would silently drift into a different age every day this
    test runs, which is the same fossil-by-time failure the guard exists for.
    """
    m = _load()
    old = (dt.datetime.now() - dt.timedelta(days=50)).strftime("%Y-%m-%d")
    age = m._window_age_days(old)
    assert age is not None and 49.0 < age < 51.5


def test_full_iso_timestamp_is_accepted():
    """The journal writes bare YYYY-MM-DD; a restored writer may add precision."""
    m = _load()
    stamp = (dt.datetime.now() - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    age = m._window_age_days(stamp)
    assert age is not None and 2.5 < age < 3.5


@pytest.mark.parametrize("bad", ["not-a-date", "", None, 12345, "2026-13-99"])
def test_unparseable_dates_fail_open_rather_than_reading_as_stale(bad):
    """None, never an exception and never a fake age.

    Fails OPEN by design: if a journal-format change made dates unparseable and
    this returned a large number instead, the guard would flip to always-on and
    silently disable the real window for every agent -- trading the bug for a
    worse one.
    """
    m = _load()
    assert m._window_age_days(bad) is None


# ---------------------------------------------------------------------------
# The guard in situ — the pair that actually pins the behaviour
# ---------------------------------------------------------------------------

def _days_ago(n, fmt="%Y-%m-%d"):
    return (dt.datetime.now() - dt.timedelta(days=n)).strftime(fmt)


def _harness(m, tmp_path, monkeypatch, *, store_goals, journal_date=None,
             journal_ids=()):
    """Point BOTH completion sources at fixtures this test fully controls.

    The first version of these tests read the LIVE aspirations store for real
    goal ids. That made them non-hermetic (a quiet queue could skip them) and,
    worse, unable to test the store path at all -- the live store always carries
    fresh completions, so a stale-store case was unconstructible. Seeding both
    sources is what lets each path be driven in both directions.
    """
    store = tmp_path / "world-aspirations.jsonl"
    store.write_text(
        json.dumps({"id": "asp-test", "goals": list(store_goals)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "WORLD_ASP_PATH", store)
    monkeypatch.setattr(m, "AGENT_ASP_PATH", None)

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(exist_ok=True)
    if journal_date is not None:
        (agent_dir / "journal.jsonl").write_text(
            json.dumps({"date": journal_date,
                        "goals_completed": list(journal_ids)}) + "\n",
            encoding="utf-8",
        )
    else:
        (agent_dir / "journal.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(m, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(m, "read_wm", lambda: {"goals_completed_this_session": []})


def _goal(gid, work_class="framework", **kw):
    g = {"id": gid, "work_class": work_class}
    g.update(kw)
    return g


# --- store path (remedy (a)) -----------------------------------------------

def test_store_is_the_primary_source(tmp_path, monkeypatch, capsys):
    """MUTATION PROOF for remedy (a) -- fails if the store branch is removed.

    The journal here is deliberately STALE and holds DIFFERENT ids. If the
    reader still preferred the journal, it would either warn or return the
    journal's goal -- so asserting on which id comes back distinguishes the two
    sources rather than merely asserting "something came back".
    """
    m = _load()
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-store-1", completed_at=_days_ago(0, "%Y-%m-%dT%H:%M:%S")),
                     _goal("g-journal-1")],          # no marker -> not in `dated`
        journal_date=_days_ago(60), journal_ids=["g-journal-1"],
    )

    result = m.load_recent_class_completions()

    assert "STALE" not in capsys.readouterr().err
    assert [r["goal_id"] for r in result] == ["g-store-1"], (
        "the aspirations store must win over the journal"
    )


def test_stale_store_window_falls_back(tmp_path, monkeypatch, capsys):
    """MUTATION PROOF -- fails if the guard is dropped from the STORE path.

    This is the case an earlier draft could not catch: the guard sat only on the
    journal path, which a non-empty store made unreachable.
    """
    m = _load()
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-old-1", completed_at=_days_ago(60, "%Y-%m-%dT%H:%M:%S"))],
    )

    result = m.load_recent_class_completions()
    err = capsys.readouterr().err

    assert "STALE" in err, "a 60-day-old store window must warn"
    assert "aspirations store" in err, "the warning must name the source"
    assert "g-115-4293" in err, "the warning must name the root cause"
    assert result == [], "must fall back, not return the fossil window"


def test_fresh_store_window_is_not_flagged(tmp_path, monkeypatch, capsys):
    """Half of the pair: a guard that fires on everything is useless."""
    m = _load()
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-new-1", completed_at=_days_ago(0, "%Y-%m-%dT%H:%M:%S"))],
    )

    result = m.load_recent_class_completions()

    assert "STALE" not in capsys.readouterr().err
    assert [r["goal_id"] for r in result] == ["g-new-1"]


def test_completed_date_and_last_achieved_at_are_accepted(tmp_path, monkeypatch):
    """`completed_at` is best-covered, but it is not the only marker.

    lastAchievedAt matters disproportionately: it is the ONLY marker a recurring
    goal ever gets, so ignoring it would silently drop recurring work from the
    window that class_balance_bonus scores.
    """
    m = _load()
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-cd", completed_date=_days_ago(1)),
                     _goal("g-la", lastAchievedAt=_days_ago(0, "%Y-%m-%dT%H:%M:%S"))],
    )

    assert {r["goal_id"] for r in m.load_recent_class_completions()} == {"g-cd", "g-la"}


def test_window_is_chronological_and_capped(tmp_path, monkeypatch):
    """Consumers slice `recent[-N:]`, so order is load-bearing, not cosmetic."""
    m = _load()
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal(f"g-{i}", completed_at=_days_ago(i, "%Y-%m-%dT%H:%M:%S"))
                     for i in range(6)],
    )

    result = m.load_recent_class_completions(window_size=3)

    # g-0 is newest (0 days ago), g-5 oldest. Chronological == oldest first.
    assert [r["goal_id"] for r in result] == ["g-2", "g-1", "g-0"]


# --- scope: self, not the fleet ---------------------------------------------

def test_self_scoped_completions_win_over_partners(tmp_path, monkeypatch, capsys):
    """MUTATION PROOF -- fails if the completed_by filter is removed.

    Sourcing the SHARED aspirations store makes fleet leakage the default, and
    it is invisible: the window still looks full and fresh. Measured on the
    first draft of this fix, 7 of 8 entries belonged to partners. Harmless for
    per_goal_saturation, wrong for class_balance_bonus and context_coherence,
    which both ask about SELF.
    """
    m = _load()
    monkeypatch.setattr(m, "AGENT_NAME", "alpha")
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[
            _goal("g-mine", completed_by="alpha",
                  completed_at=_days_ago(1, "%Y-%m-%dT%H:%M:%S")),
            # Newer than mine -- a plain recency sort would prefer these.
            _goal("g-bravo", completed_by="bravo",
                  completed_at=_days_ago(0, "%Y-%m-%dT%H:%M:%S")),
            _goal("g-zeta", completed_by="zeta",
                  completed_at=_days_ago(0, "%Y-%m-%dT%H:%M:%S")),
        ],
    )

    result = m.load_recent_class_completions()

    assert "STALE" not in capsys.readouterr().err
    assert [r["goal_id"] for r in result] == ["g-mine"], (
        "partners' completions must not enter this agent's window"
    )


def test_falls_back_to_fleet_when_nothing_self_attributed(tmp_path, monkeypatch):
    """A fresh agent -- or a deployment not populating completed_by -- still
    gets a window. Too-wide beats absent, which is the defect being fixed."""
    m = _load()
    monkeypatch.setattr(m, "AGENT_NAME", "alpha")
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-someone", completed_by="bravo",
                           completed_at=_days_ago(0, "%Y-%m-%dT%H:%M:%S"))],
    )

    assert [r["goal_id"] for r in m.load_recent_class_completions()] == ["g-someone"]


def test_unattributed_fallback_is_labelled_in_the_stale_warning(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """The warning must say WHICH window went stale, so a reader can tell a
    self-scoped fossil from a fleet-wide one without re-deriving it."""
    m = _load()
    monkeypatch.setattr(m, "AGENT_NAME", "alpha")
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-old", completed_by="bravo",
                           completed_at=_days_ago(60, "%Y-%m-%dT%H:%M:%S"))],
    )

    m.load_recent_class_completions()

    assert "fleet-wide" in capsys.readouterr().err


# --- journal fallback path --------------------------------------------------

def test_journal_used_when_store_has_no_completion_markers(tmp_path, monkeypatch,
                                                           capsys):
    """The fallback must still work for a world predating completion markers."""
    m = _load()
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-journal-1")],          # work_class but no marker
        journal_date=_days_ago(0), journal_ids=["g-journal-1"],
    )

    result = m.load_recent_class_completions()

    assert "STALE" not in capsys.readouterr().err
    assert [r["goal_id"] for r in result] == ["g-journal-1"]


def test_store_is_used_even_with_no_journal_file(tmp_path, monkeypatch):
    """MUTATION PROOF -- fails if the journal-existence check gates the function.

    That check was correct while the journal was the only source. Left in place
    once the store became primary, it made the store path unreachable for any
    agent without a journal.jsonl -- a fresh or transplanted agent -- silently
    pinning it to the in-session list. The failure is invisible: such an agent
    simply never gets a cross-session window.
    """
    m = _load()
    monkeypatch.setattr(m, "AGENT_NAME", "alpha")
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-store-only", completed_by="alpha",
                           completed_at=_days_ago(0, "%Y-%m-%dT%H:%M:%S"))],
    )
    (tmp_path / "agent" / "journal.jsonl").unlink()   # no journal at all

    assert [r["goal_id"] for r in m.load_recent_class_completions()] == ["g-store-only"]


def test_missing_journal_still_falls_back_cleanly(tmp_path, monkeypatch):
    """The other side: no store markers AND no journal must not raise."""
    m = _load()
    monkeypatch.setattr(m, "AGENT_NAME", "alpha")
    _harness(m, tmp_path, monkeypatch, store_goals=[_goal("g-nomarker")])
    (tmp_path / "agent" / "journal.jsonl").unlink()

    assert m.load_recent_class_completions() == []


def test_stale_journal_window_falls_back(tmp_path, monkeypatch, capsys):
    """MUTATION PROOF -- fails if the guard is dropped from the JOURNAL path."""
    m = _load()
    _harness(
        m, tmp_path, monkeypatch,
        store_goals=[_goal("g-journal-1")],
        journal_date=_days_ago(60), journal_ids=["g-journal-1"],
    )

    result = m.load_recent_class_completions()
    err = capsys.readouterr().err

    assert "STALE" in err, "a 60-day-old journal window must warn"
    assert "journal.jsonl" in err, "the warning must name the source"
    assert result == [], "must fall back, not return the fossil window"


def test_threshold_is_env_overridable(tmp_path, monkeypatch):
    """Tunable without a code edit -- and the override must actually bind."""
    monkeypatch.setenv("RECENT_WINDOW_MAX_AGE_DAYS", "0.5")
    m = _load()
    assert m.RECENT_WINDOW_MAX_AGE_DAYS == 0.5
