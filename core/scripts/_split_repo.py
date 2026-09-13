#!/usr/bin/env python3
"""Split-Repo Registry: the ONE definition of which repos merge to `dev`.

g-115-9675. Lifted out of `completed-not-committed-sweep.py` (g-115-9040) so the
sweep and `gates/uncommitted_work.py` share one registry parse instead of two.

WHY THIS MODULE EXISTS. The same defect was found in two consumers and only one
was fixed: work correctly merged to the `dev` branch on a registry-listed repo
reads as STRANDED when containment is asked against the default branch. On
those repos the charter states verbatim "Nothing merges to `main` directly" —
feature PRs merge to `dev`, and dev->main travels only on the weekly promotion
PR (asp-370). So containment against the default branch asks a question whose
answer is "no" for up to a week BY DESIGN, and the prescribed remedy ("merge
it") is a guard-5389 charter violation into an auto-deploying PROD target.

PROVENANCE OF THE TWO MEASUREMENTS BELOW, kept separate because they were made
by different sessions and only one of them was re-checked here.
  INHERITED, not re-measured by the session that wrote this module: on
  2026-09-05 the Vinheim-Web-App checkout had its dev branch 64 commits ahead
  of and 0 behind the default branch, and four goals were flagged whose commits
  had all already merged to dev. Recorded on the g-115-9675 goal record from the
  g-115-9040 lineage (guard-6045); treat it as dated, not as current state.
  MEASURED HERE (g-115-9675, alpha, 2026-09-12) on the same checkout: asking
  containment against the default branch reported 181 commits off-ref, against
  the landing ref 121, over an 839-commit reachable control — so 60 correctly
  merged commits stop reading as stranded debt. A second registry repo
  (ayoai-lambda-common) was unchanged by the switch, which is the negative
  control for "the landing ref only moves repos that actually target dev".

WHAT IS SHARED HERE, AND WHAT IS DELIBERATELY NOT. Shared: the registry PARSE —
which repos are split — because that is the part carrying real subtlety (the
table IS the test, and the fail-closed direction is load-bearing). NOT shared:
the ref SPELLING. The two consumers use different ref shapes on purpose —
`completed-not-committed-sweep.py` works in short refs (`origin/dev`,
`origin/main`) while `gates/uncommitted_work.py` works in full refs
(`refs/remotes/origin/dev`, `refs/remotes/origin/master`), and its `default_ref`
flows on into five further sites (a `git diff` at :494, a `rev-list` at :500, an
emitted `default_ref` field at :509 and an operator message at :638). Folding
both spellings into one function would mean injecting a resolver callback to
serve a rule that is two lines in each caller — the single-use abstraction
`implementation-discipline.md` rule 3 forbids. Each consumer spells its own ref
and asks THIS module the only question that was ever duplicated.

FAIL-CLOSED IS THE INVARIANT, AND IT MUST SURVIVE EVERY EDIT HERE. A missing
world path, an unreadable file, an absent section or an unparseable table all
return the EMPTY set, which makes every caller fall through to its default-branch
behaviour — i.e. exactly what it did before this module existed. A registry we
cannot read must NEVER silently widen what counts as landed: widening turns a
close gate into a rubber stamp, and that failure is silent in the dangerous
direction.
"""
from __future__ import annotations

import os
import re
from typing import FrozenSet, Optional

REGISTRY_RELPATH = ("conventions", "sdlc-environments.md")
REGISTRY_HEADING = "## Split-Repo Registry"
# ANY markdown heading terminates the table ( fresh-eyes F-1). A
# "## "-only test left every table under a "###" subsection in scope; the live
# file was correct only because no such table had "YES" in column 2.
_HEADING_RE = re.compile(r"^#{1,6}\s")


def split_repo_names(world_path: Optional[str] = None) -> FrozenSet[str]:
    """Repo names the Split-Repo Registry marks as split. Impure (reads the
    domain convention).

    The registry table IS the test for whether a repo is split — "named
    somewhere in the convention" is explicitly NOT the test (g-370-15, which
    exists because the rule was stated in two places and found in neither). So
    this parses the one `## Split-Repo Registry` table and nothing else.

    Fails closed: every unreadable or unparseable input yields frozenset().
    """
    world = world_path or os.environ.get("WORLD_PATH")
    if not world:
        return frozenset()
    path = os.path.join(str(world), *REGISTRY_RELPATH)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return frozenset()
    names, in_section = set(), False
    for line in lines:
        if _HEADING_RE.match(line):
            if in_section:
                break  # the next heading ends the table
            in_section = line.startswith(REGISTRY_HEADING)
            continue
        if not in_section or not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 2 or cols[1].strip().strip("*").upper() != "YES":
            continue
        name = cols[0].strip().strip("`").strip()
        if name and not name.startswith("("):  # skip the placeholder row
            names.add(name)
    return frozenset(names)


def is_split_repo(repo, split_names: Optional[FrozenSet[str]] = None) -> bool:
    """True when ``repo``'s basename is registry-listed as split.

    ``split_names`` is INJECTED so the split decision stays testable without a
    world on disk; when omitted the registry is read via ``split_repo_names()``.
    Passing an explicitly empty frozenset means "nothing is split" and is
    honoured as such — only ``None`` triggers the read.
    """
    if split_names is None:
        split_names = split_repo_names()
    if not split_names:
        return False
    name = os.path.basename(os.path.normpath(str(repo)))
    return name in split_names
