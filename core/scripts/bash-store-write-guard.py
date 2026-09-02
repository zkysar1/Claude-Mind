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

Scope: every direct WRITE, plus one READ shape -- a HAND PARSER over the raw
file (inline Python that opens a store, or a shell read of a store piped into
inline Python / jq). Plain shell reads stay allowed: skills legitimately
`grep -c <id> agents/<a>/experience.jsonl` to confirm a write landed, and
`wc -l` / `tail -1` / `grep ... > /tmp/x` are presence checks, not parsers.
The parser is refused because it is what fails: measured 2026-08-29 on an
8-Body downstream fleet, 79 hand parsers in 12 h against 4 calls to the
sanctioned reader, and the parsers are the tracebacks in every Body log
(`JSONDecodeError` on a banner line, `NameError` in a one-liner, `No such
file` on a store that lives at an external path the model guessed). The deny
names the reader, so the fix is one copy away (g-353-71). Writes into a temp
path (/tmp, $TMPDIR, agents/<a>/temp/) are not governed stores and pass.

Fail-open contract (CRITICAL): any parse/IO/logic error -> approve. A broken
gate is recoverable; a fail-closed one stalls every autonomous loop.

Override: put STORE_WRITE_GUARD_OVERRIDE anywhere in the command -- for an
operator restoring a store from a .history snapshot who has read
.claude/rules/archive-before-delete.md. Every override is logged. It is honored
ONLY while no agent in the deployment is RUNNING: a governed store is never
hand-written under a live loop, and a Body IS the live loop.

