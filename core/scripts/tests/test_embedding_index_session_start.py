"""Tests for `embedding-index-freshness.py --session-start` (, ask A).

A box that never runs the loop and never serves a retrieve stayed on the token
baseline for good: the tick returned at "no index" from both of its call sites,
so only an operator ever built the first index. --session-start, called from
sessionstart-orchestrator.sh, builds it (detached, claimed, niced) or says why
retrieval is token-only.

Hermetic: the module is importlib-loaded (hyphenated filename), the index dir
and the build log live under tmp_path, Popen is replaced, and the encoder-stack
probe and the blend flag are monkeypatched. conftest.py points
MIND_EMBEDDING_INDEX_DIR at a nonexistent dir, so an unpatched Popen here would
start a REAL build — the fixture owns that, and every test uses it."""
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

_spec = importlib.util.spec_from_file_location(
    "embedding_index_freshness_session_start", CORE_SCRIPTS / "embedding-index-freshness.py")
fresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fresh)

import _vendor_path  # noqa: E402  (core/scripts is on sys.path via the loader above)

ORCHESTRATOR = CORE_SCRIPTS / "sessionstart-orchestrator.sh"
posix_only = pytest.mark.skipif(os.name == "nt", reason="priority and session flags are POSIX")


class _Box:
    """One simulated session-start box: where the index would be, what was
    spawned, and how to run the decision."""

    def __init__(self, idx, log):
        self.idx = idx
        self.log = log
        self.calls = []

    @property
    def marker(self):
        return self.idx / ".last-update-attempt"

    def run(self, dry_run=False):
        return fresh.session_start(dry_run)


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """Blend on, encoder stack present, no index, nothing spawned yet."""
    idx = tmp_path / "index"  # deliberately not created: the claim must make it
    log = tmp_path / "update.log"
    b = _Box(idx, log)
    monkeypatch.setenv("EMBED_FRESHNESS_INDEX_DIR", str(idx))
    monkeypatch.delenv("EMBED_FRESHNESS_DRYRUN", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.setattr(fresh, "UPDATE_LOG", log)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    monkeypatch.setattr(_vendor_path, "stack_absent_reason", lambda: None)
    monkeypatch.setattr(fresh.subprocess, "Popen",
                        lambda args, **kw: b.calls.append((args, kw)))
    return b


def _make_index(idx):
    idx.mkdir(parents=True, exist_ok=True)
    (idx / "meta.json").write_text("{}", encoding="utf-8")


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


# ── what it says and when it stays quiet ─────────────────────────────────────

def test_blend_off_is_silent_and_never_probes_the_stack(box, monkeypatch):
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: False)
    monkeypatch.setattr(_vendor_path, "stack_absent_reason",
                        lambda: pytest.fail("must not probe the stack when the blend is off"))
    res = box.run()
    assert res == {"status": "off", "message": None}
    assert not box.calls and not box.idx.exists()


def test_stack_absent_says_so_in_one_line_with_the_reason_and_the_recipe(box, monkeypatch):
    monkeypatch.setattr(_vendor_path, "stack_absent_reason", lambda: "numpy is not importable")
    res = box.run()
    assert res["status"] == "stack-absent"
    msg = res["message"]
    assert "\n" not in msg and msg.startswith("[embedding-index]")
    assert "numpy is not importable" in msg and "TOKEN-ONLY" in msg
    assert fresh.STACK_RECIPE in msg and "guard-1427" in msg
    assert not box.calls and not box.idx.exists()  # nothing built, nothing claimed


def test_stack_absent_is_reported_even_when_an_index_exists(box, monkeypatch):
    """The cc-05 shape: index files present, no numpy in the interpreter. Retrieval
    is token-only there too, and nothing told the agent."""
    _make_index(box.idx)
    monkeypatch.setattr(_vendor_path, "stack_absent_reason",
                        lambda: "no encoder backend is importable (fastembed or sentence-transformers)")
    res = box.run()
    assert res["status"] == "stack-absent" and "no encoder backend" in res["message"]
    assert not box.calls


