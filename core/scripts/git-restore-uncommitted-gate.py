#!/usr/bin/env python3
"""PreToolUse[Bash] ADVISORY: `git checkout` / `git restore` on a path that
currently carries uncommitted work.

WHAT IT CATCHES. `git checkout -- <path>` and `git restore <path>` reset the
worktree from the index/HEAD. When that path also carries uncommitted work, the
work is destroyed silently: the command succeeds, prints nothing alarming, and
the loss is invisible until a later grep. Worse, the natural verification --
`git status --porcelain` coming back clean -- is the SIGNATURE OF THE LOSS, not
of success: clean means the tree now matches HEAD, i.e. your change is gone.

WHY A HOOK AND NOT A GUARDRAIL (the measured argument, from g-115-3890). Two
correct guardrails already say exactly this. guard-1838 had times_active = 0 --
it had NEVER fired -- while describing verbatim the loss that happened in
g-335-440, and guard-1646 sat at 533 times_active and was consulted at NEITHER
decision point of its own third recurrence (g-335-647). Per rb-5741 the
retrieval boundary is drawn at "before deciding WHAT to build", and a
mutation-restore ARISES mid-execution as a mechanical sub-step of verifying work
already decided on -- after that boundary. No amount of pre-goal retrieval
discipline reaches it. A Bash chokepoint does, because it fires at the moment of
use rather than at the moment of planning.

POSTURE: ADVISORY, never blocking. `git checkout <path>` is a legitimate and
common command; the failure mode is narrow (the path carries uncommitted work)
and the agent, not this gate, is the one who knows whether that work is
disposable. `permissionDecision: "allow"` -- the command still runs.

PREDICATE (token-anchored via shlex, so quoted prose about these commands never
trips it, per guard-958):
  1. a `git` binary token, followed by subcommand `checkout` or `restore`
  2. NOT a branch-creating / branch-switching form (-b/-B/--orphan/--detach/
     --track) and NOT interactive (-p/--patch, which prompts rather than
     silently overwriting)
  3. NOT `git restore --staged` without `--worktree` -- that rewrites the INDEX
     only and leaves the worktree file untouched, so no worktree content is lost
  4. at least one pathspec operand for which `git status --porcelain -uno`
     reports an entry

Step 4 is the whole false-positive defense and it delegates to git itself: a
branch name (`git checkout main`) and a nonexistent pathspec BOTH return empty,
measured, so neither can fire. `-uno` excludes untracked files, which are the
one class `checkout` does not touch.

DESIGN CONSTRAINT, from the goal and learned the hard way twice: this gate must
NOT depend on MIND_AGENT. PreToolUse hooks other than Bash never receive it,
and an `[ -z "$MIND_AGENT" ] && exit 0` bail is half of why
pre-edit-context-gate.sh was inert for two months while hand-testing green (the
only environment where it failed was the only environment where it ran).
Nothing here reads it.

SAFETY: fail open at every step. No git token, no checkout/restore subcommand,
unparseable shell line, git missing, not a repo, subprocess timeout, or ANY
exception -> approve with no mutation. Never exits non-zero.

Lineage: g-115-3890 (this hook); guard-1646 + guard-1838 (the rules it carries
to the moment of use); rb-5741 (why retrieval cannot cover this); g-335-440 /
g-335-647 (the two measured losses); trailing-echo-exit-gate.py (the measured
advisory-delivery shape); git-hook-bypass-gate.py (the structural template);
core/config/conventions/gate-overrides.md (override discipline).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from hook_helpers import (  # noqa: E402
        approve_no_mutation,
        stdin_json_or_approve,
    )
except Exception:
    # Hardened deliberately, and it is what lets the .sh wrapper leave stderr
    # UNSUPPRESSED (unlike the sibling git-hook-bypass-gate.sh, which pipes
    # python stderr to /dev/null). This gate's advisory writes to stderr on
    # purpose -- that is the human-at-the-terminal channel -- so blanket
    # suppression there would make emit_advisory's stderr half dead code. The
    # only thing that could otherwise reach stderr is a module-level import
    # traceback on every single Bash call; catching it here removes that risk
    # at the source instead of muting the channel that carries the message.
    sys.exit(0)

OVERRIDE_TOKEN = "GIT_RESTORE_UNCOMMITTED_OVERRIDE"

_GIT_NAMES = frozenset({"git", "git.exe"})
_SUBCOMMANDS = frozenset({"checkout", "restore"})

# Forms that are NOT a worktree path-restore. Any of these -> approve outright.
_BRANCH_FORMS = frozenset({"-b", "-B", "--orphan", "--detach", "--track", "-t"})
_INTERACTIVE_FORMS = frozenset({"-p", "--patch"})

# Flags whose VALUE is a separate token (so the value is not a pathspec).
# --ours / --theirs are DELIBERATELY ABSENT: they take NO value, so listing them
# here made the loop below skip the NEXT token -- which is the pathspec. Measured
# 2026-08-02 (fresh-eyes-code on this file's own first commit): `git checkout
# --ours dirty.txt` parsed to None and the gate stayed SILENT on one of the most
# destructive forms there is (merge-conflict resolution overwrites the worktree
# file outright). The original comment here read "no value, but harmless to
# skip-as-flag below"; it was not harmless, and a false NEGATIVE is the one
# direction a safety gate must never fail in.
_VALUE_FLAGS = frozenset({
    "-b", "-B", "--orphan", "--track", "-t",
    "-s", "--source", "--conflict", "--pathspec-from-file",
})

_SHORT_CLUSTER_RE = re.compile(r"^-[a-zA-Z]+$")

# Bound the work. Cost no longer scales with path count (one git call per repo,
# see _git_status), so this is a runaway-argv backstop rather than a budget --
# generous enough that real commands are never silently truncated.
GIT = shutil.which("git") or "git"
_MAX_PATHSPECS = 64
_MAX_REPORTED = 6
_GIT_TIMEOUT_S = 5

ADVISORY_TEMPLATE = (
    "[git-restore-uncommitted-gate] ADVISORY (guard-1646 / guard-1838): this "
    "`git {sub}` targets {n} path(s) that currently carry UNCOMMITTED work. "
    "Restoring from the index/HEAD DESTROYS it silently -- the command succeeds "
    "and prints nothing alarming.\n"
    "{findings}\n"
    "Before running this:\n"
    "  - If you are restoring after a MUTATION PROOF: reverse the mutation "
    "(re-apply the exact inverse edit), or restore from a copy you made OUTSIDE "
    "the repo first. The cheapest fix is one step earlier -- COMMIT the file "
    "before mutating it, which makes `git checkout` trivially safe.\n"
    "  - If you must run it: `git diff -- <path> > /tmp/keep.patch` first.\n"
    "  - AFTERWARDS, do NOT read a clean `git status` as confirmation. For a "
    "file that carried uncommitted work, clean IS the signature of the loss. "
    "Grep for a distinctive token from the FEATURE (not from the mutation).\n"
    "Advisory only -- the command still runs. Suppress with "
    + OVERRIDE_TOKEN + '="<justification>".'
)


def emit_advisory(message: str) -> None:
    """Deliver a NON-BLOCKING advisory that actually reaches the model.

    Shape copied VERBATIM from trailing-echo-exit-gate.py:113-139, which carries
    g-115-3511's five-probe delivery table. stderr alone does NOT reach the model
    from a non-blocking PreToolUse hook (guard-1680) -- only a DENY feeds stderr
    back. Do NOT narrow these fields from first principles: `allow` +
    permissionDecisionReason ALONE was probed and did not deliver. stderr is
    written too, deliberately, as what a human at the terminal sees.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": message,
            "additionalContext": message,
        },
        "systemMessage": message,
    }
    sys.stderr.write(message + "\n")
    print(json.dumps(payload))


