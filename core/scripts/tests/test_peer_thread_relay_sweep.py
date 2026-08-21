"""Tests for the peer-thread relay sweep ().

THE LOAD-BEARING TEST is test_status_keyed_predicate_goes_red: g-115-5890
requires that "its regression test must go red against a status-based
predicate". That is not ceremony. The live population measured 2026-08-12
(echo, cc-03) is 8 `[Omni]` user replies aged 1.3-4.8d, ALL of them
`in-progress` and 6 of them never relayed — against which a `status ==
"pending"` predicate returns ZERO. Any future refactor that quietly re-keys
this sweep on goal status would restore a permanently-clean report over a
population that is genuinely stranded, and that test is what fails.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _peer_registry import (  # noqa: E402
    classify_agent_name, load_env_registry, peer_agent_names, peer_envs,
)
from _peer_thread_relay import (  # noqa: E402
    build_delivery_index, extract_bracket, is_inbound_email_goal,
    is_non_terminal, routed_agents, status_keyed_control, sweep,
)

REGISTRY = {
    "ayoai-mind": {"environment_id": "ayoai-mind"},
    "zds-mind": {"environment_id": "zds-mind", "known_agents": ["omni", "zeta"]},
}
SELF = "ayoai-mind"
ROSTER = {"alpha", "bravo", "echo", "foxtrot", "zeta"}


def _goal(gid, title, status="in-progress", sig="alert-email:x", created="2026-08-08T00:00:00"):
    return {"id": gid, "title": title, "status": status, "priority": "HIGH",
            "origin_signal": sig, "created_at": created}


def _relay(msg_id, goal_ids, target="omni@zds-mind"):
    return {"id": msg_id, "tags": ["relay", "forward-to:%s" % target] + list(goal_ids)}


# --------------------------------------------------------------------------
# The required falsifying control
# --------------------------------------------------------------------------

def test_status_keyed_predicate_goes_red():
    """A status-keyed predicate finds NOTHING in a genuinely stranded queue."""
    goals = [
        _goal("g-1-1", "Directive: Re: [Omni] VA solicitation"),
        _goal("g-1-2", "Directive: Re: [Omni] DoDEA no-bid"),
        _goal("g-1-3", "Directive: Re: [Omni] GSA mostly closed"),
    ]
    delivery_keyed = sweep(goals, [], REGISTRY, SELF, ROSTER)
    assert len(delivery_keyed["undelivered"]) == 3, "delivery-keying must see all 3"
    # The same population, keyed on status, is invisible.
    assert status_keyed_control(goals, REGISTRY, SELF, ROSTER) == 0, (
        "a status=='pending' predicate must return 0 over an in-progress "
        "stranded population — this is the red g-115-5890 requires"
    )


def test_in_progress_is_not_terminal():
    """The live population is in-progress; excluding it empties the sweep."""
    assert is_non_terminal({"status": "in-progress"})
    assert is_non_terminal({"status": "pending"})
    for done in ("completed", "skipped", "expired", "decomposed", "superseded"):
        assert not is_non_terminal({"status": done})


# --------------------------------------------------------------------------
# Delivery evidence
# --------------------------------------------------------------------------

def test_relay_post_marks_goal_relayed():
    goals = [_goal("g-1-1", "Directive: Re: [Omni] VA solicitation")]
    out = sweep(goals, [_relay("msg-a", ["g-1-1"])], REGISTRY, SELF, ROSTER)
    assert not out["undelivered"]
    assert out["relayed"][0]["relayed_via"] == ["msg-a"]


def test_relay_to_a_different_peer_is_not_delivery():
    """Failing toward 'undelivered' is deliberate — see sweep()'s docstring."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] VA solicitation")]
    rows = [_relay("msg-a", ["g-1-1"], target="someone@claude-mind")]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert len(out["undelivered"]) == 1
    assert "forward to" in out["undelivered"][0]["reason"]


def test_untargeted_relay_tag_still_counts():
    """A bare `relay` tag with no forward-to still evidences a relay."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] VA solicitation")]
    rows = [{"id": "msg-a", "tags": ["relay", "g-1-1"]}]
    assert not sweep(goals, rows, REGISTRY, SELF, ROSTER)["undelivered"]


def test_non_relay_post_carrying_the_goal_id_is_not_delivery():
    """An ordinary board post citing the goal id must not read as a relay."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] VA solicitation")]
    rows = [{"id": "msg-a", "tags": ["insight_trigger", "g-1-1"]}]
    assert len(sweep(goals, rows, REGISTRY, SELF, ROSTER)["undelivered"]) == 1


