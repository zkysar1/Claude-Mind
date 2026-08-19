"""Tests for audit-user-to-agent.py lane P: seeing, and re-deriving, 'user' goals.

Two defects are pinned here.

DEFECT 1 -- the auditor could not see its own population. `_find_user_only_goals`
required `participants == ["user"]` EXACTLY. Measured on the live world queue
2026-07-29: exactly ONE goal in the fleet matched (g-314-01), and that goal is a
deliberate park the audit then excluded -- so the live candidate set was ZERO and
the tool was structurally incapable of returning a result. The other 28
user-carrying goals were all ["agent", "user"] and invisible.

The cause is the uncomfortable kind: correct routing produced the blind spot.
capability-before-user.md tells the fleet to file [agent, user] whenever both
legs are real, so the creation-time gate working as designed generated exactly
the population the audit-time tool could not see. The creation-time advisory
(gates/user_leg_scope.py) has always tested `"user" in participants`; only this
half was narrower.

DEFECT 2 -- nothing asked whether the USER was still needed. The capability gate
answers "should the agent be involved?", which is a different question: the agent
being capable says nothing about whether the human leg is discharged. That is
decidable only when the leg was DECLARED, which is what `user_leg_scope` is for
-- the creation-time advisory says so verbatim ("Standing-grant matching will
fall back to prose recognition"). The drop lane performs that match as an exact
join against the Standing User Grants table.

The join is deliberately CONSERVATIVE and this suite pins that too. Grant scope
cells qualify at length, and the qualifications name scopes the grant does NOT
convey -- grant-008's body says "PROVIDED the commit is verified" and "grant-001
already covers the mechanical push". A whole-cell token scan reads both as
grants of `commit` and `push` (measured: it does). Cutting at the first period
keeps the declarative head only. Under-matching leaves a goal routed to the user
exactly as it is today; over-matching would recommend removing the human.

Pattern: importlib + sys.path (the script name has hyphens, so it cannot be a
plain `import`) -- same shape as test_audit_deferred_defers_stale_structured.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "audit-user-to-agent.py"


def _import():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("audit_user_to_agent", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_user_to_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()

# The real grant-001 row shape: declarative scope, then parenthetical qualifier.
GRANT_001 = ("| grant-001 | commit, push (all repos, all branches) | 2026-04-20 "
             "| user email | \"go ahead with commit and push\" | never |")

# The real grant-008 shape, reduced: the HEAD grants deployment, but the BODY
# mentions `commit` and `push` while describing OTHER grants' preconditions.
# This row is the false-positive generator the head-cut exists to defuse.
GRANT_008 = ("| grant-008 | **Roblox PROD deploy / promotion.** grant-001 already "
             "covers the mechanical push; the agent may promote PROVIDED the commit "
             "is verified. | 2026-07-25 | user directive | \"...\" | never |")


def _grants_md(rows, section="## Standing User Grants"):
    return "\n".join([
        "# Capability Routing",
        "",
        "## Agent-Provisionable Services (NEVER route to user)",
        "",
        # A decoy row in the WRONG section. capability-gate.py's own
        # _load_capability_routing has the mirror-image bug (it scans only the
        # agent-provisionable heading and never the grants table), which is how
        # a standing grant could invalidate a defer verbatim and still score
        # matches:0. This parser must not repeat that error inverted.
        "| grant-999 | commit, push | 2020-01-01 | decoy | \"decoy\" | never |",
        "",
        section,
        "",
        "| id | scope | granted | source | verbatim quote | expires |",
        "|----|-------|---------|--------|---------------|---------|",
        *rows,
        "",
        "**Matching rule.** ...",
    ]) + "\n"


def _write_grants(tmp_path, rows, section="## Standing User Grants"):
    conv = tmp_path / "conventions"
    conv.mkdir(parents=True, exist_ok=True)
    (conv / "capability-routing.md").write_text(_grants_md(rows, section),
                                                encoding="utf-8")
    return tmp_path


def _goal(gid, participants, status="pending", scope=None, origin=None, **kw):
    g = {"id": gid, "participants": participants, "status": status,
         "title": f"title for {gid}"}
    if scope is not None:
        g["user_leg_scope"] = scope
    if origin is not None:
        g["origin_signal"] = origin
    g.update(kw)
    return g


def _write_asp(path, goals, asp_status="active", asp_id="asp-001"):
    path.write_text(json.dumps({"id": asp_id, "status": asp_status,
                                "goals": goals}) + "\n", encoding="utf-8")
    return path


def _cand(goal, deliberate=False, shape="agent-user"):
    return {"source": "world", "aspiration_id": "asp-001", "goal": goal,
            "file_path": "x", "shape": shape, "deliberate": deliberate}


# --------------------------------------------------------------------------
# Defect 1: the finder must SEE ['agent', 'user'] goals.
# --------------------------------------------------------------------------

def test_finder_sees_agent_user_goals(tmp_path):
    """THE guard for defect 1. Under the old `== ["user"]` predicate this
    returns nothing, which is precisely the zero-candidate blindness measured
    on the live queue."""
    p = _write_asp(tmp_path / "aspirations.jsonl",
                   [_goal("g-1", ["agent", "user"])])
    found = MOD._find_user_participant_goals("world", p)
    assert [c["goal"]["id"] for c in found] == ["g-1"]
    assert found[0]["shape"] == "agent-user"


def test_finder_still_sees_user_only_and_tags_shape(tmp_path):
    """The promote lane's original population must keep flowing, distinctly
    tagged -- widening the finder must not merge the two lanes."""
    p = _write_asp(tmp_path / "aspirations.jsonl", [
        _goal("g-only", ["user"]),
        _goal("g-both", ["user", "agent"]),
    ])
    shapes = {c["goal"]["id"]: c["shape"]
              for c in MOD._find_user_participant_goals("world", p)}
    assert shapes == {"g-only": "user-only", "g-both": "agent-user"}


def test_finder_ignores_goals_without_user(tmp_path):
    p = _write_asp(tmp_path / "aspirations.jsonl", [
        _goal("g-agent", ["agent"]),
        _goal("g-none", []),
        _goal("g-bad", "not-a-list"),
        _goal("g-yes", ["agent", "user"]),
    ])
    assert [c["goal"]["id"]
            for c in MOD._find_user_participant_goals("world", p)] == ["g-yes"]


def test_finder_tags_deliberate_rather_than_dropping_it(tmp_path):
    """A silent skip is indistinguishable from a clean sweep -- the exact
    failure this audit lane exists to correct. Deliberate routings must appear
    in the result, flagged, so they are counted and explained."""
    p = _write_asp(tmp_path / "aspirations.jsonl", [
        _goal("g-dir", ["agent", "user"], origin="user_directive"),
        _goal("g-dashed", ["agent", "user"], origin="user-directed:2026-07-26-quiesce"),
        _goal("g-org", ["agent", "user"], origin="idea:something"),
    ])
    got = {c["goal"]["id"]: c["deliberate"]
           for c in MOD._find_user_participant_goals("world", p)}
    assert got == {"g-dir": True, "g-dashed": True, "g-org": False}


def test_finder_excludes_terminal_goals_and_aspirations(tmp_path):
    p = _write_asp(tmp_path / "aspirations.jsonl", [
        _goal("g-done", ["agent", "user"], status="completed"),
        _goal("g-live", ["agent", "user"], status="blocked"),
    ])
    assert [c["goal"]["id"]
            for c in MOD._find_user_participant_goals("world", p)] == ["g-live"]

    q = _write_asp(tmp_path / "archived.jsonl",
                   [_goal("g-x", ["agent", "user"])], asp_status="archived")
    assert MOD._find_user_participant_goals("world", q) == []


# --------------------------------------------------------------------------
# The conservative scope-head extraction.
# --------------------------------------------------------------------------

def test_scope_head_cuts_at_first_period():
    assert MOD._scope_head("**Deploy.** grant-001 covers the push") == "**Deploy"
    assert MOD._scope_head("a. b. c") == "a"
    # No period -> the whole cell is the head. grant-001's real shape.
    assert MOD._scope_head("commit, push (all repos)") == "commit, push (all repos)"


def test_scope_head_undermatches_on_an_abbreviation():
    """The cut is crude and this is the price: 'incl.' ends the head early, so
    a scope named after it is missed. Pinned deliberately -- a missed match
    leaves the goal routed to the user exactly as it is today, while the
    alternative (scanning further) is what over-matches grant-008. If this ever
    needs to change, change it knowing which direction the error moves."""
    assert MOD._scope_head("PR merge (repos incl. Foo) grants push") == \
        "PR merge (repos incl"


def test_head_cut_defuses_the_grant_008_false_positive(tmp_path):
    """THE guard for the join's soundness.

    grant-008 grants deployment. Its BODY names `commit` and `push` while
    describing other grants' preconditions. A whole-cell scan therefore reads
    it as granting commit+push -- verified below on the same row -- and any
    goal declaring user_leg_scope=commit would be told the user leg is
    discharged by a grant that says no such thing.
    """
    root = _write_grants(tmp_path, [GRANT_008])
    parsed = MOD._parse_standing_grants(root)
    assert parsed["by_scope"] == {}, (
        "grant-008's head grants deployment, not commit/push -- the head-cut "
        f"leaked: {parsed['by_scope']}")
    assert [g for g, _ in parsed["unkeyed"]] == ["grant-008"]

    # Positive control: the naive predicate really does over-match this row,
    # so the assertion above is guarding a live hazard, not a hypothetical.
    import re
    cell = GRANT_008.split("|")[2].strip()
    naive = [s for s in sorted(MOD.VALID_USER_LEG_SCOPES)
             if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", cell, re.I)]
    assert set(naive) == {"commit", "push"}, (
        "the false-positive fixture stopped reproducing the hazard; if grant-008's "
        f"shape changed, re-derive this guard rather than deleting it (got {naive})")


# --------------------------------------------------------------------------
# Grant-table parsing.
# --------------------------------------------------------------------------

def test_parses_granted_scopes_from_the_real_row_shape(tmp_path):
    parsed = MOD._parse_standing_grants(_write_grants(tmp_path, [GRANT_001]))
    assert parsed["by_scope"] == {"commit": ["grant-001"], "push": ["grant-001"]}
    assert parsed["unkeyed"] == []
    assert parsed["error"] is None


def test_only_the_standing_grants_section_is_scanned(tmp_path):
    """The decoy row sits under '## Agent-Provisionable Services'. Reading it
    would be the mirror image of capability-gate.py's own section bug."""
    parsed = MOD._parse_standing_grants(_write_grants(tmp_path, [GRANT_001]))
    assert all("grant-999" not in ids for ids in parsed["by_scope"].values())


