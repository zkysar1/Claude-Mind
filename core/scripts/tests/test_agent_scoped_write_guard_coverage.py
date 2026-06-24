"""test_agent_scoped_write_guard_coverage.py — regression for .

FW-2 (g-115-957 / 7-agent feedback distillation) added the missing-agent-header
guard to agent-scoped write endpoints: a source=agent / agent-scoped write with
an empty X-Mind-Agent header must be REFUSED (400), NOT silently routed to the
alphabetically-first agent's dir via AgentPathResolver._first_available_agent()
— the footgun that nearly let bravo stomp alpha's completed g-001-240 (2026-05-25).

g-317-11 audited EVERY agent-scoped write endpoint under mind_api/src/ for the
guard. Finding (2026-06-13): coverage is COMPLETE via two mechanisms —

  1. EXPLICIT guard (_require_agent_header / _require_explicit_agent) called in
     every write handler: aspirations_write, store, experience_write, wm_write,
     curriculum. aspirations_write gates on source=="agent" (it has a shared
     world queue too); the rest are unconditional (always agent-scoped).

  2. STRUCTURAL guard: history.py._resolve_base_dir appends the agent base
     branch ONLY under `if _agent_header(ctx):`, so a headerless agent-path
     restore/prune resolves to (None, None) -> 400 instead of falling back to
     _first_available_agent. (changelog.py is read-only; its append is a
     side-effect of the already-guarded write endpoints.)

World/meta-scoped write endpoints (board_write, team_state_write, pipeline_write,
pattern_signatures_write, tree_write, spark_questions_write, meta_*) use the
header for ATTRIBUTION only and target SHARED world/meta paths, so a missing
header -> the "system" default is safe (no wrong-agent-dir stomp). They are
intentionally NOT in REGISTRY.

This test PINS that coverage so a future edit cannot silently drop a guard.
If you add/remove an agent-scoped write HANDLER, keep the guard call and update
the baseline count below. If you add a NEW agent-scoped write MODULE, add it to
REGISTRY (the resolver footgun reaches EVERY agent-scoped write endpoint —
the whole point of g-317-11). Known limitation: a brand-new unguarded handler
added to a registered module without bumping the count is not auto-caught — the
behavioral defense is that AgentPathResolver still substitutes first-available,
so the audit RB documents the invariant for handler authors.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "mind_api" / "src"

# module rel-path -> (guard_func, min_callsites).   audit baseline 2026-06-13.
REGISTRY = {
    "endpoints/aspirations_write.py": ("_require_explicit_agent", 14),
    "endpoints/store.py":             ("_require_agent_header",   5),
    "endpoints/experience_write.py":  ("_require_agent_header",   6),
    "endpoints/wm_write.py":          ("_require_agent_header",   7),
    "endpoints/curriculum.py":        ("_require_agent_header",   5),
}


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_explicit_guard_defined_and_called():
    """Every registered agent-scoped write module defines its require-agent
    guard AND calls it at >= the audited number of write handlers."""
    for rel, (guard, min_calls) in REGISTRY.items():
        src = _read(rel)
        assert re.search(rf"^def {re.escape(guard)}\b", src, re.M), (
            f"{rel}: guard {guard}() not defined — agent-scoped write endpoint "
            f"lost its missing-agent-header guard (FW-2 / g-317-11).")
        # `\bguard(` matches the def line + every call site; subtract the 1 def.
        refs = len(re.findall(rf"\b{re.escape(guard)}\(", src))
        callsites = refs - 1
        assert callsites >= min_calls, (
            f"{rel}: {guard} called {callsites}x, expected >= {min_calls} "
            f"(g-317-11 audit baseline). A write handler likely dropped the "
            f"guard — re-audit and restore it, or lower the baseline only if a "
            f"handler was legitimately removed.")


def test_history_structural_guard():
    """history.py refuses headerless agent-path writes structurally:
    _resolve_base_dir appends the agent base ONLY inside `if _agent_header(ctx):`,
    so an agent-dir file with no header resolves to (None, None) -> 400."""
    src = _read("endpoints/history.py")
    assert "if _agent_header(ctx):" in src, (
        "history.py lost the conditional agent-base branch — a headerless "
        "agent-path restore/prune could now resolve via _first_available_agent "
        "(g-317-11 structural guard).")
    cond = src.index("if _agent_header(ctx):")
    agent_append = src.index('ctx.paths.agent), "agent"')
    assert agent_append > cond, (
        "history.py: the agent-base append moved OUTSIDE the header conditional "
        "— headerless agent-path resolution is no longer refused (g-317-11).")


def test_registry_modules_exist():
    """Guards against a module rename silently disabling a REGISTRY check."""
    for rel in REGISTRY:
        assert (SRC / rel).is_file(), f"REGISTRY module missing on disk: {rel}"


if __name__ == "__main__":
    test_explicit_guard_defined_and_called()
    test_history_structural_guard()
    test_registry_modules_exist()
    print("ok")
