"""POST /v1/wm/{set,append,clear,prune,init,reset,clear-identity}, GET /v1/wm/ages.

Two layers:
  1. HTTP round-trip (running_daemon, conftest world): endpoints wired, the
     set/append/clear/prune/init/ages flows work end-to-end incl. the
     agent-header gate, structured-dict refusal, and knowledge_debt validation.
  2. Byte-compat (direct handler vs the REAL CLI wm.py): working-memory.yaml is
     byte-identical for a TOP-LEVEL key set (top-level writes skip slot_meta
     timestamping, so the file is fully deterministic). Both sides read the
     real core/config/memory-pipeline.yaml so _default_wm_data matches.

The CLI is redirected with MIND_AGENT_DIR (unit-test override) so it writes to
a temp agent dir, never the real one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
WM_PY = REPO_ROOT / "core" / "scripts" / "wm.py"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(port, path, query, body=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = (body or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _get(port, path, query=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, method="GET")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _read_wm(agent_dir: Path) -> dict:
    p = agent_dir / "session" / "working-memory.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# HTTP round-trip tests (conftest world)
# ---------------------------------------------------------------------------

def test_set_slot_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "active_strategy"},
                         '"breadth-first"')
    assert status == 200, body
    wm = _read_wm(agent_dir)
    assert wm["slots"]["active_strategy"] == "breadth-first"
    assert wm["slot_meta"]["active_strategy"]["updated_at"]


def test_set_top_level_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "last_goal_category"},
                         "framework")
    assert status == 200, body
    assert _read_wm(agent_dir)["last_goal_category"] == "framework"


def test_set_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/set", {"slot": "active_strategy"}, '"x"', agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_set_structured_dict_rejected(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/set", {"slot": "loop_state"}, '"a bare string"')
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "structured_dict_required"
    else:
        raise AssertionError("expected 400 for non-dict loop_state write")


def test_set_loop_state_dict_ok(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "loop_state"},
                         json.dumps({"signals": {"quiescence": False}}))
    assert status == 200, body
    assert _read_wm(agent_dir)["slots"]["loop_state"]["signals"]["quiescence"] is False


def test_append_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/append", {"slot": "known_blockers"},
                         json.dumps({"id": "blk-1", "reason": "waiting"}))
    assert status == 200, body
    arr = _read_wm(agent_dir)["slots"]["known_blockers"]
    assert arr[-1]["id"] == "blk-1"
    assert "_item_ts" in arr[-1]


@pytest.mark.parametrize("slot", ["known_blockers", "goals_completed_this_session"])
def test_append_reports_where_the_entry_physically_landed(running_daemon, slot):
    """: the append response must say WHICH of the two physical
    locations the entry went to — the YAML top level, or under `slots:`.

    The expectation is read from the FILE THE WRITE PRODUCED, not from a
    restated copy of TOP_LEVEL_KEYS. That matters more than the usual
    derive-don't-restate discipline (guard-1220) does here: TOP_LEVEL_KEYS is
    hand-mirrored in two files, so an expectation derived from EITHER copy would
    still agree with a `placement` computed from the same drifted set. Anchoring
    on where the bytes actually are is the one check both copies cannot fool.

    Why the field exists: reading the wrong level returns a clean, plausible 0
    that is byte-identical to "this slot is empty", so a caller who guesses wrong
    gets no error to notice — measured in both directions in one session.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    marker = f"placement-probe-{slot}"
    status, body = _post(port, "/v1/wm/append", {"slot": slot},
                         json.dumps({"id": marker}))
    assert status == 200, body
    placement = json.loads(body).get("placement")
    assert placement in ("top-level", "slots"), (
        f"append response carried no usable placement: {body!r}")

    wm = _read_wm(agent_dir)

    def _has(container):
        return (isinstance(container, list)
                and any(isinstance(e, dict) and e.get("id") == marker
                        for e in container))

    at_top = _has(wm.get(slot))
    under_slots = _has((wm.get("slots") or {}).get(slot))
    assert at_top != under_slots, (
        f"{slot} entry found at top-level={at_top} and under slots={under_slots} "
        f"— the physical location must be exactly one of the two for this "
        f"assertion to mean anything")
    expected = "top-level" if at_top else "slots"
    assert placement == expected, (
        f"{slot}: response said placement={placement!r} but the entry physically "
        f"landed at {expected!r} — a caller following the response would read the "
        f"wrong level and get a clean, wrong 0")


def test_append_placement_present_on_every_success(running_daemon):
    """Always present, for the same reason `evicted` is: a caller must be able to
    branch on it without a key-existence check. An absent key is indistinguishable
    from a pre-fix daemon, which is precisely the ambiguity this field removes."""
    _, port = running_daemon
    status, body = _post(port, "/v1/wm/append", {"slot": "known_blockers"},
                         json.dumps({"id": "blk-presence"}))
    assert status == 200, body
    assert "placement" in json.loads(body), (
        f"placement key missing from a successful append response: {body!r}")