def test_section_heading_terminates_at_the_next_h2(tmp_path):
    root = _write_grants(tmp_path, [GRANT_001])
    md = (root / "conventions" / "capability-routing.md")
    md.write_text(md.read_text(encoding="utf-8")
                  + "\n## Some Later Section\n\n"
                    "| grant-777 | data-provision | x | y | z | never |\n",
                  encoding="utf-8")
    parsed = MOD._parse_standing_grants(root)
    assert "data-provision" not in parsed["by_scope"]


def test_unkeyed_grants_are_reported_not_dropped(tmp_path):
    """A grant nothing can key to is a finding: it carries real permission that
    this audit can never apply. Reporting it names the row to reword."""
    row = ("| grant-042 | end-to-end product-flow test cold-start | 2026-07-15 "
           "| user | \"...\" | never |")
    parsed = MOD._parse_standing_grants(_write_grants(tmp_path, [GRANT_001, row]))
    assert parsed["by_scope"] == {"commit": ["grant-001"], "push": ["grant-001"]}
    assert [g for g, _ in parsed["unkeyed"]] == ["grant-042"]


def test_parse_fails_open_on_missing_convention(tmp_path):
    """No grants file must degrade to 'nothing is granted' -- the direction that
    leaves goals routed to the user -- and must SAY so rather than reporting an
    empty table as a clean read."""
    parsed = MOD._parse_standing_grants(tmp_path / "nope")
    assert parsed["by_scope"] == {}
    assert parsed["error"]
    assert MOD._parse_standing_grants(None)["error"] == "no WORLD_PATH"