def test_delivery_index_ignores_non_goal_tags():
    idx = build_delivery_index([_relay("msg-a", ["g-1-1", "time-critical", "va"])])
    assert set(idx) == {"g-1-1"}


# --------------------------------------------------------------------------
# The delivery-gap split () — RELAYED and ROUTED are different claims
#
# The defect these pin: the sweep accepted a tag that notifies nobody, so it
# reported a flat clean over posts that reached no one. The fix REPORTS the
# split and deliberately does NOT tighten the predicate (guard-3628), so every
# test below asserts BOTH halves — the new count moves AND the verdict does not.
# --------------------------------------------------------------------------

KNOWN = ROSTER | {"omni"}


def test_production_relay_shape_routes_to_nobody():
    """The real bravo post: `relay` + `forward-to:omni@zds-mind`, nothing else.

    board.py parses `forward-to:omni@zds-mind` to the AGENT `forward-to:omni`,
    which matches no one — so this post notifies nobody. Verbatim from the live
    board 2026-08-15: msg-20260812-151741-bravo-5459, tags ['relay',
    'forward-to:omni@zds-mind', 'g-115-6066', 'g-115-6067', 'peer-thread'].
    """
    goals = [_goal("g-1-1", "Directive: Re: [Omni] VA solicitation")]
    out = sweep(goals, [_relay("msg-a", ["g-1-1"])], REGISTRY, SELF, ROSTER)
    assert not out["undelivered"], "the verdict must NOT move — report, do not tighten"
    assert out["relayed"][0]["routes_to"] == []
    assert out["relayed_unrouted"] == 1
    assert "notifies a known agent" in out["relayed"][0]["routing_gap"]


def test_requires_action_by_is_the_prefix_that_routes():
    goals = [_goal("g-1-1", "Directive: Re: [Omni] VA solicitation")]
    rows = [{"id": "msg-a", "tags": ["relay", "requires_action_by:zeta", "g-1-1"]}]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert out["relayed"][0]["routes_to"] == ["zeta"]
    assert out["relayed_unrouted"] == 0
    assert "routing_gap" not in out["relayed"][0]


def test_a_peer_agent_is_routable_not_only_the_local_roster():
    """`omni` is not local — it is declared by zds-mind's known_agents. A relay
    to a peer agent routes perfectly well and must not read as unrouted."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] VA solicitation")]
    rows = [{"id": "msg-a",
             "tags": ["relay", "requires_action_by:omni@zds-mind", "g-1-1"]}]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert out["relayed"][0]["routes_to"] == ["omni"]
    assert out["relayed_unrouted"] == 0


def test_free_form_tags_are_not_agents():
    """THE PROBE BUG THIS PIN EXISTS FOR, measured while writing the fix.

    Board tags are free text and parse_routing_tag returns a bare token as an
    "agent" name, so WITHOUT a roster check `peer-thread` / `zds-mind` / `relay`
    all read as routing targets — 25 of 25 live relay posts scored as routed and
    the defect vanished from the measurement. The roster intersection is what
    makes the count mean anything (guard-2421: a probe that cannot see its own
    defect is the first hypothesis to test).
    """
    assert routed_agents(["relay", "peer-thread", "zds-mind", "time-critical"], KNOWN) == []
    # ...and the positive control on the same call, so a broken import or an
    # always-empty return cannot pass this test.
    assert routed_agents(["requires_action_by:zeta"], KNOWN) == ["zeta"]


def test_empty_roster_reports_nothing_routed_not_everything():
    """Absence of a roster is a DIFFERENT measurement, not a degraded one.

    Failing toward "routes to nobody" keeps an unreadable roster visible as a
    reported gap; failing the other way would silently bless every tag as
    routing and restore the flat clean this whole split exists to remove.
    """
    assert routed_agents(["requires_action_by:zeta"], set()) == []
    assert routed_agents(["requires_action_by:zeta"], None) == []


def test_goal_id_tags_never_count_as_routing_targets():
    assert routed_agents(["g-115-6067", "g-1-1"], KNOWN | {"g-1-1"}) == []


def test_unrouted_relay_never_becomes_undelivered():
    """guard-3628 pin: the bare-relay acceptance is DOCUMENTED breadth, so the
    reported gap must not silently re-flag 8 days of historical posts."""
    goals = [_goal("g-1-%d" % i, "Directive: Re: [Omni] item %d" % i) for i in range(1, 4)]
    rows = [{"id": "msg-%d" % i, "tags": ["relay", "g-1-%d" % i]} for i in range(1, 4)]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert out["undelivered"] == []
    assert len(out["relayed"]) == 3
    assert out["relayed_unrouted"] == 3


# --------------------------------------------------------------------------
# The collision set — the half that must never auto-route
# --------------------------------------------------------------------------

def test_ambiguous_name_is_not_routed_to_the_peer():
    """`zeta` is BOTH a local agent and in zds-mind's known_agents."""
    goals = [_goal("g-1-1", "Directive: Re: [Zeta] something")]
    out = sweep(goals, [], REGISTRY, SELF, ROSTER)
    assert not out["undelivered"], "an ambiguous name must not be relayed at a peer"
    assert len(out["ambiguous"]) == 1
    assert "zeta@<env-id>" in out["ambiguous"][0]["reason"]


