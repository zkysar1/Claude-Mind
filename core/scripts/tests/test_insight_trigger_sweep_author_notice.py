""": the ORIGINAL AUTHOR of an aged-out or skipped insight_trigger is notified.

WHY THIS FILE EXISTS: when a trigger ages out of the conversion window, or targets
an already-terminal goal, the sweep routes a note to the NAMED AGENT (the digest's
requires_action_by target, or the audit-stale note). If that address was WRONG the
escalation is wrong in exactly the same way and nobody notices -- per this goal's
filing, a real instance addressed one agent but was tagged for another and sat
unactioned, the tagged agent (rightly) ignoring someone else's work while the
author was never told. `_emit_author_notice` closes the observability half: it
notifies the author IN ADDITION to the named agent, and it must do so WITHOUT
filing a goal and WITHOUT weakening the existing per-trigger dedup.

What each pin holds, and why it earns its place:

  1. NON-CONVERTING by construction  guard-2019: the emitted tags, fed through the
     (parse level)                   REAL shared _parse_trigger_msg, return None.
                                      requires_action_by present, action_type and
                                      correction-intent absent -> the drop branch.
  2. files ZERO goals                check 1 (literal): the emitted post placed on
     (conversion-scan level)         the board FRESH is ignored by load_triggers(),
                                      so file_goal is never reached.
  3. BOTH emitters covered           guard-6670: _emit_audit_stale_note carries the
                                      author notice too, not just the digest.
  4. ONE notice per AUTHOR           guard-2177: the digest groups by author, so an
                                      author with N aged triggers gets one post, not
                                      N -- and the named-agent addressing is intact
                                      (outcome 5).
  5. author notice fires ONCE        check 3: an aged trigger routed on run 1 is
     across two runs                 deduped on run 2 (its OOW note is on the board),
                                      so the notice rides the digest's existing
                                      oow_routed_ids dedup and adds no new surface.
  6. qualified addressing +          check 4: <author>@<env> where env resolves; bare
     bare fallback                   fallback where it does not; an already-qualified
                                      author keeps its OWN env (no double-qualify).
  7. dedup is NOT weakened           guard-3944: the notice is a SEPARATE post with
                                      no OOW_TAG_PREFIX tag, so it never enters
                                      oow_routed_ids. The address is the fixed
                                      variable, not the dedup.

Running main() itself non-dry-run is unsafe on a live box (it takes the real
SWEEP_LOCK and files via the _rt daemon), so pin 5 reproduces exactly main()'s OOW
routing GATE (insight-trigger-sweep.py:1504-1583) with the real functions and
nothing else.

Run: STORAGE_BACKEND=local py -3 -m pytest \
       core/scripts/tests/test_insight_trigger_sweep_author_notice.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
SWEEP_PATH = CORE_SCRIPTS / "insight-trigger-sweep.py"
_spec = importlib.util.spec_from_file_location("its_author_notice_under_test", SWEEP_PATH)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_author_notice_under_test"] = its
_spec.loader.exec_module(its)


def _now_iso(hours_ago=0.0):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _trigger(msg_id, *, author="zeta", target="foxtrot", action="review",
             severity="constrains", channel="findings", age_h=48.0, affects_goal=None):
    """A trigger dict in the shape both emitters pass to _emit_author_notice."""
    return {"msg_id": msg_id, "author": author, "target": target, "action": action,
            "severity": severity, "channel": channel, "age_h": age_h,
            "affects_goal": affects_goal}


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    board_dir = tmp_path / "world" / "board"
    board_dir.mkdir(parents=True)
    asp = tmp_path / "world" / "aspirations.jsonl"
    asp.write_text("", encoding="utf-8")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955: never touch S3 from a test
    monkeypatch.setattr(its, "BOARD_DIR", board_dir)
    monkeypatch.setattr(its, "WORLD_ASPS", asp)
    monkeypatch.setattr(its, "_agents_root", lambda: agents_dir)
    monkeypatch.setattr(its, "ENV_REGISTRY_DIR", tmp_path / "no-environments")
    monkeypatch.setattr(its, "_self_env", lambda: "test-env")
    monkeypatch.setattr(its, "_local_roster", lambda: set())
    return {"dir": board_dir}


def _tags_of(argv):
    return argv[argv.index("--tags") + 1].split(",")


def _channel_of(argv):
    return argv[argv.index("--channel") + 1]


def _capture_run(monkeypatch, calls, *, write_board_dir=None):
    """Patch subprocess.run to record every board post argv. When write_board_dir
    is given, also APPEND the emitted post to its channel file, so a later
    load_*_triggers() harvest sees it exactly as a real board.py post would (used
    by the once-only and conversion-scan pins). board.py is never executed."""
    import subprocess

    def fake_run(argv, **kw):
        calls.append({"argv": argv, "input": kw.get("input", "")})
        msg_id = f"posted-{len(calls)}"
        if write_board_dir is not None:
            channel = _channel_of(argv)
            row = json.dumps({
                "id": msg_id, "author": "test-poster", "type": "status",
                "text": kw.get("input", ""), "tags": _tags_of(argv),
                "timestamp": _now_iso(0.0),
            }) + "\n"
            with open(write_board_dir / f"{channel}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(row)

        class _P:
            returncode = 0
            stdout = msg_id
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _author_posts(calls):
    return [c for c in calls if "insight-trigger-author-notice" in _tags_of(c["argv"])]


# ---------------------------------------------------------------------------
# 1 + 2 — the load-bearing property: informational, files ZERO goals
# ---------------------------------------------------------------------------


def test_author_notice_is_non_converting_by_construction(board, monkeypatch):
    """guard-2019 / check 1 (parse level). The EXACT tags emitted, fed through the
    shared _parse_trigger_msg, return None and land in `dropped` -- not an
    approximation of the tags, the tags themselves."""
    calls = _capture_run(monkeypatch, [])
    its._emit_author_notice("zeta", [_trigger("msg-aged-1")], "aged out of the 24h window")
    posts = _author_posts(calls)
    assert len(posts) == 1
    tags = _tags_of(posts[0]["argv"])
    assert any(t.startswith("requires_action_by:") for t in tags)   # addressed
    assert not any(t.startswith("action_type:") for t in tags)      # ...but no action_type
    assert not (set(tags) & its.CORRECTION_INTENT_TAGS)             # ...and no correction intent

    msg = {"id": "an-1", "author": "test-poster", "tags": tags, "timestamp": _now_iso(0.0)}
    now = datetime.now()
    dropped = []
    parsed = its._parse_trigger_msg(msg, "coordination", now, now - timedelta(hours=1),
                                    dropped=dropped)
    assert parsed is None                                            # NON-CONVERTING
    assert len(dropped) == 1
    assert "without action_type" in dropped[0]["reason"]


def test_author_notice_files_zero_goals_via_conversion_scan(board, monkeypatch):
    """check 1 (literal 'run conversion path, assert 0 filed'). The emitted notice
    is placed on the board FRESH -- so age cannot be the reason it is skipped --
    and the real conversion scan does not convert it.

    A POSITIVE CONTROL rides in the SAME batch (guard-2421 / guard-6772): an
    ordinary both-tagged in-window trigger that DOES convert, so the author
    notice's absence proves the scan READS the board and CAN convert -- not that
    load_triggers() is simply inert and every input yields []."""
    calls = _capture_run(monkeypatch, [], write_board_dir=board["dir"])
    # positive control: a real, convertible, in-window trigger
    (board["dir"] / "findings.jsonl").write_text(json.dumps({
        "id": "msg-real", "author": "zeta", "type": "finding", "text": "real trigger",
        "tags": ["requires_action_by:alpha", "action_type:review", "severity:constrains"],
        "timestamp": _now_iso(3.0),
    }) + "\n", encoding="utf-8")
    its._emit_author_notice("zeta", [_trigger("msg-aged-1")], "aged out of the 24h window")
    assert len(_author_posts(calls)) == 1

    converted_ids = {t["msg_id"] for t in its.load_triggers()}
    assert "msg-real" in converted_ids          # control: the scan works and CAN convert
    assert converted_ids == {"msg-real"}         # ...yet the author notice is NOT converted
    oow, routed, _ = its.load_out_of_window_triggers()
    assert oow == []                            # nor is the author notice an aged-out trigger
    assert routed == set()                      # nor does it register in the OOW dedup


# ---------------------------------------------------------------------------
# 3 + 4 — BOTH emitters, and grouping (guard-6670 / guard-2177)
# ---------------------------------------------------------------------------


def test_audit_stale_note_also_emits_author_notice(board, monkeypatch):
    """guard-6670 / check 2: the audit-stale emitter covers the author, not just
    the digest. The named-agent audit note AND the author notice both go out."""
    calls = _capture_run(monkeypatch, [])
    trigger = _trigger("msg-a", author="zeta", affects_goal="g-9-9", channel="coordination")
    res = its._emit_audit_stale_note(trigger, "completed")
    assert res["author_notice"]["posted"] is True
    posts = _author_posts(calls)
    assert len(posts) == 1
    tags = _tags_of(posts[0]["argv"])
    assert "requires_action_by:zeta@test-env" in tags               # addresses the AUTHOR
    assert not any(t.startswith("action_type:") for t in tags)      # non-converting
    assert len(calls) == 2                                          # audit note + author note


def test_out_of_window_digest_emits_one_author_notice_per_author(board, monkeypatch):
    """guard-2177 / check 2: the digest notifies each DISTINCT author once, covering
    all of that author's aged triggers -- not one post per trigger -- and leaves
    the named-agent addressing untouched (outcome 5)."""
    calls = _capture_run(monkeypatch, [])
    triggers = [
        _trigger("m1", author="zeta", age_h=48.0),
        _trigger("m2", author="zeta", age_h=49.0),
        _trigger("m3", author="echo", age_h=50.0),
    ]
    res = its._emit_out_of_window_digest("foxtrot", triggers)
    assert res["posted"] is True
    assert len(res["author_notices"]) == 2                          # two distinct authors, not three

    posts = _author_posts(calls)
    assert len(posts) == 2
    zeta_post = next(p for p in posts if "requires_action_by:zeta@test-env" in _tags_of(p["argv"]))
    ztags = _tags_of(zeta_post["argv"])
    assert sum(1 for t in ztags if t.startswith("author-notice:")) == 2   # one post, both triggers
    assert "author-notice:m1" in ztags and "author-notice:m2" in ztags

    # outcome 5: the named-agent digest addressing is unchanged
    digest_post = next(c for c in calls if "insight-trigger-out-of-window" in _tags_of(c["argv"]))
    dtags = _tags_of(digest_post["argv"])
    assert "requires_action_by:foxtrot@test-env" in dtags
    assert "action_type:triage-aged-triggers" in dtags


# ---------------------------------------------------------------------------
# 5 — ONCE across two runs (check 3), via main()'s real OOW routing gate
# ---------------------------------------------------------------------------


def _route_once(board_dir):
    """Reproduce main()'s OOW routing gate (insight-trigger-sweep.py:1504-1583,
    non-dry-run) with the REAL functions and nothing else: harvest routed_ids,
    apply the :1511 exclusion, group by target, emit. Running main() non-dry-run
    would take the real SWEEP_LOCK and file via the _rt daemon; this exercises
    exactly the gate the author notice's once-only property rides."""
    author_noticed = its.load_author_noticed_ids()                     # main() harvest
    oow, routed_ids, _ = its.load_out_of_window_triggers()
    unrouted = [t for t in oow if t["msg_id"] not in routed_ids]        # main():1511
    by_target = {}
    for t in unrouted:
        by_target.setdefault(t["target"], []).append(t)
    return [its._emit_out_of_window_digest(tg, by_target[tg], already_noticed=author_noticed)
            for tg in sorted(by_target)]


