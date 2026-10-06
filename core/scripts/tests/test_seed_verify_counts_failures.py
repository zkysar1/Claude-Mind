"""seed-verify.sh must COUNT a failing check ().

`run_check` read `$?` inside `if ! cmd; then rc=$?`, where `$?` is the status of
the NEGATED command and is always 0. So checks 1/2/3/5/8 (completeness, leak,
cruft, integrity, exec bits) printed FAIL or WARN while the summary read
`FAILS: 0` and the promotion reported PROMOTED -- on a gate that
promote-to-upstream.sh names as the ONLY basis for merging a promote PR.

Counting the failure alone would stop every promotion: the plant's own commit
fires the destination's post-commit hook, which restarts the daemon rooted at
the (throwaway) plant worktree and creates `mind_api/state/` there, and
`core/.pycache/` appears the same way. Checks 2/3 flagged those on the live
v2.12.80 hop. They are gitignored at the destination, so they cannot ship; the
checks now ask git, not only the filesystem. Both halves are pinned here.

How the assertions are built (guard-2066, guard-3991, rb-9056):
  * every check asserts its SPECIFIC id's block and the exact counters and exit
    code, never `rc != 0`;
  * every counting test first runs a copy of the script with the PRE-FIX idiom
    restored on the same fixture and asserts the check printed FAIL/WARN yet the
    summary read 0 -- the positive control, so a green test cannot be a fixture
    that never failed;
  * the scoping half carries the arm that proves the FAIL path still fires
    (tracked, untracked-not-ignored, non-git, git-cannot-answer).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash")
import _seed_engine as E  # noqa: E402

VERIFY_SH = CORE_SCRIPTS / "seed-verify.sh"

# seed-verify.sh hardcodes `--source "$PROJECT_ROOT"`, so the destination is built
# from REAL files of this repo. The samples are the ones do_verify_integrity
# hashes (source post-transform vs destination); with `transformations: []` a
# byte copy matches.
INCLUDE_RULE = ".claude/rules/first-principles.md"
EXEC_PATH = "core/scripts/check-prerequisites.sh"
SAMPLES = ["CLAUDE.md", "core/scripts/_paths.sh", ".claude/settings.json",
           "core/config/tree.yaml", "mind_api/src/server.py"]
MANIFEST_YAML = f"""\
include:
  - path: {INCLUDE_RULE}
    type: file
    required: true
  - path: {EXEC_PATH}
    type: file
    required: true
exclude_always:
  - mind_api/state/
  - mind_api/bench/
cruft_patterns:
  - core/logs/
  - core/.pycache/
