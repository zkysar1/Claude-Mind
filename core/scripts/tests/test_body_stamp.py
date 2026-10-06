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
     goal-field-append.py stores a worker's description append unsigned. A text
     whose last line already is this Body's line is not signed again (g-375-115).
     Copies of this Body's line above the end are dropped by both delegated
     writers; goal-field-append.py also drops its own sentinel from a worker's
     text and refuses any other boundary-shaped line (g-375-130).
     Every negative carries its positive control in the same test, so a writer
     that can never sign cannot pass it (guard-4166).
  4. closure-evidence-write.sh leaves the deferral path's preserved note unsigned,
     and a helper failure in either bash writer is said aloud, never silent.
  5. aspirations-update-goal.sh, which REPLACES a field, signs a worker's
     progress_note or outcome_note value and a companion outcome_note, and
     nothing else (g-375-115). A value already ending with this Body's line is
     not signed again. goal-field-append.py and closure-evidence-write.sh pass
     MIND_NOTE_SIGNED=1, so a note written through them reaches the REAL
     wrapper and is stored with exactly one signature.
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


def test_a_worker_block_already_ending_with_its_line_is_not_signed_again(monkeypatch, capsys):
    """Measured 2026-10-04: a Body that ended its text with its own line got two."""
    _as_worker(monkeypatch)
    mine = "measured the thing\n" + NOTE_LINE
    writes = _fake_store(monkeypatch, "outcome_note")
    assert GFA.main(["g-1", "outcome_note", "m1", mine + "\n"]) == GFA.RC_OK
    assert writes == [mine + "\n" + GFA.sentinel_for("m1")]
    # Positive control: text ending with ANOTHER Body's line gets this Body's after it.
    other = "measured the thing\nAuto-signed: agent-x worker Body ffffffff, hostname box-9."
    writes = _fake_store(monkeypatch, "outcome_note")
    assert GFA.main(["g-1", "outcome_note", "m1", other]) == GFA.RC_OK
    assert writes == [other + "\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m1")]


def test_a_worker_text_copying_a_signed_ending_is_stored_with_one_line_and_one_sentinel(
        monkeypatch, capsys):
    """, measured 2026-10-05: 17 of the 18 boundary-shaped lines in worker texts
    sent here were the sentinel of the very call that sent them, and one text carried
    the Body's own line above its end."""
    _as_worker(monkeypatch)
    copied = "measured the thing\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m1")
    writes = _fake_store(monkeypatch, "outcome_note")
    assert GFA.main(["g-1", "outcome_note", "m1", copied]) == GFA.RC_OK
    assert writes == ["measured the thing\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m1")]
    assert GFA.split_blocks(writes[0]) == [writes[0]]
    # The Body's line above the end is dropped, and the line ends the block once.
    above = NOTE_LINE + "\nmeasured the thing\n" + NOTE_LINE + "\nand more"
    writes = _fake_store(monkeypatch, "outcome_note")
    assert GFA.main(["g-1", "outcome_note", "m2", above]) == GFA.RC_OK
    assert writes == ["measured the thing\nand more\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m2")]
    # A text that was nothing but the writer's own sentinel is empty, and refused.
    writes = _fake_store(monkeypatch, "outcome_note")
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "outcome_note", "m3", GFA.sentinel_for("m3")])
    assert exc.value.code == GFA.RC_VALUE_SHAPE and writes == []
    # A blank line the dropped sentinel leaves below the Body's line, one of spaces or a
    # CRLF remainder, does not hide the line: it still ends the text and is not added
    # again, so the text is stored exactly as it was sent.
    for nl, gap in (("\n", "   "), ("\r\n", "")):
        tail = "measured the thing" + nl + NOTE_LINE + nl + gap + nl + GFA.sentinel_for("m5")
        writes = _fake_store(monkeypatch, "outcome_note")
        assert GFA.main(["g-1", "outcome_note", "m5", tail]) == GFA.RC_OK
        assert writes == [tail], repr(nl)
        assert GFA.split_blocks(writes[0]) == [writes[0]]
    # Positive control: another Body's line in the text is content and stays.
    other = "Auto-signed: agent-x worker Body ffffffff, hostname box-9."
    writes = _fake_store(monkeypatch, "outcome_note")
    assert GFA.main(["g-1", "outcome_note", "m4", "quoted\n" + other + "\nmine"]) == GFA.RC_OK
    assert writes == ["quoted\n" + other + "\nmine\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m4")]