def test_local_agent_thread_is_ignored():
    assert sweep([_goal("g-1-1", "Directive: Re: [Alpha] x")], [],
                 REGISTRY, SELF, ROSTER)["scanned"] == 0


def test_unknown_bracket_token_is_ignored():
    """Bracket tokens are free text — `[bash]` appears live and is not an agent."""
    assert sweep([_goal("g-1-1", "Directive: Re: [bash] x")], [],
                 REGISTRY, SELF, ROSTER)["scanned"] == 0


def test_display_case_token_matches_lowercase_known_agents():
    """Subject lines say `[Omni]`; known_agents says `omni`. Comparing raw
    matches nothing and reports a permanently clean queue."""
    assert sweep([_goal("g-1-1", "Re: [Omni] x")], [],
                 REGISTRY, SELF, ROSTER)["scanned"] == 1


# --------------------------------------------------------------------------
# Origin-signal family
# --------------------------------------------------------------------------

def test_all_three_inbound_prefixes_match():
    """`alert-email:` 202, `user_directed:` 3, `user-directed:` 1 live."""
    for sig in ("alert-email:k", "user_directed:k", "user-directed:k"):
        assert is_inbound_email_goal({"origin_signal": sig}), sig


def test_agent_filed_goal_is_not_swept():
    goals = [_goal("g-1-1", "Re: [Omni] x", sig="investigate:something")]
    assert sweep(goals, [], REGISTRY, SELF, ROSTER)["scanned"] == 0


def test_extract_bracket():
    assert extract_bracket("Directive: Re: [Omni] DoDEA") == "Omni"
    assert extract_bracket("no brackets here") is None


# --------------------------------------------------------------------------
# Registry SSOT — the name set must not fork
# --------------------------------------------------------------------------

def test_peer_names_exclude_self_env():
    assert peer_agent_names(REGISTRY, SELF) == {"omni", "zeta"}
    assert "ayoai-mind" not in peer_envs(REGISTRY, SELF)


def test_unresolvable_self_env_yields_no_peers():
    """Unsafe direction: every env would read as a peer, including ours."""
    assert peer_envs(REGISTRY, None) == set()
    assert peer_agent_names(REGISTRY, "") == set()
    assert classify_agent_name("omni", REGISTRY, None, ROSTER) == ("unknown", None)


def test_classify_four_verdicts():
    assert classify_agent_name("omni", REGISTRY, SELF, ROSTER) == ("peer", "zds-mind")
    assert classify_agent_name("zeta", REGISTRY, SELF, ROSTER) == ("ambiguous", "zds-mind")
    assert classify_agent_name("alpha", REGISTRY, SELF, ROSTER) == ("local", None)
    assert classify_agent_name("nobody", REGISTRY, SELF, ROSTER) == ("unknown", None)


def test_missing_registry_fails_open_to_no_peers(tmp_path):
    """An unreadable registry must not turn local agents into peers."""
    assert load_env_registry(registry_dir=tmp_path / "nope") == {}


