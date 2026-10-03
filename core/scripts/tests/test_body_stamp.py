"""test_body_stamp.py — : the writer signs a worker Body's text, never the Body.

WHAT BROKE. A worker Body signed what it filed and noted ("<agent> worker Body
<sid>, <host>") from memory, and nothing ever handed it its host. A census on
2026-10-02 found 8 of 22 such signatures naming the wrong box. The fix moves the
signature into the three writers a Body sends model text through, which read the
facts from the Body's own environment: goal-field-append.py (a progress_note or
outcome_note block), closure-evidence-write.sh (the closure narrative) and
aspirations-add-goal.sh (a filing's description). worker-loop's "Mark it" step
now tells the Body never to type its host or sid.

WHAT IS PINNED.
  1. The helper: lines for a worker Body, none for anyone else, and a
     description filter that passes anything it cannot sign through unchanged.
     Its CLI reads and writes UTF-8 whatever the platform's default.
  2. The filing line stays out of the way of the gates that read a description
     as scope: no file path, no sid, and no structural token but the host,
     checked with the duplication gate's and the close-risk tier's own patterns.
  3. Each writer signs a worker's text and stores anyone else's byte-exact, and
     goal-field-append.py stores a worker's description append unsigned. Every
     negative carries its positive control in the same test, so a writer that can
     never sign cannot pass it (guard-4166).
  4. closure-evidence-write.sh leaves the deferral path's preserved note unsigned,
     and a helper failure in either bash writer is said aloud, never silent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _bash_helpers import BASH  # noqa: E402  (rb-1472: bin-first, clean-PATH-safe)
import test_worker_closure_evidence as cew  # noqa: E402  (its staged-script harness)

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _body_stamp  # noqa: E402
import goal_close_risk_tier  # noqa: E402
from gates import goal_duplication  # noqa: E402

WORKER_LOOP = SCRIPTS.parent.parent / ".claude" / "skills" / "worker-loop" / "SKILL.md"
AGENT, SID = "agent-x", "0123abcd4567ef890123abcd4567ef89"
WORKER_ENV = {"BODY_ROLE": "worker", "MIND_AGENT": AGENT, "MIND_SID": SID}
NOTE_LINE = f"Auto-signed: {AGENT} worker Body 0123abcd, hostname {platform.node()}."
FILING_LINE = f"Auto-signed: filed by {AGENT} worker Body on hostname {platform.node()}."


def _as_worker(monkeypatch):
    for k, v in WORKER_ENV.items():
        monkeypatch.setenv(k, v)


# ── 1. the helper ────────────────────────────────────────────────────────────

def test_a_worker_body_gets_lines_naming_this_box(monkeypatch):
    _as_worker(monkeypatch)
    assert _body_stamp.stamp_line() == NOTE_LINE
    assert _body_stamp.filing_line() == FILING_LINE


@pytest.mark.parametrize("change", [
    {"BODY_ROLE": None}, {"BODY_ROLE": "reducer"}, {"BODY_ROLE": "observer"},
    {"MIND_SID": None}, {"MIND_SID": "  "}, {"MIND_AGENT": None},
], ids=["no-role", "reducer", "observer", "no-sid", "blank-sid", "no-agent"])
def test_no_line_for_anyone_but_a_worker_body_with_its_ids(monkeypatch, change):
    _as_worker(monkeypatch)
    assert (_body_stamp.stamp_line(), _body_stamp.filing_line()) == (NOTE_LINE, FILING_LINE), \
        "positive control"
    for k, v in change.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    assert (_body_stamp.stamp_line(), _body_stamp.filing_line()) == (None, None)


def test_a_worker_filing_ends_its_description_with_the_filing_line(monkeypatch):
    _as_worker(monkeypatch)
    raw = json.dumps({"title": "Idea: x", "description": "Case C: only here.  \n",
                      "priority": "HIGH"})
    out = json.loads(_body_stamp.stamp_description(raw))
    assert out["description"] == "Case C: only here.\n\n" + FILING_LINE
    assert (out["title"], out["priority"]) == ("Idea: x", "HIGH")


def test_a_filing_with_no_description_gets_the_line_as_its_description(monkeypatch):
    _as_worker(monkeypatch)
    assert json.loads(_body_stamp.stamp_description('{"title": "t"}'))["description"] == FILING_LINE


@pytest.mark.parametrize("raw", ["not json {", '["a list"]', '{"description": 5}'],
                         ids=["not-json", "not-an-object", "description-not-text"])
def test_a_body_the_filter_cannot_sign_passes_through_byte_identical(monkeypatch, raw):
    _as_worker(monkeypatch)
    signable = '{"description": "d"}'
    assert _body_stamp.stamp_description(signable) != signable, "positive control"
    assert _body_stamp.stamp_description(raw) == raw


def test_a_non_worker_filing_passes_through_byte_identical(monkeypatch):
    raw = '{"title": "t",   "description": "spacing kept \\u00e9"}\n'
    _as_worker(monkeypatch)
    assert _body_stamp.stamp_description(raw) != raw, "positive control"
    monkeypatch.delenv("BODY_ROLE")
    assert _body_stamp.stamp_description(raw) == raw


def test_the_cli_prints_the_line_for_a_worker_and_nothing_for_anyone_else():
    base = {k: v for k, v in os.environ.items() if k not in WORKER_ENV}

    def cli(args, env):
        return subprocess.run([sys.executable, str(SCRIPTS / "_body_stamp.py"), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    worker = cli(["line"], {**base, **WORKER_ENV})
    assert (worker.returncode, worker.stdout) == (0, NOTE_LINE + "\n")
    other = cli(["line"], base)
    assert (other.returncode, other.stdout) == (0, "")
    assert cli(["sign"], base).returncode == 2
    assert cli(["line", "extra"], base).returncode == 2


def test_the_cli_reads_and_writes_utf8_whatever_the_platform_default():
    """PYTHONIOENCODING=cp1252 gives the streams a Windows default. U+00C1 is the
    bytes C3 81, and 0x81 has no cp1252 character, so a cp1252 read fails and the
    filing would go unsigned; a cp1252 write would send C1, which is not UTF-8."""
    env = {**{k: v for k, v in os.environ.items() if k not in WORKER_ENV},
           **WORKER_ENV, "PYTHONIOENCODING": "cp1252"}
    body = json.dumps({"description": "Case Á."}, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPTS / "_body_stamp.py"), "description"],
                          input=body, capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert json.loads(proc.stdout.decode("utf-8"))["description"] == \
        "Case Á.\n\n" + FILING_LINE


# ── 2. the filing line stays out of the scope gates' way ────────────────────

def test_the_filing_line_adds_no_file_path_no_sid_and_no_identifier_but_the_host(monkeypatch):
    """A description is read as scope: the duplication gate takes its title and
    description when a goal has no verification block, and the close-risk tier
    counts its named entities. The old typed instruction already put the host
    there, so the host is the one identifier the line may add."""
    _as_worker(monkeypatch)
    line = _body_stamp.filing_line()
    tokens = re.findall(r"[A-Za-z0-9_.-]+", line)
    structural = {t.lower().strip(".") for t in tokens
                  if goal_duplication._is_structural_identifier(t.lower().strip("."))}
    assert goal_duplication._FILE_PATH_RE.findall(line) == []
    assert structural <= {platform.node().lower()}, structural
    assert [m.group(0) for m in goal_close_risk_tier._ENTITY_RE.finditer(line)] == []
    assert "0123abcd" not in line
    # The control: the note line does carry the sid, so the probe above can see one.
    assert "0123abcd" in _body_stamp.stamp_line()
    assert [m.group(0) for m in goal_close_risk_tier._ENTITY_RE.finditer(
        _body_stamp.stamp_line())] == ["0123abcd"]


# ── 3. goal-field-append.py signs a worker's block ──────────────────────────

def _load_gfa():
    # goal-field-append.py is hyphenated, so it loads by path.
    spec = importlib.util.spec_from_file_location("gfa_body_stamp", SCRIPTS / "goal-field-append.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GFA = _load_gfa()


class _Res:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _fake_store(monkeypatch, field="progress_note"):
    """An empty goal field behind GFA's subprocess layer. Returns the values written."""
    store = {"goal_id": "g-1", "priority": "MEDIUM", field: ""}
    writes = []

    def fake_run(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "aspirations-query.sh" in joined:
            return _Res(stdout=json.dumps([dict(store)]))
        if "aspirations-update-goal.sh" in joined:
            writes.append(kw["input"])
            store[field] = kw["input"]
            return _Res(stdout=json.dumps(dict(store)))
        raise AssertionError(f"unexpected subprocess: {joined}")

    monkeypatch.setattr(GFA, "_run", fake_run)
    return writes


@pytest.mark.parametrize("field", ["progress_note", "outcome_note"])
def test_a_worker_block_ends_with_its_signature_above_the_sentinel(monkeypatch, capsys, field):
    _as_worker(monkeypatch)
    writes = _fake_store(monkeypatch, field)
    assert GFA.main(["g-1", field, "m1", "measured the thing"]) == GFA.RC_OK
    assert writes == ["measured the thing\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m1")]
    # Inside the block it signs, so a rotation moves the two together.
    assert GFA.split_blocks(writes[0]) == [writes[0]]


