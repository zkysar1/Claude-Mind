"""Tests for core/scripts/liveness_check ().

MOST of this file exercises the PURE decision function, passing every input
directly. The final section is different and deliberately so: it drives ``main``
end-to-end against a real shard on disk, because the pure tests cannot see the
WIRING (g-115-3737 — deleting the ``fetch_retirement_tombstone`` call from
``main`` left all 22 pure tests green).

Both halves stay hermetic and credential-free. The integration tests pass a
FRESH ``--last-active`` so ``main`` short-circuits before
``fetch_authoritative_last_active_with_provenance`` and ``fetch_fresh_signal``;
the only IO they perform is reading a YAML shard out of ``tmp_path``.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from liveness_check import (  # noqa: E402
    decide_liveness, _parse_iso, _age, main, fetch_retirement_tombstone,
)

NOW = datetime(2026, 7, 14, 10, 0, 0)


def _ago(**kw):
    """ISO string for a time `kw` before NOW (e.g. _ago(minutes=30))."""
    return (NOW - timedelta(**kw)).isoformat()


# --- Fast path: a fresh last_active is sufficient -------------------------

def test_fresh_last_active_is_alive():
    r = decide_liveness(_ago(minutes=30), None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "last_active"


def test_fresh_last_active_wins_even_if_fresh_signal_stale():
    # Fast path short-circuits before the fresh signal matters.
    r = decide_liveness(_ago(minutes=10), _ago(days=7), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "last_active"


# --- THE bug-fix case: stale last_active but the partner is active ---------

def test_stale_last_active_fresh_signal_is_alive():
    # bravo scenario: local last_active mirror 7d stale, but the shard's
    # authoritative-store push time is 4 minutes old -> ALIVE, not dormant.
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "fresh_signal"


def test_absent_last_active_fresh_signal_is_alive():
    r = decide_liveness(None, _ago(minutes=45), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "fresh_signal"


# --- Genuine dormancy: both real signals say old --------------------------

def test_both_stale_is_dormant():
    r = decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW)
    assert r["verdict"] == "dormant"
    assert r["signal"] is None


def test_absent_last_active_stale_fresh_signal_is_dormant():
    r = decide_liveness(None, _ago(hours=9), threshold_hours=6, now=NOW)
    assert r["verdict"] == "dormant"


# --- False-dormant guard: unavailable fresh signal -> unknown -------------

def test_stale_last_active_unavailable_fresh_signal_is_unknown():
    # fresh-signal fetch failed / no creds / shard absent -> fresh signal is None.
    # Concluding dormant here would be a false-dormant on a transient fetch failure.
    r = decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "unknown"


def test_absent_last_active_unavailable_fresh_signal_is_unknown():
    r = decide_liveness(None, None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "unknown"


def test_garbage_fresh_signal_is_unknown_not_dormant():
    # Unparseable fresh signal must degrade to unavailable (unknown), never dormant.
    r = decide_liveness(_ago(days=3), "not-a-timestamp", threshold_hours=6, now=NOW)
    assert r["verdict"] == "unknown"


# --- Threshold boundary ----------------------------------------------------

def test_exactly_at_threshold_is_alive():
    r = decide_liveness(_ago(hours=6), None, threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"  # <= threshold is fresh


def test_just_over_threshold_last_active_falls_through_to_fresh_signal():
    # 6h1m last_active is not fresh; a fresh signal rescues it.
    r = decide_liveness(_ago(hours=6, minutes=1), _ago(minutes=5), threshold_hours=6, now=NOW)
    assert r["verdict"] == "alive"
    assert r["signal"] == "fresh_signal"


def test_custom_threshold_widens_freshness():
    # A 12h last_active is stale at 6h but fresh at a 24h threshold.
    stale6 = decide_liveness(_ago(hours=12), None, threshold_hours=6, now=NOW)
    fresh24 = decide_liveness(_ago(hours=12), None, threshold_hours=24, now=NOW)
    assert stale6["verdict"] == "unknown"      # no fresh signal to fall back on
    assert fresh24["verdict"] == "alive"


# --- _parse_iso / _age tolerance ------------------------------------------

def test_parse_iso_tolerates_quotes_and_z():
    assert _parse_iso('"2026-07-14T09:00:00"') == datetime(2026, 7, 14, 9, 0, 0)
    # Z / offset normalized to naive local — just assert it parses to a datetime.
    assert isinstance(_parse_iso("2026-07-14T09:00:00Z"), datetime)


def test_parse_iso_none_and_empty():
    for v in (None, "", "null", "none", '""'):
        assert _parse_iso(v) is None


def test_age_future_skew_clamped_to_zero():
    # A peer clock slightly ahead must read as fresh (age 0), not negative.
    future = (NOW + timedelta(minutes=3)).isoformat()
    a = _age(future, NOW)
    assert a is not None and a.total_seconds() == 0


def test_age_missing_returns_none():
    assert _age(None, NOW) is None
    assert _age("garbage", NOW) is None


# --- Retirement tombstone dominates freshness () -----------------
# A retired agent's shard SURVIVES (delete-less store) and keeps getting
# written, so shard freshness alone reports a decommissioned agent as alive.
# The retirement write itself refreshes that signal, so retiring an agent made
# it look MORE alive for a full threshold window. Measured on `meta-tiebreaker`
# 2026-07-28: retired_at 17:08:19, authoritative-store push 17:08:20, verdict
# "alive" 2.8h later.

RETIRED = {"retired": True, "retired_at": "2026-07-14T09:00:00", "retired_by": "bravo"}


def test_retired_beats_fresh_shard_signal():
    # The exact production shape: last_active absent (composing the roster drops
    # retired rows), shard push 10 minutes old -> would have been "alive".
    r = decide_liveness(None, _ago(minutes=10), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert r["verdict"] == "retired"
    assert r["signal"] == "retirement_tombstone"


def test_retired_beats_the_fresh_last_active_fast_path():
    # Ordering is load-bearing: an agent retired moments ago STILL has a fresh
    # last_active, so a freshness-first ordering would report it alive.
    r = decide_liveness(_ago(minutes=1), _ago(minutes=1), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert r["verdict"] == "retired"


def test_retired_reason_names_who_and_when():
    r = decide_liveness(None, _ago(minutes=10), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert "2026-07-14T09:00:00" in r["reason"] and "bravo" in r["reason"]


def test_retired_is_not_dormant_so_goals_stay_routed():
    # goal-selector._liveness_confirms_dormant tests `verdict == "dormant"`.
    # "retired" must NOT satisfy it — retired and dormant authorise different
    # things, and False is the fail-safe direction (goals stay routed).
    r = decide_liveness(None, _ago(days=7), threshold_hours=6, now=NOW,
                        retired_entry=RETIRED)
    assert r["verdict"] == "retired"
    assert r["verdict"] != "dormant"


def test_absent_tombstone_preserves_every_existing_verdict():
    # retired_entry defaults to None, so pre-existing callers are byte-identical.
    for la, fs, expected in (
        (_ago(minutes=30), None, "alive"),
        (_ago(days=7), _ago(minutes=4), "alive"),
        (_ago(days=7), _ago(days=7), "dormant"),
        (_ago(days=7), None, "unknown"),
    ):
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW)["verdict"] == expected
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW,
                               retired_entry=None)["verdict"] == expected


def test_revived_agent_is_not_retired_here():
    # _team_state._is_retired owns the revival rule (a heartbeat newer than
    # retired_at un-retires) and is applied by fetch_retirement_tombstone, which
    # then passes None. Assert the pure function honors that contract: given
    # None it must fall through to the freshness verdict, never a sticky retired.
    r = decide_liveness(_ago(minutes=5), None, threshold_hours=6, now=NOW,
                        retired_entry=None)
    assert r["verdict"] == "alive"


# --- Mind vs Body: the shard OBJECT time is not mind liveness (-e) ---
#
# The shard object's write time says "something on that box wrote this shard".
# Under the Mind/Body split that something can be a worker Body while the
# reducer is dead, so object freshness alone must never promote to "alive".


def test_fresh_object_with_stale_authoritative_value_is_not_alive():
    """THE REGRESSION. A worker Body writing the shard refreshes the object while
    the mind's own heartbeat has aged out. Before the fix this returned alive."""
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] != "alive"
    assert r["verdict"] == "unknown"


