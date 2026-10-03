"""test_uncommitted_edits_prune.py —  cleanup of rows already logged for git-ignored trees.

Verifies core/scripts/uncommitted-edits-prune.py, which removes from an agent's
<agent>/session/uncommitted-edits.jsonl the rows bash-edit-record.sh wrote for paths
git ignores (core/logs/, core/.pycache/, ...) before the recorder stopped walking
them (test_bash_edit_record.py covers the recorder).

Contract exercised here:
  - a row is dropped only when its file lies under a skipped tree inside core/ or
    .claude/; every other row (look-alike names, world paths, absolute paths,
    unparseable lines, non-object JSON) stays, byte for byte and in order;
  - the whole original is archived first: gzip, byte-exact, .archive-marker,
    RECEIPT.json with per-prefix counts and before/after rows and bytes;
  - a dry run writes nothing; a second run finds nothing and makes no archive;
  - a row appended between the read and the swap, or through a handle opened
    before the swap, is kept exactly once and is copied as it is even when it is
    junk: a row the archive does not hold is never dropped;
  - a failure before the swap leaves the log untouched: an unwritable archive, an
    archive that reads back wrong, a log replaced by another writer every time.
"""

from __future__ import annotations

import collections
import gzip
import hashlib
import importlib.util
import itertools
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PRUNE_PY = CORE_SCRIPTS / "uncommitted-edits-prune.py"

# The swap renames over a file the prune still has open; the script is written for POSIX.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="uncommitted-edits-prune.py is POSIX-only")

sys.path.insert(0, str(CORE_SCRIPTS))
import _edit_record_skip  # noqa: E402

_spec = importlib.util.spec_from_file_location("uncommitted_edits_prune", PRUNE_PY)
prune_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune_mod)

LOG_NAME = "uncommitted-edits.jsonl"
GZ_NAME = LOG_NAME + ".gz"

# Same namespace strip as test_bash_edit_record.py: the subprocess must resolve paths
# from the seeded temp repo, not from the parent's framework env.
_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_",
    "BODY_",
)

KEPT_FILES = (
    "core/scripts/real-tool.py",
    ".claude/skills/example/SKILL.md",
    "core/config/logs/notes.md",          # `logs` below config/, not core/logs
    "core/scripts/tests/test_real.py",    # tests/ is tracked; only _tmp_* inside it is ignored
    ".mind-data/world/scripts/w.py",      # outside the scan roots: domain-suite-gate reads these rows
    "/abs/legacy/core/logs/x.log",        # an absolute path is not proof of a skipped tree
)
JUNK_FILES = (
    "core/logs/hook-gate.jsonl",
    "core/logs/sub/dir/x.log",
    "core/.pycache/core/scripts/a.cpython-312.pyc",
    "core/scripts/tests/_tmp_case/x.py",
    "core/scripts/__pycache__/m.cpython-312.pyc",
    ".claude/.history/skills/x.md",
    ".claude/worktrees/w1/f.txt",
    "core\\logs\\win.log",                # a legacy row written with backslashes
)
# Rows that are not a file row at all: with no proof they are junk, they stay.
ODD_KEPT_ROWS = (b"not json at all", b'["a","list"]', b'{"mtime":1}', b'{"file":5}', b'{"file":null}', b"")

EXPECTED_BY_PREFIX_PER_COPY = {
    "core/logs": 3, "core/.pycache": 1, "core/scripts": 2, ".claude/.history": 1, ".claude/worktrees": 1,
}


def _hermetic_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"}


def _row(file: str, n: int = 0) -> bytes:
    return json.dumps({"file": file, "mtime": 1_700_000_000 + n, "edit_ts": "2026-10-02T00:00:00",
                       "goal_id": "", "sid": ""}, separators=(",", ":")).encode()


def _lines(rows) -> bytes:
    return b"".join(r + b"\n" for r in rows)


def _n(rows) -> int:
    return sum(1 for r in rows if r.strip())


