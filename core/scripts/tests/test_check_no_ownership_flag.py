"""Regression tests for the OWNERSHIP_MODE gate's two detectors (goal ).

`check-no-ownership-flag.sh` refuses a re-introduced READ of the removed
`OWNERSHIP_MODE` / `MACHINE_OWNED_AGENTS` env vars (g-115-1737). Its grep path
strips leading-`#` comment lines, which is a complete comment model for `.sh`
and `SKILL.md` and is NOT one for Python: a triple-quoted docstring carries no
`#`, so PROSE documenting the removed flag matched the env-read regex and
BLOCKED the commit — the false-positive-blocker class of rb-246 / guard-147.
`.py` is therefore delegated to `check-no-ownership-flag-py.py`, which parses
an AST: an env read is a Call or Subscript node and a docstring is
`Expr(Constant(str))`, so prose cannot reach the detector at all rather than
being filtered out of it.

What is pinned here, in the order g-115-3323's VERIFICATION names it:
  1. the reproduction fixture (a docstring mention) stops being blocked;
  2. a REAL read is STILL blocked — all four syntactic shapes, so the fix
     cannot have disarmed the gate;
  3. a `#` comment mention stays unflagged, on BOTH halves;
  4. plus two things the criteria do not name but the fix would be unsafe
     without: SCOPE PARITY with the shell glob it replaces, and the WIRING
     (guard-1943 — a green helper certifies the FUNCTION, never the WIRING;
     these tests drive the real `.sh`, which is what the pre-commit hook runs).

`test_detection_is_load_bearing` supplies the discriminating power guard-1475
requires: each detection rule is deleted from a copy of the module and the
fixture it catches must stop being flagged, proving these tests measure the
detector rather than merely co-existing with it.

This file sits under `*/tests/*`, which BOTH detectors exclude from scope —
that is why it may name the removed flags freely in fixture text.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from _bash_helpers import BASH

SCRIPTS = Path(__file__).resolve().parents[1]
GATE_PY = SCRIPTS / "check-no-ownership-flag-py.py"
GATE_SH = SCRIPTS / "check-no-ownership-flag.sh"
REPO = Path(__file__).resolve().parents[3]


def _load(path: Path = GATE_PY):
    """Import the hyphenated detector module under a unique name."""
    spec = importlib.util.spec_from_file_location(f"_own_{path.stem}_{id(path)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load()

# --- fixtures ---------------------------------------------------------------

# The goal's reproduction, verbatim in shape: prose in a docstring that names
# the removed read in order to document that it was removed.
DOCSTRING = (
    'def f():\n'
    '    """Historical: this used to call os.environ.get("OWNERSHIP_MODE")\n'
    '    and that read was removed in g-115-1737. Prose only.\n'
    '    """\n'
    '    return 1\n'
)
MODULE_PROSE = (
    '"""Notes on MACHINE_OWNED_AGENTS, which os.environ.get no longer reads."""\n'
    'NOTE = "OWNERSHIP_MODE was removed; see the design record."\n'
)
COMMENT = (
    'import os\n'
    '# was os.environ.get("OWNERSHIP_MODE") before g-115-1737\n'
    'def g():\n'
    '    return 1\n'
)

READ_GET = 'import os\ndef g():\n    return os.environ.get("OWNERSHIP_MODE", "off")\n'
READ_SUBSCRIPT = 'import os\ndef g():\n    return os.environ["MACHINE_OWNED_AGENTS"]\n'
READ_GETENV = 'import os\ndef g():\n    return os.getenv("MACHINE_OWNED_AGENTS")\n'
READ_INDIRECT = 'import os\nOWNERSHIP_MODE = "OWNERSHIP_MODE"\nv = os.environ.get(OWNERSHIP_MODE)\n'

IN = "core/scripts/x.py"


# --- 1. the reproduction stops being blocked (criterion 1) -------------------

@pytest.mark.parametrize("label,src", [("docstring", DOCSTRING), ("module-prose", MODULE_PROSE)])
def test_prose_mention_is_not_flagged(label, src):
    """Criterion 1. Prose is Expr(Constant(str)) — structurally unreachable."""
    assert GATE.scan_source(src, IN) == [], f"{label} MUST NOT be flagged — this is the bug"


# --- 2. a REAL read is still blocked (criterion 2) --------------------------