def test_body_write_does_not_make_a_dead_reducer_dormant_either():
    """guard-1042 + the goal-selector contract. `dormant` is the ONLY verdict
    _liveness_confirms_dormant acts on, so answering dormant here would leak an
    active agent's routed goals cross-agent. Not alive, and not dormant."""
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] != "dormant"


def test_fresh_authoritative_value_is_alive_and_names_its_signal():
    r = decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(minutes=10))
    assert r["verdict"] == "alive"
    assert r["signal"] == "authoritative_last_active"


def test_fresh_authoritative_value_beats_a_stale_object():
    # Mind heartbeating but the object read came back old: still alive. The VALUE
    # is the mind signal; object time is only corroboration.
    r = decide_liveness(None, _ago(days=3), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(minutes=1))
    assert r["verdict"] == "alive"


def test_both_authoritative_and_object_stale_is_still_dormant():
    # Two independent authoritative signals agree the agent is quiet.
    r = decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] == "dormant"


def test_stale_authoritative_with_unreadable_object_is_unknown_not_dormant():
    # The object read failed, so there is no corroboration for a death claim.
    r = decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert r["verdict"] == "unknown"


def test_retirement_still_dominates_a_fresh_authoritative_value():
    # A just-retired agent's last heartbeat is still fresh; retirement wins.
    r = decide_liveness(_ago(minutes=1), _ago(minutes=1), threshold_hours=6, now=NOW,
                        retired_entry={"retired": True, "retired_at": "2026-07-14T09:00:00"},
                        authoritative_last_active_iso=_ago(minutes=1))
    assert r["verdict"] == "retired"