def test_anyone_elses_block_is_stored_exactly_as_passed(monkeypatch, capsys):
    _as_worker(monkeypatch)
    signed = _fake_store(monkeypatch)
    GFA.main(["g-1", "progress_note", "m1", "same text"])
    assert NOTE_LINE in signed[0], "positive control"
    monkeypatch.delenv("BODY_ROLE")
    plain = _fake_store(monkeypatch)
    GFA.main(["g-1", "progress_note", "m1", "same text"])
    assert plain == ["same text\n" + GFA.sentinel_for("m1")]


def test_a_worker_description_append_is_stored_exactly_as_passed(monkeypatch, capsys):
    """A description is read as scope, where the note line's sid would count as a
    named entity toward the close-risk tier (_body_stamp.py)."""
    _as_worker(monkeypatch)
    noted = _fake_store(monkeypatch, "progress_note")
    GFA.main(["g-1", "progress_note", "m1", "same text"])
    assert NOTE_LINE in noted[0], "positive control"
    described = _fake_store(monkeypatch, "description")
    GFA.main(["g-1", "description", "m1", "same text"])
    assert described == ["same text\n" + GFA.sentinel_for("m1")]


# ── 4. closure-evidence-write.sh signs a worker's narrative ─────────────────

NARRATIVE = "OUTCOME 1: MET. measured.\n\nsecond paragraph"