@pytest.mark.parametrize("label,src", [
    ("environ.get", READ_GET),
    ("environ[...]", READ_SUBSCRIPT),
    ("os.getenv", READ_GETENV),
    ("flag-named-by-identifier", READ_INDIRECT),
])
def test_real_read_is_flagged(label, src):
    """Criterion 2. The fix must not disarm the gate on any read shape."""
    assert GATE.scan_source(src, IN), f"{label} MUST be flagged — the gate is disarmed"


# --- 3. comment mentions preserved (criterion 3) ----------------------------

def test_hash_comment_is_not_flagged():
    """Criterion 3, .py half. Comments never enter an AST at all."""
    assert GATE.scan_source(COMMENT, IN) == []


def test_detector_exempts_itself():
    """Else the detector could never be committed once it names the flags."""
    assert GATE.scan_source(READ_GET, "core/scripts/check-no-ownership-flag-py.py") == []


def test_unparseable_source_raises_for_the_caller_to_decide():
    """scan_source must not silently swallow a SyntaxError.

    Callers fail OPEN on it (a gate that cannot parse must never block), but
    that decision belongs to the caller — a detector that returned [] here
    would be indistinguishable from a clean file.
    """
    with pytest.raises(SyntaxError):
        GATE.scan_source("def broken(:\n", IN)


# --- 4a. SCOPE PARITY with the shell glob this replaces ---------------------

def test_scope_matches_the_shell_glob_population():
    """The AST port must cover exactly what the grep path was covering.

    Bash `case` globs let `*` span `/`, so the shell's `core/scripts/*.py`
    ALREADY reached core/scripts/gates/*.py and audit_helpers/*.py. Porting it
    as an exact-parent match dropped 21 tracked files (measured 2026-07-31) —
    a coverage REGRESSION wearing a fix's clothes, since the shell's new
    `*.py) return 1` early return takes those files OFF the grep path and only
    this predicate can put them back.

    The comparison is against git's OWN enumeration (git pathspec `*` spans
    `/` identically), not against a restatement of the glob in Python — so this
    pins two independent implementations together rather than pinning the port
    to its author's belief about globbing.
    """
    def _ls(*specs):
        out = subprocess.run(["git", "ls-files", *specs], cwd=str(REPO),
                             capture_output=True, text=True, timeout=60)
        return [r for r in out.stdout.split()
                if "/tests/" not in r and "__pycache__" not in r
                and r != "core/scripts/check-no-ownership-flag-py.py"]

    shell_pop = set(_ls("core/scripts/*.py", "mind_api/src/*.py", "mind_api/scripts/*.py"))
    ast_pop = {r for r in _ls("*.py") if GATE.is_in_scope(r)}
    assert shell_pop, "population probe returned empty — the query, not the tree, is wrong"
    assert shell_pop - ast_pop == set(), \
        f"grep path covered these, AST port does not: {sorted(shell_pop - ast_pop)[:10]}"
    assert ast_pop - shell_pop == set(), \
        f"AST port covers these, grep path did not: {sorted(ast_pop - shell_pop)[:10]}"


@pytest.mark.parametrize("rel,expected", [
    ("core/scripts/x.py", True),
    ("core/scripts/gates/capability.py", True),        # the 21-file regression
    ("core/scripts/audit_helpers/_paired_diff.py", True),
    ("mind_api/src/agent_paths.py", True),             # depth 0
    ("mind_api/src/endpoints/x.py", True),             # depth 1
    ("mind_api/scripts/x.py", True),
    ("core/scripts/tests/x.py", False),                # fixtures may name the flags
    ("mind_api/docs/design.py", False),
    ("core/config/upgrade-recipes/x.py", False),       # rename maps, not reads
    ("core/scripts/check-no-ownership-flag-py.py", False),
    ("core/scripts/x.sh", False),                      # .py only — the .sh half is grep's
    ("agents/alpha/temp/x.py", False),
])
def test_scope_table(rel, expected):
    assert GATE.is_in_scope(rel) is expected


# --- 4b. discriminating power (guard-1475) ---------------------------------

