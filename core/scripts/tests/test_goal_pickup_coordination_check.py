"""Tests for goal-pickup-coordination-check.py ( / US-03).

Advisory same-surface-race probe at goal-pickup. These tests pin the pure
classifier contract — path/keyword extraction, commit-goal-id parse, path
overlap, and the overlap classifier including the canonical 2026-05-13 race
(partner already shipped) and the own-goal exclusion. The git/daemon/team-state
reads in main() are impure and otherwise exercised only at runtime — the sole
exception is the telemetry test at the bottom (g-115-3626), which spawns main()
because asserting the `_gate_log.log(...)` call exists in the source would prove
wiring and not execution (guard-1451).

Pattern: importlib + sys.path (the script name has hyphens, so it cannot be a
plain `import`), per test_defer_drift_check.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "goal-pickup-coordination-check.py"


def _import():
    spec = importlib.util.spec_from_file_location(
        "goal_pickup_coordination_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["goal_pickup_coordination_check"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _import()


def _commit(h, subject, files):
    return {"hash": h, "subject": subject, "files": files}


# ── extract_paths ────────────────────────────────────────────────────────────

def test_extract_paths_governed_roots():
    text = "Edit core/scripts/aspirations-select/SKILL.md and .claude/rules/foo.md"
    paths = M.extract_paths(text)
    assert "core/scripts/aspirations-select/SKILL.md" in paths
    assert ".claude/rules/foo.md" in paths


def test_extract_paths_bare_filename():
    paths = M.extract_paths("the fix lives in goal-selector.py and team-state.yaml")
    assert "goal-selector.py" in paths
    assert "team-state.yaml" in paths


def test_extract_paths_rejects_bare_extension():
    # A bare extension with no filename stem must NOT be captured (too broad).
    assert M.extract_paths("update the .py files and .md docs") == set()


# ── extract_keywords ─────────────────────────────────────────────────────────

def test_extract_keywords_keeps_compounds_drops_filler():
    kw = M.extract_keywords(
        "Add coordination check at goal-pickup to prevent same-surface races")
    assert {"coordination", "goal-pickup", "same-surface", "races"} <= kw
    for filler in ("add", "check", "at", "to", "prevent"):
        assert filler not in kw


def test_extract_keywords_strips_goal_id_and_scope():
    kw = M.extract_keywords("feat(g-305-03): coordination guard")
    assert "coordination" in kw and "guard" in kw
    assert not any(t.startswith("g-305") for t in kw)
    assert "feat" not in kw  # conventional-commit scope stripped


# ── commit_goal_id ───────────────────────────────────────────────────────────

def test_commit_goal_id_from_scope():
    assert M.commit_goal_id("feat(g-115-697): same-surface fix") == "g-115-697"


def test_commit_goal_id_none_when_absent():
    assert M.commit_goal_id("chore(gate-d): validator tweak") is None
    assert M.commit_goal_id("") is None


# ── _path_overlap ────────────────────────────────────────────────────────────

def test_path_overlap_same_basename():
    assert M._path_overlap("core/scripts/goal-selector.py",
                           "core/scripts/goal-selector.py")
    assert M._path_overlap("goal-selector.py", "core/scripts/goal-selector.py")


def test_path_overlap_dir_prefix():
    assert M._path_overlap("core/scripts", "core/scripts/foo.py")


def test_path_overlap_bare_dir_no_false_basename():
    # A dot-less directory token must not basename-match an unrelated file.
    assert not M._path_overlap("docs", "core/scripts/foo.py")


def test_path_overlap_rejects_partial_filename_suffix():
    # A bare basename must NOT match a different file whose name merely ENDS with
    # the same suffix without a path boundary (regression for the dropped
    # `c.endswith(a)` over-match — fresh-eyes  / board msg-1989).
    assert not M._path_overlap("test.py", "core/scripts/tests/contest.py")
    assert not M._path_overlap("self.md", "agents/bravo/myself.md")
    assert not M._path_overlap("bar.py", "core/scripts/foobar.py")
    # A genuine path-boundary suffix still matches.
    assert M._path_overlap("test.py", "core/scripts/test.py")


# ── classify_overlap ─────────────────────────────────────────────────────────

def test_classify_overlap_canonical_2026_05_13_path_match():
    # The incident: a partner shipped  touching the same file that
    #  (about to be claimed) would. Surface (file) overlap → race.
    affected = {"core/scripts/aspirations-select.py"}
    kw = M.extract_keywords("Refactor aspirations-select scoring")
    commits = [_commit("abc123",
                       "feat(g-115-697): rework aspirations-select scoring",
                       ["core/scripts/aspirations-select.py"])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-115-696")
    assert race is True
    assert overlapping[0]["committed_goal_id"] == "g-115-697"
    assert "core/scripts/aspirations-select.py" in overlapping[0]["matched_paths"]


def test_classify_overlap_keyword_only():
    affected = set()
    kw = {"coordination", "goal-pickup", "same-surface"}
    commits = [_commit("d1",
                       "feat(g-200-01): goal-pickup coordination same-surface guard",
                       [])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-305-03")
    assert race is True
    assert len(overlapping[0]["matched_keywords"]) >= 2


def test_classify_overlap_excludes_own_goal():
    # A commit whose scope IS the claiming goal id is the agent's own WIP, not
    # a race (e.g. autocompact-resume re-claim) — must be excluded.
    affected = {"core/scripts/foo.py"}
    commits = [_commit("self1", "feat(g-305-03): wip on this very goal",
                       ["core/scripts/foo.py"])]
    race, overlapping = M.classify_overlap(affected, set(), commits, "g-305-03")
    assert race is False
    assert overlapping == []


def test_classify_overlap_no_overlap():
    affected = {"core/scripts/foo.py"}
    kw = {"coordination"}
    commits = [_commit("x1", "feat(g-999-99): unrelated client runtime change",
                       ["src/client/Bar.lua"])]
    race, _ = M.classify_overlap(affected, kw, commits, "g-305-03")
    assert race is False


def test_classify_overlap_single_keyword_below_threshold():
    affected = set()
    kw = {"coordination", "races"}
    commits = [_commit("y1", "feat(g-1-1): coordination of something unrelated", [])]
    # Only "coordination" is shared (1) — below the default threshold of 2.
    race, _ = M.classify_overlap(affected, kw, commits, "g-305-03",
                                 min_shared_keywords=2)
    assert race is False


# ── : boilerplate goal-vocabulary stopwording ──────────────────────

def test_extract_keywords_drops_boilerplate_goal_vocab():
    # : agent/user/participants/field/etc. are boilerplate goal-record
    # vocabulary with no same-surface identity; they must be stopworded so
    # race_risk fires on substantive overlap, not goal boilerplate.
    kw = M.extract_keywords(
        "Idea: set participants agent user field verification status priority")
    for boilerplate in ("agent", "user", "participant", "participants", "field",
                        "verification", "status", "priority", "source",
                        "aspiration", "category"):
        assert boilerplate not in kw, f"{boilerplate} must be stopworded (g-115-1425)"


def test_classify_overlap_boilerplate_only_no_race():
    #  canonical FP (): when a goal's only keyword overlap with
    # a recent commit is boilerplate goal-vocabulary ("agent", "user"), it must
    # NOT flag a same-surface race. Mirrors the merge-authority 286090d7d
    # incident (matched_paths empty, "agent"+"user" the sole shared tokens).
    affected = set()
    kw = M.extract_keywords("Set participants agent user on this goal")
    commits = [_commit(
        "ma1", "feat(g-999-01): merge-authority grants agent user participants", [])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-115-1425")
    assert race is False
    assert overlapping == []


def test_classify_overlap_substantive_token_still_races():
    # Guard against over-stopping: a SUBSTANTIVE shared token alongside
    # boilerplate must still flag (2-keyword threshold met by real tokens).
    affected = set()
    kw = M.extract_keywords("Refactor coordination scoring for agent user")
    commits = [_commit(
        "s1", "feat(g-888-02): coordination scoring rework for the agent", [])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-115-1425")
    assert race is True
    assert {"coordination", "scoring"} <= set(overlapping[0]["matched_keywords"])


# ── _basename_stem () ──────────────────────────────────────────────

def test_basename_stem_strips_dir_and_extension():
    assert M._basename_stem(
        "src/main/java/com/ayoai/IntentEngineVerticle.java") == "intentengineverticle"
    assert M._basename_stem("core/scripts/goal-selector.py") == "goal-selector"
    assert M._basename_stem("team-state.yaml") == "team-state"


def test_basename_stem_edge_cases():
    assert M._basename_stem("") == ""
    assert M._basename_stem("noext") == "noext"          # no extension
    assert M._basename_stem("foo.test.py") == "foo.test"  # only final ext stripped


# ── classify_uncommitted_overlap () ────────────────────────────────

def test_classify_uncommitted_bare_class_name_stem_match():
    # THE incident: a goal names a class WITHOUT an extension
    # ('IntentEngineVerticle'), so extract_paths returns empty and the commit
    # probe's path signal can't fire. A partner is editing
    # 'IntentEngineVerticle.java' uncommitted (in_flight). The basename-stem
    # match — file stem 'intentengineverticle' ∩ goal keyword set — catches it.
    affected = set()
    kw = M.extract_keywords(
        "Investigate: probe missed concurrent IntentEngineVerticle edit")
    assert "intentengineverticle" in kw          # extension-less class name survives
    uncommitted = ["src/main/java/com/ayoai/IntentEngineVerticle.java"]
    matched = M.classify_uncommitted_overlap(affected, kw, uncommitted)
    assert len(matched) == 1
    assert matched[0]["matched_stem"] == "intentengineverticle"
    assert matched[0]["matched_paths"] == []


def test_classify_uncommitted_path_match():
    # When the goal DID name a path, the path signal fires (same as commits),
    # and unrelated uncommitted files (telemetry) are not matched.
    affected = {"core/scripts/goal-selector.py"}
    uncommitted = ["core/scripts/goal-selector.py", "agents/zeta/journal.jsonl"]
    matched = M.classify_uncommitted_overlap(affected, set(), uncommitted)
    assert len(matched) == 1
    assert matched[0]["file"] == "core/scripts/goal-selector.py"
    assert "core/scripts/goal-selector.py" in matched[0]["matched_paths"]


def test_classify_uncommitted_no_match():
    affected = {"core/scripts/foo.py"}
    kw = {"coordination"}
    uncommitted = ["agents/zeta/health/2026-06-16.jsonl", "world/changelog.jsonl"]
    assert M.classify_uncommitted_overlap(affected, kw, uncommitted) == []


def test_classify_uncommitted_stem_carries_no_boilerplate():
    # The stem path matches against the goal KEYWORD set, which extract_keywords
    # already stripped of stopwords / sub-4-char tokens. So a boilerplate-only
    # title yields an empty keyword set and a stem like 'status' cannot match
    # status.py — no self-FP on goal-record vocabulary ( family).
    affected = set()
    kw = M.extract_keywords("Set status field on the goal")  # -> empty
    assert kw == set()
    uncommitted = ["core/scripts/status.py"]
    assert M.classify_uncommitted_overlap(affected, kw, uncommitted) == []


# ── classify_board_mentions ( board probe) ─────────────────────────

def _msg(mid, author, mtype, text, tags=None, ts="2026-07-09T15:50:08"):
    return {"id": mid, "author": author, "timestamp": ts, "type": mtype,
            "text": text, "tags": tags or []}


def test_board_claim_by_type():
    #  contract: a type=claim post is claim-kind for the id it
    # STRUCTURALLY claims — extracted from goal-id-shaped tags (the ceremony
    # always tags the claimed id) or a "claim:/Claiming <id>" text prefix.
    msgs = [_msg("m1", "alpha", "claim", "picked up g-115-1876",
                 tags=["g-115-1876", "alpha"])]
    hits = M.classify_board_mentions("g-115-1876", "bravo", msgs)
    assert len(hits) == 1 and hits[0]["kind"] == "claim"


def test_board_claim_by_type_body_mention_only_dropped():
    # : a claim post with NO parseable claimed id (no goal-shaped
    # tag, no claim prefix) does NOT become claim-kind via a body mention —
    # body mentions are citations, dropped like any bare mention.
    msgs = [_msg("m1", "alpha", "claim", "picked up g-115-1876")]
    assert M.classify_board_mentions("g-115-1876", "bravo", msgs) == []


def test_board_claim_prefix_text_form_extracts_id():
    # The  atomic-announce text shape, tags absent: the claimed id
    # comes from the "claim: <id> — <title>" prefix.
    msgs = [_msg("m1", "alpha", "claim", "claim: g-115-1876 — fix the gate")]
    hits = M.classify_board_mentions("g-115-1876", "bravo", msgs)
    assert len(hits) == 1 and hits[0]["kind"] == "claim"


def test_board_claim_post_citing_another_goal_dropped():
    # Live FP specimen msg-20260713-171224-alpha-5101 (): alpha's
    # atomic claim-announce FOR  cites -c in its body
    # ("clears -c Layer 3 suite-green gate"). Probing -c
    # must NOT see a claim (the pre-fix type-only leg did, and the digest
    # branch would have wrongly yielded); probing  must.
    text = ("claim: g-115-2104 — make test_compact_restore_preserves_live_"
            "loop_state daemon-agnostic (direct WM-file read like test 2/4); "
            "clears g-115-2084-c Layer 3 suite-green gate. rb-3331.")
    msgs = [_msg("m1", "alpha", "claim", text,
                 tags=["g-115-2104", "alpha", "framework"])]
    assert M.classify_board_mentions("g-115-2084-c", "bravo", msgs) == []
    hits = M.classify_board_mentions("g-115-2104", "bravo", msgs)
    assert len(hits) == 1 and hits[0]["kind"] == "claim"


def test_board_claim_by_text_prefix():
    # The Phase-4 ceremony shape: type=status but text "Claiming <id> ...".
    msgs = [_msg("m1", "alpha", "status", "Claiming g-115-1876 for the fix",
                 tags=["claim", "g-115-1876"])]
    hits = M.classify_board_mentions("g-115-1876", "bravo", msgs)
    assert len(hits) == 1 and hits[0]["kind"] == "claim"


def test_board_complete_by_text_prefix():
    # The canonical 2026-07-09 contentless completion post shape.
    msgs = [_msg("m1", "bravo", "status", "Completed: g-115-1876 [g-115-1876]")]
    hits = M.classify_board_mentions("g-115-1876", "alpha", msgs)
    assert len(hits) == 1 and hits[0]["kind"] == "complete"


def test_board_own_author_excluded():
    msgs = [_msg("m1", "bravo", "claim", "Claiming g-115-1876")]
    assert M.classify_board_mentions("g-115-1876", "bravo", msgs) == []


def test_board_bare_mention_dropped():
    # Findings/insight posts cite goal ids topically — must NOT flip race_risk.
    msgs = [_msg("m1", "alpha", "finding",
                 "the claim collision on g-115-1876 was informative",
                 tags=["affects:g-115-1876"])]
    assert M.classify_board_mentions("g-115-1876", "bravo", msgs) == []


def test_board_claim_for_other_goal_dropped():
    # A partner's claim post for a DIFFERENT goal that merely mentions this
    # goal-id in its narrative is a mention, not a claim on THIS goal.
    msgs = [_msg("m1", "alpha", "status",
                 "Claiming g-001-311 re the claim collision on g-115-1876",
                 tags=["claim", "g-001-311"])]
    assert M.classify_board_mentions("g-115-1876", "bravo", msgs) == []


def test_board_recurring_skips_completions_keeps_claims():
    msgs = [
        _msg("m1", "alpha", "status", "Completed: g-115-151 bitnet probe"),
        _msg("m2", "alpha", "claim", "working g-115-151 now",
             tags=["g-115-151", "alpha"]),
    ]
    hits = M.classify_board_mentions("g-115-151", "bravo", msgs,
                                     goal_recurring=True)
    assert [h["id"] for h in hits] == ["m2"]


def test_board_recurring_drops_stale_prior_cycle_claim():
    # : a recurring-goal CLAIM whose timestamp PRE-DATES the goal's
    # lastAchievedAt was a claim for an already-completed prior cycle — history,
    # not a live race. It must be dropped when goal_last_achieved is passed.
    # (Incident: echo claim 15:19:21 pre-dated  lastAchievedAt
    # 15:27:15, causing an unnecessary yield of a due, collision-safe goal.)
    msgs = [_msg("stale", "echo", "claim", "claiming g-115-105",
                 tags=["g-115-105", "echo"], ts="2026-07-23T15:19:21")]
    # Without lastAchievedAt: claim counts (baseline preserved).
    hits = M.classify_board_mentions("g-115-105", "foxtrot", msgs,
                                     goal_recurring=True)
    assert [h["id"] for h in hits] == ["stale"]
    # With lastAchievedAt AFTER the claim: stale-cycle claim dropped.
    hits = M.classify_board_mentions(
        "g-115-105", "foxtrot", msgs, goal_recurring=True,
        goal_last_achieved="2026-07-23T15:27:15")
    assert hits == []


def test_board_recurring_keeps_fresh_claim_after_last_achieved():
    # A claim AFTER lastAchievedAt is a live race for the current cycle — kept.
    msgs = [_msg("fresh", "echo", "claim", "claiming g-115-105",
                 tags=["g-115-105", "echo"], ts="2026-07-23T15:30:00")]
    hits = M.classify_board_mentions(
        "g-115-105", "foxtrot", msgs, goal_recurring=True,
        goal_last_achieved="2026-07-23T15:27:15")
    assert [h["id"] for h in hits] == ["fresh"]


# ── agent-queue namespace () ──────────────────────────────────────
# Agent-queue goal ids are per-agent records, so the same id names N different
# goals across the fleet. The pre-existing stale-prior-cycle drop compares the
# partner's claim against the PROBER's lastAchievedAt, which is the wrong record
# by construction once N > 1.

def test_is_private_agent_queue_predicate():
    # World: one id, one record — never private, semantics unchanged.
    assert M.is_private_agent_queue("world") is False
    assert M.is_private_agent_queue("world", "bravo") is False
    # Own agent-queue goal: per-agent record, no partner can contend it.
    assert M.is_private_agent_queue("agent") is True
    assert M.is_private_agent_queue("agent", None) is True
    assert M.is_private_agent_queue("agent", "") is True
    # Cross-agent: ONE shared record that genuinely can be contended. This case
    # is why the predicate is not `source == "agent"` — blanket-exempting
    # source=agent would drop the only real race this lane must still catch.
    assert M.is_private_agent_queue("agent", "bravo") is False


def test_board_agent_queue_private_drops_partner_claim_live_specimen():
    # Live specimen, measured 2026-08-09T21:38 (bravo, cc-05): bravo probed its
    # OWN  (lastAchievedAt 2026-08-02T02:34:25, interval 31.0h) and got
    # race_risk=true with EVERY other evidence lane empty. The sole contributor
    # was echo's claim on ECHO's own  (interval 5.33h, its own
    # lastAchievedAt 2026-08-09T20:35:29 — 11 min AFTER its own claim, i.e. echo
    # had already finished its copy 63 min before bravo probed).
    msgs = [_msg("msg-20260809-202426-echo-7195", "echo", "claim",
                 "Claiming g-001-01: Reflect and journal",
                 tags=["g-001-01", "echo"], ts="2026-08-09T20:24:26")]

    # POSITIVE CONTROL — the specimen must reproduce the false positive on the
    # pre-fix path, or this test would pass for the wrong reason. echo's claim
    # (20:24:26) is NEWER than the prober's lastAchievedAt (2026-08-02), so the
    #  stale-cycle drop keeps it and the digest hard-yields.
    hits = M.classify_board_mentions(
        "g-001-01", "bravo", msgs, goal_recurring=True,
        goal_last_achieved="2026-08-02T02:34:25")
    assert [h["id"] for h in hits] == ["msg-20260809-202426-echo-7195"]
    assert hits[0]["kind"] == "claim"  # claim-kind is the hard-yield path

    # FIXED: the id names bravo's own per-agent record, so echo's post is about
    # a different goal entirely and no board hit survives.
    hits = M.classify_board_mentions(
        "g-001-01", "bravo", msgs, goal_recurring=True,
        goal_last_achieved="2026-08-02T02:34:25", agent_queue_private=True)
    assert hits == []


def test_board_agent_queue_private_drop_is_independent_of_last_achieved():
    # The namespace drop must NOT depend on lastAchievedAt at all — that field is
    # exactly the one that names the wrong record here. A partner mid-cycle on
    # its OWN copy (claim NEWER than its own lastAchievedAt) still cannot race
    # this record, so the stale-cycle comparison could never have caught it.
    msgs = [_msg("live", "echo", "claim", "Claiming g-001-01: Reflect and journal",
                 tags=["g-001-01", "echo"], ts="2026-08-09T20:24:26")]
    for la in (None, "2026-08-02T02:34:25", "2026-08-09T23:59:59"):
        assert M.classify_board_mentions(
            "g-001-01", "bravo", msgs, goal_recurring=True,
            goal_last_achieved=la, agent_queue_private=True) == []


def test_board_cross_agent_goal_keeps_partner_claim():
    # Outcome 2: a cross-agent goal names ONE shared record. Partner claims stay
    # in scope, and the existing stale-cycle semantics still apply to it.
    msgs = [_msg("live", "echo", "claim", "claiming g-001-01",
                 tags=["g-001-01", "echo"], ts="2026-08-09T20:24:26")]
    private = M.is_private_agent_queue("agent", "bravo")  # cross-agent owner set
    assert private is False
    hits = M.classify_board_mentions(
        "g-001-01", "alpha", msgs, goal_recurring=True,
        goal_last_achieved="2026-08-09T10:00:00", agent_queue_private=private)
    assert [h["id"] for h in hits] == ["live"]


def test_board_world_source_semantics_unchanged():
    # Outcome 3: world goals are never private, so the  stale-cycle
    # drop keeps governing them exactly as before.
    msgs = [_msg("stale", "echo", "claim", "claiming g-115-105",
                 tags=["g-115-105", "echo"], ts="2026-07-23T15:19:21")]
    private = M.is_private_agent_queue("world")
    assert private is False
    assert M.classify_board_mentions(
        "g-115-105", "foxtrot", msgs, goal_recurring=True,
        goal_last_achieved="2026-07-23T15:27:15",
        agent_queue_private=private) == []          # stale-cycle drop still fires
    assert [h["id"] for h in M.classify_board_mentions(
        "g-115-105", "foxtrot", msgs, goal_recurring=True,
        agent_queue_private=private)] == ["stale"]  # and without it, still kept


def test_board_stale_drop_failsafe_unparseable_ts_keeps_claim():
    # Fail-safe: an unparseable timestamp must KEEP the claim (conservative —
    # a false yield is safer than a missed race). Also: the drop is
    # recurring-only — a non-recurring goal never drops on staleness.
    msgs = [_msg("bad", "echo", "claim", "claiming g-115-105",
                 tags=["g-115-105", "echo"], ts="not-a-timestamp")]
    hits = M.classify_board_mentions(
        "g-115-105", "foxtrot", msgs, goal_recurring=True,
        goal_last_achieved="2026-07-23T15:27:15")
    assert [h["id"] for h in hits] == ["bad"]
    # Non-recurring goal: staleness drop does not apply even with a stale ts.
    msgs2 = [_msg("nr", "echo", "claim", "claiming g-115-105",
                  tags=["g-115-105", "echo"], ts="2026-07-23T15:19:21")]
    hits2 = M.classify_board_mentions(
        "g-115-105", "foxtrot", msgs2, goal_recurring=False,
        goal_last_achieved="2026-07-23T15:27:15")
    assert [h["id"] for h in hits2] == ["nr"]


def test_board_empty_me_returns_nothing():
    # MIND_AGENT injection is fail-open and can drop (bravo-fec 2026-07-13):
    # with me="" every author passes the partner filter, so the agent's OWN
    # claim post would flag as a partner claim on an autocompact re-claim
    # probe. Falsy me must yield [] (no-hits is the advisory-safe direction).
    msgs = [_msg("m1", "alpha", "claim", "Claiming g-115-1876")]
    assert M.classify_board_mentions("g-115-1876", "", msgs) == []
    assert M.classify_board_mentions("g-115-1876", None, msgs) == []


# ── _git_log_commits (stale-clone fetch, ) ─────────────────────────
# The  miss: the race scan ran on a clone whose last pull predated 20h
# of upstream commits, so the REAL overlap (partner's pushed fix) was invisible
# to a HEAD-only `git log`. The fix fetches remote-tracking refs first and
# scans --all. These fixture tests exercise the impure helper against real tmp
# repos (no daemon, no shared state).

import subprocess as _sp


def _run_git(args, cwd):
    _sp.run(["git", "-c", "user.email=test@test", "-c", "user.name=test",
             *args], cwd=str(cwd), check=True,
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)


def test_git_log_commits_sees_origin_only_commit(tmp_path, monkeypatch):
    # partner clone: seed history, then a bare origin both boxes share.
    partner = tmp_path / "partner"
    partner.mkdir()
    _run_git(["-c", "init.defaultBranch=main", "init", "-q"], partner)
    (partner / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(["add", "."], partner)
    _run_git(["commit", "-q", "-m", "chore(seed): base"], partner)
    origin = tmp_path / "origin.git"
    _run_git(["clone", "-q", "--bare", str(partner), str(origin)], tmp_path)
    _run_git(["remote", "add", "origin", str(origin)], partner)
    # "my box" clones BEFORE the partner ships the overlap commit.
    mine = tmp_path / "mine"
    _run_git(["clone", "-q", str(origin), str(mine)], tmp_path)
    # partner ships the overlap commit and pushes; mine never pulls.
    sub = partner / "core" / "scripts"
    sub.mkdir(parents=True)
    (sub / "target-surface.py").write_text("x = 1\n", encoding="utf-8")
    _run_git(["add", "."], partner)
    _run_git(["commit", "-q", "-m",
              "feat(g-999-01): rework target-surface race scan"], partner)
    _run_git(["push", "-q", "origin", "main"], partner)
    # Scan from the stale clone: the commit exists ONLY on origin.
    monkeypatch.setattr(M, "PROJECT_ROOT", mine)
    commits = M._git_log_commits(2.0)
    subjects = [c["subject"] for c in commits]
    assert any("target-surface" in s for s in subjects), subjects
    # And the classifier flags it on path overlap (end-to-end for the fix).
    race, overlapping = M.classify_overlap(
        {"core/scripts/target-surface.py"}, set(), commits, "g-115-0000")
    assert race and overlapping[0]["committed_goal_id"] == "g-999-01"


def test_git_log_commits_no_remote_fail_open(tmp_path, monkeypatch):
    # A repo with NO remote (fresh world, test fixture): the fetch fails
    # silently and the scan still returns local commits — the pre-fix
    # behavior, no regression (fail-open contract).
    solo = tmp_path / "solo"
    solo.mkdir()
    _run_git(["-c", "init.defaultBranch=main", "init", "-q"], solo)
    (solo / "a.txt").write_text("a\n", encoding="utf-8")
    _run_git(["add", "."], solo)
    _run_git(["commit", "-q", "-m", "feat(g-999-02): local-only work"], solo)
    monkeypatch.setattr(M, "PROJECT_ROOT", solo)
    commits = M._git_log_commits(2.0)
    assert any("local-only" in c["subject"] for c in commits)


# ── : product-repo surface extension ───────────────────────────────
# The check was blind to AGENT_WRITE_PATH product repos — the  shape
# (deliverable PR shipped ~24h before claim, only a sibling mind commit
# flagged). These tests pin the pure detection/classification contract plus
# the impure scan end-to-end on synthetic repos.

# detect_product_surfaces (pure) ----------------------------------------------

# 'acme' (brand-like prefix) appears in 7/8 names -> frequency-suppressed;
# 'operator' in 2/8 -> distinctive; full names always match.
_REPO_NAMES = [
    "acme-operator-api", "deploy-acme-operator", "acme-widget-service",
    "acme-billing-core", "acme-ingest-core", "acme-notify-core",
    "acme-portal-site", "standalone-tooling",
]


def test_detect_full_repo_name_matches():
    labels, matched = M.detect_product_surfaces(
        "Fix the session pool in acme-widget-service before the release",
        _REPO_NAMES)
    assert "acme-widget-service" in labels
    assert matched == ["acme-widget-service"]


def test_detect_distinctive_token_selects_repo_family():
    # Goal prose says just 'operator' (no full repo name) — the 
    # trigger list names exactly this case. The token is distinctive (2/8
    # names < thresh 3) so it selects the operator-family repos.
    labels, matched = M.detect_product_surfaces(
        "Add the start-session endpoint to the operator API", _REPO_NAMES)
    assert "operator" in labels
    assert set(matched) == {"acme-operator-api", "deploy-acme-operator"}


def test_detect_full_name_ordered_before_token_family():
    # The bounded network budget spends on matched[:3] — a full-name match is
    # the strongest statement of WHICH repo the goal means, so it must come
    # FIRST (live 2026-07-17 replay: a leaked token family burned all 3 slots
    # on alphabetically-early repos and the deliverable repo got no PR search).
    labels, matched = M.detect_product_surfaces(
        "Author the deploy-acme-operator PR for the operator start-session",
        _REPO_NAMES)
    assert matched[0] == "deploy-acme-operator"
    assert set(matched) == {"acme-operator-api", "deploy-acme-operator"}


def test_detect_brand_prefix_frequency_suppressed():
    # 'acme' appears in 7/8 repo names — an org/brand prefix is
    # non-distinctive BY FREQUENCY (no hardcoded vocabulary; domain-free).
    labels, matched = M.detect_product_surfaces(
        "General acme work on the loop", _REPO_NAMES)
    assert labels == set() and matched == []


def test_detect_brand_prefix_suppressed_at_fleet_scale():
    # The live off-by-one (2026-07-17): 13 brand-prefixed names at a 56-name
    # fleet slipped a 25% threshold (14). The 12.5% threshold catches it.
    fleet = [f"brandx-svc-{i:02d}" for i in range(13)] + [
        f"tool-{c}" for c in "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopq"[:43]]
    assert len(fleet) == 56
    labels, matched = M.detect_product_surfaces(
        "General brandx maintenance", fleet)
    assert matched == []  # 13 owners >= max(3, ceil(56/8)=7) -> suppressed


def test_detect_generic_tokens_never_trigger():
    # server/status/api/... are generic surface tokens; framework goals say
    # them constantly and must not trigger a product scan.
    labels, matched = M.detect_product_surfaces(
        "Restart the server and check status of the api gateway",
        _REPO_NAMES)
    assert labels == set() and matched == []


def test_detect_write_path_literal_adds_label_only():
    labels, matched = M.detect_product_surfaces(
        "Commit the fix under /opt/work/acme and push",
        _REPO_NAMES, write_path_entries=("/opt/work/acme",))
    assert "acme" in labels
    assert matched == []  # path match triggers the scan, no network focus


def test_detect_empty_inputs():
    assert M.detect_product_surfaces("", _REPO_NAMES) == (set(), [])
    assert M.detect_product_surfaces("operator work", []) == (set(), [])


# ── : served-domain tier ───────────────────────────────────────────
# THE  SHAPE, reproduced structurally. Goal prose names two DOMAINS
# and neither deliverable repo NAME; a brand token leaks a 13-repo family; the
# caller spends its <=3 budget on matched[:3]. Measured on the live estate the
# deliverable repo sat at index 7 of `matched` and was truncated away, so the
# PR probe searched three alphabetical neighbours and reported 0pr.

# 13 brand-prefixed repos against a 110-name denominator -> thresh 14, so
# 'brandx' leaks by ONE. This is the live off-by-one re-opened by unioning the
# convention catalog into the denominator (56 -> 110 names lifts thresh 7 ->
# 14). test_detect_brand_prefix_suppressed_at_fleet_scale above pins the
# 56-name case where the same token is correctly suppressed; this fixture pins
# what happens at the inflated denominator, and the two must both hold.
# 'brandx-www-site' sorts LAST inside its own leaked family (after every
# brandx-svc-NN), which is what puts it outside the [:3] budget — the live
# shape, where the deliverable repo sat at index 7. 'orbital-checkout-app'
# shares NO token with the domain that serves it, so the token pass cannot
# reach it by accident; that is the live zacharykysar.com ->
# Zak-Data-Solutions-Web-App relationship, and it is why a domain map is
# needed rather than a smarter tokenizer.
_LEAKY_FLEET = (
    [f"brandx-svc-{i:02d}" for i in range(11)]
    + ["brandx-portal-app", "brandx-www-site", "orbital-checkout-app"]
    + [f"unrelated-tool-{i:02d}" for i in range(96)]
)
_LEAKY_DOMAINS = {
    "brandx-demo.com": {"brandx-www-site"},
    "widgetco-demo.com": {"orbital-checkout-app"},
}


def test_leaky_fleet_fixture_reproduces_the_off_by_one():
    # Guards the fixture itself: if a future edit changes the counts, the
    # regression below would pass for the wrong reason (the leak gone rather
    # than the domain tier working). 13 owners < thresh 14 -> still leaks.
    names = [n for n in _LEAKY_FLEET if len(n) >= 5]
    assert len(names) == 110
    assert max(3, (len(names) + 7) // 8) == 14
    assert sum(1 for n in names if n.startswith("brandx")) == 13


def test_detect_domain_promotes_deliverable_repo_past_leaked_token():
    prose = ("Fix: brand mark renders in two cases on brandx-demo.com "
             "(footer + og:image:alt) and widgetco-demo.com")
    # BEFORE (no domain map) — the pre-fix behaviour, asserted so this case
    # cannot silently start passing for an unrelated reason. The two repos
    # fail in DIFFERENT ways, which is why both are pinned: one is found and
    # truncated, the other is never found at all.
    _, before = M.detect_product_surfaces(prose, _LEAKY_FLEET)
    assert "brandx-www-site" in before, "fixture must leak, not miss"
    assert before.index("brandx-www-site") > 2, (
        "pre-fix, the deliverable repo must fall outside the <=3 budget")
    assert "orbital-checkout-app" not in before, (
        "pre-fix, a repo named only by its domain is never matched at all")
    # AFTER — both deliverable repos inside the budget window.
    labels, matched = M.detect_product_surfaces(
        prose, _LEAKY_FLEET, domain_repos=_LEAKY_DOMAINS)
    assert {"brandx-demo.com", "widgetco-demo.com"} <= labels
    assert set(matched[:3]) >= {"brandx-www-site", "orbital-checkout-app"}


def test_detect_domain_matches_www_prefixed_prose():
    # One www-stripped key must match both spellings — prose in the wild says
    # 'https://www.brandx-demo.com/' as often as the bare registrable form.
    labels, matched = M.detect_product_surfaces(
        "the footer at https://www.brandx-demo.com/ renders it capitalised",
        _LEAKY_FLEET, domain_repos=_LEAKY_DOMAINS)
    assert "brandx-demo.com" in labels
    assert matched[0] == "brandx-www-site"


def test_detect_full_name_still_outranks_domain():
    # A repo the prose NAMES is a stronger statement than one it implies via a
    # domain, so the explicit name keeps slot 0.
    _, matched = M.detect_product_surfaces(
        "port the orbital-checkout-app footer fix to brandx-demo.com",
        _LEAKY_FLEET, domain_repos=_LEAKY_DOMAINS)
    assert matched[0] == "orbital-checkout-app"
    assert "brandx-www-site" in matched[:3]


def test_detect_domain_boundary_rejects_neighbouring_registrable_domains():
    # Found by fresh-eyes review of THIS goal's own first draft. `_bounded`'s
    # class is [a-z0-9], which excludes letters but not '-' or '.', so
    # 'brandx-demo.com' matched inside 'evil-brandx-demo.com' and
    # 'brandx-demo.com.attacker.net'. Both are different registrable domains
    # and each would have taken one of the three network slots — the exact
    # wrong-repo-in-the-budget failure this goal exists to fix.
    accept = [
        "we changed brandx-demo.com today",              # bare
        "see https://www.brandx-demo.com/ now",          # www (design intent)
        "the api at api.brandx-demo.com responds",       # subdomain
        "the outage was at brandx-demo.com.",            # sentence period
    ]
    reject = [
        "the unrelated host evil-brandx-demo.com",       # hyphen-prefixed
        "phishing at brandx-demo.com.attacker.net",      # further label
        "notbrandx-demo.com is a different site",        # letter-prefixed
    ]
    # Assert on the domain LABEL, not on `matched` membership. Membership is
    # CONFOUNDED here: the same prose carries the 'brandx' token, which leaks
    # (13 owners < thresh 14) and adds every brandx repo including this one via
    # the token pass — so a membership assertion passes for the reject cases no
    # matter what the domain pass does. The label is the domain pass's own
    # signature and is the only uncontaminated signal. (My first draft of this
    # test asserted membership and failed for exactly that reason.)
    for prose in accept:
        labels, matched = M.detect_product_surfaces(
            prose, _LEAKY_FLEET, domain_repos=_LEAKY_DOMAINS)
        assert "brandx-demo.com" in labels, f"should match: {prose!r}"
        assert matched[0] == "brandx-www-site", (
            f"domain hit must take slot 0: {prose!r}")
    for prose in reject:
        labels, matched = M.detect_product_surfaces(
            prose, _LEAKY_FLEET, domain_repos=_LEAKY_DOMAINS)
        assert "brandx-demo.com" not in labels, f"must NOT match: {prose!r}"
        assert matched[:1] != ["brandx-www-site"], (
            f"must not be promoted into the budget window: {prose!r}")


def test_detect_domain_absent_from_prose_adds_nothing():
    labels, matched = M.detect_product_surfaces(
        "General loop maintenance", _LEAKY_FLEET,
        domain_repos=_LEAKY_DOMAINS)
    assert labels == set() and matched == []


def test_detect_domain_map_default_is_backward_compatible():
    # Every pre-existing caller passes no map; behaviour must be identical.
    assert (M.detect_product_surfaces("acme-widget-service work", _REPO_NAMES)
            == M.detect_product_surfaces("acme-widget-service work",
                                         _REPO_NAMES, domain_repos=None))


# extract_domain_repos (pure) -------------------------------------------------

_CATALOG = """
| repo | remote | notes |
|---|---|---|
| `brandx-www-site` | `org/brandx-www-site` | Marketing site (https://www.brandx-demo.com/) |
| `org/orbital-checkout-app` | **`user/orbital-checkout-app`** | Console — serves **`widgetco-demo.com`** |
| `brandx-svc-00` | `org/brandx-svc-00` | Batch worker, no public surface |
Config lives in `env.json` and `tasks.json`; CI is `ci.yml` for `brandx-svc-00`.
Prose near ambiguous-demo.com naming `brandx-www-site`, `orbital-checkout-app` and `brandx-portal-app` at once asserts no ownership.
"""
# NOTE: that last line must stay ONE physical line. Line-scoping is the
# predicate, so a wrapped fixture puts the domain and the repos on different
# lines and the row is skipped before the cap is ever consulted — the test
# then passes with the cap REMOVED. Caught by mutation (cap -> 99 survived).


def test_extract_domain_repos_pairs_line_scoped():
    got = M.extract_domain_repos(_CATALOG, set(_LEAKY_FLEET))
    assert got.get("brandx-demo.com") == {"brandx-www-site"}
    assert got.get("widgetco-demo.com") == {"orbital-checkout-app"}


def test_extract_domain_repos_strips_www_to_one_key():
    got = M.extract_domain_repos(_CATALOG, set(_LEAKY_FLEET))
    assert "www.brandx-demo.com" not in got


def test_extract_domain_repos_reads_qualified_org_repo_form():
    # The orbital row writes ONLY `org/repo` and `user/repo`, never the bare
    # name — the bare-name regex alone would return nothing for that row.
    got = M.extract_domain_repos(_CATALOG, set(_LEAKY_FLEET))
    assert "widgetco-demo.com" in got


def test_extract_domain_repos_ignores_filenames_as_domains():
    # env.json / tasks.json / ci.yml are dotted tokens that are not domains.
    # A catalog is full of filenames, so an open TLD pattern would pair junk
    # keys against real repos on almost every line.
    got = M.extract_domain_repos(_CATALOG, set(_LEAKY_FLEET))
    assert not any(k.endswith((".json", ".yml", ".py")) for k in got)


def test_extract_domain_repos_skips_ambiguous_multi_repo_line():
    # The prose line names 3 known repos beside a domain — over the cap, so it
    # asserts no ownership and must contribute nothing.
    got = M.extract_domain_repos(_CATALOG, set(_LEAKY_FLEET))
    assert "ambiguous-demo.com" not in got


def test_extract_domain_repos_only_maps_known_repos():
    # A name the caller does not recognise must never enter the map — so the
    # orbital row, whose only repo is now unknown, pairs nothing at all.
    got = M.extract_domain_repos(_CATALOG, {"brandx-www-site"})
    assert got["brandx-demo.com"] == {"brandx-www-site"}
    assert "widgetco-demo.com" not in got


def test_extract_domain_repos_rejects_non_repo_shaped_names():
    # THE LIVE FALSE POSITIVE (measured 2026-08-09): the loose catalog regex
    # backticks ordinary words, and the estate catalog paired
    # 'github.com -> main' and 'schema.org -> Organization'. Neither is on
    # disk, so neither could waste a network slot — but each would add a
    # LABEL, and a label alone triggers the whole product scan. 'github.com'
    # is in ordinary goal prose constantly.
    catalog = ("| `main` | deployed from https://github.com/org/x |\n"
               "| `Organization` | the schema.org block names it |\n")
    assert M.extract_domain_repos(catalog, {"main", "Organization"}) == {}
    assert M._is_repo_shaped("Ayo-Public-Web-App")
    assert M._is_repo_shaped("SendErrorAlert")      # 3 CamelCase humps
    assert M._is_repo_shaped("zds_inference")       # underscore
    assert not M._is_repo_shaped("main")
    assert not M._is_repo_shaped("Organization")    # 1 hump
    assert not M._is_repo_shaped("")


def test_extract_domain_repos_empty_inputs():
    assert M.extract_domain_repos("", {"brandx-public-site"}) == {}
    assert M.extract_domain_repos(_CATALOG, set()) == {}
    assert M.extract_domain_repos(None, None) == {}


# classify_product_overlap (pure) ---------------------------------------------

def test_product_overlap_force_includes_own_goal_id():
    # THE  shape: the shipped product commit carries the CLAIMING
    # goal's id. classify_overlap would exclude it as own-WIP; the product
    # variant force-includes it as the strongest already-shipped evidence.
    commits = [_commit("p1", "feat(g-115-2156): add start-session endpoint",
                       ["src/gateway/session.py"])]
    hits = M.classify_product_overlap(set(), set(), commits, "g-115-2156")
    assert len(hits) == 1
    assert hits[0]["matched_goal_id"] is True
    assert hits[0]["committed_goal_id"] == "g-115-2156"


def test_product_overlap_recurring_skips_force_include():
    # A recurring goal legitimately ships product commits under its own id
    # every cycle — the force-include would self-flag forever.
    commits = [_commit("p1", "feat(g-115-151): nightly probe artifacts", [])]
    hits = M.classify_product_overlap(set(), set(), commits, "g-115-151",
                                      goal_recurring=True)
    assert hits == []


def test_product_overlap_keyword_match_annotated_not_goal_id():
    commits = [_commit("p2",
                       "feat(g-777-01): start-session endpoint for gateway",
                       [])]
    kw = M.extract_keywords("Ship the start-session endpoint gateway change")
    hits = M.classify_product_overlap(set(), kw, commits, "g-115-2428")
    assert len(hits) == 1
    assert hits[0]["matched_goal_id"] is False
    assert len(hits[0]["matched_keywords"]) >= 2


# _parse_write_path_conf ------------------------------------------------------

def test_parse_write_path_conf_quoted_semicolon(tmp_path):
    conf = tmp_path / "local-paths.conf"
    conf.write_text(
        "# comment\n"
        "WORLD_PATH=/somewhere/world\n"
        'AGENT_WRITE_PATH="/opt/work/acme;/opt/work/beta"\n'
        "AGENT_WRITE_PATH_EXTRA=/must/not/be/parsed\n",  # exact-key only
        encoding="utf-8")
    assert M._parse_write_path_conf(conf) == [
        "/opt/work/acme", "/opt/work/beta"]


def test_detect_short_names_never_trigger():
    # Convention-file name extraction is regex-loose; a short backticked
    # token (table header word) must not become a full-name scan trigger.
    labels, matched = M.detect_product_surfaces(
        "Tier work on the loop", ["Tier", "acme-widget-service"])
    assert labels == set() and matched == []


def test_parse_write_path_conf_absent_is_silent():
    # Verification check: "absent conf is silent" — no exception, empty list.
    assert M._parse_write_path_conf("/nonexistent/nowhere.conf") == []


# _agent_write_repos ----------------------------------------------------------

def test_agent_write_repos_direct_and_container(tmp_path):
    # A direct repo entry contributes itself; a CONTAINER entry (plain dir of
    # independent clones) contributes its depth-1 git children only.
    direct = tmp_path / "solo-repo"
    (direct / ".git").mkdir(parents=True)
    container = tmp_path / "work"
    (container / "repo-a" / ".git").mkdir(parents=True)
    (container / "repo-b" / ".git").mkdir(parents=True)
    (container / "not-a-repo").mkdir()
    repos = M._agent_write_repos([str(direct), str(container),
                                  str(tmp_path / "missing")])
    assert [(n, p.name) for n, p in repos] == [
        ("solo-repo", "solo-repo"), ("repo-a", "repo-a"),
        ("repo-b", "repo-b")]


# _scan_product_repos (impure, end-to-end) ------------------------------------

def _isolate_scan(monkeypatch, entries):
    """Pin the scan's environment seams: synthetic conf entries, no domain
    convention read, no gh network."""
    monkeypatch.setenv("MIND_AGENT", "test-agent")
    monkeypatch.setattr(M, "_parse_write_path_conf", lambda _p: entries)
    monkeypatch.setattr(M, "_convention_repo_names", lambda: set())
    monkeypatch.setattr(M, "_gh_available", lambda **kw: False)


def test_scan_synthetic_product_repo_reported(tmp_path, monkeypatch):
    # Verification outcome: a matching commit in a synthetic product repo is
    # reported as an overlapping-commit entry WITH repo attribution.
    container = tmp_path / "work"
    repo = container / "widget-service"
    repo.mkdir(parents=True)
    _run_git(["-c", "init.defaultBranch=main", "init", "-q"], repo)
    (repo / "session.py").write_text("x = 1\n", encoding="utf-8")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m",
              "feat(g-115-2156): add start-session endpoint to widget gateway"],
             repo)
    _isolate_scan(monkeypatch, [str(container)])
    text = "Ship the start-session endpoint in widget-service gateway"
    result = M._scan_product_repos(
        "g-115-2156", text, set(), M.extract_keywords(text), 48.0, 2)
    # labels may also carry the distinctive token ('widget' bounded-matches
    # inside 'widget-service') — membership is the contract, not exact set.
    assert "widget-service" in result["surfaces"]
    assert result["repos_scanned"] == ["widget-service"]
    assert len(result["commits"]) == 1
    hit = result["commits"][0]
    assert hit["repo"] == "widget-service"          # repo attribution
    assert hit["matched_goal_id"] is True
    assert hit["committed_goal_id"] == "g-115-2156"


def test_scan_absent_conf_is_silent(monkeypatch):
    # Verification check: absent conf (parser returns []) -> empty verdict,
    # no exception, nothing scanned — even when the prose names a surface.
    _isolate_scan(monkeypatch, [])
    result = M._scan_product_repos(
        "g-115-2428", "work on the operator API", set(), set(), 48.0, 2)
    # "behind" joined the shape in . It is present on BOTH the empty
    # verdict and the populated result deliberately: a key that appears only on
    # the populated branch makes every consumer write a .get() fallback, and a
    # fallback is where a real signal goes to hide (communication-clarity r5).
    assert result == {"surfaces": [], "repos_scanned": [], "commits": [],
                      "branch_hits": [], "pr_hits": [], "behind": []}


def _behind_fixture(tmp_path, n_behind):
    """Build a real upstream + clone, advance upstream by n_behind commits,
    fetch. Returns the clone path. Real git, not a mock: the whole point of
    _git_behind_count is which refs git actually resolves."""
    up = tmp_path / "upstream"
    up.mkdir()
    _run_git(["-c", "init.defaultBranch=main", "init", "-q"], up)
    _run_git(["config", "user.email", "t@t"], up)
    _run_git(["config", "user.name", "t"], up)
    (up / "f.txt").write_text("v1\n")
    _run_git(["add", "."], up)
    _run_git(["commit", "-qm", "c1"], up)

    clone = tmp_path / "clone"
    _run_git(["clone", "-q", str(up), str(clone)], tmp_path)
    for i in range(2, 2 + n_behind):
        (up / "f.txt").write_text(f"v{i}\n")
        _run_git(["commit", "-qam", f"c{i}"], up)
    _run_git(["fetch", "-q"], clone)
    return clone


def test_behind_count_reports_exact_lag(tmp_path):
    #  check 1: a repo deliberately left behind origin reports the
    # correct count. The number is the whole deliverable — 78 encodings
    # already say "fetch first"; none of them shows how far behind you are.
    assert M._git_behind_count(_behind_fixture(tmp_path, 3)) == 3


def test_behind_count_zero_when_current(tmp_path):
    #  check 2: quiet on the common case. 0 is falsy, which is what
    # the call site branches on, so a current repo emits no advisory line.
    assert M._git_behind_count(_behind_fixture(tmp_path, 0)) == 0


def test_behind_count_fails_open_without_origin(tmp_path):
    # No remote at all -> None, not an exception. This probe is advisory and
    # must never block a claim, so every unresolvable case degrades to silence
    # rather than raising (same posture as _git_fetch_remote above it).
    solo = tmp_path / "solo"
    solo.mkdir()
    _run_git(["-c", "init.defaultBranch=main", "init", "-q"], solo)
    _run_git(["config", "user.email", "t@t"], solo)
    _run_git(["config", "user.name", "t"], solo)
    (solo / "a").write_text("x")
    _run_git(["add", "."], solo)
    _run_git(["commit", "-qm", "x"], solo)
    assert M._git_behind_count(solo) is None
    assert M._git_behind_count(tmp_path / "does-not-exist") is None


def test_since_arg_is_gone_no_probe_may_use_git_log_since():
    # . `_since_arg` was DELETED, not merely unused: `git log
    # --since` is a traversal cutoff, so any bound built from it can DROP
    # commits a %ct filter keeps, and leaving a convenient helper in the
    # module invites the cutoff straight back in. This pins the removal AND
    # the absence of `--since` from every argv in the module, which is the
    # property that actually matters -- a future re-introduction fails here.
    assert not hasattr(M, "_since_arg")

    # Test the ARGVS, not the file text. A substring scan also matches prose
    # in docstrings and comments (it did, on the first draft of this test),
    # which makes the guard fire on documentation and teaches the next reader
    # to weaken it. Walk the AST instead and inspect only list literals that
    # are actually a `git log ...` command line.
    import ast
    tree = ast.parse(Path(M.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        literals = [e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if "git" not in literals or "log" not in literals:
            continue
        for elt in node.elts:
            # a bare literal, e.g. "--since=2 hours ago"
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                if "--since" in elt.value:
                    offenders.append(elt.value)
            # an f-string, e.g. f"--since={n} minutes ago"
            elif isinstance(elt, ast.JoinedStr):
                lit = "".join(v.value for v in elt.values
                              if isinstance(v, ast.Constant)
                              and isinstance(v.value, str))
                if "--since" in lit:
                    offenders.append(lit)
    assert not offenders, (
        f"a git-log --since bound came back into a probe argv: {offenders}. "
        "--since is a TRAVERSAL CUTOFF -- use --max-count plus "
        "_ct_cutoff/_within_cutoff instead (guard-4539)")


def test_ct_cutoff_is_epoch_seconds_back_from_now():
    now = 1_800_000_000
    assert M._ct_cutoff(2.0, now_epoch=now) == now - 7200
    assert M._ct_cutoff(48.0, now_epoch=now) == now - 172800
    assert M._ct_cutoff(0.5, now_epoch=now) == now - 1800


def test_within_cutoff_keeps_recent_drops_old_and_keeps_unparseable():
    cut = 1_000_000
    rows = [
        {"hash": "a", "ct": "1000001"},          # newer than cutoff
        {"hash": "b", "ct": "1000000"},          # exactly at cutoff -> kept
        {"hash": "c", "ct": "999999"},           # older -> dropped
        {"hash": "d", "ct": None},               # unreadable -> KEPT
        {"hash": "e", "ct": "not-a-number"},     # unreadable -> KEPT
    ]
    kept = {c["hash"] for c in M._within_cutoff(rows, cut)}
    assert kept == {"a", "b", "d", "e"}, kept
    # The fail-open direction is the whole point: this filter replaced a bound
    # whose failure mode was DROPPING real work, and an unreadable timestamp
    # must not silently reproduce it. Over-reporting costs a second look;
    # under-reporting authorizes duplicate work.


def test_git_log_commits_float_window_excludes_old(tmp_path, monkeypatch):
    # Behavioral pin for the float--since bug: an OLD commit (3h ago) must
    # NOT surface under since_hours=2.0. Pre-fix, the float string disabled
    # the filter entirely and the old commit leaked into the race scan.
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["-c", "init.defaultBranch=main", "init", "-q"], repo)
    import datetime as _dt
    old = (_dt.datetime.now().astimezone()
           - _dt.timedelta(hours=3)).isoformat(timespec="seconds")
    (repo / "old.txt").write_text("old\n", encoding="utf-8")
    _run_git(["add", "."], repo)
    _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "feat(g-999-03): old work"],
            cwd=str(repo), check=True, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            env={**os.environ, "GIT_COMMITTER_DATE": old,
                 "GIT_AUTHOR_DATE": old})
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", "feat(g-999-04): fresh work"], repo)
    monkeypatch.setattr(M, "PROJECT_ROOT", repo)
    subjects = [c["subject"] for c in M._git_log_commits(2.0)]
    assert any("fresh work" in s for s in subjects), subjects
    assert not any("old work" in s for s in subjects), subjects


import os  # noqa: E402  (used by the float-window fixture above)


def test_old_dated_tip_does_not_hide_recent_commits(tmp_path, monkeypatch):
    """THE regression pin for  / guard-4539.

    `git log --since` is a TRAVERSAL CUTOFF, not a filter: git walks from the
    tip and stops at the first commit older than the cutoff, so ONE old-dated
    commit at the tip hides every recent commit behind it. Commit dates go
    non-monotonic in ordinary operation (rebase, cherry-pick, --amend --date,
    a merged long-lived branch, peer clock skew), so this is not exotic.

    This probe's empty result AUTHORIZES pickup, so a false empty does not
    merely under-report -- it green-lights duplicate work.

    The fixture is the measured one: N recent commits, then one old-dated
    commit at the TIP. Pre-fix this returned ZERO.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["-c", "init.defaultBranch=main", "init", "-q"], repo)
    for i in range(1, 8):
        (repo / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", f"feat(g-888-0{i}): recent work {i}"],
                 repo)
    # ...then an OLD-DATED commit at the TIP. This is the cutoff trigger.
    old = "2020-01-01T00:00:00"
    (repo / "tip.txt").write_text("tip\n", encoding="utf-8")
    _run_git(["add", "."], repo)
    _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "chore: old-dated tip"],
            cwd=str(repo), check=True, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            env={**os.environ, "GIT_COMMITTER_DATE": old,
                 "GIT_AUTHOR_DATE": old})

    monkeypatch.setattr(M, "PROJECT_ROOT", repo)
    monkeypatch.setattr(M, "_git_fetch_remote", lambda *a, **k: None)
    subjects = [c["subject"] for c in M._git_log_commits(2.0)]

    # POSITIVE CONTROL: prove the old-dated tip really does defeat `--since`
    # in THIS fixture, so a green assertion below cannot be vacuous.
    since_out = _sp.run(
        ["git", "log", "--all", "--since=120 minutes ago", "--format=%H"],
        cwd=str(repo), check=True, stdout=_sp.PIPE, stderr=_sp.DEVNULL,
    ).stdout.decode()
    assert since_out.strip() == "", (
        "fixture no longer reproduces the traversal cutoff -- `--since` "
        "returned commits, so this test can no longer detect a regression")

    assert len(subjects) == 7, subjects
    for i in range(1, 8):
        assert any(f"recent work {i}" in s for s in subjects), (i, subjects)
    # The old-dated tip itself is correctly OUTSIDE the 2h window.
    assert not any("old-dated tip" in s for s in subjects), subjects