# --------------------------------------------------------------------------
# Defect 2: the user-leg re-derivation verdicts.
# --------------------------------------------------------------------------

def test_grant_covered_recommends_dropping_user():
    grants = {"by_scope": {"push": ["grant-001"]}, "unkeyed": [], "error": None}
    v = MOD._assess_user_leg(_cand(_goal("g-1", ["agent", "user"], scope="push")),
                             grants)
    assert v["verdict"] == "grant-covered"
    assert v["grants"] == ["grant-001"]
    assert "grant-001" in v["reason"]


def test_declared_but_ungranted_scope_is_kept():
    """3 of the 4 declared legs on the live queue are credential-grant, which
    grant-009 explicitly still routes to the user. Keeping them is the correct
    answer, and a lane that recommended dropping them would be worse than one
    that saw nothing at all."""
    grants = {"by_scope": {"commit": ["grant-001"]}, "unkeyed": [], "error": None}
    v = MOD._assess_user_leg(
        _cand(_goal("g-1", ["agent", "user"], scope="credential-grant")), grants)
    assert v["verdict"] == "keep"


def test_undeclared_leg_is_named_as_the_finding():
    grants = {"by_scope": {}, "unkeyed": [], "error": None}
    v = MOD._assess_user_leg(_cand(_goal("g-1", ["agent", "user"])), grants)
    assert v["verdict"] == "undeclared"
    assert "aspirations-update-goal.sh g-1 user_leg_scope" in v["reason"]
    assert "commit" in v["valid_scopes"]