def test_live_registry_declares_the_peer():
    """Guards the real file: if zds-mind stops declaring known_agents, this
    sweep silently stops seeing peer threads."""
    live = load_env_registry()
    if "zds-mind" not in live:      # a deployment without the peer registered
        return
    assert "omni" in peer_agent_names(live, "ayoai-mind"), (
        "zds-mind.yaml must declare known_agents including omni, or the sweep "
        "goes permanently clean"
    )


# --------------------------------------------------------------------------
# Peer acks (2026-08-17) — the INBOUND half. Receipt IS observable here, in one
# form only: a post AUTHORED BY A PEER AGENT that cites the goal id. Every case
# below was measured against the live board shape (author `omni` 40/42, author
# `omni@zds-mind` 2/42, ids cited deep in long per-id dispositions).
# --------------------------------------------------------------------------

def _peer_post(msg_id, text, author="omni", tags=()):
    return {"id": msg_id, "author": author, "text": text, "tags": list(tags),
            "timestamp": "2026-08-16T06:33:24"}


def test_peer_authored_post_citing_id_in_text_is_an_ack_and_supersedes():
    """The live shape: omni cites the id in prose, no relay tag anywhere. The
    goal must land in peer_acked and NOWHERE else — a peer's own citation is
    stronger than any relay tag this side wrote, and stronger than 'no relay'."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] Navy proposal slipped")]
    rows = [_peer_post("msg-omni-1", "RECEIPT CONFIRMED — g-1-1 check 2 is satisfied.")]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert [r["goal_id"] for r in out["peer_acked"]] == ["g-1-1"]
    assert out["peer_acked"][0]["peer_acked_via"] == ["msg-omni-1"]
    assert out["peer_acked"][0]["peer_ack_authors"] == ["omni"]
    assert out["undelivered"] == [] and out["relayed"] == []
    assert out["peer_ack_posts_scanned"] == 1


def test_qualified_peer_author_form_is_accepted():
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    rows = [_peer_post("m", "done: g-1-1", author="omni@zds-mind")]
    assert [r["goal_id"] for r in sweep(goals, rows, REGISTRY, SELF, ROSTER)["peer_acked"]] == ["g-1-1"]


def test_qualified_author_naming_the_wrong_env_is_refused():
    """`omni@ayoai-mind` is nonsense — omni is zds-mind's. A mismatched suffix
    must not be laundered into a peer ack by dropping the suffix."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    rows = [_peer_post("m", "done: g-1-1", author="omni@ayoai-mind")]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert out["peer_acked"] == [] and len(out["undelivered"]) == 1


def test_ambiguous_author_never_acks():
    """zeta is BOTH local and in zds-mind's roster. A zeta post citing the id
    is a local agent talking — reading it as peer receipt is exactly the
    laundered all-clear the module refuses. classify says `ambiguous`; the
    ack index must refuse anything but `peer`."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    rows = [_peer_post("m", "relayed g-1-1 to omni", author="zeta")]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert out["peer_acked"] == [] and len(out["undelivered"]) == 1
    assert out["peer_ack_posts_scanned"] == 0


def test_local_author_never_acks():
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    rows = [_peer_post("m", "I relayed g-1-1", author="alpha")]
    out = sweep(goals, rows, REGISTRY, SELF, ROSTER)
    assert out["peer_acked"] == [] and len(out["undelivered"]) == 1


def test_ack_must_come_from_the_threads_own_peer_env():
    """A peer ack is matched on peer_env, not merely 'some peer'. Build a
    second peer deployment and have ITS agent cite an [Omni] goal: not an ack."""
    reg = dict(REGISTRY)
    reg["other-mind"] = {"environment_id": "other-mind", "known_agents": ["quill"]}
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    rows = [_peer_post("m", "seen g-1-1", author="quill")]
    out = sweep(goals, rows, reg, SELF, ROSTER)
    assert out["peer_acked"] == [] and len(out["undelivered"]) == 1
    # positive control on the same call shape: the RIGHT peer's agent does ack
    rows = [_peer_post("m", "seen g-1-1", author="omni")]
    assert len(sweep(goals, rows, reg, SELF, ROSTER)["peer_acked"]) == 1


def test_citation_deep_in_a_long_disposition_is_found():
    """guard-3712: per-id dispositions put their answers in later sections BY
    CONSTRUCTION. The predicate is a regex over the FULL text; a prefix slice
    of the size a hand reader would print (~200 chars) does not contain it."""
    filler = "HEADLINE, and it is good news for your queue. " * 12   # ~560 chars
    text = filler + "\n  g-1-1 -> DONE: closed on this side."
    assert "g-1-1" not in text[:200], "fixture must put the id past a hand-slice"
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    out = sweep(goals, [_peer_post("m", text)], REGISTRY, SELF, ROSTER)
    assert [r["goal_id"] for r in out["peer_acked"]] == ["g-1-1"]


def test_goal_id_in_tags_of_a_peer_post_is_an_ack():
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    out = sweep(goals, [_peer_post("m", "ack", tags=["g-1-1"])], REGISTRY, SELF, ROSTER)
    assert [r["goal_id"] for r in out["peer_acked"]] == ["g-1-1"]


def test_ack_of_a_different_goal_does_not_bleed():
    """g-1-10 contains the substring g-1-1 and must not ack g-1-1. Pinned by
    greedy `\\d+` in GOAL_ID_ANYWHERE_RE, NOT by its `\\b` anchors — measured by
    mutation 2026-08-17: removing `\\b` leaves this test green, because the
    greedy run consumes '10' whole and 'g-1-1' is never a match. The anchors stay
    as cheap defence against an id glued to letters, but do not read this test
    as proof they work (guard-3860)."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    out = sweep(goals, [_peer_post("m", "done g-1-10 and g-1-12")], REGISTRY, SELF, ROSTER)
    assert out["peer_acked"] == [] and len(out["undelivered"]) == 1