def test_detection_is_load_bearing(tmp_path):
    """Removing a detection rule MUST break the matching assertion.

    A test that passes with and without the code it claims to pin measures
    nothing. Each rule is deleted from a copy of the module; the fixture that
    rule catches must stop being flagged.
    """
    original = GATE_PY.read_text(encoding="utf-8")

    call_rule = 'if leaf in _ENV_CALLS and root_ok or leaf == "getenv":'
    sub_rule = 'if "environ" in dotted.split("."):'
    flags = '_FLAGS = ("OWNERSHIP_MODE", "MACHINE_OWNED_AGENTS")'
    for needle in (call_rule, sub_rule, flags):
        assert needle in original, f"detection shape changed — retarget this test on {needle!r}"

    m_call = _load(_write(tmp_path / "no_call.py", original.replace(call_rule, "if False:")))
    assert m_call.scan_source(READ_GET, IN) == [], "environ.get still flagged with the Call rule cut"
    assert m_call.scan_source(READ_GETENV, IN) == [], "getenv still flagged with the Call rule cut"
    assert m_call.scan_source(READ_SUBSCRIPT, IN), "cutting the Call rule must not affect Subscript"

    m_sub = _load(_write(tmp_path / "no_sub.py", original.replace(sub_rule, "if False:")))
    assert m_sub.scan_source(READ_SUBSCRIPT, IN) == [], "environ[...] still flagged with the Subscript rule cut"
    assert m_sub.scan_source(READ_GET, IN), "cutting the Subscript rule must not affect Call"

    m_flags = _load(_write(tmp_path / "no_flags.py", original.replace(flags, "_FLAGS = ()")))
    for src in (READ_GET, READ_SUBSCRIPT, READ_GETENV):
        assert m_flags.scan_source(src, IN) == [], "still flagged with _FLAGS emptied"

    # Control: the unmodified module flags all three and clears the prose.
    for src in (READ_GET, READ_SUBSCRIPT, READ_GETENV):
        assert GATE.scan_source(src, IN)
    assert GATE.scan_source(DOCSTRING, IN) == []


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- 5. pre-commit mode: added-lines scoping -------------------------------

def _repo_with(tmp_path, rel: str, body: str, *, committed_first: str | None = None) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=tmp_path,
                   check=True, timeout=60)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, timeout=60)
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    if committed_first is not None:
        f.write_text(committed_first, encoding="utf-8")
        subprocess.run(["git", "add", rel], cwd=tmp_path, check=True, timeout=60)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True, timeout=60)
    f.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=tmp_path, check=True, timeout=60)
    return tmp_path


def _precommit_py(repo: Path):
    return subprocess.run([sys.executable, str(GATE_PY)], cwd=str(repo),
                          capture_output=True, text=True, timeout=120)


def test_precommit_blocks_a_newly_added_read(tmp_path):
    r = _precommit_py(_repo_with(tmp_path, "core/scripts/new.py", READ_GET))
    assert r.returncode == 1, f"expected BLOCKED, got rc={r.returncode} {r.stderr}"
    assert "BLOCKED" in r.stderr
    assert "docstring or comment is fine" in r.stderr, \
        "the fix hint must say prose is NOT what this reports — that omission is the bug's twin"


def test_precommit_allows_the_docstring_repro(tmp_path):
    r = _precommit_py(_repo_with(tmp_path, "core/scripts/new.py", DOCSTRING))
    assert r.returncode == 0, f"the reproduction must pass: {r.stderr}"


def test_precommit_ignores_a_preexisting_read(tmp_path):
    """A pre-existing site must not refuse an unrelated commit."""
    repo = _repo_with(tmp_path, "core/scripts/old.py", READ_GET + "y = 2\n",
                      committed_first=READ_GET)
    assert _precommit_py(repo).returncode == 0


def test_precommit_ignores_out_of_scope_paths(tmp_path):
    assert _precommit_py(_repo_with(tmp_path, "docs/scratch.py", READ_GET)).returncode == 0


# --- 6. WIRING: the .sh is what the pre-commit hook actually runs ----------

def _shell(repo: Path, *args):
    """Drive the REAL .sh from a throwaway repo.

    The helper is deliberately NOT copied into the throwaway: the shell
    resolves its delegate from SCRIPT_DIR, not $REPO_ROOT, and this is what
    pins that. A $REPO_ROOT-relative lookup would miss the helper here and
    fail open to a FALSE clean — which is the failure shape that reads as a
    pass.
    """
    return subprocess.run([BASH, str(GATE_SH), *args], cwd=str(repo),
                          capture_output=True, text=True, timeout=180)