def _mixed(copies: int = 10):
    """(every row, the rows that must survive), the two kinds interleaved."""
    pairs = []
    for kf, jf in itertools.zip_longest(KEPT_FILES, JUNK_FILES):
        pairs += [(f, junk) for f, junk in ((kf, False), (jf, True)) if f]
    rows, kept = list(ODD_KEPT_ROWS), list(ODD_KEPT_ROWS)
    for i in range(copies):
        for f, junk in pairs:
            r = _row(f, i)
            rows.append(r)
            if not junk:
                kept.append(r)
    return rows, kept


def _make_log(tmp_path: Path, copies: int = 10):
    log = tmp_path / LOG_NAME
    rows, kept = _mixed(copies)
    log.write_bytes(_lines(rows))
    return log, rows, kept


def _has_junk(log: Path) -> int:
    """Positive control for every 'the log is untouched' assertion: how many rows a real
    run WOULD drop. Zero would make 'unchanged' mean nothing."""
    n = prune_mod.prune(log, log.parent / "never-made", dry_run=True)["would_drop_rows"]
    assert n > 0
    return n


def _archive_bytes(arch: Path) -> bytes:
    return gzip.decompress((arch / GZ_NAME).read_bytes())


def _run_cli(*args, script: Path = PRUNE_PY, **kw):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                          timeout=60, env=_hermetic_env(), **kw)


# ---------------------------------------------------------------------------


def test_only_rows_of_skipped_trees_are_dropped_and_the_rest_stay_in_order(tmp_path):
    log, rows, kept = _make_log(tmp_path)
    out = prune_mod.prune(log, tmp_path / "arch", settle_s=0)
    assert out["status"] == "pruned"
    assert log.read_bytes() == _lines(kept)
    # Positive control: both sides are populated, so the equality above is not vacuous.
    assert kept and len(kept) < len(rows)
    assert out["dropped_rows"] == _n(rows) - _n(kept) == len(JUNK_FILES) * 10
    assert out["before"] == {"bytes": len(_lines(rows)), "rows": _n(rows)}
    assert out["after"] == {"bytes": log.stat().st_size, "rows": _n(kept)}
    assert out["dropped_by_prefix"] == {k: v * 10 for k, v in EXPECTED_BY_PREFIX_PER_COPY.items()}


def test_the_log_keeps_its_permission_bits(tmp_path):
    log, _, _ = _make_log(tmp_path)
    log.chmod(0o640)
    prune_mod.prune(log, tmp_path / "arch", settle_s=0)
    assert stat.S_IMODE(log.stat().st_mode) == 0o640


def test_the_archive_is_the_original_byte_for_byte_with_a_receipt(tmp_path):
    log, rows, kept = _make_log(tmp_path)
    original = log.read_bytes()
    arch = tmp_path / "arch"
    out = prune_mod.prune(log, arch, settle_s=0)
    assert _archive_bytes(arch) == original
    assert (arch / ".archive-marker").is_file()
    receipt = json.loads((arch / "RECEIPT.json").read_text())
    assert out["receipt"] == str(arch / "RECEIPT.json")
    assert out["archive"] == {"path": str(arch / GZ_NAME), "original_bytes": len(original),
                              "original_sha256": hashlib.sha256(original).hexdigest(),
                              "gz_bytes": (arch / GZ_NAME).stat().st_size}
    assert receipt["archive"] == out["archive"]
    assert receipt["before"] == out["before"] and receipt["after"] == out["after"]
    assert receipt["log"] == str(log) and receipt["restore"] and receipt["host"] and receipt["pruned_at"]
    assert sum(receipt["dropped_by_prefix"].values()) == receipt["dropped_rows"] > 0
    # The one invariant a reader of the receipt relies on: nothing was lost but the dropped rows.
    assert receipt["after"]["rows"] == (receipt["before"]["rows"] - receipt["dropped_rows"]
                                        + sum(receipt["rows_appended_during_run"].values()))


