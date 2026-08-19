"""Framework must not BRANCH on an aspiration field that nothing writes — .

This is the rb-245 class ("audit/branch against a nonexistent field") with the
FRAMEWORK as the offender rather than an audit script. Four consumers read
`asp.last_worked`, which has zero writers anywhere in core/scripts or mind_api
and is absent from every live record. Each read therefore evaluated to None
forever, and each consumer silently degraded to a constant:

  goal-selector.py cooldown filter   -> never skipped anything
  evolve 2.75d staleness demotion    -> `is null OR ...` demoted EVERY HIGH
                                        aspiration (g-029-82, 9 of 9), later
                                        guarded to `is NOT null AND ...`, which
                                        made it never fire at all
  evolve cap enforcement             -> always took the never-started RETIRE
                                        branch, so an aspiration WITH progress
                                        would be retired instead of completed
  create-aspiration cap check        -> the `and last_worked is null` half was
                                        always true, reducing the test to
                                        "no completed goals"

The field looked real because all three bootstrap seed templates carry
`"last_worked": null` — seeded, never stamped. A reader greps, finds it in the
schema, and reasonably concludes it exists.

WHY THE OBVIOUS FIX IS A TRAP, and why test_cooldown_uses_a_parser_that_can_parse_the_field
is the load-bearing test here rather than a nicety: the natural repair is
`days_since(asp.get("last_selected"))` — same shape, one identifier changed.
That is STILL permanently dead, because last_selected is written as
`datetime.now().isoformat(timespec="seconds")` and days_since() calls
date.fromisoformat(), which raises on any string carrying a time component.
days_since swallows the raise and returns None. The fix would review clean, pass
every token-matching assertion, and change nothing — reproducing the exact defect
one level up.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SELECTOR = REPO / "core" / "scripts" / "goal-selector.py"

# Runtime-STAMPED aspiration fields: written by the framework during normal
# operation and branched on elsewhere. Deliberately NOT every aspiration key --
# `cooldown_days`, `tags`, `motivation` etc. are authored at creation time and
# correctly have no runtime writer, so demanding one would make this test noisy
# and it would be silenced rather than fixed.
RUNTIME_STAMPED_FIELDS = ["last_selected", "selection_count"]

WRITER_FILES = [
    REPO / "core" / "scripts" / "aspirations.py",
    REPO / "mind_api" / "src" / "endpoints" / "aspirations_write.py",
]

# Framework surfaces that BRANCH on aspiration fields.
CONSUMER_GLOBS = [
    (REPO / "core" / "scripts", "*.py"),
    (REPO / "mind_api" / "src", "**/*.py"),
    (REPO / ".claude" / "skills", "*/SKILL.md"),
]


def _load_selector():
    """Import the hyphenated selector module by path."""
    sys.path.insert(0, str(REPO / "core" / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("_gs_under_test", SELECTOR)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


def _consumer_files():
    for root, pat in CONSUMER_GLOBS:
        if not root.exists():
            continue
        for p in root.glob(pat):
            # This test NAMES the dead field many times; excluding it keeps the
            # test from being its own only hit.
            if p.resolve() == Path(__file__).resolve():
                continue
            yield p


def test_last_worked_has_no_readers_left():
    """The regression pin: nothing may branch on the never-written field again."""
    offenders = []
    for p in _consumer_files():
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(src.splitlines(), 1):
            if "last_worked" not in line:
                continue
            # A line that only EXPLAINS the defect is fine; a line that reads the
            # field is not. Comment/prose markers are how the two are told apart.
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("—") or stripped.startswith("-"):
                continue
            if re.search(r"""(get\(\s*["']last_worked["']|\[["']last_worked["']\]|\.last_worked\b)""", line):
                offenders.append(f"{p.relative_to(REPO)}:{i}: {stripped[:100]}")
    assert not offenders, (
        "framework branches on `last_worked`, which has ZERO writers and is absent "
        "from every live aspiration record — the read is permanently None and the "
        "consumer silently degrades to a constant (g-115-3097). Use `last_selected`:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("field", RUNTIME_STAMPED_FIELDS)
def test_runtime_stamped_field_has_a_writer(field):
    """Every field the framework branches on for recency/state must be stamped.

    Generalises the specific pin above: this is what catches the NEXT dead field,
    not just the one that motivated the goal.
    """
    hits = []
    for wf in WRITER_FILES:
        if not wf.exists():
            continue
        src = wf.read_text(encoding="utf-8", errors="replace")
        if re.search(r"""\[["']%s["']\]\s*=""" % re.escape(field), src):
            hits.append(wf.name)
    assert hits, (
        f"aspiration field `{field}` is branched on by framework code but no writer "
        f"assigns it in {[w.name for w in WRITER_FILES]} — a read of it is permanently "
        f"None and every consumer silently degrades to a constant (rb-245 class)"
    )


def test_cooldown_uses_a_parser_that_can_parse_the_field():
    """days_since() CANNOT parse last_selected. This is the trap, pinned behaviourally.

    Not a style assertion: last_selected carries a time component, and
    date.fromisoformat() raises on that, which days_since swallows into None. A
    repoint to days_since(last_selected) is therefore still-dead code that looks
    fixed. Asserted against the real functions so a future refactor of either
    parser re-runs the discrimination rather than inheriting this claim.
    """
    gs = _load_selector()
    live_shape = "2026-08-10T05:51:36"  # exactly what aspirations.py stamps

    assert gs.days_since(live_shape) is None, (
        "days_since now parses a datetime-shaped stamp. If that is deliberate, the "
        "cooldown site may use either parser — but re-verify before relaxing this."
    )
    assert gs.days_since("2026-08-01") is not None, "date-only must still parse"

    hs = gs.hours_since(live_shape)
    assert hs is not None, "hours_since must parse the datetime-shaped stamp"
    assert hs >= 0

    # And the call site must actually use the parser that works.
    src = SELECTOR.read_text(encoding="utf-8")
    m = re.search(
        r"cooldown\s*=\s*asp\.get\(\s*[\"']cooldown_days[\"']\s*,\s*0\s*\)(.{0,400})",
        src,
        re.S,
    )
    assert m, "cooldown filter not found in goal-selector.py"
    block = m.group(1)
    assert "hours_since(asp.get(\"last_selected\"))" in block, (
        "cooldown filter must read last_selected via hours_since — days_since "
        "returns None on the stamped format, leaving the branch permanently dead"
    )
    assert "cooldown * 24" in block, (
        "cooldown_days is a DAY count; comparing it against hours without the *24 "
        "makes the cooldown 24x too short"
    )


def test_cooldown_branch_actually_skips_when_forced():
    """Force the antecedent (guard-2982).

    No live record sets cooldown_days > 0, so every real run satisfies this
    branch vacuously and proves nothing. Reproduce the site's own predicate
    against fixtures on both sides of the boundary.
    """
    gs = _load_selector()

    def skips(cooldown_days, last_selected):
        cooldown = cooldown_days or 0
        if cooldown > 0:
            hs = gs.hours_since(last_selected)
            if hs is not None and hs < cooldown * 24:
                return True
        return False

    from datetime import datetime, timedelta
    fresh = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")

    assert skips(7, fresh) is True, "inside the cooldown window -> must skip"
    assert skips(7, old) is False, "outside the cooldown window -> must not skip"
    assert skips(0, fresh) is False, "cooldown disabled -> never skip"
    assert skips(7, None) is False, "null recency -> never skip (fail-open)"
    # The regression this whole goal is about: the pre-fix pairing.
    assert gs.days_since(fresh) is None, (
        "days_since(fresh datetime stamp) must be None — this is why the naive "
        "repoint would have stayed dead"
    )