def test_shell_delegates_py_to_the_ast_detector(tmp_path):
    """guard-1943: a green helper certifies the FUNCTION, never the WIRING."""
    repo = _repo_with(tmp_path, "core/scripts/probe.py", READ_GET)
    assert _shell(repo).returncode == 1, "shell did not reach the .py detector"


def test_shell_allows_the_docstring_repro(tmp_path):
    """The end-to-end statement of criterion 1, through the hook's entry point."""
    repo = _repo_with(tmp_path, "core/scripts/probe.py", DOCSTRING)
    r = _shell(repo)
    assert r.returncode == 0, f"the reproduction is still blocked end-to-end: {r.stderr}"


def test_shell_still_greps_dot_sh(tmp_path):
    """The .sh half is untouched — delegating .py must not disarm shell files."""
    repo = _repo_with(tmp_path, "core/scripts/probe.sh",
                      '#!/usr/bin/env bash\nif [ -n "${OWNERSHIP_MODE:-}" ]; then echo x; fi\n')
    assert _shell(repo).returncode == 1, "the .sh grep path regressed"


def test_shell_allows_a_dot_sh_comment_mention(tmp_path):
    """Criterion 3, .sh half — the leading-# strip that is complete for shell."""
    repo = _repo_with(tmp_path, "core/scripts/probe.sh",
                      '#!/usr/bin/env bash\n# read via $OWNERSHIP_MODE before g-115-1737\necho ok\n')
    assert _shell(repo).returncode == 0


def test_shell_audit_does_not_print_clean_on_a_py_hit(tmp_path):
    """Ordering bug class: the delegate must run BEFORE the clean banner.

    Reversed, a `.py` hit prints `audit clean` alongside its own AUDIT HIT
    line — a gate reporting both verdicts at once, where the reassuring one
    is the one a reader carries away.
    """
    repo = _repo_with(tmp_path, "core/scripts/probe.py", READ_GETENV)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True, timeout=60)
    r = _shell(repo, "--audit")
    assert r.returncode == 1
    assert "AUDIT HIT" in r.stderr
    assert "audit clean" not in (r.stdout + r.stderr), "clean banner printed alongside a hit"


# --- 7. "clean" must mean a surface was READ, not that nothing ran ----------
#
# All three below come from this goal's own fresh-eyes review, which found the
# fix had shipped with the very hazard the fix's guardrail (guard-2097) names:
# delegation removes the old path's coverage, so a failure in the new path is a
# hole with nothing behind it. F1 was reproduced live — the gate printed
# "audit clean" with a real os.environ.get(OWNERSHIP_MODE) committed.

def _repo_without_delegate(tmp_path, body: str) -> Path:
    """A repo carrying a COPY of the .sh but no helper, so SCRIPT_DIR misses it."""
    (tmp_path / "core" / "scripts").mkdir(parents=True)
    (tmp_path / "core" / "scripts" / GATE_SH.name).write_bytes(GATE_SH.read_bytes())
    (tmp_path / "core" / "scripts" / "probe.py").write_text(body, encoding="utf-8")
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@example.invalid"],
                ["git", "config", "user.name", "t"],
                ["git", "add", "-A"], ["git", "commit", "-qm", "base"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, timeout=60,
                       capture_output=True)
    return tmp_path


