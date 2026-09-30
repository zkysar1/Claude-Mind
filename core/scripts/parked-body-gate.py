#!/usr/bin/env python3
"""parked-body-gate.py -- a PARKED worker Body does no work until its park check runs.

Two hook events share this file because they share one question: may this turn
of a parked Body do work?

  PreToolUse[Bash|Write|Edit|MultiEdit] (no argument). On a session whose worker
  manifest reads body_state=parked, DENY the call unless one of these holds:
    1. a re-poll is open: `body-manifest.py park-due` answered DUE after the last
       park (manifest repoll_opened_at later than last_parked_at);
    2. an operator slash command opened a turn after the last park (the stamp the
       `stamp` mode below writes) -- /start and /stop must always get through;
    3. the call is the park path itself: a Bash call that reads or writes the
       manifest through body-manifest.py or grep, tests stop-requested, posts the
       park, records the stop reason, checks for a recovery yank, notifies, or
       writes the body-closing sentinel -- or a lone echo, the turn's terminal;
    4. the manifest carries no readable park time or next-poll time. That is
       park-due's own fail-toward-polling case: a park the check cannot time is
       not one this gate may hold.
  UserPromptSubmit (argument `stamp`). When the prompt is an operator slash
  command, write sessions/<sid>/operator-input-at. Prints nothing: a
  UserPromptSubmit hook's stdout would be folded into the turn.

WHY (g-375-98 -> g-375-104, measured 2026-09-30 on a worker Body). The park
sequence arms the park wakeup and ends the turn, and worker-loop Phase -0 is the
only park check, which runs only when a turn enters the worker loop. Both
harnesses deliver a background command's exit as a NEW turn. The measured Body
got one the second its park turn ended; that turn never entered the loop, so
nothing checked the park. It continued its plan for 3 h 21 min with no claim
while its manifest read parked, and its park wakeup waited four hours behind the
turn. A denied call hands the model the one instruction that fixes the turn:
re-enter the worker loop, whose Phase -0 runs the park check.

Only Bash and the three file writers are gated, because they are how work lands.
Reads cost model calls, not correctness, and Skill and ScheduleWakeup ARE the
park path.

Fail-open (hook_helpers contract): an unreadable binding, manifest or stamp
approves. A broken gate is recoverable; a gate that wedged a parked Body would
cost the fleet a worker.
"""

import datetime
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)

GATED_TOOLS = ("Bash", "Write", "Edit", "MultiEdit")
MANIFEST_FILENAME = "body-manifest.yaml"
OPERATOR_STAMP_FILENAME = "operator-input-at"
# body-manifest.py's stamp format. Every stamp compared here is written in it, so
# string order is time order.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# The park path, as the worker loop prints it: Phase -0-stop's stop-requested
# test, Phase -0's body_state read and park-due, and the park sequence's post,
# stop-reason recorder, recovery-yank check, notify fallback and body-closing
# sentinel. A substring match on the command is enough: the model runs these
# lines as the skill writes them, and this gate guards against a turn that
# forgot the park, not against an adversary.
PARK_PATH_MARKERS = (
    "body-manifest",
    "stop-requested",
    "board-post.sh",
    "stop-reason-record.py",
    "recovery_yank.py",
    "notify-user.sh",
    "aspirations-add-goal.sh",
    "body-closing",
)

# A typed slash command. Claude Code hands UserPromptSubmit the raw text
# ("/start alpha"); a harness that expands the command first hands it the
# expansion frame, <command-message>...</command-message><command-name>/...
# The raw form is ONE word after the slash, then whitespace or the end: a prompt
# that opens with a pasted path ("/opt/...", "/README.md") is not a command.
_OPERATOR_PROMPT = re.compile(
    r"\A\s*(?:/[A-Za-z][\w:-]*(?:\s|\Z)"
    r"|<command-message>[^\n]*</command-message>\s*<command-name>/)"
)


def _valid_ts(value) -> bool:
    try:
        datetime.datetime.strptime(str(value or ""), TS_FORMAT)
        return True
    except ValueError:
        return False


