#!/usr/bin/env python3
"""PreToolUse[Bash] hook — refuse hook-bypassing git commit invocations.

The Layer-B half of the guard-901 defense (goal g-115-3515). guard-901 is
Layer-A only (a rule in primed context), and rb-5390 records it failing in the
exact way enumeration-based rules fail: the rule named ``--no-verify``, the
agent committed all session with ``git -c core.hooksPath=/dev/null`` —
identical effect, different mechanism, and the non-match FELT like compliance.
A pre-commit hook structurally cannot catch its own bypass (it does not run),
so enforcement sits here, at the tool-invocation layer, before git is called.

FORMS refused (each token-anchored — never a raw substring, so a commit
MESSAGE that mentions ``--no-verify`` or ``core.hooksPath`` does not trip the
gate; guard-958 surgical-scoping. Known residual false-positive, same trade
the gradle-tests gate documents: shlex flattens HEREDOC bodies into the token
stream, so one command that both runs ``git commit`` and writes a heredoc
whose body contains a bare ``--no-verify`` token will deny — split the
commands or use the override token):

  A. ``--no-verify`` on a ``git commit`` (also the short ``-n`` and any short
     cluster containing ``n`` positioned after the ``commit`` subcommand —
     ``-anm`` is ``--all --no-verify -m``).
  B. ``core.hooksPath=<value>`` injected via ``-c`` / ``--config-env`` on a
     command that also runs ``git commit``, at ANY value except the repo's
     canonical ``core/githooks`` (explicitly re-stating the correct chain is
     repair, not bypass).
  C. Env equivalents on a ``git commit`` line: ``GIT_CONFIG_PARAMETERS=...``
     containing core.hooksPath, or ``GIT_CONFIG_KEY_<n>=core.hooksPath``.
  D. Persistent redirection: ``git config [--flags] core.hooksPath <value>``
     writes, and ``git config --unset core.hooksPath`` — on this repo the
     hook chain LIVES at core.hooksPath=core/githooks, so unsetting it
     disables all 9 pre-commit gates. Reads (``--get``, or the bare key with
     no value) are diagnostic and approved. Writing the canonical
     ``core/githooks`` value back is restore and approved.

NOT COVERED (documented residue, per the rb-5390 mechanism-class lesson —
enumerations leak, so name the leak): ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM``
pointed at a crafted config file, and ``git config --file <f>`` edits to a
file later used via those. Catching file-indirection requires reading files at
hook time; out of latency budget. The override ledger + guard-901 remain the
net there.

The pre-commit chain's own internal git re-invocations (the goal's NOTE
constraint) never reach this gate structurally: PreToolUse hooks fire on the
Bash TOOL call only, not on subprocesses spawned inside it.

OVERRIDE (gate-overrides.md discipline): a genuine bypass need exists — e.g.
repairing a broken hook script that itself wedges every commit. Prefix the
command with ``GIT_HOOK_BYPASS_OVERRIDE="<written justification>"``. A
non-empty justification approves the command AND appends an audit row to
``core/logs/git-hook-bypass-overrides.jsonl`` (local append — hooks are
IRREDUCIBLY LOCAL, no daemon hop). An empty justification does not approve.

HARDNESS (rb-2682 — gate hardness by execution context): hard-deny is correct
here with no advisory split, because (1) detection is token-exact with a
near-zero false-positive surface, and (2) the autonomous commit path
(iteration-commit.sh) never emits a bypass form — it runs the hooks — so a
deny can only hit a deliberate bypass or authored text about one, and the
override token covers both legitimate cases.

SAFETY: fail open at every step. No git token, no commit context, unparseable
shell line, or ANY exception → approve with no mutation. Never exits non-zero.

Lineage: g-115-3515 (this hook); guard-901 (the rule); rb-5390 (the
core.hooksPath bypass incident); bare-bash-authoring-gate.py (the structural
template); core/config/conventions/gate-overrides.md (override discipline).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)

OVERRIDE_TOKEN = "GIT_HOOK_BYPASS_OVERRIDE"
CANONICAL_HOOKS_PATH = "core/githooks"
LEDGER_RELPATH = Path("core/logs/git-hook-bypass-overrides.jsonl")

_GIT_NAMES = frozenset({"git", "git.exe"})
_SHORT_CLUSTER_RE = re.compile(r"^-[a-zA-Z]+$")
_HOOKSPATH_ASSIGN_RE = re.compile(r"^core\.hookspath=(.*)$", re.IGNORECASE)
_CONFIG_ENV_RE = re.compile(r"^--config-env=core\.hookspath=", re.IGNORECASE)
_GIT_CONFIG_PARAMS_RE = re.compile(r"^GIT_CONFIG_PARAMETERS=.*core\.hookspath", re.IGNORECASE)
_GIT_CONFIG_KEY_RE = re.compile(r"^GIT_CONFIG_KEY_\d+=core\.hookspath$", re.IGNORECASE)

DENY_TEMPLATE = (
    "BLOCKED by git-hook-bypass-gate (guard-901): this command suppresses the "
    "pre-commit hook chain.\n\n"
    "{findings}\n\n"
    "The chain at core.hooksPath=core/githooks is SLOW, not optional — it runs 9 "
    "gates over the tree. The correct remedy for a timing-out commit is to retry "
    "the SAME commit with run_in_background=true and let the hooks finish, never "
    "to route around them. rb-5390 records a full session committed via "
    "`-c core.hooksPath=/dev/null` while guard-901 sat in context — a prohibition "
    "that names one flag does not cover the mechanism class, so this gate covers "
    "the class.\n\n"
    "Genuinely need the bypass (e.g. repairing a wedged hook script)? Re-run with "
    "a written justification:\n"
    '  GIT_HOOK_BYPASS_OVERRIDE="<why the hook chain must not run>" git ...\n'
    "The justification is required non-empty and is logged to "
    "core/logs/git-hook-bypass-overrides.jsonl for audit."
)


def _basename_lower(tok: str) -> str:
    try:
        return Path(tok).name.lower()
    except Exception:
        return tok.lower()


def _tokenize(command: str) -> list[str] | None:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None  # unbalanced quotes — out of reach, approve


def find_override(tokens: list[str]) -> str | None:
    """Return the override justification when the token is present.

    Empty-string justification returns "" (present but invalid); absent
    returns None. The env-assignment form is a single shlex token
    ``GIT_HOOK_BYPASS_OVERRIDE=<value>``.
    """
    for tok in tokens:
        if tok.startswith(OVERRIDE_TOKEN + "="):
            return tok.split("=", 1)[1].strip()
    return None


def scan_command(command: str) -> list[tuple[str, str]]:
    """Return (form, detail) findings for hook-bypassing git usage.

    Token-anchored: every predicate matches whole shlex tokens (or anchored
    prefixes of assignment tokens), so quoted prose — commit messages, echo
    payloads — cannot trip it.
    """
    if "git" not in command:
        return []
    tokens = _tokenize(command)
    if not tokens:
        return []

    has_git = any(_basename_lower(t) in _GIT_NAMES for t in tokens)
    if not has_git:
        return []

    findings: list[tuple[str, str]] = []
    lowered = [t.lower() for t in tokens]
    has_commit = "commit" in lowered
    commit_idx = lowered.index("commit") if has_commit else -1

    # ── Form D: persistent config writes (self-contained; no commit needed) ──
    if "config" in lowered:
        cfg_idx = lowered.index("config")
        after = tokens[cfg_idx + 1:]
        after_lower = [t.lower() for t in after]
        if "core.hookspath" in after_lower:
            key_pos = after_lower.index("core.hookspath")
            flags = {t for t in after_lower[:key_pos] if t.startswith("-")}
            value = after[key_pos + 1] if key_pos + 1 < len(after) else None
            if "--unset" in after_lower or "--unset-all" in after_lower:
                findings.append((
                    "config-unset",
                    "git config --unset core.hooksPath — on this repo that DISABLES "
                    "the 9-gate chain (it lives at core.hooksPath=core/githooks)",
                ))
            elif value is not None and not value.startswith("-") \
                    and "--get" not in flags and "--get-all" not in flags:
                if value.replace("\\", "/").strip("/") != CANONICAL_HOOKS_PATH:
                    findings.append((
                        "config-write",
                        f"git config core.hooksPath {value!r} — persistent hook "
                        "redirection away from core/githooks",
                    ))
            # bare key / --get reads: diagnostic, approved.

    if not has_commit:
        return findings

    # ── Form A: --no-verify / -n on git commit ──
    if "--no-verify" in tokens:
        findings.append(("no-verify", "--no-verify on git commit"))
    else:
        for tok in tokens[commit_idx + 1:]:
            if tok == "--":
                break
            if _SHORT_CLUSTER_RE.match(tok) and "n" in tok[1:]:
                findings.append((
                    "no-verify-short",
                    f"short flag {tok!r} after `commit` contains `n` "
                    "(git parses it as --no-verify)",
                ))
                break

    # ── Form B: core.hooksPath injected via -c / --config-env ──
    for i, tok in enumerate(tokens):
        m = _HOOKSPATH_ASSIGN_RE.match(tok)
        if m and i > 0 and tokens[i - 1] == "-c":
            value = m.group(1)
            if value.replace("\\", "/").strip("/") != CANONICAL_HOOKS_PATH:
                findings.append((
                    "hookspath-c",
                    f"-c {tok} — redirects the hook chain for this commit",
                ))
        elif tok.startswith("-c") and len(tok) > 2:
            m2 = _HOOKSPATH_ASSIGN_RE.match(tok[2:])
            if m2 and m2.group(1).replace("\\", "/").strip("/") != CANONICAL_HOOKS_PATH:
                findings.append((
                    "hookspath-c",
                    f"{tok} — redirects the hook chain for this commit",
                ))
        elif _CONFIG_ENV_RE.match(tok):
            findings.append((
                "hookspath-config-env",
                f"{tok} — redirects the hook chain via --config-env",
            ))

    # ── Form C: env-var equivalents on the same command line ──
    for tok in tokens:
        if _GIT_CONFIG_PARAMS_RE.match(tok) or _GIT_CONFIG_KEY_RE.match(tok):
            findings.append((
                "hookspath-env",
                f"{tok.split('=', 1)[0]}=... sets core.hooksPath for this commit",
            ))
            break

    return findings


def _log_override(justification: str, command: str) -> None:
    """Append an audit row. Fail-open — a logging error must not block."""
    try:
        env_override = os.environ.get("GIT_HOOK_BYPASS_LEDGER")
        if env_override:
            ledger = Path(env_override)  # test hermeticity — no residue in the real ledger
        else:
            root = Path(__file__).resolve().parents[2]
            ledger = root / LEDGER_RELPATH
        ledger.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "justification": justification,
            "command_head": command[:300],
        }
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
    if "git" not in command:
        approve_no_mutation()  # cheap pre-filter before any tokenization

    findings = scan_command(command)
    if not findings:
        approve_no_mutation()

    tokens = _tokenize(command) or []
    justification = find_override(tokens)
    if justification:
        _log_override(justification, command)
        approve_no_mutation()

    lines = [f"  [{form}] {detail}" for form, detail in findings]
    if justification == "":
        lines.append(
            f"  [override-rejected] {OVERRIDE_TOKEN} present but EMPTY — "
            "a written justification is required"
        )
    emit_deny(DENY_TEMPLATE.format(findings="\n".join(lines)))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Bottom catch-all: a broken hook must never block legitimate work.
        pass
