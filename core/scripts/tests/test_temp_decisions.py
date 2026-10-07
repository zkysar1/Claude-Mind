#!/usr/bin/env python3
"""test_temp_decisions.py — temp_decisions.py, the decision log that governs
agents/<agent>/temp/ (user directive, 2026-10-05).

Pins the contract the purge and the review rely on:
  - decide is validated and all-or-nothing; a discard the purge could never
    execute (cited, receipted, git work, cited set unknown) is refused
  - the decision in force is the latest after the last deletion, and applies
    only while the fingerprint matches (an edit puts the item back up)
  - keep expires; in-flight items (any depth) are never pending or deletable
  - bulk-junk decides only aged empties and run output, never anything cited,
    tracked, already decided, a folder or a dotfile
  - deletable / log-deleted / show round-trip, byte-exact for non-ASCII names
  - reading git state never writes to the repo (a plain `git status` rewrites
    .git/index, which changed a folder's fingerprint and froze its decision)

cited_index and tracked_names are monkeypatched: the real ones scan the live
stores and the repo, which a unit test must not depend on.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import temp_decisions as td  # noqa: E402

HAS_GIT = shutil.which("git") is not None
GITC = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


class _Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data if isinstance(data, bytes) else data.encode("utf-8"))


@pytest.fixture
def temp(tmp_path, monkeypatch):
    """An empty temp/ with nothing cited and nothing tracked."""
    t = tmp_path / "temp"
    t.mkdir()
    monkeypatch.setattr(td, "cited_index", lambda: {})
    monkeypatch.setattr(td, "tracked_names", lambda temp_dir: set())
    return t


def _age(p, hours=3):
    """Set p (and, for a folder, everything under it) to `hours` ago."""
    t = time.time() - hours * 3600
    p = Path(p)
    if p.is_dir():
        for root, dirs, files in os.walk(p, topdown=False):
            for n in files + dirs:
                os.utime(os.path.join(root, n), (t, t))
    os.utime(p, (t, t))


def _write(temp, name, text="x\n", age=3):
    p = temp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if age:
        _age(p, age)
    return p


def _run(temp, argv, capsys, monkeypatch, stdin=b""):
    monkeypatch.setattr(sys, "stdin", _Stdin(stdin))
    rc = td.main(["--temp-dir", str(temp)] + list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def _decide(temp, records, capsys, monkeypatch):
    rc, out, _ = _run(temp, ["decide"], capsys, monkeypatch, json.dumps(records))
    return rc, json.loads(out)


def _rows(temp):
    return td.read_ledger(temp)[0]


def _git(*args, cwd):
    return subprocess.run(["git", *GITC, *args], cwd=str(cwd), capture_output=True, text=True)


# ── names ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["a.txt", "dir-1", "run_2026.log", "ünïcode.md"])
def test_valid_item_name_accepts_plain_names(name):
    assert td.valid_item_name(name)


@pytest.mark.parametrize("name", ["", ".", "..", ".hidden", "drained", "a/b", "a\\b",
                                  "tab\there", "nl\nx", "del\x7f", "lone\udcff", None, 3])
def test_valid_item_name_refuses_names_the_purge_cannot_round_trip(name):
    assert not td.valid_item_name(name)


def test_resolve_temp_dir_refuses_anything_but_an_absolute_temp(tmp_path):
    assert td.resolve_temp_dir(str(tmp_path / "temp")) == tmp_path / "temp"
    assert td.resolve_temp_dir(str(tmp_path / "other")) is None
    assert td.resolve_temp_dir("relative/temp") is None


# ── decide ───────────────────────────────────────────────────────────────────

def test_decide_records_every_decision_with_reason_and_fingerprint(temp, capsys, monkeypatch):
    _write(temp, "a.log", "run\n")
    _write(temp, "notes.md", "# n\n")
    _write(temp, "tool.sh", "echo hi\n")
    (temp / "d").mkdir()
    _write(temp, "d/f.txt", "y\n")
    rc, out = _decide(temp, [
        {"item": "a.log", "decision": "discard", "why": "finished run"},
        {"item": "notes.md", "decision": "encode", "why": "lesson", "where": "rb-1"},
        {"item": "tool.sh", "decision": "promote", "why": "reusable", "where": "world/scripts/tool.sh"},
        {"item": "d", "decision": "keep", "why": "in use"},
    ], capsys, monkeypatch)
    assert rc == 0 and out["recorded"] == 4
    rows = {r["item"]: r for r in _rows(temp)}
    assert rows["a.log"]["fp"] == "sha256:" + td.file_sha256(temp / "a.log")
    assert rows["a.log"]["why"] == "finished run" and "where" not in rows["a.log"]
    assert rows["tool.sh"]["where"] == "world/scripts/tool.sh"
    assert rows["d"]["kind"] == "dir" and rows["d"]["files"] == 1
    assert rows["d"]["fp"].startswith("tree:")
    assert all(r["host"] and r["by"] and r["ts"] for r in rows.values())
    assert not (temp / ".temp-decisions.lock").exists()


def test_decide_accepts_one_record_per_line(temp, capsys, monkeypatch):
    _write(temp, "a.log")
    _write(temp, "b.log")
    stdin = ('{"item":"a.log","decision":"discard","why":"w"}\n'
             '{"item":"b.log","decision":"keep","why":"w"}\n')
    rc, out, _ = _run(temp, ["decide"], capsys, monkeypatch, stdin)
    assert rc == 0 and json.loads(out)["recorded"] == 2


@pytest.mark.parametrize("record, needle", [
    ({"item": "a.log", "decision": "delete", "why": "w"}, "decision must be one of"),
    ({"item": "a.log", "decision": "discard"}, "why is required"),
    ({"item": "a.log", "decision": "discard", "why": "  "}, "why is required"),
    ({"item": "a.log", "decision": "encode", "why": "w"}, "where is required"),
    ({"item": "a.log", "decision": "archive", "why": "w", "where": " "}, "where is required"),
    ({"item": "missing.log", "decision": "keep", "why": "w"}, "not found"),
    ({"item": ".a.log", "decision": "keep", "why": "w"}, "top-level name"),
    ({"item": "sub/a.log", "decision": "keep", "why": "w"}, "top-level name"),
    ({"item": "drained", "decision": "keep", "why": "w"}, "top-level name"),
    ("not-an-object", "not an object"),
])
def test_decide_refuses_an_invalid_record(temp, capsys, monkeypatch, record, needle):
    _write(temp, "a.log")
    rc, out = _decide(temp, [record], capsys, monkeypatch)
    assert rc == 1 and out["recorded"] == 0
    assert any(needle in e for e in out["errors"]), out["errors"]
    assert not td.ledger_path(temp).exists()


def test_decide_is_all_or_nothing(temp, capsys, monkeypatch):
    _write(temp, "good.log")
    rc, out = _decide(temp, [
        {"item": "good.log", "decision": "discard", "why": "w"},
        {"item": "good.log", "decision": "keep", "why": "w"},
    ], capsys, monkeypatch)
    assert rc == 1 and any("listed twice" in e for e in out["errors"])
    assert _rows(temp) == []


def test_decide_refuses_a_git_tracked_file(temp, capsys, monkeypatch):
    _write(temp, "t.md")
    monkeypatch.setattr(td, "tracked_names", lambda temp_dir: {"t.md"})
    rc, out = _decide(temp, [{"item": "t.md", "decision": "keep", "why": "w"}], capsys, monkeypatch)
    assert rc == 1 and "git-tracked" in out["errors"][0]


def test_decide_refuses_everything_when_tracked_set_unknown(temp, capsys, monkeypatch):
    _write(temp, "a.log")
    monkeypatch.setattr(td, "tracked_names", lambda temp_dir: None)
    rc, out = _decide(temp, [{"item": "a.log", "decision": "keep", "why": "w"}], capsys, monkeypatch)
    assert rc == 3 and out["recorded"] == 0 and _rows(temp) == []


# ── discard refusals: never record a discard the purge could not execute ─────

def test_discard_of_a_cited_item_is_refused(temp, capsys, monkeypatch):
    _write(temp, "evidence.log")
    monkeypatch.setattr(td, "cited_index",
                        lambda: {"evidence.log": ["agents/x/temp/evidence.log"]})
    rc, out = _decide(temp, [{"item": "evidence.log", "decision": "discard", "why": "w"}],
                      capsys, monkeypatch)
    assert rc == 1 and "cited by a durable record" in out["errors"][0]
    rc, out = _decide(temp, [{"item": "evidence.log", "decision": "keep", "why": "cited"}],
                      capsys, monkeypatch)
    assert rc == 0


def test_a_folder_whose_contents_are_cited_counts_as_cited(temp, monkeypatch):
    # The real index keys a cited path by basename AND by its first segment
    # after /temp/, so citing a file inside a folder protects the folder.
    monkeypatch.setattr(td, "cited_index", lambda: {"proj": ["agents/x/temp/proj/out.txt"],
                                                     "out.txt": ["agents/x/temp/proj/out.txt"]})
    assert td.citing_paths("proj", td.cited_index()) == ["agents/x/temp/proj/out.txt"]
    assert td.citing_paths("proj-2", td.cited_index()) == []


def test_discard_is_refused_while_the_cited_set_is_unknown(temp, capsys, monkeypatch):
    _write(temp, "a.log")
    _write(temp, "b.md")
    monkeypatch.setattr(td, "cited_index", lambda: None)
    rc, out = _decide(temp, [{"item": "a.log", "decision": "discard", "why": "w"}],
                      capsys, monkeypatch)
    assert rc == 1 and "cited set is unknown" in out["errors"][0]
    # Only a discard needs the cited set.
    rc, out = _decide(temp, [{"item": "b.md", "decision": "keep", "why": "w"}], capsys, monkeypatch)
    assert rc == 0


@pytest.mark.parametrize("marker", ["RECEIPT", "receipt.md", "Receipt.json", ".archive-marker"])
def test_discard_of_a_receipted_folder_is_refused(temp, capsys, monkeypatch, marker):
    _write(temp, f"arc/{marker}", "restore notes\n")
    rc, out = _decide(temp, [{"item": "arc", "decision": "discard", "why": "w"}], capsys, monkeypatch)
    assert rc == 1 and "receipted archive" in out["errors"][0]


def test_a_receipt_lookalike_does_not_protect_a_folder(temp, capsys, monkeypatch):
    _write(temp, "scratch/old-receipt-notes.txt")
    rc, _ = _decide(temp, [{"item": "scratch", "decision": "discard", "why": "w"}], capsys, monkeypatch)
    assert rc == 0


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_discard_of_a_folder_with_unpushed_git_work_is_refused(temp, capsys, monkeypatch):
    repo = temp / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "c1", cwd=repo)
    rc, out = _decide(temp, [{"item": "repo", "decision": "discard", "why": "w"}], capsys, monkeypatch)
    assert rc == 1 and "git repo with work" in out["errors"][0]
    assert '"unpushed": true' in out["errors"][0]


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_reading_git_state_never_writes_to_the_repo(temp, tmp_path, capsys, monkeypatch):
    """A pushed, clean repo whose files are stat-stale (aged after commit): a
    plain `git status` rewrites .git/index, which moved the folder's
    fingerprint so its discard stopped applying, and made it look in flight."""
    bare = tmp_path / "bare.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    repo = temp / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "c1", cwd=repo)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    assert _git("push", "-q", "origin", "HEAD", cwd=repo).returncode == 0
    _age(repo)
    index_before = os.stat(repo / ".git" / "index").st_mtime_ns
    rc, _ = _decide(temp, [{"item": "repo", "decision": "discard", "why": "w"}], capsys, monkeypatch)
    assert rc == 0
    # A detailed census re-checks the discard (git included) ...
    rc, _, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    assert rc == 0
    # ... and the decision still applies, because nothing in the repo moved.
    assert os.stat(repo / ".git" / "index").st_mtime_ns == index_before
    rc, out, _ = _run(temp, ["deletable"], capsys, monkeypatch)
    assert rc == 0 and out == "dir\trepo\n"
    assert not td.is_fresh(repo, "dir", td.DEFAULT_AGE_MIN)


# ── the decision in force ────────────────────────────────────────────────────

def test_current_decision_is_the_latest_after_the_last_deletion():
    rows = [
        {"item": "a", "decision": "keep", "why": "1"},
        {"item": "a", "decision": "discard", "why": "2"},
        {"item": "b", "decision": "discard", "why": "3"},
        {"item": "b", "event": "deleted", "lane": "decided"},
        {"item": "c", "decision": "discard", "why": "4"},
        {"item": "c", "event": "deleted", "lane": "decided"},
        {"item": "c", "decision": "keep", "why": "5"},
        {"item": "d", "decision": "not-a-decision"},
        {"decision": "keep", "why": "no item"},
    ]
    cur = td.current_decisions(rows)
    assert {k: v["why"] for k, v in cur.items()} == {"a": "2", "c": "5"}


def test_read_ledger_skips_and_counts_bad_lines(temp):
    td.ledger_path(temp).write_text(
        '{"item":"a","decision":"keep","why":"w"}\n{torn\n[1,2]\n\n'
        '{"item":"b","decision":"keep","why":"w"}\n', encoding="utf-8")
    rows, bad = td.read_ledger(temp)
    assert [r["item"] for r in rows] == ["a", "b"] and bad == 2


def test_an_edit_after_review_puts_a_file_back_up(temp, capsys, monkeypatch):
    _write(temp, "a.txt", "v1\n")
    assert _decide(temp, [{"item": "a.txt", "decision": "discard", "why": "w"}],
                   capsys, monkeypatch)[0] == 0
    _write(temp, "a.txt", "v2\n")
    rc, out, _ = _run(temp, ["deletable"], capsys, monkeypatch)
    assert rc == 0 and out == ""
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    items = json.loads(out)["items"]
    assert [(i["name"], i["why_pending"]) for i in items] == [("a.txt", "changed-since-decision")]


def test_a_new_file_in_a_reviewed_folder_puts_it_back_up(temp, capsys, monkeypatch):
    _write(temp, "d/one.txt")
    _age(temp / "d")
    assert _decide(temp, [{"item": "d", "decision": "discard", "why": "w"}],
                   capsys, monkeypatch)[0] == 0
    assert _run(temp, ["deletable"], capsys, monkeypatch)[1] == "dir\td\n"
    _write(temp, "d/two.txt")
    _age(temp / "d")
    assert _run(temp, ["deletable"], capsys, monkeypatch)[1] == ""


def test_a_name_reused_by_another_kind_is_not_deletable(temp, capsys, monkeypatch):
    _write(temp, "x")
    assert _decide(temp, [{"item": "x", "decision": "discard", "why": "w"}],
                   capsys, monkeypatch)[0] == 0
    (temp / "x").unlink()
    _write(temp, "x/inner.txt")
    assert _run(temp, ["deletable"], capsys, monkeypatch)[1] == ""


def test_keep_expires_and_comes_back(temp, capsys, monkeypatch):
    p = _write(temp, "a.md")
    fp, size, _ = td.stats(p, "file")
    old = (datetime.now(timezone.utc) - timedelta(days=td.KEEP_DAYS + 1)).strftime(td._TS_FMT)
    td.append_rows(temp, [{"ts": old, "item": "a.md", "kind": "file", "bytes": size,
                           "fp": fp, "decision": "keep", "why": "w", "by": "t", "host": "h"}])
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    assert [i["why_pending"] for i in json.loads(out)["items"]] == ["keep-expired"]
    assert td.pressure_counts(temp)["pending"] == 1
    assert td.keep_expired({"ts": "not-a-time"})
    assert not td.keep_expired({"ts": td._now_ts()})


def test_an_unexecuted_move_stays_pending(temp, capsys, monkeypatch):
    _write(temp, "tool.sh", "echo hi\n")
    assert _decide(temp, [{"item": "tool.sh", "decision": "promote", "why": "w",
                           "where": "world/scripts/tool.sh"}], capsys, monkeypatch)[0] == 0
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    item = json.loads(out)["items"][0]
    assert item["why_pending"] == "decided-not-executed"
    assert item["last_decision"]["where"] == "world/scripts/tool.sh"
    assert _run(temp, ["deletable"], capsys, monkeypatch)[1] == ""


def test_a_discard_cited_after_the_decision_comes_back_for_review(temp, capsys, monkeypatch):
    _write(temp, "a.log")
    assert _decide(temp, [{"item": "a.log", "decision": "discard", "why": "w"}],
                   capsys, monkeypatch)[0] == 0
    monkeypatch.setattr(td, "cited_index", lambda: {"a.log": ["world/x/node.md"]})
    rc, out, err = _run(temp, ["deletable"], capsys, monkeypatch)
    assert out == "" and "CITED" in err
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    doc = json.loads(out)
    assert doc["summary"]["awaiting_purge"] == 0 and doc["summary"]["pending"] == 1
    assert doc["items"][0]["why_pending"].startswith("discard-blocked: cited by a durable record")
    # The cheap count does not look citations up (documented under-count).
    assert td.pressure_counts(temp)["awaiting_purge"] == 1


# ── in flight, pressure and the census ───────────────────────────────────────

def test_newest_mtime_sees_any_depth(temp):
    _write(temp, "d/a/b/deep.txt", age=0)
    _age(temp / "d" / "a")
    os.utime(temp / "d", (time.time() - 3 * 3600,) * 2)
    os.utime(temp / "d" / "a" / "b" / "deep.txt", None)   # fresh, three levels down
    assert td.is_fresh(temp / "d", "dir", 120)
    _age(temp / "d")
    assert not td.is_fresh(temp / "d", "dir", 120)
    cutoff = time.time() - 60
    os.utime(temp / "d" / "a", None)
    assert td.newest_mtime(temp / "d", "dir", cutoff=cutoff) > cutoff


def test_pressure_counts_the_review_population(temp, capsys, monkeypatch):
    _write(temp, "old.md")
    _write(temp, "old.sh")
    _write(temp, "old.log")
    _write(temp, "empty.txt", "")
    _write(temp, "other.bin")
    _write(temp, "dir/f")
    _age(temp / "dir")
    _write(temp, "fresh.md", age=0)
    _write(temp, "nested/sub/f", age=0)
    _age(temp / "nested" / "sub")
    _age(temp / "nested")
    os.utime(temp / "nested" / "sub" / "f", None)          # folder aged, entry fresh
    _write(temp, "arc/RECEIPT.md")
    _write(temp, ".dotfile")
    _write(temp, "drained/x.md")
    _write(temp, "kept.md")
    _write(temp, "gone.log")
    assert _decide(temp, [{"item": "kept.md", "decision": "keep", "why": "w"},
                          {"item": "gone.log", "decision": "discard", "why": "w"}],
                   capsys, monkeypatch)[0] == 0
    p = td.pressure_counts(temp)
    assert p["pending"] == 6
    assert p["by_class"] == {"doc": 1, "script": 1, "run-output": 1, "empty": 1,
                             "other": 1, "dir": 1}
    assert (p["fresh"], p["kept"], p["awaiting_purge"], p["receipted_dirs"]) == (2, 1, 1, 1)
    # The detailed census agrees with the cheap count.
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    s = json.loads(out)["summary"]
    assert (s["pending"], s["fresh"], s["kept"], s["awaiting_purge"], s["receipted_dirs"]) == \
           (6, 2, 1, 1, 1)
    assert s["by_class"] == p["by_class"]


def test_pressure_skips_tracked_files(temp, monkeypatch):
    _write(temp, "t.md")
    _write(temp, "u.md")
    monkeypatch.setattr(td, "tracked_names", lambda temp_dir: {"t.md"})
    p = td.pressure_counts(temp)
    assert p["pending"] == 1 and p["tracked_skipped"] == 1


@pytest.mark.skipif(os.name == "nt", reason="needs control characters / symlinks in names")
def test_unroundtrippable_names_and_symlinks_are_counted_never_governed(temp, capsys, monkeypatch):
    _write(temp, "bad\tname.txt")
    os.symlink(str(temp / "bad\tname.txt"), str(temp / "link.txt"))
    p = td.pressure_counts(temp)
    assert (p["pending"], p["bad_names"], p["symlinks_skipped"]) == (0, 1, 1)
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    assert json.loads(out)["summary"]["bad_names"] == [ascii("bad\tname.txt")]


def test_pending_flags_twins_and_existing_script_homes(temp, capsys, monkeypatch):
    _write(temp, "a.md", "same\n")
    _write(temp, "b.md", "same\n")
    _write(temp, "drained/a.md", "same\n")
    _write(temp, "temp-decisions.sh", "not the real wrapper\n")
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    items = {i["name"]: i for i in json.loads(out)["items"]}
    assert "b.md" in items["a.md"]["same_as"] and "drained/a.md" in items["a.md"]["same_as"]
    assert "core/scripts/temp-decisions.sh (differs)" in items["temp-decisions.sh"]["same_as"]
    assert items["a.md"]["cited"] is False and items["a.md"]["head"] == "same"


def test_pending_hides_in_flight_items_unless_asked(temp, capsys, monkeypatch):
    _write(temp, "new.md", age=0)
    rc, out, _ = _run(temp, ["pending", "--json"], capsys, monkeypatch)
    doc = json.loads(out)
    assert doc["items"] == [] and doc["summary"]["fresh"] == 1
    rc, out, _ = _run(temp, ["pending", "--json", "--include-fresh"], capsys, monkeypatch)
    assert json.loads(out)["items"][0]["fresh"] is True


# ── bulk-junk ────────────────────────────────────────────────────────────────

def test_bulk_junk_decides_only_aged_empties_and_run_output(temp, capsys, monkeypatch):
    _write(temp, "empty.md", "")
    _write(temp, "run.log", "out\n")
    _write(temp, "run.OUT", "out\n")
    _write(temp, "notes.txt", "keep me\n")
    _write(temp, "tool.sh", "echo\n")
    _write(temp, "fresh.log", "out\n", age=0)
    _write(temp, "fresh-empty.txt", "", age=0)
    _write(temp, "cited.err", "out\n")
    _write(temp, "tracked.raw", "out\n")
    _write(temp, "dir.log/inner.log", "out\n")
    _write(temp, ".hidden.log", "out\n")
    monkeypatch.setattr(td, "cited_index", lambda: {"cited.err": ["world/x.md"]})
    monkeypatch.setattr(td, "tracked_names", lambda temp_dir: {"tracked.raw"})
    rc, out, _ = _run(temp, ["bulk-junk", "--dry-run"], capsys, monkeypatch)
    assert rc == 0 and _rows(temp) == []
    rc, out, _ = _run(temp, ["bulk-junk"], capsys, monkeypatch)
    doc = json.loads(out)
    assert sorted(i["item"] for i in doc["items"]) == ["empty.md", "run.OUT", "run.log"]
    assert doc["skipped"] == {"cited": 1, "tracked": 1, "too_fresh": 2, "decided": 0}
    rows = {r["item"]: r for r in _rows(temp)}
    assert rows["empty.md"]["why"] == "bulk: empty file"
    assert rows["run.log"]["why"].startswith("bulk: run output (.log)")
    assert all(r["by"] == "bulk-junk" and r["decision"] == "discard" for r in rows.values())
    # A second pass records nothing new.
    rc, out, _ = _run(temp, ["bulk-junk"], capsys, monkeypatch)
    assert json.loads(out)["decided"] == 0 and len(_rows(temp)) == 3


def test_bulk_junk_never_overrides_a_reviewers_keep(temp, capsys, monkeypatch):
    _write(temp, "probe.log", "evidence\n")
    assert _decide(temp, [{"item": "probe.log", "decision": "keep", "why": "evidence"}],
                   capsys, monkeypatch)[0] == 0
    rc, out, _ = _run(temp, ["bulk-junk"], capsys, monkeypatch)
    assert json.loads(out)["decided"] == 0
    assert td.current_decisions(_rows(temp))["probe.log"]["decision"] == "keep"


def test_bulk_junk_decides_nothing_it_cannot_check(temp, capsys, monkeypatch):
    _write(temp, "run.log")
    monkeypatch.setattr(td, "cited_index", lambda: None)
    rc, out, _ = _run(temp, ["bulk-junk"], capsys, monkeypatch)
    assert rc == 3 and json.loads(out)["decided"] == 0 and _rows(temp) == []


# ── deletable / log-deleted / show ───────────────────────────────────────────

def test_deletable_lists_matching_discards_as_utf8_bytes(temp, capsysbinary, monkeypatch):
    _write(temp, "ünï.log")
    _write(temp, "keep.md")
    _write(temp, "d/f")
    _age(temp / "d")
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps([
        {"item": "ünï.log", "decision": "discard", "why": "w"},
        {"item": "keep.md", "decision": "keep", "why": "w"},
        {"item": "d", "decision": "discard", "why": "w"}])))
    assert td.main(["--temp-dir", str(temp), "decide"]) == 0
    capsysbinary.readouterr()
    assert td.main(["--temp-dir", str(temp), "deletable"]) == 0
    out = capsysbinary.readouterr().out
    assert sorted(out.split(b"\n")) == sorted([b"", b"dir\td", "file\tünï.log".encode("utf-8")])


@pytest.mark.parametrize("which", ["cited", "tracked"])
def test_deletable_lists_nothing_when_a_lookup_fails(temp, capsys, monkeypatch, which):
    _write(temp, "a.log")
    assert _decide(temp, [{"item": "a.log", "decision": "discard", "why": "w"}],
                   capsys, monkeypatch)[0] == 0
    if which == "cited":
        monkeypatch.setattr(td, "cited_index", lambda: None)
    else:
        monkeypatch.setattr(td, "tracked_names", lambda temp_dir: None)
    rc, out, err = _run(temp, ["deletable"], capsys, monkeypatch)
    assert rc == 3 and out == "" and "fail-closed" in err


def test_log_deleted_records_only_what_is_really_gone(temp, capsys, monkeypatch):
    _write(temp, "a.log")
    _write(temp, "b.log")
    assert _decide(temp, [{"item": "a.log", "decision": "discard", "why": "finished run"},
                          {"item": "b.log", "decision": "discard", "why": "w"}],
                   capsys, monkeypatch)[0] == 0
    fp_a = td.current_decisions(_rows(temp))["a.log"]["fp"]
    (temp / "a.log").unlink()
    rc, out, _ = _run(temp, ["log-deleted", "--lane", "decided"], capsys, monkeypatch,
                      "file\ta.log\nfile\tb.log\nfile\t../escape\nfile\t/abs\nno-tab-line\n")
    assert rc == 0 and json.loads(out) == {"logged": 1, "still_present": 1}
    row = _rows(temp)[-1]
    assert (row["item"], row["event"], row["lane"], row["why"], row["fp"]) == \
           ("a.log", "deleted", "decided", "finished run", fp_a)
    assert set(td.current_decisions(_rows(temp))) == {"b.log"}


def test_log_deleted_other_lanes_carry_the_given_reason(temp, capsys, monkeypatch):
    rc, out, _ = _run(temp, ["log-deleted", "--lane", "drained-gc", "--why", "retention"],
                      capsys, monkeypatch, "file\tdrained/old.md\n")
    assert json.loads(out)["logged"] == 1
    rc, out, _ = _run(temp, ["log-deleted", "--lane", "migration"], capsys, monkeypatch,
                      "file\t.drain-watermark\n")
    rows = _rows(temp)
    assert [(r["item"], r["lane"], r["why"]) for r in rows] == [
        ("drained/old.md", "drained-gc", "retention"),
        (".drain-watermark", "migration", "migration")]


def test_show_filters_and_formats(temp, capsys, monkeypatch):
    _write(temp, "a.log")
    _write(temp, "b.md")
    assert _decide(temp, [{"item": "a.log", "decision": "discard", "why": "finished run"},
                          {"item": "b.md", "decision": "encode", "why": "lesson",
                           "where": "rb-9"}], capsys, monkeypatch)[0] == 0
    (temp / "a.log").unlink()
    _run(temp, ["log-deleted", "--lane", "decided"], capsys, monkeypatch, "file\ta.log\n")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(td._TS_FMT)
    td.append_rows(temp, [{"ts": old, "item": "z.log", "event": "deleted", "lane": "decided",
                           "why": "old", "by": "t", "host": "h"}])
    rc, out, _ = _run(temp, ["show", "--deleted"], capsys, monkeypatch)
    lines = out.strip().splitlines()
    assert len(lines) == 2 and "a.log [decided]  -- finished run" in lines[0]
    rc, out, _ = _run(temp, ["show", "--deleted", "--since", "1d"], capsys, monkeypatch)
    assert "z.log" not in out and "a.log" in out
    rc, out, _ = _run(temp, ["show", "--item", "b.md"], capsys, monkeypatch)
    assert "encode" in out and "-> rb-9" in out and "a.log" not in out
    rc, out, _ = _run(temp, ["show", "--json", "--limit", "1"], capsys, monkeypatch)
    doc = json.loads(out)
    assert doc["matched"] == 1 and doc["rows"][0]["item"] == "z.log"
    rc, out, _ = _run(temp, ["show", "--item", "nothing"], capsys, monkeypatch)
    assert out.strip() == "(no matching rows)"


# ── environment ──────────────────────────────────────────────────────────────

def test_a_missing_temp_dir(tmp_path, capsys, monkeypatch):
    missing = tmp_path / "gone" / "temp"
    assert _run(missing, ["deletable"], capsys, monkeypatch)[:2] == (0, "")
    rc, out, _ = _run(missing, ["pressure"], capsys, monkeypatch)
    assert rc == 0 and "does not exist" in out
    assert _run(missing, ["decide"], capsys, monkeypatch, "[]")[0] == 2


def test_a_non_temp_dir_is_refused(tmp_path, capsys, monkeypatch):
    rc, _, err = _run(tmp_path, ["pending"], capsys, monkeypatch)
    assert rc == 2 and "no safe temp dir" in err


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_tracked_names_reads_depth_one_tracked_files(tmp_path):
    repo = tmp_path / "repo"
    (repo / "temp" / "sub").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    (repo / "temp" / "t.md").write_text("x\n", encoding="utf-8")
    (repo / "temp" / "sub" / "deep.md").write_text("x\n", encoding="utf-8")
    (repo / "temp" / "untracked.md").write_text("x\n", encoding="utf-8")
    _git("add", "temp/t.md", "temp/sub/deep.md", cwd=repo)
    assert td.tracked_names(repo / "temp") == {"t.md"}