def _session_dir(sid: str):
    """sessions/<sid> of the agent this session is bound to, or None.

    The binding comes first: MIND_AGENT is injected only into Bash tool calls,
    so a Write or Edit hook that read the env alone would never see an agent
    (guard-1742). The agent is resolved BEFORE _paths is imported and handed to
    it through MIND_AGENT, as the sibling wrappers hand it over: _paths picks
    its conf at import and, with no agent set on a multi-agent box, warns on
    stderr -- one breakage-log line per tool call, drowning the real signal."""
    from _session_binding import _valid_sid_shape, resolve_agent_name
    if not sid or not _valid_sid_shape(sid):
        return None
    agent = resolve_agent_name(sid, SCRIPT_DIR.parent.parent)
    agent = agent or os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    os.environ["MIND_AGENT"] = agent
    from _paths import agent_dir
    return Path(agent_dir(agent)) / "sessions" / sid


def _worker_manifest(session_dir):
    """The manifest dict when this session is a worker Body, else None."""
    import yaml
    path = session_dir / MANIFEST_FILENAME
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("role") != "worker":
        return None
    return data


def _operator_stamp(session_dir) -> str:
    try:
        text = (session_dir / OPERATOR_STAMP_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if _valid_ts(text) else ""


def _is_lone_echo(command: str) -> bool:
    """True for one echo/printf with no second command, redirect or substitution.

    An operator inside quotes is text ("parked; wakeup armed"), so operators are
    looked for only after the quoted segments are removed. A substitution runs
    even inside double quotes, so that is looked for in the whole command."""
    s = command.strip()
    if not re.match(r"(echo|printf)\b", s) or any(t in s for t in ("`", "$(", "\n")):
        return False
    bare = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "", s)
    return not any(tok in bare for tok in (";", "&", "|", "<", ">"))


def _deny_reason(data: dict, last_park: str) -> str:
    return (
        "PARKED WORKER BODY: this Body's manifest reads body_state=parked (parked "
        f"{data.get('parked_at')}, last park {last_park}), and no park check has "
        "run since. Do not continue plan or goal work in this turn, whatever "
        "opened it: a background-command exit, a plan prompt or a hook's words. "
        "Invoke Skill(worker-loop) now. Its Phase -0 runs the park check "
        "(body-manifest.py park-due). Not due: re-arm the park wakeup and end the "
        "turn. Due: the re-poll runs, and a claim resumes this Body. (g-375-104)"
    )


def gate(payload: dict) -> None:
    tool = payload.get("tool_name")
    if tool not in GATED_TOOLS:
        approve_no_mutation()
    session_dir = _session_dir(str(payload.get("session_id") or ""))
    data = _worker_manifest(session_dir) if session_dir is not None else None
    if data is None or data.get("body_state") != "parked":
        approve_no_mutation()
    last_park = str(data.get("last_parked_at") or data.get("parked_at") or "")
    if not _valid_ts(last_park) or not _valid_ts(data.get("park_next_poll_at")):
        approve_no_mutation()  # 4. a park the check cannot time
    # STRICTLY after the last park, both of them: a re-park in the same second as
    # a DUE answer must close the re-poll it ends. (A DUE answer in the same
    # second as its park never happens outside a test: the orbit puts the next
    # full poll an hour out.)
    opened = str(data.get("repoll_opened_at") or "")
    if _valid_ts(opened) and opened > last_park:
        approve_no_mutation()  # 1. the park check answered DUE since the park
    operator = _operator_stamp(session_dir)
    if operator and operator > last_park:
        approve_no_mutation()  # 2. an operator command since the park
    if tool == "Bash":
        tool_input = payload.get("tool_input")
        command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
        if any(marker in command for marker in PARK_PATH_MARKERS) or _is_lone_echo(command):
            approve_no_mutation()  # 3. the park path
    emit_deny(_deny_reason(data, last_park))


def stamp(payload: dict) -> None:
    if not _OPERATOR_PROMPT.match(str(payload.get("prompt") or "")):
        return
    session_dir = _session_dir(str(payload.get("session_id") or ""))
    # Worker Bodies only, and never create a session dir from a hook.
    if session_dir is None or not session_dir.is_dir() or _worker_manifest(session_dir) is None:
        return
    target = session_dir / OPERATOR_STAMP_FILENAME
    tmp = session_dir / (OPERATOR_STAMP_FILENAME + ".tmp")
    tmp.write_text(datetime.datetime.now().strftime(TS_FORMAT) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def main() -> None:
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()
    if sys.argv[1:2] == ["stamp"]:
        stamp(payload)
        approve_no_mutation()
    gate(payload)


if __name__ == "__main__":
    # SystemExit from approve/deny propagates; any other error approves (fail-open).
    try:
        main()
    except Exception:
        sys.exit(0)
