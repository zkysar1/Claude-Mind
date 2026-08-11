""" outcomes 2+3: the out-of-window AUDIT half of insight-trigger-sweep.

`load_triggers` drops a both-tagged post the instant it is older than
WINDOW_HOURS and says nothing. Because `scanned` is *defined* as what the sweep
chose to look at, an aged-out trigger is absent from the DENOMINATOR rather than
skipped within it — so a run that slipped past the window reported perfect
conservation while losing real work. First live measurement on this box: 23
unconverted triggers had aged out inside 7 days, several `severity:invalidates`.

What each pin holds, and why it is here rather than implied:

  1. aged-out is COUNTED            — the reported defect, stated as behavior.
  2. conservation is UNCHANGED      — outcome 1 must still hold EXACTLY. The
                                      audit half reports alongside `scanned`,
                                      never inside it. An implementation that
                                      folded aged-out triggers into `scanned`
                                      would pass pin 1 and silently redefine
                                      the one number outcome 1 pins.
  3. converted vs unconverted split — only the unconverted ones are lost work.
                                      Counting all 34 as dropped would cry wolf
                                      on the 11 that did convert.
  4. audit window is BOUNDED and SAYS SO — a bounded scan that does not report
                                      what it declined to look at reads as
                                      coverage it never had (guard-1760).
  5. routing is IDEMPOTENT          — the out-of-window condition is MONOTONE
                                      (a post only gets older), so a stateless
                                      re-post fires every cadence forever
                                      (guard-2177/guard-1826). Prior notes are
                                      harvested from ANY age, not just from
                                      inside the audit window.
  6. one digest PER TARGET          — 23 separate posts in one cadence trains
                                      readers to filter the tag, which is a
                                      louder version of the silence being
                                      fixed. Volume must be bounded by the
                                      roster, not by the backlog.
  7. in-grace posts are NOT aged out — a post younger than GRACE_HOURS is
                                      pending, not lost. Classifying it as
                                      out-of-window would route work the next
                                      run was about to convert normally.

Run: py -3 -m pytest core/scripts/tests/test_insight_trigger_sweep_out_of_window.py
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
_spec = importlib.util.spec_from_file_location("its_oow_under_test", SWEEP_PATH)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_oow_under_test"] = its
_spec.loader.exec_module(its)


def _msg(msg_id, *, author="zeta", target="alpha", action="review",
         severity="constrains", tags=None, hours_ago=3.0):
    if tags is None:
        tags = [
            f"requires_action_by:{target}",
            f"action_type:{action}",
            f"severity:{severity}",
        ]
    ts = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    return json.dumps({
        "id": msg_id,
        "author": author,
        "type": "finding",
        "text": f"test trigger {msg_id}",
        "tags": tags,
        "timestamp": ts,
    }) + "\n"


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    board_dir = tmp_path / "world" / "board"
    board_dir.mkdir(parents=True)
    asp_jsonl = tmp_path / "world" / "aspirations.jsonl"
    asp_jsonl.write_text("", encoding="utf-8")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    monkeypatch.setattr(its, "BOARD_DIR", board_dir)
    monkeypatch.setattr(its, "WORLD_ASPS", asp_jsonl)
    monkeypatch.setattr(its, "_agents_root", lambda: agents_dir)
    monkeypatch.setattr(its, "ENV_REGISTRY_DIR", tmp_path / "no-environments")
    monkeypatch.setattr(its, "_self_env", lambda: "test-env")
    monkeypatch.setattr(its, "_local_roster", lambda: set())
    return {"dir": board_dir}


def _write(board, channel, *rows):
    (board["dir"] / f"{channel}.jsonl").write_text("".join(rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1 — the defect, stated as behavior
# ---------------------------------------------------------------------------


def test_aged_out_trigger_is_counted_not_dropped(board):
    """A both-tagged post past WINDOW_HOURS lands in the audit bucket."""
    _write(board, "findings", _msg("msg-old-1", hours_ago=48.0))
    assert its.load_triggers() == []          # conversion half still ignores it
    oow, routed, truncated = its.load_out_of_window_triggers()
    assert [t["msg_id"] for t in oow] == ["msg-old-1"]
    assert truncated == 0
    assert routed == set()


def test_in_window_trigger_never_enters_the_audit_bucket(board):
    """Ownership is exclusive — the conversion half keeps its own."""
    _write(board, "findings", _msg("msg-fresh", hours_ago=3.0))
    assert [t["msg_id"] for t in its.load_triggers()] == ["msg-fresh"]
    oow, _, _ = its.load_out_of_window_triggers()
    assert oow == []


# ---------------------------------------------------------------------------
# 2 — conservation must be untouched (outcome 1)
# ---------------------------------------------------------------------------


def test_aged_out_triggers_stay_out_of_the_scanned_denominator(board):
    """`scanned` counts what the sweep chose to LOOK AT — nothing more.

    This is the pin that a "just widen WINDOW_HOURS" implementation fails: it
    would make the audit visible AND silently redefine `scanned`, so outcome 1's
    identity would still balance while meaning something different.
    """
    _write(board, "findings",
           _msg("msg-fresh", hours_ago=3.0),
           _msg("msg-old-a", hours_ago=48.0),
           _msg("msg-old-b", hours_ago=72.0))
    assert len(its.load_triggers()) == 1
    oow, _, _ = its.load_out_of_window_triggers()
    assert len(oow) == 2


# ---------------------------------------------------------------------------
# 3 — converted vs unconverted
# ---------------------------------------------------------------------------


def test_converted_and_unconverted_are_separable(board):
    """An aged-out trigger that DID convert is not lost work.

    Pinned at the seam the caller uses (`load_converted_ids` membership) rather
    than through main(), because conflating the two is what would turn an
    accurate 23 into an alarmist 34.
    """
    _write(board, "findings",
           _msg("msg-old-conv", hours_ago=48.0),
           _msg("msg-old-lost", hours_ago=50.0))
    oow, _, _ = its.load_out_of_window_triggers()
    converted_ids = {"msg-old-conv"}
    unconverted = [t for t in oow if t["msg_id"] not in converted_ids]
    assert [t["msg_id"] for t in unconverted] == ["msg-old-lost"]


# ---------------------------------------------------------------------------
# 4 — the bound is reported, not swallowed (guard-1760)
# ---------------------------------------------------------------------------


def test_posts_older_than_the_audit_window_are_reported_as_truncated(board):
    """"None aged out" and "I stopped looking" must not render identically."""
    _write(board, "findings",
           _msg("msg-in-audit", hours_ago=48.0),
           _msg("msg-ancient", hours_ago=its.AUDIT_WINDOW_HOURS + 24.0))
    oow, _, truncated = its.load_out_of_window_triggers()
    assert [t["msg_id"] for t in oow] == ["msg-in-audit"]
    assert truncated == 1


def test_audit_window_is_strictly_wider_than_the_conversion_window():
    """A collapse to equality would make the audit half structurally empty."""
    assert its.AUDIT_WINDOW_HOURS > its.WINDOW_HOURS


# ---------------------------------------------------------------------------
# 5 — routing idempotency (guard-2177 / guard-1826)
# ---------------------------------------------------------------------------


def test_prior_routing_note_suppresses_a_repost(board):
    """The board note IS the cooldown record."""
    prior = json.dumps({
        "id": "msg-route-note",
        "author": "bravo",
        "type": "status",
        "text": "digest",
        "tags": ["insight-trigger-out-of-window",
                 f"{its.OOW_TAG_PREFIX}msg-old-1"],
        "timestamp": (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
    }) + "\n"
    _write(board, "coordination", prior)
    _write(board, "findings", _msg("msg-old-1", hours_ago=48.0))
    oow, routed, _ = its.load_out_of_window_triggers()
    assert [t["msg_id"] for t in oow] == ["msg-old-1"]
    assert "msg-old-1" in routed


def test_routing_notes_are_harvested_from_ANY_age(board):
    """A note older than the audit window must still suppress a re-post.

    The harvest is deliberately unbounded by the audit window: bounding it would
    make an 8-day-old trigger re-route forever, since its trigger stays inside
    the 7-day audit band far longer than a same-age note would remain visible.
    """
    prior = json.dumps({
        "id": "msg-route-note-ancient",
        "author": "bravo",
        "type": "status",
        "text": "digest",
        "tags": [f"{its.OOW_TAG_PREFIX}msg-old-1"],
        "timestamp": (datetime.now()
                      - timedelta(hours=its.AUDIT_WINDOW_HOURS + 48)).strftime("%Y-%m-%dT%H:%M:%S"),
    }) + "\n"
    _write(board, "coordination", prior)
    _write(board, "findings", _msg("msg-old-1", hours_ago=48.0))
    _, routed, _ = its.load_out_of_window_triggers()
    assert "msg-old-1" in routed


# ---------------------------------------------------------------------------
# 6 — digest shape: bounded by roster, not by backlog
# ---------------------------------------------------------------------------


def test_digest_groups_by_target_and_tags_every_msg_id(board, monkeypatch):
    """One post per target; every id carried so dedup stays per-trigger."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "msg-digest-1"

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input", "")
        return _Proc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    batch = [
        {"msg_id": "m1", "author": "zeta", "channel": "findings", "target": "alpha",
         "action": "review", "severity": "invalidates", "age_h": 48.0},
        {"msg_id": "m2", "author": "echo", "channel": "coordination", "target": "alpha",
         "action": "push", "severity": "informs", "age_h": 30.0},
    ]
    res = its._emit_out_of_window_digest("alpha", batch)
    assert res["posted"] is True
    assert res["count"] == 2

    tags = captured["argv"][captured["argv"].index("--tags") + 1]
    assert f"{its.OOW_TAG_PREFIX}m1" in tags
    assert f"{its.OOW_TAG_PREFIX}m2" in tags
    # It must be addressed, so the digest itself converts to ONE triage goal.
    assert "requires_action_by:alpha" in tags
    assert "action_type:triage-aged-triggers" in tags
    # Every id is NAMED in the body — a count alone tells a reader something was
    # lost without telling them what (guard-1227).
    assert "m1" in captured["input"] and "m2" in captured["input"]


