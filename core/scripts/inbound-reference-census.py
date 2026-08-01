#!/usr/bin/env python3
"""inbound-reference-census.py — read-only inbound-reference census for
NON-TREE artifacts (g-306-99 / D1).

WHY THIS EXISTS
---------------
`core/scripts/tree.py::_iter_body_md_refs` is the shared iterator behind both
the dangling-ref validator (g-115-1419) and the post-reparent repair tool
(g-115-1830). Its own docstring declares TWO scope filters:

  1. `.md`-only  — the regex matches only backtick-quoted `*.md`
  2. tree-paths-only — non-tree refs (`core/`, `.claude/`, `world/conventions/`,
     `agents/*/temp/`, ...) are "out of scope to keep false positives low"

So a reference to `agents/<agent>/temp/x.json` is invisible to BOTH tools. This
census is the same shape with both filters removed, widened to the stores where
those references actually accumulate. Read-only, no behavior change; D2-D5 of
asp-306 consume it.

THE LOAD-BEARING CORRECTION: DANGLING-NESS IS BOX-DEPENDENT
-----------------------------------------------------------
`agents/<other>/temp/x` lives on THAT agent's box. Every other box reads it as
absent whether or not it was ever purged, so `Path.exists()` is authoritative
ONLY for artifacts this box owns. A census that reports one `dangling` count
per machine is not measuring the fleet — it is measuring which agent ran it.

This is not hypothetical and it is not a niche edge:

  - The filing goal (g-306-99) cites "33 of 62 concrete agents/*/temp/
    references DANGLE (53%)" measured on one deployment. That figure carries
    this flaw — it necessarily counted every other agent's temp path as
    dangling.
  - The same error was made and corrected in this deployment 2026-07-31
    during g-306-101: an initial "49 of 49 dangle" was really 15 measurable
    (all 15 dangling) plus 34 unmeasurable from that box.
  - `core/scripts/temp-citation-ratchet.py` reached the same conclusion
    independently and is why it counts CITATIONS rather than DANGLING
    citations: "a dangling-count baseline would report a different number on
    every machine."

Therefore this tool reports THREE states, never two:

  live         — the referent resolves here, and this box can vouch for it
  dangling     — the referent does NOT resolve AND this box owns it
  unmeasurable — the referent belongs to another agent's box; absence here is
                 not evidence of anything

`unmeasurable` is deliberately NOT folded into either other bucket. Folding it
into `dangling` reproduces the flaw above; folding it into `live` hides real
breakage. It is a third answer because the honest answer is "I cannot tell from
here" (rb-245: a zero/absence needs its schema checked before it is believed).

Exit codes: 0 always (advisory, read-only). Use --exit-on-dangling to make a
non-zero exit on measurable dangling refs for CI/recurring-goal wiring.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, agents_root, resolve_file_path  # noqa: E402

# Artifact classes this census covers. Deliberately a SUPERSET of the two
# filters _iter_body_md_refs applies: any extension, any non-tree path.
#
# The character class excludes backslash and quote characters deliberately.
# JSONL store lines carry JSON-ESCAPED prose, so a permissive class swallows
# the `\n` of an escaped newline and yields refs like
# `core/config/aspirations.yaml\nhealth_regression.mode`. Those are not
# citations, they are two lines of prose glued together by the escape — and
# they classify as `dangling` because no such file exists, which is how a
# census turns into a noise generator. The real fix is decoding the JSONL
# (see `_iter_store_text`); this class is the belt to that suspenders.
# WHY `meta/` CARRIES A BOUNDARY GUARD AND ITS FOUR SIBLINGS DO NOT ()
#
# `meta` is a short, common directory NAME as well as one of this deployment's
# two external configured roots, so a bare `meta/` alternative matches the tail
# of unrelated real paths. Measured 2026-07-31 (bravo, cc-05) across the full
# census corpus: bare matched 48 refs, guarded 45, and all 3 of the difference
# were fabrications, confirmed by reading the source text —
#
#   `mind_api/src/meta/meta_yaml.py`  -> emitted `meta/meta_yaml.py`
#   `mind_api/src/meta/meta_impk.py`  -> emitted `meta/meta_impk.py`
#   `cognitive-horizons/meta/memory-pipeline/gates.yaml`
#                                     -> emitted `meta/memory-pipeline/gates.yaml`
#
# Those are paths under `mind_api`, not under META_DIR. Left bare, each would
# resolve through `resolve_file_path` to `$META_PATH/<name>`, find nothing, and
# be reported as a DANGLING reference into the meta root — a referent that was
# never cited by anyone. That is precisely the resolver-artifact class the
#  fresh-eyes pass removed (~70 of 132 dangling refs were resolver
# fabrications), reintroduced through a different door. No legitimate ref is
# lost to the guard: the 45 survivors are the whole measured population.
#
# WHAT THE GUARD CANNOT DO, MEASURED RATHER THAN ASSUMED. It keys on the
# character before `meta/`, so it only fires when the FULL path was written.
# Prose that cites the same files in shorthand — a bulleted site list reading
# `\n  meta/meta_backpressure.py`, relative to an earlier `mind_api/src/` — has
# nothing to look behind and passes. 5 of the 9 `meta/` refs this census
# classifies dangling are that shape (`meta_backpressure`, `meta_experiment`,
# `meta_generations`, `meta_transfer`, `strategy_apply` — all real files under
# `mind_api/src/meta/`). They are dangling AS WRITTEN, which is literally true
# and still misleading in aggregate, so the count is reported rather than
# quietly filtered. A `meta/*.py`-is-noise rule would clear all five today
# because the meta root holds no Python — that is a guess about the root's
# future shape, not a measurement, and is deliberately not encoded here.
# The remaining 4 are genuine: `meta/decision-rules.yaml`,
# `meta/strategic-pulse-runs.jsonl`, a `.json`-for-`.jsonl` typo recorded in a
# store, and one malformed `meta/meta/goal-selection-strategy.yaml`.
#
# The four pre-existing families are deliberately left unguarded. `world/`,
# `core/`, `.claude/` and `agents/<name>/` are all long enough or anchored
# enough that the same substring collision was not observed for them, and
# widening the guard would move the headline numbers for reasons this goal did
# not measure. A probe comparing bare vs guarded on those four showed a 5-ref
# delta whose direction (false positives vs legitimate refs the guard would
# DROP) was NOT resolved — the grep that settled the `meta/` case was not run
# for them. Filed separately rather than guessed at here.
_ARTIFACT_RE = re.compile(
    r"(?:agents/[a-z][a-z0-9-]*/(?:temp|reports|experience|session|journal)/[^\s`\)\]\"',;:\\]+"
    r"|world/conventions/[^\s`\)\]\"',;:\\]+"
    r"|(?<![\w/-])meta/[^\s`\)\]\"',;:\\]+"
    r"|core/(?:config|scripts)/[^\s`\)\]\"',;:\\]+"
    r"|\.claude/(?:skills|rules)/[^\s`\)\]\"',;:\\]+)"
)

# A ref is noise, not a citation, when it is a glob, a placeholder, a regex
# fragment, or a bare directory. Each token here was observed as a live
# false positive on the first run of this tool (2026-07-31), not guessed:
#   `core/config/X.yaml` (placeholder)   `core/config/.+` (regex fragment)
#   `core/config/'` (dangling quote)     `core/config/` (bare dir)
_NOISE = ("*", "<", "{", "$", "?", "+", "|", "\\", "'", '"', "`")

# A final path segment that is a metavariable rather than a filename. ALL-CAPS
# stems are the documentation convention for a placeholder (`PYFILE.py`,
# `X.yaml`), so they are excluded as a class rather than enumerated.
_PLACEHOLDER_SEG = re.compile(r"^(?:[A-Z][A-Z0-9_]*|name|agent|id|foo|bar|baz)$")

# A census of ARTIFACTS requires an artifact extension. Without this, a
# `module.function` reference parses as stem+extension and is reported as a
# dangling file: `core/scripts/_dt.parse_naive_iso` was classified dangling on
# this tool's first run, and it is not a path at all. Requiring a real
# extension is the principled form of that filter — it needs no list of
# function names and does not rot as new helpers are added.
_ARTIFACT_EXT = {
    "md", "py", "sh", "yaml", "yml", "json", "jsonl", "txt", "log",
    "csv", "ini", "toml", "cfg", "lua", "ts", "js", "java",
}


def _is_noise(ref):
    if ref.endswith("/") or any(t in ref for t in _NOISE) or "..." in ref:
        return True
    last = ref.rsplit("/", 1)[-1]
    # A trailing segment with no extension is a directory reference, not an
    # artifact citation — the census counts artifacts.
    if "." not in last:
        return True
    stem, ext = last.split(".", 1)
    if ext.lower() not in _ARTIFACT_EXT:
        return True
    return bool(_PLACEHOLDER_SEG.match(stem))


def _resident_agents():
    """Agents whose artifacts this box can vouch for.

    An agent is resident when its directory carries a `local-paths.conf` — the
    same signal `_paths.enumerate_agent_confs` uses to identify a configured
    agent on this machine. MIND_AGENT is always included: the bound agent owns
    its own temp even mid-bootstrap.
    """
    resident = set()
    bound = os.environ.get("MIND_AGENT", "").strip()
    if bound:
        resident.add(bound)
    try:
        for conf in agents_root().glob("*/local-paths.conf"):
            resident.add(conf.parent.name)
    except OSError:
        pass
    return resident


def _owning_agent(ref):
    """Agent name for an `agents/<name>/...` ref, else None."""
    parts = ref.split("/")
    if len(parts) >= 2 and parts[0] == "agents":
        return parts[1]
    return None


def _sources(store_names):
    """(iterable_of_paths, label) pairs for every scanned surface.

    Coverage is D1's stated set — tree node bodies + world/conventions/ +
    aspirations.jsonl / reasoning-bank.jsonl / pipeline.jsonl — plus
    guardrails.jsonl, which was MEASURED to carry these references too and
    whose omission would make the census under-report by construction.
    """
    tree_root = WORLD_DIR / "knowledge" / "tree"
    yield (tree_root.rglob("*.md") if tree_root.is_dir() else []), "tree"
    conv = WORLD_DIR / "conventions"
    yield (conv.rglob("*.md") if conv.is_dir() else []), "conventions"
    yield [WORLD_DIR / n for n in store_names], "stores"


def _walk_strings(obj):
    """Yield every string value in a decoded JSON structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _iter_store_text(path):
    """Yield DECODED string values from a JSONL store, one per field.

    Scanning the raw line instead would regex JSON-escaped prose, where a `\\n`
    escape glues two unrelated lines into one token — the dominant false
    positive on this tool's first run. Decoding first means every candidate is
    a real field value with real newlines. Fail-open per line: a malformed line
    is skipped, never fatal (these are append-only stores and one bad row must
    not blind the census).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                yield from _walk_strings(rec)
    except OSError:
        return


def collect(store_names, extra_roots=()):
    """Map ref -> set(surface labels). Fail-open on any unreadable file."""
    hits = defaultdict(set)
    surfaces = list(_sources(store_names))
    for root, label in extra_roots:
        surfaces.append((root, label))
    for paths, label in surfaces:
        for p in paths:
            p = Path(p)
            if label == "stores":
                chunks = _iter_store_text(p)
            else:
                try:
                    chunks = [p.read_text(encoding="utf-8", errors="ignore")]
                except (OSError, ValueError):
                    continue  # fail-open, mirroring _iter_body_md_refs
            for text in chunks:
                for m in set(_ARTIFACT_RE.findall(text)):
                    ref = m.rstrip(".,);:")
                    if not _is_noise(ref):
                        hits[ref].add(label)
    return hits


def classify(ref, resident, assume_local_authoritative=False):
    """live | dangling | unmeasurable — see the module docstring.

    Resolution goes through `_paths.resolve_file_path`, NEVER `PROJECT_ROOT /
    ref`. `world/` and `meta/` are EXTERNAL configured paths on this
    deployment (`.claude/rules/path-resolution.md`), so joining them to
    PROJECT_ROOT yields a path that never exists — and this function's whole
    job is deciding whether a path exists. Measured 2026-07-31 (g-306-99
    fresh-eyes): with the naive join, `world/conventions/capability-routing.md`
    — the single most-referenced convention in the repo — classified
    `dangling`, and ~70 of 132 reported dangling refs were fabrications of the
    resolver rather than real breakage. A census that resolves paths wrongly
    reports the loudest possible false alarm, so this line is the one to guard.
    """
    exists = resolve_file_path(ref).exists()
    if exists:
        return "live"
    owner = _owning_agent(ref)
    if owner is None:
        # Non-agent artifact. For `core/` and `.claude/` this is sound: they are
        # git-tracked and present on every box, so absence here IS absence.
        #
        # It is WEAKER for the two EXTERNAL roots, `world/conventions/` and (as
        # of ) `meta/`. Those are gitignored and, on an own-cloud
        # deployment, the local tree is a read-through cache — a file nobody has
        # read on this box may not be materialized locally even though it is
        # alive in the store of record (guard-980). So a `dangling` verdict on
        # those two families can be a cache miss wearing a breakage costume.
        #
        # Not corrected here, deliberately: routing them through a backend HEAD
        # would be a design change, and  was scoped to the pattern.
        # Scale, measured 2026-07-31: 9 of 1287 refs are `meta/` dangling, and
        # 2 of those 9 (`meta/decision-rules.yaml`,
        # `meta/strategic-pulse-runs.jsonl`) are absent locally and UNVERIFIED
        # against the store — a backend probe was attempted and failed its own
        # positive control, so it returned no signal rather than a negative one.
        # Tracked by . Treat a small `dangling` count on an external
        # family as an upper bound, not a finding.
        return "dangling"
    if assume_local_authoritative or owner in resident:
        return "dangling"
    return "unmeasurable"


def census(store_names, assume_local_authoritative=False):
    resident = _resident_agents()
    hits = collect(store_names)
    records = []
    for ref in sorted(hits):
        records.append({
            "ref": ref,
            "inbound_count": len(hits[ref]),
            "surfaces": sorted(hits[ref]),
            "owner": _owning_agent(ref),
            "status": classify(ref, resident, assume_local_authoritative),
        })
    return records, sorted(resident)


def main():
    ap = argparse.ArgumentParser(
        description="Read-only inbound-reference census for non-tree artifacts "
                    "(g-306-99). Reports live / dangling / unmeasurable.")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--status", choices=("live", "dangling", "unmeasurable"),
                    help="only show refs in this state")
    ap.add_argument("--min-count", type=int, default=1,
                    help="only show refs with at least this many referencing surfaces")
    ap.add_argument("--assume-local-authoritative", action="store_true",
                    help="treat local absence as authoritative for EVERY agent. "
                         "Single-box deployments only — on a fleet this "
                         "re-introduces the box-dependence flaw the three-state "
                         "model exists to prevent.")
    ap.add_argument("--exit-on-dangling", action="store_true",
                    help="exit 1 when any MEASURABLE dangling ref exists")
    ap.add_argument("--stores", default="aspirations.jsonl,reasoning-bank.jsonl,"
                                        "pipeline.jsonl,guardrails.jsonl",
                    help="comma-separated JSONL stores under WORLD_DIR to scan")
    args = ap.parse_args()

    store_names = [s.strip() for s in args.stores.split(",") if s.strip()]
    records, resident = census(store_names, args.assume_local_authoritative)

    shown = [r for r in records
             if r["inbound_count"] >= args.min_count
             and (args.status is None or r["status"] == args.status)]
    counts = Counter(r["status"] for r in records)
    measurable_dangling = counts["dangling"]

    if args.json:
        print(json.dumps({
            "total_refs": len(records),
            "by_status": dict(counts),
            "resident_agents": resident,
            "measurable_dangling": measurable_dangling,
            "assume_local_authoritative": args.assume_local_authoritative,
            "records": shown,
        }, indent=2))
    else:
        print("inbound-reference census — %d distinct non-tree artifact refs"
              % len(records))
        print("  live %d | dangling %d | unmeasurable %d   (resident agents: %s)"
              % (counts["live"], counts["dangling"], counts["unmeasurable"],
                 ", ".join(resident) or "none"))
        if counts["unmeasurable"]:
            print("  NOTE: %d ref(s) belong to non-resident agents' boxes — absence "
                  "here is NOT evidence they dangle." % counts["unmeasurable"])
        for r in sorted(shown, key=lambda x: (-x["inbound_count"], x["ref"])):
            print("  %-12s n=%-3d %-28s %s"
                  % (r["status"], r["inbound_count"], ",".join(r["surfaces"]), r["ref"]))

    if args.exit_on_dangling and measurable_dangling:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