def test_scope_outside_the_canonical_set_is_undeclared():
    grants = {"by_scope": {"commit": ["grant-001"]}, "unkeyed": [], "error": None}
    v = MOD._assess_user_leg(
        _cand(_goal("g-1", ["agent", "user"], scope="vibes")), grants)
    assert v["verdict"] == "undeclared"


def test_deliberate_routing_outranks_a_covering_grant():
    """Even a granted scope must not override an explicit user directive --
    the reversal for a park is the user editing participants, not a sweep."""
    grants = {"by_scope": {"push": ["grant-001"]}, "unkeyed": [], "error": None}
    v = MOD._assess_user_leg(
        _cand(_goal("g-1", ["agent", "user"], scope="push",
                    origin="user_directive"), deliberate=True), grants)
    assert v["verdict"] == "deliberate"


def test_assess_never_mutates_the_goal():
    """The drop lane reports; it does not act. Removing the human is a one-way
    door inside the loop."""
    grants = {"by_scope": {"push": ["grant-001"]}, "unkeyed": [], "error": None}
    g = _goal("g-1", ["agent", "user"], scope="push")
    before = json.dumps(g, sort_keys=True)
    MOD._assess_user_leg(_cand(g), grants)
    assert json.dumps(g, sort_keys=True) == before


# --------------------------------------------------------------------------
# main() wiring: the report must reach a consumer intact.
# --------------------------------------------------------------------------

def _run_main(monkeypatch, tmp_path, goals, grants, argv):
    """Drive the real main() over fixtures. Hermetic: no live world, no
    capability-gate subprocess (the promote lane is empty in these cases, so
    the gate is never invoked)."""
    import contextlib
    import io
    monkeypatch.setattr(MOD, "_discover_agents", lambda: [])
    monkeypatch.setattr(MOD, "_world_dir_for", lambda a: tmp_path)
    monkeypatch.setattr(MOD, "_find_user_participant_goals",
                        lambda src, path: list(goals))
    monkeypatch.setattr(MOD, "_parse_standing_grants", lambda w: grants)
    monkeypatch.setenv("MIND_AGENT", "test-agent")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = MOD.main(argv)
    return rc, buf.getvalue()