def test_a_worker_text_with_another_boundary_line_is_refused_and_nothing_is_written(
        monkeypatch, capsys):
    """: 3 of 20 typed sentinels in 8 days carried another marker. Stored, such a
    line cuts its block in two, and under the substring idempotency test it would
    silence a later append that used its marker."""
    _as_worker(monkeypatch)
    writes = _fake_store(monkeypatch, "progress_note")
    with pytest.raises(SystemExit) as exc:
        GFA.main(["g-1", "progress_note", "m1", "text\n[appended:other]\nmore"])
    assert exc.value.code == GFA.RC_VALUE_SHAPE
    assert writes == []
    assert "line 2 of the text reads as a block boundary" in capsys.readouterr().err
    # Positive control: the same line indented is content, not a boundary, and is stored.
    writes = _fake_store(monkeypatch, "progress_note")
    assert GFA.main(["g-1", "progress_note", "m1", "text\n  [appended:other]\nmore"]) == GFA.RC_OK
    assert writes == ["text\n  [appended:other]\nmore\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m1")]
    # Scope control: anyone else's text is stored exactly as passed, the line included.
    monkeypatch.delenv("BODY_ROLE")
    writes = _fake_store(monkeypatch, "progress_note")
    assert GFA.main(["g-1", "progress_note", "m1", "text\n[appended:other]\nmore"]) == GFA.RC_OK
    assert writes == ["text\n[appended:other]\nmore\n" + GFA.sentinel_for("m1")]


# ── 4. closure-evidence-write.sh signs a worker's narrative ─────────────────

NARRATIVE = "OUTCOME 1: MET. measured.\n\nsecond paragraph"


def _close(tmp_path, worker, with_helper=True, narrative=NARRATIVE, **record):
    """Run the staged producer once, as a worker or not. Returns (proc, the value written)."""
    tmp_path.mkdir()
    script = cew._stage(tmp_path)
    if with_helper:
        (script.parent / "_body_stamp.py").write_text(
            (SCRIPTS / "_body_stamp.py").read_text(encoding="utf-8"), encoding="utf-8")
    note = tmp_path / "n.txt"
    note.write_text(narrative, encoding="utf-8")
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


def test_a_narrative_already_ending_with_its_line_is_not_signed_again(tmp_path):
    """Measured 2026-10-04: a Body that ended its narrative with its own line got two."""
    mine = NARRATIVE + "\n\n" + NOTE_LINE
    proc, value = _close(tmp_path / "mine", worker=True, narrative=mine + "  \n")
    assert proc.returncode == 0, proc.stderr
    assert value == mine + "  "
    # Positive control: a narrative ending with ANOTHER Body's line gets this Body's after it.
    other = NARRATIVE + "\n\nAuto-signed: agent-x worker Body ffffffff, hostname box-9."
    proc, value = _close(tmp_path / "other", worker=True, narrative=other)
    assert proc.returncode == 0, proc.stderr
    assert value == other + "\n\n" + NOTE_LINE


def test_a_narrative_carrying_its_line_above_the_end_is_stored_with_one(tmp_path):
    """, measured 2026-10-05: a Body copied a signed ending, its own line then a
    sentinel-shaped line, into the text it sent here. This script writes a note, not a
    block, so the sentinel-shaped line is the Body's to keep; the copied line goes."""
    above = "OUTCOME 1: MET. measured.\n" + NOTE_LINE + "\n[appended:m]\n\nsecond paragraph"
    proc, value = _close(tmp_path / "above", worker=True, narrative=above)
    assert proc.returncode == 0, proc.stderr
    assert value == "OUTCOME 1: MET. measured.\n[appended:m]\n\nsecond paragraph\n\n" + NOTE_LINE
    # Positive control: anyone else's narrative carrying the same line stays byte-exact.
    proc, value = _close(tmp_path / "other", worker=False, narrative=above)
    assert proc.returncode == 0, proc.stderr
    assert value == above


def test_a_retry_of_a_narrative_that_lost_a_copied_line_stays_idempotent(tmp_path):
    """The drop runs BEFORE the idempotency compare, so a retried verify still finds the
    summary it wrote inside the stored note (g-375-130). Dropped in the signing block
    instead, the retry would supersede its own note."""
    above = NARRATIVE + "\n" + NOTE_LINE + "\nthird paragraph"
    prior = cew._marked("the prior occurrence's note", ach=1)
    proc, value = _close(tmp_path / "first", worker=True, narrative=above,
                         existing_note=prior, recurring=True, achieved=2)
    assert proc.returncode == 0, proc.stderr
    assert value.count(NOTE_LINE) == 1, value
    proc, again = _close(tmp_path / "retry", worker=True, narrative=above,
                         existing_note=value, recurring=True, achieved=2)
    assert proc.returncode == 0, proc.stderr
    assert again is None, "a retry re-wrote the note it had already written"
    assert "already carries THIS summary" in proc.stdout


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