def test_terminal_goals_are_not_acked_or_swept():
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x", status="completed")]
    out = sweep(goals, [_peer_post("m", "g-1-1")], REGISTRY, SELF, ROSTER)
    assert out["scanned"] == 0 and out["peer_acked"] == []


def test_status_keyed_control_unchanged_by_acks():
    """Adding a bucket must not move the falsifying control  requires."""
    goals = [_goal("g-1-1", "Directive: Re: [Omni] x")]
    assert status_keyed_control(goals, REGISTRY, SELF, ROSTER) == 0


# --------------------------------------------------------------------------
# The mutation — close_acked in the wrapper, with the daemon call injected.
# --------------------------------------------------------------------------

def _load_wrapper():
    import importlib.util
    p = Path(__file__).resolve().parents[1] / "peer-thread-relay-sweep.py"
    spec = importlib.util.spec_from_file_location("ptrs", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_close_acked_appends_note_then_completes_and_preserves_prior_note():
    """The daemon's update_goal is a plain REPLACE (goal[field] = value), so the
    note write must carry the prior note forward VERBATIM. A close that erased
    the relay/handoff evidence would be an unexplained close — the class this
    sweep exists to end. Order pins: note first, status second."""
    mod = _load_wrapper()
    calls = []
    def fake_run(script, *args, timeout=90):
        calls.append((script,) + args)
        return ("ok", 0)
    acked = [{"goal_id": "g-1-1", "peer_acked_via": ["msg-omni-1"],
              "peer_ack_authors": ["omni"], "peer_env": "zds-mind"}]
    goals_by_id = {"g-1-1": {"id": "g-1-1", "progress_note": "PRIOR RELAY EVIDENCE"}}
    out = mod.close_acked(acked, goals_by_id, run=fake_run)
    assert out["closed"] == [{"goal_id": "g-1-1", "via": ["msg-omni-1"]}]
    assert out["failed"] == []
    assert len(calls) == 2
    assert calls[0][:3] == ("core/scripts/aspirations-update-goal.sh", "g-1-1", "progress_note")
    note = calls[0][3]
    assert note.startswith("PRIOR RELAY EVIDENCE"), "prior note must be carried forward first"
    assert "msg-omni-1" in note and "omni" in note and "guard-3824" in note
    assert calls[1][:4] == ("core/scripts/aspirations-update-goal.sh", "g-1-1", "status", "completed")


def test_close_acked_does_not_complete_when_note_write_fails():
    """A goal completed WITHOUT its evidence note is exactly the unexplained
    close this exists to end. Note failure must short-circuit the status write."""
    mod = _load_wrapper()
    calls = []
    def fake_run(script, *args, timeout=90):
        calls.append(args)
        return ("daemon refused", 1)
    out = mod.close_acked([{"goal_id": "g-1-1", "peer_acked_via": ["m"]}], {}, run=fake_run)
    assert out["closed"] == []
    assert out["failed"][0]["goal_id"] == "g-1-1" and out["failed"][0]["step"] == "progress_note"
    assert len(calls) == 1, "status must NOT be written after a failed note write"


def test_close_acked_is_fail_soft_per_goal():
    """One refused close must not strand the rest — that is the defect this
    sweep exists to fix, one level down."""
    mod = _load_wrapper()
    def fake_run(script, *args, timeout=90):
        gid = args[0]
        if gid == "g-1-2" and args[1] == "status":
            return ("refused", 1)
        return ("ok", 0)
    acked = [{"goal_id": g, "peer_acked_via": ["m"]} for g in ("g-1-1", "g-1-2", "g-1-3")]
    out = mod.close_acked(acked, {}, run=fake_run)
    assert [c["goal_id"] for c in out["closed"]] == ["g-1-1", "g-1-3"]
    assert [f["goal_id"] for f in out["failed"]] == ["g-1-2"]
    assert out["failed"][0]["step"] == "status"


def test_close_acked_with_no_prior_note_writes_only_ours():
    mod = _load_wrapper()
    calls = []
    def fake_run(script, *args, timeout=90):
        calls.append(args); return ("ok", 0)
    mod.close_acked([{"goal_id": "g-1-1", "peer_acked_via": ["m"]}], {"g-1-1": {}}, run=fake_run)
    assert calls[0][2].startswith("[peer-thread-relay-sweep --close-acked]")


# ---------------------------------------------------------------------------
# _run's own return contract ()
#
# WHY THESE EXIST. Every close_acked test above injects `fake_run`, and those
# fakes return a NON-EMPTY output on failure (e.g. ("daemon refused", 1)) —
# i.e. the fixtures encoded the contract the docstring promises ("failures
# carry the wrapper's rc + output") while the real `_run` returned p.stdout
# alone and so produced ("", 1) for every refusal. The fake was more correct
# than the code, and because nothing exercised `_run` itself, the whole file
# stayed green across that divergence. Measured live 2026-08-20 (zeta, cc-02):
# the failure row for  read {rc: 1, output: ""} while the discarded
# stderr held 12,088 bytes naming the actual refusal.
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout, stderr, returncode):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _patch_subprocess(mod, monkeypatch, proc):
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: proc)