def test_index_present_and_stack_present_is_silent(box):
    _make_index(box.idx)
    res = box.run()
    assert res == {"status": "index-present", "message": None}
    assert not box.calls and not box.marker.exists()


def test_a_probe_that_raises_is_silence_not_a_build(box, monkeypatch):
    def boom():
        raise RuntimeError("probe broke")
    monkeypatch.setattr(_vendor_path, "stack_absent_reason", boom)
    res = box.run()
    assert res == {"status": "probe-failed", "message": None}
    assert not box.calls and not box.marker.exists()


# ── the build it starts ──────────────────────────────────────────────────────

def test_no_index_spawns_the_initial_build_once_and_says_where_the_log_is(box):
    res = box.run()
    assert res["status"] == "spawned"
    assert "\n" not in res["message"] and str(box.log) in res["message"]
    assert "TOKEN-ONLY" in res["message"] and "HF_HUB_OFFLINE=1" in res["message"]
    assert len(box.calls) == 1
    args, kw = box.calls[0]
    assert args == [sys.executable, str(CORE_SCRIPTS / "embedding-index-build.py"),
                    "--build", "--out", str(box.idx)]
    assert box.marker.exists()
    assert kw["stdout"].name == str(box.log) and kw["stderr"] is kw["stdout"]
    assert kw["cwd"] == str(CORE_SCRIPTS.parent.parent)


def test_the_log_is_appended_to_never_truncated(box):
    box.log.write_text("earlier build output\n", encoding="utf-8")
    box.run()
    assert box.log.read_text(encoding="utf-8") == "earlier build output\n"


def test_the_tick_seam_outranks_the_reader_seam_here_too(box, tmp_path, monkeypatch):
    """The tick and the reader must agree on one index (the module docstring's
    precedence): a process whose reader is redirected builds where it reads."""
    reader = tmp_path / "reader-index"
    monkeypatch.setenv("MIND_EMBEDDING_INDEX_DIR", str(reader))
    box.run()
    assert box.idx.is_dir() and not reader.exists()
    assert box.calls[0][0][-2:] == ["--out", str(box.idx)]
    monkeypatch.delenv("EMBED_FRESHNESS_INDEX_DIR")
    box.run()
    assert reader.is_dir() and box.calls[1][0][-2:] == ["--out", str(reader)]


def test_the_build_never_names_a_model(box):
    """embedding_model_name is the calibration anchor (guard-5905): a build that
    picked whatever model is cached would silently void every cosine floor."""
    box.run()
    args, _ = box.calls[0]
    assert "--model" not in args and not any(a.startswith("--model") for a in args)


def test_the_build_child_may_fetch_the_model_into_an_empty_cache(box):
    """guard-1427: the builder's own HF_HUB_OFFLINE=1 default cannot fetch it."""
    box.run()
    env = box.calls[0][1]["env"]
    assert env["HF_HUB_OFFLINE"] == "0" and env["TRANSFORMERS_OFFLINE"] == "0"
    # a copy: this process (and the daemon-style reader beside it) stays offline
    assert env is not os.environ
    assert "HF_HUB_OFFLINE" not in os.environ and "TRANSFORMERS_OFFLINE" not in os.environ