def _basename_lower(tok: str) -> str:
    try:
        return Path(tok).name.lower()
    except Exception:
        return tok.lower()


def _tokenize(command: str) -> list[str] | None:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None  # unbalanced quotes -- out of reach, approve


def find_override(tokens: list[str]) -> str | None:
    """Return the override justification when present ("" = present but empty)."""
    for tok in tokens:
        if tok.startswith(OVERRIDE_TOKEN + "="):
            return tok.split("=", 1)[1].strip()
    return None


def parse_invocation(tokens: list[str]) -> tuple[str, str | None, list[str]] | None:
    """Locate a `git checkout|restore` and return (subcommand, base_dir, pathspecs).

    base_dir comes from a `cd <dir>` appearing BEFORE the git token -- the
    `cd X && git ...` shape this codebase writes constantly. None means "use the
    hook process cwd".

    Returns None when the command is not a worktree path-restore at all
    (no git, no checkout/restore, a branch form, an interactive form, or an
    index-only `restore --staged`).
    """
    base_dir: str | None = None
    git_idx = -1
    for i, tok in enumerate(tokens):
        if tok == "cd" and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            base_dir = tokens[i + 1]
        if _basename_lower(tok) in _GIT_NAMES:
            git_idx = i
            break
    if git_idx < 0:
        return None

    # Subcommand = first non-flag token after `git` that is a known subcommand.
    # `git -C <dir> checkout ...` and `git -c k=v restore ...` both land here.
    sub = None
    sub_idx = -1
    j = git_idx + 1
    while j < len(tokens):
        tok = tokens[j]
        low = tok.lower()
        if low in _SUBCOMMANDS:
            sub, sub_idx = low, j
            break
        if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            j += 2  # flag + its value
            continue
        if tok.startswith("-"):
            j += 1
            continue
        return None  # some other subcommand (add, commit, ...) -- not ours
    if sub is None:
        return None

    # `git -C <dir>` overrides any `cd` for path resolution.
    for k in range(git_idx + 1, sub_idx):
        if tokens[k] == "-C" and k + 1 < sub_idx:
            base_dir = tokens[k + 1]

    rest = list(tokens[sub_idx + 1:])
    # An UNSPACED `;` does not survive as its own token: shlex.split renders
    # `... dirty.txt; echo done` as ['dirty.txt;', 'echo', 'done'], so the
    # separator scan below never sees it and the pathspec keeps a trailing
    # semicolon that matches no file -- silent false negative. Measured
    # 2026-08-02 (fresh-eyes-code). Cut at the first such token and strip it.
    for idx, tok in enumerate(rest):
        if tok.endswith(";"):
            rest = rest[:idx] + [tok[:-1]]
            break
    # Stop at a shell separator -- `git checkout -- a.txt && echo done` must not
    # swallow `echo` and `done` as pathspecs.
    for stop in ("&&", "||", ";", "|"):
        if stop in rest:
            rest = rest[:rest.index(stop)]

    flags = {t for t in rest if t.startswith("-")}
    flag_lower = {t.lower() for t in flags}
    if flag_lower & _BRANCH_FORMS or flag_lower & _INTERACTIVE_FORMS:
        return None
    # Short clusters like -bt: treat any cluster containing b/B/t/p as a branch
    # or interactive form. Conservative in the APPROVE direction.
    for t in flags:
        if _SHORT_CLUSTER_RE.match(t) and set(t[1:]) & set("bBtp"):
            return None

    if sub == "restore":
        staged = "--staged" in flag_lower or "-S" in flags
        worktree = "--worktree" in flag_lower or "-W" in flags
        if staged and not worktree:
            return None  # index-only: the worktree file is untouched

    # Pathspecs: everything after `--`; otherwise every non-flag token that is
    # not the value of a value-taking flag.
    if "--" in rest:
        pathspecs = [t for t in rest[rest.index("--") + 1:] if t]
    else:
        pathspecs = []
        skip_next = False
        for tok in rest:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                if tok in _VALUE_FLAGS:
                    skip_next = True
                continue
            pathspecs.append(tok)

    pathspecs = [p for p in pathspecs if p][:_MAX_PATHSPECS]
    if not pathspecs:
        return None
    return sub, base_dir, pathspecs


