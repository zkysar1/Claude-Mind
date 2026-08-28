#!/usr/bin/env python3
"""PreToolUse[Bash] hook — refuse a bare-``"bash"`` argv[0] in INLINE Python.

The authoring-time half of the guard-580 Layer-B defense (goal g-115-3171).
The pre-commit gate (``check-no-bare-bash.py``) covers committed files. This
covers the surface that gate structurally cannot reach: ad-hoc, one-off,
throwaway Python passed straight to the interpreter as ``python -c '...'`` /
``py -3 -c '...'`` and never written to a tracked file.

WHY THIS HALF IS THE LOAD-BEARING ONE (rb-5255, the scope correction on
g-115-3171): the reintroduction that promoted this work to HIGH was NOT in a
committed script. Having just swept the bug class out of 12 production sites
and rescoped guard-580 in the same hour, the author wrote a **one-off** script
using a bare argv[0] and it hung exactly as the guardrail predicts. Throwaway
code is written with attention allocated elsewhere and feels exempt from a rule
aimed at "production scripts" — so it is precisely where the pattern comes
back. A gate limited to committed files would not have caught that instance.

MECHANISM being prevented: on win32, CreateProcess with
``lpApplicationName=NULL`` searches ``System32`` BEFORE ``PATH``, so a bare
``"bash"`` reaches the WSL launcher; on a box whose WSL is broken it BLOCKS
FOREVER on a dead LxssManager. ``shutil.which("bash")`` searches PATH only and
returns the CORRECT Git Bash, so it appears to exonerate WSL — that
disagreement IS the bug (guard-580).

DETECTION is delegated wholesale to ``check-no-bare-bash.py``'s
``scan_source`` (single source of truth — one AST implementation, one override
marker, one set of forms). This module only does extraction: pull the ``-c``
payload out of the shell command line and hand it over. Scope globs are NOT
applied — inline code has no path, and restricting by path is exactly the
blind spot this layer exists to remove.

SAFETY: fail open at every step. A command with no inline Python, an
unparseable shell line, an unparseable payload, a missing engine, or ANY
exception approves with no mutation. The only non-approving path is a
successfully-extracted, successfully-parsed payload that genuinely constructs
a bare-bash argv. Escape hatch is the same marker the pre-commit gate honors:
``# allow-bare-bash: <reason>`` inside the payload.

Lineage: g-115-3171 (this hook); guard-580 (the rule); rb-5255 (the same-hour
reintroduction proving the honour-system layer insufficient); rb-5143 (the
7-site sweep); ``check-no-bare-bash.py`` (the detection engine).
"""
from __future__ import annotations

import importlib.util
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)

# Interpreter spellings that accept an inline program via -c. `py` is the
# Windows launcher (see core/config/conventions/python-invocation.md).
INTERPRETERS = frozenset({"python", "python3", "py", "python.exe", "python3.exe", "py.exe"})

GATE_ID = "bare-bash-authoring-gate"


def _log(decision, *, path, form=None, error=None):
    """Best-effort firing record. NEVER raises, NEVER changes the verdict.

    Instrumented for g-115-3325: without this the hook wrote nothing on deny,
    so its firing rate was unmeasurable, rb-5255's "throwaway code is where the
    pattern returns" claim was untestable, and — because the hook is fail-open
    by design — a regression that swallowed a real deny left no trace at all.

    WHY _gate_log AND NOT A BESPOKE LOG. The goal specified a new local
    core/logs/bare-bash-denials.jsonl plus a hand-rolled writer, on the premise
    that the shared ledger costs a 4.7-10s whole-object remote RMW on the
    interactive path. That premise is STALE (measured 2026-08-27): _gate_log
    routes the hot path to a machine-local lockless O_APPEND spool
    (_gate_log.py:301-304, `_spool_active()` True on this box) and only
    gate-firings-flush.py batches it into the shared store at iteration-close.
    So the standard mechanism already IS the "one local append, fail-open"
    shape the goal asked to build by hand, and guard-502 requires gates use it.

    THE IMPORT IS LAZY ON PURPOSE. main() returns before reaching any _log()
    call on the hot path (a Bash command containing no inline Python), so the
    overwhelmingly common case pays neither the import nor the write.
    """
    try:
        import sys as _s
        _s.path.insert(0, str(Path(__file__).resolve().parent))
        import _gate_log
        _gate_log.log(GATE_ID, decision, caller=path,
                      trigger_matched=form, gate_error=error)
    except Exception:
        return  # telemetry must never influence the gate

DENY_TEMPLATE = (
    "BLOCKED by bare-bash-authoring-gate (guard-580): this inline Python builds a "
    "subprocess argv whose argv[0] is a bare \"bash\".\n\n"
    "{findings}\n\n"
    "On win32 CreateProcess searches System32 BEFORE PATH, so a bare \"bash\" "
    "resolves to the WSL launcher and BLOCKS FOREVER on a dead LxssManager — the "
    "parent hangs in communicate() with a 0-CPU child. shutil.which(\"bash\") "
    "searches PATH only and returns the CORRECT Git Bash, so it will appear to "
    "exonerate WSL; that disagreement IS the bug.\n\n"
    "Resolve the binary explicitly instead:\n"
    "  import sys; sys.path.insert(0, 'core/scripts')\n"
    "  from _runtime_bash import BASH, bash_cmd\n"
    "  subprocess.run(bash_cmd('core/scripts/x.sh', arg), capture_output=True, text=True)\n"
    "  # or:  subprocess.run([BASH, Path(script).as_posix(), *args], ...)\n\n"
    "Simplest fix for a one-off: call the .sh directly from Bash instead of "
    "wrapping it in Python.\n\n"
    "Genuinely POSIX-only? Add '# allow-bare-bash: <reason>' inside the payload.\n"
    "This is ad-hoc code — rb-5255 records that this is exactly where the pattern "
    "comes back, which is why the gate covers it."
)


