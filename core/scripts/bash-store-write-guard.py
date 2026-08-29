#!/usr/bin/env python3
"""PreToolUse[Bash] hook -- refuse an ad-hoc command that WRITES a governed store
file directly (shell redirect, tee, sed -i, rm/truncate, cp/mv INTO it, or inline
Python that opens it for writing), bypassing the framework scripts.

Canonical incident (a downstream worker Body, 2026-08-29 00:59). The
worker-loop's Phase 4a names the close writer in a fenced block
(`iteration-close.sh --phase verify --goal <id> --status completed ...`). The
Body instead ran

    python3 << 'PYEOF'
    ... goal["status"] = "done" ... open("agents/alpha/aspirations.jsonl", "w")

and announced "Verified: g-005-06 status = done". "done" is not a status the
schema knows (pending / in-progress / completed / blocked / skipped / expired /
decomposed / superseded), so the goal was invisible to every terminal-status
reader; no completed_date, outcome_class, board post or in_flight clear
happened, and the parent aspiration could never complete. guard-996 and
CLAUDE.md ("All JSONL stores accessed exclusively via scripts") were both in
context. Two honor-system rails prevented nothing: the population is ad-hoc,
LLM-composed commands, and the one chokepoint that sees them is this hook (the
same escalation silent-zero-gate.py and bare-bash-authoring-gate.py took).

DENY, NOT ADVISORY -- the same trade silent-zero-gate documents: an `allow`
payload short-circuits the sibling deny gates on the Bash matcher.

Scope is deliberately WRITES ONLY. Direct reads of a store are also against the
rules, but skills legitimately `grep -c <id> agents/<a>/experience.jsonl` to
confirm a write landed, and refusing reads would refuse those. Writes into a
temp path (/tmp, $TMPDIR, agents/<a>/temp/) are not governed stores and pass.

Fail-open contract (CRITICAL): any parse/IO/logic error -> approve. A broken
gate is recoverable; a fail-closed one stalls every autonomous loop.

Override: put STORE_WRITE_GUARD_OVERRIDE anywhere in the command -- for an
operator restoring a store from a .history snapshot who has read
.claude/rules/archive-before-delete.md. Every override is logged.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)

OVERRIDE_TOKEN = "STORE_WRITE_GUARD_OVERRIDE"

# The lifecycle stores CLAUDE.md declares script-only, plus the three YAML
# stores with a single sanctioned writer.
GOVERNED_BASENAMES = (
    "aspirations.jsonl",
    "aspirations-archive.jsonl",
    "pipeline.jsonl",
    "experience.jsonl",
    "journal.jsonl",
    "reasoning-bank.jsonl",
    "guardrails.jsonl",
    "pattern-signatures.jsonl",
    "spark-questions.jsonl",
    "meta-log.jsonl",
    "evolution-log.jsonl",
    "gate-firings.jsonl",
    "changelog.jsonl",
    "override-bypass-ledger.jsonl",
    "execution-diary.jsonl",
    "working-memory.yaml",
    "team-state.yaml",
    "_tree.yaml",
)

# The sanctioned writer(s), named in the deny so the fix is one copy away.
WRITER_FOR = {
    "aspirations.jsonl": (
        "aspirations-add-goal.sh / aspirations-update-goal.sh / "
        "aspirations-claim.sh / aspirations-release.sh; a goal's STATUS closes "
        "through `iteration-close.sh --phase verify --goal <id> "
        "--status <completed|blocked|skipped> --source <world|agent> ...`"
    ),
    "aspirations-archive.jsonl": "aspirations-archive.sh / aspirations-retire.sh",
    "pipeline.jsonl": "pipeline-add.sh / pipeline-move.sh / pipeline-update.sh",
    "experience.jsonl": "experience-add.sh",
    "journal.jsonl": "journal-add.sh",
    "reasoning-bank.jsonl": "reasoning-bank-add.sh / reasoning-bank-update-field.sh",
    "guardrails.jsonl": "guardrails-add.sh / guardrails-update-field.sh",
    "pattern-signatures.jsonl": "pattern-signatures-add.sh",
    "spark-questions.jsonl": "spark-questions-add.sh",
    "meta-log.jsonl": "meta-set.sh (the audit line is written for you)",
    "evolution-log.jsonl": "evolution-log-add.sh",
    "gate-firings.jsonl": "_gate_log.py (gates log themselves)",
    "changelog.jsonl": "_fileops.py (every governed write appends for you)",
    "override-bypass-ledger.jsonl": "_override_helpers.py (the gate you bypass writes it)",
    "execution-diary.jsonl": "execution-diary.sh",
    "working-memory.yaml": "wm-set.sh / wm-append.sh / wm-prune.sh",
    "team-state.yaml": "team-state-update.sh",
    "_tree.yaml": "tree-update.sh (Edit the node .md; the hook syncs the index)",
}

_BASENAME_ALT = "(?:" + "|".join(re.escape(b) for b in GOVERNED_BASENAMES) + ")"
# A path token that ENDS in a governed basename: `world/aspirations.jsonl`,
# `"$WORLD_PATH/guardrails.jsonl"`, `agents/alpha/session/working-memory.yaml`.
# The lookbehind keeps `backup-aspirations.jsonl` / `my_guardrails.jsonl` out: a
# governed basename is the WHOLE final path component, never a suffix of one.
_PATH_TOKEN = r"(?:[\w./~$\{\}\-]*/)?(?<![\w\-])" + _BASENAME_ALT + r"(?![\w.\-])"
_BASENAME_END_RE = re.compile(r"(?<![\w\-])" + _BASENAME_ALT + r"$")

_REDIRECT_RE = re.compile(r"(?<![<>&\d])\d?>{1,2}\s*[\"']?(" + _PATH_TOKEN + ")")
_TEE_RE = re.compile(r"\btee\b(?:\s+-\w+)*\s+[\"']?(" + _PATH_TOKEN + ")")
_INPLACE_RE = re.compile(
    r"\b(?:sed|perl)\b[^|;&\n]*?\s(?:-[a-zA-Z]*i[a-zA-Z]*|--in-place)\b[^|;&\n]*?[\"']?("
    + _PATH_TOKEN
    + ")"
)
_DESTROY_RE = re.compile(r"\b(?:rm|truncate|shred)\b[^|;&\n]*?[\"']?(" + _PATH_TOKEN + ")")
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")
_INLINE_PY_RE = re.compile(
    r"\b(?:python3?|py)\b(?:\s+-3)?[^\n]*?(?:\s-c\s|\s-\s*<<|\s<<|\s-\s*$|\s-\s*\n)"
)
_PY_WRITE_IDIOMS = (
    r"open\([^)]*[\"'](?:w|a|r\+|w\+|a\+|wb|ab|wt|at)[\"']",
    r"mode\s*=\s*[\"'](?:w|a|r\+)",
    r"\.write_text\(",
    r"\.write_bytes\(",
    r"\.writelines\(",
    r"(?<!stdout)(?<!stderr)\.write\(",
    r"json\.dump\(",
    r"yaml\.(?:safe_)?dump\(",
    r"os\.replace\(",
    r"os\.rename\(",
    r"os\.remove\(",
    r"shutil\.(?:copy\w*|move)\(",
    r"\.unlink\(",
    r"\.truncate\(",
)
_PY_WRITE_RE = re.compile("|".join(_PY_WRITE_IDIOMS))
_MENTION_RE = re.compile(_PATH_TOKEN)
# After the `<<` of a heredoc marker: `'PYEOF'`, `-EOF`, `"EOF"`.
_HEREDOC_TAG_RE = re.compile(r"^-?\s*[\"']?(\w+)[\"']?")
# After `-c `: the quoted program (double quotes may carry escaped quotes).
_C_ARG_RE = re.compile(r"^\s*(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|(\S+))", re.S)


def _python_bodies(cmd: str):
    """The Python SOURCE of every inline invocation in `cmd` -- and nothing else.

    The inline-Python rule must read the program, not the shell around it. A
    worker Body's every command is prefixed by the framework's own env exports
    (`export BODY_WM_PATH=".../working-memory.yaml"; ...`), so scanning the whole
    command for a governed basename denied a worker that was writing a TEST
    FILE (measured 2026-08-29 02:56, a downstream worker Body) -- the store name
    came from the prefix, the write from the program, and the two were never
    the same statement.
    """
    bodies = []
    for m in _INLINE_PY_RE.finditer(cmd):
        marker = m.group(0)
        rest = cmd[m.end():]
        if "<<" in marker:
            tag = _HEREDOC_TAG_RE.match(rest)
            nl = rest.find("\n")
            if not tag or nl < 0:
                continue
            body = rest[nl + 1 :]
            end = re.search(r"(?m)^\s*" + re.escape(tag.group(1)) + r"\s*$", body)
            bodies.append(body[: end.start()] if end else body)
        elif "-c" in marker:
            arg = _C_ARG_RE.match(rest)
            if arg:
                bodies.append(next(g for g in arg.groups() if g is not None))
    return bodies


def _is_temp_path(token: str) -> bool:
    t = token.replace("\\", "/")
    return (
        "/tmp/" in t
        or t.startswith("tmp/")
        or "$TMPDIR" in t
        or "${TMPDIR" in t
        or "/temp/" in t
        or "/scratch/" in t
    )


def _basename_of(token: str) -> str:
    m = _BASENAME_END_RE.search(token)
    return m.group(0) if m else token


def _governed(token: str) -> bool:
    return not _is_temp_path(token)


def _copy_move_targets(cmd: str):
    """`cp x world/aspirations.jsonl` / `mv x ...jsonl`: the LAST operand is the store."""
    hits = []
    for seg in _SEGMENT_SPLIT.split(cmd):
        words = seg.strip().split()
        # skip leading env assignments / sudo / command wrappers
        while words and (re.match(r"^\w+=", words[0]) or words[0] in ("sudo", "env", "command")):
            words = words[1:]
        if len(words) < 3 or words[0] not in ("cp", "mv", "install"):
            continue
        operands = [w for w in words[1:] if not w.startswith("-")]
        if len(operands) < 2:
            continue
        target = operands[-1].strip("\"'")
        if _BASENAME_END_RE.search(target) and _governed(target):
            hits.append(("cp/mv into", target))
    return hits


def direct_store_writes(cmd: str):
    """Return [(idiom, path)] for every direct write the command would perform."""
    hits = []
    for rx, label in (
        (_REDIRECT_RE, "shell redirect into"),
        (_TEE_RE, "tee into"),
        (_INPLACE_RE, "in-place edit of"),
        (_DESTROY_RE, "rm/truncate of"),
    ):
        for m in rx.finditer(cmd):
            tok = m.group(1)
            if _governed(tok):
                hits.append((label, tok))
    hits.extend(_copy_move_targets(cmd))
    for body in _python_bodies(cmd):
        if not _PY_WRITE_RE.search(body):
            continue
        for m in _MENTION_RE.finditer(body):
            tok = m.group(0)
            if _governed(tok):
                hits.append(("inline Python writing", tok))
                break
    return hits


def build_reason(hits) -> str:
    idiom, path = hits[0]
    base = _basename_of(path)
    writer = WRITER_FOR.get(base, "the store's *-add.sh / *-update*.sh script")
    return (
        f"direct store write refused: {idiom} `{path}`.\n\n"
        "Every JSONL/YAML store is written ONLY through its framework script "
        "(CLAUDE.md 'All JSONL stores accessed exclusively via scripts'; guard-996). "
        "A hand write skips the lock, the .history snapshot, the changelog, the "
        "schema validation and the daemon's read cache -- the measured result on "
        "2026-08-29 was a goal parked at status \"done\", a value no reader knows, "
        "so it never completed, never posted, and never released its Body's "
        "in_flight row.\n\n"
        f"Use instead ({base}): {writer}.\n"
        "Reading a store to CHECK something is fine; if you are restoring one from "
        "a .history snapshot, use `bash core/scripts/history.py restore ...` -- and "
        f"only after .claude/rules/archive-before-delete.md. Genuine exception: put "
        f"{OVERRIDE_TOKEN} anywhere in the command (it is logged)."
    )


_ENV_PREFIX_RE = re.compile(r"^(?:\s*(?:export\s+)?[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|\S*)\s*;?\s*)+")


def _without_env_prefix(cmd: str) -> str:
    """`cmd` minus the leading env-assignment chain the framework prepends to a worker
    Body's every command -- five `export`s that ate the whole 400-char log window and
    hid the write itself (measured 2026-08-29 03:55: five identical denies logged with
    the payload cut off)."""
    return _ENV_PREFIX_RE.sub("", cmd, count=1)


def _log(kind: str, cmd: str, hits) -> None:
    try:
        root = Path(os.environ.get("PROJECT_ROOT") or SCRIPT_DIR.parent.parent)
        log_dir = root / "core" / "logs" / "hook-fires"
        log_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "agent": os.environ.get("MIND_AGENT") or os.environ.get("MIND_AGENT"),
            "sid": os.environ.get("MIND_SID") or os.environ.get("MIND_SID"),
            "hits": [list(h) for h in hits][:5],
            "command": _without_env_prefix(cmd)[:400],
        }
        with (log_dir / "store-write-guard.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main() -> None:
    try:
        payload = stdin_json_or_approve()
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
            approve_no_mutation()
        tool_input = payload.get("tool_input")
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(cmd, str) or not cmd.strip():
            approve_no_mutation()
        hits = direct_store_writes(cmd)
        if not hits:
            approve_no_mutation()
        if OVERRIDE_TOKEN in cmd:
            _log("override", cmd, hits)
            approve_no_mutation()
        _log("deny", cmd, hits)
        emit_deny(build_reason(hits))
    except SystemExit:
        raise
    except Exception:
        approve_no_mutation()


if __name__ == "__main__":
    main()
