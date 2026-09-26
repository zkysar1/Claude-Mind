"""test_stop_handoff_check.py — graceful-stop D7 handoff check ().

D7 must not flip agent-mode until THIS stop has written handoff.yaml. The
fast consolidation path's only handoff writer (digest Step 9) sits past the
200-line limit a vessel mind read the digest with, and D7 still printed
"handoff saved" (3/3 vessel graceful endings had no handoff.yaml).

Lanes:
  1. absent -> rc 1, Step 9 printed verbatim (and nothing past it)
  2. fresh (written after the GS-0 stamp) -> rc 0, "Handoff saved"
  3. stale (older than the GS-0 stamp) -> rc 1
  4. --proceed-without-handoff "<why>" -> rc 0, reason printed AND logged
  5. resume: GS-0 re-writes the checkpoint but keeps stop_started_at, so a
     handoff from before the interruption still passes
  6. no checkpoint -> stop-target-mode mtime is the reference (the measured
     vessel run skipped GS-0's write)
  7. no reference at all -> refused, with a re-stamp step
  8. an empty file is not a handoff
  9. Step 9 extraction ignores `## ` lines inside a fence
 10. the real digest's Step 9 extracts, ends before Step 9.5
 11. vessel call shape: MIND_AGENT prefix, MIND_AGENT unset, cwd = repo root
 12. D7 wiring: the check leads the mode flip on the same && chain, and the
     fixed D7 text no longer claims "handoff saved"
 13. leftover checkpoint (g-373-141): an earlier stop missed D7.1, so the new
     stop's GS-0 kept the old stamp; the fresh stop-target-mode is the
     reference and the earlier stop's handoff is refused
 14. resume with stop-target-mode present: the request predates GS-0, so the
     checkpoint stamp still wins and a pre-interruption handoff passes
 15. a non-UTF-8 handoff is refused as invalid, not failed open
 16. --step D4.1: pre-flush wording (no D7 / D7.1 text) and telemetry caller
 17. D4.1 wiring: the early check sits after D4, before D6.62's commit and
     D6.7's flush
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
REPO = CORE_SCRIPTS.parents[1]
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bash_helpers import BASH  # noqa: E402
import stop_checkpoint as sc  # noqa: E402
import stop_handoff_check as shc  # noqa: E402

HANDOFF = "session_number: 7\nnext_focus: finish g-1\nfirst_action:\n  goal_id: g-1\n"


def _stamp_stop(session: Path, seconds_ago: int = 60) -> float:
    """The production GS-0 write, then backdate stop_started_at."""
    rec = sc.write_checkpoint(session, "assistant")
    start = time.time() - seconds_ago
    rec["stop_started_at"] = shc._iso(start)
    (session / sc.CHECKPOINT_NAME).write_text(json.dumps(rec), encoding="utf-8")
    return start


def _run(session: Path, *extra: str, capsys):
    rc = shc.main(["--session-dir", str(session), *extra])
    return rc, capsys.readouterr().out


def test_absent_refuses_and_prints_step9(tmp_path, capsys):
    _stamp_stop(tmp_path)
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 1
    assert "STOP NOT FINISHED" in out and "does not exist" in out
    assert "## Step 9: Continuation Handoff" in out
    assert "Write agents/<agent>/session/handoff.yaml:" in out
    assert "## Step 9.5" not in out
    assert "Handoff saved" not in out


def test_fresh_handoff_passes(tmp_path, capsys):
    _stamp_stop(tmp_path)
    (tmp_path / "handoff.yaml").write_text(HANDOFF, encoding="utf-8")
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 0
    assert out.startswith("Handoff saved")
    assert "(stop-checkpoint)" in out


def test_stale_handoff_refuses(tmp_path, capsys):
    start = _stamp_stop(tmp_path)
    h = tmp_path / "handoff.yaml"
    h.write_text(HANDOFF, encoding="utf-8")
    os.utime(h, (start - 3600, start - 3600))
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 1
    assert "BEFORE this stop began" in out
    assert "## Step 9: Continuation Handoff" in out


def test_override_passes_and_logs_reason(tmp_path, capsys, monkeypatch):
    import _gate_log
    calls = []
    monkeypatch.setattr(_gate_log, "log", lambda *a, **k: calls.append((a, k)))
    _stamp_stop(tmp_path)
    why = "consolidation crashed twice; handoff builder unavailable"
    rc, out = _run(tmp_path, "--proceed-without-handoff", why, capsys=capsys)
    assert rc == 0
    assert "Handoff NOT saved" in out and why in out
    assert "Handoff saved" not in out
    (args, kwargs), = calls
    assert args == ("stop-handoff-check", "override")
    assert kwargs["override_reason"] == why
    assert kwargs["trigger_matched"] == "absent"


def test_every_decision_is_logged(tmp_path, capsys, monkeypatch):
    import _gate_log
    calls = []
    monkeypatch.setattr(_gate_log, "log", lambda *a, **k: calls.append(a))
    _stamp_stop(tmp_path)
    _run(tmp_path, capsys=capsys)
    (tmp_path / "handoff.yaml").write_text(HANDOFF, encoding="utf-8")
    _run(tmp_path, capsys=capsys)
    assert calls == [("stop-handoff-check", "block"), ("stop-handoff-check", "pass")]


def test_resume_keeps_the_original_stop_start(tmp_path, capsys):
    _stamp_stop(tmp_path, seconds_ago=600)
    h = tmp_path / "handoff.yaml"
    h.write_text(HANDOFF, encoding="utf-8")
    written = time.time() - 300  # handoff landed before the interruption
    os.utime(h, (written, written))
    rec = sc.write_checkpoint(tmp_path, "assistant")  # --resume re-entry: file is new
    assert rec["resume_count"] == 1
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 0, out


def test_target_mode_is_the_fallback_reference(tmp_path, capsys):
    tm = tmp_path / "stop-target-mode"
    tm.write_text("assistant\n", encoding="utf-8")
    requested = time.time() - 120
    os.utime(tm, (requested, requested))
    h = tmp_path / "handoff.yaml"
    h.write_text(HANDOFF, encoding="utf-8")
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 0 and "(stop-target-mode)" in out
    os.utime(h, (requested - 60, requested - 60))
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 1 and "BEFORE this stop began" in out


def test_no_reference_refuses_with_restamp(tmp_path, capsys):
    (tmp_path / "handoff.yaml").write_text(HANDOFF, encoding="utf-8")
    rc, out = _run(tmp_path, "--agent", "alpha", capsys=capsys)
    assert rc == 1
    assert "cannot be dated" in out
    assert "1. Re-stamp the stop start: MIND_AGENT=alpha" in out
    assert "3. Re-run the D7 command unchanged." in out


def test_empty_handoff_is_not_a_handoff(tmp_path, capsys):
    _stamp_stop(tmp_path)
    (tmp_path / "handoff.yaml").write_text("", encoding="utf-8")
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 1 and "empty or not a YAML mapping" in out


def test_internal_crash_fails_open(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(shc, "inspect_handoff",
                        lambda _d: (_ for _ in ()).throw(RuntimeError("boom")))
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 0
    assert out.startswith("Handoff NOT VERIFIED") and "RuntimeError: boom" in out
    assert "Handoff saved" not in out


def test_step9_extraction_skips_headers_inside_fences(tmp_path):
    d = tmp_path / "digest.md"
    d.write_text("## Step 8\nx\n## Step 9: Continuation Handoff\n```\n## not a header\n"
                 "write it\n```\ntail\n## Step 9.5: Next\ny\n", encoding="utf-8")
    got = shc.extract_step9(d)
    assert got.splitlines()[0] == "## Step 9: Continuation Handoff"
    assert "## not a header" in got and "tail" in got
    assert "Step 9.5" not in got and "## Step 8" not in got


def test_real_digest_step9_extracts():
    got = shc.extract_step9(shc.DIGEST)
    assert got is not None
    assert got.startswith("## Step 9: Continuation Handoff")
    assert "handoff.yaml" in got
    assert "## Step 9.5" not in got


def _shell(env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("MIND_AGENT", "MIND_AGENT")}
    env.update(STORAGE_BACKEND="local", **env_extra)
    return subprocess.run([BASH, "core/scripts/stop-handoff-check.sh"], cwd=REPO,
                          env=env, capture_output=True, text=True, timeout=120)


def test_vessel_call_shape_resolves_mind_agent():
    # A name no box provisions: the refusal path only READS, so nothing is created.
    agent = "zz-handoff-probe-nonexistent"
    assert not (REPO / "agents" / agent).exists()
    proc = _shell({"MIND_AGENT": agent})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"agents/{agent}/session/handoff.yaml does not exist" in proc.stdout
    assert "## Step 9: Continuation Handoff" in proc.stdout
    assert not (REPO / "agents" / agent).exists()


def test_framework_call_shape_matches_vessel_shape():
    agent = "zz-handoff-probe-nonexistent"
    vessel = _shell({"MIND_AGENT": agent})
    framework = _shell({"MIND_AGENT": agent})
    assert framework.returncode == vessel.returncode == 1
    assert framework.stdout == vessel.stdout


def test_housekeeping_loader_names_step9_line():
    # Nonexistent agent + fresh sid: no read tracker, so the digest path is emitted.
    agent = "zz-handoff-probe-nonexistent"
    proc = subprocess.run(
        [BASH, "core/scripts/load-consolidation-housekeeping.sh"], cwd=REPO,
        env={**os.environ, "MIND_AGENT": agent, "MIND_SID": "zz-loader-probe-sid",
             "STORAGE_BACKEND": "local"},
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    lines = shc.DIGEST.read_text(encoding="utf-8").splitlines()
    step9 = next(i for i, ln in enumerate(lines, 1) if ln.startswith("## Step 9:"))
    path_line, note = proc.stdout.splitlines()
    assert path_line.endswith("consolidation-housekeeping.md")
    assert f"NOTE: {len(lines)} lines." in note
    assert f"is at line {step9}," in note
    assert not (REPO / "agents" / agent).exists()


def test_d7_runs_the_check_before_the_mode_flip():
    skill = (REPO / ".claude/skills/aspirations-graceful-stop/SKILL.md").read_text(
        encoding="utf-8")
    flip = [ln for ln in skill.splitlines()
            if 'session-mode-set.sh "{target_mode}"' in ln and ln.startswith("Bash:")]
    assert len(flip) == 1, flip
    line = flip[0]
    assert line.index("stop-handoff-check.sh") < line.index("session-mode-set.sh")
    assert "stop-handoff-check.sh && " in line
    assert "handoff saved" not in skill


def _leftover_then_new_stop(session: Path) -> None:
    """An earlier stop stamped GS-0 an hour ago and wrote its handoff, then ran
    D7 (which deletes stop-target-mode) but missed D7.1, so its checkpoint
    stayed. A new stop is requested and runs the production GS-0 write."""
    _stamp_stop(session, seconds_ago=3600)
    h = session / "handoff.yaml"
    h.write_text(HANDOFF, encoding="utf-8")
    old = time.time() - 3000
    os.utime(h, (old, old))
    tm = session / "stop-target-mode"
    tm.write_text("assistant\n", encoding="utf-8")
    requested = time.time() - 60
    os.utime(tm, (requested, requested))
    rec = sc.write_checkpoint(session, "assistant")  # the new stop's GS-0
    assert rec["resume_count"] == 1  # indistinguishable from a --resume here


def test_leftover_checkpoint_refuses_the_previous_stops_handoff(tmp_path, capsys):
    _leftover_then_new_stop(tmp_path)
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 1, out
    assert "BEFORE this stop began" in out and "(stop-target-mode)" in out
    (tmp_path / "handoff.yaml").write_text(HANDOFF, encoding="utf-8")  # this stop's own
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 0 and out.startswith("Handoff saved"), out


def test_resume_with_stop_target_mode_still_passes(tmp_path, capsys):
    tm = tmp_path / "stop-target-mode"
    tm.write_text("assistant\n", encoding="utf-8")
    requested = time.time() - 900  # the request predates GS-0
    os.utime(tm, (requested, requested))
    _stamp_stop(tmp_path, seconds_ago=600)
    h = tmp_path / "handoff.yaml"
    h.write_text(HANDOFF, encoding="utf-8")
    written = time.time() - 300  # handoff landed before the interruption
    os.utime(h, (written, written))
    assert sc.write_checkpoint(tmp_path, "assistant")["resume_count"] == 1
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 0 and "(stop-checkpoint)" in out, out


def test_non_utf8_handoff_is_refused_not_failed_open(tmp_path, capsys):
    _stamp_stop(tmp_path)
    (tmp_path / "handoff.yaml").write_bytes(b"session_number: 7\nnext_focus: \xff\xfe\n")
    rc, out = _run(tmp_path, capsys=capsys)
    assert rc == 1, out
    assert "empty or not a YAML mapping" in out
    assert "NOT VERIFIED" not in out


def test_pre_flush_step_wording_and_caller(tmp_path, capsys, monkeypatch):
    import _gate_log
    callers = []
    monkeypatch.setattr(_gate_log, "log", lambda *a, **k: callers.append(k["caller"]))
    _stamp_stop(tmp_path)
    rc, out = _run(tmp_path, "--step", "D4.1", capsys=capsys)
    assert rc == 1
    assert "NO HANDOFF YET" in out and "Re-run the D4.1 command unchanged." in out
    assert "D7.1" not in out and "D7 refused" not in out
    assert "## Step 9: Continuation Handoff" in out
    (tmp_path / "handoff.yaml").write_text(HANDOFF, encoding="utf-8")
    rc, out = _run(tmp_path, "--step", "D4.1", capsys=capsys)
    assert rc == 0 and out.startswith("Handoff saved")
    assert callers == ["aspirations-graceful-stop D4.1"] * 2


def test_d41_runs_the_check_before_the_commit_and_flush():
    skill = (REPO / ".claude/skills/aspirations-graceful-stop/SKILL.md").read_text(
        encoding="utf-8")
    d41 = [ln for ln in skill.splitlines()
           if ln.startswith("Bash:") and "stop-handoff-check.sh --step D4.1" in ln]
    assert len(d41) == 1, d41
    pos = skill.index(d41[0])
    assert skill.index("# D4: Consolidation") < pos < skill.index("# D4.5:")
    assert pos < skill.index("iteration-commit.sh --goal-id graceful-stop")
    assert pos < skill.index("bash core/scripts/owncloud-flush.sh")
