"""Meta-coverage pin over core/githooks/pre-commit's _gate list ().

Three pins, none of which duplicate the per-gate test files:

  1. HOOK SHAPE — the exact argv the hook uses for each gate is pinned, so a
     rewiring (dropped gate, swapped interpreter, changed flag) fails a test.
     This is the guard-914-style pin the goal asked for.
  2. GATE EXISTS — every _gate line names a script that is on disk.
  3. COVERAGE — every gate is exercised by some test, with an EXPLICIT allowlist
     of the known-uncovered. A 14th gate arriving uncovered fails loudly instead
     of joining silently, and shrinking the allowlist without adding a test also
     fails.

WHY THE COVERAGE PREDICATE IS SHAPED THIS WAY. The goal's own count moved
8 -> 5 -> 4 across three probes, and re-measuring on 2026-08-29 moved it again,
because a name-grep is wrong in BOTH directions:

  * FALSE POSITIVE — `check-no-python-cli-fallback.sh` and
    `check-settings-deny-baseline.py` are each named only inside a DOCSTRING or
    COMMENT of a test for a different gate ("copied from ...", "(see ...)").
    A plain grep scores both as covered. Neither is tested. Hence: comments and
    docstrings are stripped before matching.
  * FALSE NEGATIVE — `check-mind-api-endpoint-registry.py` is covered by
    test_check_daemon_endpoint_registry.sh and `check-sh-exec-bits.sh` by
    test-check-sh-exec-bits-staged.sh. A `*.py`-only glob cannot see the first;
    a `test_`-prefix glob cannot see the second (it is hyphenated). Hence: all
    extensions, both prefixes.

So the allowlist below is not a to-do list inherited from the goal -- it is what
the corrected predicate MEASURES, and it is the artifact that stops the count
from drifting a fifth time. guard-5501, rb-6205, guard-1802.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOK = REPO / "core" / "githooks" / "pre-commit"
TEST_DIRS = [REPO / "core" / "scripts" / "tests", REPO / "core" / "tests",
             REPO / "mind_api" / "tests"]

GATE_RE = re.compile(r"^\s*_gate\s+(\S+)\s+(.*)$")

# Pinned argv per gate, exactly as core/githooks/pre-commit invokes it.
# "$PY" and "$REPO" are left as the hook's literal tokens on purpose: this pins
# the SHAPE, and expanding them here would let an interpreter swap pass.
EXPECTED = {
    "check-no-python-cli-fallback":    'bash "$REPO/core/scripts/check-no-python-cli-fallback.sh"',
    "check-no-daemon-wrapper-reparse": 'bash "$REPO/core/scripts/check-no-daemon-wrapper-reparse.sh"',
    "check-mind-api-endpoint-registry": '$PY "$REPO/core/scripts/check-mind-api-endpoint-registry.py"',
    "meta-imports-world-gate":         '$PY "$REPO/core/scripts/meta-imports-world-gate.py"',
    "layer1-no-runtime-imports-gate":  '$PY "$REPO/core/scripts/layer1-no-runtime-imports-gate.py"',
    "check-settings-deny-baseline":    '$PY "$REPO/core/scripts/check-settings-deny-baseline.py"',
    "check-no-bare-agent-prefix":      'bash "$REPO/core/scripts/check-no-bare-agent-prefix.sh"',
    "check-no-hardcoded-secrets":      'bash "$REPO/core/scripts/check-no-hardcoded-secrets.sh"',
    "session-manifest-gate":           'bash "$REPO/core/scripts/session-manifest-gate.sh"',
    "skill-edit-precommit-gate":       '$PY "$REPO/core/scripts/skill-edit-precommit-gate.py"',
    "check-no-ownership-flag":         'bash "$REPO/core/scripts/check-no-ownership-flag.sh"',
    "check-no-bare-bash":              '$PY "$REPO/core/scripts/check-no-bare-bash.py"',
    "check-sh-exec-bits":              'bash "$REPO/core/scripts/check-sh-exec-bits.sh" --staged',
}

# Gates with NO test that exercises them. Measured 2026-08-29 with the corrected
# predicate below. Both were scored "covered" by the goal's original name-grep;
# both are only named in another gate's prose. Shrink this list by writing a
# test, never by editing it alone.
KNOWN_UNCOVERED = {
    "check-no-python-cli-fallback.sh",
    "check-settings-deny-baseline.py",
}

_DOCSTRING_RE = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')


def _code_only(text: str) -> str:
    """Strip docstrings and comment lines. A gate named only in prose is NOT covered."""
    text = _DOCSTRING_RE.sub("", text)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _gate_lines():
    out = []
    for line in HOOK.read_text(encoding="utf-8").splitlines():
        m = GATE_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def _test_sources():
    for d in TEST_DIRS:
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix in (".py", ".sh") and p.name != Path(__file__).name:
                    try:
                        yield p, _code_only(p.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue


GATES = _gate_lines()


def test_hook_still_has_gates():
    """Guards the parser itself: a regex that matches nothing would make every
    parametrized test below vacuously pass (the guard-1715 empty-population class)."""
    assert len(GATES) >= 13, f"only {len(GATES)} _gate lines parsed from {HOOK}"


def test_gate_argv_shape_is_pinned():
    """PIN 1 -- a hook rewiring fails here."""
    assert {g for g, _ in GATES} == set(EXPECTED), (
        "the hook's gate SET changed; update EXPECTED and add coverage for any new gate"
    )
    for gate_id, argv in GATES:
        assert argv == EXPECTED[gate_id], f"{gate_id} invocation changed:\n  hook: {argv}\n  pin:  {EXPECTED[gate_id]}"


@pytest.mark.parametrize("gate_id,argv", GATES, ids=[g for g, _ in GATES])
def test_gate_script_exists(gate_id, argv):
    """PIN 2."""
    m = re.search(r'\$REPO/(\S+?)"', argv)
    assert m, f"could not extract a script path from: {argv}"
    assert (REPO / m.group(1)).is_file(), f"{gate_id} names a missing script: {m.group(1)}"


def test_every_gate_is_covered_or_explicitly_allowlisted():
    """PIN 3 -- the drift stopper. Matches across BOTH extensions and BOTH test-file
    prefixes, and only outside comments/docstrings."""
    sources = list(_test_sources())
    assert sources, "no test sources found -- predicate is blind, not clean"

    uncovered = set()
    for _gate_id, argv in GATES:
        m = re.search(r'\$REPO/core/scripts/(\S+?)"', argv)
        assert m, argv
        basename = m.group(1)
        if not any(basename in body for _p, body in sources):
            uncovered.add(basename)

    assert uncovered == KNOWN_UNCOVERED, (
        f"pre-commit gate coverage moved.\n"
        f"  newly uncovered (write a test): {sorted(uncovered - KNOWN_UNCOVERED)}\n"
        f"  newly covered (drop from KNOWN_UNCOVERED): {sorted(KNOWN_UNCOVERED - uncovered)}"
    )
