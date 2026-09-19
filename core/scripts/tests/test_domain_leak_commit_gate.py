"""Tests for core/scripts/domain-leak-commit-gate.py + core/githooks/commit-msg Gate M3 (g-115-10048).

The gate's whole value is SCOPE: it must refuse a domain term on a line the
commit ADDS while never refusing one that was already there. The tree carries a
large pre-existing backlog (238 files for one term alone), so a gate that keys
on file contents instead of the staged diff would fail every subsequent commit
for every agent on the shared tree — the exact failure guard-1426 names. So the
load-bearing pins are the polarity pair: `test_added_term_is_refused_naming_file_line_term`
and `test_pre_existing_line_never_blocks`. Either one alone proves nothing;
together they pin that the gate reads the DIFF.

The second load-bearing pin is `test_bare_prose_mention_of_the_marker_does_not_exempt`.
The scanner's own marker-honor filter is an unanchored substring test
(guard-6989 / g-115-10246), so a file that merely MENTIONS the exemption token
exempts itself from the whole wall. Inherited into a BLOCKING gate that is not a
quirk, it IS the bypass — one line of prose would clear any commit. This gate
anchors the predicate to the token OPENING A COMMENT, and that test is what
keeps it anchored.

Two shapes, matching test_hot_path_size_gate.py:
  - PURE: parse_override(), in_scope(), the marker regex, and a drift detector
    that parses domain-leak-check.sh rather than trusting this module's copy of
    its scope constants.
  - PRODUCTION-SHAPE: a tmp git repo whose `core/scripts` is a SYMLINK to the
    real one (so the gate imports `_paths`/`_fileops` exactly as in production)
    and whose `core/githooks/commit-msg` is a byte copy of the real hook, wired
    via `core.hooksPath`. Commits are made with real `git commit`, so what is
    pinned is the hook contract itself. MIND_WORLD/MIND_META point at tmp dirs
    and STORAGE_BACKEND=local (guard-955), so the override audit record lands in
    tmp and never touches the real store.

The blocklist used by the repo tests is SYNTHETIC ("Widgetron", "Acme Gateway").
Pinning against the real blocklist would couple these tests to a file that other
goals are actively growing, and would make a failure here ambiguous between "the
gate broke" and "someone edited the blocklist".
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
_ROOT = _SCRIPTS.parent.parent
SCRIPT = _SCRIPTS / "domain-leak-commit-gate.py"
SCANNER = _SCRIPTS / "domain-leak-check.sh"
HOOK = _ROOT / "core" / "githooks" / "commit-msg"
sys.path.insert(0, str(_SCRIPTS))

BLOCKLIST = "# synthetic test blocklist\nWidgetron\nAcme Gateway\n"
TERM = "Widgetron"


def _load():
    spec = importlib.util.spec_from_file_location("dlcg", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load()


# ─── PURE ────────────────────────────────────────────────────────────────────

def test_parse_override_accepts_rejects_and_ignores_comments(gate):
    j, note = gate.parse_override("subject\n\ndomain-leak-override: the term is a literal fixture value")
    assert j == "the term is a literal fixture value" and note == ""
    # too short -> not an override, and the note explains why rather than
    # silently reading as "no trailer supplied"
    j, note = gate.parse_override("subject\n\ndomain-leak-override: nope")
    assert j is None and "too short" in note
    # git has not stripped comments yet when the hook runs
    j, _ = gate.parse_override("subject\n# domain-leak-override: a commented-out justification")
    assert j is None
    # no trailer at all is the quiet case: no justification AND no note
    assert gate.parse_override("just a subject line") == (None, "")


def test_marker_predicate_is_anchored_to_a_comment(gate):
    """guard-6989: documenting the opt-out must not TAKE the opt-out.

    The scanner's unanchored predicate would return True for every string
    below. This gate must accept only the ones where the token OPENS a comment.
    """
    rx = gate.MARKER_ANCHORED_RX
    for ok in ("# domain-leak-exempt: functional regex",
               "   # domain-leak-exempt: indented",
               "<!-- domain-leak-exempt: markdown -->",
               "// domain-leak-exempt: c-style",
               "-- domain-leak-exempt: sql/lua style"):
        assert rx.match(ok), ok
    for bad in ('        "         # domain-leak-exempt: <why>",',
                "the domain-leak-exempt: marker is explained below",
                "See domain-leak-exempt: for details",
                'print("domain-leak-exempt:")'):
        assert not rx.match(bad), bad


def test_in_scope_covers_the_framework_and_nothing_else(gate):
    assert gate.in_scope("core/scripts/foo.py")
    assert gate.in_scope(".claude/rules/bar.md")
    assert gate.in_scope("mind_api/src/x.py")
    assert not gate.in_scope("world/conventions/x.md")      # domain home, not framework
    assert not gate.in_scope("agents/alpha/journal.jsonl")  # per-agent state
    assert not gate.in_scope("core/scripts/foo.json")       # extension not scanned
    assert not gate.in_scope("README.md")                   # outside every scan dir


def test_scope_matches_scanner(gate):
    """DRIFT DETECTOR. This module mirrors domain-leak-check.sh's scan scope, so
    the two could silently disagree — a file the scanner audits but the gate does
    not is a hole, and the reverse is a surprise refusal. Parse the bash rather
    than trusting the copy."""
    text = SCANNER.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"SCAN_DIRS=\((.*?)\n\)", text, re.S)
    assert block, "could not locate SCAN_DIRS=( ... ) in domain-leak-check.sh"
    dirs = set(re.findall(r"\$PROJECT_ROOT/([^\"\s)]+)", block.group(1)))
    for line in text.splitlines():
        if "SCAN_DIRS+=(" in line:
            dirs |= set(re.findall(r"\$PROJECT_ROOT/([^\"\s)]+)", line))
    exts = set(re.findall(r'--include="\*(\.[a-z]+)"', text))
    # anti-vacuity: a broken regex returning empty sets would make the equality
    # below pass against an empty mirror (guard-5501 — prove it can fire).
    assert len(dirs) >= 3 and len(exts) >= 3, f"parser returned too little: {dirs} {exts}"
    assert set(gate.SCAN_DIRS) == dirs, f"scope drift: gate={set(gate.SCAN_DIRS)} scanner={dirs}"
    assert set(gate.EXTS) == exts, f"extension drift: gate={set(gate.EXTS)} scanner={exts}"


# ─── PRODUCTION SHAPE ────────────────────────────────────────────────────────

def _git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


@pytest.fixture()
def repo(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    root = tmp_path / "repo"
    (root / "core" / "githooks").mkdir(parents=True)
    (root / "core" / "config").mkdir(parents=True)
    (root / ".claude" / "rules").mkdir(parents=True)
    try:
        os.symlink(_SCRIPTS, root / "core" / "scripts", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot symlink core/scripts into the tmp repo")
    shutil.copyfile(HOOK, root / "core" / "githooks" / "commit-msg")
    os.chmod(root / "core" / "githooks" / "commit-msg", 0o755)
    (root / "core" / "config" / "domain-term-blocklist.txt").write_text(BLOCKLIST, encoding="utf-8")

    world = tmp_path / "world"
    meta = tmp_path / "meta"
    world.mkdir()
    meta.mkdir()
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            env.pop(k)
    env.update({
        "STORAGE_BACKEND": "local",
        "MIND_WORLD": str(world),
        "MIND_META": str(meta),
        "MIND_AGENT": "gatetest",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig-empty"),
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    # Gate M2 abstains on an unset session id; keep it unset so these tests
    # exercise M3 rather than a claim backstop.
    env.pop("MIND_SID", None)
    (tmp_path / "gitconfig-empty").write_text("", encoding="utf-8")

    _git(root, "init", "-q", "-b", "main", env=env)
    _git(root, "config", "user.name", "gate test", env=env)
    _git(root, "config", "user.email", "gate@test.local", env=env)
    _git(root, "config", "commit.gpgsign", "false", env=env)
    _git(root, "config", "core.hooksPath", "core/githooks", env=env)

    (root / "b.txt").write_text("b\n", encoding="utf-8")
    (root / ".gitignore").write_text("core/scripts\n", encoding="utf-8")
    _git(root, "add", "-A", env=env)
    _git(root, "commit", "-q", "-m", "seed", env=env)
    return {"root": root, "env": env, "world": world, "meta": meta}


def _write(repo, rel, text):
    p = repo["root"] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(repo, msg, *pathspec):
    _git(repo["root"], "add", "-A", env=repo["env"])
    args = ["commit", "-q", "-m", msg]
    if pathspec:
        args += ["--", *pathspec]
    return _git(repo["root"], *args, env=repo["env"], check=False)


def test_clean_commit_passes(repo):
    _write(repo, ".claude/rules/clean.md", "a generic service talks to remote storage\n")
    r = _commit(repo, "add a clean rule")
    assert r.returncode == 0, r.stdout + r.stderr


def test_added_term_is_refused_naming_file_line_term(repo):
    _write(repo, ".claude/rules/leak.md", f"line one\nline two mentions {TERM} here\n")
    r = _commit(repo, "add a leaking rule")
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "REFUSED" in out
    # file:line:term — the line number must be the SECOND line, not the first
    assert f".claude/rules/leak.md:2:{TERM}" in out, out
    assert "domain-leak-override:" in out
    # a refusal is not a reset: the index survives so the author can fix and retry
    staged = _git(repo["root"], "diff", "--cached", "--name-only", env=repo["env"]).stdout
    assert ".claude/rules/leak.md" in staged


def test_pre_existing_line_never_blocks(repo):
    """THE guard-1426 PROPERTY. Land a term with an override, then touch a
    DIFFERENT line of the same file with no trailer at all. The pre-existing
    term is still right there in the file; only the diff decides."""
    _write(repo, ".claude/rules/legacy.md", f"a line about {TERM}\nsecond line\n")
    r = _commit(repo, f"seed legacy\n\ndomain-leak-override: pre-existing content for the scope test")
    assert r.returncode == 0, r.stdout + r.stderr

    _write(repo, ".claude/rules/legacy.md", f"a line about {TERM}\nsecond line edited\n")
    r = _commit(repo, "edit an unrelated line")
    assert r.returncode == 0, ("a pre-existing term blocked a commit that did not add it:\n"
                               + r.stdout + r.stderr)


def test_override_trailer_allows_and_writes_an_audit_record(repo):
    _write(repo, ".claude/rules/fixture.md", f"the {TERM} token is a literal fixture\n")
    r = _commit(repo, "add fixture\n\ndomain-leak-override: the term is a literal test fixture value")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OVERRIDE accepted" in (r.stdout + r.stderr)

    led = repo["world"] / "override-bypass-ledger.jsonl"
    assert led.is_file(), "override accepted but nothing was audited"
    rows = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines() if l.strip()]
    mine = [x for x in rows if x.get("gate") == "domain-leak-commit-gate"]
    assert len(mine) == 1, rows
    rec = mine[0]
    assert rec["justification"].startswith("the term is a literal test fixture")
    assert rec["agent"] == "gatetest"
    assert TERM in rec["context"]["terms"]
    assert rec["context"]["sites"] == [f".claude/rules/fixture.md:1:{TERM}"]


def test_short_justification_still_refuses(repo):
    _write(repo, ".claude/rules/short.md", f"{TERM} appears\n")
    r = _commit(repo, "try a weak override\n\ndomain-leak-override: why")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "too short" in (r.stdout + r.stderr)


def test_anchored_marker_exempts_the_file(repo):
    _write(repo, ".claude/rules/functional.md",
           f"<!-- domain-leak-exempt: the token is matched literally here -->\n{TERM} pattern\n")
    r = _commit(repo, "add a genuinely exempt file")
    assert r.returncode == 0, r.stdout + r.stderr


def test_bare_prose_mention_of_the_marker_does_not_exempt(repo):
    """THE BYPASS PIN (guard-6989). Under the scanner's unanchored predicate this
    file is exempt from the entire wall. A blocking gate that inherited that
    could be cleared by one line of prose, so it must still refuse."""
    _write(repo, ".claude/rules/sneaky.md",
           f"This document explains what domain-leak-exempt: means.\n{TERM} appears below.\n")
    r = _commit(repo, "mention the marker in prose")
    assert r.returncode != 0, ("a bare prose mention of the marker exempted the file — "
                               "the blocking gate is bypassable:\n" + r.stdout + r.stderr)
    assert f".claude/rules/sneaky.md:2:{TERM}" in (r.stdout + r.stderr)


def test_out_of_scope_paths_are_not_gated(repo):
    _write(repo, "notes.md", f"{TERM} in a repo-root file\n")
    _write(repo, "core/scripts_docs/x.md", f"{TERM} outside every scan dir\n")
    r = _commit(repo, "add out-of-scope files")
    assert r.returncode == 0, r.stdout + r.stderr


def test_merge_commit_is_not_gated(repo):
    """Merges combine already-gated commits; re-gating them would refuse a merge
    for a term its own side already justified. Same posture as Gate M1."""
    env = repo["env"]
    _git(repo["root"], "checkout", "-q", "-b", "side", env=env)
    _write(repo, ".claude/rules/side.md", f"{TERM} on the side branch\n")
    r = _commit(repo, "side work\n\ndomain-leak-override: justified on the side branch")
    assert r.returncode == 0, r.stdout + r.stderr
    _git(repo["root"], "checkout", "-q", "main", env=env)
    _write(repo, "other.txt", "main-side change\n")
    assert _commit(repo, "main work").returncode == 0
    r = _git(repo["root"], "merge", "--no-ff", "-m", "merge side", "side", env=env, check=False)
    assert r.returncode == 0, ("the merge was gated:\n" + r.stdout + r.stderr)