def test_run_falls_back_to_stderr_when_the_call_fails_with_no_stdout(monkeypatch):
    """The live failure shape: wrappers write refusals to stderr and nothing to
    stdout. Returning stdout alone reports every such failure as output:""."""
    mod = _load_wrapper()
    _patch_subprocess(mod, monkeypatch, _FakeProc("", "uncommitted_work_blocked", 1))
    out, rc = mod._run("core/scripts/whatever.sh", "arg")
    assert rc == 1
    assert out == "uncommitted_work_blocked", (
        "a failure with empty stdout must surface stderr, else the failure "
        "reporter drops the failure's own diagnostic: %r" % (out,)
    )


def test_run_does_not_append_stderr_to_a_nonempty_failure_payload(monkeypatch):
    """guard-1963: never merge stderr into a captured data stream. When the call
    failed but still produced stdout, that stdout is the payload and must come
    back unmodified — the substitution is a fallback, not a concatenation."""
    mod = _load_wrapper()
    _patch_subprocess(mod, monkeypatch, _FakeProc('{"partial": true}', "noise", 1))
    out, rc = mod._run("core/scripts/whatever.sh")
    assert (out, rc) == ('{"partial": true}', 1)


def test_run_never_returns_stderr_on_success(monkeypatch):
    """The success path is the one load_goals/load_board json-parse, and they
    DISCARD the rc — so stderr leaking in here would corrupt a live payload
    rather than merely confuse a log line. A wrapper that warns on stderr while
    succeeding is ordinary; its warning must not reach the parser."""
    mod = _load_wrapper()
    _patch_subprocess(mod, monkeypatch, _FakeProc('[{"id": "g-1-1"}]', "WARN: slow", 0))
    out, rc = mod._run("core/scripts/aspirations-read.sh")
    assert (out, rc) == ('[{"id": "g-1-1"}]', 0)
    assert "WARN" not in out
