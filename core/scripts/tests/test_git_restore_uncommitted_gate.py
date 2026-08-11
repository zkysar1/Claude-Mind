"""Pin : the Layer-B advisory for `git checkout|restore` on a path
that carries uncommitted work.

guard-1646 and guard-1838 are Layer-A rules and the goal's own measurement is
that they do not reach the moment of use: guard-1838 sat at times_active = 0
(never fired) while describing verbatim the loss that happened in g-335-440,
and guard-1646 (533 times_active) was consulted at NEITHER decision point of
its own third recurrence. This gate carries them to the Bash chokepoint.

PRODUCTION SHAPE (guard-920 / rb-5235). Two properties of the real environment
are pinned deliberately, because a hand-run invocation has BOTH and is therefore
the one environment where the corresponding bugs do not occur:

  1. MIND_AGENT is REMOVED from the env for every run. PreToolUse hooks other
     than Bash never receive it, and an agent-resolution bail is half of why
     pre-edit-context-gate.sh was inert for two months while hand-testing green.
     This gate must never grow that dependency.
  2. The stdin envelope is Claude Code's real PreToolUse JSON, and the answer is
     read off STDOUT — empty (approve) or an `allow` advisory payload — with the
     exit code asserted 0 on every path.

DELIVERY FIELDS ARE PINNED INDIVIDUALLY (guard-1680 / g-115-3511). `allow` +
permissionDecisionReason alone was probed live and did NOT reach the model; the
delivered shape carries additionalContext and systemMessage too. Narrowing the
payload is the exact silent-failure mode this gate exists to avoid, so each
field gets its own assertion rather than riding on one.

Hermetic: every case runs against a throwaway git repo under tmp_path, never
the live tree. Per guard-1165 nothing here mutates os.environ at module level.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
GATE = SCRIPTS / "git-restore-uncommitted-gate.py"
WRAPPER = SCRIPTS / "git-restore-uncommitted-gate.sh"

DIRTY = "dirty.txt"
CLEAN = "clean.txt"
FEATURE_TOKEN = "UNCOMMITTED-FEATURE-WORK"


def _env_without_agent() -> dict:
    """Production shape property 1 — the gate must not need MIND_AGENT."""
    env = os.environ.copy()
    env.pop("MIND_AGENT", None)
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one dirty tracked file and one clean tracked file."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "test")
    (r / DIRTY).write_text("committed\n", encoding="utf-8")
    (r / CLEAN).write_text("committed\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    # The uncommitted work whose silent destruction this gate warns about.
    (r / DIRTY).write_text(FEATURE_TOKEN + "\n", encoding="utf-8")
    return r


def _run(command: str, tool_name: str = "Bash", target=GATE, cwd: Path | None = None):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    argv = ([sys.executable, str(target)] if str(target).endswith(".py")
            else [shutil.which("bash") or "/bin/bash", str(target)])
    proc = subprocess.run(
        argv, input=json.dumps(payload), capture_output=True, text=True,
        timeout=120, env=_env_without_agent(), cwd=str(cwd) if cwd else None,
    )
    # Hook contract: never exit non-zero, on any path.
    assert proc.returncode == 0, proc.stderr
    return proc


def _fired(proc) -> bool:
    out = proc.stdout.strip()
    if not out:
        return False
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "allow"


# ── FIRES: the destructive forms ────────────────────────────────────────────

@pytest.mark.parametrize("template", [
    "git checkout -- {p}",
    "git checkout {p}",                    # no `--` separator
    "git restore {p}",
    "git restore --staged --worktree {p}",  # --worktree DOES touch the file
    "git checkout HEAD -- {p}",             # explicit tree-ish source
    "git checkout -- {p} && echo done",     # chained
])
def test_fires_on_destructive_form_against_dirty_path(repo, template):
    proc = _run(template.format(p=str(repo / DIRTY)))
    assert _fired(proc), f"expected advisory for: {template}"
    assert DIRTY in proc.stdout


def test_fires_via_cd_prefix_with_relative_path(repo):
    """`cd X && git checkout -- rel` is the shape this codebase writes constantly.

    The pathspec is relative, so the gate can only resolve it by honouring the
    `cd`. Without that, a relative path would silently resolve against the hook
    process cwd and find nothing — a false ALL-CLEAR, the dangerous direction.
    """
    proc = _run(f"cd {repo} && git checkout -- {DIRTY}")
    assert _fired(proc)


def test_fires_via_git_C_with_relative_path(repo):
    proc = _run(f"git -C {repo} checkout -- {DIRTY}")
    assert _fired(proc)


def test_fires_on_whole_tree_pathspec(repo):
    """`git checkout -- .` is the maximally destructive form."""
    proc = _run(f"cd {repo} && git checkout -- .")
    assert _fired(proc)


@pytest.mark.parametrize("flag", ["--ours", "--theirs"])
def test_fires_on_merge_conflict_resolution_forms(repo, flag):
    """`git checkout --ours <path>` overwrites the worktree file outright.

    REGRESSION (fresh-eyes-code on this file's own first commit, 2026-08-02):
    --ours/--theirs were listed in _VALUE_FLAGS as "no value, but harmless to
    skip-as-flag". Not harmless -- the skip consumed the NEXT token, which is
    the pathspec, so the gate parsed to None and stayed SILENT on one of the
    most destructive forms git has. Remove either flag's absence from
    _VALUE_FLAGS and this goes red.
    """
    proc = _run(f"cd {repo} && git checkout {flag} {DIRTY}")
    assert _fired(proc), f"{flag} must not swallow the pathspec"


def test_fires_when_semicolon_chains_the_next_command(repo):
    """`... <path>; echo done` -- shlex attaches an UNSPACED `;` to the token.

    REGRESSION (same review): split() yields ['dirty.txt;', 'echo', 'done'], so
    the separator scan never sees a bare `;` and the pathspec keeps a trailing
    semicolon that matches no file. Silent false negative. The `&&` form was
    already covered and passes -- which is exactly why this one hid.
    """
    proc = _run(f"cd {repo} && git checkout -- {DIRTY}; echo done")
    assert _fired(proc)


def test_many_pathspecs_are_not_silently_truncated(repo):
    """A truncation that drops paths is a false ALL-CLEAR (guard-1760).

    The dirty file is placed LAST among 30 pathspecs, so it survives only if the
    cap is above the list length. Under the original _MAX_PATHSPECS=12 the gate
    examined the 12 clean paths, found nothing, and said nothing.
    """
    pads = [f"pad{i}.txt" for i in range(30)]
    for name in pads:
        (repo / name).write_text("x\n", encoding="utf-8")
    # Scope the add to the pads. `git add -A` would stage and commit DIRTY's
    # uncommitted change into this very commit, leaving the fixture clean and
    # the assertion below vacuously unsatisfiable -- a test that fails for a
    # reason unrelated to what it pins.
    _git(repo, "add", *pads)
    _git(repo, "commit", "-q", "-m", "pad")
    specs = " ".join(pads) + f" {DIRTY}"
    proc = _run(f"cd {repo} && git checkout -- {specs}")
    assert _fired(proc), "the dirty path must be examined, not truncated away"


# ── SILENT: the legitimate / non-destructive forms ──────────────────────────

@pytest.mark.parametrize("template,why", [
    ("git checkout -- {c}", "clean tracked file — nothing to lose"),
    ("git restore --staged {p}", "index-only; the worktree file is untouched"),
    ("git checkout -b newbranch", "branch creation, not a path restore"),
    ("git checkout -p {p}", "interactive — prompts rather than silently overwriting"),
    ("git status --porcelain", "not checkout/restore"),
    ("git add {p}", "not checkout/restore"),
    ("git commit -m 'restore checkout'", "prose mentioning both words"),
])
def test_silent_on_non_destructive_forms(repo, template, why):
    cmd = template.format(p=str(repo / DIRTY), c=str(repo / CLEAN))
    assert not _fired(_run(cmd)), f"false positive ({why}): {cmd}"


def test_silent_on_branch_name_not_a_path(repo):
    """A branch name and a pathspec are syntactically identical after `checkout`.

    The discriminator is delegated to git itself: `git status --porcelain` for a
    ref returns empty, so the gate cannot fire. Pinned because a predicate that
    guessed structurally (contains a slash, exists on disk) would misfire here.
    """
    _git(repo, "branch", "somefeature")
    assert not _fired(_run(f"cd {repo} && git checkout somefeature"))


def test_silent_on_nonexistent_pathspec(repo):
    assert not _fired(_run(f"cd {repo} && git checkout -- no/such/file.txt"))


def test_silent_on_untracked_file(repo):
    """`-uno` is load-bearing: checkout/restore do not touch untracked files, so
    counting them would be a pure false positive."""
    (repo / "untracked.txt").write_text("scratch\n", encoding="utf-8")
    assert not _fired(_run(f"cd {repo} && git checkout -- untracked.txt"))


def test_silent_on_quoted_prose_mentioning_the_command(repo):
    """guard-958: a token-anchored predicate must not trip on authored text."""
    p = str(repo / DIRTY)
    assert not _fired(_run(f'echo "never run git checkout -- {p}"'))


def test_silent_when_not_a_bash_tool(repo):
    assert not _fired(_run(f"git checkout -- {repo / DIRTY}", tool_name="Edit"))


def test_silent_on_unbalanced_quotes(repo):
    """Unparseable shell line — fail open rather than guess."""
    assert not _fired(_run(f"git checkout -- '{repo / DIRTY}"))


def test_override_suppresses_the_advisory(repo):
    cmd = (f'GIT_RESTORE_UNCOMMITTED_OVERRIDE="mutation scratch, disposable" '
           f'git checkout -- {repo / DIRTY}')
    assert not _fired(_run(cmd))


# ── DELIVERY SHAPE: the half that fails silently when wrong ─────────────────

def test_advisory_payload_carries_every_delivered_field(repo):
    """guard-1680 / : `allow` + reason ALONE was probed and did not
    reach the model. Each field is asserted separately so a narrowing edit fails
    here loudly instead of producing a gate that fires and communicates nothing.
    """
    proc = _run(f"git checkout -- {repo / DIRTY}")
    d = json.loads(proc.stdout)
    hso = d["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow", "advisory must NEVER deny"
    msg = hso["permissionDecisionReason"]
    assert hso["additionalContext"] == msg
    assert d["systemMessage"] == msg
    # The remedy must actually be carried, not just a bare warning.
    assert "guard-1646" in msg and "guard-1838" in msg
    assert "COMMIT the file before mutating" in msg
    assert "clean IS the signature of the loss" in msg


def test_wrapper_delivers_on_both_channels(repo):
    """The .sh wrapper deliberately does NOT pipe python stderr to /dev/null
    (unlike its sibling git-hook-bypass-gate.sh), because stderr is the
    human-at-the-terminal half of the delivery. A future 2>/dev/null 'tidy-up'
    would mute it silently — this pins both channels through the real wrapper.
    """
    proc = _run(f"git checkout -- {repo / DIRTY}", target=WRAPPER, cwd=SCRIPTS.parents[1])
    assert _fired(proc), "wrapper must deliver the structured payload"
    assert "ADVISORY" in proc.stderr, "wrapper must not suppress the stderr channel"