def test_scan_skips_when_no_surface_named(tmp_path, monkeypatch):
    # Gating: repos exist, but the goal prose names no product surface ->
    # zero git work (repos_scanned stays empty). Ordinary framework goals
    # pay ~nothing.
    container = tmp_path / "work"
    (container / "widget-service" / ".git").mkdir(parents=True)
    _isolate_scan(monkeypatch, [str(container)])
    result = M._scan_product_repos(
        "g-115-2428", "Fix the learning-gate stub condition in the loop",
        set(), set(), 48.0, 2)
    assert result["surfaces"] == [] and result["repos_scanned"] == []


# ── : claim/release pairing + live-state corroboration ─────────────
#
# The defect these pin: the probe had no notion of a release, so once ANY claim
# post existed race_risk was true forever, for every agent, permanently. An
# abandoned claim became a permanent lien on the goal. Measured on 
# (2026-07-27): zeta claimed 03:59/04:00, released explicitly 06:18, and alpha +
# bravo still yielded four times over ~3h because the probe could not see it.

def _bmsg(mid, author, ts, mtype, text, tags=None):
    return {"id": mid, "author": author, "timestamp": ts, "type": mtype,
            "text": text, "tags": tags or []}


def test_released_ids_text_prefix_forms():
    # The real  shape is a type=status post whose text OPENS with
    # "RELEASING <id>" — there is no type=release on the board.
    assert M._released_ids(
        "status", "RELEASING g-335-292 explicitly — @alpha, do not wait", []
    ) == {"g-335-292"}
    for verb in ("release:", "Released", "Unclaiming", "Abandoning"):
        assert M._released_ids("status", f"{verb} g-115-99 because ...", []) \
            == {"g-115-99"}, verb


