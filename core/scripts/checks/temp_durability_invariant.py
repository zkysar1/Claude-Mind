#!/usr/bin/env python3
"""verify-learning check: the SURVIVING temp/ durability invariant ().

A file directly under agents/*/temp/ that is NEITHER git-tracked NOR covered by
a temp-drain-purge.sh lane has no durability AND no lifecycle. Under
STORAGE_BACKEND=local the own-cloud S3 sweep that .gitignore names as the
durability mechanism does not run -- such a file has no copy anywhere.

WHAT "COVERED" MEANS DEPENDS ON THE LANE'S SHAPE, which is why predicate_shape()
detects it rather than assuming (g-306-111):
  * allow-list (pre-2026-07-31): covered == suffix is in the parsed ephemera list.
  * inverted (2026-07-31 .. 2026-10-05): Lane 1 purged by DEFAULT, so nearly
    everything was covered and the residual was the EXEMPT set -- a file cited by
    a durable record, hence never purged, un-drainable if third-class, and
    untracked because temp/ is gitignored. That is the artifact D2 says to
    promote into a receipted dir (temp-store.md § The third class (b)).
  * decided (current, 2026-10-05): Lane 1 deletes only what a review decided
    (temp_decisions.py), so every item has a lifecycle -- the review, scheduled
    by the precheck pressure count. The residual is the same cited set, now for
    EVERY suffix: the review refuses to discard a cited file, so it stays
    untracked with no durable copy until someone promotes it or folds it into
    the citing record. That is what this check reports.

SUPERSEDES g-001-210's proposed check, whose invariant ("git-ignored IFF purged")
is obsolete: .gitignore now ignores ALL of agents/*/temp/* by design (g-115-1765)
while the purge covers 8 extensions, so the biconditional is false in BOTH
directions and asserting it would fail permanently.

The lane's definition is PARSED from temp-drain-purge.sh, never hardcoded -- the
extension list grew 5 -> 8 without anything noticing, which is how g-001-210's
premise went stale. Parsing is necessary but NOT sufficient: g-306-111 kept the
same `-name '*.ext'` tokens while inverting the expression's polarity, so a
parser reading tokens without their surrounding SENSE would have flipped this
check's verdict silently. Read the shape, not just the tokens.

WARNS, never fails (always exits 0): a fresh working doc legitimately sits
untracked for minutes. Age threshold 24h. Says nothing about depth-2 drained/,
which has its own Lane-2 mtime GC -- a different lifecycle, not a violation.

ENVIRONMENT-GATED BY CONSTRUCTION (back-ported UP from ZDS 2026-07-31; authored
there as g-001-249). The original carried "ZDS-LOCAL BY DESIGN (KEEP-IN-SYNC)":
the invariant only holds where the backend is local, upstream runs own-cloud,
and a mirror-style sync would delete the file (rb-842). Instead of keeping a
deployment-local fork forever, the check now resolves the ACTIVE storage
backend the same way storage_backend.get_backend() does and SKIPs unless it is
"local" -- correct on every deployment, so it lives at the dev source and
promotion carries it instead of deleting it. The mirror is deliberate: when the
box would behave local (explicit pin, registry says local, or nothing set at
all -- the same ambient default get_backend() applies), git/purge really are
the only durability there, so the invariant applies exactly where the check runs.
"""
import fnmatch
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# CWD-INDEPENDENT BY CONSTRUCTION. The first version of this check used relative
# paths ("agents", "core/scripts/temp-drain-purge.sh"). Run from anywhere but
# PROJECT_ROOT it found nothing and printed a confident SKIP/PASS having scanned
# zero files — a silent-failure defect inside a check written to catch silent
# failures. Caught by fresh-eyes review (); the three original controls
# all ran from the repo root, so they passed under both "works" and
# "cwd-dependent" (rb-847: a check that passes under every hypothesis tests
# nothing). agents_root() is also the mandated accessor — CLAUDE.md forbids
# hardcoding the "agents" path segment (AGENTS_PARENT_DIR).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import PROJECT_ROOT, agents_root  # noqa: E402