def test_digest_post_failure_leaves_no_dedup_tag(board, monkeypatch):
    """Fail-open: a failed post must be retried, not silently swallowed."""
    class _Proc:
        returncode = 1
        stdout = ""

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    res = its._emit_out_of_window_digest("alpha", [
        {"msg_id": "m1", "author": "zeta", "channel": "findings", "target": "alpha",
         "action": "review", "severity": "informs", "age_h": 48.0}])
    assert res["posted"] is False
    assert res["msg_id"] is None


# ---------------------------------------------------------------------------
# 7 — SPECIFICITY controls
# ---------------------------------------------------------------------------


def test_post_still_inside_grace_is_not_aged_out(board):
    """Younger than GRACE_HOURS = pending, not lost."""
    _write(board, "findings", _msg("msg-grace", hours_ago=0.2))
    assert its.load_triggers() == []            # held back by grace
    oow, _, _ = its.load_out_of_window_triggers()
    assert oow == []                            # but NOT reported as dropped


def test_untagged_old_post_is_not_an_aged_out_trigger(board):
    """The audit half applies the same both-tags definition as conversion."""
    _write(board, "findings", _msg("msg-untagged", hours_ago=48.0, tags=[]))
    oow, _, truncated = its.load_out_of_window_triggers()
    assert oow == []
    assert truncated == 0