def test_released_ids_first_class_release_type():
    # board.py VALID_MESSAGE_TYPES carries a real "release" type. A type=release
    # post tagging this goal is unambiguous — no text prefix needed. Symmetric
    # to how _claimed_ids reads type=claim + goal-id tags.
    assert M._released_ids(
        "release", "handing this back", ["g-115-99", "zeta"]) == {"g-115-99"}


def test_released_ids_tag_leg():
    assert M._released_ids(
        "status", "no prefix here", ["release", "g-115-99"]) == {"g-115-99"}


def test_released_ids_body_mention_is_not_a_release():
    # Precision-first, mirroring _claimed_ids: the regex anchors at start of
    # text, so a mid-body mention never counts.
    assert M._released_ids(
        "status", "I am not releasing g-115-99 yet, still working", []) == set()


def test_released_ids_claim_typed_post_never_releases():
    # An incoherent post that both claims and releases stays a CLAIM — the
    # conservative direction (race_risk stays set).
    assert M._released_ids(
        "claim", "Releasing g-115-99", ["release", "g-115-99"]) == set()


def test_classify_board_mentions_emits_release_kind():
    hits = M.classify_board_mentions("g-115-99", "alpha", [
        _bmsg("m1", "zeta", "2026-07-27T04:00:00", "status",
             "RELEASING g-115-99 explicitly", []),
    ])
    assert [h["kind"] for h in hits] == ["release"]


