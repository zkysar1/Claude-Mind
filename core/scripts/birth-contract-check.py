#!/usr/bin/env python3
"""birth-contract-check -- report what a mind lacks from the pre-landed
(headless) birth contract (g-377-32).

A mind reaches its first autonomous boot by one of two routes:

  1. The /start interview. agent-state is absent (UNINITIALIZED) and so is the
     `.initialized` marker, so Phase C runs: it installs the hook slots (C0.5),
     The Program (C1), curriculum stages (C6) and self.md (C7), and it stops
     for a human at C5.
  2. PRE-LANDED. A provisioner writes the agent's `.initialized` marker, and
     optionally an IDLE agent-state, BEFORE /start. /start takes the IDLE branch
     into /boot or, with no agent-state, resumes the marked agent through Phase
     A-0 (transplant resume). Nothing is asked either way.

Route 2 is the only headless route, and nothing checked that it carried what
route 1 would have installed. This script is that check. The contract is
written down in core/config/conventions/session-state.md, section
"Pre-Landed Birth Contract"; ELEMENT_IDS below is its executable twin, and
test_birth_contract_check.py fails when the two disagree.

Design notes:
- Placeholders do not count. The init scripts seed self.md and program.md as
  zero-byte files and curriculum.yaml as `current_stage: null, stages: []`.
  Each of those exists on disk and is still MISSING here.
- An element this script cannot READ is reported missing, with the read error
  as the reason. It is never skipped: a detector whose "cannot tell" answer is
  silent turns every failure it cannot read into zero signal (guard-7231).
- The hook-slot list is DERIVED, not listed: one slot per
  core/config/templates/<slot>-default.md, the set /start C0.5 copies. If that
  glob finds nothing, the derivation itself is reported missing rather than
  read as "no slots required".
- The two route elements follow /start's own branching. session-state-get.sh
  reads an absent agent-state as UNINITIALIZED, which runs the interview only
  when the `.initialized` marker is absent too. The marker is required on its
  own: without it the first boot's init-agent.sh treats the agent as new and
  re-seeds curriculum.yaml, profile.yaml and developmental-stage.yaml.
- The curriculum predicate mirrors curriculum.py cmd_status: a truthy
  current_stage that names an entry of `stages` by id. It is re-implemented
  here because curriculum-status.sh is daemon-only and a boot-time check must
  not depend on a daemon; the tests pin it against cmd_status.
- Read-only. It reports and never installs anything.

Exit: 0 = every element present; 1 = at least one missing; 2 = usage error.
The boot-time caller (init-mind.sh) ignores the exit code on purpose: a mind
that lacks an element should boot and say so, not fail to boot.

Usage:
  python3 core/scripts/birth-contract-check.py <agent-name>
  MIND_AGENT=<name> python3 core/scripts/birth-contract-check.py
"""

import argparse
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent

CONTRACT_REF = ("core/config/conventions/session-state.md, section "
                "\"Pre-Landed Birth Contract\"")

# The fixed elements, in contract order. Hook slots follow as "hook-slot:<slot>".
ELEMENT_IDS = ("agent-initialized", "agent-state", "self-md", "program-md",
               "curriculum-stage")
HOOK_SLOT_PREFIX = "hook-slot:"
TEMPLATE_SUFFIX = "-default.md"
VALID_STATES = ("IDLE", "RUNNING")


def _element(eid, path, reason):
    """reason None = present; otherwise the reason it is missing."""
    return {"id": eid, "path": str(path) if path is not None else None,
            "present": reason is None, "reason": reason}


def _read_text(path):
    """(text, None) on success, (None, reason) when absent or unreadable.

    The read is the existence test. An exists() pre-check re-raises every stat
    error except ENOENT and ENOTDIR (a permission-denied parent, a name too
    long), which would end the report in a traceback instead of a reason.
    """
    try:
        return path.read_bytes().decode("utf-8", errors="replace"), None
    except FileNotFoundError:
        return None, "file absent"
    except OSError as e:
        return None, f"unreadable: {e}"


def _non_blank(eid, path, placeholder_note):
    text, reason = _read_text(path)
    if reason is None and not text.strip():
        reason = f"empty ({placeholder_note})"
    return _element(eid, path, reason)


def _check_state(path, initialized):
    text, reason = _read_text(path)
    if reason is not None and reason != "file absent":
        return _element("agent-state", path, reason)
    # Read it as session-state-get.sh prints it: absent is UNINITIALIZED, and a
    # present file loses every whitespace character (tr -d '[:space:]'), so a
    # CRLF-written IDLE still reads as IDLE.
    value = "UNINITIALIZED" if reason else "".join(text.split())
    if value == "UNINITIALIZED":
        # With the marker, /start Phase A-0 resumes the agent headless.
        reason = None if initialized else (
            "UNINITIALIZED and no .initialized marker, so /start would run the interview")
    elif value not in VALID_STATES:
        reason = f"value {value!r} is not IDLE, RUNNING or UNINITIALIZED"
    return _element("agent-state", path, reason)