def test_an_operator_exported_offline_value_wins(box, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    box.run()
    env = box.calls[0][1]["env"]
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "0"


@posix_only
def test_the_build_is_detached_and_niced(box, monkeypatch):
    seen = []
    monkeypatch.setattr(fresh.os, "nice", lambda n: seen.append(n))
    box.run()
    kw = box.calls[0][1]
    assert kw["start_new_session"] is True
    kw["preexec_fn"]()  # what the child runs just before exec
    assert seen == [fresh.INITIAL_BUILD_NICE] and fresh.INITIAL_BUILD_NICE > 0


@posix_only
def test_the_update_spawn_is_unchanged_by_the_shared_helper(box, monkeypatch):
    """The default tick now spawns through _spawn_detached too. It must still
    inherit the env and run at normal priority: only the initial build is niced."""
    _make_index(box.idx)
    _age(box.idx / "meta.json", 7200)
    monkeypatch.setattr(fresh, "_source_mtime", lambda: time.time())
    assert fresh.main() == 0
    args, kw = box.calls[0]
    assert args[1:3] == [str(CORE_SCRIPTS / "embedding-index-build.py"), "--update"]
    assert kw["start_new_session"] is True
    assert "env" not in kw and "preexec_fn" not in kw


def test_dry_run_claims_but_spawns_nothing(box):
    res = box.run(dry_run=True)
    assert res["status"] == "would-spawn" and str(box.log) in res["message"]
    assert box.marker.exists() and not box.calls


# ── one build, however many sessions start ──────────────────────────────────

def test_a_second_session_inside_the_window_spawns_nothing(box):
    box.run()
    res = box.run()
    assert len(box.calls) == 1
    assert res["status"] == "attempt-recorded" and "\n" not in res["message"]
    assert str(box.log) in res["message"] and "has not landed" in res["message"]
    assert fresh._stamp(box.marker.stat().st_mtime) in res["message"]
    assert "next attempt is allowed after" in res["message"]


def test_a_dir_without_meta_json_is_not_an_index(box):
    """A claim (or a failed build) leaves the dir behind; only meta.json makes an index."""
    box.run()
    assert box.idx.is_dir() and not (box.idx / "meta.json").exists()
    assert box.run()["status"] == "attempt-recorded"


def test_an_expired_claim_lets_the_next_session_build_again(box):
    """A build that never produced meta.json is retried once its window is up."""
    box.run()
    _age(box.marker, fresh.DEBOUNCE_SECONDS + 60)
    res = box.run()
    assert res["status"] == "spawned" and len(box.calls) == 2


def test_the_claim_has_exactly_one_winner(tmp_path):
    marker = tmp_path / "i" / ".last-update-attempt"
    now = time.time()
    assert fresh._claim_initial_build(marker, now) == (True, None)
    won, attempted = fresh._claim_initial_build(marker, now)
    assert won is False and abs(attempted - now) < 5


def test_an_expired_claim_is_taken_over_once_and_leaves_nothing_behind(tmp_path):
    marker = tmp_path / "i" / ".last-update-attempt"
    marker.parent.mkdir()
    marker.write_text("old", encoding="utf-8")
    _age(marker, fresh.DEBOUNCE_SECONDS + 60)
    assert fresh._claim_initial_build(marker, time.time()) == (True, None)
    assert time.time() - marker.stat().st_mtime < 5  # a fresh claim, not the old file
    assert fresh._claim_initial_build(marker, time.time())[0] is False
    assert sorted(p.name for p in marker.parent.iterdir()) == [".last-update-attempt"]


def test_a_claim_two_hours_old_still_holds(tmp_path):
    """Only the long window frees a claim: the 10-minute success interval belongs
    to the update path, and a build that is still running must not be doubled."""
    marker = tmp_path / "i" / ".last-update-attempt"
    marker.parent.mkdir()
    marker.write_text("recent", encoding="utf-8")
    _age(marker, 2 * 3600)
    won, attempted = fresh._claim_initial_build(marker, time.time())
    assert won is False and attempted is not None
    assert marker.read_text(encoding="utf-8") == "recent"


def test_a_takeover_lost_to_another_session_changes_nothing(tmp_path, monkeypatch):
    marker = tmp_path / "i" / ".last-update-attempt"
    marker.parent.mkdir()
    marker.write_text("old", encoding="utf-8")
    _age(marker, fresh.DEBOUNCE_SECONDS + 60)
    before = marker.stat().st_mtime

    def lost(src, dst):
        raise FileNotFoundError(src)  # the other session renamed it first
    monkeypatch.setattr(fresh.os, "rename", lost)
    won, attempted = fresh._claim_initial_build(marker, time.time())
    assert won is False and attempted == before
    assert marker.stat().st_mtime == before


def test_a_holder_that_vanishes_between_create_and_stat_is_retried(tmp_path, monkeypatch):
    marker = tmp_path / "i" / ".last-update-attempt"
    real_open = os.open
    calls = []

    def flaky(path, flags, mode=0o777):
        calls.append(path)
        if len(calls) == 1:
            raise FileExistsError(path)  # a holder, gone again by the time we stat it
        return real_open(path, flags, mode)
    monkeypatch.setattr(fresh.os, "open", flaky)
    assert fresh._claim_initial_build(marker, time.time()) == (True, None)
    assert len(calls) == 2


# ── failing quietly, and recovering ──────────────────────────────────────────

def test_a_spawn_failure_is_reported_and_releases_the_claim(box, monkeypatch):
    def boom(args, **kw):
        raise OSError("no such interpreter")
    monkeypatch.setattr(fresh.subprocess, "Popen", boom)
    res = box.run()
    assert res["status"] == "spawn-failed" and "no such interpreter" in res["message"]
    assert "\n" not in res["message"]
    assert not box.marker.exists()  # no process exists, so this was not an attempt
    monkeypatch.setattr(fresh.subprocess, "Popen", lambda args, **kw: box.calls.append((args, kw)))
    assert box.run()["status"] == "spawned" and len(box.calls) == 1


def test_a_claim_that_cannot_be_recorded_is_silence_not_a_build(box, monkeypatch):
    def boom(marker, now):
        raise PermissionError("read-only index dir")
    monkeypatch.setattr(fresh, "_claim_initial_build", boom)
    res = box.run()
    assert res == {"status": "claim-failed", "message": None}
    assert not box.calls


# ── the entry point and the wiring ───────────────────────────────────────────

def test_main_prints_exactly_the_message_and_returns_zero(box, capsys, monkeypatch):
    monkeypatch.setenv("EMBED_FRESHNESS_DRYRUN", "1")
    assert fresh.main(["--session-start"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("[embedding-index] no index on this box")


def test_main_is_silent_when_there_is_nothing_to_say(box, capsys):
    _make_index(box.idx)
    assert fresh.main(["--session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_the_default_tick_still_never_builds_an_initial_index(box, capsys):
    """Without the flag nothing changed: no index means silence and no claim."""
    assert fresh.main() == 0 and fresh.main([]) == 0
    assert capsys.readouterr().out == ""
    assert not box.calls and not box.marker.exists()


def test_an_unknown_flag_is_refused(box):
    with pytest.raises(SystemExit) as exc:
        fresh.main(["--no-such-flag"])
    assert exc.value.code == 2 and not box.calls


def test_the_orchestrator_runs_the_step_and_keeps_its_stdout():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    head = text.index("# ─── Step 2.76:")
    block = text[head:text.index("# ─── Step 2.8:")]
    # the step runs THIS script, in its session-start mode
    assert '_ei="$SCRIPT_DIR/embedding-index-freshness.py"' in block
    lines = [l for l in block.splitlines() if "--session-start" in l and "python3" in l]
    assert len(lines) == 1, lines
    line = lines[0]
    assert '"$_ei"' in line and line.strip().startswith("python3 ")
    # stdout is the agent's channel: only stderr is redirected, and into the build's log
    assert "2>>" in line and "embedding-index-update.log" in line
    assert ">/dev/null" not in line and "&>" not in line
    # unconditional like its siblings: after Step 2.75, before the source=compact block
    assert text.index("history-vacuum-tick.sh") < head < text.index('if [ "$SOURCE" = "compact" ]')
    assert block.rstrip().endswith(") || true")  # the chain's fail-open contract