def test_absent_authoritative_value_preserves_every_existing_verdict():
    """Backward-compat twin of test_absent_tombstone_preserves_every_existing_verdict.
    Omitting the new argument must leave every pre-existing caller byte-identical —
    including the legacy object-freshness-implies-alive row, which is still correct
    when no mind signal could be read at all."""
    for la, fs, expected in (
        (_ago(minutes=30), None, "alive"),
        (_ago(days=7), _ago(minutes=4), "alive"),
        (_ago(days=7), _ago(days=7), "dormant"),
        (_ago(days=7), None, "unknown"),
    ):
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW)["verdict"] == expected
        assert decide_liveness(la, fs, threshold_hours=6, now=NOW,
                               authoritative_last_active_iso=None)["verdict"] == expected


def test_authoritative_age_is_reported_in_every_result():
    # The new field must be present on all verdicts so callers can log why.
    r = decide_liveness(_ago(days=7), _ago(minutes=4), threshold_hours=6, now=NOW,
                        authoritative_last_active_iso=_ago(days=7))
    assert "authoritative_last_active_age_min" in r
    assert r["authoritative_last_active_age_min"] > 6 * 60
    r2 = decide_liveness(_ago(minutes=5), None, threshold_hours=6, now=NOW)
    assert r2["authoritative_last_active_age_min"] is None