transformations: []
"""
CHECK_IDS = "12345678"


def _git(dest: Path, *args, check=True):
    return subprocess.run(["git", "-C", dest.as_posix(), *args],
                          capture_output=True, text=True, check=check)


def _commit(dest: Path, msg: str):
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", msg)


def _source_is_exec(rel: str):
    """The SOURCE index mode, which is what check 8 compares against."""
    r = subprocess.run(["git", "-C", PROJECT_ROOT.as_posix(), "ls-files", "-s", "--", rel],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split()[0] == "100755"


def _make_dest(tmp_path: Path, *, git: bool = True):
    """A destination that every check passes, so each test's single change is the
    only thing that can move a counter."""
    dest = tmp_path / "dest"
    for rel in [INCLUDE_RULE, *SAMPLES]:
        src = PROJECT_ROOT / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / rel)
    # Check 6 runs the destination's own prerequisites script. A stub keeps the
    # fixture independent of what this box happens to have installed.
    stub = dest / EXEC_PATH
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    # Disk modes follow the SOURCE INDEX, so a checkout that lost its exec bits
    # (core.fileMode=false) cannot turn check 8 red in the baseline.
    for rel in [INCLUDE_RULE, EXEC_PATH, *SAMPLES]:
        if (dest / rel).is_file():
            os.chmod(dest / rel, 0o755 if _source_is_exec(rel) else 0o644)
    # The staging repo ignores these two; so does this destination.
    (dest / ".gitignore").write_text("/mind_api/state/\ncore/.pycache/\n", encoding="utf-8")
    if git:
        _git(dest, "init", "-q")
        _git(dest, "config", "user.email", "seed-test@example.invalid")
        _git(dest, "config", "user.name", "seed-test")
        _commit(dest, "plant")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    return dest, manifest


def _run(dest: Path, manifest: Path, *, script: Path = VERIFY_SH, expect_commit: bool = True):
    extra = ("--expect-commit",) if expect_commit else ()
    r = subprocess.run(bash_cmd(script, dest.as_posix(), "--manifest", manifest.as_posix(), *extra),
                       capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def _block(out: str, cid: str) -> str:
    m = re.search(rf"(?ms)^\[{cid}\] .*?(?=^\[\d\] |^\[seed-verify\] SUMMARY)", out)
    assert m, f"no [{cid}] block in the output:\n{out[-2500:]}"
    return m.group(0)


def _status(out: str, cid: str) -> str:
    lines = [ln.strip() for ln in _block(out, cid).splitlines()[1:]]
    for word in ("FAIL", "WARN", "SKIP"):
        if any(ln.startswith(word) for ln in lines):
            return word
    return "PASS" if any("PASS" in ln for ln in lines) else "INFO"


def _counts(out: str):
    f = re.search(r"FAILS: (\d+)", out)
    w = re.search(r"WARNS: (\d+)", out)
    assert f and w, f"no SUMMARY counters in the output:\n{out[-1500:]}"
    return int(f.group(1)), int(w.group(1))


def _pre_fix_copy(tmp_path: Path) -> Path:
    """seed-verify.sh with the shipped defect restored, resolving its helpers from
    the real scripts dir. Built by exact replacement so a refactor of the fixed
    line fails HERE, loudly, instead of silently turning the control into a copy
    of the fixed script."""
    src = VERIFY_SH.read_text(encoding="utf-8")
    anchor = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    fixed = '$PYLAUNCH "$SCRIPT_DIR/_seed_verify_format.py" "$check_id" "$@" 2>&1 || rc=$?'
    old = ('if ! $PYLAUNCH "$SCRIPT_DIR/_seed_verify_format.py" "$check_id" "$@" 2>&1; then\n'
           '        rc=$?\n    fi')
    assert src.count(anchor) == 1 and src.count(fixed) == 1, (
        "seed-verify.sh no longer has the exact lines this positive control rewrites; "
        "update _pre_fix_copy to the new run_check shape")
    out = tmp_path / "seed-verify-pre-fix.sh"
    out.write_text(src.replace(anchor, f'SCRIPT_DIR="{CORE_SCRIPTS.as_posix()}"').replace(fixed, old),
                   encoding="utf-8")
    return out


# --- one change per check, each committed so check 4 stays clean ----------------

def _m_missing_include(dest: Path):                      # check 1
    _git(dest, "rm", "-q", INCLUDE_RULE)
    _git(dest, "commit", "-q", "-m", "drop a required include")


def _m_tracked_leak(dest: Path):                         # check 2 (tracked, past an ignore rule)
    (dest / "mind_api/state").mkdir(parents=True)
    (dest / "mind_api/state/leak.txt").write_text("leak\n", encoding="utf-8")
    _git(dest, "add", "-f", "mind_api/state/leak.txt")
    _git(dest, "commit", "-q", "-m", "a tracked excluded path")


def _m_tracked_cruft(dest: Path):                        # check 3
    (dest / "core/logs").mkdir(parents=True)
    (dest / "core/logs/x.log").write_text("x\n", encoding="utf-8")
    _commit(dest, "tracked cruft")


def _m_sample_drift(dest: Path):                         # check 5
    with open(dest / "CLAUDE.md", "a", encoding="utf-8") as fh:
        fh.write("\ndrift\n")
    _commit(dest, "a sampled file diverges from the source")


def _m_stripped_exec(dest: Path):                        # check 8 (the v2.12.47 shape)
    if _source_is_exec(EXEC_PATH) is not True:
        pytest.skip(f"the source index does not carry {EXEC_PATH} as 100755 here")
    _git(dest, "update-index", "--chmod=-x", EXEC_PATH)
    _git(dest, "commit", "-q", "-m", "exec bit lost in the destination index")
    os.chmod(dest / EXEC_PATH, 0o644)


def test_baseline_destination_fails_no_check(tmp_path):
    dest, manifest = _make_dest(tmp_path)
    rc, out = _run(dest, manifest)
    assert rc == 0, out[-3000:]
    assert _counts(out)[0] == 0
    for cid in "123458":
        assert _status(out, cid) == "PASS", (cid, _block(out, cid))


@pytest.mark.parametrize("cid,mutate", [("1", _m_missing_include), ("2", _m_tracked_leak),
                                         ("8", _m_stripped_exec)], ids=["check1", "check2", "check8"])
def test_a_failing_fail_kind_check_is_counted(tmp_path, cid, mutate):
    dest, manifest = _make_dest(tmp_path)
    mutate(dest)

    # Positive control: the SAME fixture under the pre-fix idiom. The check says
    # FAIL, the summary says nothing happened -- the defect, reproduced.
    rc0, out0 = _run(dest, manifest, script=_pre_fix_copy(tmp_path))
    assert _status(out0, cid) == "FAIL", _block(out0, cid)
    assert _counts(out0)[0] == 0 and rc0 == 0, (rc0, out0[-1500:])

    rc, out = _run(dest, manifest)
    assert _status(out, cid) == "FAIL", _block(out, cid)
    assert _counts(out)[0] == 1 and rc == 1, (rc, out[-1500:])
    assert "Status: FAIL" in out
    assert [c for c in CHECK_IDS if c != cid and _status(out, c) == "FAIL"] == []


@pytest.mark.parametrize("cid,mutate", [("3", _m_tracked_cruft), ("5", _m_sample_drift)],
                         ids=["check3", "check5"])
def test_a_failing_warn_kind_check_raises_warns_without_failing(tmp_path, cid, mutate):
    base_dest, base_manifest = _make_dest(tmp_path / "base")
    _, base_out = _run(base_dest, base_manifest)
    w0 = _counts(base_out)[1]          # check 7 may legitimately WARN on a copy of real files

    dest, manifest = _make_dest(tmp_path / "bad")
    mutate(dest)

    rc0, out0 = _run(dest, manifest, script=_pre_fix_copy(tmp_path))
    assert _status(out0, cid) == "WARN", _block(out0, cid)
    assert _counts(out0)[1] == w0 and rc0 == 0, (w0, out0[-1500:])

    rc, out = _run(dest, manifest)
    assert _status(out, cid) == "WARN", _block(out, cid)
    assert _counts(out) == (0, w0 + 1) and rc == 0, (w0, out[-1500:])
    assert "Status: WARN" in out


# --- the coupling: runtime state the plant's own commit creates -----------------

def test_runtime_state_created_after_the_plant_commit_is_not_a_leak(tmp_path, monkeypatch):
    dest, manifest = _make_dest(tmp_path)
    (dest / "mind_api/state").mkdir(parents=True)
    (dest / "mind_api/state/daemon.log").write_text("x\n", encoding="utf-8")
    (dest / "core/.pycache").mkdir(parents=True)
    (dest / "core/.pycache/x.pyc").write_bytes(b"x")

    rc, out = _run(dest, manifest)
    assert rc == 0 and _counts(out)[0] == 0, out[-2500:]
    assert _status(out, "2") == "PASS" and _status(out, "3") == "PASS"
    # Suppressed, not hidden: the paths stay visible as INFO lines.
    assert ("INFO: mind_api/state/ present at destination but gitignored and untracked: "
            "cannot ship") in _block(out, "2")
    assert "INFO: core/.pycache/ present at destination but gitignored and untracked" in _block(out, "3")

    # Positive control: take the scoping away and this identical fixture is the
    # leak check 2 flagged on the live hop -- so the scoping is what makes it pass.
    manifest_dict = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    monkeypatch.setattr(E, "_any_can_ship", lambda *_a, **_k: True)
    res = E.do_verify_leak_check(dest, manifest_dict)
    assert res["leaked"] == ["mind_api/state/"] and res["ignored"] == [], res
    assert E.do_verify_cruft(dest, manifest_dict)["present"] == ["core/.pycache/"]


def test_untracked_not_ignored_excluded_path_still_leaks(tmp_path):
    dest, manifest = _make_dest(tmp_path)
    (dest / "mind_api/bench").mkdir(parents=True)
    (dest / "mind_api/bench/x.txt").write_text("x\n", encoding="utf-8")
    # No --expect-commit: the dirty tree is informational there, so check 2 is the
    # only check that can fail.
    rc, out = _run(dest, manifest, expect_commit=False)
    assert _status(out, "2") == "FAIL" and "- mind_api/bench/" in _block(out, "2"), _block(out, "2")
    assert _counts(out)[0] == 1 and rc == 1, out[-1500:]


def test_non_git_destination_keeps_the_filesystem_verdict(tmp_path):
    dest, manifest = _make_dest(tmp_path, git=False)
    (dest / "mind_api/state").mkdir(parents=True)
    (dest / "mind_api/state/x").write_text("x\n", encoding="utf-8")
    rc, out = _run(dest, manifest, expect_commit=False)
    assert _status(out, "2") == "FAIL" and "- mind_api/state/" in _block(out, "2"), _block(out, "2")
    assert _counts(out)[0] == 1 and rc == 1, out[-1500:]


def test_a_git_destination_that_cannot_answer_keeps_the_filesystem_verdict(tmp_path):
    dest = tmp_path / "dest"
    (dest / "mind_api/state").mkdir(parents=True)
    (dest / "mind_api/state/x").write_text("x\n", encoding="utf-8")
    (dest / "core/.pycache").mkdir(parents=True)
    (dest / "core/.pycache/x.pyc").write_bytes(b"x")
    # A worktree pointer at a gitdir that does not exist: git exits 128 for every
    # query, which must read as "unknown", never as "cannot ship".
    (dest / ".git").write_text("gitdir: " + (tmp_path / "no-such-gitdir").as_posix() + "\n",
                               encoding="utf-8")
    probe = _git(dest, "ls-files", check=False)
    assert probe.returncode != 0, "the broken gitdir pointer must make git fail"
    manifest = yaml.safe_load(MANIFEST_YAML)
    leak = E.do_verify_leak_check(dest, manifest)
    assert leak["leaked"] == ["mind_api/state/"] and leak["ignored"] == [], leak
    cruft = E.do_verify_cruft(dest, manifest)
    assert cruft["present"] == ["core/.pycache/"] and cruft["ignored"] == [], cruft


def _plain_git_dest(tmp_path: Path, ignore: str) -> Path:
    dest = tmp_path / "dest"
    dest.mkdir()
    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "seed-test@example.invalid")
    _git(dest, "config", "user.name", "seed-test")
    (dest / ".gitignore").write_text(ignore, encoding="utf-8")
    _commit(dest, "base")
    return dest


def test_glob_patterns_are_scoped_per_match_too(tmp_path):
    # leak check: top-level glob; cruft check: `**/` rglob. One pattern each, so
    # each branch of both functions runs against ignored AND tracked matches.
    dest = _plain_git_dest(tmp_path, "*.pyc\n*.orig\n")
    (dest / "stray.pyc").write_bytes(b"x")
    (dest / "a/b").mkdir(parents=True)
    (dest / "a/b/x.orig").write_text("x\n", encoding="utf-8")
    leak_manifest = {"exclude_always": ["*.pyc"]}
    cruft_manifest = {"cruft_patterns": ["**/*.orig"]}

    leak = E.do_verify_leak_check(dest, leak_manifest)
    assert (leak["leaked"], leak["ignored"]) == ([], ["*.pyc"]), leak
    cruft = E.do_verify_cruft(dest, cruft_manifest)
    assert (cruft["present"], cruft["ignored"]) == ([], ["**/*.orig"]), cruft

    _git(dest, "add", "-f", "stray.pyc", "a/b/x.orig")
    _git(dest, "commit", "-q", "-m", "track both past the ignore rules")
    leak = E.do_verify_leak_check(dest, leak_manifest)
    assert (leak["leaked"], leak["ignored"]) == (["*.pyc"], []), leak
    cruft = E.do_verify_cruft(dest, cruft_manifest)
    assert (cruft["present"], cruft["ignored"]) == (["**/*.orig"], []), cruft


def test_a_directory_mixing_ignored_and_untracked_files_still_leaks(tmp_path):
    # Half-ignored is not unshippable: one untracked-not-ignored file is enough.
    dest = _plain_git_dest(tmp_path, "mind_api/state/*.log\n")
    (dest / "mind_api/state").mkdir(parents=True)
    (dest / "mind_api/state/daemon.log").write_text("x\n", encoding="utf-8")
    manifest = {"exclude_always": ["mind_api/state/"]}
    assert E.do_verify_leak_check(dest, manifest)["ignored"] == ["mind_api/state/"]
    (dest / "mind_api/state/keep.txt").write_text("x\n", encoding="utf-8")
    res = E.do_verify_leak_check(dest, manifest)
    assert (res["leaked"], res["ignored"]) == (["mind_api/state/"], []), res
