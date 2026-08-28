""" — threshold-crossing conflict alerts from the own-cloud sweep.

The sweep is the only wedge producer running on WALL-CLOCK cadence, so it is the
only one that can surface a wedge on a box whose loop is idle. These tests pin
the two properties the alert has to have to be usable — exactly one post per
episode, and a genuinely NEW episode after a reset — plus the compatibility
property that makes storing the marker in the streaks artifact safe at all.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ocs = _load("_ocs_alert_uut", "owncloud_sync.py")
mirror_health = _load("_mh_alert_uut", "mirror_health.py")

T = ocs._CONFLICT_ALERT_THRESHOLD
KEY = ocs._CONFLICT_ALERTED_KEY
P = "world/knowledge/tree/system/wedged-node.md"
Q = "world/conventions/other.md"


# ── the crossing itself ────────────────────────────────────────────────────

def test_below_threshold_does_not_cross():
    crossings, carried = ocs._conflict_alert_transitions({}, {P: T - 1})
    assert crossings == []
    assert carried == {}


def test_reaching_threshold_crosses_exactly_once():
    crossings, carried = ocs._conflict_alert_transitions({}, {P: T})
    assert crossings == [P]
    assert carried == {}


def test_crossing_is_from_below_not_at_every_sweep_past_it():
    """The property the whole design turns on: a wedge sits past the threshold
    for HUNDREDS of sweeps (measured 400-1283), so a naive `>= threshold` test
    would post on every one of them."""
    old = {P: T, KEY: {P: "2026-08-27T11:00:00"}}
    crossings, carried = ocs._conflict_alert_transitions(old, {P: T + 500})
    assert crossings == []
    assert carried == {P: "2026-08-27T11:00:00"}


def test_idempotent_across_many_subsequent_sweeps():
    state = {}
    posts = 0
    for sweep in range(1, T + 40):
        crossings, carried = ocs._conflict_alert_transitions(state, {P: sweep})
        posts += len(crossings)
        for k in crossings:
            carried[k] = "t%d" % sweep
        state = {P: sweep}
        if carried:
            state[KEY] = carried
    assert posts == 1, "one post per episode, not one per sweep"


# ── episode semantics ──────────────────────────────────────────────────────

def test_reset_drops_the_marker_and_recross_posts_again():
    alerted = {P: "2026-08-27T11:00:00"}
    # The path reconciles: the sweep rebuilds `new` from THIS sweep's conflicts,
    # so it is simply absent.
    crossings, carried = ocs._conflict_alert_transitions({P: T, KEY: alerted}, {})
    assert crossings == []
    assert carried == {}, "marker must leave with the streak"
    # ... and a later re-wedge is a NEW episode.
    crossings2, _ = ocs._conflict_alert_transitions({KEY: carried}, {P: T})
    assert crossings2 == [P]


def test_one_paths_reset_does_not_clear_anothers_marker():
    old = {P: T + 3, Q: T + 3, KEY: {P: "t1", Q: "t1"}}
    crossings, carried = ocs._conflict_alert_transitions(old, {Q: T + 4})
    assert crossings == []
    assert carried == {Q: "t1"}


def test_multiple_simultaneous_crossings_are_all_reported_sorted():
    crossings, _ = ocs._conflict_alert_transitions({}, {Q: T, P: T + 1})
    assert crossings == sorted([P, Q])


# ── robustness of the shared artifact ──────────────────────────────────────

@pytest.mark.parametrize("junk", [None, [], "nope", 3])
def test_malformed_marker_map_degrades_to_no_markers(junk):
    """A corrupt marker must re-alert (noisy), never suppress (silent)."""
    crossings, carried = ocs._conflict_alert_transitions({P: T, KEY: junk}, {P: T})
    assert crossings == [P]
    assert carried == {}


def test_non_int_streak_values_never_cross():
    crossings, _ = ocs._conflict_alert_transitions({}, {P: "99"})
    assert crossings == []


# ── the compatibility property the in-file marker depends on ───────────────

def test_reserved_key_does_not_disturb_mirror_health_verdict():
    """mirror_health guards both comprehensions with isinstance(v, int); this
    pins that, because the whole in-file-marker decision rests on it."""
    without = mirror_health.classify({P: T}, age_min=1.0)
    with_marker = mirror_health.classify(
        {P: T, KEY: {P: "2026-08-27T11:00:00"}}, age_min=1.0)
    assert with_marker["verdict"] == without["verdict"] == "wedged"
    assert with_marker["wedged_count"] == without["wedged_count"] == 1
    assert KEY not in with_marker["files"]


def test_reserved_key_alone_reads_healthy_not_wedged():
    v = mirror_health.classify({KEY: {P: "t"}}, age_min=1.0)
    assert v["verdict"] == "healthy"
    assert v["wedged_count"] == 0


def test_reserved_key_cannot_collide_with_a_repo_relative_path():
    assert KEY.startswith("#")


# ── end-to-end through the real bookkeeping function ───────────────────────

def _sweep(tmp_path, monkeypatch, paths, posted_ok=True):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    calls = []

    def fake_post(rel_path, streak):
        calls.append((rel_path, streak))
        return posted_ok

    monkeypatch.setattr(ocs, "_post_conflict_alert", fake_post)
    stats = {"conflict_paths": list(paths)}
    ocs._update_conflict_streaks(stats)
    art = json.loads((tmp_path / "owncloud-conflict-streaks.json").read_text())
    return stats, art, calls


def test_end_to_end_posts_once_then_stays_quiet(tmp_path, monkeypatch):
    total = []
    for _ in range(T + 5):
        _, art, calls = _sweep(tmp_path, monkeypatch, [P])
        total += calls
    assert len(total) == 1
    assert art[KEY][P]
    assert art[P] == T + 5


def test_end_to_end_failed_post_retries_next_sweep(tmp_path, monkeypatch):
    """A failed post must not consume the episode — the alert is at-least-once."""
    for _ in range(T):
        _, art, calls = _sweep(tmp_path, monkeypatch, [P], posted_ok=False)
    assert calls == [(P, T)]
    assert KEY not in art, "no marker may be written for a post that failed"
    _, art2, calls2 = _sweep(tmp_path, monkeypatch, [P], posted_ok=True)
    assert calls2 == [(P, T + 1)], "next sweep retries"
    assert art2[KEY][P]


def test_end_to_end_stats_expose_the_alerted_set(tmp_path, monkeypatch):
    for _ in range(T):
        stats, _, _ = _sweep(tmp_path, monkeypatch, [P])
    assert stats["conflict_alerted"] == [P]
    assert stats["conflict_persistent"] == [P]


# ── the routing contract (fresh-eyes finding, ) ──────────────────
# The alert is only worth posting if a consumer ROUTES it. insight-trigger-gate
# skips self-authored findings, and board-post.sh defaults the author to
# $MIND_AGENT — so the obvious implementation posts as the very agent it
# addresses and is dropped in silence, looking delivered the whole time. This
# pins the author choice with a positive control, so a future edit that drops
# --author fails here instead of quietly disabling the mechanism.

def _gate():
    spec = importlib.util.spec_from_file_location(
        "_itg_uut", SCRIPTS / "insight-trigger-gate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_itg_uut"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    mod._already_processed = lambda _id: False
    return mod


_ALERT_TAGS = ["insight_trigger", "severity:constrains",
               "affects:%s" % P, "mirror-wedge", "requires_action_by:zeta"]


def test_alert_tag_shape_routes_through_the_real_gate():
    g = _gate()
    rec = {"id": "msg-1", "author": "owncloud-sync", "tags": _ALERT_TAGS, "text": "w"}
    assert len(g._collect_triggers([rec], "zeta")) == 1


def test_positive_control_self_authored_alert_is_dropped():
    """The pre-fix shape. If this ever starts passing, the gate's self-trigger
    skip changed and the --author workaround may no longer be needed."""
    g = _gate()
    rec = {"id": "msg-2", "author": "zeta", "tags": _ALERT_TAGS, "text": "w"}
    assert g._collect_triggers([rec], "zeta") == []


def test_the_poster_actually_passes_a_non_agent_author():
    """Guards the specific line the finding was about."""
    src = (SCRIPTS / "owncloud_sync.py").read_text(encoding="utf-8")
    body = src[src.index("def _post_conflict_alert"):]
    body = body[:body.index("\ndef ")] if "\ndef " in body else body
    assert '"--author", "owncloud-sync"' in body