def test_release_supersedes_earlier_claim_by_same_author():
    hits = M.classify_board_mentions("g-115-99", "alpha", [
        _bmsg("m1", "zeta", "2026-07-27T03:59:00", "claim",
             "Claiming g-115-99: doing the thing", ["g-115-99", "zeta"]),
        _bmsg("m2", "zeta", "2026-07-27T06:18:00", "status",
             "RELEASING g-115-99 explicitly — @alpha take it", []),
    ])
    live, superseded = M.supersede_released_claims(hits)
    assert [h["id"] for h in superseded] == ["m1"]
    assert not any(h["kind"] == "claim" for h in live)


def test_release_does_not_clear_a_different_authors_claim():
    # A release by zeta must never clear bravo's live claim.
    hits = M.classify_board_mentions("g-115-99", "alpha", [
        _bmsg("m1", "bravo", "2026-07-27T03:59:00", "claim",
             "Claiming g-115-99", ["g-115-99", "bravo"]),
        _bmsg("m2", "zeta", "2026-07-27T06:18:00", "status",
             "RELEASING g-115-99", []),
    ])
    live, superseded = M.supersede_released_claims(hits)
    assert superseded == []
    assert any(h["kind"] == "claim" and h["author"] == "bravo" for h in live)


def test_claim_after_release_is_not_superseded():
    # Re-claim AFTER releasing: latest event wins, so the claim stands.
    hits = M.classify_board_mentions("g-115-99", "alpha", [
        _bmsg("m1", "zeta", "2026-07-27T03:00:00", "status",
             "RELEASING g-115-99", []),
        _bmsg("m2", "zeta", "2026-07-27T06:00:00", "claim",
             "Claiming g-115-99 again", ["g-115-99", "zeta"]),
    ])
    live, superseded = M.supersede_released_claims(hits)
    assert superseded == []
    assert any(h["kind"] == "claim" for h in live)