# --- INTEGRATION PATH: shard on disk -> fetch_retirement_tombstone -> main ---
#
# Everything above passes `retired_entry` in by hand, so none of it can observe
# whether main() still CALLS fetch_retirement_tombstone. Measured ():
# delete that call and all 22 pure tests stay green while the feature is gone.
# These tests never mention retired_entry — the tombstone has to travel from a
# real YAML file through _team_state._is_retired and into the verdict on its
# own, so the expected value is produced by the components under test rather
# than restated by the test (guard-1220).
#
# INT_NOW is deliberately distinct from NOW above: these read absolute ISO
# strings out of a shard, and _is_retired compares retired_at/last_active as
# STRINGS, so the fixtures must be readable as literals rather than as offsets.

INT_NOW = "2026-08-10T02:00:00"
INT_RETIRED_AT = "2026-08-10T00:00:00"


def _write_shard(world_dir, agent, entry):
    """Write a team-state shard exactly where fetch_retirement_tombstone looks."""
    import yaml
    d = os.path.join(world_dir, "team-state", "agents")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{agent}.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(entry, fh)


def _run_main(capsys, world_dir, agent, last_active):
    """Drive main() end-to-end and return its parsed JSON verdict."""
    import json
    rc = main(["--agent", agent, "--world-dir", world_dir, "--json",
               "--now", INT_NOW, "--last-active", last_active])
    assert rc == 0, "main must always exit 0 — the verdict is the signal"
    return json.loads(capsys.readouterr().out)


def test_integration_tombstone_on_disk_yields_retired(tmp_path, capsys, monkeypatch):
    """The wiring test the pure suite cannot provide.

    --last-active is deliberately FRESH (1 minute old). That is the strongest
    form: main() short-circuits the expensive reads on a fresh last_active, so a
    verdict of "retired" here proves the tombstone is consulted on the fast path
    too — which is the exact inversion g-115-3702 was filed about, where the
    retirement write itself refreshes the signal and makes a decommissioned
    agent look MORE alive.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955
    world = str(tmp_path / "world")
    _write_shard(world, "ghost", {
        "retired": True,
        "retired_at": INT_RETIRED_AT,
        "retired_by": "bravo",
        # older than retired_at -> _is_retired stays True
        "last_active": "2026-08-09T12:00:00",
    })
    out = _run_main(capsys, world, "ghost", last_active="2026-08-10T01:59:00")
    assert out["verdict"] == "retired"
    # The reason must carry the tombstone's own fields, proving the verdict came
    # from the file rather than from a default.
    assert "bravo" in out["reason"]
    assert INT_RETIRED_AT in out["reason"]


def test_integration_revived_shard_reads_alive(tmp_path, capsys, monkeypatch):
    """Revival case: a heartbeat NEWER than retired_at un-retires the row.

    Same shard shape as above with one field changed, so a failure here isolates
    the revival rule rather than the plumbing.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    world = str(tmp_path / "world")
    _write_shard(world, "revived", {
        "retired": True,
        "retired_at": INT_RETIRED_AT,
        "retired_by": "bravo",
        # NEWER than retired_at -> _is_retired returns False
        "last_active": "2026-08-10T01:00:00",
    })
    out = _run_main(capsys, world, "revived", last_active="2026-08-10T01:59:00")
    assert out["verdict"] == "alive"