def test_a_dry_run_writes_nothing_and_counts_what_a_run_drops(tmp_path):
    log, rows, kept = _make_log(tmp_path)
    before = log.read_bytes()
    arch = tmp_path / "arch"
    dry = prune_mod.prune(log, arch, dry_run=True)
    assert dry["status"] == "dry-run"
    assert log.read_bytes() == before and not arch.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [LOG_NAME]
    real = prune_mod.prune(log, arch, settle_s=0)
    assert dry["would_drop_rows"] == real["dropped_rows"] > 0
    assert dry["before"] == real["before"]


def test_a_second_run_finds_nothing_and_makes_no_archive(tmp_path):
    log, rows, kept = _make_log(tmp_path)
    first = prune_mod.prune(log, tmp_path / "arch1", settle_s=0)
    assert first["status"] == "pruned"  # positive control: the first run had work to do
    after_first = log.read_bytes()
    second = prune_mod.prune(log, tmp_path / "arch2", settle_s=0)
    assert second["status"] == "nothing-to-prune" and second["dropped_rows"] == 0
    assert log.read_bytes() == after_first
    assert not (tmp_path / "arch2").exists()


def test_the_prune_uses_the_recorders_skip_rule_not_a_copy_of_it():
    assert prune_mod.is_skipped_path is _edit_record_skip.is_skipped_path


def test_rows_appended_between_the_read_and_the_swap_are_kept_as_they_are(tmp_path, monkeypatch):
    log, rows, kept = _make_log(tmp_path)
    original = log.read_bytes()
    during = [_row("core/scripts/during-1.py", 90), _row("core/logs/during.log", 91), _row("core/scripts/during-2.py", 92)]
    real_write = prune_mod._write_archive

    def write_then_append(data, gz_path, mode):
        real_write(data, gz_path, mode)
        with open(log, "ab") as f:  # a recorder, after the prune has read the log
            f.write(_lines(during))

    monkeypatch.setattr(prune_mod, "_write_archive", write_then_append)
    arch = tmp_path / "arch"
    out = prune_mod.prune(log, arch, settle_s=0)
    # The junk row appended after the read is NOT in the archive, so it is not dropped.
    assert log.read_bytes() == _lines(kept + during)
    assert out["rows_appended_during_run"] == {"before_swap": 3, "after_swap": 0}
    assert _archive_bytes(arch) == original
    monkeypatch.undo()
    # The leftover junk is the next run's work, and that run archives it first.
    again = prune_mod.prune(log, tmp_path / "arch2", settle_s=0)
    assert again["dropped_rows"] == 1
    assert log.read_bytes() == _lines(kept + [during[0], during[2]])


@pytest.mark.parametrize("delay_s", [0, 0.1])
def test_a_row_written_through_a_handle_opened_before_the_swap_is_kept_once(tmp_path, monkeypatch, delay_s):
    log, rows, kept = _make_log(tmp_path)
    straggler = open(log, "ab")  # a recorder that opened the log before the swap
    late = _row("core/scripts/straggler.py", 99)
    real_replace = os.replace
    timers = []

    def write_late():
        straggler.write(late + b"\n")  # lands in the file the swap just unlinked
        straggler.flush()

    def replace_then_write(src, dst):
        real_replace(src, dst)
        if Path(dst) == log:
            if delay_s:  # the settle window, not just the first look after the swap, must catch it
                timers.append(threading.Timer(delay_s, write_late))
                timers[-1].start()
            else:
                write_late()

    monkeypatch.setattr(prune_mod.os, "replace", replace_then_write)
    try:
        out = prune_mod.prune(log, tmp_path / "arch", settle_s=0.4)
    finally:
        for t in timers:
            t.join()
        straggler.close()
    monkeypatch.undo()
    final = log.read_bytes().split(b"\n")[:-1]
    assert final.count(late) == 1
    assert final == kept + [late]
    assert out["rows_appended_during_run"] == {"before_swap": 0, "after_swap": 1}