AGE_SECONDS = 24 * 3600
PURGE = Path(PROJECT_ROOT) / "core" / "scripts" / "temp-drain-purge.sh"


def active_storage_backend() -> str:
    """Resolve the backend NAME exactly as storage_backend.get_backend() does,
    without instantiating a backend: run the SAME env self-heal it runs
    (_bootstrap_env_defaults — .env.local gap-fill + registry derivation,
    g-115-2297/g-115-3070; explicit env always wins; pytest-hermetic by its
    own guard), then read the var with the same default. This keeps one
    source of truth: the check runs exactly where the storage layer would
    actually behave local for this process."""
    try:
        from storage_backend import _bootstrap_env_defaults
        _bootstrap_env_defaults()
    except Exception:
        pass  # missing module/SDK: fall through to the ambient env read
    return os.environ.get("STORAGE_BACKEND", "local").strip().lower()


def predicate_shape():
    """('decided', None) | ('inverted', None) | ('allowlist', {exts}) | (None, None).

    g-306-111 inverted Lane 1 from an allow-list of ephemera extensions to
    purge-by-default-with-exemptions. That MOVED the meaning of every
    `-name '*.ext'` token in the assignment: under the allow-list they named
    what IS purged; under the inversion the only two left (`.md`, `.json`) name
    what is EXEMPT. Parsing them the old way inverts the check's verdict —
    it would have reported every `.jsonl`/`.yaml`/`.tsv` as lifecycle-less at
    the exact moment they gained a lifecycle, and silently credited `.md`/`.json`
    to the purge lane they are exempt from.

    So the shape is detected, not assumed. The docstring's own lesson (the list
    "grew 5 -> 8 without anything noticing") generalizes: a parser that reads
    tokens without reading the SENSE of the expression around them goes stale
    the first time the expression's polarity changes.
    """
    if not PURGE.is_file():
        return None, None
    m = re.search(r"PURGE_FIND_PRED=\((.*?)\)\s*$",
                  PURGE.read_text(encoding="utf-8"), re.M | re.S)
    if not m:
        return None, None
    body = m.group(1)
    # The decided lane's signature: the predicate is applied to each NAMED
    # candidate (`find <path> -maxdepth 0 ...`), never to a directory scan.
    if re.search(r"-maxdepth\s+0\b", body):
        return "decided", None
    # The inversion's signature: the .md/.json group is NEGATED.
    if re.search(r"!\s*\\\(\s*-name '\*\.md'", body):
        return "inverted", None
    exts = set(re.findall(r"-name '\*(\.[A-Za-z0-9]+)'", body))
    return ("allowlist", exts) if exts else (None, None)


def cited_basenames():
    """Basenames a durable record cites, via the purge lane's own source of
    truth. Returns None when the cited set is UNKNOWN — the caller must not
    read that as 'nothing is cited' (same fail-loud contract the purge lane
    relies on)."""
    script = Path(PROJECT_ROOT) / "core" / "scripts" / "temp-citation-ratchet.py"
    if not script.is_file():
        return None
    try:
        r = subprocess.run([sys.executable, str(script), "--cited-paths"],
                           capture_output=True, text=True, timeout=120,
                           cwd=str(PROJECT_ROOT))
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return {p.rstrip("/").rsplit("/", 1)[-1] for p in r.stdout.split("\n") if p.strip()}