def _check_curriculum(path):
    text, reason = _read_text(path)
    if reason is None:
        try:
            import yaml
        except ImportError:
            return _element("curriculum-stage", path, "unreadable: PyYAML is not installed")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return _element("curriculum-stage", path, f"unreadable: {e}")
        if not isinstance(data, dict):
            reason = "unreadable: top level is not a mapping"
        elif not data.get("current_stage"):
            reason = "no current_stage (the init seed is current_stage: null)"
        else:
            stage, stages = data["current_stage"], data.get("stages") or []
            if not isinstance(stages, list):
                reason = f"unreadable: stages is a {type(stages).__name__}, not a list"
            else:
                ids = [s.get("id") for s in stages if isinstance(s, dict)]
                if stage not in ids:
                    reason = f"current_stage {stage!r} is not among stages {ids}"
    return _element("curriculum-stage", path, reason)


def _check_hook_slots(world_dir, templates_dir):
    templates = sorted(templates_dir.glob("*" + TEMPLATE_SUFFIX)) if templates_dir.is_dir() else []
    if not templates:
        return [_element(HOOK_SLOT_PREFIX + "*", templates_dir,
                         f"no *{TEMPLATE_SUFFIX} template found, so the slot list cannot be derived")]
    out = []
    for t in templates:
        slot = t.name[:-len(TEMPLATE_SUFFIX)]
        eid = HOOK_SLOT_PREFIX + slot
        if world_dir is None:
            out.append(_element(eid, None, "world directory is not configured"))
        else:
            out.append(_non_blank(eid, world_dir / "conventions" / f"{slot}.md",
                                  "an empty slot runs no steps"))
    return out


def check(agent_dir, state_dir, world_dir, templates_dir):
    """Return one element dict per contract element, in contract order.

    Every path is passed in, resolved by the caller, so this function reads
    only what it is handed. world_dir may be None (world not configured).
    """
    agent_dir, state_dir, templates_dir = Path(agent_dir), Path(state_dir), Path(templates_dir)
    world_dir = Path(world_dir) if world_dir is not None else None
    marker = agent_dir / ".initialized"
    _, marker_reason = _read_text(marker)
    if marker_reason == "file absent":
        marker_reason = ("marker absent (init-agent.sh would treat the agent as new and re-seed "
                         "curriculum.yaml, profile.yaml and developmental-stage.yaml)")
    elements = [
        _element("agent-initialized", marker, marker_reason),
        _check_state(state_dir / "agent-state", marker_reason is None),
        _non_blank("self-md", agent_dir / "self.md", "init-agent.sh seeds a zero-byte placeholder"),
    ]
    if world_dir is None:
        elements.append(_element("program-md", None, "world directory is not configured"))
    else:
        elements.append(_non_blank("program-md", world_dir / "program.md",
                                   "init-world.sh seeds a zero-byte placeholder"))
    elements.append(_check_curriculum(agent_dir / "curriculum.yaml"))
    elements.extend(_check_hook_slots(world_dir, templates_dir))
    return elements


def render(agent, elements):
    missing = [e for e in elements if not e["present"]]
    total = len(elements)
    if not missing:
        return [f"[birth-contract] {agent}: {total}/{total} elements present"]
    lines = [f"[birth-contract] WARNING {agent}: {len(missing)} of {total} birth-contract "
             f"element(s) MISSING. The /start interview installs these; a pre-landed "
             f"(headless) birth must supply them itself. Report only, nothing was "
             f"changed. Contract: {CONTRACT_REF}"]
    for e in missing:
        lines.append(f"[birth-contract]   MISSING {e['id']} ({e['path'] or 'no path'}): {e['reason']}")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="birth-contract-check.py",
        description="Report which pre-landed birth-contract elements a mind lacks.")
    ap.add_argument("agent", nargs="?", default=None,
                    help="agent name (default: $MIND_AGENT)")
    args = ap.parse_args(argv)
    agent = args.agent or os.environ.get("MIND_AGENT") or ""
    if not agent or agent in (".", "..") or Path(agent).name != agent:
        print("birth-contract-check: an agent name is required (argument or MIND_AGENT), "
              f"and it must be a bare directory name; got {agent!r}", file=sys.stderr)
        return 2

    # Bind the NAMED agent before _paths resolves WORLD_DIR at import ().
    # Unbound, it resolved the caller's agent or the first agent, so program-md
    # and the hook slots were read from another agent's world. The seed export
    # renames this token and the one _paths reads together (G2, rb-9407). An
    # inherited MIND_WORLD still wins, as for every _paths caller, which is why
    # init-mind.sh binds before it sources _paths.sh.
    os.environ["MIND_AGENT"] = agent
    sys.path.insert(0, str(SCRIPT_DIR))
    from _paths import CORE_ROOT, WORLD_DIR, agent_dir, agent_state_dir

    elements = check(agent_dir(agent), agent_state_dir(agent), WORLD_DIR,
                     CORE_ROOT / "config" / "templates")
    for line in render(agent, elements):
        print(line)
    return 0 if all(e["present"] for e in elements) else 1


if __name__ == "__main__":
    sys.exit(main())