def test_author_notice_emitted_once_across_two_runs(board, monkeypatch):
    """check 3: run 1 routes the aged trigger (writing its OOW note to the board);
    run 2 harvests that note into routed_ids and excludes the trigger, so no second
    digest and no second author notice. The notice is deduped BY the digest, not
    by a parallel mechanism of its own."""
    calls = _capture_run(monkeypatch, [], write_board_dir=board["dir"])
    (board["dir"] / "findings.jsonl").write_text(json.dumps({
        "id": "msg-aged-1", "author": "zeta", "type": "finding", "text": "aged trigger",
        "tags": ["requires_action_by:foxtrot", "action_type:review", "severity:constrains"],
        "timestamp": _now_iso(48.0),
    }) + "\n", encoding="utf-8")

    r1 = _route_once(board["dir"])
    r2 = _route_once(board["dir"])

    assert len(r1) == 1 and r1[0]["posted"] is True     # routed on run 1
    assert r2 == []                                      # deduped on run 2
    assert len(_author_posts(calls)) == 1                # author notified exactly ONCE


def test_audit_stale_author_notice_is_once_only_across_runs(board, monkeypatch):
    """outcome 4 for the AUDIT-STALE path. That emitter has no dedup of its own:
    a terminal-goal trigger is re-encountered and its status note re-posts every
    cadence. The ADDRESSED author notice must NOT re-fire (guard-2177), so
    load_author_noticed_ids() harvests the prior notice's author-notice:<id> tag
    and suppresses the re-post -- while the status note keeps re-firing as before."""
    calls = _capture_run(monkeypatch, [], write_board_dir=board["dir"])
    trigger = _trigger("msg-term-1", author="zeta", affects_goal="g-9-9",
                       channel="coordination")

    # run 1: no prior notice on the board -> emits both the status note and the notice
    r1 = its._emit_audit_stale_note(trigger, "completed",
                                    already_noticed=its.load_author_noticed_ids())
    # run 2: the harvest now sees run 1's author-notice tag -> the notice is skipped
    r2 = its._emit_audit_stale_note(trigger, "completed",
                                    already_noticed=its.load_author_noticed_ids())

    assert r1["author_notice"]["posted"] is True
    assert r2["author_notice"]["posted"] is False
    assert r2["author_notice"].get("skipped") == "already-noticed"
    assert len(_author_posts(calls)) == 1                # author notified ONCE across runs
    # ...but the status note itself DID re-fire both runs (pre-existing behaviour,
    # deliberately unchanged -- it is unaddressed, so it does not spam a queue)
    audit_posts = [c for c in calls if "audit-stale" in _tags_of(c["argv"])]
    assert len(audit_posts) == 2


