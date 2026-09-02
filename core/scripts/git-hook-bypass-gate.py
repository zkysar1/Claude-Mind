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
gate; guard-958 surgical-scoping).

SCOPE IS ONE SIMPLE COMMAND (g-115-4695). The line is split on control
operators AND on newlines, and Forms A/B/C arm only for a simple command whose
argv[0] basename is git and whose first non-option word is ``commit``. Heredoc
BODIES are removed before tokenization — they are stdin, never argv. This
replaced a flat scan that matched ``commit`` as a bare token anywhere, which
produced two measured false positives: a commit-message heredoc containing
ordinary prose (``bash -n clean on both`` — rewording it, and nothing else, was
accepted), and a pipeline with NO git invocation at all, where ``git`` and
``commit`` arrived as quoted grep PATTERNS. The docstring here previously
described the heredoc case as an accepted residual trade; it is now fixed, and
the false-positive/true-positive matrices in the test file are the contract.
Note the narrowing itself opened a hole that adversarial probing caught — a
newline is a command boundary, so without re-inserting it a bypass on its own
line joined the previous line's argv and approved (pinned by
``test_g4695_newline_is_a_command_boundary``):

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
# Control operators only — see _split_simple_commands for why redirections are excluded.
_SEPARATORS = frozenset({";", ";;", "&&", "||", "|", "|&", "&"})
# Heredoc introducer: << or <<- then an optionally quoted word. Anchored on an
# identifier so a bit-shift or a `<<` inside quotes cannot look like one.
_HEREDOC_RE = re.compile(r"<<-?\s*([\'\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# NAME=value prefixes (`GIT_CONFIG_COUNT=1 git commit …`). Anchored on the shell's
# own rule: a leading assignment is NAME=…, where NAME cannot start with a digit.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# git GLOBAL options whose value is a SEPARATE token, so the subcommand scan must
# skip two. Glued forms (-ccore.x=y, --git-dir=…) are handled by the generic
# startswith("-") branch and need no entry here.
_GIT_VALUE_OPTS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env", "--super-prefix",
})
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
    """Tokenize with SHELL OPERATORS as their own tokens.

    `shlex.split` leaves `;` glued to the preceding word (`shlex.split('a; b')`
    -> `['a;', 'b']`) while emitting `|` and `&&` standalone — so a separator
    scan over its output silently misses every semicolon. `punctuation_chars`
    fixes that and, critically, does NOT reach inside quotes: measured,
    `git log --grep="commit|foo"` still yields one token `--grep=commit|foo`,
    and `git commit -m "fix; use sed -n here"` still yields one message token.
    That property is what lets Form A below scan a commit's OWN argv while
    prose in the message stays out of reach.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        out: list[str] = []
        prev_line = lex.lineno
        for tok in lex:
            out.append(tok)
            # A NEWLINE IS A COMMAND BOUNDARY and shlex eats it as whitespace, so
            # it is re-inserted here as an explicit separator. Without this, a
            # `git commit -n` on its OWN LINE joins the previous line's argv and
            # `_git_commit_argv` reads that line's argv[0] instead — a real bypass
            # would pass. Caught by adversarial probe, not by the existing suite.
            #
            # `lex.lineno` LAGS: it is read after the token is consumed, so an
            # increase means newline(s) were consumed while producing THIS token.
            #
            # THE JUMP MUST BE APPORTIONED, not merely tested. A multi-line
            # QUOTED token swallows its own line breaks, and the trailing
            # separator newline is consumed in the SAME step — so the jump counts
            # both and a bare "does this token contain a newline?" test throws the
            # separator away with the embedded ones. Measured on
            # `git commit -m "a\nb"\nsort -n f`: jump=2, one embedded + one real,
            # and the earlier `\n not in tok` guard emitted NO separator, merging
            # `sort -n` into the commit's argv and DENYING a benign command. That
            # is the exact false-positive class this gate was scoped to remove
            # (), and it fired on the ordinary shape — every multi-line
            # commit message in this repo — while the single-line case stayed
            # correct, which is why it hid.
            #
            # newlines INSIDE the token are quoted content; the remainder are real
            # command boundaries. Only the remainder may separate.
            if lex.lineno > prev_line:
                if (lex.lineno - prev_line) - tok.count("\n") > 0:
                    out.append(";")
                prev_line = lex.lineno
        return out
    except ValueError:
        return None  # unbalanced quotes — out of reach, approve


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc BODIES before tokenization — they are stdin, never argv.

    The originating incident (g-115-4695): `git commit -F - <<EOF` with a message
    reading "bash -n clean on both" was REFUSED, because shlex flattens the body
    into the token stream and Form A read that `-n` as a flag on the commit.
    Rewording the prose — changing nothing else — was accepted.

    Removed here rather than filtered afterwards because after shlex there is no
    way left to tell a body word from a real flag.

    FAIL-SAFE ON AMBIGUITY: if no terminator line is found, NOTHING is stripped.
    Over-stripping would swallow a later `git commit -n` on the same line and
    turn a false positive (annoying) into a false negative (a real bypass through
    an unguarded commit), so an unterminated heredoc keeps every token.
    """
    if "<<" not in command:
        return command
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = _HEREDOC_RE.search(line)
        if not m:
            continue
        delim = m.group(2)
        j = i
        while j < len(lines) and lines[j].strip() != delim:
            j += 1
        if j < len(lines):      # terminator found — drop body AND terminator
            i = j + 1
        # else: unterminated — strip nothing, keep scanning from i
    return "\n".join(out)