def test_json_mode_stdout_is_parseable(monkeypatch, tmp_path):
    """A human-readable banner printed ahead of the JSON makes `json.load` fail
    on char 1 -- which is how a machine-readable mode ends up documented but
    unusable. aspirations-precheck Phase 0.5b.14 consumes this by field path."""
    goals = [_cand(_goal("g-1", ["agent", "user"], scope="push"))]
    grants = {"by_scope": {"push": ["grant-001"]}, "unkeyed": [], "error": None}
    rc, out = _run_main(monkeypatch, tmp_path, goals, grants, ["--output", "json"])
    assert rc == 0
    doc = json.loads(out)          # the assertion: nothing but JSON on stdout
    assert doc["drop_lane"]["counts"] == {"grant-covered": 1}
    assert doc["grants"]["by_scope"] == {"push": ["grant-001"]}
    assert doc["applied"] is False
    # Every field path named in the Phase 0.5b.14 pseudocode must resolve.
    assert isinstance(doc["promote_lane"]["reclassified"], list)
    assert isinstance(doc["drop_lane"]["verdicts"], list)
    assert isinstance(doc["grants"]["unkeyed"], list)


def test_empty_promote_lane_still_reports_the_drop_lane(monkeypatch, tmp_path):
    """THE guard for the early return. On the live queue the promote lane is
    legitimately empty while the drop lane has 28 goals; an early `return 0`
    there prints a clean line and strands the entire drop lane."""
    goals = [_cand(_goal("g-1", ["agent", "user"], scope="push"))]
    grants = {"by_scope": {"push": ["grant-001"]}, "unkeyed": [], "error": None}
    rc, out = _run_main(monkeypatch, tmp_path, goals, grants, [])
    assert rc == 0
    assert "g-1" in out, "drop lane vanished when the promote lane was empty"
    assert "DROP 'user'" in out


def test_unreadable_grants_are_announced_not_silently_empty(monkeypatch, tmp_path):
    """An unreadable grants table yields the same by_scope as a table granting
    nothing. The fail-safe direction is right, but reporting it as a clean read
    would let 'no drops found' mean 'the file was missing'."""
    goals = [_cand(_goal("g-1", ["agent", "user"], scope="push"))]
    grants = {"by_scope": {}, "unkeyed": [], "error": "not found: x"}
    _, out = _run_main(monkeypatch, tmp_path, goals, grants, [])
    assert "UNREADABLE" in out
    assert "NOT evidence" in out


# --------------------------------------------------------------------------
# Structural guards.
# --------------------------------------------------------------------------