def test_rows_a_recorder_appends_while_the_prune_runs_are_all_kept_exactly_once(tmp_path):
    log, rows, kept = _make_log(tmp_path, copies=300)
    stop = threading.Event()
    appended = []

    def recorder():
        i = 0
        while not stop.is_set():
            r = _row(f"core/scripts/live-{i}.py", i)
            with open(log, "ab") as f:  # open, append, close: what both recorders do
                f.write(r + b"\n")
            appended.append(r)
            i += 1
            time.sleep(0.0002)

    t = threading.Thread(target=recorder)
    t.start()
    try:
        time.sleep(0.05)
        out = prune_mod.prune(log, tmp_path / "arch", settle_s=0.4)
    finally:
        stop.set()
        t.join()
    counts = collections.Counter(log.read_bytes().split(b"\n")[:-1])
    assert len(appended) >= 10, "the recorder thread never ran, so nothing was exercised"
    missing = [r for r in appended if counts[r] != 1]
    assert not missing, f"{len(missing)} of {len(appended)} appended rows not kept exactly once; summary {out}"
    # Everything else is the original kept rows, each exactly as many times as it was written.
    for r, c in collections.Counter(kept).items():
        assert counts[r] == c
    assert out["dropped_rows"] == _n(rows) - _n(kept)


def test_an_unterminated_last_line_is_left_out_and_counted(tmp_path):
    log, rows, kept = _make_log(tmp_path, copies=2)
    fragment = b'{"file":"core/scripts/torn.py","mtime":17'
    log.write_bytes(_lines(rows) + fragment)
    arch = tmp_path / "arch"
    out = prune_mod.prune(log, arch, settle_s=0)
    assert log.read_bytes() == _lines(kept)  # ends on a newline: the next append starts a fresh row
    assert out["unterminated_tail_bytes_left_behind"] == len(fragment)
    assert _archive_bytes(arch).endswith(fragment)


def test_an_unwritable_archive_leaves_the_log_untouched(tmp_path):
    log, rows, kept = _make_log(tmp_path)
    original = log.read_bytes()
    _has_junk(log)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the archive directory's parent should be\n")
    with pytest.raises(prune_mod.PruneError, match="the log was not touched"):
        prune_mod.prune(log, blocker / "arch", settle_s=0)
    assert log.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["blocker", LOG_NAME]


