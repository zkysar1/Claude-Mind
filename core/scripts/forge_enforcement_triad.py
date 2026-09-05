#!/usr/bin/env python3
# domain-leak-exempt: emits scaffold text containing example gate/tool names; the
# sample spec uses synthetic placeholders only (widget-service style), never domain terms.
"""Scaffold generator for the enforcement-triad shape (gap-035).

THE SHAPE, as it exists three times in this repo (schedule-wakeup-gate,
bare-bash-authoring-gate, gradle-tests-gate):

    _<slug>_predicate.py        shared predicate module -- the SSOT
    <name>-gate.py              PreToolUse gate, reads the payload via hook_helpers
    <name>-gate.sh              thin wrapper carrying the hook-fire sentinel
    .claude/rules/<rule>.md     Layer-B behavioural rule
    <name>-audit.py             Layer-C detective over the committed corpus
    tests/test_<slug>_gate.py   behavioural subprocess tests (guard-1451)
    .claude/settings.json       one hook entry

WHY A GENERATOR AND NOT A TEMPLATE DIRECTORY: the pieces are not independent
files, they are one shape with a name threaded through it at eleven sites
(three of them inside a single bash line in the wrapper). Hand-authoring the
third instance "took most of a goal and re-derived nothing new" -- and the
failure mode of hand-authoring is not a typo, it is a MISSING PIECE: a triad
shipped without its Layer-C detective looks complete and is not.

WHAT THIS GENERATOR DELIBERATELY DOES NOT DO -- read this before extending it:

  1. It does NOT write the predicate. `decide()` is emitted as a stub that
     RAISES NotImplementedError. Deciding what to refuse is the analytical
     half of the work and is exactly the part a scaffold cannot do; a
     generator that emitted a plausible-looking predicate would produce a
     gate that fires on the wrong condition while looking finished. The stub
     fails loudly instead. gap-035 registers this procedure as type=utility
     for the SCAFFOLD only.

  2. It does NOT edit .claude/settings.json as a side effect. That file is
     agent-editable but is gated by the fail-closed settings-structural-
     validator, and a generator that rewrites it on every run is a footgun.
     The hook entry is RETURNED as text so the caller applies it deliberately.

  3. It performs NO file I/O at all. Every function here returns strings.
     The writing lives in the .sh wrapper, which makes the whole generator
     testable on fixtures without a filesystem -- the same split that made
     the last forged skill mutation-provable.

FAIL-OPEN IS THE EMITTED CONTRACT, NOT THIS MODULE'S: the generated gate
approves on any parse/IO/logic error, because a broken gate must never block
a loop. That contract is baked into the emitted text and is not configurable
here -- a fail-CLOSED enforcement gate is a different shape and should not be
produced by copying this one.
"""

from __future__ import annotations

import re
from typing import Dict

# The eleven substitution sites, grouped by the file each lands in. Kept as a
# module constant because the test suite asserts against it: if a future edit
# adds a site to a template without adding it here, the parity test fails.
SUBSTITUTION_SITES = {
    "gate.sh": ("hook-fire comment", "hook-fire sentinel path", "SCRIPT_PATH"),
    "gate.py": ("docstring", "predicate import", "tool-name check"),
    "predicate.py": ("module docstring", "OVERRIDE_TOKEN"),
    "audit.py": ("docstring", "predicate import"),
    "rule.md": ("title",),
}

# A gate whose tool is Bash reads a different payload field than one whose tool
# is a structured tool. This is the ONE place the difference is expressed.
TOOL_FIELD = {
    "Bash": "command",
    "Write": "content",
    "Edit": "new_string",
    "ScheduleWakeup": "prompt",
}


class SpecError(ValueError):
    """Raised for a spec that cannot produce a coherent triad."""


def slug_of(name: str) -> str:
    """`my-gate` -> `my_gate`. Python module names cannot carry hyphens."""
    return name.replace("-", "_")


def override_token_of(name: str) -> str:
    """`my-gate` -> `MY_GATE_OVERRIDE`, matching the three live instances."""
    return slug_of(name).upper() + "_OVERRIDE"


