""": a gracefully-stopped reducer/observer Body leaves a CLOSED carrier.

THE DEFECT. A reducer has a body-manifest.yaml but `forked_wm_hash: null`, so
close_body_on_genuine returns 'no-forked-wm' and never transitions it. Measured
on the live box (alpha, cc-04, 2026-09-15): manifest role 'reducer' /
body_state 'active', carrier "body_state":"active". Nothing runs at graceful
stop, so both files stay 'active' until the stale-binding sweep eventually
arrives -- and in that window worker_stall grades the carrier stalled_no_close
and ALERTS. Every fleet stop minted a fresh cohort of those.

THE FIX IS A MISSING CALL PLUS A DISTINCT VALUE, NOT A NEW WRITER.
close_body_late already closes this population (its 'marked-stale' branch), and
set_state already mirrors the manifest write to the carrier. What it lacked was
a value that is CLOSED without being byte-identical to
orphan_carrier_repair.REPAIR_STATE -- which would hide a clean stop among the
sweep's own abandoned specimens.

guard-4166 governs the shape below: a classifier that graded EVERYTHING benign
would satisfy every benign assertion here, so the classifier tests carry a
negative control at the same carrier age.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))

import worker_stall as ws  # noqa: E402
import _frontier  # noqa: E402
import orphan_carrier_repair as ocr  # noqa: E402

_spec = importlib.util.spec_from_file_location("body_manifest", CORE / "body-manifest.py")
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)

AGENT = "alpha"
RED = "aaaaaaaa-0000-4000-8000-00000000f001"   # the reducer being stopped
GRACEFUL = "closed-graceful"
STALE_AGE = ws.DEFAULT_STALE_MINUTES * 4       # unambiguously stale


@pytest.fixture(autouse=True)
def _hermetic_world(tmp_path, monkeypatch):
    """Never let a producer test reach the live world (guard-955)."""
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w


def _reducer_body(root: Path, sid: str) -> None:
    """A reducer: manifest present, NO forked working-memory.yaml."""
    # _agent_paths refuses a root whose agent dir does not exist, so build it.
    (root / "agents" / AGENT / "sessions" / sid).mkdir(parents=True, exist_ok=True)
    (root / "agents" / AGENT / "session").mkdir(parents=True, exist_ok=True)
    bm.write_manifest(sid, AGENT, role="reducer", project_root=root)
    _, session_dir, state_dir = bm._agent_paths(AGENT, sid, root)
    assert not (session_dir / bm._WM_FILENAME).is_file(), "fixture must have no forked WM"
    # Write the carrier the way heartbeat-tick.sh does. _mirror_state_to_carrier
    # is a MIRROR -- it updates an existing carrier and does not mint one -- so
    # seeding it here is what makes this fixture match a real running Body.
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"body-heartbeat-{sid}.json").write_text(json.dumps({
        "sid": sid, "agent": AGENT, "host": "box-test",
        "ts": "2026-09-15T00:00:00", "body_state": "active",
        "machine_id": "box-test",
    }) + "\n", encoding="utf-8")


def _carrier(root: Path, sid: str) -> dict:
    _, _, state_dir = bm._agent_paths(AGENT, sid, root)
    return json.loads((state_dir / f"body-heartbeat-{sid}.json").read_text(encoding="utf-8"))


def _manifest_state(root: Path, sid: str) -> str:
    return bm.read_manifest(sid, AGENT, root).get("body_state")


# ── the partition: five sites, two of them pinned by nothing else ────────────

def test_graceful_is_closed_not_closeable_in_the_ssot():
    assert GRACEFUL in bm.VALID_STATES, "set_state would raise on it"
    assert GRACEFUL in bm.CLOSED_STATES
    assert GRACEFUL not in bm.CLOSEABLE_STATES


def test_graceful_reached_both_unpinned_mirrors():
    """test_state_partition_matches_body_manifest pins worker_stall<->SSOT.

    It does NOT pin these two, so they are the half that can silently drift.
    """
    assert GRACEFUL in ws.CLOSED_BODY_STATES          # pinned elsewhere; asserted here too
    assert GRACEFUL in _frontier.CLOSED_BODY_STATES   # NOT pinned elsewhere
    assert GRACEFUL not in _frontier.ACTIVE_BODY_STATES

    hook = (CORE / "stop-hook.sh").read_text(encoding="utf-8")
    pattern = re.search(r"\^body_state: .*?\(([^)]*)\)", hook)
    assert pattern, "stop-hook's closed-state grep not found -- it moved or was renamed"
    alternatives = pattern.group(1).split("|")
    assert GRACEFUL in alternatives, (
        "stop-hook.sh's closed-state grep is the 5th partition site and nothing "
        "else pins it; a Body closed gracefully must stand the worker net down")


# ── outcome 2: the stall classifier stops alerting ───────────────────────────

def test_stale_graceful_carrier_is_not_a_stall():
    verdict = ws.classify_body(carrier_age_minutes=STALE_AGE, holds_live_claim=False,
                               body_state=GRACEFUL)
    assert verdict not in ws.ALERTING_VERDICTS
    assert verdict == ws.V_STALE_NO_CLAIM


def test_negative_control_active_at_the_same_age_still_alerts():
    """Without this, the test above passes on a classifier that never alerts."""
    verdict = ws.classify_body(carrier_age_minutes=STALE_AGE, holds_live_claim=False,
                               body_state="active")
    assert verdict == ws.V_STALLED_NO_CLOSE
    assert verdict in ws.ALERTING_VERDICTS


def test_a_live_reducer_is_alive_on_freshness_whatever_its_state():
    """Outcome 4's structural half: freshness is read BEFORE body_state."""
    assert ws.classify_body(carrier_age_minutes=0.0, holds_live_claim=False,
                            body_state="active") == ws.V_ALIVE


