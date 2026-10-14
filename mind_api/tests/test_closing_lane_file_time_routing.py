""" — file-time routing check for closed aspirations (guard-7084).

THE DEFECT. A lane that carries a non-empty ``closing_set`` takes strays for
the whole life of the freeze, and only the 24h sweep finds them — by which
point some are already COMPLETED in-lane and drew the lane's strategic_focus
boost, the dilution the closing protocol's GOVERNANCE clause forbids.
Measured on the record (g-353-161 description): asp-326 took 32 non-set goals
in 33h after activation, 9 of them completed in the lane before the sweep ran.
guard-7084 prescribes the file-time test, but it was honor-system only —
nothing in core/scripts, mind_api or .claude read closing_set at filing time.

THE FIX PINNED HERE. The daemon's add_goal endpoint (the LIVE path — the
wrapper is daemon-only, .claude/rules/no-python-cli-fallback.md) reads the
target aspiration's ``closing_set`` at filing time. A filing into such a lane
is closing work only if it IS a closing-set member or it carries guard-7084
rule 2's escape line; otherwise it is REROUTED to the lane's own
``follow_on_aspiration`` (a GENERIC field the world fills — core names no
lane). The reroute is loud (200 + a warnings[] entry the wrapper re-emits to
stderr naming both lanes) or, when the follow-on lane does not exist, a
400 ``closing_lane_no_follow_on`` refusal that names the lane, the intended
destination and the reason — a caller cannot mistake either for a successful
filing in the lane it named (verification outcomes 1 + 2).

The moves exclusion is structural and pinned here too:
aspirations-move-goals.py re-homes existing goals without routing through
add_goal, so a move can never be refused by the gate.

Outcome 3 (the first post-landing g-353-127 census finds 0 post-freeze
non-set filings) is DEPLOY-coupled — it is measured against the live fleet
after the reducer ships this to origin/main and the world's closing lanes
carry ``follow_on_aspiration`` — and is NOT tested here; a test asserting it
would assert about a world state this box does not own.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_SRC.parent))

from src.endpoints import aspirations_write  # noqa: E402


# ---------------------------------------------------------------------------
# Unit level: the pure routing predicate
# ---------------------------------------------------------------------------

def _lane(closing_set=(), follow_on=None):
    asp = {"id": "asp-001", "closing_set": list(closing_set)}
    if follow_on is not None:
        asp["follow_on_aspiration"] = follow_on
    return asp


def _goal(**kw):
    g = {"title": kw.pop("title", "Idea: a stray filing"),
         "description": kw.pop("description", "an idea that belongs in the follow-on lane")}
    g.update(kw)
    return g


ESC = "WHICH CLOSING-SET OUTCOME THIS UNBLOCKS: g-326-282 outcome 3"


def test_rerouted_when_not_a_member_and_no_escape():
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=["g-326-01"], follow_on="asp-374"),
        _goal()) == "asp-374"


def test_stays_when_explicit_id_is_a_closing_set_member():
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=["g-326-01"], follow_on="asp-374"),
        _goal(id="g-326-01", title="Apply: finish the set member")) is None


def test_stays_when_the_rule_2_escape_line_is_present():
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=["g-326-01"], follow_on="asp-374"),
        _goal(description=f"Fix: unblocks the lane. {ESC}")) is None


def test_escape_line_in_title_counts_too():
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=["g-326-01"], follow_on="asp-374"),
        _goal(title=f"Unblock: {ESC}")) is None


def test_escape_line_with_a_suffixed_goal_id_counts():
    # _GOAL_ID_RE admits g-NNN-NNN-a (hyphen + ONE lowercase letter, per the
    # (-[a-z])? branch); a narrower retype of the id grammar in the escape
    # regex would reroute this.
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=["g-326-01"], follow_on="asp-374"),
        _goal(description="WHICH CLOSING-SET OUTCOME THIS UNBLOCKS: "
                          "g-326-282-a outcome 2")) is None


def test_escape_line_with_an_illegal_goal_id_does_not_count():
    # a (no hyphen before the suffix) is NOT a legal goal id per
    # _GOAL_ID_RE, so a line naming it is not the rule-2 line: the filing is
    # rerouted. Symmetric with the mangled-line test — both directions pin
    # that the escape matcher tracks the id grammar exactly, no wider.
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=["g-326-01"], follow_on="asp-374"),
        _goal(description="WHICH CLOSING-SET OUTCOME THIS UNBLOCKS: "
                          "g-326-282a outcome 2")) == "asp-374"


def test_mangled_escape_line_does_not_count():
    # No outcome number, wrong goal id shape, or the phrase without the id:
    # none of these is the rule-2 line, and a looser matcher would let
    # strays stay in the lane on a substring match.
    for bad in (
        "WHICH CLOSING-SET OUTCOME THIS UNBLOCKS: g-326-282",      # no outcome n
        "WHICH CLOSING-SET OUTCOME THIS UNBLOCKS: xyz outcome 3",  # bad id
        "closing-set outcome this unblocks: g-326-282 outcome 3",  # no colon, lowercased
    ):
        assert aspirations_write._closing_lane_routing(
            _lane(closing_set=["g-326-01"], follow_on="asp-374"),
            _goal(description=bad)), bad


def test_lane_without_closing_set_is_outside_the_gate():
    # guard-7084 rule 3: a boosted cost lane may not have a frozen set; the
    # gate must not route (or refuse) for it.
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=[], follow_on="asp-374"), _goal()) is None
    assert aspirations_write._closing_lane_routing(
        {"id": "asp-001", "follow_on_aspiration": "asp-374"}, _goal()) is None


def test_lane_with_closing_set_but_no_follow_on_is_a_noop():
    # No destination named => nothing to reroute to => the sweep-time check
    # keeps covering it. Refusing here would wedge every filing against the
    # lane with no remedy.
    assert aspirations_write._closing_lane_routing(
        _lane(closing_set=["g-326-01"]), _goal()) is None


def test_non_list_closing_set_is_a_noop():
    # A malformed field (string, dict) must read as "not a closing lane",
    # not as a crash and not as "lane with an unparseable set".
    for bad in ("g-326-01", {"id": "g-326-01"}):
        assert aspirations_write._closing_lane_routing(
            {"id": "asp-001", "closing_set": bad,
             "follow_on_aspiration": "asp-374"}, _goal()) is None


def test_escape_id_grammar_is_derived_from_the_goal_id_regex():
    # guard-742/547 class: the two id grammars must be the same object's
    # pattern, not two hand-typed copies that drift when one changes.
    pat = aspirations_write._CLOSING_SET_ESCAPE_RE.pattern
    goal_id_pat = aspirations_write._GOAL_ID_RE.pattern
    assert goal_id_pat[1:-1] in pat
    assert re.search(pat, f"x {ESC} x")
    assert not re.search(pat, "x WHICH CLOSING-SET OUTCOME THIS UNBLOCKS: "
                              "g-326-282 outcome 3y x")  # id must not run on


# ---------------------------------------------------------------------------
# Endpoint level: the daemon's add_goal behaviour (verification outcomes 1+2)
# ---------------------------------------------------------------------------

def _post(port: int, path: str, query: dict, body: bytes,
          *, agent: str = "alpha", headers: dict | None = None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _add_goal(port, goal, asp_id="asp-001"):
    return _post(port, "/v1/aspirations/add-goal",
                 {"asp_id": asp_id, "source": "world"},
                 json.dumps(goal).encode("utf-8"))


def _read_live(project_root):
    live = project_root / "world" / "aspirations.jsonl"
    return [json.loads(l) for l in live.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _seed_lanes(project_root, closing_lane="asp-900", follow_lane="asp-901",
                follow_missing=False):
    """Append the lane records the fixture world does not carry.

    ``follow_missing`` names the follow-on lane in the closing lane's
    ``follow_on_aspiration`` field but does NOT create that lane's record —
    the 400 branch. The field-absent branch is a different one (no-op;
    test_lane_without_follow_on_field_is_unaffected). The fixture store is
    below the daemon's 10 KB cache threshold (direct read on every
    request), so records appended after daemon start are visible to the
    next request — no invalidation needed."""
    live = project_root / "world" / "aspirations.jsonl"
    with live.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": closing_lane, "title": "Frozen closing lane", "status": "active",
            "priority": "HIGH", "archived": False,
            "closing_set": ["g-900-01", "g-900-02"],
            "follow_on_aspiration": follow_lane,
            "goals": [], "progress": {"completed_goals": 0, "total_goals": 0},
        }) + "\n")
        if not follow_missing:
            f.write(json.dumps({
                "id": follow_lane, "title": "Follow-on lane", "status": "active",
                "priority": "MEDIUM", "archived": False,
                "goals": [], "progress": {"completed_goals": 0, "total_goals": 0},
            }) + "\n")


def test_filing_into_closing_lane_is_rerouted_to_follow_on(running_daemon):
    pr, port = running_daemon
    _seed_lanes(pr)
    code, body = _add_goal(port, {
        "title": "Idea: a stray that belongs in the follow-on lane",
        "priority": "MEDIUM", "participants": ["agent"],
        "origin_signal": "idea:g-900-01",
    }, asp_id="asp-900")
    assert code == 200, body[:400]
    resp = json.loads(body)
    # The goal is NOT in the lane the caller named (outcome 1).
    assert resp["aspiration_id"] == "asp-901", body[:400]
    live = {a["id"]: a for a in _read_live(pr)}
    assert resp["goal_id"] in [g["id"] for g in live["asp-901"]["goals"]]
    assert resp["goal_id"] not in [g["id"] for g in live["asp-900"]["goals"]]
    # And the success is LOUD (outcome 2): a warning naming both lanes and
    # the reason, which aspirations-add-goal.sh re-emits to stderr.
    warn = "\n".join(resp.get("warnings") or [])
    assert "CLOSING-LANE REROUTE" in warn, warn
    assert "asp-900" in warn and "asp-901" in warn, warn
    assert "closing_set" in warn and "guard-7084" in warn, warn


def test_set_member_filing_stays_in_the_lane(running_daemon):
    pr, port = running_daemon
    _seed_lanes(pr)
    code, body = _add_goal(port, {
        # Explicit id that IS in the closing set: the filing is closing work
        # whatever its kind.
        "id": "g-900-02",
        "title": "Apply: finish the closing-set member",
        "priority": "HIGH", "participants": ["agent"],
        "origin_signal": "user_directive",
    }, asp_id="asp-900")
    assert code == 200, body[:400]
    resp = json.loads(body)
    assert resp["aspiration_id"] == "asp-900", body[:400]
    live = {a["id"]: a for a in _read_live(pr)}
    assert "g-900-02" in [g["id"] for g in live["asp-900"]["goals"]]
    assert "g-900-02" not in [g["id"] for g in live["asp-901"]["goals"]]
    assert "CLOSING-LANE REROUTE" not in "\n".join(resp.get("warnings") or [])


def test_rule_2_escape_filing_stays_in_the_lane(running_daemon):
    pr, port = running_daemon
    _seed_lanes(pr)
    code, body = _add_goal(port, {
        "title": "Fix: unblocks a closing-set outcome",
        "priority": "HIGH", "participants": ["agent"],
        "origin_signal": "unblock:g-900-01",
        "description": ("Fix the gate so the lane can close. "
                        "WHICH CLOSING-SET OUTCOME THIS UNBLOCKS: "
                        "g-900-01 outcome 2"),
    }, asp_id="asp-900")
    assert code == 200, body[:400]
    resp = json.loads(body)
    assert resp["aspiration_id"] == "asp-900", body[:400]
    live = {a["id"]: a for a in _read_live(pr)}
    assert resp["goal_id"] in [g["id"] for g in live["asp-900"]["goals"]]
    assert "CLOSING-LANE REROUTE" not in "\n".join(resp.get("warnings") or [])


def test_missing_follow_on_lane_refuses_loudly(running_daemon):
    pr, port = running_daemon
    _seed_lanes(pr, follow_missing=True)  # closing lane names asp-901, absent
    code, body = _add_goal(port, {
        "title": "Idea: a stray with nowhere named to land",
        "priority": "MEDIUM", "participants": ["agent"],
        "origin_signal": "idea:g-900-01",
    }, asp_id="asp-900")
    assert code == 400, body[:400]
    resp = json.loads(body)
    assert resp.get("error") == "closing_lane_no_follow_on", body[:400]
    detail = json.dumps(resp)
    assert "asp-900" in detail and "asp-901" in detail, detail
    # The filing is not lost into either lane: nothing was created.
    live = {a["id"]: a for a in _read_live(pr)}
    for lane in ("asp-900",):
        assert all(g["title"] != "Idea: a stray with nowhere named to land"
                   for g in live[lane]["goals"])


def test_lane_without_follow_on_field_is_unaffected(running_daemon):
    # The gate is keyed on BOTH generic fields: a closing_set without a
    # named follow_on is outside it (no destination => the sweep keeps
    # covering it), so a plain filing still lands in the named lane.
    pr, port = running_daemon
    live = pr / "world" / "aspirations.jsonl"
    with live.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": "asp-902", "title": "Closing set, no follow-on named",
            "status": "active", "priority": "HIGH", "archived": False,
            "closing_set": ["g-902-01"], "goals": [],
            "progress": {"completed_goals": 0, "total_goals": 0},
        }) + "\n")
    code, body = _add_goal(port, {
        "title": "Idea: filed into a set lane without a named follow-on",
        "priority": "MEDIUM", "participants": ["agent"],
        "origin_signal": "idea:g-902-01",
    }, asp_id="asp-902")
    assert code == 200, body[:400]
    resp = json.loads(body)
    assert resp["aspiration_id"] == "asp-902", body[:400]
    assert "CLOSING-LANE REROUTE" not in "\n".join(resp.get("warnings") or [])


def test_fixture_world_untouched_without_closing_fields(running_daemon):
    # Negative control for the whole file: an ordinary filing into the
    # fixture world's asp-001 (no closing_set) is byte-for-byte the pre-
    #  behaviour — no reroute, no new warning.
    pr, port = running_daemon
    code, body = _add_goal(port, {
        "title": "Idea: ordinary filing into an ordinary lane",
        "priority": "MEDIUM", "participants": ["agent"],
        "origin_signal": "idea:g-001-01",
    })
    assert code == 200, body[:400]
    resp = json.loads(body)
    assert resp["aspiration_id"] == "asp-001"
    assert "CLOSING-LANE REROUTE" not in "\n".join(resp.get("warnings") or [])


def test_moves_are_excluded_by_construction():
    # The goal's design constraint: "Moves made by aspirations-move-goals.py
    # are not filings and must not be refused." The exclusion is structural
    # — the move script re-homes existing goals in the store via a direct
    # locked read-modify-write (``_fileops.locked_modify_jsonl``) and never
    # touches the add-goal endpoint, so the gate cannot see a move. Pin the
    # shape at the CALL surface, not by substring (the script's docstring
    # legitimately mentions aspirations-add-goal.sh in narrative prose, and
    # a substring pin would trip on prose): no endpoint path, no daemon
    # add_goal call, and the direct-write primitive present.
    move = (pathlib.Path(__file__).resolve().parents[2]
            / "core" / "scripts" / "aspirations-move-goals.py")
    src = move.read_text(encoding="utf-8")
    assert "/v1/aspirations/add-goal" not in src, (
        "aspirations-move-goals.py now routes a move through the add-goal "
        "endpoint — a move would be refused by the closing-lane gate; the "
        "goal requires moves to stay excluded")
    assert re.search(r"\badd_goal\s*\(", src) is None, (
        "aspirations-move-goals.py now calls add_goal() directly — a move "
        "would be refused by the closing-lane gate")
    assert "locked_modify_jsonl" in src, (
        "the move script no longer writes the store directly — verify "
        "whether its write path still bypasses the add endpoint")