def validate(spec: Dict[str, str]) -> None:
    """Refuse a spec that would emit a broken or misleading triad.

    Every check here corresponds to a way a hand-authored triad has actually
    been got wrong, or to a name that cannot work mechanically.
    """
    for key in ("name", "tool", "purpose"):
        if not spec.get(key):
            raise SpecError(f"spec is missing required key: {key!r}")

    name = spec["name"]
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise SpecError(
            f"name {name!r} must be lowercase kebab-case (CLAUDE.md naming rules); "
            "it becomes a filename, a Python module name and a sentinel path")
    if name.endswith("-gate"):
        raise SpecError(
            f"name {name!r} must NOT end in '-gate' -- the suffix is added by the "
            "generator, and a name carrying it produces 'foo-gate-gate.py'")

    tool = spec["tool"]
    if tool not in TOOL_FIELD:
        raise SpecError(
            f"tool {tool!r} is not one this scaffold knows how to read a payload "
            f"from. Known: {sorted(TOOL_FIELD)}. Adding one means adding its "
            "payload field to TOOL_FIELD, not special-casing the template.")


def render(spec: Dict[str, str]) -> Dict[str, str]:
    """Return {relative_path: file_content} for the whole triad.

    Pure. No I/O, no cwd dependence, no clock. Given the same spec it returns
    byte-identical output, which is what makes the tests meaningful.
    """
    validate(spec)
    name = spec["name"]
    slug = slug_of(name)
    tool = spec["tool"]
    field = TOOL_FIELD[tool]
    token = override_token_of(name)
    purpose = spec["purpose"]
    rule = spec.get("rule_name") or f"{name}-pattern"
    goal = spec.get("goal_id", "(goal id not supplied)")

    out: Dict[str, str] = {}
    out[f"core/scripts/_{slug}_predicate.py"] = _predicate(slug, name, token, purpose, goal)
    out[f"core/scripts/{name}-gate.py"] = _gate_py(slug, name, tool, field, token, purpose, rule, goal)
    out[f"core/scripts/{name}-gate.sh"] = _gate_sh(name, tool, purpose, rule, goal)
    out[f"core/scripts/{name}-audit.py"] = _audit(slug, name, purpose, goal)
    out[f".claude/rules/{rule}.md"] = _rule(name, tool, purpose, token, goal)
    out[f"core/scripts/tests/test_{slug}_gate.py"] = _tests(slug, name, tool, field, token)
    return out


def settings_hook_entry(spec: Dict[str, str]) -> str:
    """The .claude/settings.json PreToolUse entry, as text for deliberate application.

    Returned rather than applied -- see the module docstring, point 2.
    """
    validate(spec)
    return (
        '{\n'
        f'  "matcher": "{spec["tool"]}",\n'
        '  "hooks": [\n'
        '    {\n'
        '      "type": "command",\n'
        f'      "command": "bash $CLAUDE_PROJECT_DIR/core/scripts/{spec["name"]}-gate.sh"\n'
        '    }\n'
        '  ]\n'
        '}'
    )


def _predicate(slug: str, name: str, token: str, purpose: str, goal: str) -> str:
    return f'''#!/usr/bin/env python3
"""Shared predicate for the {name} enforcement triad -- THE SINGLE SOURCE OF TRUTH.

{purpose}

Imported by BOTH {name}-gate.py (Layer A, write-time) and {name}-audit.py
(Layer C, detective). Do NOT inline this logic in either caller: the two layers
drifting apart is the specific defect this split prevents -- a gate and its
detective that disagree will each report the corpus clean for a different
half of it.

Filed under {goal}.
"""

OVERRIDE_TOKEN = "{token}"


def decide(payload_text: str) -> str | None:
    """Return a human-readable REASON to refuse, or None to allow.

    ############################################################################
    # THIS IS THE ANALYTICAL HALF AND THE SCAFFOLD DELIBERATELY LEFT IT EMPTY. #
    ############################################################################
    The generator can produce every other file in this triad correctly because
    they are mechanical. It cannot decide WHAT to refuse. Implement this, and
    write the behavioural tests that pin it, before wiring the hook.

    Contract this function must honour:
      * Return None to ALLOW. Return a non-empty string to DENY, and make the
        string say what to do instead -- a deny that only says "no" costs the
        caller a turn and teaches nothing.
      * Be PURE: no I/O, no clock, no cwd. Both callers depend on that, and the
        tests exercise it directly with no filesystem.
      * NEVER raise. The gate fails open, so an exception here is silently an
        allow -- which reads exactly like a clean corpus. Return None
        explicitly for input you do not understand.
    """
    raise NotImplementedError(
        "{name}: decide() is a scaffold stub. Implement the predicate before "
        "wiring {name}-gate.sh into .claude/settings.json -- an unimplemented "
        "gate fails OPEN and will report every payload clean.")
'''


