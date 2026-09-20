#!/usr/bin/env python3
"""Pin the shared `domain-leak-exempt` marker predicate ().

Two properties are under test and they fail differently, so they are separate:

  BOTH BRANCHES -- a token that OPENS A COMMENT claims the exemption; a prose
  mention of the same token does not. This is the defect the goal was filed
  for: an unanchored substring let any file exempt itself from the whole
  border wall by merely naming the marker (guard-6989).

  CROSS-ENGINE PARITY -- three consumers are bash and four are python, so the
  predicate ships as a canonical POSIX ERE plus a derived python pattern. Both
  are pinned against ONE shared fixture corpus so they can never drift apart
  silently (rb-1915: pin both sides to a shared fixture).

The corpus lives in the module, not here, so a future fixture is added once and
both engines are re-pinned by construction.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "_domain_leak_marker.py"

sys.path.insert(0, str(SCRIPTS))
from _runtime_bash import BASH  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("_domain_leak_marker", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


# --------------------------------------------------------------------------
# Both branches, on the shared corpus
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", M.FIXTURES)
def test_python_predicate_matches_fixture(line, expected):
    assert M.claims_exemption(line) is expected, (
        f"python predicate disagrees with the shared corpus on {line!r}"
    )


def test_corpus_covers_both_branches():
    """A corpus that drifted to one branch would pass every test above."""
    positives = [line for line, exp in M.FIXTURES if exp]
    negatives = [line for line, exp in M.FIXTURES if not exp]
    assert len(positives) >= 5, "corpus lost its comment-opening cases"
    assert len(negatives) >= 5, "corpus lost its prose-mention cases"


# --------------------------------------------------------------------------
# Cross-engine parity: the SAME corpus through real grep
# --------------------------------------------------------------------------

def _grep_says_exempt(ere: str, line: str, tmp_path: Path) -> bool:
    f = tmp_path / "fixture.txt"
    f.write_text(line + "\n", encoding="utf-8")
    # BASH is resolved explicitly rather than spelled "bash": on Windows a bare
    # "bash" resolves to the WSL launcher ahead of PATH and blocks forever
    # (guard-580). Running the predicate through a real shell is the point of
    # this test -- an interactive profile can define `grep` as a FUNCTION
    # wrapping another implementation, which is not exported to children, so a
    # hand-run check can disagree with what every script actually executes
    # (guard-7056).
    r = subprocess.run(
        [BASH, "-c", 'grep -qE "$1" "$2"', "_", ere, str(f)],
        capture_output=True,
    )
    return r.returncode == 0


@pytest.mark.parametrize("line,expected", M.FIXTURES)
def test_grep_predicate_matches_fixture(line, expected, tmp_path):
    assert _grep_says_exempt(M.MARKER_ANCHORED_ERE, line, tmp_path) is expected, (
        f"grep -qE disagrees with the shared corpus on {line!r}"
    )


def test_ere_uses_posix_class_not_backslash_t():
    r"""`[ \t]` inside a POSIX bracket expression matches a literal backslash
    and the letter t, not a tab. The canonical ERE must use [[:blank:]]."""
    assert "[[:blank:]]" in M.MARKER_ANCHORED_ERE
    assert "\\t" not in M.MARKER_ANCHORED_ERE


def test_python_pattern_is_derived_from_the_ere():
    assert M.MARKER_ANCHORED_RX.pattern == M._ere_to_python(M.MARKER_ANCHORED_ERE)


# --------------------------------------------------------------------------
# On real files, through the same entry points the consumers use
# --------------------------------------------------------------------------

def test_real_file_with_comment_marker_is_exempt(tmp_path):
    f = tmp_path / "carrier.py"
    f.write_text("# " + M.MARKER_TOKEN + " functional regex\nvalue = 1\n", encoding="utf-8")
    assert M.file_claims_exemption(f) is True


def test_real_file_with_prose_mention_is_not_exempt(tmp_path):
    f = tmp_path / "discusses.md"
    f.write_text(
        "# Notes\n\nOpt a file out with the " + M.MARKER_TOKEN + " marker.\n",
        encoding="utf-8",
    )
    assert M.file_claims_exemption(f) is False


def test_module_does_not_exempt_itself():
    """guard-6087: a check whose own text contains the searched token must not
    be falsified by its own installation. This module names the token many
    times, all inside string literals and prose."""
    assert M.file_claims_exemption(MODULE_PATH) is False


def test_unreadable_file_is_not_exempt(tmp_path):
    assert M.file_claims_exemption(tmp_path / "does-not-exist") is False


# --------------------------------------------------------------------------
# CLI, which is how the bash consumers reach the predicate
# --------------------------------------------------------------------------

def test_cli_print_ere_round_trips():
    r = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--print-ere"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == M.MARKER_ANCHORED_ERE


def test_cli_file_exit_codes(tmp_path):
    carrier = tmp_path / "a.sh"
    carrier.write_text("#!/bin/sh\n# " + M.MARKER_TOKEN + " why\n", encoding="utf-8")
    prose = tmp_path / "b.md"
    prose.write_text("the " + M.MARKER_TOKEN + " marker is explained here\n", encoding="utf-8")

    def rc(p):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), "--file", str(p)], capture_output=True
        ).returncode

    assert rc(carrier) == 0
    assert rc(prose) == 1
    assert rc(tmp_path / "missing") == 2


# --------------------------------------------------------------------------
# Single source of truth: no consumer may re-declare the predicate
# --------------------------------------------------------------------------

COMMENT_OPENERS = ("#", "//", "--", "<!--", "*")


def _code_lines(src: str) -> list[str]:
    """Drop whole-line comments.

    This audit is itself subject to the defect it audits (guard-6087): each of
    these files now carries a comment explaining WHAT unanchored shape was
    replaced, and that comment quotes the forbidden literal verbatim. Scanning
    raw text therefore reports every file it just fixed. Anchoring the audit the
    same way the predicate is anchored -- a line that opens a comment is prose,
    not code -- is the consistent answer; deleting the explanatory comments to
    satisfy a scanner would be the wrong one.
    """
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if any(stripped.startswith(op) for op in COMMENT_OPENERS):
            continue
        out.append(line)
    return out


def _discover_consumers() -> list[str]:
    """DERIVE the consumer list; never hand-maintain it.

    The defect this whole module fixes was a hand-copied predicate drifting
    across call sites, and the goal's own progress note got the consumer COUNT
    wrong twice (three, then six; the true figure measured on 2026-09-20 was
    seven). A hard-coded tuple here would reproduce exactly that: a NEW consumer
    that hand-rolls the unanchored test would simply not be in the list, and the
    audit would report clean forever -- guard-1802 / reclaim-routed-work.md rule
    7, an audit predicate narrower than the population it governs.

    So the population is computed. TWO shapes, and the second is the one a
    naive version misses: a file is a consumer if it names the TOKEN on a code
    line (the un-converged shape), OR if it references the SHARED PREDICATE
    (the converged shape). Keying only on the token was tried first and the
    positive control below caught it immediately -- it found the four python
    consumers and MISSED both bash ones, because a correctly-converged bash
    call site says `grep -qE "$MARKER_RX"` and no longer names the token
    anywhere except in its explanatory comment. A "who uses X" audit keyed on
    the literal X is blind to exactly the call sites that were fixed to stop
    naming X, which would have quietly dropped every converged consumer from
    the audit as soon as it was converged (sibling of guard-5611).
    """
    predicate_refs = (
        "_domain_leak_marker",   # python import of the shared module
        "MARKER_RX",             # bash: the ERE resolved once per run
        "--print-ere",           # bash: the CLI that resolves it
    )
    found = []
    for path in sorted(SCRIPTS.glob("*.py")) + sorted(SCRIPTS.glob("*.sh")):
        if path.name == MODULE_PATH.name:
            continue  # the source of truth itself
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = _code_lines(src)
        names_token = any(M.MARKER_TOKEN in line for line in code)
        uses_predicate = any(ref in line for line in code for ref in predicate_refs)
        if names_token or uses_predicate:
            found.append(path.name)
    return found


# The six known at fix time. Kept ONLY as a floor: if discovery returns fewer
# than these, discovery itself has broken (a glob typo, a renamed file), which
# would otherwise present as a clean audit over an empty population -- the
# guard-1715 shape where "nothing to check" and "everything checked" print the
# same. Never edit this to silence a failure; fix the file it names.
KNOWN_CONSUMERS = (
    "domain-leak-check.sh",
    "seed-path-self-reference-scan.sh",
    "marker-placement-gate.py",
    "domain-leak-commit-gate.py",
    "rule-vs-convention-gate.py",
    "_seed_transforms.py",
)

CONSUMERS = tuple(sorted(set(_discover_consumers()) | set(KNOWN_CONSUMERS)))


def test_discovery_finds_every_known_consumer():
    """Positive control for the derivation above."""
    discovered = set(_discover_consumers())
    missing = set(KNOWN_CONSUMERS) - discovered
    assert not missing, (
        f"consumer discovery missed {sorted(missing)} — the glob or the "
        "code-line filter is broken, and the audit below would then pass over "
        "an under-counted population."
    )


@pytest.mark.parametrize("name", CONSUMERS)
def test_consumer_has_no_hand_copied_unanchored_test(name):
    """Outcome 1: one shared predicate, no hand-copied occurrence.

    The forbidden shapes are the literal unanchored tests the goal measured --
    `grep -q "<token>"` in bash and `"<token>" in <var>` in python. Prose and
    comments naming the token stay legal; these files DOCUMENT the marker and
    forbidding the word would be the same over-match the goal is about.
    """
    code = "\n".join(_code_lines((SCRIPTS / name).read_text(encoding="utf-8")))
    tok = M.MARKER_TOKEN
    forbidden = (
        'grep -q "' + tok + '"',
        "grep -q '" + tok + "'",
        '"' + tok + '" in content',
        '"' + tok + '" in txt',
        '"' + tok + '" in proposed',
    )
    for bad in forbidden:
        assert bad not in code, (
            f"{name} still carries a hand-copied unanchored marker test: {bad!r}. "
            "Import the predicate from _domain_leak_marker instead."
        )


def test_the_audit_itself_would_fail_on_a_real_regression(tmp_path):
    """Positive control for the audit above.

    `_code_lines` makes the audit ignore comments, which is exactly the move
    that could silently neuter it. Plant the forbidden shape on a CODE line and
    assert it survives the filter, so the audit is proven able to fail.
    """
    planted = 'if grep -q "' + M.MARKER_TOKEN + '" "$f"; then\n'
    kept = "\n".join(_code_lines("# a comment mentioning it\n" + planted))
    assert 'grep -q "' + M.MARKER_TOKEN + '"' in kept
    assert "a comment mentioning it" not in kept
