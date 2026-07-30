"""Pins the ORDER-CRITICAL invariant shared by the session-binding hook family
(g-115-3944): agent resolution MUST happen BEFORE `source _platform.sh`.

WHY THIS EXISTS. `_platform.sh` exports `MSYS_NO_PATHCONV=1`. Under that flag Git
Bash stops rewriting MSYS paths into Windows form for native binaries, so
`session-binding-read.sh` — which calls `py -3 "$SCRIPT_DIR/_session_binding.py"`,
and `py.exe` is native — receives a literal `/c/...`, mangles it to `C:\\c\\...`,
and exits rc=2. Every caller swallows that with `2>/dev/null || true`, so the agent
name lands EMPTY and the hook exits 0 having emitted nothing: indistinguishable
from "nothing to warn about". Sourcing the platform helper one block too early
therefore kills the hook silently, on Windows only.

WHY A SOURCE-ORDER ASSERTION AND NOT AN END-TO-END ONE. The mechanism is a
Windows/MSYS path-conversion behaviour with no effect on Linux, so the failing
half cannot be reproduced off Windows at all — the refuse-case that
`guard-1451` normally requires is unavailable for the END-TO-END behaviour.
Stating that limit rather than shipping a structure-only test silently is the
point. Two things make this more than a grep:

  * The `check_ordering` predicate below is itself exercised in BOTH directions —
    `test_checker_rejects_inverted_order` mutates a real hook and requires a
    failure, `test_checker_ignores_the_order_critical_comment` requires a pass on
    the shape that fools a naive matcher. So the assertion has a measured
    refuse-case and allow-case even though the platform behaviour does not.
  * The observable CONSEQUENCE is covered where it can be observed:
    `test_pre_edit_context_gate.py::test_production_shape_advisory_fires_without_agent_env`
    asserts the gate resolves an agent from the session binding with no
    `MIND_AGENT` in the environment — the exact path this ordering protects.

WHY THE COMMENT-STRIPPING IS LOAD-BEARING. Every one of these files mentions
`_platform.sh` in an ORDER-CRITICAL comment placed ABOVE the agent-resolution
block. A matcher that greps raw lines finds that comment first and reports the
invariant VIOLATED in all four files — measured during the authoring of this test,
which is how the requirement was found. `check_ordering` compares STATEMENTS only.

The invariant has silently killed the pre-edit gate twice inside two months (59
days via an env-var bail the hook never satisfies, then again the same day it was
revived). Both times it hand-tested green, because a hand-run shell has neither the
injected env var nor the path-conversion flag — the coverage-blind-spot class
`guard-1908` names.
"""
import re
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent

_PLATFORM_SRC = re.compile(r"^\s*(source|\.)\s+.*_platform\.sh")
_BINDING_CALL = re.compile(r"session-binding-read\.sh")

# The covered population is DERIVED, not listed. A hand-maintained list is the
# same mirror-drift defect this session encoded as rb-5785 (a watch-set and the
# corpus it guards drift apart silently, and the drift is invisible because the
# check keeps passing on the stale set). A hook added to the family tomorrow is
# covered tomorrow, with no edit here. KNOWN_MEMBERS below is a floor, not the
# population — see test_discovery_is_not_silently_empty.
KNOWN_MEMBERS = {
    "pre-edit-context-gate.sh",
    "context-reads-record.sh",
    "context-reads-gate.sh",
    "context-reads-skill-gate.sh",
}