def _load_engine():
    """Import the hyphenated detection engine. None when unavailable."""
    path = Path(__file__).resolve().parent / "check-no-bare-bash.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_bare_bash_engine", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_inline_python(command: str) -> list[str]:
    """Return every inline ``-c`` payload in ``command``.

    Uses shlex so shell quoting is honored rather than guessed. A command that
    shlex cannot parse yields [] (fail open) — the alternative, a regex over
    raw text, would mis-slice heredocs and nested quotes and produce false
    positives on the loop's hot path.
    """
    if not command or "-c" not in command:
        return []
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return []  # unbalanced quotes — out of reach, approve

    payloads: list[str] = []
    for i, tok in enumerate(tokens):
        if tok != "-c" or i + 1 >= len(tokens):
            continue
        # Walk back over interpreter flags (`py -3 -c`, `python -X utf8 -c`)
        # to confirm an interpreter actually owns this -c.
        for back in range(i - 1, max(-1, i - 4), -1):
            cand = Path(tokens[back]).name.lower()
            if cand in INTERPRETERS:
                payloads.append(tokens[i + 1])
                break
            if not tokens[back].startswith("-"):
                break  # a non-flag, non-interpreter token: this -c isn't ours
    return payloads


# `py -3 - <<'PY'` / `python3 - <<EOF` — interpreter reads the program from
# STDIN, fed by a heredoc. Group 1 is the delimiter (quotes stripped by the
# alternation); the body runs to a line containing only that delimiter.
# `<<-` strips leading TABS from the body and the terminator, so it is accepted
# and the terminator match tolerates leading whitespace.
_HEREDOC_RE = re.compile(
    r"(?:^|[|&;(\s])(?:py|python|python3)(?:\.exe)?"     # interpreter
    r"(?:\s+-[A-Za-z0-9]+)*"                             # -3, -u, -X utf8 ...
    r"\s+-\s*"                                           # the bare `-` = read stdin
    r"<<-?\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))",
    re.MULTILINE,
)


def extract_heredoc_python(command: str) -> list[str]:
    """Return every heredoc-fed inline Python program in ``command``.

    WHY THIS EXISTS (2026-07-27): the `-c` extractor above is blind to
    `py -3 - <<'PY'`, and that is the form multi-line ad-hoc Python actually
    takes — a `-c` payload cannot carry newlines comfortably, so anything
    non-trivial is written as a heredoc. Measured against this very gate: the
    `-c` spelling DENIED and both heredoc spellings were APPROVED.

    That gap is not academic. This gate was built FROM rb-5255, in which the
    author reintroduced bare-bash within an hour of sweeping it from 12 sites.
    On 2026-07-26 the same author reintroduced it three more times in one
    session — every one a heredoc, so the gate that exists precisely to catch
    this never fired. The rule was right, the gate was right, and the aim was
    wrong. Coverage of the FORM the code is actually written in is the whole
    load-bearing property of an authoring-time gate.

    Deliberately regex, not shlex: shlex.split mangles heredoc bodies (that is
    why extract_inline_python documents avoiding regex for ITS job — the two
    extractors want opposite tools). Fail-open on anything unparseable.
    """
    if not command or "<<" not in command:
        return []
    out: list[str] = []
    try:
        for m in _HEREDOC_RE.finditer(command):
            delim = m.group(1) or m.group(2) or m.group(3)
            if not delim:
                continue
            rest = command[m.end():]
            body_lines: list[str] = []
            for line in rest.split("\n")[1:]:   # skip remainder of opener line
                if line.strip() == delim:
                    break
                body_lines.append(line)
            if body_lines:
                out.append("\n".join(body_lines))
    except Exception:
        return []   # never block on an extractor bug
    return out


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

    snippets = extract_inline_python(command) + extract_heredoc_python(command)
    if not snippets:
        approve_no_mutation()

    engine = _load_engine()
    if engine is None:
        # SILENT-BREAKAGE PATH. Inline Python was present and extracted, but the
        # detection engine could not be loaded, so the command is approved
        # WITHOUT having been checked. Indistinguishable from a clean approval
        # unless it is recorded (guard-502's rationale: a branch with no record
        # reads identically to "gate never ran").
        _log("fail_open", path="engine_unavailable")
        approve_no_mutation()

    findings: list[str] = []
    for src in snippets:
        try:
            hits = engine.scan_source(src, "<inline>")
        except SyntaxError:
            continue  # fragment / not standalone-parseable — approve
        except Exception as exc:
            # SILENT-BREAKAGE PATH, same class as engine_unavailable: the
            # detector threw, so this payload was never actually scanned.
            _log("fail_open", path="scan_source_raised", error=repr(exc)[:200])
            approve_no_mutation()
        for lineno, form, detail in hits:
            findings.append(f"  line {lineno} [{form}] {detail}")

    if findings:
        # Log BEFORE emit_deny: emit_deny terminates the process.
        _log("block", path="bare_bash_argv_found",
             form="; ".join(f.strip() for f in findings)[:200])
        emit_deny(DENY_TEMPLATE.format(findings="\n".join(findings)))
    approve_no_mutation()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Bottom catch-all: a broken hook must never block legitimate work.
        pass