def test_unparseable_timestamp_keeps_the_claim():
    # Fail-safe: a false yield is cheaper than a missed race.
    hits = [
        {"id": "m1", "author": "zeta", "timestamp": "not-a-date",
         "kind": "claim", "text": "x"},
        {"id": "m2", "author": "zeta", "timestamp": "2026-07-27T06:18:00",
         "kind": "release", "text": "y"},
    ]
    live, superseded = M.supersede_released_claims(hits)
    assert superseded == []
    assert any(h["kind"] == "claim" for h in live)


def test_corroborate_downgrades_claim_when_partner_is_demonstrably_elsewhere():
    hits = [{"id": "m1", "author": "zeta", "timestamp": "2026-07-27T04:00:00",
             "kind": "claim", "text": "x"}]
    live, stale = M.corroborate_claims("g-115-99", hits, {
        "zeta": {"in_flight_goal_id": "g-115-3342", "current_focus": None,
                 "last_active_minutes": 2.0}})
    assert live == [] and len(stale) == 1
    assert "g-115-3342" in stale[0]["stale_reason"]


def test_corroborate_never_downgrades_on_current_focus_alone():
    """A live claim must SURVIVE when in_flight is null, using the LITERAL
    production current_focus shape.

    Regression for the fresh-eyes finding on 2026-07-27. current_focus is
    "asp-NNN: <goal title>" in production — measured across all 5 live agents —
    so it can never contain a goal-id. The original implementation tested
    `gid not in focus` and therefore downgraded EVERY claim by every live agent
    (5/5 measured), clearing live claims and re-opening the double-pickup race.
    in_flight is null for most of a goal's life (cleared at Phase 5 verify), so
    that branch was the common path, not an edge case.

    The bug survived 15 new unit tests because the original version of THIS test
    fed a synthetic focus that DID contain a goal-id. That is guard-920 /
    rb-5346: replicate the literal production shape, not the contract-ideal one.
    """
    hits = [{"id": "m1", "author": "zeta", "timestamp": "2026-07-27T04:00:00",
             "kind": "claim", "text": "x"}]
    live, stale = M.corroborate_claims("g-335-292", hits, {
        "zeta": {"in_flight_goal_id": None,
                 # verbatim production shape — title, never an id
                 "current_focus": ("asp-115: Investigate: failed Operator "
                                   "deploy leaves operator.ayoai unreachable"),
                 "last_active_minutes": 13.0}})
    assert stale == [], "live claim wrongly downgraded on current_focus alone"
    assert len(live) == 1


