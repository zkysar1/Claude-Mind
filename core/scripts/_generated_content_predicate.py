"""Single source of truth: "does this commit carry vendored/generated content?"

Imported by:
  - generated-content-commit-audit.py  (Layer C — observe the committed corpus)

There is deliberately no Layer-A gate today. When one is added it MUST import
from here rather than re-implementing the path test: the whole point of the
split is that the enforcing layer and the observing layer cannot disagree about
what "generated" means. (Same contract as `_gradle_tests_predicate.py` and
`_swakeup_predicate.py`.)

THE MECHANISM (guard-793):
`git add -A` in a shared working tree stages whatever happens to be untracked,
not whatever the current goal touched. When a build artifact directory is
untracked and un-ignored, one careless stage vendors it wholesale. The commit
SUBJECT still describes the intended one-line change, so nothing downstream
reads as wrong — the diff is simply never looked at.

Measured 2026-08-10 (g-115-3664): a commit whose subject described a one-line
regex anchor change contained 547 files, 545 of them a vendored virtualenv. It
reached the default branch and sat there 11 days, unnoticed.

WHY TIERED, AND WHY `build`/`dist`/`env` ARE NOT IN THE DEFAULT SET:
A detective that fires on every repo's legitimate `build/` output directory is
one whose readers learn to skip it, and a skipped detective is worth exactly as
much as the absent one it replaced. The HIGH set contains only path segments
that are never hand-authored source. The AMBIGUOUS set is real signal but needs
a human read, so it is opt-in behind a flag and reported separately — never
merged into the default verdict.

WHY THE `--diff-filter=A` SPLIT MATTERS:
A commit that DELETES a vendored directory carries generated paths too, and it
is the good outcome, not the defect. Counting ADDED generated paths separately
is what keeps a cleanup commit from being reported as the thing it cleaned up.
"""

import re

# Path segments that are never hand-authored source. A match here is the
# default flag condition.
HIGH_CONFIDENCE_SEGMENTS = (
    ".venv",
    "venv",
    "virtualenv",
    "site-packages",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "bower_components",
)

# Real signal, but each of these is a legitimate hand-authored directory name in
# some project layout. Opt-in via --include-ambiguous; reported separately.
#
# `vendor` SITS HERE DELIBERATELY, and the first live run is why (2026-08-11,
# cc-03). It started in the HIGH set and flagged 8c18bf7 — a commit that vendors
# a third-party agent repo ON PURPOSE, 46 vendored paths alongside 68 authored
# ones. The line that separates the two sets is not "is it third-party" but
# "did anyone CHOOSE to commit it": `.venv` / `node_modules` / `__pycache__` are
# tool output that no one elects to stage, whereas a `vendor/` tree is a
# reviewed decision. guard-793 is about content the author did not intend, so a
# deliberate vendoring is outside it by definition.
AMBIGUOUS_SEGMENTS = (
    "vendor",
    "build",
    "dist",
    "env",
    "out",
    "target",
    "coverage",
)

# Suffixes that are generated regardless of where they sit in the tree.
GENERATED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".so",
    ".dylib",
)

# `*.egg-info` is a directory whose name is only known by suffix.
_SUFFIX_DIR_RE = re.compile(r"(^|/)[^/]+\.egg-info(/|$)")


def _segments(path):
    """Split a git path into its directory/file segments.

    git always reports forward slashes, on every platform — so a backslash here
    is a literal filename character, not a separator, and must NOT be split on.
    """
    return [s for s in path.split("/") if s]


def classify_path(path, include_ambiguous=False):
    """Return the matched marker for `path`, or None.

    The return value is the specific marker that matched, not a bare bool: a
    caller reporting *why* a commit was flagged is far more useful than one
    asserting *that* it was, and it lets a reader falsify the call cheaply.
    """
    if not path:
        return None

    segs = _segments(path)

    for seg in segs:
        if seg in HIGH_CONFIDENCE_SEGMENTS:
            return seg

    for suffix in GENERATED_SUFFIXES:
        if path.endswith(suffix):
            return "*" + suffix

    if _SUFFIX_DIR_RE.search(path):
        return "*.egg-info"

    if include_ambiguous:
        for seg in segs:
            if seg in AMBIGUOUS_SEGMENTS:
                return seg

    return None


def classify_paths(paths, include_ambiguous=False):
    """Classify an iterable of paths.

    Returns (generated, markers) where `generated` is the list of matching
    paths and `markers` is an ordered count per matched marker.
    """
    generated = []
    markers = {}
    for p in paths or ():
        marker = classify_path(p, include_ambiguous=include_ambiguous)
        if marker:
            generated.append(p)
            markers[marker] = markers.get(marker, 0) + 1
    return generated, markers


def evaluate_commit(all_paths, added_paths, include_ambiguous=False, min_added=1):
    """Decide whether one commit is a guard-793 violation candidate.

    `all_paths`   — every path the commit touches
    `added_paths` — the subset the commit ADDS (git --diff-filter=A)

    The verdict keys on ADDED generated paths. A commit that only deletes or
    modifies generated paths is a cleanup, and reporting it as a violation
    would invert the signal.
    """
    all_paths = list(all_paths or ())
    added_paths = list(added_paths or ())

    gen_all, markers_all = classify_paths(all_paths, include_ambiguous)
    gen_added, markers_added = classify_paths(added_paths, include_ambiguous)

    total = len(all_paths)
    n_added = len(gen_added)
    flagged = n_added >= min_added

    return {
        "flagged": flagged,
        "total_files": total,
        "generated_total": len(gen_all),
        "generated_added": n_added,
        "non_generated_files": total - len(gen_all),
        "markers": markers_added or markers_all,
        # Bounded — a 545-file sample is not evidence, it is a wall of text.
        "sample": gen_added[:5] if gen_added else gen_all[:5],
        "cleanup_only": bool(gen_all) and not gen_added,
    }