def test_integration_absent_shard_falls_through_without_error(tmp_path, capsys, monkeypatch):
    """Fail-open: no shard at all must not raise and must not fabricate a
    retirement. Guards the direction that would break every non-retired agent."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    world = str(tmp_path / "world")
    os.makedirs(world, exist_ok=True)
    out = _run_main(capsys, world, "nobody", last_active="2026-08-10T01:59:00")
    assert out["verdict"] == "alive"


# --- The IO half on its own: fetch_retirement_tombstone --------------------
#
# The main()-level tests above cover the WIRING, but only one of them is killed
# by severing that wiring: the two that expect "alive" expect the same verdict
# the severed code produces, so they document behaviour without discriminating
# (guard-1793 — mutate against the assertion, not the suite). These test the
# reader directly, which is the other gap  names, and they give the
# revival rule an assertion that cannot pass vacuously.


def test_fetch_tombstone_returns_entry_for_a_retired_shard(tmp_path):
    world = str(tmp_path / "world")
    _write_shard(world, "ghost", {
        "retired": True, "retired_at": INT_RETIRED_AT, "retired_by": "bravo",
        "last_active": "2026-08-09T12:00:00",
    })
    entry = fetch_retirement_tombstone("ghost", world)
    assert entry is not None
    assert entry["retired_by"] == "bravo"


def test_fetch_tombstone_returns_none_when_heartbeat_is_newer(tmp_path):
    """The revival rule, read off disk. Same shard as above with last_active
    moved past retired_at — this is the assertion the main()-level revival test
    cannot make, because there "alive" is also the severed-wiring answer."""
    world = str(tmp_path / "world")
    _write_shard(world, "revived", {
        "retired": True, "retired_at": INT_RETIRED_AT, "retired_by": "bravo",
        "last_active": "2026-08-10T01:00:00",
    })
    assert fetch_retirement_tombstone("revived", world) is None


def test_fetch_tombstone_returns_none_for_a_live_agent(tmp_path):
    world = str(tmp_path / "world")
    _write_shard(world, "busy", {"last_active": "2026-08-10T01:00:00"})
    assert fetch_retirement_tombstone("busy", world) is None


def test_fetch_tombstone_fails_open_on_absent_and_unreadable_shards(tmp_path):
    """Fail-open by contract: a missing shard and malformed YAML must both yield
    None rather than raising, because this runs inside every liveness read.
    Note the absent case is NOT evidence an agent is live — under own-cloud the
    local tree is a read-through cache (guard-980)."""
    world = str(tmp_path / "world")
    os.makedirs(os.path.join(world, "team-state", "agents"), exist_ok=True)
    assert fetch_retirement_tombstone("never-written", world) is None

    bad = os.path.join(world, "team-state", "agents", "broken.yaml")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{[not: valid yaml at all\n")
    assert fetch_retirement_tombstone("broken", world) is None


# --- CROSS-AGENT ROW STAMP (, guard-3604) -----------------------
#
# team-state-clear-in-flight.sh sets `last_active = now` and stamps
# `row_updated_by = <the clearer>`, so POLICING a dormant peer's stranded claim
# makes that peer read ALIVE for a full threshold window. Measured 2026-08-13 on
# echo (dormant at 419.1min -> alive at 1.8min, row_updated_by=bravo, echo never
# having woken) and 2026-08-16 (foxtrot dead 11.64h while reading alive).
#
# The bump itself is CORRECT and is not what these tests protect: shard merges
# are whole-snapshot LWW on last_active, so an unstamped pop loses the merge and
# resurrects the cleared claim. What is wrong is reading its side effect as
# evidence about the subject.

STAMP_KW = dict(row_updated_by="bravo", row_agent="echo")


def test_cross_stamped_fresh_last_active_is_not_alive():
    """The measured 2026-08-16 shape: a dormant peer must NOT read alive."""
    r = decide_liveness(_ago(minutes=1.8), None, threshold_hours=6, now=NOW, **STAMP_KW)
    assert r["verdict"] != "alive", "a row stamped by another agent cannot certify its subject alive"
    assert r["verdict"] == "unknown", "and it is NOT dormant either — the peer may be alive"
    # The verdict must name the actual stamper, so a reader can act on
    # guard-3604's 'post a correction naming the peer' without re-deriving it.
    assert "bravo" in r["reason"] and "echo" in r["reason"]


def test_cross_stamp_also_blocks_the_authoritative_fall_through():
    """The finding that shaped this fix — measured BEFORE writing it.

    Disqualifying only the fast path and 'falling through to the authoritative
    read' does NOT work: the bumped value is exactly what that read returns,
    because it must reach the store to win the LWW merge. Pre-fix that path
    returned alive/authoritative_last_active — and WORSE than the original,
    since provenance moved null -> "authoritative", erasing the `provenance:
    null` tell guard-3604 names as the self-concealing signature, under a reason
    string asserting "the mind is running".

    So this asserts the SECOND door is shut, not just the first.

    CASE B IS THE ONE THAT DISCRIMINATES, and case A alone silently did not —
    caught by running the mutation instead of asserting it. With both signals
    fresh (case A, the same-box shape) an `la_age`-only guard STILL fires, so
    that case cannot tell the scoped-correctly fix from the literal remedy. The
    mirror-lag shape does: another box cleared the row, this box's composed
    `last_active` is still the pre-clear stale value, and only the authoritative
    read returns the bump. Mutation-proven: scope the guard to `la_age` alone
    and case B goes red.
    """
    bumped = _ago(minutes=1.8)
    # (A) same box: the clear bumped the value this box already reads.
    ra = decide_liveness(bumped, None, threshold_hours=6, now=NOW,
                         authoritative_last_active_iso=bumped,
                         authoritative_provenance="authoritative", **STAMP_KW)
    assert ra["verdict"] == "unknown"
    assert ra["signal"] is None, "must not be promoted via authoritative_last_active"

    # (B) another box cleared it; this mirror has not synced. Stale locally,
    #     bumped in the store — the exact door the literal remedy leaves open.
    rb = decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW,
                         authoritative_last_active_iso=bumped,
                         authoritative_provenance="authoritative", **STAMP_KW)
    assert rb["verdict"] == "unknown", (
        "a stale local last_active with a cross-stamped fresh AUTHORITATIVE value "
        "must not promote to alive — this is the fall-through the goal prescribed")
    assert rb["signal"] is None


def test_cross_stamp_blocks_fresh_signal_promotion():
    """Third door: the shard OBJECT time is the same foreign write's push.

    With a stale/absent last_active and no authoritative value, a fresh object
    time promotes to alive/fresh_signal. The clearer's push produces exactly
    that, so the guard must cover it too.
    """
    r = decide_liveness(None, _ago(minutes=2), threshold_hours=6, now=NOW, **STAMP_KW)
    assert r["verdict"] == "unknown"
    assert r["signal"] is None


def test_same_agent_stamp_keeps_the_fast_path():
    """Outcome 2: a self-stamped row is untouched and pays nothing.

    This is the common case — all five live agents self-stamp — so a guard that
    caught it would degrade every routine liveness read.
    """
    r = decide_liveness(_ago(minutes=30), None, threshold_hours=6, now=NOW,
                        row_updated_by="echo", row_agent="echo")
    assert r["verdict"] == "alive"
    assert r["signal"] == "last_active"
    # provenance stays null => no authoritative store read was consulted
    assert r["authoritative_last_active_provenance"] is None


def test_unknown_row_agent_disqualifies_nothing():
    """Backward compatibility: a caller that supplies neither field, or only the
    stamp, must behave exactly as before. An unknown owner cannot establish that
    a stamp is foreign."""
    for kw in ({}, {"row_updated_by": "bravo"}, {"row_agent": "echo"},
               {"row_updated_by": None, "row_agent": None}):
        r = decide_liveness(_ago(minutes=30), None, threshold_hours=6, now=NOW, **kw)
        assert r["verdict"] == "alive", f"pre-existing caller shape changed: {kw}"


def test_aged_out_cross_stamp_still_reaches_dormant():
    """The SCOPING half, and the reason the guard tests freshness rather than
    firing on the stamp alone.

    A cross-stamp can only ever manufacture a false ALIVE. Once every signal has
    aged past the threshold the artifact is spent, and dormant is again the
    supported conclusion. Without this scoping, policing a peer ONCE would render
    it permanently unjudgeable — and 'dormant' is the only verdict that lets
    goal-selector reclaim its stranded goals.
    """
    r = decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW, **STAMP_KW)
    assert r["verdict"] == "dormant"


def test_cross_stamp_never_overrides_retirement():
    """Retirement dominates: a retired row stamped by its retirer stays retired,
    not unknown. Ordering regression — the guard sits BELOW the tombstone check."""
    r = decide_liveness(_ago(minutes=1), None, threshold_hours=6, now=NOW,
                        retired_entry={"retired_at": "2026-07-28T17:08:19", "retired_by": "bravo"},
                        **STAMP_KW)
    assert r["verdict"] == "retired"


# --- Cross-stamp WIRING: shard on disk -> fetch_row_stamp -> main ----------
#
# Same gap as the tombstone section above: every pure test hands the stamp in by
# hand, so none of them notices if main() stops CALLING fetch_row_stamp. These
# drive it from a real YAML file.

def test_integration_cross_stamped_shard_is_not_alive(tmp_path, capsys, monkeypatch):
    """--last-active is deliberately FRESH, which is the whole point: main()
    short-circuits the expensive reads on a fresh last_active, so the stamp has
    to be consulted on the fast path it disqualifies. Delete the fetch_row_stamp
    call from main() and this is the test that dies."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955
    world = str(tmp_path / "world")
    _write_shard(world, "echo", {
        "last_active": "2026-08-10T01:58:00",   # bumped, fresh vs INT_NOW
        "row_updated": "2026-08-10T01:58:00",
        "row_updated_by": "bravo",              # ...by the CLEARER
    })
    out = _run_main(capsys, world, "echo", last_active="2026-08-10T01:58:00")
    assert out["verdict"] == "unknown"
    assert out["row_updated_by"] == "bravo", "the stamp must be surfaced structurally"


