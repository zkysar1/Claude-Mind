"""Tests for goal-pickup-coordination-check.py ( / US-03).

Advisory same-surface-race probe at goal-pickup. These tests pin the pure
classifier contract — path/keyword extraction, commit-goal-id parse, path
overlap, and the overlap classifier including the canonical 2026-05-13 race
(partner already shipped) and the own-goal exclusion. The git/daemon/team-state
reads in main() are impure and exercised only at runtime, not here.

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
    assert result == {"surfaces": [], "repos_scanned": [], "commits": [],
                      "branch_hits": [], "pr_hits": []}


def test_since_arg_integer_minutes():
    # git approxidate silently mishandles FLOAT hour strings (git 2.43,
    # observed live 2026-07-17): "2.0 hours ago" parses as NO filter
    # (full-history scan -> 6-day-old commits flagged as 2h races) while
    # "48.0 hours ago" parses as an EMPTY window (0 commits). Integer
    # minutes are unambiguous and preserve fractional hours.
    assert M._since_arg(2.0) == "--since=120 minutes ago"
    assert M._since_arg(48.0) == "--since=2880 minutes ago"
    assert M._since_arg(0.5) == "--since=30 minutes ago"
    assert M._since_arg(0.001) == "--since=1 minutes ago"  # floor of 1


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