def _close(tmp_path, worker, with_helper=True, **record):
    """Run the staged producer once, as a worker or not. Returns (proc, the value written)."""
    tmp_path.mkdir()
    script = cew._stage(tmp_path)
    if with_helper:
        (script.parent / "_body_stamp.py").write_text(
            (SCRIPTS / "_body_stamp.py").read_text(encoding="utf-8"), encoding="utf-8")
    note = tmp_path / "n.txt"
    note.write_text(NARRATIVE, encoding="utf-8")
    env = cew._env(tmp_path, **record)
    if worker:
        env.update(WORKER_ENV)
    proc = subprocess.run(
        [BASH, script.as_posix(), "--goal", cew.GID, "--source", "world",
         "--summary-file", str(note), "--prefix", "[worker-loop] close:"],
        capture_output=True, text=True, env=env, timeout=120)
    writes = cew._note_writes(tmp_path)
    assert len(writes) <= 1, writes
    return proc, (writes[0][writes[0].index("outcome_note") + 1] if writes else None)


def test_a_worker_narrative_is_signed_and_anyone_elses_is_byte_exact(tmp_path):
    proc, value = _close(tmp_path / "worker", worker=True)
    assert proc.returncode == 0, proc.stderr
    assert value == NARRATIVE + "\n\n" + NOTE_LINE
    proc, value = _close(tmp_path / "other", worker=False)
    assert proc.returncode == 0, proc.stderr
    assert value == NARRATIVE