def _split_simple_commands(tokens: list[str]) -> list[list[str]]:
    """Split a token stream into simple commands on shell separators.

    ONLY control operators split (`;`, `&&`, `||`, `|`, `|&`, `&`, `;;`).
    Redirections deliberately do NOT: `git commit -m x > out.log` is one
    command, and `2>&1` tokenizes to `2`, `>&`, `1` — `>&` is not a member,
    so exact matching keeps it joined.
    """
    cmds: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


def _git_subcommand_argv(cmd: list[str], sub: str) -> list[str] | None:
    """Return the argv AFTER git's OWN subcommand `sub`, else None.

    argv[0]'s basename must be git, and `sub` must be the first non-option word
    after any leading env assignments and any git GLOBAL options. A bare
    `commit` or `config` token appearing as a grep pattern, a filename, or
    heredoc prose can therefore never arm a form. That conflation is the whole
    defect (g-115-4695): the gate matched `commit` anywhere in the flat token
    stream, so a pipeline with no git invocation at all — `grep -n "commit" f |
    grep -i "git" | head` — refused on the `-n`/`-rn` of an unrelated grep.
    Form D had the same shape until g-115-8672 (see scan_command).
    """
    i = 0
    while i < len(cmd) and _ENV_ASSIGN_RE.match(cmd[i]):
        i += 1
    if i >= len(cmd) or _basename_lower(cmd[i]) not in _GIT_NAMES:
        return None
    i += 1
    while i < len(cmd):
        tok = cmd[i]
        if tok in _GIT_VALUE_OPTS:      # takes a SEPARATE value token: -c k=v, -C dir
            i += 2
        elif tok.startswith("-"):       # glued/standalone: -ccore.x=y, --config-env=…, --no-pager
            i += 1
        else:
            break
    if i < len(cmd) and cmd[i].lower() == sub:
        return cmd[i + 1:]
    return None


def _git_commit_argv(cmd: list[str]) -> list[str] | None:
    """Return the argv AFTER `commit` when `cmd` really is a `git commit`."""
    return _git_subcommand_argv(cmd, "commit")


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
    command = _strip_heredoc_bodies(command)
    tokens = _tokenize(command)
    if not tokens:
        return []

    has_git = any(_basename_lower(t) in _GIT_NAMES for t in tokens)
    if not has_git:
        return []

    findings: list[tuple[str, str]] = []

    # ── Form D: persistent config writes (self-contained; no commit needed) ──
    # Scoped to ONE SIMPLE COMMAND whose argv[0] is git, exactly as Forms A-C
    # are (). The flat-stream scan this replaced read the token AFTER
    # `core.hooksPath` as the VALUE across a `;` / `&&` boundary, so the
    # read-only `git config core.hooksPath; echo x` was DENIED as a config-write
    # with value ';' (measured 2026-09-02, ) — and a `config` token in
    # an UNRELATED command (`grep config core.hooksPath notes.txt; git status`)
    # armed it with `notes.txt` as the value.
    for cmd in _split_simple_commands(tokens):
        cfg = _git_subcommand_argv(cmd, "config")
        if cfg is None:
            continue
        cfg_lower = [t.lower() for t in cfg]
        if "core.hookspath" not in cfg_lower:
            continue
        key_pos = cfg_lower.index("core.hookspath")
        flags = {t for t in cfg_lower[:key_pos] if t.startswith("-")}
        value = cfg[key_pos + 1] if key_pos + 1 < len(cfg) else None
        if "--unset" in cfg_lower or "--unset-all" in cfg_lower:
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

    # Forms A/B/C are COMMIT-SCOPED, and the scope is one SIMPLE COMMAND — not
    # the whole line (). Each is evaluated only against a simple
    # command that really is a `git commit`, so an unrelated pipeline stage
    # cannot contribute either the `commit` token or the short flag.
    commit_cmds = []
    for cmd in _split_simple_commands(tokens):
        post = _git_commit_argv(cmd)
        if post is not None:
            commit_cmds.append((cmd, post))
    if not commit_cmds:
        return findings

    for cmd, post_commit in commit_cmds:
        # ── Form A: --no-verify / -n on git commit ──
        # Scanned over the commit's OWN argv. `--no-verify` likewise: a bare
        # `--no-verify` in a heredoc body or an echo payload belongs to no git
        # commit and must not deny.
        if "--no-verify" in post_commit:
            findings.append(("no-verify", "--no-verify on git commit"))
        else:
            for tok in post_commit:
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
        # Over the WHOLE simple command: these are git GLOBAL options and sit
        # BEFORE the subcommand, so post_commit would not contain them.
        for i, tok in enumerate(cmd):
            m = _HOOKSPATH_ASSIGN_RE.match(tok)
            if m and i > 0 and cmd[i - 1] == "-c":
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
        for tok in cmd:
            if _GIT_CONFIG_PARAMS_RE.match(tok) or _GIT_CONFIG_KEY_RE.match(tok):
                findings.append((
                    "hookspath-env",
                    f"{tok.split('=', 1)[0]}=... sets core.hooksPath for this commit",
                ))
                break

    # Two `git commit`s on one line would otherwise report the same form twice.
    deduped: list[tuple[str, str]] = []
    for f in findings:
        if f not in deduped:
            deduped.append(f)
    return deduped


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