# ---------------------------------------------------------------------------
# 6 — qualified addressing + bare fallback (check 4)
# ---------------------------------------------------------------------------


def test_author_notice_addressing_qualified_and_bare_fallback(board, monkeypatch):
    """check 4: mirrors the digest's req_target (932-933)."""
    # (a) bare LOCAL author + resolvable env -> qualified with THIS env
    calls_a = _capture_run(monkeypatch, [])
    its._emit_author_notice("zeta", [_trigger("m1")], "aged")
    assert "requires_action_by:zeta@test-env" in _tags_of(calls_a[-1]["argv"])

    # (b) env unresolvable -> BARE fallback, never zeta@None
    monkeypatch.setattr(its, "_self_env", lambda: None)
    calls_b = _capture_run(monkeypatch, [])
    its._emit_author_notice("zeta", [_trigger("m2")], "aged")
    tags_b = _tags_of(calls_b[-1]["argv"])
    assert "requires_action_by:zeta" in tags_b
    assert not any(t.startswith("requires_action_by:zeta@") for t in tags_b)

    # (c) an already-qualified author keeps its OWN env (no zeta@peer-env@test-env)
    monkeypatch.setattr(its, "_self_env", lambda: "test-env")
    calls_c = _capture_run(monkeypatch, [])
    its._emit_author_notice("zeta@peer-env", [_trigger("m3")], "aged")
    tags_c = _tags_of(calls_c[-1]["argv"])
    assert "requires_action_by:zeta@peer-env" in tags_c
    assert not any(t.startswith("requires_action_by:") and "@test-env" in t for t in tags_c)


# ---------------------------------------------------------------------------
# 7 — the dedup is NOT weakened (guard-3944)
# ---------------------------------------------------------------------------


def test_author_notice_does_not_pollute_oow_dedup(board, monkeypatch):
    """guard-3944: a SEPARATE post carrying no OOW_TAG_PREFIX tag never enters
    oow_routed_ids, so the digest's per-trigger dedup is untouched. The address is
    the variable being fixed, not the dedup."""
    calls = _capture_run(monkeypatch, [], write_board_dir=board["dir"])
    its._emit_author_notice("zeta", [_trigger("m1"), _trigger("m2")], "aged")
    posts = _author_posts(calls)
    assert len(posts) == 1
    tags = _tags_of(posts[0]["argv"])
    assert not any(t.startswith(its.OOW_TAG_PREFIX) for t in tags)
    _, routed, _ = its.load_out_of_window_triggers()
    assert routed == set()                       # the author post contributes no dedup id


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