def _git_status(repo_dir: str, pathspecs: list[str]) -> list[str]:
    """`git status --porcelain -uno -- <pathspec>...` lines, or [] on ANY failure.

    Takes ALL pathspecs for one repo in a SINGLE call. git accepts many
    pathspecs, so the previous one-subprocess-per-path shape bought nothing and
    made cost scale with path count -- which is what forced a tight truncation
    cap, and a truncation that drops paths silently is a false ALL-CLEAR
    (guard-1760: never report what you declined to look at as coverage). One
    call per repo makes the cap generous at no cost.

    -uno excludes untracked files: `checkout`/`restore` do not touch them, so
    counting them would be a pure false positive.
    """
    try:
        proc = subprocess.run(
            [GIT, "-C", repo_dir, "status", "--porcelain", "-uno", "--", *pathspecs],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []  # not a repo / bad pathspec -- nothing to say
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def scan(tokens: list[str], cwd: str) -> tuple[str, list[str]] | None:
    """Return (subcommand, findings) when uncommitted work is at risk."""
    parsed = parse_invocation(tokens)
    if parsed is None:
        return None
    sub, base_dir, pathspecs = parsed

    # Group by repo so each repo costs exactly ONE git call, not one per path.
    by_repo: dict[str, list[str]] = {}
    for spec in pathspecs:
        if os.path.isabs(spec):
            repo_dir = os.path.dirname(spec) or "/"
        else:
            repo_dir = base_dir or cwd
        if not os.path.isdir(repo_dir):
            continue
        by_repo.setdefault(repo_dir, []).append(spec)

    findings: list[str] = []
    for repo_dir, specs in by_repo.items():
        findings.extend(_git_status(repo_dir, specs))
    if not findings:
        return None
    # Dedup, preserve order.
    seen, uniq = set(), []
    for f in findings:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return sub, uniq


def main() -> None:
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()
    if payload.get("tool_name") != "Bash":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        approve_no_mutation()
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        approve_no_mutation()

    # Cheap pre-filter before any tokenization or subprocess. INVARIANT: may
    # produce false ADMITS, must never produce a false reject -- every form the
    # predicate matches contains both a git token and one of the subcommands.
    if "git" not in command:
        approve_no_mutation()
    if "checkout" not in command and "restore" not in command:
        approve_no_mutation()

    tokens = _tokenize(command)
    if not tokens:
        approve_no_mutation()

    if find_override(tokens):
        approve_no_mutation()

    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    result = scan(tokens, cwd)
    if result is None:
        approve_no_mutation()

    sub, findings = result
    shown = findings[:_MAX_REPORTED]
    lines = [f"  {ln}" for ln in shown]
    if len(findings) > _MAX_REPORTED:
        lines.append(f"  ... and {len(findings) - _MAX_REPORTED} more")
    emit_advisory(ADVISORY_TEMPLATE.format(
        sub=sub, n=len(findings), findings="\n".join(lines),
    ))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Bottom catch-all: a broken hook must never block legitimate work.
        pass