def test_half_tagged_old_post_is_not_an_aged_out_trigger(board):
    """requires_action_by WITHOUT action_type is not a trigger, at any age."""
    _write(board, "findings",
           _msg("msg-half", hours_ago=48.0, tags=["requires_action_by:alpha"]))
    oow, _, _ = its.load_out_of_window_triggers()
    assert oow == []


# ---------------------------------------------------------------------------
# 8 — the digest must resolve its OWN addressing before emitting ()
#
# A digest is itself an in-window trigger by this sweep's definition, so one
# carrying an unresolvable target can never convert: it ages out, gets
# re-digested for the same target, and refuses identically — one new post per
# audit cycle, forever, with the routed work never arriving.
#
# guard-1082: assert on the SPECIFIC refusal evidence (the verdict string and
# the msg_id), never a coarse signal, and PAIR every refusal test with a control
# call that MUST SUCCEED. Without the control, a harness that refuses everything
# — a broken collision set, a crash in resolve_addressing — passes the refusal
# assertion while proving nothing.
# ---------------------------------------------------------------------------


def _collision_board(board, monkeypatch):
    """Make bare `zeta` ambiguous and bare `alpha` unambiguous.

    `zeta` is in BOTH the local roster and a peer deployment's known_agents;
    `alpha` is local only. That asymmetry is what lets one test carry its own
    control.
    """
    monkeypatch.setattr(its, "_local_roster", lambda: {"zeta", "alpha"})
    monkeypatch.setattr(
        its, "_load_env_registry",
        lambda: {"test-env": {"known_agents": ["alpha", "zeta"]},
                 "peer-env": {"known_agents": ["zeta"]}},
    )


