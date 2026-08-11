#!/usr/bin/env python3
"""Print the forged-skill registry as a compact index — the Phase 4 reader.

WHY THIS EXISTS (g-115-3811, sig-48). `world/forged-skills.yaml` is the registry
of already-forged capabilities, and `.claude/rules/forged-skill-resolution.md`
tells the agent to consult it before hand-rolling a procedure. `guard-1516`
states the failure verbatim: "Nothing in Phase 4 surfaces the forged registry
automatically - retrieve.py does not index it and aspirations-execute never
reads it - so this check is entirely manual." Measured twice, ~5 weeks apart,
different agents, same double-miss shape (g-335-45 missed gap-019 + gap-017;
g-335-448 missed gap-011 + gap-019).

That is pattern signature sig-48 exactly — a correct, retrievable rule that
nothing READS at the moment of the action — and its prescription is to build a
reader at the action point rather than restate the rule. sig-48 is 7/7
CONFIRMED, and its strongest recorded evidence (g-335-409) is that the working
shape is an UNCONDITIONAL step, because then nothing has to remember.

NO MATCHER (g-115-4475, retiring the one this file used to carry). Earlier
versions filtered the registry per goal, on the theory that an advisory firing
on most goals is one nobody reads. g-115-4446 scored five candidate matchers on
a 30-goal hand-labelled sample (24 ground-truth goal->skill pairs) on BOTH
precision and recall per guard-2224, and the frontier has no usable point.
Re-run independently on a second box 2026-08-01 (echo, g-115-4475), same
numbers:

    matcher                   TP   FP   FN   prec    rec     F1   fire%
    v2-phrase (what shipped)   0    4   24   0.00   0.00   0.00     10%
    C1 all-content-tokens      3   37   21   0.07   0.12   0.09     63%
    C2 >=2-content-tokens     19  256    5   0.07   0.79   0.13     97%
    C3 phrase OR >=75%         6   46   18   0.12   0.25   0.16     77%
    C4 trigger+gap-join        0    4   24   0.00   0.00   0.00     10%

The shipped matcher's recall was ZERO — all four of its fires were false
positives, so it surfaced nothing correct on any goal in the sample. Precision
never exceeded 0.12 on any candidate, so tuning a threshold moves ALONG a bad
frontier rather than off it. The measured reason is a vocabulary mismatch: goal
text is PROBLEM vocabulary written by the filer, triggers are PROCEDURE
vocabulary written by the forger, and their overlap is close to orthogonal to
relevance.

So this file no longer decides. It prints all of them, always. Recall is 1.00 by
construction and there is no threshold left to drift. The precision objection
that motivated the matcher applies to an ALERT claiming relevance, not to a MENU
claiming only existence — different artifacts, different reading contracts.

MEASURED COST (2026-08-01, 42 skills): 2,426 characters of index body, 2,800
including the header and footer lines; ~57ms wall clock for the registry read
plus 42 front-matter reads. The character counts are the hard numbers; a token
count is NOT asserted here because no tokenizer was available on the measuring
box — chars/4 puts it near 700, and identifier-heavy text tokenizes worse than
that, so treat that as a floor rather than a figure.

REUSE, NOT REBUILD. The registry loader is `gates/capability.py`'s, which
already reads this same file for the capability gate. Descriptions come from
each skill's own SKILL.md front matter via `_skill_md.parse_front_matter` —
which handles YAML block scalars, where a naive `^description:` regex captures
the `>-` marker instead of the folded text (2 of 42 skills, measured). Coverage
is 42/42; a skill whose front-matter `name` diverges from its registry key is
caught separately by the /verify-learning forged-skill-transport check.

ADVISORY ONLY: always exits 0. This must never block goal execution.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _paths import PROJECT_ROOT, WORLD_DIR  # noqa: E402

try:
    from gates.capability import _load_forged_skills  # noqa: E402
except Exception:  # pragma: no cover - import guard, advisory must not break
    _load_forged_skills = None

try:
    from _skill_md import parse_front_matter  # noqa: E402
except Exception:  # pragma: no cover - import guard, advisory must not break
    parse_front_matter = None

# Characters of description kept per row. Chosen against the measured render in
# the docstring: the 42 skill NAMES alone are ~950 characters, so this is the
# knob that sets the whole index's size. Widening it is the supported way to
# trade tokens for detail; there is no per-goal filtering to tune instead.
DESC_WIDTH = 34


def _one_line(text, width: int = DESC_WIDTH) -> str:
    """Collapse whitespace and clip to `width` on a word boundary."""
    s = " ".join(str(text or "").split())
    if len(s) <= width:
        return s
    cut = s[:width]
    sp = cut.rfind(" ")
    # Only honour the word boundary when it does not throw away most of the
    # budget — a single long token would otherwise clip to almost nothing.
    if sp > width * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:.-—")


def _description(skill_name: str) -> str:
    """One-line description from the skill's own SKILL.md front matter."""
    if parse_front_matter is None:
        return ""
    try:
        fm = parse_front_matter(
            PROJECT_ROOT / ".claude" / "skills" / str(skill_name) / "SKILL.md")
    except Exception:
        return ""
    return str((fm or {}).get("description") or "")


def build_index(wdir) -> list:
    """Return [{skill, description, scripts}] for every registered skill.

    Sorted by name: this is a menu, so a stable alphabetical order is what makes
    it scannable. Never raises — an unreadable registry yields an empty index.
    """
    if _load_forged_skills is None:
        return []
    try:
        entries = _load_forged_skills(wdir)
    except Exception:
        return []

    out = []
    for entry in entries:
        name = entry.get("skill")
        if not name:
            continue
        out.append({
            "skill": str(name),
            "description": _description(name),
            "scripts": entry.get("scripts") or [],
        })
    out.sort(key=lambda e: e["skill"])
    return out


def render(index: list, width: int = DESC_WIDTH) -> str:
    """The printed menu body — one row per skill, no filtering."""
    return "\n".join(
        "  /%s — %s" % (e["skill"], _one_line(e["description"], width))
        for e in index
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON")
    # parse_known_args, not parse_args: callers holding the pre-
    # pseudocode still pass --goal/--source. Those are now meaningless, but
    # erroring on them would break this script's one hard invariant — it must
    # never block goal execution — for every in-flight session on the fleet.
    args, _ignored = ap.parse_known_args(argv)

    index = build_index(WORLD_DIR)

    if args.json:
        print(json.dumps({"skills": index, "count": len(index)}, indent=2))
        return 0

    if not index:
        # Empty registry or an unreadable one. Say so on stderr rather than
        # printing nothing, which would be indistinguishable from a world that
        # genuinely has no forged skills.
        print("[forged-skill-surface] registry empty or unreadable "
              "(world/forged-skills.yaml)", file=sys.stderr)
        return 0

    print("[forged-skill-surface] ▸ %d FORGED SKILLS ALREADY EXIST — "
          "check here before hand-rolling a procedure:" % len(index))
    print(render(index))
    print("[forged-skill-surface] Registry: world/forged-skills.yaml — "
          ".claude/rules/forged-skill-resolution.md rule 2: check the registry, "
          "do not reason about whether a skill 'should' exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