def _discover_hooks():
    """Every .sh that BOTH resolves an agent from the session binding AND sources
    the platform helper as statements. A script doing only one is not subject to
    the invariant and must not be flagged."""
    found = []
    for path in sorted(CORE_SCRIPTS.glob("*.sh")):
        try:
            stmts = _statements(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        has_plat = any(_PLATFORM_SRC.search(line) for _, line in stmts)
        has_bind = any(_BINDING_CALL.search(line) for _, line in stmts)
        if has_plat and has_bind:
            found.append(path.name)
    return found


def _statements(text):
    """(1-based line number, line) for lines that are neither blank nor a
    whole-line comment. Trailing comments are truncated, so a `#` mention after
    real code cannot be mistaken for a statement either."""
    out = []
    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((i, raw.split("#", 1)[0]))
    return out


def check_ordering(text):
    """(ok, detail). ok is True when the platform source comes strictly after the
    binding call, or when either is absent (nothing to order)."""
    stmts = _statements(text)
    plat = next((n for n, line in stmts if _PLATFORM_SRC.search(line)), None)
    bind = next((n for n, line in stmts if _BINDING_CALL.search(line)), None)
    if plat is None or bind is None:
        return True, f"not applicable (platform_src={plat}, binding_call={bind})"
    if bind < plat:
        return True, f"binding_call L{bind} precedes platform_src L{plat}"
    return False, (
        f"platform_src L{plat} precedes binding_call L{bind} — "
        "MSYS_NO_PATHCONV will be set before the binding is resolved, so the "
        "hook goes silently inert on Windows"
    )


# Evaluated at import so the parametrize decorators below see the live set.
HOOKS = _discover_hooks()


def test_discovery_is_not_silently_empty():
    """A derived population removes drift but adds a failure mode a hardcoded
    list does not have: if discovery breaks, the parametrized tests below
    silently cover NOTHING and the suite still reports green (guard-1639 — a
    loop that asserts only inside itself proves nothing on an empty collection).
    Pin the floor so that cannot happen quietly."""
    assert HOOKS, "hook discovery returned an EMPTY set — the ordering assertions cover nothing"
    missing = KNOWN_MEMBERS - set(HOOKS)
    assert not missing, (
        f"discovery no longer finds known family members {sorted(missing)} — "
        "either they were renamed (update KNOWN_MEMBERS) or discovery regressed"
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_agent_is_resolved_before_platform_is_sourced(hook):
    path = CORE_SCRIPTS / hook
    assert path.exists(), f"{hook} missing — update HOOKS if it was renamed"
    ok, detail = check_ordering(path.read_text(encoding="utf-8"))
    assert ok, f"{hook}: {detail}"


@pytest.mark.parametrize("hook", HOOKS)
def test_each_hook_carries_the_rationale(hook):
    """A future editor must be able to see WHY the line is where it is. Without
    this, the ordering reads as arbitrary and gets 'tidied' — which is exactly
    how it was broken before."""
    text = (CORE_SCRIPTS / hook).read_text(encoding="utf-8")
    assert "ORDER-CRITICAL" in text, (
        f"{hook}: ordering is correct but unexplained — an unexplained "
        "constraint is one refactor away from being removed"
    )
    assert "MSYS_NO_PATHCONV" in text, (
        f"{hook}: ORDER-CRITICAL present but does not name the mechanism"
    )


def test_checker_rejects_inverted_order(tmp_path):
    """REFUSE-CASE (guard-1451 / guard-1475). Mutate a real hook by hoisting its
    platform source above the binding call and require the checker to fail. This
    is what makes the four passing assertions above evidence rather than a
    tautology — a checker that never fails proves nothing."""
    text = (CORE_SCRIPTS / "pre-edit-context-gate.sh").read_text(encoding="utf-8")
    stmts = _statements(text)
    plat_line = next(n for n, line in stmts if _PLATFORM_SRC.search(line))
    lines = text.splitlines()
    hoisted = lines.pop(plat_line - 1)
    bind_idx = next(
        i for i, line in enumerate(lines)
        if _BINDING_CALL.search(line) and not line.strip().startswith("#")
    )
    lines.insert(bind_idx, hoisted)
    mutated = tmp_path / "mutated.sh"
    mutated.write_text("\n".join(lines), encoding="utf-8", newline="")

    ok, detail = check_ordering(mutated.read_text(encoding="utf-8"))
    assert not ok, f"checker passed an inverted file — it cannot detect the defect: {detail}"
    assert "precedes binding_call" in detail


def test_checker_ignores_the_order_critical_comment():
    """SPECIFICITY. Every hook mentions `_platform.sh` in a comment placed ABOVE
    the agent-resolution block. A raw-line matcher reports all four VIOLATED —
    measured while authoring this test. The checker must read statements only."""
    text = (
        "#!/usr/bin/env bash\n"
        "# ORDER-CRITICAL: must stay BEFORE `source _platform.sh`; MSYS_NO_PATHCONV\n"
        "# (set by _platform.sh) breaks session-binding-read.sh on Git Bash.\n"
        'AGENT_NAME="${MIND_AGENT:-}"\n'
        'AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$sid")"\n'
        'source "$CORE_ROOT/scripts/_platform.sh"\n'
    )
    ok, detail = check_ordering(text)
    assert ok, f"checker was fooled by the comment: {detail}"


def test_checker_is_silent_when_the_pair_is_absent():
    """A hook that does neither is not in scope — the checker must not invent a
    violation for it (that is how a shared assertion starts crying wolf and gets
    disabled)."""
    ok, detail = check_ordering("#!/usr/bin/env bash\necho hello\n")
    assert ok and "not applicable" in detail