def _run_main_json(monkeypatch, capsys, argv=("--dry-run", "--json")):
    monkeypatch.setattr(sys, "argv", ["insight-trigger-sweep.py", *argv])
    rc = its.main()
    assert rc == 0
    return rc, json.loads(capsys.readouterr().out)


def test_ambiguous_digest_target_is_refused_and_unambiguous_one_still_emits(
        board, monkeypatch, capsys):
    """The refusal and its CONTROL, in one run so the harness proves itself."""
    _collision_board(board, monkeypatch)
    _write(board, "findings",
           _msg("msg-oow-zeta", author="echo", target="zeta", hours_ago=48.0),
           _msg("msg-oow-alpha", author="echo", target="alpha", hours_ago=48.0))

    _, summary = _run_main_json(monkeypatch, capsys)

    # --- the thing under test: SPECIFIC evidence, not a coarse count ---
    assert summary["out_of_window_digest_refused"] == 1
    refused = summary["out_of_window_digest_refused_details"]
    assert [r["msg_id"] for r in refused] == ["msg-oow-zeta"]
    assert refused[0]["verdict"] == "ambiguous_collision"
    assert "zeta@<env-id>" in refused[0]["reason"]
    # It must NOT have been emitted — that is the recursion this fix prevents.
    assert "zeta" not in [d["target"] for d in summary["out_of_window_digests"]]

    # --- the CONTROL: a resolvable target in the SAME run still reaches emit ---
    alpha = [d for d in summary["out_of_window_digests"] if d["target"] == "alpha"]
    assert len(alpha) == 1, "control failed — harness refuses everything, verdict void"
    assert alpha[0]["msg_ids"] == ["msg-oow-alpha"]


def test_healthy_run_reports_digest_refused_as_an_explicit_zero(
        board, monkeypatch, capsys):
    """An omitted bucket reads identically to an empty one — the whole lineage
    of this goal. The healthy case must SAY zero, not stay silent."""
    _collision_board(board, monkeypatch)
    _write(board, "findings",
           _msg("msg-oow-alpha", author="echo", target="alpha", hours_ago=48.0))

    _, summary = _run_main_json(monkeypatch, capsys)

    assert "out_of_window_digest_refused" in summary
    assert summary["out_of_window_digest_refused"] == 0
    assert summary["out_of_window_digest_refused_details"] == []
    assert summary["out_of_window_stranded_by_prior_digest"] == {}


def test_stranding_is_reported_when_NOTHING_is_unrouted(
        board, monkeypatch, capsys):
    """The cleanup half must not key off the refusal set.

    Triggers claimed by a PRE-FIX digest carry OOW tags, so they are excluded
    from the unrouted set — a target in that state produces NO refusal at all.
    A stranding report keyed off refusals is therefore inert in exactly the case
    it exists for. Measured on this fix's own first dry-run: 21 of 21 unconverted
    triggers were already-routed and a refusal-keyed report saw ZERO, while the
    corrected predicate found 8 stranded. Same vacuous-bucket class as the
    omitted out-of-window count this goal was filed to close — reproduced inside
    its own remedy, which is why it is pinned here.
    """
    _collision_board(board, monkeypatch)
    _write(board, "findings",
           _msg("msg-oow-zeta", author="echo", target="zeta", hours_ago=48.0),
           # the pre-fix debris digest: claims the trigger above via its OOW tag
           _msg("msg-debris", author="bravo", target="zeta", hours_ago=20.0,
                tags=["insight-trigger-out-of-window",
                      "requires_action_by:zeta",
                      "action_type:triage-aged-triggers",
                      f"{its.OOW_TAG_PREFIX}msg-oow-zeta"]))

    _, summary = _run_main_json(monkeypatch, capsys)

    # Precondition of the test: nothing is unrouted, so there is no refusal.
    assert summary["out_of_window_already_routed"] == 1
    assert summary["out_of_window_digest_refused"] == 0
    # The stranding is reported ANYWAY — that is the point.
    assert summary["out_of_window_stranded_by_prior_digest"] == {
        "zeta": ["msg-oow-zeta"]
    }