def test_missing_delegate_never_claims_clean(tmp_path):
    """F1. Fail OPEN on blocking, fail LOUD on the verdict — they differ."""
    repo = _repo_without_delegate(tmp_path, READ_GET)
    local = repo / "core" / "scripts" / GATE_SH.name
    r = subprocess.run([BASH, str(local), "--audit"], cwd=str(repo),
                       capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode == 0, "a gate that cannot run must never block (rb-246/guard-147)"
    assert "audit clean" not in out, \
        "claimed clean while the .py surface was covered by NOTHING — the F1 defect"
    assert "delegate missing" in out, "the absence must be announced, not silent"


def test_strict_git_distinguishes_failure_from_empty():
    """F2. '' is ambiguous; a failed enumeration must not read as a clean tree."""
    repo = REPO
    bad = ["rev-parse", "--verify", "definitely-no-such-ref"]
    ctl = subprocess.run(["git", *bad], cwd=str(repo), capture_output=True, timeout=60)
    assert ctl.returncode != 0, "positive control: this git call must actually fail"
    assert GATE._git(bad, repo) == "", "non-strict must stay fail-open (rev-parse path)"
    with pytest.raises(GATE._GitError):
        GATE._git(bad, repo, strict=True)


def test_git_timeout_is_caught_not_propagated():
    """F3. Uncaught, TimeoutExpired exits non-zero, which pre-commit reads as BLOCKED."""
    import unittest.mock as mock
    with mock.patch.object(GATE.subprocess, "run",
                           side_effect=subprocess.TimeoutExpired("git", 60)):
        assert GATE._git(["ls-files"], REPO) == "", "non-strict must fail open on timeout"
        with pytest.raises(GATE._GitError):
            GATE._git(["ls-files"], REPO, strict=True)


# F4/F5 extend the same section from a later fresh-eyes pass (bravo, ).
# F2 converted the two ENUMERATION callers to strict; _added_lines was the third
# _git call deriving a population and it was left non-strict. It is the worst of
# the three to leave open: it runs only for a file already known to CONTAIN
# candidate hits, and it failed SILENTLY at rc 0 where the other two fail loudly
# at rc 3.


def test_unreadable_diff_never_clears_a_file_with_hits(tmp_path, monkeypatch, capsys):
    """F4. The added-lines diff derives a population too — '' there reads as clean.

    On failure it returned '', the caller read that as "no line of this file is
    new", and every hit was dropped. Same rb-245 zero-count class F2 fixed.
    """
    import unittest.mock as mock
    repo = _repo_with(tmp_path, "core/scripts/victim.py", READ_GET)
    monkeypatch.chdir(repo)

    assert GATE.main([]) == 1, "positive control: the staged violation must BLOCK"

    real_git = GATE._git

    def only_the_added_lines_diff_fails(args, repo_arg, strict=False):
        if args[:2] == ["diff", "--cached"] and "-U0" in args:
            if strict:
                raise GATE._GitError("simulated git failure")
            return ""  # exactly what the pre-fix code consumed as the added-line set
        return real_git(args, repo_arg, strict)

    with mock.patch.object(GATE, "_git", only_the_added_lines_diff_fails):
        rc = GATE.main([])

    assert rc == 3, (
        f"expected the loud NOT-CHECKED code 3, got rc={rc}. rc=0 is the defect: "
        "the violation is still staged and would pass unread"
    )
    err = capsys.readouterr().err
    assert "victim.py" in err, "the unread file must be NAMED, not merely counted"
    assert "NOT checked" in err, "silence here is indistinguishable from clean"


def test_a_real_violation_still_blocks_when_another_file_is_unreadable(tmp_path, monkeypatch):
    """F5. Block-wins precedence — and the .sh wrapper is why it is load-bearing.

    check-no-ownership-flag.sh maps the delegate's rc: 1 -> block, but `*`
    (which includes 3) -> WARNING + `return 0`, never block. So downgrading a
    found violation to 3 because some OTHER file's diff was unreadable would be
    translated into "do not block" one layer up, and the violation would ship.
    """
    import unittest.mock as mock
    repo = _repo_with(tmp_path, "core/scripts/seen.py", READ_GET)
    _write(repo / "core" / "scripts" / "blind.py", READ_GET)
    subprocess.run(["git", "add", "core/scripts/blind.py"], cwd=repo,
                   check=True, timeout=60)
    monkeypatch.chdir(repo)

    real_git = GATE._git

    def only_blind_pys_diff_fails(args, repo_arg, strict=False):
        if (args[:2] == ["diff", "--cached"] and "-U0" in args
                and args[-1].endswith("blind.py")):
            if strict:
                raise GATE._GitError("simulated git failure")
            return ""
        return real_git(args, repo_arg, strict)

    with mock.patch.object(GATE, "_git", only_blind_pys_diff_fails):
        rc = GATE.main([])

    assert rc == 1, (
        f"expected BLOCKED (1), got rc={rc} — a real violation must outrank an "
        "unreadable surface, or the .sh maps it to 'do not block'"
    )


# --- 8. the .sh's `*)` rc arm: neither block nor clean () ---------
#
# F5 above STATES the .sh's rc mapping as its rationale — 0 -> clean, 1 ->
# block, `*` (which includes 3) -> WARNING + `return 0`, never block — and
# nothing here executed that `*)` arm: grepping this file for rc=3 /
# PY_VERDICT / "did not complete" returned ZERO before this section existed.
# So the arm F5 leans on was reasoned about and never run, and rewriting `*)`
# to `return 1` would have made F5's stated rationale silently stale with no
# test noticing. guard-2543: "is there a test for X" is the wrong question —
# ask which CALL SITES are executed.
#
# BOTH call sites are covered deliberately. run_py_detector is invoked from
# the precommit branch (MODE's default, and what the pre-commit hook runs)
# and from the --audit branch, and ONLY the audit branch gates the clean
# banner on PY_VERDICT. A guard proven at one entry point proves nothing
# about the entry point production uses (guard-4376), so the precommit case
# is not a duplicate of the audit one.

def _repo_with_stub_delegate(tmp_path, body: str, delegate_rc: int) -> Path:
    """A repo carrying a COPY of the .sh plus a STUB delegate that exits `delegate_rc`.

    Mirrors _repo_without_delegate, and for the same reason: the .sh resolves
    its delegate from SCRIPT_DIR, not $REPO_ROOT, so a stub only BECOMES the
    delegate when it sits beside the copied .sh. Driving the real GATE_SH via
    _shell() would reach the real delegate instead and this arm would never be
    entered — the rc has to be forced from outside the detector, because no
    input to the REAL detector reliably produces 3 (it means "git could not
    enumerate", which is an environment failure, not a code shape).
    """
    (tmp_path / "core" / "scripts").mkdir(parents=True)
    (tmp_path / "core" / "scripts" / GATE_SH.name).write_bytes(GATE_SH.read_bytes())
    (tmp_path / "core" / "scripts" / GATE_PY.name).write_text(
        f"import sys\n\nsys.exit({delegate_rc})\n", encoding="utf-8")
    (tmp_path / "core" / "scripts" / "probe.py").write_text(body, encoding="utf-8")
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@example.invalid"],
                ["git", "config", "user.name", "t"],
                ["git", "add", "-A"], ["git", "commit", "-qm", "base"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, timeout=60,
                       capture_output=True)
    return tmp_path


def _run_local_sh(repo: Path, *args):
    """Run the repo-LOCAL copy of the .sh, so SCRIPT_DIR resolves to the stub."""
    return subprocess.run(
        [BASH, str(repo / "core" / "scripts" / GATE_SH.name), *args],
        cwd=str(repo), capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("delegate_rc", [3, 7])
def test_audit_delegate_error_neither_blocks_nor_claims_clean(tmp_path, delegate_rc):
    """The two-sided assertion F1 makes for a MISSING delegate, for a FAILING one.

    The repo carries a real violation the stub never reports, which is the
    point: an unreadable population must not block (rb-246/guard-147 — a gate
    that cannot run must not become a false-positive blocker) and must not be
    laundered into "clean" either. Exactly one of those two is a mistake a
    reader would notice, so both are asserted.

    rc=7 is not decoration: `*)` is documented as "rc=3 ... or any unexpected
    code", and parametrizing pins it as a catch-all rather than an rc==3
    equality that a later edit could narrow.
    """
    repo = _repo_with_stub_delegate(tmp_path, READ_GET, delegate_rc)
    r = _run_local_sh(repo, "--audit")
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"delegate rc={delegate_rc} blocked (rc={r.returncode}) — an UNKNOWN "
        f"population must never block, which is the premise F5 depends on: {out}")
    assert f"did not complete (rc={delegate_rc})" in out, \
        f"the `*)` arm never ran, so this test proves nothing about it: {out}"
    assert "audit clean" not in out, \
        "claimed clean while the .py surface returned an error — the F1 defect"
    assert "delegate: error" in out, \
        f"PY_VERDICT did not reach the banner as 'error': {out}"


def test_precommit_delegate_error_does_not_block(tmp_path):
    """The same arm at the OTHER call site — MODE's default, i.e. the hook's.

    run_py_detector is called unconditionally by the precommit branch (outside
    its staged-file loop), so this fires with nothing staged. There is no clean
    banner on this path to assert against; what must hold is that the failing
    delegate is ANNOUNCED and does not turn into a commit block.
    """
    repo = _repo_with_stub_delegate(tmp_path, READ_GET, 3)
    r = _run_local_sh(repo)
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"a failing delegate blocked the pre-commit path (rc={r.returncode}) — "
        f"every commit on a box with a broken delegate would be refused: {out}")
    assert "did not complete (rc=3)" in out, \
        f"the precommit call site never reached the `*)` arm: {out}"