@pytest.mark.parametrize("corrupt", ["garbage", "truncated", "same-length-other-bytes"])
def test_an_archive_that_reads_back_wrong_leaves_the_log_untouched(tmp_path, monkeypatch, corrupt):
    log, rows, kept = _make_log(tmp_path)
    original = log.read_bytes()
    _has_junk(log)
    real_write = prune_mod._write_archive

    def bad_write(data, gz_path, mode):
        if corrupt == "garbage":
            gz_path.write_bytes(b"not a gzip stream")
        elif corrupt == "truncated":
            real_write(data, gz_path, mode)
            blob = gz_path.read_bytes()
            gz_path.write_bytes(blob[: len(blob) // 2])
        else:
            real_write(data[:-10] + b"x" * 10, gz_path, mode)

    monkeypatch.setattr(prune_mod, "_write_archive", bad_write)
    arch = tmp_path / "arch"
    with pytest.raises(prune_mod.PruneError, match="the log was not touched"):
        prune_mod.prune(log, arch, settle_s=0)
    assert log.read_bytes() == original
    assert not (arch / "RECEIPT.json").exists()
    assert not list(tmp_path.glob("*.prune-*.tmp"))


def _replace_with_new_inode(log: Path, rows) -> None:
    other = log.with_name("other-writer.tmp")
    other.write_bytes(_lines(rows))
    os.replace(other, log)  # what iteration-commit.sh does after a self-commit


def test_a_log_replaced_by_another_writer_is_retried(tmp_path, monkeypatch):
    log, rows, kept = _make_log(tmp_path)
    other_rows, other_kept = _mixed(copies=3)
    real_write = prune_mod._write_archive
    calls = []

    def write_then_replace_once(data, gz_path, mode):
        real_write(data, gz_path, mode)
        calls.append(1)
        if len(calls) == 1:
            _replace_with_new_inode(log, other_rows)

    monkeypatch.setattr(prune_mod, "_write_archive", write_then_replace_once)
    arch = tmp_path / "arch"
    out = prune_mod.prune(log, arch, settle_s=0)
    assert len(calls) == 2
    assert log.read_bytes() == _lines(other_kept)
    assert _archive_bytes(arch) == _lines(other_rows)  # the archive is of what was actually replaced
    assert out["before"]["rows"] == _n(other_rows)
    assert not list(tmp_path.glob("*.prune-*.tmp"))


def test_a_log_replaced_by_another_writer_every_time_is_refused(tmp_path, monkeypatch):
    log, rows, kept = _make_log(tmp_path)
    real_write = prune_mod._write_archive
    calls = []

    def write_then_replace(data, gz_path, mode):
        real_write(data, gz_path, mode)
        calls.append(1)
        _replace_with_new_inode(log, rows)

    monkeypatch.setattr(prune_mod, "_write_archive", write_then_replace)
    with pytest.raises(prune_mod.PruneError, match="replaced by another writer"):
        prune_mod.prune(log, tmp_path / "arch", settle_s=0)
    assert len(calls) == prune_mod.ATTEMPTS
    assert log.read_bytes() == _lines(rows)  # the other writer's file, never ours
    assert not list(tmp_path.glob("*.prune-*.tmp"))


def test_the_cli_prunes_the_log_it_is_given_and_prints_the_summary(tmp_path):
    log, rows, kept = _make_log(tmp_path)
    before = log.read_bytes()
    root = tmp_path / "temp"
    dry = _run_cli("--log", str(log), "--archive-root", str(root), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["status"] == "dry-run"
    assert log.read_bytes() == before and not root.exists()
    real = _run_cli("--log", str(log), "--archive-root", str(root), "--settle-s", "0")
    assert real.returncode == 0, real.stderr
    out = json.loads(real.stdout)
    assert out["status"] == "pruned" and out["dropped_rows"] == _n(rows) - _n(kept)
    arch_dirs = list(root.glob("uncommitted-edits-prune-*"))
    assert len(arch_dirs) == 1 and (arch_dirs[0] / "RECEIPT.json").is_file()
    assert _archive_bytes(arch_dirs[0]) == before
    assert log.read_bytes() == _lines(kept)


def test_the_cli_finds_the_agent_log_and_puts_the_archive_in_the_agent_temp_dir(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("uncommitted-edits-prune.py", "_edit_record_skip.py", "_paths.py"):
        (scripts / name).write_bytes((CORE_SCRIPTS / name).read_bytes())
    session = repo / "agents" / "alpha" / "session"
    session.mkdir(parents=True)
    rows, kept = _mixed()
    log = session / LOG_NAME
    log.write_bytes(_lines(rows))
    r = _run_cli("--agent", "alpha", "--settle-s", "0", script=scripts / "uncommitted-edits-prune.py")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["status"] == "pruned"
    assert log.read_bytes() == _lines(kept)
    arch_dirs = list((repo / "agents" / "alpha" / "temp").glob("uncommitted-edits-prune-*"))
    assert len(arch_dirs) == 1 and (arch_dirs[0] / ".archive-marker").is_file()


def test_the_cli_refuses_without_an_agent_and_reports_a_missing_log(tmp_path):
    no_agent = _run_cli()
    assert no_agent.returncode == 2 and "no agent" in no_agent.stderr
    missing = _run_cli("--log", str(tmp_path / "absent.jsonl"), "--archive-root", str(tmp_path / "temp"))
    assert missing.returncode == 0
    assert json.loads(missing.stdout)["status"] == "no-log"
    assert not (tmp_path / "temp").exists()


def test_the_cli_exits_1_and_names_the_state_when_the_archive_cannot_be_written(tmp_path):
    log, rows, kept = _make_log(tmp_path)
    original = log.read_bytes()
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    r = _run_cli("--log", str(log), "--archive-root", str(blocker), "--settle-s", "0")
    assert r.returncode == 1
    assert "FAILED" in r.stderr and "the log was not touched" in r.stderr
    assert log.read_bytes() == original