def test_unreadable_env_registry_fails_OPEN_and_still_emits(
        board, monkeypatch, capsys):
    """guard-142: a gate must not block work on its own dependency errors.

    An unreadable registry yields an empty peer set, hence an empty collision
    set, hence no refusals — digests keep flowing. The failure direction that
    matters is the one where a broken registry silently strands every target.
    """
    monkeypatch.setattr(its, "_local_roster", lambda: {"zeta", "alpha"})
    monkeypatch.setattr(its, "_load_env_registry", lambda: {})
    _write(board, "findings",
           _msg("msg-oow-zeta", author="echo", target="zeta", hours_ago=48.0))

    _, summary = _run_main_json(monkeypatch, capsys)

    assert summary["out_of_window_digest_refused"] == 0
    assert [d["target"] for d in summary["out_of_window_digests"]] == ["zeta"]


# ---------------------------------------------------------------------------
# 9 — the digest's OWN emitted tag must survive the NEXT run's addressing check
#
# Section 8 covers the INPUT side: an unresolvable target never reaches the
# emitter. This is the OUTPUT side — a different defect with the same
# consequence. `_emit_out_of_window_digest` receives a target that is already
# bare and known-local, and wrote that bare name straight into its own
# `requires_action_by:` tag. On the NEXT run that tag is read back by
# resolve_addressing — the digest IS an in-window trigger, and that read is the
# entire conversion mechanism — where rule 3 refuses a bare name in the
# collision set. The digest then never converts, and guard-2177 correctly
# forbids re-posting it, so the loss is PERMANENT rather than late.
#
# Section 8 does not shadow this. A trigger addressed in the QUALIFIED form the
# convention recommends (`zeta@<self-env>`) resolves cleanly, reaches the
# emitter, and — pre-fix — minted a fresh bare-tagged digest that the next run
# refused. So the live path kept producing new permanently-unconvertible debris
# even with section 8 working exactly as designed.
#
# Producer and validator live in the same file and were each individually
# correct, which is why neither section 8 nor
# test_insight_trigger_sweep_addressing.py could see it. Per guard-2806 the
# acceptance check for "my payload reaches the consumer" must be observed in the
# CONSUMER'S OUTPUT, never at the transport layer — so these tests run the real
# emitter, feed its real emitted tag through the real parser and the real
# resolver, and assert on what the RESOLVER says. A "did we post?" assertion
# would stay green through the entire defect.
# ---------------------------------------------------------------------------