def test_wrapper_extractor_parses_a_REAL_daemon_response(running_daemon):
    """The wrapper's own sed, run verbatim against the daemon's own bytes.

    core/scripts/tests/test_wm_append_placement_notice.py proves the wrapper
    prints the right line for a HAND-WRITTEN response. That leaves one
    restatement between the tests and the truth: nothing there checks that the
    daemon actually EMITS the shape the wrapper is able to read.

    What the extractor survives was MEASURED, not assumed — an earlier draft of
    this docstring claimed a switch to compact `separators=(",", ":")` would
    break it, and that is FALSE: `[[:space:]]*` matches zero spaces, so
    `{"ok":true,"placement":"slots"}` parses fine, as does an indented one.
    The regex is robust to whitespace. What it is NOT robust to is the VALUE
    TYPE: it requires a quoted string, so `"placement": ["slots"]` and
    `"placement": true` both extract nothing. `null` also extracts nothing, and
    there that is correct and deliberate — null means the resolver never ran, so
    the wrapper prints no placement line.

    So the drift this test catches is a producer-side change to the field's
    TYPE or NAME that leaves the consumer silently unable to read it — the
    g-115-6541 defect (`evicted` emitted by the daemon and displayed by nobody
    for its whole existence). It restates NOTHING: it lifts the extractor out of
    the production wrapper and runs it on the production response (guard-920 —
    replicate the literal production shape, not the contract-ideal one).
    """
    sys.path.insert(0, str(REPO_ROOT / "core" / "scripts" / "tests"))
    from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash")

    _, port = running_daemon
    status, body = _post(port, "/v1/wm/append", {"slot": "known_blockers"},
                         json.dumps({"id": "blk-extractor"}))
    assert status == 200, body
    assert "placement" in json.loads(body), f"producer emitted no placement: {body!r}"

    # The extractor, lifted verbatim from the wrapper rather than retyped.
    wrapper = (REPO_ROOT / "core" / "scripts" / "wm-append.sh").read_text(encoding="utf-8")
    sed_line = next(l for l in wrapper.splitlines()
                    if '"placement"' in l and l.strip().startswith("| sed"))
    sed_expr = sed_line.strip()[len("| sed -n "):].split(" | head")[0].strip("'")

    # Pass BOTH the payload and the sed script as ARGUMENTS. Interpolating the
    # sed script into the -c string takes three levels of quoting, and the first
    # attempt at it silently extracted "" — a harness failure BYTE-IDENTICAL to
    # the producer/consumer drift this test exists to detect. A harness whose
    # failure mode is indistinguishable from a real finding is worse than none
    # (guard-2298), hence the separate emptiness assert below.
    proc = subprocess.run(
        [BASH, "-c", 'printf %s "$1" | sed -n "$2" | head -1', "_", body, sed_expr],
        capture_output=True, text=True, timeout=30)
    got = proc.stdout.strip()
    assert got, (
        f"the extractor returned NOTHING — check the HARNESS before reading this "
        f"as drift: sed script {sed_expr!r}, stderr {proc.stderr!r}, "
        f"response {body!r}")
    assert got == json.loads(body)["placement"], (
        f"the wrapper's own sed extracted {got!r} from the daemon's real "
        f"response {body!r} — expected {json.loads(body)['placement']!r}. The "
        f"serialization shape and the extractor have drifted apart; the wrapper "
        f"will print nothing and no single-sided test will notice.")