# ── the writer: both files, and only the sid it was given ────────────────────

def test_graceful_close_writes_manifest_and_carrier(tmp_path):
    _reducer_body(tmp_path, RED)
    assert _manifest_state(tmp_path, RED) == "active"
    assert _carrier(tmp_path, RED)["body_state"] == "active"

    verdict = bm.close_body_late(RED, AGENT, tmp_path, no_wm_state=GRACEFUL)

    assert verdict == "marked-graceful"
    assert _manifest_state(tmp_path, RED) == GRACEFUL
    # The carrier is the assertion that matters -- worker_stall reads it, and it
    # is what survives when the session dir is removed.
    assert _carrier(tmp_path, RED)["body_state"] == GRACEFUL


def test_graceful_close_touches_only_the_sid_it_was_given(tmp_path):
    """Outcome 4: the close is ADDRESSED, so a live peer Body is never swept."""
    other = "bbbbbbbb-0000-4000-8000-00000000f002"
    _reducer_body(tmp_path, RED)
    _reducer_body(tmp_path, other)

    bm.close_body_late(RED, AGENT, tmp_path, no_wm_state=GRACEFUL)

    assert _carrier(tmp_path, other)["body_state"] == "active"
    assert _manifest_state(tmp_path, other) == "active"


# ── outcomes 3 and 5: the sweep's own specimens stay recognisable ────────────

def test_default_close_is_byte_identical_to_its_historical_behaviour(tmp_path):
    _reducer_body(tmp_path, RED)
    assert bm.close_body_late(RED, AGENT, tmp_path) == "marked-stale"
    assert _manifest_state(tmp_path, RED) == "closed-stale"


def test_graceful_is_distinguishable_from_the_repair_state():
    assert ocr.REPAIR_STATE == "closed-stale"
    assert GRACEFUL != ocr.REPAIR_STATE, (
        "a clean stop must not be masked among orphan_carrier_repair's own "
        "abandoned specimens (outcome 3)")
    # Outcome 5: the repair path never learns the new value, so its selection on
    # a >=3.0d abandoned carrier is unchanged by construction -- and because
    # select_rows requires body_state to be a LIVE state, a carrier this change
    # closes is excluded from that sweep rather than fought over by it.
    assert ocr.DEFAULT_MIN_AGE_DAYS == 3.0
    assert GRACEFUL not in bm.CLOSEABLE_STATES


# ── the CLI verb the bash graceful-stop caller uses ──────────────────────────

def test_cli_graceful_flag_passes_the_state_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(bm, "close_body_late",
                        lambda sid, agent, **kw: seen.update(kw) or "marked-graceful")
    bm.main(["close-body-late", "--sid", RED, "--agent", AGENT, "--graceful"])
    assert seen == {"no_wm_state": GRACEFUL}


def test_cli_without_the_flag_passes_no_kwarg_at_all(monkeypatch):
    """The reap's call shape must stay the two-positional-arg form it has always been."""
    seen = {}
    monkeypatch.setattr(bm, "close_body_late",
                        lambda sid, agent, **kw: seen.update(kw) or "marked-stale")
    bm.main(["close-body-late", "--sid", RED, "--agent", AGENT])
    assert seen == {}