def test_the_signature_sits_before_the_auto_mark_and_a_retry_stays_idempotent(tmp_path):
    prior = cew._marked("the prior occurrence's note", ach=1)
    proc, value = _close(tmp_path / "first", worker=True,
                         existing_note=prior, recurring=True, achieved=2)
    assert proc.returncode == 0, proc.stderr
    assert value.index(NARRATIVE) < value.index(NOTE_LINE) < value.index(cew.CE_AUTO_MARK), value
    # The retried verify finds the bare narrative inside the signed note.
    proc, again = _close(tmp_path / "retry", worker=True,
                         existing_note=value, recurring=True, achieved=2)
    assert proc.returncode == 0, proc.stderr
    assert again is None, "a retry re-wrote the note it had already written"
    assert "already carries THIS summary" in proc.stdout


def test_the_deferral_path_does_not_sign_a_note_the_body_did_not_write(tmp_path):
    hand_written = "a note written by hand for this occurrence"
    proc, value = _close(tmp_path / "defer", worker=True,
                         existing_note=hand_written, recurring=True, achieved=2)
    assert proc.returncode == 0, proc.stderr
    assert value.startswith(hand_written) and cew.CE_DEFER_MARK in value, \
        "the deferral path was not reached, so its negative below proves nothing"
    assert "Auto-signed" not in value


def test_a_helper_failure_is_said_aloud_and_the_narrative_still_lands(tmp_path):
    proc, value = _close(tmp_path / "nohelper", worker=True, with_helper=False)
    assert proc.returncode == 0, proc.stderr
    assert "could not sign this worker narrative" in proc.stderr
    assert value == NARRATIVE


# ── 5. aspirations-add-goal.sh signs a worker's filing ──────────────────────

# The daemon is replaced at the one seam the wrapper calls: rt_call records the
# body it would POST and answers the way the add-goal endpoint does.
STUB_RUNTIME = """
rt_python_launcher() { printf '%s\\n' "$PY_REAL"; }
rt_call() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --body-string) printf '%s' "$2" > "$BODY_SINK"; shift 2;;
            *) shift;;
        esac
    done
    printf '%s' '{"goal": {"id": "g-1-01"}}'
}
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "no daemon" >&2; exit 1; }
"""


def _file(tmp_path, body, worker, with_helper=True):
    """File `body` through the staged wrapper. Returns (proc, the body it POSTed)."""
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)
    for name in ("aspirations-add-goal.sh", "_argv_strict.sh") + (
            ("_body_stamp.py",) if with_helper else ()):
        (core / name).write_text((SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")
    (core / "_runtime.sh").write_text(STUB_RUNTIME, encoding="utf-8")
    sink = tmp_path / "body.json"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "PY_REAL": sys.executable,
           "BODY_SINK": str(sink)}
    if worker:
        env.update(WORKER_ENV)
    proc = subprocess.run([BASH, (core / "aspirations-add-goal.sh").as_posix(), "asp-1"],
                          input=body, capture_output=True, text=True, env=env, timeout=60)
    return proc, (sink.read_text(encoding="utf-8") if sink.exists() else None)


FILING = json.dumps({"title": "Idea: x", "description": "Case C: machine-local.",
                     "priority": "MEDIUM", "participants": ["agent"]})


def test_a_worker_filing_is_signed_and_anyone_elses_is_sent_as_written(tmp_path):
    proc, sent = _file(tmp_path / "worker", FILING, worker=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(sent)["description"] == "Case C: machine-local.\n\n" + FILING_LINE
    proc, sent = _file(tmp_path / "other", FILING, worker=False)
    assert proc.returncode == 0, proc.stderr
    assert sent == FILING


def test_a_filing_helper_failure_is_said_aloud_and_the_goal_still_files(tmp_path):
    proc, sent = _file(tmp_path / "nohelper", FILING, worker=True, with_helper=False)
    assert proc.returncode == 0, proc.stderr
    assert "could not sign this worker filing" in proc.stderr
    assert sent == FILING


# ── 6. worker-loop no longer asks a Body for its host ───────────────────────

def test_worker_loop_tells_a_body_never_to_type_its_host_or_sid():
    text = WORKER_LOOP.read_text(encoding="utf-8")
    assert "worker Body on <hostname>" not in text
    assert re.search(r"Never\s+type your host or sid in a note or filing;\s+scripts add them"
                     r"\s+\(g-375-111\)", text)
