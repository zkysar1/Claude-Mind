"""stranded-claim-sweep: holder liveness after the breadcrumb stopped counting (2026-08-30).

Measured on coach (zc-03, 8 zakcode sessions): the claim-time diary breadcrumb
(g-115-6677) satisfied the sweep's "diary entry after claimed_at" predicate for
every claim by construction, so all 6 in-progress claims read
`possible-displacement (do NOT auto-act)` — including one held by a session dead
for 18 h — and nothing was ever released. Meanwhile 4 of the 5 LIVE holders had
no body carrier (a worker mid-unit never writes one) and no diary rows (Phase 4
writes none before close), so once the breadcrumb is ignored the sweep needs a
liveness signal it can actually observe: the holder's own transcript on this box.

Pins:
  * _scan_diary_text ignores the breadcrumb and still sees a real row
  * _holder_transcript_verdict: zakcode doc fresh -> alive; old -> stale;
    no transcript -> absent; unreadable doc -> unreadable; Claude Code
    transcript wins when present; a probe never raises
"""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("stranded_claim_sweep_holder_ut", SCRIPTS / "stranded-claim-sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)

NOW = dt.datetime(2026, 8, 30, 4, 40, 0)
SID = "57abfcf713ac4053ae8ff6b90f0c5d83"
CLAIMED_AT = "2026-08-29T05:49:25"


def _breadcrumb(goal_id, ts):
    return json.dumps({"entry_type": "observation", "goal_id": goal_id, "timestamp": ts,
                       "content": f"{sweep.CLAIM_BREADCRUMB_MARKER} for {goal_id} (source=world) - g-115-6677"})


def _phase(goal_id, ts):
    return json.dumps({"entry_type": "phase_start", "goal_id": goal_id, "timestamp": ts,
                       "phase": "phase-4-execute", "content": "phase_start phase-4-execute"})


def test_breadcrumb_alone_is_not_execution_activity():
    text = _breadcrumb("g-006-03", CLAIMED_AT)
    assert sweep._scan_diary_text(text, "g-006-03", CLAIMED_AT) is False


def test_a_real_row_after_the_claim_still_counts_even_beside_a_breadcrumb():
    text = "\n".join([_breadcrumb("g-006-03", CLAIMED_AT), _phase("g-006-03", "2026-08-29T06:10:00")])
    assert sweep._scan_diary_text(text, "g-006-03", CLAIMED_AT) is True
    # and a row for ANOTHER goal never counts for this one
    other = "\n".join([_breadcrumb("g-006-03", CLAIMED_AT), _phase("g-006-04", "2026-08-29T06:10:00")])
    assert sweep._scan_diary_text(other, "g-006-03", CLAIMED_AT) is False


def _zakcode_home(tmp_path, sid, assistant_at):
    home = tmp_path / "zakhome"
    (home / "sessions").mkdir(parents=True)
    messages = [{"role": "user", "created_at": "2026-08-29T05:31:20+00:00"}]
    if assistant_at is not None:
        messages.append({"role": "assistant", "created_at": assistant_at})
    (home / "sessions" / f"{sid}.json").write_text(json.dumps({"messages": messages}), encoding="utf-8")
    return str(home)


def test_fresh_zakcode_turn_is_alive(tmp_path):
    home = _zakcode_home(tmp_path, SID, "2026-08-30T04:33:00+00:00")   # 7 min before NOW
    v = sweep._holder_transcript_verdict(SID, 100, NOW, transcripts_dir=tmp_path / "none", zakcode_home=home)
    assert v["verdict"] == "alive"
    assert v["source"] == "zakcode"
    assert v["age_minutes"] == 7.0


def test_old_zakcode_turn_is_stale(tmp_path):
    home = _zakcode_home(tmp_path, SID, "2026-08-29T10:32:23+00:00")   # 18 h before NOW
    v = sweep._holder_transcript_verdict(SID, 100, NOW, transcripts_dir=tmp_path / "none", zakcode_home=home)
    assert v["verdict"] == "stale"
    assert v["age_minutes"] > 1000


def test_no_transcript_anywhere_is_absent(tmp_path):
    home = tmp_path / "zakhome"
    (home / "sessions").mkdir(parents=True)
    v = sweep._holder_transcript_verdict(SID, 100, NOW, transcripts_dir=tmp_path / "none", zakcode_home=str(home))
    assert v["verdict"] == "absent"


def test_empty_sid_is_absent_without_touching_disk(tmp_path):
    assert sweep._holder_transcript_verdict("", 100, NOW, transcripts_dir=tmp_path)["verdict"] == "absent"


def test_unreadable_document_is_unreadable_not_a_verdict(tmp_path):
    home = tmp_path / "zakhome"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / f"{SID}.json").write_text("{not json", encoding="utf-8")
    v = sweep._holder_transcript_verdict(SID, 100, NOW, transcripts_dir=tmp_path / "none", zakcode_home=str(home))
    assert v["verdict"] == "unreadable"
    assert "error" in v


def test_claude_code_transcript_wins_over_zakcode_doc(tmp_path):
    tdir = tmp_path / "projects"
    tdir.mkdir()
    rows = [{"type": "user", "timestamp": "2026-08-30T04:00:00Z"},
            {"type": "assistant", "timestamp": "2026-08-30T04:35:00Z"}]
    (tdir / f"{SID}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    home = _zakcode_home(tmp_path, SID, "2026-08-29T10:32:23+00:00")   # stale doc must not be consulted
    v = sweep._holder_transcript_verdict(SID, 100, NOW, transcripts_dir=tdir, zakcode_home=home)
    assert v["verdict"] == "alive"
    assert v["source"] == "claude-code"
    assert v["age_minutes"] == 5.0


def test_threshold_is_inclusive_and_tunable(tmp_path):
    home = _zakcode_home(tmp_path, SID, "2026-08-30T03:00:00+00:00")   # 100 min before NOW
    assert sweep._holder_transcript_verdict(SID, 100, NOW, transcripts_dir=tmp_path / "n", zakcode_home=home)["verdict"] == "alive"
    assert sweep._holder_transcript_verdict(SID, 99, NOW, transcripts_dir=tmp_path / "n", zakcode_home=home)["verdict"] == "stale"


def test_default_holder_window_matches_the_carrier_window():
    """Both windows exist for the same reason (above the measured step gap,
    below the foreign-sid grace); drifting one without the other reintroduces a
    release-a-live-worker window on one signal only."""
    assert sweep.DEFAULT_HOLDER_FRESH_MINUTES == sweep.DEFAULT_CARRIER_FRESH_MINUTES
    assert sweep.DEFAULT_HOLDER_FRESH_MINUTES < sweep.DEFAULT_FOREIGN_SID_GRACE_MINUTES