# ── 7. aspirations-update-goal.sh signs a worker's note () ─────────
#
# A worker Body sent 35 of its 100 note writes through the field-replacing
# wrapper in the 28 hours after the writers above began signing, and none was
# signed. The wrapper now signs a progress_note or outcome_note value, and an
# outcome_note riding a status write, unless the caller passes
# MIND_NOTE_SIGNED=1, which goal-field-append.py and closure-evidence-write.sh
# do because their values are already signed or deliberately unsigned. These
# tests run the REAL wrapper; only the daemon seam is stubbed.

STUB_UPDATE_RUNTIME = """
rt_python_launcher() { printf '%s\\n' "$PY_REAL"; }
rt_url_encode() { printf '%s' "$1"; }
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
UPDATE_FILES = ("aspirations-update-goal.sh", "_goal-arg-normalize.sh", "_argv_strict.sh")
STDIN_REPLACE = ["--source", "world", "--value-stdin", "--override-narrative-replace", "whole note"]


def _stage_update(core, with_helper=True):
    """The real wrapper and the files it sources, beside a stub runtime, in `core`."""
    core.mkdir(parents=True, exist_ok=True)
    for name in UPDATE_FILES + (("_body_stamp.py",) if with_helper else ()):
        (core / name).write_text((SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")
    (core / "_runtime.sh").write_text(STUB_UPDATE_RUNTIME, encoding="utf-8")
    return core / "aspirations-update-goal.sh"


def _sent(sink):
    return json.loads(sink.read_text(encoding="utf-8")) if sink.exists() else None


def _update(tmp_path, args, env_extra, stdin=None, with_helper=True):
    """One call of the staged wrapper. Returns (proc, the value it POSTed, decoded)."""
    script = _stage_update(tmp_path / "core" / "scripts", with_helper)
    sink = tmp_path / "body.json"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "PY_REAL": sys.executable,
           "BODY_SINK": str(sink), **env_extra}
    proc = subprocess.run([BASH, script.as_posix()] + args, input=stdin, capture_output=True,
                          text=True, env=env, timeout=60)
    return proc, _sent(sink)


@pytest.mark.parametrize("field", ["progress_note", "outcome_note"])
def test_a_worker_note_sent_through_the_wrapper_ends_with_its_signature(tmp_path, field):
    proc, sent = _update(tmp_path / "arg", ["--source", "world", "g-1-01", field, "measured it"],
                         WORKER_ENV)
    assert proc.returncode == 0, proc.stderr
    assert sent == "measured it\n\n" + NOTE_LINE
    # The shape the Bodies use most: a whole note on stdin, with the override.
    proc, sent = _update(tmp_path / "stdin", STDIN_REPLACE + ["g-1-01", field], WORKER_ENV,
                         stdin=NARRATIVE + "\n")
    assert proc.returncode == 0, proc.stderr
    assert sent == NARRATIVE + "\n\n" + NOTE_LINE


def test_anyone_elses_note_and_a_signed_writers_note_are_sent_as_written(tmp_path):
    args = ["--source", "world", "g-1-01", "progress_note", "same text"]
    _proc, signed = _update(tmp_path / "worker", args, WORKER_ENV)
    assert signed == "same text\n\n" + NOTE_LINE, "positive control"
    proc, plain = _update(tmp_path / "other", args, {})
    assert proc.returncode == 0, proc.stderr
    assert plain == "same text"
    proc, delegated = _update(tmp_path / "delegated", args, {**WORKER_ENV, "MIND_NOTE_SIGNED": "1"})
    assert proc.returncode == 0, proc.stderr
    assert delegated == "same text"


def test_a_note_riding_a_status_write_is_signed_and_the_status_is_not(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    note = tmp_path / "note.txt"
    note.write_text(NARRATIVE, encoding="utf-8")
    args = ["--source", "world", "g-1-01", "status", "completed", "--outcome-note-file", str(note)]
    proc, sent = _update(tmp_path / "worker", args, WORKER_ENV)
    assert proc.returncode == 0, proc.stderr
    assert sent == {"value": "completed", "outcome_note": NARRATIVE + "\n\n" + NOTE_LINE}
    proc, sent = _update(tmp_path / "other", args, {})
    assert proc.returncode == 0, proc.stderr
    assert sent == {"value": "completed", "outcome_note": NARRATIVE}


def test_a_note_that_already_ends_with_its_line_is_not_signed_again(tmp_path):
    mine = NARRATIVE + "\n\n" + NOTE_LINE + "\n"
    proc, sent = _update(tmp_path / "mine", STDIN_REPLACE + ["g-1-01", "outcome_note"], WORKER_ENV,
                         stdin=mine)
    assert proc.returncode == 0, proc.stderr
    assert sent == mine
    # Positive control: a note ending with ANOTHER Body's line is this Body's
    # write now, so it gets this Body's line after that one.
    other = NARRATIVE + "\n\nAuto-signed: agent-x worker Body ffffffff, hostname box-9."
    proc, sent = _update(tmp_path / "other", STDIN_REPLACE + ["g-1-01", "outcome_note"], WORKER_ENV,
                         stdin=other)
    assert proc.returncode == 0, proc.stderr
    assert sent == other + "\n\n" + NOTE_LINE


@pytest.mark.parametrize("field,value,expected", [
    ("progress_note", '{"a": 1}', {"a": 1}),
    ("progress_note", "42", 42),
    ("outcome_note", "null", None),
    ("outcome_note", "   ", "   "),
    ("description", "a scope sentence", "a scope sentence"),
    ("defer_reason", "precondition_unmet: x", "precondition_unmet: x"),
    ("outcome_note", "plain text", "plain text\n\n" + NOTE_LINE),   # positive control
], ids=["json", "number", "literal", "blank", "description", "defer_reason", "text"])
def test_only_a_text_note_is_signed(tmp_path, field, value, expected):
    proc, sent = _update(tmp_path, ["--source", "world", "g-1-01", field, value], WORKER_ENV)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "body.json").exists(), "nothing was POSTed, so None below would prove nothing"
    assert sent == expected


def test_a_signing_failure_is_said_aloud_and_the_note_is_still_sent(tmp_path):
    proc, sent = _update(tmp_path, ["--source", "world", "g-1-01", "outcome_note", "the note"],
                         WORKER_ENV, with_helper=False)
    assert proc.returncode == 0, proc.stderr
    assert "could not sign this worker note" in proc.stderr
    assert sent == "the note"


def test_a_worker_with_no_sid_gets_no_line_and_no_warning(tmp_path):
    """No line beats a wrong one (_body_stamp.py). A worker whose MIND_SID is
    missing is not a failure to sign, so its note goes as written, with no warning."""
    args = ["--source", "world", "g-1-01", "outcome_note", "the note"]
    no_sid = {k: v for k, v in WORKER_ENV.items() if k != "MIND_SID"}
    proc, sent = _update(tmp_path / "nosid", args, no_sid)
    assert proc.returncode == 0, proc.stderr
    assert "could not sign" not in proc.stderr
    assert sent == "the note"
    _proc, sent = _update(tmp_path / "sid", args, WORKER_ENV)
    assert sent == "the note\n\n" + NOTE_LINE, "positive control"


def test_a_goal_field_append_block_reaches_the_store_with_one_signature(tmp_path, monkeypatch):
    """goal-field-append.py writes its signed block through the REAL wrapper, which
    must not sign it again: the block ends with its sentinel, so without the marker
    the wrapper would add a second line after it."""
    _as_worker(monkeypatch)
    script = _stage_update(tmp_path / "core" / "scripts")
    sink = tmp_path / "body.json"
    monkeypatch.setenv("PY_REAL", sys.executable)
    monkeypatch.setenv("BODY_SINK", str(sink))
    store = {"goal_id": "g-1", "priority": "MEDIUM", "progress_note": ""}

    def fake_run(argv, **kw):
        joined = " ".join(str(a) for a in argv)
        if "aspirations-query.sh" in joined:
            return _Res(stdout=json.dumps([dict(store)]))
        if "aspirations-update-goal.sh" in joined:
            argv = [script.as_posix() if str(a).endswith("aspirations-update-goal.sh") else a
                    for a in argv]
            res = subprocess.run(argv, input=kw["input"], capture_output=True, text=True,
                                 env=kw.get("env"), timeout=60)
            store["progress_note"] = _sent(sink)
            return _Res(stdout=res.stdout, returncode=res.returncode, stderr=res.stderr)
        raise AssertionError(f"unexpected subprocess: {joined}")

    monkeypatch.setattr(GFA, "_run", fake_run)
    rc = GFA.main(["g-1", "progress_note", "m1", "measured the thing"])
    assert store["progress_note"] == "measured the thing\n" + NOTE_LINE + "\n" + GFA.sentinel_for("m1")
    assert rc == GFA.RC_OK
    # Positive control: this staged wrapper does sign a worker's direct write.
    proc = subprocess.run([BASH, script.as_posix(), "g-1", "progress_note", "x"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert _sent(sink) == "x\n\n" + NOTE_LINE


def test_a_note_rotation_reaches_the_store_unsigned(tmp_path, monkeypatch):
    """A rotation rewrites the field with the oldest blocks cut and a notice added.
    That text is not a note the worker wrote, so the wrapper must not sign it."""
    _as_worker(monkeypatch)
    script = _stage_update(tmp_path / "core" / "scripts")
    sink = tmp_path / "body.json"
    monkeypatch.setenv("PY_REAL", sys.executable)
    monkeypatch.setenv("BODY_SINK", str(sink))
    monkeypatch.setattr(GFA, "_sink_path", lambda g, f: tmp_path / "archive.md")
    # The block shape compose() writes, as test_goal_note_rotation.py builds it.
    pre = "\n\n".join(f"BLOCK{i:04d}-HEAD {'x' * 4000} body{i}\n[appended:m{i}]" for i in range(20))
    store = {"progress_note": pre}
    monkeypatch.setattr(GFA, "read_goal", lambda goal_id, source: dict(store))

    def fake_run(argv, **kw):
        argv = [script.as_posix() if str(a).endswith("aspirations-update-goal.sh") else a
                for a in argv]
        res = subprocess.run(argv, input=kw.get("input"), capture_output=True, text=True,
                             env=kw.get("env"), timeout=60)
        store["progress_note"] = _sent(sink)
        return _Res(stdout=res.stdout, returncode=res.returncode, stderr=res.stderr)

    monkeypatch.setattr(GFA, "_run", fake_run)
    out = GFA.rotate_oversize("g-1", "progress_note", "world", pre)
    assert GFA.ROTATE_NOTICE_HEAD in out, "the rotation did not run, so the negative below proves nothing"
    assert store["progress_note"] == out
    assert "Auto-signed" not in out
    # Positive control: this staged wrapper does sign a worker's direct write.
    proc = subprocess.run([BASH, script.as_posix(), "g-1", "progress_note", "x"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert _sent(sink) == "x\n\n" + NOTE_LINE


def _close_through_wrapper(tmp_path, **record):
    """closure-evidence-write.sh as a worker, writing through the REAL wrapper.
    Returns (proc, the outcome_note value the wrapper POSTed, its staged path)."""
    tmp_path.mkdir()
    script = cew._stage(tmp_path)
    wrapper = _stage_update(script.parent)        # replaces the harness's stub wrapper
    note = tmp_path / "n.txt"
    note.write_text(NARRATIVE, encoding="utf-8")
    sink = tmp_path / "body.json"
    env = {**cew._env(tmp_path, **record), **WORKER_ENV, "BODY_SINK": str(sink)}
    proc = subprocess.run(
        [BASH, script.as_posix(), "--goal", cew.GID, "--source", "world",
         "--summary-file", str(note), "--prefix", "[worker-loop] close:"],
        capture_output=True, text=True, env=env, timeout=120)
    return proc, _sent(sink), wrapper, env


def test_a_closure_narrative_reaches_the_store_with_one_signature(tmp_path):
    """The recurring supersede path ends the note with the auto-mark, after the
    signature, so without the marker the wrapper would sign again after the mark."""
    prior = cew._marked("the prior occurrence's note", ach=1)
    proc, value, wrapper, env = _close_through_wrapper(
        tmp_path / "close", existing_note=prior, recurring=True, achieved=2)
    assert proc.returncode == 0, proc.stderr
    assert value.count("Auto-signed") == 1, value
    assert value.index(NARRATIVE) < value.index(NOTE_LINE) < value.index(cew.CE_AUTO_MARK)
    # Positive control: the same staged wrapper signs a worker's direct write.
    direct = subprocess.run([BASH, wrapper.as_posix(), cew.GID, "outcome_note", "x"],
                            capture_output=True, text=True, env=env, timeout=60)
    assert direct.returncode == 0, direct.stderr
    assert _sent(Path(env["BODY_SINK"])) == "x\n\n" + NOTE_LINE


def test_the_deferral_path_stays_unsigned_through_the_wrapper(tmp_path):
    hand_written = "a note written by hand for this occurrence"
    proc, value, _wrapper, _env = _close_through_wrapper(
        tmp_path / "defer", existing_note=hand_written, recurring=True, achieved=2)
    assert proc.returncode == 0, proc.stderr
    assert value.startswith(hand_written) and cew.CE_DEFER_MARK in value, \
        "the deferral path was not reached, so its negative below proves nothing"
    assert "Auto-signed" not in value