def test_integration_self_stamped_shard_pays_no_store_read(tmp_path, capsys, monkeypatch):
    """Outcome 2, proven by construction rather than by inspection.

    Both authoritative fetches are replaced with raisers. A same-agent row must
    still return alive — if the guard ever forced a store read on the common
    path, this raises instead of passing.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    import liveness_check as lc

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("fast path must not touch the authoritative store")
    monkeypatch.setattr(lc, "fetch_authoritative_last_active_with_provenance", _boom)
    monkeypatch.setattr(lc, "fetch_fresh_signal", _boom)

    world = str(tmp_path / "world")
    _write_shard(world, "echo", {
        "last_active": "2026-08-10T01:58:00",
        "row_updated_by": "echo",               # self-stamped
    })
    out = _run_main(capsys, world, "echo", last_active="2026-08-10T01:58:00")
    assert out["verdict"] == "alive"
    assert out["authoritative_last_active_provenance"] is None


# --- The IO half on its own: fetch_row_stamp ------------------------------

def test_fetch_row_stamp_reads_the_stamp(tmp_path):
    from liveness_check import fetch_row_stamp
    world = str(tmp_path / "world")
    _write_shard(world, "echo", {"row_updated_by": "bravo"})
    # Arg order is (agent, world_dir) — matching fetch_retirement_tombstone, its
    # sibling reader of the same file. Both callers pass it positionally.
    assert fetch_row_stamp("echo", world) == "bravo"


def test_fetch_row_stamp_fails_open(tmp_path):
    """Absent shard, unreadable YAML, and an absent key all return None rather
    than raising — a liveness read must never be blocked by this probe."""
    from liveness_check import fetch_row_stamp
    world = str(tmp_path / "world")
    os.makedirs(world, exist_ok=True)
    assert fetch_row_stamp("nobody", world) is None          # no shard
    _write_shard(world, "nokey", {"last_active": "2026-08-10T01:00:00"})
    assert fetch_row_stamp("nokey", world) is None           # no row_updated_by
    d = os.path.join(world, "team-state", "agents")
    with open(os.path.join(d, "broken.yaml"), "w", encoding="utf-8") as fh:
        fh.write("{[not valid yaml")
    assert fetch_row_stamp("broken", world) is None          # unparseable