THE DENY MUST NOT NAME THE TOKEN. The audience of a deny is the LLM being
refused; the audience of the override is the operator reading this docstring
or gates.yaml. Measured on a downstream deployment, 2026-08-29: the deny ended
"Genuine exception: put STORE_WRITE_GUARD_OVERRIDE anywhere in the command (it
is logged)", and 6 of 42 firings (14%) became overrides within 3-12 minutes --
three whole-file rewrites of aspirations.jsonl (one Body set
os.environ['STORE_WRITE_GUARD_OVERRIDE'] INSIDE its program, commented "so the
hook recognizes this as a genuine exception") and three of reasoning-bank.jsonl
(a hand "fold" of rb-1 into rb-2 that deleted a record). A refusal that names
its own bypass is an instruction.
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
    "reasoning-bank.jsonl": (
        "reasoning-bank-add.sh / reasoning-bank-update-field.sh (to FOLD entries: "
        "update-field the survivor's content, then update-field the folded entry's "
        "status to retired -- a record is never deleted)"
    ),
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

# The sanctioned READER, named in the parse deny. The single-record forms come
# first because that is what a hand parser is almost always after (measured:
# `grep '"id": "g-N-N"' aspirations.jsonl | python3 -c ...` to learn an asp_id).
READER_FOR = {
    "aspirations.jsonl": (
        "`aspirations-query.sh --goal-field id <goal-id> --full` (ONE goal: id, "
        "asp_id, status, description, verification) / `aspirations-read.sh "
        "--source <world|agent> --id <asp-id>` (one aspiration) / "
        "`aspirations-query.sh --goal-status <status>` / `--title-contains <s>`"
    ),
    "aspirations-archive.jsonl": "`aspirations-read.sh --archive`",
    "pipeline.jsonl": "`pipeline-read.sh` (see --help for --status / --id)",
    "experience.jsonl": "`experience-read.sh --goal <goal-id>` / `experience-read.sh --id <exp-id>`",
    "journal.jsonl": "`journal-read.sh`",
    "reasoning-bank.jsonl": "`reasoning-bank-read.sh --id <rb-id>` / `--category <cat>` / `retrieve.sh --category <q>`",
    "guardrails.jsonl": "`guardrails-read.sh --id <guard-id>` / `--category <cat>` / `retrieve.sh --category <q>`",
    "pattern-signatures.jsonl": "`pattern-signatures-read.sh --active`",
    "spark-questions.jsonl": "`spark-questions-read.sh`",
    "execution-diary.jsonl": "`execution-diary.sh read --limit <N> [--goal <goal-id>] [--json]`",
    "working-memory.yaml": "`wm-read.sh <slot> --json` (the slot name, e.g. current_goal, goals_completed_this_session)",
    "team-state.yaml": "`team-state-read.sh --field <dotted.path> --json`",
    "_tree.yaml": "`tree-find-node.sh --text <q>` / `retrieve.sh --category <q> --depth shallow`",
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


# `<id>` / `<goal-id>` / `<world|agent>`: a documentation placeholder. Bash would
# read `<id>` as two redirects, so `grep -c <id> experience.jsonl` inside a commit
# message or a JSON payload matched the redirect scan three times in one session
# (2026-08-29). An LLM-composed command that REALLY means `<in >out` never writes
# it without spaces; the placeholder reading wins, for the write scan only.
_PLACEHOLDER_RE = re.compile(r"<[\w.|\-]+>")


def direct_store_writes(cmd: str):
    """Return [(idiom, path)] for every direct write the command would perform."""
    hits = []
    scan = _PLACEHOLDER_RE.sub("PLACEHOLDER", cmd)
    for rx, label in (
        (_REDIRECT_RE, "shell redirect into"),
        (_TEE_RE, "tee into"),
        (_INPLACE_RE, "in-place edit of"),
        (_DESTROY_RE, "rm/truncate of"),
    ):
        for m in rx.finditer(scan):
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


# A statement is a `;` / `&&` / `||` / newline-separated unit; a PIPE chain is the
# `|` split inside one statement. The parse shape is "a read tool touches the
# store in one pipe segment and inline Python / jq sits in a LATER segment".
_STATEMENT_SPLIT = re.compile(r"\|\||&&|[;\n]")
_READ_TOOL_RE = re.compile(r"(?:^|\s)(?:grep|egrep|fgrep|rg|cat|head|tail|tac|awk|sed|cut|zcat|xargs|jq)\b")
_PIPE_PARSER_RE = re.compile(r"(?:^|\s)(?:python3?|py)\b|(?:^|\s)jq\b")
# A program that OPENS or LOADS a file. A store merely NAMED in a string
# (`print('guardrails.jsonl has rows')`) is not a parse.
_PY_READ_RE = re.compile(
    r"\bopen\(|\.read_text\(|\.read_bytes\(|\.open\(|\.readlines\(|"
    r"json\.load\(|yaml\.(?:safe_)?load\(|\bfileinput\b"
)


def direct_store_parses(cmd: str):
    """Return [(idiom, path)] for every HAND PARSE of a governed store.

    Two shapes, both measured on a downstream Body fleet 2026-08-29:
      * inline Python whose PROGRAM mentions a store path -- `json.load(open(
        'world/aspirations.jsonl'))`, a heredoc looping over its lines;
      * a shell read of the store piped into inline Python / jq --
        `grep '"id": "g-N-N"' .../aspirations.jsonl | head -1 | python3 -c ...`.
    Reads the program, not the shell around it (the same rule as the write
    branch: a worker's env prefix names its working-memory.yaml). A plain
    `grep -c` / `wc -l` / `tail -1` / redirect to a temp file is not a parse.
    """
    hits = []
    for body in _python_bodies(cmd):
        if _PY_WRITE_RE.search(body):
            continue  # the write branch owns it
        if not _PY_READ_RE.search(body):
            continue  # a store NAMED in a string is not a store OPENED
        for m in _MENTION_RE.finditer(body):
            tok = m.group(0)
            if _governed(tok):
                hits.append(("inline Python parsing", tok))
                break
    for stmt in _STATEMENT_SPLIT.split(cmd):
        segs = stmt.split("|")
        for i, seg in enumerate(segs[:-1]):
            if not _READ_TOOL_RE.search(seg):
                continue
            mention = next((m.group(0) for m in _MENTION_RE.finditer(seg) if _governed(m.group(0))), None)
            if mention is None:
                continue
            if any(_PIPE_PARSER_RE.search(later) for later in segs[i + 1 :]):
                hits.append(("shell read piped into an inline parser:", mention))
                break
    return hits


def build_parse_reason(hits) -> str:
    idiom, path = hits[0]
    base = _basename_of(path)
    reader = READER_FOR.get(base, "the store's *-read.sh wrapper (ls core/scripts/*-read.sh)")
    return (
        f"direct store parse refused: {idiom} `{path}`.\n\n"
        "A store is READ through its framework script, exactly as it is written "
        "(CLAUDE.md 'the LLM never reads/edits JSONL files directly'). The script "
        "knows the schema, the store-of-record path and the daemon's cache; a hand "
        "parser over the raw file guesses all three -- measured 2026-08-29 on an "
        "8-Body fleet: 79 hand parsers in 12 h against 4 wrapper calls, and the "
        "parsers are the tracebacks in the Body logs (JSONDecodeError on a banner "
        "line, NameError in a one-liner, 'No such file' on a store that lives at "
        "an external path).\n\n"
        f"Use instead ({base}): {reader}.\n"
        f"A presence check is fine (`grep -c <id> {base}`, `wc -l`); it is the "
        "parse that is refused. If no wrapper can answer the question, file it -- "
        f"`aspirations-add-goal.sh` with title `Investigate: {base} read not "
        "expressible via its script: <what>` -- and continue with the goal."
    )


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
        "Reading a store to CHECK something is fine. To INSPECT an old version, use "
        "`history.py diff <file> <version>` (read-only) -- NOT `history.py restore`, "
        "which is not a read at all: it OVERWRITES the live file and prints only a "
        "one-line confirmation, so a bare call reads as \"returned nothing\" while it "
        "has already reverted the store (guard-4165; twice now -- g-115-5401 via "
        "history-restore.sh, and again 2026-08-31 via this very message). Reach for "
        "restore only to genuinely RESTORE, and "
        "only after .claude/rules/archive-before-delete.md. There is no in-session "
        "bypass: if the script cannot express the write you need, file it -- "
        f"`aspirations-add-goal.sh` with title `Investigate: {base} write not "
        "expressible via its script: <what>` -- and continue with the goal."
    )


def running_agents(root: Path):
    """Names of the agents whose `agent-state` reads RUNNING under `root`.

    Layout comes from the resolver constants (AGENTS_PARENT_DIR / SESSION_DIRNAME,
    CLAUDE.md "Agent-dir Resolution"), rooted at the hook's PROJECT_ROOT rather
    than _paths.PROJECT_ROOT so a test can stage a deployment in a tmp dir.
    Fail-open: unreadable layout -> no running agents -> the override is honored.
    """
    try:
        from _paths import AGENTS_PARENT_DIR, SESSION_DIRNAME

        agents = root / AGENTS_PARENT_DIR if AGENTS_PARENT_DIR else root
        names = []
        for d in sorted(agents.iterdir()):
            state = d / SESSION_DIRNAME / "agent-state"
            try:
                if state.read_text(encoding="utf-8").strip() == "RUNNING":
                    names.append(d.name)
            except OSError:
                continue
        return names
    except Exception:
        return []


def build_override_refusal(hits, running) -> str:
    idiom, path = hits[0]
    base = _basename_of(path)
    writer = WRITER_FOR.get(base, "the store's *-add.sh / *-update*.sh script")
    return (
        f"direct store write refused: {idiom} `{path}` -- the override is not "
        f"honored while an agent is RUNNING ({', '.join(running)}).\n\n"
        "A governed store is never hand-written under a live loop: the daemon "
        "holds the lock, the .history snapshot and the changelog, and a Body IS "
        "the live loop. The override exists for an operator restoring a snapshot "
        "on a stopped deployment.\n\n"
        f"Use instead ({base}): {writer}.\n"
        "Restoring from .history: `bash core/scripts/history.py restore ...` (a "
        "script call -- it never trips this guard, but it OVERWRITES the live file; "
        "to only LOOK at an old version use `history.py diff`). If the script cannot express "
        f"the write, file `Investigate: {base} write not expressible via its "
        "script: <what>` with aspirations-add-goal.sh and continue."
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
            parses = direct_store_parses(cmd)
            if not parses:
                approve_no_mutation()
            # No override for a parse: the reader wrapper always exists for a
            # governed store, so there is no snapshot-restore case to carve out.
            _log("deny-parse", cmd, parses)
            emit_deny(build_parse_reason(parses))
        if OVERRIDE_TOKEN in cmd:
            root = Path(os.environ.get("PROJECT_ROOT") or SCRIPT_DIR.parent.parent)
            running = running_agents(root)
            if running:
                _log("override-refused", cmd, hits)
                emit_deny(build_override_refusal(hits, running))
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