def test_corroborate_absence_is_never_clearance():
    # guard-1560 / check-team-state-before-silent rule 5: a null in_flight AND
    # empty focus is ABSENCE, not evidence. The claim must stand.
    hits = [{"id": "m1", "author": "zeta", "timestamp": "2026-07-27T04:00:00",
             "kind": "claim", "text": "x"}]
    live, stale = M.corroborate_claims("g-115-99", hits, {
        "zeta": {"in_flight_goal_id": None, "current_focus": None,
                 "last_active_minutes": 2.0}})
    assert stale == [] and len(live) == 1


def test_corroborate_stale_row_never_downgrades():
    # A partner whose heartbeat is stale may simply have a broken writer (the
    # 2026-07-14 incident: two LIVE agents read 59h/66h stale). Not evidence.
    hits = [{"id": "m1", "author": "zeta", "timestamp": "2026-07-27T04:00:00",
             "kind": "claim", "text": "x"}]
    live, stale = M.corroborate_claims("g-115-99", hits, {
        "zeta": {"in_flight_goal_id": "g-115-3342", "current_focus": None,
                 "last_active_minutes": 4000.0}})
    assert stale == [] and len(live) == 1


def test_corroborate_partner_in_flight_on_THIS_goal_stays_live():
    # The  protection: a partner genuinely in_flight on this goal is
    # a real race and must survive corroboration untouched.
    hits = [{"id": "m1", "author": "zeta", "timestamp": "2026-07-27T04:00:00",
             "kind": "claim", "text": "x"}]
    live, stale = M.corroborate_claims("g-115-99", hits, {
        "zeta": {"in_flight_goal_id": "g-115-99", "current_focus": "g-115-99",
                 "last_active_minutes": 1.0}})
    assert stale == [] and len(live) == 1


def test_corroborate_passes_through_non_claim_kinds():
    hits = [{"id": "m1", "author": "zeta", "timestamp": "2026-07-27T04:00:00",
             "kind": "complete", "text": "x"}]
    live, stale = M.corroborate_claims("g-115-99", hits, {
        "zeta": {"in_flight_goal_id": "g-115-3342", "current_focus": None,
                 "last_active_minutes": 1.0}})
    assert stale == [] and len(live) == 1