def _gate_py(slug, name, tool, field, token, purpose, rule, goal) -> str:
    return f'''#!/usr/bin/env python3
"""PreToolUse[{tool}] hook -- Layer A of the {name} defense ({goal}).

{purpose}

The predicate lives in _{slug}_predicate.py -- shared with the Layer C audit.
The rule file (.claude/rules/{rule}.md) is Layer B.

FAIL-OPEN CONTRACT (do not change without revisiting the trade): any
parse/IO/logic error approves. A broken gate is recoverable -- the audit script
catches what it missed. A fail-CLOSED gate would block legitimate {tool} calls
and stall autonomous loops. Escape hatch: the {token} token in the payload.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)
from _{slug}_predicate import OVERRIDE_TOKEN, decide  # noqa: E402


def main() -> None:
    payload = stdin_json_or_approve()
    if payload is None:
        return approve_no_mutation()

    if payload.get("tool_name") != "{tool}":
        return approve_no_mutation()

    text = (payload.get("tool_input") or {{}}).get("{field}") or ""
    if OVERRIDE_TOKEN in text:
        return approve_no_mutation()

    try:
        reason = decide(text)
    except Exception:
        # Fail open, loudly in the log only. See the contract above.
        return approve_no_mutation()

    if reason:
        return emit_deny(reason)
    return approve_no_mutation()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        approve_no_mutation()
'''


def _gate_sh(name, tool, purpose, rule, goal) -> str:
    budget = ("per-Bash-call" if tool == "Bash" else "per-tool-call")
    return f'''#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- {budget} latency budget / hook. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit (g-115-636) — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/{name}-gate
# = last fire of this hook.
{{ _HF_DIR="${{BASH_SOURCE[0]%/*}}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/{name}-gate" 2>/dev/null ; unset _HF_DIR ; }} 2>/dev/null || true

# PreToolUse[{tool}] hook — Layer A of the {name} defense ({goal}).
# {purpose}
# The rule file (.claude/rules/{rule}.md) is Layer B; {name}-audit.py is the
# Layer C detective over the committed corpus.
#
# Thin bash wrapper. The Python body lives in {name}-gate.py because a heredoc
# on `python -` would consume stdin before json.load runs (same reason as
# bash-path-resolution-hook.sh and bare-bash-authoring-gate.sh).
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract.

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/{name}-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
'''


def _audit(slug, name, purpose, goal) -> str:
    return f'''#!/usr/bin/env python3
"""Layer C detective for the {name} triad ({goal}).

{purpose}

Reports only; never mutates. Reuses the SAME predicate the gate uses
(_{slug}_predicate.decide), so a corpus hit here and a deny there cannot
disagree.

WHY THIS LAYER EXISTS AT ALL: the gate fails open by contract, and a hook can
be skipped (timeout, an unwired settings.json, a shell that never fires it).
This sweep is what makes that recoverable -- without it, a silently-inert gate
and a genuinely clean corpus are the same observation.
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _{slug}_predicate import decide  # noqa: E402


def scan(paths):
    """Yield (path, lineno, reason) for every line the predicate refuses."""
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            try:
                reason = decide(line)
            except NotImplementedError:
                raise
            except Exception:
                continue
            if reason:
                yield (str(p), n, reason)


def main() -> int:
    ap = argparse.ArgumentParser(description="Layer C detective for {name}")
    ap.add_argument("paths", nargs="*", help="files to scan")
    ap.add_argument("--exit-on-hits", action="store_true",
                    help="exit 1 when hits are found (for CI); default reports only")
    args = ap.parse_args()

    hits = list(scan(args.paths))
    for path, n, reason in hits:
        print(f"{{path}}:{{n}}: {{reason}}")
    # POSITIVE CONTROL for the reader: a zero here means nothing unless the
    # scan actually addressed files. Print the denominator beside the count.
    print(f"[{name}-audit] {{len(hits)}} hit(s) across {{len(args.paths)}} file(s) scanned")
    return 1 if (hits and args.exit_on_hits) else 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _rule(name, tool, purpose, token, goal) -> str:
    return f'''# {name.replace("-", " ").title()}