def main():
    backend = active_storage_backend()
    if backend not in ("", "local", "local-files"):
        print("SKIP: temp-durability invariant applies only under a local storage "
              "backend (this box resolves %r; the own-cloud S3 sweep provides "
              "temp/ durability there)" % backend)
        return 0

    shape, exts = predicate_shape()
    if shape is None:
        print("SKIP: could not parse PURGE_FIND_PRED from temp-drain-purge.sh")
        return 0
    cited = None
    if shape in ("inverted", "decided"):
        cited = cited_basenames()
        if cited is None:
            print("SKIP: the cited set is UNKNOWN — the residual (cited files) is not "
                  "computable, and reporting 0 orphans here would be a vacuous PASS "
                  "(the purge lane itself deletes nothing in this case)")
            return 0
    try:
        tracked = set(subprocess.run(["git", "ls-files"], capture_output=True,
                                     text=True, timeout=30,
                                     cwd=str(PROJECT_ROOT)).stdout.split("\n"))
    except Exception as exc:
        print("SKIP: git ls-files unavailable (%s)" % type(exc).__name__)
        return 0

    now, orphans = time.time(), []
    for p in Path(agents_root()).glob("*/temp/*"):
        if not p.is_file() or p.name.startswith("."):
            continue
        if (now - p.stat().st_mtime) < AGE_SECONDS:
            continue                                   # fresh working doc: legitimate
        if str(p.relative_to(PROJECT_ROOT)).replace("\\", "/") in tracked:
            continue                                   # durable via git
        if shape == "allowlist":
            if p.suffix in exts:
                continue                               # has a lifecycle via purge
        elif shape == "decided":
            # Every item has a lifecycle (the review); only a cited file is left
            # with no way out but a keep. Same glob rule as below.
            if not any(fnmatch.fnmatch(p.name, c) for c in cited):
                continue
        else:
            # Purge-by-default: covered unless exempt. The two exemptions have
            # DIFFERENT standing, and only one of them leaves a real residual:
            #   .md/.json  -> the DRAIN lane owns them (encode, then drained/,
            #                 then Lane 2's mtime GC). A lifecycle, not a gap.
            #   cited      -> exempt from purge BY DESIGN, un-drainable if it is
            #                 third-class, and untracked because temp/ is
            #                 gitignored. That is genuinely no durability and no
            #                 lifecycle — and it is exactly the artifact D2 says
            #                 to promote by wrapping in a receipted dir
            #                 (temp-store.md § The third class (b)).
            if p.suffix in (".md", ".json"):
                continue
            # GLOB, not set-membership. 4 of 64 live cited paths are wildcards
            # ("…/temp/-*") because durable records cite a FAMILY, and
            # the purge lane honors them as globs. An exact-membership test here
            # would silently under-report every wildcard-exempt artifact —
            # measured: it reported 0 unpromoted while a 31h-old, untracked,
            # purge-exempt file sat in temp/. Under-reporting is the dangerous
            # direction for a check whose whole job is to notice what nothing
            # else retains, so the predicate must match the lane's exactly.
            if not any(fnmatch.fnmatch(p.name, c) for c in cited):
                continue                               # purge lane covers it
        orphans.append(str(p))

    if orphans:
        if shape == "decided":
            print("WARN: %d temp file(s) >24h are cited by a durable record, so the "
                  "review cannot discard them, but they are not git-tracked — no "
                  "durable copy, and a keep is their only lifecycle. Promote each into "
                  "a receipted dir (agents/<agent>/temp/<slug>/ + RECEIPT.*), or fold "
                  "the content into the citing record: %s"
                  % (len(orphans), ", ".join(orphans[:5])))
        elif shape == "inverted":
            print("WARN: %d temp file(s) >24h are cited by a durable record, so the "
                  "purge lane exempts them, but they are neither git-tracked nor "
                  "drainable — no durability, no lifecycle. Promote each into a "
                  "receipted dir (agents/<agent>/temp/<slug>/ + RECEIPT.*), or fold "
                  "the content into the citing record: %s"
                  % (len(orphans), ", ".join(orphans[:5])))
        else:
            print("WARN: %d temp file(s) >24h are NEITHER git-tracked NOR purge-covered "
                  "(no durability, no lifecycle): %s" % (len(orphans), ", ".join(orphans[:5])))
        if len(orphans) > 5:
            print("      ... +%d more" % (len(orphans) - 5))
    elif shape == "decided":
        print("PASS: temp-durability invariant holds (Lane 1 deletes only reviewed "
              "discards; %d cited exemption(s) checked, 0 unpromoted)" % len(cited))
    elif shape == "inverted":
        print("PASS: temp-durability invariant holds (Lane 1 purge-by-default; "
              "%d cited exemption(s) checked, 0 unpromoted)" % len(cited))
    else:
        print("PASS: temp-durability invariant holds (%d ext(s) parsed, 0 orphans)" % len(exts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