def test_g335292_replay_release_clears_race_risk():
    """Goal check 1 — replay the real  board history.

    zeta claimed twice at 03:59/04:00 and RELEASED explicitly at 06:18; bravo
    posted a deadlock-break routing it to alpha at 06:16. Before the fix this
    returned race_risk=true and cost ~3h across two agents.
    """
    msgs = [
        _bmsg("msg-20260727-035951-zeta-5796", "zeta", "2026-07-27T03:59:51",
             "claim", "Claiming g-335-292: MindWorlds PutItem probe",
             ["g-335-292", "zeta"]),
        _bmsg("msg-20260727-040009-zeta-5797", "zeta", "2026-07-27T04:00:09",
             "claim", "Claiming g-335-292 (re-announce)",
             ["g-335-292", "zeta"]),
        _bmsg("msg-20260727-061831-zeta-5847", "zeta", "2026-07-27T06:18:31",
             "status",
             "RELEASING g-335-292 explicitly — @alpha, do not wait an "
             "iteration. I CANNOT execute it from here.", []),
    ]
    hits = M.classify_board_mentions("g-335-292", "alpha", msgs)
    live, superseded = M.supersede_released_claims(hits)
    live, stale = M.corroborate_claims("g-335-292", live, {
        "zeta": {"in_flight_goal_id": "g-115-3342", "current_focus": None,
                 "last_active_minutes": 13.0}})
    assert len(superseded) == 2, superseded
    race_risk = any(h["kind"] in ("claim", "complete") for h in live)
    assert race_risk is False


def test_live_claim_still_yields_no_g115_1876_regression():
    """Goal check 2 — the protection must NOT regress.

    A partner in_flight on THIS goal with no release still sets race_risk.
    """
    msgs = [_bmsg("m1", "bravo", "2026-07-27T07:00:00", "claim",
                 "Claiming g-115-99: live work", ["g-115-99", "bravo"])]
    hits = M.classify_board_mentions("g-115-99", "alpha", msgs)
    live, superseded = M.supersede_released_claims(hits)
    live, stale = M.corroborate_claims("g-115-99", live, {
        "bravo": {"in_flight_goal_id": "g-115-99",
                  "current_focus": "g-115-99 live work",
                  "last_active_minutes": 1.0}})
    assert superseded == [] and stale == []
    assert any(h["kind"] == "claim" for h in live)


def test_release_alone_does_not_set_race_risk():
    # A release is evidence the goal is FREE — counting it would invert the
    # signal it was added to carry.
    hits = M.classify_board_mentions("g-115-99", "alpha", [
        _bmsg("m1", "zeta", "2026-07-27T06:18:00", "status",
             "RELEASING g-115-99", [])])
    live, _ = M.supersede_released_claims(hits)
    live, _ = M.corroborate_claims("g-115-99", live, {})
    assert live and not any(
        h["kind"] in ("claim", "complete") for h in live)


# ── telemetry () ───────────────────────────────────────────────────
# END-TO-END: spawns the probe and reads the firing back off disk. A source-grep
# assertion would prove the call is WRITTEN, never that it RUNS (guard-1451).
#
# COVERAGE LIMIT, stated rather than glossed: only the `noop` decision is
# reachable hermetically. `pass` and `block` require a goal that EXISTS in the
# live queue (the probe reads its title/paths from there — there is no
# text-injection flag), so producing them here would make the test depend on
# live queue contents and on a git/board scan.
#
# Out-of-band confirmation, so this reads as a test limit and not an unverified
# branch: ALL THREE decisions were confirmed live on 2026-07-28.
#   noop  — the hermetic test below (nonexistent goal, nothing to compare).
#   block — probe run against  itself, race_risk=true → one `block`
#           record with 18 affected_paths / 12 overlapping_commits.
#   pass  — a real loop pickup of  at 21:31:41, race_risk=false with
#           3 affected_paths / 8 keywords / 58 product_repos_scanned.
# So the mapping is fully exercised in production; what remains untestable HERE
# is only reproducing pass/block hermetically, which is a fixture limitation and
# not an unverified code path.

def test_telemetry_firing_lands_on_disk(tmp_path):
    import json
    import os
    import subprocess

    meta = tmp_path / "meta"
    meta.mkdir()
    env = dict(os.environ)
    env.update({"MIND_META": str(meta),
                "GATE_LOG_ALLOW_PYTEST": "1",   # lift _gate_log's pytest no-op
                "STORAGE_BACKEND": "local"})    # never S3 (guard-955 / rb-2983)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--goal-id", "g-999-99",
         "--source", "world", "--since-hours", "1", "--output", "json"],
        capture_output=True, text=True, timeout=300, env=env)
    assert r.returncode == 0, r.stderr

    log = meta / "gate-firings.jsonl"
    assert log.exists(), "the probe ran but emitted no firing record"
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1, rows
    assert rows[0]["gate_id"] == "goal-pickup-coordination"
    # A goal id that matches nothing yields no paths and no keywords: the probe
    # was invoked but had nothing to compare, which is exactly `noop`.
    assert rows[0]["decision"] == "noop"
    assert rows[0]["extra"]["race_risk"] is False
    assert rows[0]["caller"] == "goal-pickup-coordination-check.main"
    # The guarded goal's id must be in `extra`, which _gate_log persists
    # verbatim — NOT only in `payload`, which it reduces to `payload_hash`
    # and discards (). Without this the firing cannot be joined to
    # the pickup it guarded, and pickup-coverage collapses to a ratio of two
    # independently-counted populations, biased up by the duplicate-firing
    # rate (measured 65.7% raw vs 48.2% de-duplicated).
    assert rows[0]["extra"]["goal_id"] == "g-999-99"
    # payload_hash is still emitted; it is a fingerprint, not a join key —
    # two different pickups whose probe inputs coincide share one hash.
    assert "payload_hash" in rows[0]


def test_telemetry_gate_is_registered_in_gates_yaml():
    import yaml
    root = Path(__file__).resolve().parent.parent.parent
    reg = yaml.safe_load((root / "config/gates.yaml").read_text(encoding="utf-8"))
    row = next((g for g in reg["gates"] if g["id"] == "goal-pickup-coordination"), None)
    assert row is not None, "goal-pickup-coordination missing from core/config/gates.yaml"
    assert row["instrumented"] is True
    assert row["script"] == "core/scripts/goal-pickup-coordination-check.py"


# --- Done-but-pending disposition ( / gap-100) ------------------
# These pin the PURE half of the extension. The git/gh probes it feeds on
# (_own_goal_commits, _sha_on_origin_main, _live_pr_state) are impure and
# fail-open by contract, exactly like the product-repo probes above.


def test_external_surface_is_all_not_any_so_a_mixed_surface_stays_visible():
    m = _import()
    # All-external -> the git rail is blind and the caller must be told so.
    assert m._surface_is_external(["world/scripts/a.sh", "meta/b.yaml"]) is True
    # ONE in-repo path makes git informative again. If this were `any`, a goal
    # touching both trees would report CANNOT-SEE while git could have answered.
    assert m._surface_is_external(["world/scripts/a.sh", "core/scripts/b.py"]) is False
    assert m._surface_is_external(["core/scripts/b.py"]) is False
    # No paths at all is NOT blindness — it is the ordinary "prose named no
    # path" case, which must fall through to GENUINELY-PENDING.
    assert m._surface_is_external([]) is False
    assert m._surface_is_external(None) is False


def test_merged_own_commit_yields_done_and_merged_and_obliges_a_coverage_read():
    m = _import()
    verdict, obliges = m.classify_shipped(
        [{"hash": "a" * 40, "subject": "fix(g-1-1): x", "on_origin_main": True}],
        [], [], False)
    assert verdict == "DONE-AND-MERGED"
    # The obligation is the whole point: gap-100's PARTIALLY-DONE is a READING
    # of verify-step coverage, so the classifier must hand that judgement back
    # rather than fabricate a verdict it cannot measure.
    assert "PARTIALLY-DONE" in obliges


def test_an_undeterminable_sha_is_never_read_as_merged_or_as_unshipped():
    m = _import()
    # on_origin_main=None means the object is absent from this clone (rc=128).
    # It must not satisfy DONE-AND-MERGED...
    verdict, _ = m.classify_shipped(
        [{"hash": "b" * 40, "subject": "fix(g-1-1): x", "on_origin_main": None}],
        [], [], False)
    assert verdict != "DONE-AND-MERGED"
    # ...and it must not collapse to GENUINELY-PENDING either: a commit naming
    # this goal exists, which is evidence of work whatever its merge state.
    assert verdict == "WORK-EXISTS-UNMERGED"


def test_open_pr_outranks_unmerged_commits_and_demands_the_live_reread():
    m = _import()
    verdict, obliges = m.classify_shipped(
        [{"hash": "c" * 40, "subject": "fix(g-1-1): x", "on_origin_main": False}],
        [{"repo": "r", "number": 9, "state": "OPEN"}], [], False)
    assert verdict == "OPEN-PR-STALE"
    assert "live" in obliges.lower()


def test_a_merged_pr_does_not_trigger_the_open_pr_verdict():
    m = _import()
    # Only OPEN state counts. A MERGED PR with no local commit evidence is not
    # a stale-PR situation; treating every PR hit as OPEN would send the caller
    # to rebase something that already landed.
    verdict, _ = m.classify_shipped(
        [], [{"repo": "r", "number": 9, "state": "MERGED"}], [], False)
    assert verdict != "OPEN-PR-STALE"


def test_branch_hit_alone_is_work_not_nothing():
    m = _import()
    verdict, _ = m.classify_shipped([], [], [{"repo": "r", "branch": "fix/g-1-1"}], False)
    assert verdict == "WORK-EXISTS-UNMERGED"


def test_external_blindness_beats_a_clean_report_but_loses_to_evidence():
    m = _import()
    # Nothing found + blind rail -> say "cannot see", never "nothing shipped".
    assert m.classify_shipped([], [], [], True)[0] == "CANNOT-SEE"
    # Evidence outranks blindness: a merged commit is an ANSWER, and reporting
    # CANNOT-SEE over it would discard the strongest signal available.
    assert m.classify_shipped(
        [{"hash": "d" * 40, "subject": "s", "on_origin_main": True}],
        [], [], True)[0] == "DONE-AND-MERGED"


def test_all_rails_clean_and_visible_is_genuinely_pending():
    m = _import()
    verdict, obliges = m.classify_shipped([], [], [], False)
    assert verdict == "GENUINELY-PENDING"
    assert "normally" in obliges.lower()


def test_disposition_never_touches_race_risk():
    m = _import()
    # classify_shipped returns a 2-tuple of strings and nothing else — it
    # cannot flip race_risk even by accident. Fusing the two questions would
    # make every already-shipped goal read as a partner collision to the
    # digest's yield branch.
    out = m.classify_shipped(
        [{"hash": "e" * 40, "subject": "s", "on_origin_main": True}], [], [], False)
    assert isinstance(out, tuple) and len(out) == 2
    assert all(isinstance(x, str) for x in out)


def test_a_recurring_goals_prior_cycle_commits_do_not_read_as_done():
    m = _import()
    # The exact live false positive: , achievedCount 302,
    # lastAchievedAt 2026-08-09T09:23:30, three commits from 08-04/06/08. A
    # recurring goal's id is in a commit subject on EVERY cycle it has ever
    # run, so without the filter this verdict is wrong 100% of the time for
    # every recurring goal in the queue.
    prior = [{"hash": "a" * 40, "subject": "chore(g-115-105): x",
              "date": "2026-08-08T01:04:51+00:00", "on_origin_main": True}]
    assert m.classify_shipped(prior, [], [], False,
                              goal_recurring=True,
                              goal_last_achieved="2026-08-09T09:23:30"
                              )[0] == "GENUINELY-PENDING"
    # Same commits, NON-recurring goal -> the filter must not fire.
    assert m.classify_shipped(prior, [], [], False)[0] == "DONE-AND-MERGED"


