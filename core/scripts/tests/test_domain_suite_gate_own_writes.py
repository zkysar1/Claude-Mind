"""Pins the own-writes narrowing of core/scripts/domain-suite-gate.py ().

The gate used to run the whole domain suite on a close whenever ANY code file under
$WORLD_PATH/scripts was newer than the goal's claim. On a fleet-synced tree that is mostly
another session's file, and the run costs the full 900 s bound for no verdict. The gate now
keeps only the files this unit's sessions wrote, read from the per-agent edit log the
PostToolUse hook fills (uncommitted-edits.jsonl).

Three things are pinned, each against its control (guard-1082):
  1. own_writes() — which rows count: this unit's sids, inside the claim window, under the scripts tree.
  2. evaluate() — a peer-only change skips the suite, an own change runs it, and every blind spot
     the log can see (no sid, no log, a world outside the project root) keeps the wide trigger.
     "Skipped" is observed through a runner hook that leaves a marker, not inferred from rc.
  3. The coverage pin rb-12529 asks for: the REAL recorder, fed a world-script Write payload,
     produces a row the gate attributes. A recorder "fixed" to match its own header (which says
     world is skipped) would drop that row and silently fail the gate OPEN for every own write.

The gate runs in-process here with its telemetry stubbed, so no test writes a live gate-firings
row or a live log.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE = CORE_SCRIPTS / "domain-suite-gate.py"

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(CORE_SCRIPTS))
from _bash_helpers import BASH  # noqa: E402

SID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"   # the closing session
SID_CLAIM = "cccccccc-3333-4333-8333-cccccccccccc"  # the session the claim recorded (a restart handed the unit on)
SID_PEER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"   # a sibling session of the same agent on the same box
PREFIX = ".mind-data/world/scripts/"

# Every framework-env name a leaked session could carry (same prefixes as the recorder tests).
_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_", "BODY_",
)


def _gate(monkeypatch, tmp_path):
    """A fresh import of the gate with its telemetry stubbed and its retained-log dir in tmp."""
    monkeypatch.setenv("DOMAIN_SUITE_LOG_DIR", str(tmp_path / "retained"))
    spec = importlib.util.spec_from_file_location("domain_suite_gate_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls: list[dict] = []
    mod._gate_log = lambda *a, **k: calls.append({"args": a, "kwargs": k})
    mod._telemetry = calls
    return mod


def _tree(tmp_path: Path, marker: Path | None = None) -> tuple[Path, Path, Path]:
    """(project root, world dir, scripts dir) with the world INSIDE the root, like a fleet box."""
    root = tmp_path / "root"
    world = root / ".mind-data" / "world"
    scripts = world / "scripts"
    (scripts / "pkg").mkdir(parents=True)
    (scripts / "tests").mkdir()
    (scripts / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (scripts / "pkg" / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    (scripts / "tests" / "test_peer.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    if marker is not None:
        # The runner hook: leaves a marker when the gate runs the suite, so a SKIP is observed, not inferred.
        (scripts / "run-domain-tests.sh").write_text(f'#!/usr/bin/env bash\necho ran > "{marker}"\nexit 0\n', encoding="utf-8")
    return root, world, scripts


def _row(rel: str, sid: str | None, mtime: float, tree: str = PREFIX) -> dict:
    return {"file": tree + rel, "mtime": int(mtime), "edit_ts": "2026-10-02T12:00:00", "goal_id": "", "sid": sid}


def _ledger(path: Path, rows: list, raw_lines: list[str] | None = None) -> Path:
    lines = [json.dumps(r) for r in rows] + (raw_lines or [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


SINCE = datetime(2026, 10, 2, 12, 0, 0)
SINCE_EPOCH = SINCE.timestamp()


# ─── 1. own_writes: which rows count ──────────────────────────────────────

def test_own_writes_keeps_this_units_rows_inside_the_window(monkeypatch, tmp_path):
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    ledger = _ledger(tmp_path / "ledger.jsonl", [
        _row("pkg/config.py", SID, SINCE_EPOCH + 30),                  # mine, in the window
        _row("tests/test_peer.py", SID_PEER, SINCE_EPOCH + 40),        # a sibling session's file
        _row("pkg/old.py", SID, SINCE_EPOCH - 3600),                   # mine, but long before the claim
        _row("x.py", SID, SINCE_EPOCH + 50, tree="core/scripts/"),     # mine, but another tree
        _row("y.py", SID, SINCE_EPOCH + 55,                            # a pyc whose path merely CONTAINS the prefix
             tree="core/.pycache/opt/ayoai-mind/.mind-data/world/scripts/"),
    ])
    mine, why = g.own_writes(scripts, SINCE, {SID}, ledger, root)
    assert why == ""
    assert mine == {"pkg/config.py"}


def test_own_writes_counts_a_write_just_before_the_claim_but_not_long_before(monkeypatch, tmp_path):
    # SLACK_SECONDS is 60: a Body that edits in the same minute it claims must not slip under the window.
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    ledger = _ledger(tmp_path / "ledger.jsonl", [
        _row("inside_slack.py", SID, SINCE_EPOCH - 30),
        _row("outside_slack.py", SID, SINCE_EPOCH - 90),
    ])
    mine, _why = g.own_writes(scripts, SINCE, {SID}, ledger, root)
    assert mine == {"inside_slack.py"}


def test_own_writes_matches_any_of_the_units_sessions(monkeypatch, tmp_path):
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    ledger = _ledger(tmp_path / "ledger.jsonl", [
        _row("a.py", SID, SINCE_EPOCH + 5), _row("b.py", SID_CLAIM, SINCE_EPOCH + 6), _row("c.py", SID_PEER, SINCE_EPOCH + 7),
    ])
    mine, _why = g.own_writes(scripts, SINCE, {SID, SID_CLAIM}, ledger, root)
    assert mine == {"a.py", "b.py"}


@pytest.mark.parametrize("case", ["no-sids", "no-log", "log-is-none", "world-outside-root"])
def test_own_writes_answers_none_with_a_reason_when_the_log_cannot_speak(monkeypatch, tmp_path, case):
    # Skipping the suite needs POSITIVE evidence. Each blind spot the function can see must keep the wide trigger.
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, SINCE_EPOCH + 30)])
    args = {"sids": {SID}, "ledger": ledger, "project_root": root}
    expect = {
        "no-sids": ("sids", set(), "no session id"),
        "no-log": ("ledger", tmp_path / "missing.jsonl", "no edit log"),
        "log-is-none": ("ledger", None, "no edit log"),
        "world-outside-root": ("project_root", tmp_path / "elsewhere", "outside the project root"),
    }[case]
    args[expect[0]] = expect[1]
    (tmp_path / "elsewhere").mkdir(exist_ok=True)
    mine, why = g.own_writes(scripts, SINCE, args["sids"], args["ledger"], args["project_root"])
    assert mine is None
    assert expect[2] in why


def test_own_writes_answers_none_when_the_log_exists_but_cannot_be_read(monkeypatch, tmp_path):
    # A present-but-unreadable log is a blind spot too: an empty answer there would read as "wrote nothing".
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, SINCE_EPOCH + 30)])

    def refuse(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(g, "open", refuse, raising=False)   # shadows the builtin inside the gate module only
    mine, why = g.own_writes(scripts, SINCE, {SID}, ledger, root)
    assert mine is None and "unreadable" in why and "PermissionError" in why


def test_own_writes_control_the_same_inputs_attribute_when_nothing_is_blind(monkeypatch, tmp_path):
    # The positive control for the table above: same ledger, same tree, nothing blind -> an answer, not None.
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, SINCE_EPOCH + 30)])
    mine, why = g.own_writes(scripts, SINCE, {SID}, ledger, root)
    assert mine == {"pkg/config.py"} and why == ""


def test_own_writes_tolerates_garbage_rows_and_still_counts_the_good_one(monkeypatch, tmp_path):
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    junk = [
        "{not json " + PREFIX + "bad.py",                                   # unparsable, names the tree
        json.dumps([PREFIX + "list.py"]) + "  " + PREFIX,                   # parses to a list
        json.dumps({"file": PREFIX + "listsid.py", "mtime": SINCE_EPOCH + 9, "sid": [SID]}),   # unhashable sid
        json.dumps({"file": PREFIX + "boolmt.py", "mtime": True, "sid": SID}),
        json.dumps({"file": PREFIX + "nomtime.py", "sid": SID}),
        json.dumps({"file": 7, "mtime": SINCE_EPOCH + 9, "sid": SID, "x": PREFIX}),
    ]
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("good.py", SID, SINCE_EPOCH + 20)], raw_lines=junk)
    mine, why = g.own_writes(scripts, SINCE, {SID}, ledger, root)
    assert why == "" and mine == {"good.py"}


def test_own_writes_relates_a_resolved_scripts_dir_to_a_root_given_through_a_symlink(monkeypatch, tmp_path):
    # The scripts dir arrives resolved while the root was handed in through a symlink: only the resolved
    # pair relates them. Without it the gate would answer "outside the project root" and run the suite.
    g = _gate(monkeypatch, tmp_path)
    root, _world, scripts = _tree(tmp_path)
    link = tmp_path / "root-link"
    link.symlink_to(root, target_is_directory=True)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, SINCE_EPOCH + 30)])
    mine, why = g.own_writes(scripts, SINCE, {SID}, ledger, link)
    assert why == "" and mine == {"pkg/config.py"}


def test_unit_sessions_unions_the_closing_process_and_the_claim(monkeypatch, tmp_path):
    g = _gate(monkeypatch, tmp_path)
    monkeypatch.setenv("MIND_SID", SID)
    assert g.unit_sessions(None) == {SID}
    assert g.unit_sessions({"claimed_by_sid": SID_CLAIM, "executed_by_sid": SID_PEER}) == {SID, SID_CLAIM, SID_PEER}
    assert g.unit_sessions({"claimed_by_sid": None, "executed_by_sid": "  "}) == {SID}   # blanks never become an id
    monkeypatch.delenv("MIND_SID")
    assert g.unit_sessions({"claimed_by_sid": SID_CLAIM}) == {SID_CLAIM}
    assert g.unit_sessions(None) == set()


def test_edits_ledger_is_the_agents_session_log(monkeypatch, tmp_path):
    g = _gate(monkeypatch, tmp_path)
    p = g.edits_ledger("alpha")
    assert p is not None and p.parts[-3:] == ("alpha", "session", "uncommitted-edits.jsonl")
    assert g.edits_ledger(None) is None and g.edits_ledger("") is None


# ─── 2. evaluate: skip on positive evidence, run otherwise ────────────────

def _evaluate(g, scripts_world: Path, root: Path, ledger: Path | None, capsys, since=SINCE - timedelta(days=3650)):
    rc = g.evaluate("g-999-01", "world", since, None, 60, scripts_world, ledger=ledger, project_root=root)
    out = capsys.readouterr()
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"exactly one JSON line expected on stdout, got: {out.out!r}"
    return rc, json.loads(lines[0]), out.err


def test_a_peer_only_change_skips_the_suite_and_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    now = time.time()
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("tests/test_peer.py", SID_PEER, now), _row("pkg/config.py", SID_PEER, now)])
    rc, doc, err = _evaluate(g, world, root, ledger, capsys)
    assert rc == 0 and doc["decision"] == "noop"
    assert not marker.exists(), "the runner hook ran: the suite was NOT skipped"
    assert "none written by this unit's sessions" in doc["reason"]
    assert "pkg/config.py" in doc["peer_touched"] and "tests/test_peer.py" in doc["peer_touched"]
    assert "[domain-suite-gate] not running the domain suite for g-999-01" in err
    assert not [ln for ln in err.splitlines() if ln.startswith("[domain-suite-gate] running")]
    assert "Bash" in err   # the blind spot is named at the moment of the skip
    (call,) = g._telemetry
    assert call["kwargs"]["trigger_matched"] is False and call["kwargs"]["payload"]["peer_touched"] == len(doc["peer_touched"])


def test_an_own_change_runs_the_suite_and_names_only_the_own_files(monkeypatch, tmp_path, capsys):
    # The control for the skip above: same tree, same peers, plus ONE row of this session's own.
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    now = time.time()
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, now), _row("tests/test_peer.py", SID_PEER, now)])
    rc, doc, err = _evaluate(g, world, root, ledger, capsys)
    assert rc == 0 and doc["decision"] == "pass"
    assert marker.exists(), "the runner hook did not run: an own change must run the suite"
    assert [t[0] for t in doc["touched"]] == ["pkg/config.py"]
    (line,) = [ln for ln in err.splitlines() if ln.startswith("[domain-suite-gate] running")]
    assert "1 domain script(s) changed since the claim (pkg/config.py), all written by this unit's sessions" in line
    assert "test_peer" not in line
    assert doc.get("peer_touched") is None


def test_a_write_by_the_session_the_claim_recorded_counts_as_own(monkeypatch, tmp_path, capsys):
    # The closing process is SID; the claim says SID_CLAIM started the unit (a restart handed it on).
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    claimed = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    g.claim_record = lambda goal, source: {"id": goal, "claimed_at": claimed, "claimed_by_sid": SID_CLAIM, "executed_by_sid": SID_CLAIM}
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID_CLAIM, time.time() - 600)])
    rc, doc, _err = _evaluate(g, world, root, ledger, capsys, since=None)
    assert rc == 0 and doc["decision"] == "pass" and marker.exists()


def test_a_write_from_before_the_claim_is_not_this_units_even_when_the_file_is_newer(monkeypatch, tmp_path, capsys):
    # An earlier goal's edit that a later push or sync re-stamped: file mtime is new, the row's mtime is old.
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    claimed = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    g.claim_record = lambda goal, source: {"id": goal, "claimed_at": claimed, "claimed_by_sid": SID}
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, time.time() - 6 * 3600)])
    rc, doc, _err = _evaluate(g, world, root, ledger, capsys, since=None)
    assert rc == 0 and doc["decision"] == "noop" and not marker.exists()


@pytest.mark.parametrize("blind", ["no-session-id", "no-log", "world-outside-root"])
def test_every_blind_spot_the_gate_can_see_keeps_the_wide_trigger_and_says_why(monkeypatch, tmp_path, capsys, blind):
    marker = tmp_path / "ran.marker"
    if blind == "no-session-id":
        monkeypatch.delenv("MIND_SID", raising=False)
    else:
        monkeypatch.setenv("MIND_SID", SID)
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("tests/test_peer.py", SID_PEER, time.time())])  # peers only
    if blind == "no-log":
        ledger = tmp_path / "missing.jsonl"
    if blind == "world-outside-root":
        root = tmp_path / "elsewhere"
        root.mkdir()
    rc, doc, err = _evaluate(g, world, root, ledger, capsys)
    assert rc == 0 and doc["decision"] == "pass"
    assert marker.exists(), "a blind spot must run the suite, never skip it"
    (line,) = [ln for ln in err.splitlines() if ln.startswith("[domain-suite-gate] running")]
    assert "[not narrowed to this unit's own writes:" in line


def test_the_six_hour_fallback_is_intersected_with_own_writes_too(monkeypatch, tmp_path, capsys):
    # The agent queue never claims, so there the fallback window IS the normal path (rb-10907).
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    g.claim_record = lambda goal, source: None
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("tests/test_peer.py", SID_PEER, time.time())])
    rc, doc, err = _evaluate(g, world, root, ledger, capsys, since=None)
    assert rc == 0 and doc["decision"] == "noop" and not marker.exists()
    assert "claimed_at unreadable; used the last 6 hours" in doc["reason"]
    assert "claimed_at unreadable" in err


def test_nothing_changed_is_still_the_plain_noop_with_no_skip_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MIND_SID", SID)
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, tmp_path / "ran.marker")
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, time.time())])
    rc, doc, err = _evaluate(g, world, root, ledger, capsys, since=datetime(2999, 1, 1))
    assert rc == 0 and doc["decision"] == "noop" and doc["reason"].startswith("no domain script modified since 2999")
    assert "not running" not in err and doc.get("peer_touched") is None


# ─── 3. the coverage pin: the real recorder -> the gate ───────────────────

def _recorder_repo(tmp_path: Path) -> Path:
    """A repo whose core/scripts holds the REAL recorder + _paths.sh, so _paths.sh anchors PROJECT_ROOT to it."""
    repo = tmp_path / "recorder-repo"
    agent = repo / "agents" / "alpha"
    (agent / "session").mkdir(parents=True)
    (agent / "self.md").write_text("# alpha\n", encoding="utf-8")
    (agent / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8")
    core = repo / "core" / "scripts"
    core.mkdir(parents=True)
    (repo / ".claude").mkdir()
    for name in ("uncommitted-edits-record.sh", "_paths.sh", "_python_launcher.sh"):
        dst = core / name
        dst.write_bytes((CORE_SCRIPTS / name).read_bytes())
        dst.chmod(0o755)
    stub = core / "team-state-read.sh"
    stub.write_text("#!/usr/bin/env bash\necho null\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    shim = core / ".python-shim"
    shim.mkdir()
    for name in ("python3", "python"):
        s = shim / name
        s.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        s.chmod(0o755)
    return repo


def _record_write(repo: Path, target: Path, sid: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"}
    env["MIND_AGENT"] = "alpha"
    payload = json.dumps({"session_id": sid, "tool_input": {"file_path": str(target)}})
    return subprocess.run([BASH, str(repo / "core" / "scripts" / "uncommitted-edits-record.sh")], input=payload,
                          capture_output=True, text=True, timeout=60, env=env, check=False)


def test_the_real_recorder_records_a_world_script_write_and_the_gate_attributes_it(monkeypatch, tmp_path):
    # THE COVERAGE PIN (rb-12528 precondition b, rb-12529). The recorder's own header says world paths are
    # skipped; the code skips only the VIRTUAL `world/` prefix, so an absolute path under the project root is
    # recorded. If someone "fixes" the code to match the header, every own world row disappears and this gate
    # would skip its suite for every own write. This test fails first.
    g = _gate(monkeypatch, tmp_path)
    repo = _recorder_repo(tmp_path)
    scripts = repo / ".mind-data" / "world" / "scripts"
    (scripts / "tests").mkdir(parents=True)
    target = scripts / "tests" / "test_written_by_the_unit.py"
    target.write_text("def test_x():\n    assert True\n", encoding="utf-8")

    r = _record_write(repo, target, SID)
    assert r.returncode == 0, r.stderr
    ledger = repo / "agents" / "alpha" / "session" / "uncommitted-edits.jsonl"
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [x["file"] for x in rows] == [".mind-data/world/scripts/tests/test_written_by_the_unit.py"], rows
    assert rows[0]["sid"] == SID

    mine, why = g.own_writes(scripts, datetime.now() - timedelta(hours=1), {SID}, ledger, repo)
    assert why == "" and mine == {"tests/test_written_by_the_unit.py"}
    # Control: another session's id does not claim it.
    other, _why = g.own_writes(scripts, datetime.now() - timedelta(hours=1), {SID_PEER}, ledger, repo)
    assert other == set()


def test_the_real_recorder_records_nothing_for_a_world_outside_the_root_and_the_gate_knows_it(monkeypatch, tmp_path):
    # The documented blind spot (rb-12528 precondition b, other half): a world outside the project root is
    # dropped by the recorder, so an empty answer would mean "nothing recorded". The gate must answer None.
    g = _gate(monkeypatch, tmp_path)
    repo = _recorder_repo(tmp_path)
    outside = tmp_path / "outside-world" / "scripts"
    outside.mkdir(parents=True)
    target = outside / "written.py"
    target.write_text("x = 1\n", encoding="utf-8")

    r = _record_write(repo, target, SID)
    assert r.returncode == 0, r.stderr
    ledger = repo / "agents" / "alpha" / "session" / "uncommitted-edits.jsonl"
    assert not ledger.exists() or ledger.read_text(encoding="utf-8").strip() == ""
    mine, why = g.own_writes(outside, datetime.now() - timedelta(hours=1), {SID}, ledger, repo)
    assert mine is None
    assert "outside the project root" in why or "no edit log" in why