# ── outcome 5: the >=3.0d repair path is unchanged, measured not asserted ────

def _fact(sid, state, age_minutes, agent=AGENT, host="box-a", held=None):
    return {"agent": agent, "sid": sid, "host": host,
            "age_minutes": age_minutes, "body_state": state, "held_goal": held}


MIN_AGE_MIN = ocr.DEFAULT_MIN_AGE_DAYS * 24 * 60


def test_abandoned_active_carrier_is_still_selected_for_repair():
    """The population orphan_carrier_repair exists for must still be selected.

    The live dry-run on cc-04 could NOT show this -- it read selected=0 against
    carriers_found=86 because every carrier on the box is already closed-stale,
    so the live-state clause excludes them all and no specimen existed. A zero
    from an empty population is not a positive control, so the specimen is
    synthesised here instead.
    """
    selected, excluded = ocr.select_rows(
        [_fact("11111111-0000-4000-8000-00000000f0aa", "active", MIN_AGE_MIN + 1)],
        MIN_AGE_MIN, AGENT)
    assert len(selected) == 1, f"the repair stopped selecting its own population: {excluded}"
    assert ocr.REPAIR_STATE == "closed-stale"


def test_a_gracefully_closed_carrier_is_never_re_repaired():
    """What the partition change NEWLY does here, stated as the intended effect.

    live_body_state routes through ws.CLOSED_BODY_STATES deliberately ("a future
    state joins CLOSED_BODY_STATES or it does not, and this predicate must
    inherit that answer rather than fork it"), so adding closed-graceful there
    excludes a cleanly-stopped Body from the orphan sweep -- correct, and the
    reason the two modules must not fight over the same carrier.
    """
    assert ocr.live_body_state("active") is True
    assert ocr.live_body_state(GRACEFUL) is False

    selected, excluded = ocr.select_rows(
        [_fact("22222222-0000-4000-8000-00000000f0bb", GRACEFUL, MIN_AGE_MIN + 1)],
        MIN_AGE_MIN, AGENT)
    assert selected == []
    assert any("not-a-LIVE-body_state" in r for r in excluded[0]["exclusion_reasons"])


def test_daemon_claim_probe_is_the_sixth_partition_site(): 
    """: the daemon's cross-box carrier probe is partition site SIX.

    g-115-9957 enumerated the five sites under `core/scripts` and shipped
    `closed-graceful` to all of them. It never swept `mind_api/src`, where the
    daemon's claim path keeps its OWN hand-written closed-set. The consequence
    was not cosmetic: that probe returns False (= not live, claim takeable) for
    a closed state, so an unlisted `closed-graceful` fell through to the
    freshness test and reported a gracefully-stopped Body LIVE for the whole
    staleness window, holding its claims un-takeable.

    Found by /fresh-eyes-code reviewing g-115-9957's own commits. This is
    guard-1127 (enumerate ALL consumers before widening a shared constant) with
    the scope lesson attached: the enumeration must be REPO-WIDE, not limited to
    the directory being edited.

    Read as SOURCE, not by import -- importing the daemon endpoint drags in the
    whole mind_api app. Same technique this file already uses for the stop-hook
    grep, and it pins the literal that actually ships.
    """
    root = CORE.parent.parent
    src = (root / "mind_api" / "src" / "endpoints" / "aspirations_write.py").read_text(
        encoding="utf-8")
    m = re.search(
        r'body_state.{0,40}?\)\s*in\s*\(\s*((?:\s*"[a-z-]+",?)+)\s*\)', src, re.S)
    assert m, ("the daemon claim probe's closed-set tuple was not found -- it "
               "moved or was renamed; re-find it rather than deleting this test")
    listed = set(re.findall(r'"([a-z-]+)"', m.group(1)))
    missing = set(bm.CLOSED_STATES) - listed
    assert not missing, (
        f"mind_api claim probe is missing {sorted(missing)} from its closed set. "
        f"It is the SIXTH declaration of the body_state partition and body-manifest's "
        f"CLOSED_STATES is the SSOT: every member must appear here or a Body in that "
        f"state reads LIVE to the daemon and its claims stay un-takeable.")
    assert GRACEFUL in listed, "closed-graceful specifically must be listed (g-115-10026)"