## Principle

{purpose}

## Scope

Applies to every {tool} call in every mode (reader / assistant / autonomous).
The failure mode is mode-agnostic, so the rule is.

## Rules

1. Do not author the refused pattern. The gate refuses it at write time; this
   rule is why, so a reader who hits the deny understands the reason rather
   than reaching for the override.
2. `{token}` anywhere in the payload bypasses the gate. Use it only when you
   have established the specific call is a genuine exception, and say why in
   the same breath.
3. A bypass is not a fix. If you find yourself overriding twice for the same
   reason, the predicate is wrong -- correct the predicate, do not accumulate
   overrides.

## Enforcement

| Layer | Mechanism | What it catches |
|---|---|---|
| A — gate | `core/scripts/{name}-gate.sh` (PreToolUse[{tool}]) | the pattern at write time; fail-open by contract |
| B — rule | this file | documents the reason for human and LLM authors |
| C — detective | `core/scripts/{name}-audit.py` | drift when the gate is bypassed, times out, or is unwired |

The gate FAILS OPEN. That is deliberate -- a broken gate must never stall a
loop -- and it is exactly why layer C is not optional: an inert gate and a
clean corpus produce the same silence.

## Cross-references

- {goal} — the goal that filed this triad
- `.claude/rules/schedule-wakeup-correctness.md`, `.claude/rules/gradle-tests-pattern.md`
  — sibling instances of this same enforcement shape
'''


def _tests(slug, name, tool, field, token) -> str:
    return f'''"""Behavioural tests for the {name} gate (guard-1451: subprocess, not import).

These run the gate the way the hook runs it -- a real process, a real JSON
payload on stdin -- because an import-level test cannot see the wrapper, the
stdin contract, or the fail-open path, which is where this shape has actually
broken before.

THE STUB TEST BELOW IS LOAD-BEARING. It asserts the scaffold's predicate is
still unimplemented, and it is designed to FAIL the moment you implement
decide(). That failure is your reminder to replace this file's placeholder
cases with real ones -- delete the stub test, then pin the true predicate.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

GATE = SCRIPT_DIR / "{name}-gate.py"


def _run(payload):
    return subprocess.run(
        [sys.executable, str(GATE)], input=json.dumps(payload),
        capture_output=True, text=True)


def _payload(text, tool="{tool}"):
    return {{"tool_name": tool, "tool_input": {{"{field}": text}}}}


def test_gate_never_exits_nonzero_even_on_garbage():
    """Fail-open contract: the gate must never break the calling tool."""
    r = subprocess.run([sys.executable, str(GATE)], input="not json",
                       capture_output=True, text=True)
    assert r.returncode == 0


def test_unrelated_tool_is_approved_untouched():
    r = _run(_payload("anything", tool="SomeOtherTool"))
    assert r.returncode == 0
    assert r.stdout.strip() in ("", "{{}}") or "deny" not in r.stdout.lower()


def test_override_token_bypasses_before_the_predicate_runs():
    """The override must short-circuit ahead of decide(), so it works even
    while decide() is an unimplemented stub. If this ever fails, the ordering
    in the gate's main() was changed and the escape hatch is dead."""
    r = _run(_payload("whatever {token} whatever"))
    assert r.returncode == 0
    assert "deny" not in r.stdout.lower()


def test_predicate_is_still_a_stub_DELETE_ME_WHEN_IMPLEMENTED():
    """Deliberately fails once decide() is implemented -- see module docstring."""
    from _{slug}_predicate import decide
    import pytest
    with pytest.raises(NotImplementedError):
        decide("any input")
'''