def _emit_and_capture_tags(monkeypatch, target, batch):
    """Run the REAL emitter; return (tag_string, body) it actually posted."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "msg-digest-emitted"

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input", "")
        return _Proc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = its._emit_out_of_window_digest(target, batch)
    assert res["posted"] is True, "emitter did not post — harness is broken"
    argv = captured["argv"]
    return argv[argv.index("--tags") + 1], captured["input"]


def _resolve_as_next_run_would(tag_string, msg_id="msg-digest-emitted",
                               author="echo"):
    """Feed an emitted tag through the REAL parse + resolve path.

    The consumer half of guard-2806's A->B->A: the verdict comes from
    resolve_addressing, not from the fact that a post was made.

    `author` defaults to a NON-ROSTER name, and that is load-bearing since
    clause 3b (g-115-4980): a bare collision-set target now resolves when the
    author is a non-colliding local. This helper previously hardcoded
    "alpha" — a vouching local in `_collision_board`'s roster — which silently
    converted the negative control below into a test that could no longer fail.
    Keep the default non-vouching so the control keeps discriminating.
    """
    now = datetime.now()
    msg = {"id": msg_id, "author": author, "text": "digest",
           "tags": tag_string.split(","),
           "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S")}
    trig = its._parse_trigger_msg(msg, "coordination", now, now)
    assert trig is not None, "the emitted digest is not even a parseable trigger"
    resolved, refused, _ = its.resolve_addressing([trig])
    return trig, resolved, refused


def _oow_batch(target):
    return [{"msg_id": "m1", "author": "echo", "channel": "findings",
             "target": target, "action": "review", "severity": "invalidates",
             "age_h": 48.0}]


def test_emitted_digest_tag_survives_the_next_runs_addressing_check(
        board, monkeypatch):
    """THE FIX: a colliding target round-trips emitter -> resolver, resolved."""
    _collision_board(board, monkeypatch)          # bare `zeta` is ambiguous
    tags, body = _emit_and_capture_tags(monkeypatch, "zeta", _oow_batch("zeta"))

    # Rule 1's EXACT form, not the bare name rule 3 refuses.
    assert "requires_action_by:zeta@test-env" in tags
    # The body's self-description must match the tag it actually carries —
    # a post that misreports its own addressing teaches the next reader wrong.
    assert "requires_action_by:zeta@test-env" in body

    trig, resolved, refused = _resolve_as_next_run_would(tags)
    assert trig["target"] == "zeta@test-env"
    assert refused == []
    # Qualifier stripped back to the local agent — the digest converts for zeta.
    assert [t["target"] for t in resolved] == ["zeta"]


def test_bare_digest_tag_is_exactly_what_the_next_run_refuses(board, monkeypatch):
    """NEGATIVE CONTROL: the pre-fix shape, through the SAME consumer, refuses.

    Without this the test above proves only that some string round-trips; it
    could not distinguish the fix from a resolver that accepts everything.
    """
    _collision_board(board, monkeypatch)
    pre_fix_tags = ("insight-trigger-out-of-window,requires_action_by:zeta,"
                    "action_type:triage-aged-triggers")

    _trig, resolved, refused = _resolve_as_next_run_would(pre_fix_tags)

    assert resolved == []
    assert [r["verdict"] for r in refused] == ["ambiguous_collision"]


def test_bare_digest_from_vouching_local_author_resolves_under_3b(
        board, monkeypatch):
    """The SCOPE of the control above, stated so it is not mistaken for a
    universal. Since clause 3b (g-115-4980) the SAME bare tag resolves when
    the author is a non-colliding local — so the emitter's unconditional
    qualification is no longer what saves the LOCAL round-trip.

    It is still correct and still required, for a reason the local round-trip
    cannot show: the peer deployment reads this same channel, and `zeta@<env>`
    is the only form that tells it whose zeta the digest means. Explicit
    beats inferred (guard-2586) — 3b is a floor under unqualified posts from
    authors we cannot re-author, not a licence to stop qualifying our own.
    """
    _collision_board(board, monkeypatch)
    pre_fix_tags = ("insight-trigger-out-of-window,requires_action_by:zeta,"
                    "action_type:triage-aged-triggers")

    _trig, resolved, refused = _resolve_as_next_run_would(
        pre_fix_tags, author="alpha")

    assert refused == []
    assert [t["addressing"] for t in resolved] == ["author_scoped_local"]


def test_non_colliding_target_also_round_trips(board, monkeypatch):
    """CONTROL: the majority path is unchanged by qualifying unconditionally."""
    _collision_board(board, monkeypatch)          # bare `alpha` is unambiguous
    tags, _body = _emit_and_capture_tags(monkeypatch, "alpha", _oow_batch("alpha"))

    assert "requires_action_by:alpha@test-env" in tags
    _trig, resolved, refused = _resolve_as_next_run_would(tags)
    assert refused == []
    assert [t["target"] for t in resolved] == ["alpha"]


def test_unresolvable_environment_id_falls_back_to_bare_never_at_None(
        board, monkeypatch):
    """`<target>@None` would be refused as unknown_env — worse than bare.

    With ENVIRONMENT_ID unresolvable there is no qualified form to write, so the
    emitter must degrade to the status quo rather than mint a target naming an
    unregistered deployment.
    """
    monkeypatch.setattr(its, "_self_env", lambda: None)
    tags, _body = _emit_and_capture_tags(monkeypatch, "alpha", _oow_batch("alpha"))

    assert "requires_action_by:alpha" in tags
    assert "@None" not in tags
    assert "alpha@" not in tags