def test_scope_vocabulary_is_imported_not_copied():
    """gates/user_leg_scope.py is the SSOT and also backs the creation-time
    advisory. A local copy would let audit time and creation time drift, which
    is how the two halves diverged in the first place."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "from gates.user_leg_scope import VALID_USER_LEG_SCOPES" in src
    assert "frozenset({" not in src, "vocabulary appears to be redefined locally"
    from gates.user_leg_scope import VALID_USER_LEG_SCOPES as SSOT
    assert MOD.VALID_USER_LEG_SCOPES is SSOT


def test_no_early_return_strands_the_drop_lane():
    """main() used to `return 0` when the promote lane was empty. On the live
    queue that lane IS empty, so the early return would print a clean line and
    silently skip the whole drop lane -- reproducing the invisibility this
    change exists to fix."""
    src = SCRIPT.read_text(encoding="utf-8")
    head = src.index("reclassified = []")
    assert "if not candidates:\n        return 0" not in src[:head], (
        "the empty-promote-lane early return is back; it strands the drop lane")


def test_apply_flag_cannot_reach_the_drop_lane():
    """--apply governs promotion only. If a future edit wires it to the drop
    path, dropping the human becomes automatic on a field populated for a
    minority of goals."""
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("def _assess_user_leg")
    end = src.index("def _summarize")
    body = src[start:end]
    # Forbid INVOCATION, not the substring. `aspirations-update-goal.sh` occurs
    # legitimately inside the advice text the assessor prints for the reader --
    # a naive "update-goal" token check flags that string and would push a
    # future author to weaken the guard to silence a false alarm.
    for forbidden in ("args.apply", "_update_goal_participants(",
                      "subprocess", "locked_append_jsonl", "_log_reclassification("):
        assert forbidden not in body, f"drop-lane assessor references {forbidden!r}"


# --------------------------------------------------------------------------
#  (a) -- a QUALIFIED grant must not recommend removing a human.
#
# The goal's diagnosis and `_scope_head`'s docstring are BOTH right, about
# opposite failure directions of one predicate: scanning the whole cell reads a
# refusal as a grant, and cutting at the period hides the carve-out where the
# residual human leg lives. A token-presence test on prose cannot express
# "granted EXCEPT when X" either way, so the fix is to stop claiming coverage
# when a qualifier exists -- NOT to move the cut, which would reintroduce the
# exact false-positive class the tests above already pin (guard-2260: the
# goal's REMEDY is a separate claim from its DIAGNOSIS).
# --------------------------------------------------------------------------

# Head keys to `credential-grant`; the carve-out past the period is the real
# human leg, and it is invisible to a head match.
GRANT_CARVEOUT = ("| grant-020 | credential-grant for minted service keys. "
                  "Excludes any credential requiring a human-owned account, which "
                  "still routes to the user. | 2026-08-01 | user email | \"...\" "
                  "| never |")
GRANT_CLEAN = ("| grant-021 | credential-grant | 2026-08-01 | user email "
               "| \"...\" | never |")


def test_qualified_grant_downgrades_to_review_not_drop(tmp_path):
    _write_grants(tmp_path, [GRANT_CARVEOUT])
    grants = MOD._parse_standing_grants(tmp_path)
    assert "grant-020" in grants["by_scope"].get("credential-grant", []), \
        "precondition: the head must still key, or this test proves nothing"
    v = MOD._assess_user_leg(
        _cand(_goal("g-1", ["agent", "user"], scope="credential-grant")), grants)
    assert v["verdict"] == "grant-qualified"
    assert "grant-020" in v["grant_qualifiers"]
    assert "human-owned account" in v["grant_qualifiers"]["grant-020"]


def test_unqualified_grant_still_recommends_the_drop(tmp_path):
    # The load-bearing negative: without this, disabling the lane entirely
    # would pass the test above and look like a fix.
    _write_grants(tmp_path, [GRANT_CLEAN])
    grants = MOD._parse_standing_grants(tmp_path)
    v = MOD._assess_user_leg(
        _cand(_goal("g-2", ["agent", "user"], scope="credential-grant")), grants)
    assert v["verdict"] == "grant-covered"
    assert v["grants"] == ["grant-021"]


def test_one_unqualified_grant_is_enough_and_it_alone_is_cited(tmp_path):
    _write_grants(tmp_path, [GRANT_CARVEOUT, GRANT_CLEAN])
    grants = MOD._parse_standing_grants(tmp_path)
    v = MOD._assess_user_leg(
        _cand(_goal("g-3", ["agent", "user"], scope="credential-grant")), grants)
    assert v["verdict"] == "grant-covered"
    # The qualified grant must not be cited as the authority for the drop.
    assert v["grants"] == ["grant-021"]


# --------------------------------------------------------------------------
#  (b) -- the drop lane must REMEMBER refusals.
# Measured downstream: 3 runs, 8 lifetime opportunities, 8 refusals, 0 true
# positives, run 3 re-presenting run 2's six goals byte-for-byte and cold.
# --------------------------------------------------------------------------

def _ledger(tmp_path, rows):
    p = tmp_path / MOD._REFUSAL_LEDGER
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_previously_refused_candidate_is_suppressed_with_its_reason(tmp_path):
    _ledger(tmp_path, [{"goal_id": "g-9", "run_date": "2026-08-05T01:02:03",
                        "verdict": "keep", "reason": "the key is human-owned"}])
    verdicts = [{"goal_id": "g-9", "verdict": "grant-covered", "reason": "orig"}]
    n = MOD._apply_refusals(verdicts, MOD._load_refusal_ledger(tmp_path))
    assert n == 1
    assert verdicts[0]["verdict"] == "previously-refused"
    assert verdicts[0]["prior_verdict"] == "grant-covered"
    assert verdicts[0]["prior"]["reason"] == "the key is human-owned"
    # The REASON must reach the reader: one refusal downstream was stored with a
    # factually false reason and would otherwise be re-derived wrongly forever.
    assert "the key is human-owned" in verdicts[0]["reason"]
    assert "2026-08-05" in verdicts[0]["reason"]


def test_a_qualified_review_is_suppressible_too(tmp_path):
    _ledger(tmp_path, [{"goal_id": "g-10", "run_date": "d", "verdict": "keep",
                        "reason": "r"}])
    verdicts = [{"goal_id": "g-10", "verdict": "grant-qualified", "reason": "orig"}]
    assert MOD._apply_refusals(verdicts, MOD._load_refusal_ledger(tmp_path)) == 1


def test_keep_and_undeclared_are_never_suppressed(tmp_path):
    # Suppressing `undeclared` would hide a backfill that is still owed, and
    # suppressing `keep` would suppress a verdict that already says "leave it".
    _ledger(tmp_path, [{"goal_id": "g-11", "run_date": "d", "verdict": "keep",
                        "reason": "r"},
                       {"goal_id": "g-12", "run_date": "d", "verdict": "keep",
                        "reason": "r"}])
    verdicts = [{"goal_id": "g-11", "verdict": "keep", "reason": "o"},
                {"goal_id": "g-12", "verdict": "undeclared", "reason": "o"}]
    assert MOD._apply_refusals(verdicts, MOD._load_refusal_ledger(tmp_path)) == 0
    assert [v["verdict"] for v in verdicts] == ["keep", "undeclared"]


def test_a_corrected_reason_supersedes_the_original(tmp_path):
    _ledger(tmp_path, [
        {"goal_id": "g-13", "run_date": "2026-08-01", "verdict": "keep",
         "reason": "WRONG -- misread the grant"},
        {"goal_id": "g-13", "run_date": "2026-08-09", "verdict": "keep",
         "reason": "corrected: the account is human-owned"},
    ])
    led = MOD._load_refusal_ledger(tmp_path)
    assert led["g-13"]["reason"].startswith("corrected:")


def test_absent_ledger_suppresses_nothing_and_does_not_raise(tmp_path):
    verdicts = [{"goal_id": "g-14", "verdict": "grant-covered", "reason": "o"}]
    assert MOD._apply_refusals(verdicts, MOD._load_refusal_ledger(tmp_path)) == 0
    assert verdicts[0]["verdict"] == "grant-covered"


def test_record_refusal_refuses_without_a_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MOD, "_world_dir_for", lambda a: tmp_path)
    monkeypatch.setattr(MOD, "_discover_agents", lambda: ["alpha"])
    rc = MOD.main(["--record-refusal", "g-15"])
    assert rc == 2
    assert not (tmp_path / MOD._REFUSAL_LEDGER).exists()


def test_record_refusal_writes_a_row_the_next_run_can_join_on(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "_world_dir_for", lambda a: tmp_path)
    monkeypatch.setattr(MOD, "_discover_agents", lambda: ["alpha"])
    rc = MOD.main(["--record-refusal", "g-16",
                   "--refusal-reason", "the account is human-owned"])
    assert rc == 0
    led = MOD._load_refusal_ledger(tmp_path)
    assert set(("run_date", "verdict", "reason")) <= set(led["g-16"])
    assert led["g-16"]["reason"] == "the account is human-owned"
    # Round-trip: the row it wrote must actually suppress on the next run.
    verdicts = [{"goal_id": "g-16", "verdict": "grant-covered", "reason": "o"}]
    assert MOD._apply_refusals(verdicts, led) == 1