def test_append_knowledge_debt_invalid_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/append", {"slot": "knowledge_debt"},
              json.dumps({"node_key": "no-such-node"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for unresolvable knowledge_debt node_key")


# . The two strings below are VERBATIM from
# agents/alpha/capture-evictions-archive.jsonl — the only two non-boolean
# `load_bearing` values in 24,620 archived rows, each re-archived 9 times. Both
# are observations a writer put in the wrong key, and both are non-empty, so
# every consumer (_is_flagged, capture_fast_lane._flagged, body_capture_carrier)
# counted them as TRUE and granted eviction-exemption plus a carrier push. They
# are fixtures rather than invented strings on purpose: a synthetic "not a bool"
# would prove the type check compiles, while these prove it catches the shape
# that actually reached the store.
_PROSE_LOAD_BEARING = [
    "Both sides' records are self-consistent, so neither Body can detect it "
    "locally -- only a Body that re-reads after a pull sees the change. The "
    "evidence is a two-store comparison: team-state in_flight_bodies holds the "
    "first claim time, aspirations holds the surviving one.",
    "The guard's deny message already contained the safe form. The refusal was "
    "not a knowledge gap, it was a not-reading-the-offer gap.",
]


@pytest.mark.parametrize("prose", _PROSE_LOAD_BEARING)
def test_append_refuses_prose_in_load_bearing(running_daemon, prose):
    """A non-boolean load_bearing is refused 400 rather than stored truthy."""
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/append", {"slot": "spark_capture"},
              json.dumps({"goal_id": "g-999-03", "category": "test",
                          "observation": "x" * 50, "load_bearing": prose}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        payload = json.loads(e.read())
        assert payload["error"] == "validation_failed", payload
        # The message must name the FIELD and the received TYPE — a bare
        # "validation_failed" would leave the writer guessing which key is wrong.
        assert "load_bearing" in payload["detail"], payload
        assert "str" in payload["detail"], payload
    else:
        raise AssertionError(
            "expected 400 for prose in load_bearing; storing it truthy is the "
            "defect g-115-10021 measured")


@pytest.mark.parametrize("value", [True, False])
def test_append_accepts_boolean_load_bearing(running_daemon, value):
    """POSITIVE CONTROL for the refusal above.

    Without this, a check that refused EVERY load_bearing — or an endpoint that
    happened to be 400ing for an unrelated reason — would pass the test above
    while breaking the flag entirely. Both booleans are exercised because `false`
    is the value the refusal path must not swallow: it is falsy, and a truthiness
    test written where a `is None` test belongs would reject it.
    """
    project_root, port = running_daemon
    # Distinct id per parameter: both cases share the lane, so one id would make
    # the lookup below ambiguous about which append it found.
    gid = "g-999-04-%s" % str(value).lower()
    status, body = _post(port, "/v1/wm/append", {"slot": "spark_capture"},
                         json.dumps({"goal_id": gid, "category": "test",
                                     "observation": "y" * 50,
                                     "load_bearing": value}))
    assert status == 200, body
    # Capture slots live under `slots:`, never at the top level — a top-level
    # read returns a clean, wrong answer (the wm-append wrapper warns about
    # exactly this shape).
    entries = _read_wm(project_root / "agents" / "alpha")["slots"]["spark_capture"]
    # Look the entry up by goal_id rather than taking entries[-1]: the lane is
    # SORTED by _eviction_sort_key (flagged last), so append order is not read
    # order and `[-1]` silently reads a different entry once anything flagged is
    # present. Found by mutation — disabling the refusal let two prose entries
    # into the lane and this assertion started failing on an unrelated row.
    mine = [e for e in entries if e.get("goal_id") == gid]
    assert mine, entries
    assert mine[-1]["load_bearing"] is value, mine[-1]


def test_append_accepts_omitted_load_bearing(running_daemon):
    """The field is OPTIONAL — most entries carry none, and the refusal must not
    turn an absent flag into a required one."""
    project_root, port = running_daemon
    status, body = _post(port, "/v1/wm/append", {"slot": "spark_capture"},
                         json.dumps({"goal_id": "g-999-05", "category": "test",
                                     "observation": "z" * 50}))
    assert status == 200, body
    entries = _read_wm(project_root / "agents" / "alpha")["slots"]["spark_capture"]
    mine = [e for e in entries if e.get("goal_id") == "g-999-05"]
    assert mine, entries
    assert "load_bearing" not in mine[-1], mine[-1]


def test_append_heals_int_in_goals_completed_list_slot(running_daemon):
    """2026-08-16 worker-loop Phase 4b outage: the TOP-LEVEL
    goals_completed_this_session (a LIST of hand-off rows) had been collapsed
    to an int on 3 of 3 forked Bodies checked, and every append was refused
    `not_a_list` forever — body-merge then dropped the Body's contribution
    silently. An int there carries no rows, so it is always corruption: the
    endpoint heals it to [] IN THE SAME REQUEST, appends, and says so."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    wm_path = agent_dir / "session" / "working-memory.yaml"
    data = _read_wm(agent_dir)
    data["goals_completed_this_session"] = 0          # the collided counter shape
    wm_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    status, body = _post(port, "/v1/wm/append", {"slot": "goals_completed_this_session"},
                         json.dumps({"goal_id": "g-999-01", "aspiration_id": "asp-999",
                                     "recurring": False}))
    assert status == 200, body
    out = json.loads(body)
    assert out["ok"] is True and out["healed_from"] == "int:0", out
    assert "warning" in out and "counter" in out["warning"], out
    rows = _read_wm(agent_dir)["goals_completed_this_session"]
    assert isinstance(rows, list) and rows[-1]["goal_id"] == "g-999-01", rows
    # A second append is a plain append: no heal reported, both rows kept.
    status, body = _post(port, "/v1/wm/append", {"slot": "goals_completed_this_session"},
                         json.dumps({"goal_id": "g-999-02"}))
    assert status == 200 and "healed_from" not in json.loads(body), body
    assert [r["goal_id"] for r in _read_wm(agent_dir)["goals_completed_this_session"]] == \
        ["g-999-01", "g-999-02"]


def test_append_heals_a_string_in_goals_completed_list_slot(running_daemon):
    """2026-08-28, live deployment: `wm-set.sh goals_completed_this_session
    '<timestamp>'` left a STRING in the agent-wide WM; 36 of 51 sessions cloned
    from it inherited the string and every worker close's Phase 4b append was
    refused for 39 hours, because the heal above was int-only. A string there
    carries no rows either — same heal, same loud report."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    wm_path = agent_dir / "session" / "working-memory.yaml"
    data = _read_wm(agent_dir)
    data["goals_completed_this_session"] = "2026-08-28T08:52:00"
    wm_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    status, body = _post(port, "/v1/wm/append", {"slot": "goals_completed_this_session"},
                         json.dumps({"goal_id": "g-999-03", "aspiration_id": "asp-999",
                                     "recurring": False}))
    assert status == 200, body
    out = json.loads(body)
    assert out["healed_from"] == "str:2026-08-28T08:52:00", out
    assert "stamp" in out["warning"], out
    rows = _read_wm(agent_dir)["goals_completed_this_session"]
    assert isinstance(rows, list) and len(rows) == 1 and rows[0]["goal_id"] == "g-999-03", rows


def test_set_refuses_a_scalar_into_the_hand_off_list_slot(running_daemon):
    """The set side of the same incident: the scalar that started it is refused
    with the two commands that express the intent, and a JSON array still lands."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    for raw in ('"2026-08-28T08:42:11"', "2026-08-28T08:42:11", "7"):
        try:
            _post(port, "/v1/wm/set", {"slot": "goals_completed_this_session"}, raw)
        except urllib.error.HTTPError as e:
            assert e.code == 400
            err = json.loads(e.read())
            assert err["error"] == "not_a_list_value", err
            assert "wm-append.sh goals_completed_this_session" in err["detail"], err
            assert "'[]' | wm-set.sh goals_completed_this_session" in err["detail"], err
        else:
            raise AssertionError(f"expected not_a_list_value 400 for {raw!r}")
    status, body = _post(port, "/v1/wm/set", {"slot": "goals_completed_this_session"}, "[]")
    assert status == 200, body
    assert _read_wm(agent_dir)["goals_completed_this_session"] == []
    status, body = _post(port, "/v1/wm/set", {"slot": "goals_completed_this_session"}, "null")
    assert status == 200, body  # null is "absent", which append already turns into []


def test_append_other_type_mismatch_still_refused(running_daemon):
    """The heal is scoped to the ONE hand-off slot: an int in any other array
    slot still refuses `not_a_list`."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    wm_path = agent_dir / "session" / "working-memory.yaml"
    data = _read_wm(agent_dir)
    data["slots"]["known_blockers"] = 3
    wm_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    try:
        _post(port, "/v1/wm/append", {"slot": "known_blockers"}, json.dumps({"id": "x"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "not_a_list"
    else:
        raise AssertionError("expected not_a_list 400 for known_blockers")


def test_append_not_initialized_400(running_daemon):
    project_root, port = running_daemon
    # Use bravo, whose conftest dir has no working-memory.yaml.
    try:
        _post(port, "/v1/wm/append", {"slot": "known_blockers"},
              json.dumps({"id": "x"}), agent="bravo")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "not_initialized"
    else:
        raise AssertionError("expected 400 appending to uninitialized WM")


def test_clear_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/clear", {"slot": "active_strategy"})
    assert status == 200, body
    assert _read_wm(agent_dir)["slots"]["active_strategy"] is None


# ---------------------------------------------------------------------------
# POST /v1/wm/drain-goals ()
# ---------------------------------------------------------------------------

def _seed(port, slot, entries):
    status, body = _post(port, "/v1/wm/set", {"slot": slot}, json.dumps(entries))
    assert status == 200, body


def test_drain_goals_removes_only_matching(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [
        {"goal_id": "g-1-01", "note": "a"},
        {"goal_id": "g-1-01", "note": "b"},
        {"goal_id": "g-2-02", "note": "c"},
    ])
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-1-01"]))
    assert status == 200, body
    verdict = json.loads(body)
    assert (verdict["removed"], verdict["kept"]) == (2, 1), verdict
    survivors = _read_wm(agent_dir)["slots"]["exp_capture"]
    assert [e["goal_id"] for e in survivors] == ["g-2-02"], survivors


def test_drain_goals_keeps_entries_it_cannot_classify(running_daemon):
    """An entry the classifier cannot classify must not be destroyed by it."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "encoding_capture", [
        {"goal_id": "g-3-03", "note": "doomed"},
        {"note": "no goal_id at all"},
        "a bare string, not a dict",
    ])
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "encoding_capture"},
                         json.dumps(["g-3-03"]))
    assert status == 200, body
    assert json.loads(body)["removed"] == 1
    survivors = _read_wm(agent_dir)["slots"]["encoding_capture"]
    assert len(survivors) == 2, survivors
    assert "a bare string, not a dict" in survivors


def test_drain_goals_takes_ids_never_a_survivor_list(running_daemon):
    """guard-3881: the API shape is what makes the lost update impossible.

    The retired design read the slot, filtered in the CALLER, and POSTed the
    surviving list to /v1/wm/set — a full-slot overwrite of a stale snapshot,
    so any entry appended in between was destroyed. This endpoint accepts the
    goal-id SET only; a survivor list (dicts) carries no usable id and is
    refused outright, so the old shape cannot be resurrected by accident.
    """
    _, port = running_daemon
    _seed(port, "exp_capture", [{"goal_id": "g-4-04"}])
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                             json.dumps([{"goal_id": "g-9-09", "note": "survivor"}]))
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body
    assert "empty_goal_ids" in body, body


def test_drain_goals_survives_an_append_the_caller_never_saw(running_daemon):
    """The filter runs on data read INSIDE the handler, so a late append lives.

    This is the behaviour the endpoint exists for: the caller's view of the slot
    is irrelevant because it never sends one. An entry appended after the caller
    decided — for a goal that is NOT being drained — must still be present after
    the drain.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [{"goal_id": "g-5-05", "note": "old"}])
    # The caller decides on ["g-5-05"] here. THEN a peer appends:
    status, body = _post(port, "/v1/wm/append", {"slot": "exp_capture"},
                         json.dumps({"goal_id": "g-6-06", "note": "late"}))
    assert status == 200, body
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-5-05"]))
    assert status == 200, body
    survivors = _read_wm(agent_dir)["slots"]["exp_capture"]
    assert [e["goal_id"] for e in survivors] == ["g-6-06"], survivors


def test_drain_goals_refuses_a_non_capture_slot(running_daemon):
    """Blast radius: the only destructive goal-keyed primitive stays scoped."""
    _, port = running_daemon
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "conclusions"},
                             json.dumps(["g-7-07"]))
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body
    assert "slot_not_drainable" in body, body


def test_drain_goals_refuses_an_empty_id_array(running_daemon):
    """A drain with no ids is a caller defect, and must be loud, not a no-op."""
    _, port = running_daemon
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                             json.dumps([]))
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body
    assert "empty_goal_ids" in body, body


def test_drain_goals_advances_slot_meta(running_daemon):
    """guard-540: wm-prune ages a lane from slot_meta.updated_at.

    A drain that left the meta untouched would leave the lane aged from its last
    APPEND, so the prune cadence could evict the very survivors it just spared.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [{"goal_id": "g-8-08"}, {"goal_id": "g-9-09"}])
    before = _read_wm(agent_dir)["slot_meta"]["exp_capture"]["update_count"]
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-8-08"]))
    assert status == 200, body
    meta = _read_wm(agent_dir)["slot_meta"]["exp_capture"]
    assert meta["update_count"] == before + 1, meta
    assert meta["updated_at"], meta


def test_drain_goals_no_match_leaves_the_slot_alone(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [{"goal_id": "g-10-10"}])
    before = _read_wm(agent_dir)["slot_meta"]["exp_capture"]["update_count"]
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-nope-99"]))
    assert status == 200, body
    assert json.loads(body) == {"ok": True, "slot": "exp_capture",
                                "removed": 0, "kept": 1}
    # No write at all, so no meta bump either.
    assert _read_wm(agent_dir)["slot_meta"]["exp_capture"]["update_count"] == before


def test_drain_goals_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                             json.dumps(["g-1-01"]), agent=None)
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body


def test_init_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "bravo"
    status, body = _post(port, "/v1/wm/init", {}, agent="bravo")
    assert status == 200, body
    assert json.loads(body)["slots"] >= 1
    assert (agent_dir / "session" / "working-memory.yaml").exists()


def test_ages_roundtrip(running_daemon):
    _, port = running_daemon
    status, body = _get(port, "/v1/wm/ages")
    assert status == 200, body
    data = json.loads(body)
    assert "active_context" in data


def test_prune_dry_run(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/wm/prune", {"dry_run": "1"})
    assert status == 200, body
    data = json.loads(body)
    assert data["dry_run"] is True
    assert "report" in data


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler output == real CLI output
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, agent: Path, project_root: Path, world: Path):
        self.agent = agent
        self.project_root = project_root
        self.world = world
        self.agent_name = "alpha"

    def wm_path(self, unit_key=None):
        # Mirrors AgentPaths.wm_path ( per-Body routing): no unit_key /
        # no forked body-WM-file collapses to the agent-wide WM. Tests pass no
        # SID header, so the fallback is the only branch exercised.
        return self.agent / "session" / "working-memory.yaml"


class _FakeCtx:
    def __init__(self, agent: Path, project_root: Path, world: Path,
                 query: dict, body: bytes, *, agent_name="alpha"):
        self.paths = _FakePaths(agent, project_root, world)
        self.query = query
        self.body = body
        self.headers = {"x-mind-agent": agent_name}


def _run_wm_cli(world, meta, agent_dir, args, stdin_text):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    world.mkdir(parents=True, exist_ok=True)
    meta.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(WM_PY), *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI wm.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


@pytest.mark.skipif(yaml is None, reason="PyYAML required")
@pytest.mark.skipif(not WM_PY.exists(), reason="core/scripts/wm.py missing")
def test_byte_compat_set_top_level(tmp_path):
    """Top-level set self-heals to _default_wm_data (deterministic, no
    slot_meta timestamps) then sets the key — fully byte-comparable. Both
    sides read the REAL memory-pipeline.yaml so slot_types match."""
    from mind_api.src.endpoints import wm_write

    cli_agent = tmp_path / "cli-agent"
    dae_agent = tmp_path / "dae-agent"

    _run_wm_cli(tmp_path / "world", tmp_path / "meta", cli_agent,
                ["set", "last_goal_category"], "framework-loop")
    wm_write.set_slot(_FakeCtx(dae_agent, REPO_ROOT, tmp_path / "world",
                               {"slot": "last_goal_category"},
                               b"framework-loop"))

    cli_wm = (cli_agent / "session" / "working-memory.yaml").read_bytes()
    dae_wm = (dae_agent / "session" / "working-memory.yaml").read_bytes()
    assert dae_wm == cli_wm


# ---------------------------------------------------------------------------
# : the WRAPPER layer — wm-append.sh discarded the daemon response
# ---------------------------------------------------------------------------
#
#  fixed the DAEMON: a newly appended entry is never its own eviction
# victim, and test_capture_eviction_newcomer.py pins that behaviour. This file
# covers the layer that fix does not touch. wm-append.sh discarded the entire
# response with `> /dev/null`, so `evicted` — reported by the daemon since
#  — had never once reached a caller. A fix is not shipped when the
# producer emits it; it is shipped when a consumer displays it (guard-742/547
# one layer further out: daemon-vs-wrapper, not CLI-vs-daemon).
#
# Post- an eviction always destroys an OLD entry. WHICH old entry is
# floor-aware since  and this comment named the wrong branch until
# : when flagged entries exceed (cap - _unflagged_floor) the oldest
# FLAGGED one goes, otherwise the oldest UNFLAGGED one goes. The seed below is
# 100% load_bearing, so THIS test exercises the FLAGGED branch — the
# CONDITIONALLY recoverable one: a flagged entry is mirrored to the Body's
# carrier at append time, which makes delivery possible but does not effect it.
# The carrier still has to PUSH. That push USED to fail structurally on a
# non-reducer Body — the destination sat inside the claim-protected agent tree
# and a worker never holds the runner claim (measured: 101 undelivered rows on
# one worker box).  moved the carrier to world/body-carriers/<agent>/,
# which is claim-EXEMPT, so a worker push now works (verified from a worker Body
# 2026-09-03). The DEPENDENCY this test pins is unchanged and is why the
# assertions below still stand: carrier-backing is a PRECONDITION, not a
# guarantee — a transport failure still leaves the entry undelivered, and the
# wrapper cannot observe delivery either way. The unflagged branch is
# unrecoverable everywhere (the WM slot is that entry's only copy).
#
# known_blockers (array_limits 10) is used rather than a capture lane: the
# behaviour is slot-agnostic, and known_blockers has no per-slot validation.

_KB_LIMIT = 10


def _seed_known_blockers(agent_dir: Path, n: int, *, load_bearing: bool):
    """Write n incumbents straight to disk so cap state is exact, not inferred."""
    p = agent_dir / "session" / "working-memory.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data.setdefault("slots", {})["known_blockers"] = [
        {"id": f"seed-{i}", "reason": "incumbent",
         "load_bearing": load_bearing, "_item_ts": "2020-01-01T00:00:00"}
        for i in range(n)
    ]
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


WM_APPEND_SH = REPO_ROOT / "core" / "scripts" / "wm-append.sh"


@pytest.mark.skipif(not WM_APPEND_SH.exists(), reason="wm-append.sh missing")
def test_wrapper_surfaces_eviction_on_stderr(running_daemon):
    """The THIRD layer, and the one that made the daemon-side fix inert without
    it: wm-append.sh discarded the entire response with `> /dev/null`, so the
    `evicted` field the daemon has reported since g-306-289 never reached a
    single operator. A daemon-only fix would have shipped and changed nothing
    an agent can see (guard-742 class).

    STDOUT must stay empty — the documented wrapper contract is "print nothing
    on success" and callers parse it. The diagnostic goes to STDERR.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed_known_blockers(agent_dir, _KB_LIMIT, load_bearing=True)

    env = dict(os.environ)
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    proc = subprocess.run(
        ["bash", str(WM_APPEND_SH), "known_blockers"],
        input=json.dumps({"id": "wrapper-probe", "reason": "destroyed"}),
        text=True, env=env, cwd=str(REPO_ROOT), capture_output=True, timeout=60,
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.stdout.strip() == "", (
        "the wrapper contract is silent-stdout on success; a diagnostic there "
        f"would break every caller that parses it: {proc.stdout!r}")
    assert "[wm-append]" in proc.stderr, (
        "the eviction must reach the operator, not die in `> /dev/null`: "
        f"{proc.stderr!r}")
    assert "1 older entry evicted" in proc.stderr, proc.stderr

    # : the notice must NAME BOTH victim branches and must not assert
    # the one it cannot observe. The wrapper receives `evicted` as a bare COUNT,
    # so claiming a victim class is a claim beyond what its path saw
    # (guard-2947). The previous wording said the victim is unconditionally "the
    # one that has waited longest for the reducer" — false in the flagged branch
    # THIS test's own 100%-load_bearing seed exercises, and not harmlessly: a
    # peer Body read that line, concluded that relaying through a capture lane
    # would destroy another goal's undelivered observation, and routed around
    # the lane when the append it declined would have evicted a carrier-backed
    # peer.
    assert "floor-aware" in proc.stderr, proc.stderr
    for branch in ("FLAGGED", "UNFLAGGED"):
        assert branch in proc.stderr, (
            f"the notice must name the {branch} branch so the reader can tell a "
            f"recoverable eviction from an unrecoverable one: {proc.stderr!r}")
    # Negative control on the specific false claim, so a revert to the
    # unconditional wording fails here rather than silently re-misinforming.
    assert "The victim is the OLDEST peer" not in proc.stderr, (
        "regressed to the unconditional victim claim g-306-353 removed: "
        f"{proc.stderr!r}")

    # : the SECOND false claim in this notice, and the same shape as
    # the one above — an unobservable guarantee stated as fact. Naming the
    # FLAGGED branch "recoverable" is only true where the carrier can PUSH. When
    # this was written a non-reducer Body structurally could not (claim-protected
    # destination); since the carrier moved to world/ it can, and these two
    # assertions are UNCHANGED by that, deliberately — the wrapper still cannot
    # observe delivery, so stating it remains a claim beyond what its path saw.
    # The notice must state the dependency and hand the reader the tell, or a
    # worker Body reads "carrier-backed" and treats a loss as safe.
    assert "so it still reaches the reducer" not in proc.stderr, (
        "regressed to asserting delivery the wrapper cannot observe; "
        f"carrier-backing is a precondition, not a guarantee: {proc.stderr!r}")
    assert "push FAILED" in proc.stderr, (
        "the notice must name the stderr tell that distinguishes a live "
        f"carrier from a dark one, or the dependency is unactionable: {proc.stderr!r}")

    # : the newcomer is protected, so it is the entry that SURVIVES
    # and an old peer is the one destroyed. Asserting this here keeps the
    # wrapper test honest about which layer it is exercising.
    arr = _read_wm(agent_dir)["slots"]["known_blockers"]
    assert any(e.get("id") == "wrapper-probe" for e in arr), arr
    assert len(arr) == _KB_LIMIT


@pytest.mark.skipif(not WM_APPEND_SH.exists(), reason="wm-append.sh missing")
def test_wrapper_stays_silent_on_an_ordinary_append(running_daemon):
    """No-false-positive half for the wrapper. Almost every append is ordinary;
    a wrapper that printed on all of them would train callers to ignore it."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed_known_blockers(agent_dir, 2, load_bearing=False)

    env = dict(os.environ)
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    proc = subprocess.run(
        ["bash", str(WM_APPEND_SH), "known_blockers"],
        input=json.dumps({"id": "quiet-probe", "reason": "fits"}),
        text=True, env=env, cwd=str(REPO_ROOT), capture_output=True, timeout=60,
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.stdout.strip() == "", proc.stdout
    assert "evicted to make room" not in proc.stderr, proc.stderr
    assert any(e.get("id") == "quiet-probe"
               for e in _read_wm(agent_dir)["slots"]["known_blockers"])


# ---------------------------------------------------------------------------
#  outcome 3 — a concurrent prune is REFUSED, not queued.
# ---------------------------------------------------------------------------


def test_a_concurrent_prune_is_REFUSED_409_and_does_not_run_the_body(
        monkeypatch, tmp_path):
    """A second prune must fail fast rather than wait out the first.

    THE DEFECT: file_locks.locked() acquires its threading.Lock with NO timeout
    (its `timeout` governs only the file lock beneath), so a second prune
    blocked indefinitely. The client cannot tell a timeout from a dead daemon —
    rt_curl maps curl's exit 28 and a connection failure to the same rc=3 — so
    it re-POSTs, and the retries pile up. Measured cc-04 2026-09-15: five calls
    at 481/396/311/226/109 s ending within 20 s of each other, the queued ones
    returning pruned_items: [] because the first had already drained the lane.

    The load-bearing assertion is the LAST one: a 409 that still ran the body
    would have fixed nothing.
    """
    from mind_api.src.endpoints import wm_write

    wm_file = tmp_path / "working-memory.yaml"
    ran = []
    monkeypatch.setattr(wm_write, "_require_agent_header", lambda ctx: None)
    monkeypatch.setattr(wm_write, "_wm_path", lambda ctx: wm_file)
    monkeypatch.setattr(wm_write, "_prune_locked",
                        lambda ctx: ran.append("body") or "RAN")

    # POSITIVE CONTROL FIRST. Without it a green 409 test is equally consistent
    # with a prune that never runs at all, or a gate that is permanently held.
    assert wm_write.prune(object()) == "RAN"
    assert ran == ["body"], "an uncontended prune must actually run"

    gate = wm_write._prune_gate(wm_file)
    assert gate.acquire(blocking=False), (
        "the gate must be RELEASED after a completed prune — a gate that leaks "
        "would 409 this working memory forever")
    try:
        resp = wm_write.prune(object())
    finally:
        gate.release()

    assert resp.status == 409, f"expected 409, got {resp.status}"
    assert b"prune_in_progress" in resp.body
    assert ran == ["body"], (
        "the REFUSED prune must not have run the body — queueing and then "
        "running a redundant full prune is the whole defect (g-115-9962)")


def test_the_prune_gate_is_per_working_memory_not_global(tmp_path):
    """Two agents on one box must never refuse each other's prune."""
    from mind_api.src.endpoints import wm_write

    a = tmp_path / "alpha" / "working-memory.yaml"
    b = tmp_path / "bravo" / "working-memory.yaml"
    gate_a, gate_b = wm_write._prune_gate(a), wm_write._prune_gate(b)

    assert gate_a is not gate_b, "distinct working memories must not share a gate"
    assert gate_a is wm_write._prune_gate(a), (
        "the SAME working memory must get the SAME gate, or the guard is a no-op")
    assert gate_a.acquire(blocking=False)
    try:
        assert gate_b.acquire(blocking=False), (
            "holding alpha's gate must not block bravo's prune")
        gate_b.release()
    finally:
        gate_a.release()


def test_daemon_archive_evicted_captures_is_ONE_write_for_N_rows(tmp_path, monkeypatch):
    """guard-742/2323 twin parity for the batched archive — the LIVE path.

    core/scripts/tests/test_wm_prune_capture_eviction.py pins this invariant on
    the CLI twin (wm.archive_evicted_captures). THIS is the twin the daemon
    actually executes, and the measured incident — 4,829 versions / 231.3 GiB
    of one key in a single UTC day — happened on the DAEMON path, not the CLI
    one. Until now only the un-run twin was pinned, so a regression reintroduced
    here would ship with the suite green (g-115-9962).

    Counting the WRITES is the only assertion that separates the fix from the
    defect: the archived CONTENT is identical whether N rows go out in one
    locked write or N of them.
    """
    from mind_api.src.endpoints import wm_write
    import _fileops

    calls = []
    monkeypatch.setattr(_fileops, "locked_append_jsonl_many",
                        lambda path, items: calls.append((path, list(items))))

    rows = [{"goal_id": f"g-000-{i:02d}", "_item_ts": f"2026-09-20T10:0{i}:00"}
            for i in range(6)]
    assert wm_write.archive_evicted_captures(
        str(tmp_path), "spark_capture", rows, "array_limit") is True

    assert len(calls) == 1, (
        f"6 evicted rows produced {len(calls)} locked writes — the batched "
        f"archive must issue exactly ONE (one whole-object PUT), not one per "
        f"row. One-per-row is the O(N^2) defect (guard-6134, guard-6904).")
    assert len(calls[0][1]) == 6, "all six rows must ride in the single write"
    assert [r["entry"] for r in calls[0][1]] == rows, (
        "every evicted row must reach the archive, in eviction order — "
        "archive-before-delete is only honoured if the batch is complete")
