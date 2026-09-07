"""PreToolUse[Bash] gate — Layer A of the guard-2846 xtrace/credential defense.

Refuses a Bash command that enables shell tracing on a script which sources the
credential loader, because the trace prints every secret in that environment to
a transcript that persists. Layer B is guard-2846 itself (already correctly
worded -- this gate deliberately does not restate it, and no new rule file was
added). Layer C is xtrace-credential-audit.py, the detective for when the hook
fails open.

WHY A GATE AND NOT MORE GUARDRAIL TEXT (goal g-115-6400). guard-2846 sat at
times_active 17 / times_helpful 0. Its wording is correct and complete; the
PLACEMENT is the defect. Its trigger fires only for someone already reaching for
xtrace with the hazard in mind -- but the pull toward xtrace is strongest when a
script fails SILENTLY, which is exactly the case where nothing prompts a
retrieval, because there is no error text to search on and the probe feels like
information-gathering rather than a decision. A defence reachable only by
someone who already suspects the hazard cannot cover the case where the hazard
is invisible. This moves it to the moment of use.

The predicate lives in _xtrace_credential_predicate.py and is shared with the
detective, so the two layers cannot disagree about what "bad" means.

SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
PreToolUse hook contract.

Escape hatch: the XTRACE_CREDENTIAL_GATE_OVERRIDE token anywhere in the command.
Deliberate and auditable, following the gradle-tests precedent -- never a silent
fail-open.
"""

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)
from _xtrace_credential_predicate import (  # noqa: E402
    OVERRIDE_TOKEN,
    offending,
)

GATE_ID = "xtrace-credential-gate"

try:  # telemetry is best-effort; a logging fault must never break the hook
    from _gate_log import log as _gate_log_write  # noqa: E402
except Exception:  # pragma: no cover - defensive
    def _gate_log_write(*_a, **_k):
        return None


def _search_dirs(project_root):
    """Directories a bare script name may resolve against.

    The world tree is an EXTERNAL, user-configured path, so it is read from the
    environment rather than assumed to sit beside the repo. Every credential
    wrapper measured on this deployment lives there, so omitting it would make
    the gate silently inert for the entire real population.
    """
    dirs = [os.path.join(project_root, "core", "scripts")]
    world = os.environ.get("WORLD_PATH") or os.environ.get("MIND_WORLD")
    if world:
        dirs.append(os.path.join(world, "scripts"))
    return dirs


def build_reason(scripts):
    """Compose the deny message, naming the offending script(s) and the three
    safe alternatives guard-2846 already lists."""
    names = ", ".join("'{}'".format(os.path.basename(s)) for s in scripts)
    return (
        "REFUSED: this command runs shell xtrace over {}, which sources the "
        "credential loader.\n\n"
        "WHAT WOULD HAPPEN: xtrace echoes every expanded command to stderr. The "
        "script's environment holds every secret the loader exported, so the "
        "trace prints them in plaintext into a transcript that persists. This "
        "has happened: on 2026-08-16 an agent leaked four provider keys this "
        "way. The redirect that looks protective is not -- a source inside "
        "`$( ... >/dev/null 2>&1 )` leaks identically (measured).\n\n"
        "DIAGNOSE INSTEAD WITH ONE OF THESE (guard-2846):\n"
        "  1. Targeted echoes of variable NAMES, never values -- e.g.\n"
        "     `echo \"AWS_PROFILE=${{AWS_PROFILE:+set}}\"` prints set/unset "
        "without the value.\n"
        "  2. A --dry-run / --help / no-op invocation of the script itself.\n"
        "  3. Read the script. A silent failure is usually a guard clause or an "
        "early return, and reading is faster than tracing.\n\n"
        "If you genuinely need the trace and accept that secrets will be "
        "written to this transcript, put {} anywhere in the command to bypass. "
        "The bypass is recorded.\n"
        "See guard-2846."
    ).format(names, OVERRIDE_TOKEN)


def main():
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()

    if payload.get("tool_name") != "Bash":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None

    project_root = os.environ.get("PROJECT_ROOT") or str(SCRIPT_DIR.parent.parent)
    search = _search_dirs(project_root)
    scripts = offending(command, project_root, search)
    if scripts:
        _gate_log_write(
            GATE_ID, "block",
            caller="PreToolUse[Bash]",
            trigger_matched=",".join(Path(s).name for s in scripts),
            payload=command,
        )
        emit_deny(build_reason(scripts))

    # THE OVERRIDE IS THE DECISION MOST WORTH RECORDING, and the deny message
    # above PROMISES it is ("The bypass is recorded"). An unlogged bypass makes
    # that promise false inside a security control, so the record is part of
    # the contract rather than telemetry garnish.
    #
    # `offending` short-circuits on the token, so ask the counterfactual: strip
    # the token and re-run. Only a command that WOULD have been denied is a real
    # bypass -- otherwise every command merely mentioning the token would log.
    if command and OVERRIDE_TOKEN in command:
        bypassed = offending(command.replace(OVERRIDE_TOKEN, ""), project_root, search)
        if bypassed:
            _gate_log_write(
                GATE_ID, "override",
                caller="PreToolUse[Bash]",
                trigger_matched=",".join(Path(s).name for s in bypassed),
                payload=command,
                override_reason="{} present in command".format(OVERRIDE_TOKEN),
            )

    # `noop` is deliberately NOT logged. This hook fires on EVERY Bash call, so
    # a noop record per invocation would write thousands of rows a day to buy an
    # invocation denominator nothing here consumes. block and override -- the
    # two decisions that carry security meaning -- are both recorded.
    approve_no_mutation()


if __name__ == "__main__":
    # except Exception lets SystemExit (raised by approve/emit_deny via
    # sys.exit) propagate cleanly. The catch is only for unexpected bugs in
    # main() - in which case we still fail-open per the docstring contract.
    try:
        main()
    except Exception:
        sys.exit(0)