def test_a_recurring_goal_shipped_this_cycle_is_still_seen():
    m = _import()
    # The filter must not blind the classifier to work done AFTER the last
    # achievement — otherwise a recurring goal could never report DONE.
    this_cycle = [{"hash": "b" * 40, "subject": "chore(g-115-105): x",
                   "date": "2026-08-09T11:00:00+00:00", "on_origin_main": True}]
    assert m.classify_shipped(this_cycle, [], [], False,
                              goal_recurring=True,
                              goal_last_achieved="2026-08-09T09:23:30"
                              )[0] == "DONE-AND-MERGED"


def test_unparseable_recurring_timestamp_falls_toward_doing_the_work():
    m = _import()
    # DELIBERATELY the opposite fail-safe direction from classify_board_mentions,
    # which KEEPS a hit on an unparseable timestamp. The two errors are not
    # symmetric: a false DONE-AND-MERGED ends the investigation, a false
    # GENUINELY-PENDING costs one redundant look.
    bad = [{"hash": "c" * 40, "subject": "s", "date": "not-a-date",
            "on_origin_main": True}]
    assert m.classify_shipped(bad, [], [], False, goal_recurring=True,
                              goal_last_achieved="2026-08-09T09:23:30"
                              )[0] == "GENUINELY-PENDING"
    # A missing lastAchievedAt is equally unparseable -> same direction.
    assert m.classify_shipped(
        [{"hash": "d" * 40, "subject": "s", "date": "2026-08-09T11:00:00+00:00",
          "on_origin_main": True}], [], [], False,
        goal_recurring=True, goal_last_achieved=None)[0] == "GENUINELY-PENDING"


def test_pure_agent_store_churn_is_not_shipped_evidence():
    m = _import()
    # THE DOMINANT false positive: 141 of the 200 most recent goal-id-named
    # commits on this box (70%) touched nothing but agents/. The loop stamps
    # bookkeeping commits with the goal id exactly like real ones.
    churn = [{"hash": "a" * 40, "subject": "chore(g-350-148): x",
              "date": "2026-08-07T17:26:24+00:00", "on_origin_main": True,
              "files": ["agents/echo/changelog.jsonl",
                        "agents/echo/experience.jsonl",
                        "agents/echo/experience/exp-g-350-148.md"]}]
    assert m.classify_shipped(churn, [], [], False)[0] == "GENUINELY-PENDING"


def test_one_non_store_file_makes_a_commit_count():
    m = _import()
    # all(), not any(): a commit that touched real code AND incidental store
    # churn is still shipped work. Dropping it would trade one false positive
    # for a false negative in the destructive direction.
    mixed = [{"hash": "b" * 40, "subject": "fix(g-1-1): x",
              "date": "2026-08-09T11:00:00+00:00", "on_origin_main": True,
              "files": ["agents/alpha/journal.jsonl", "core/scripts/thing.py"]}]
    assert m.classify_shipped(mixed, [], [], False)[0] == "DONE-AND-MERGED"


def test_a_commit_with_no_file_list_is_not_assumed_to_be_churn():
    m = _import()
    # Absence of evidence about the files is not evidence they were all
    # bookkeeping — fail toward counting it, which is the direction that keeps
    # a reader looking rather than closing.
    unknown = [{"hash": "c" * 40, "subject": "fix(g-1-1): x",
                "date": "2026-08-09T11:00:00+00:00", "on_origin_main": True}]
    assert m.classify_shipped(unknown, [], [], False)[0] == "DONE-AND-MERGED"


def test_store_churn_is_dropped_before_the_recurring_filter_not_after():
    m = _import()
    # Order matters: a recurring goal whose only fresh commit is churn must
    # still land on GENUINELY-PENDING rather than surviving on recency.
    fresh_churn = [{"hash": "d" * 40, "subject": "chore(g-115-105): x",
                    "date": "2026-08-09T11:00:00+00:00", "on_origin_main": True,
                    "files": ["agents/alpha/journal.jsonl"]}]
    assert m.classify_shipped(fresh_churn, [], [], False, goal_recurring=True,
                              goal_last_achieved="2026-08-09T09:23:30"
                              )[0] == "GENUINELY-PENDING"


def test_commit_goal_id_reads_the_subject_scope_not_a_body_citation():
    m = _import()
    # The pure core of the --grep-matches-the-whole-message fix. `git log
    # --grep <goal-id>` matches the BODY too, so a commit whose narrative
    # merely cites a goal arrives at the probe as that goal's work. Measured
    # live:  (a due recurring goal) read WORK-EXISTS-UNMERGED purely
    # because a DIFFERENT goal's commit named it in its verification section.
    assert m.commit_goal_id("fix(g-115-105): stall analyzer was blind") == "g-115-105"
    # A subject scoped to another goal must NOT resolve to the cited one — this
    # is what the probe now filters on.
    assert m.commit_goal_id("fix(g-115-5270): shipped_verdict was wrong") != "g-115-105"
    # MEASURED, and it corrected my first draft of this test: with no
    # `type(scope):` form, commit_goal_id falls back to a bare goal-id search
    # in the SUBJECT, so this DOES resolve. That is fine for the probe's
    # purpose — the filter's job is to exclude BODY citations, not to demand a
    # conventional-commit scope — but the assertion has to say what is true.
    assert m.commit_goal_id("chore: routine churn touching g-115-105") == "g-115-105"
    # A subject with no goal id anywhere makes no authorship claim at all.
    assert m.commit_goal_id("chore: unrelated routine churn") != "g-115-105"


# ── private agent-queue namespacing of the two git lanes () ─────────
# THE COLLISION, measured on this box 2026-08-15: 29 of 73 distinct agent-queue
# goal ids exist in more than one agent's queue, and `` exists in all
# five. Git history is keyed on that id alone, so both git lanes read five
# agents' commits as one goal's. The two lanes fail in OPPOSITE directions and
# the pins below assert both — a pin on only the ledger half would leave the
# silent half live, which is the more dangerous one (a false DONE is loud and
# gets argued with; a missing race warning is never seen).

def _foreign(files, subject="chore(g-001-01): Reflect and journal"):
    return {"hash": "f" * 40, "subject": subject, "files": files}


def test_foreign_agent_work_needs_proof_not_absence_of_mine():
    m = _import()
    # PROVABLE: another agent's dir, mine absent.
    assert m.commit_is_foreign_agent_work(["agents/echo/journal.jsonl"], "alpha")
    # MINE PRESENT anywhere -> never foreign, even alongside a partner's dir
    # (a merge or a cross-agent sweep still contains my work).
    assert not m.commit_is_foreign_agent_work(
        ["agents/echo/journal.jsonl", "agents/alpha/journal.jsonl"], "alpha")
    # UNATTRIBUTABLE: framework-only. 4.8% of the live g-001-* population
    # (61 of 1268 commits/90d). The filing goal's prescribed remedy — REQUIRE
    # agents/<me>/ — drops exactly these and turns a false DONE into a false
    # PENDING. Conservatism is the design (guard-2499).
    assert not m.commit_is_foreign_agent_work(["core/scripts/goal-selector.py"], "alpha")
    # Unknown self, or no file list: never claim proof.
    assert not m.commit_is_foreign_agent_work(["agents/echo/x"], "")
    assert not m.commit_is_foreign_agent_work([], "alpha")


def test_direction_one_a_partners_same_id_commit_is_not_my_shipped_work():
    """LEDGER half. classify_shipped is fed by _own_goal_commits, so the drop has
    to happen there — proven here through the classifier the verdict reads."""
    m = _import()
    partner = [{"hash": "a" * 40, "subject": "chore(g-001-01): Reflect and journal",
                "date": "2026-08-15T10:00:00+00:00", "on_origin_main": True,
                "files": ["agents/echo/journal.jsonl",
                          ".claude/rules/run-full-suite-after-deep-code.md"]}]
    # Unfiltered, this is the live false DONE: real partner commits, real verdict.
    assert m.classify_shipped(partner, [], [], False)[0] == "DONE-AND-MERGED"
    # Filtered as a private agent-queue id, the ledger is empty and the goal is
    # correctly still to do.
    kept = [c for c in partner
            if not m.commit_is_foreign_agent_work(c["files"], "alpha")]
    assert kept == []
    assert m.classify_shipped(kept, [], [], False)[0] == "GENUINELY-PENDING"


def test_direction_two_a_partners_same_id_commit_still_surfaces_as_a_race():
    """RACE half — the silent one. The same commit the ledger must EXCLUDE must
    still be VISIBLE to the overlap lane when it touches a path this goal named.
    Measured live: bravo's g-001-06 commit touching core/scripts/pipeline-archive.sh
    was invisible before this fix."""
    m = _import()
    commits = [_foreign(["agents/bravo/journal.jsonl",
                         "core/scripts/pipeline-archive.sh"],
                        subject="chore(g-001-06): Pipeline and experience archival")]
    paths, kw = {"core/scripts/pipeline-archive.sh"}, {"pipeline", "archival"}

    # Before: the same-id exemption swallowed it whole.
    assert m.classify_overlap(paths, kw, commits, "g-001-06") == (False, [])
    # After: re-admitted, flagged, and race_risk raised.
    race, ov = m.classify_overlap(paths, kw, commits, "g-001-06",
                                  agent_queue_private=True, me="alpha")
    assert race is True and len(ov) == 1
    assert ov[0]["matched_paths"] == ["core/scripts/pipeline-archive.sh"]
    assert ov[0]["foreign_agent_work"] is True


def test_the_keyword_route_stays_shut_for_a_readmitted_foreign_commit():
    """The measurement that overruled the filing goal's plain re-admission. Over
    alpha's whole agent queue / 168h: re-admitting everything surfaces 60
    keyword-only hits and 1 path hit. All 60 are the same shape — five agents run
    an identically TITLED per-agent recurring goal, so the title matches itself
    every cycle forever and race_risk on g-001-01 would pin True fleet-wide."""
    m = _import()
    sibling = [_foreign(["agents/echo/journal.jsonl"])]      # no shared path
    paths, kw = set(), {"reflect", "journal"}                # identical title
    race, ov = m.classify_overlap(paths, kw, sibling, "g-001-01",
                                  agent_queue_private=True, me="alpha")
    assert (race, ov) == (False, []), "identical sibling titles carry no signal"


def test_a_world_goal_is_untouched_by_all_of_it():
    """is_private_agent_queue is the only switch. A world id names ONE record, so
    both lanes must behave exactly as before — and a cross-agent goal resolves
    against an agent queue but names a genuinely contendable record, so it is
    NOT private either."""
    m = _import()
    assert m.is_private_agent_queue("world") is False
    assert m.is_private_agent_queue("agent", "bravo") is False
    assert m.is_private_agent_queue("agent") is True

    commits = [_foreign(["agents/echo/journal.jsonl", "core/scripts/x.py"])]
    # Defaults off: byte-identical classification to the pre-fix behaviour.
    assert m.classify_overlap({"core/scripts/x.py"}, set(), commits,
                              "g-001-01") == (False, [])
    # And a foreign commit is only ever dropped from the ledger under the flag.
    assert m.commit_is_foreign_agent_work(commits[0]["files"], "alpha") is True
