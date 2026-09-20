#!/usr/bin/env python3
"""domain-term-census.py -- per-term coverage census for the core/domain border.

Answers, for every candidate domain term the domain's OWN REGISTRIES produce:
how many in-scope framework files contain it, how many of those survive the
scanner's exemption cascade (the genuine leaks), and whether the term is on the
blocklist at all.

    term | class | share | in_scope | unmarked | unmarked_cs | blocklisted

WHY THIS EXISTS (g-115-10049 outcome 2)
---------------------------------------
`core/config/domain-term-blocklist.txt` is 32 HAND-TYPED terms. A hand-typed
list cannot answer "what are we NOT looking for?", so the wall's coverage was
unmeasurable: every clean scan was clean with respect to a list nobody could
audit. The input contract for this census is fixed by outcome 3
(`core/config/rationale/domain-term-classes.md`): derive candidates from the
registries named there, NEVER from a second hand-typed list.

  class A  product / world-facing names -> forged-skills.yaml skill NAMES,
                                           program.md proper nouns,
                                           world/conventions/*.md headings
  class B  deployment identities        -> core/config/environments/*.yaml ids
                                           and file basenames
  class C  infrastructure / operator ids-> NO REGISTRY, BY DESIGN. Not derivable.

Class C is not a gap in this tool; it is the recorded decision. Those terms are
measurement PROVENANCE, not vocabulary, so there is nothing to enumerate them
from. The census still COUNTS any class-C term already on the blocklist and says
how many terms it covers for that reason alone -- a scope bound stated as a
NUMBER rather than as a prose sufficiency argument (guard-6415).

NOTE ON `triggers:` -- deliberately NOT a source. Outcome 2 names forged-skill
NAMES. Triggers are natural-language phrases ("does this appear in the logs"),
and deriving tokens from them floods the universe with English: the first build
of this census derived 1,788 terms whose top rows were `path`, `never`,
`source`, `exit`. A registry-derived census is only as good as the registry
field it reads.

NO STOPWORD LIST, AND NO CLASSIFIER EITHER -- BOTH WERE TRIED AND MEASURED
--------------------------------------------------------------------------
The first build separated domain terms from English with a hand-typed STOPWORD
list. That is the very defect this goal exists to remove, reintroduced one layer
down: an unauditable typed list deciding what the audit looks at. It was removed.

TWO replacements were then tried and BOTH ARE FALSIFIED. They are recorded here
so the next reader does not spend the afternoon re-deriving them:

1. SHARE BAND -- "a domain term is rare in a corpus whose rule is to contain no
   domain terms." On the first (trigger-polluted) universe this looked clean:
   domain terms 0.4%-13.1%, framework words 66.9%-90.1%, nothing between. With
   the derivation corrected to NAMES the band CLOSED: heading-derived English
   (`Current` 18.8%, `Knowledge` 17.7%, `Layer` 19.5%) lands directly on top of
   `Ayoai` at 13.1%. There is no separating threshold.

2. LOWERCASE-OBSERVED -- "a true proper noun is never seen lowercase." Tested
   against the registry corpus (476,867 tokens): 11 of 11 known domain terms AND
   18 of 19 known English words all read `lower_observed=True`. ZERO
   discriminating power, not merely a weak signal.

So this census does NOT classify. It DERIVES, COUNTS, and REPORTS, and the
deciding is outcome 4's job by design. What it gives a reviewer instead of a
verdict is the per-source precision table, computed LIVE at every run (never
hardcoded -- it is a property of the registries on the day you run it). Measured
2026-09-20 (zeta, cc-02, Linux 6.8.0-139-generic, 3,304 in-scope files):

    source                  terms  median share   share<5%
    conventions/heading      1345          5.1%        49%
    forged-skills/name        216          6.3%        46%
    program.md/proper-noun    180          8.8%        41%
    environments/basename       6          1.9%        83%
    environments/id             6          1.9%        83%

The shape of the answer: `environments/*` (class B) is the only high-precision
registry, and it is six terms. The three class-A sources are PROSE, and one of
them (conventions headings) contributes 89% of the whole candidate universe. A
registry-derived census is exactly as precise as the registry field it reads,
and outcome 2 named the fields. Slice with `--source` to work the precise end
first; `--only-gaps` for the unblocklisted residue.

`--max-share` still exists and HIDES NOTHING BY DEFAULT (1.0). It is a usability
knob, NOT a classifier, because finding 1 above says no threshold separates the
populations. When it is set, its effect is reported as a count and
`--show-vocabulary` names the identities: a filter keyed on shape is a silent
denominator bug when its effect goes unreported (guard-6749), and a count
without identities is unresolvable by construction (guard-6214). Blocklisted
terms are NEVER hidden whatever their share -- they are already decided, and
hiding one would make this census disagree with the wall it measures.

WHY A SIBLING SCRIPT AND NOT `domain-leak-check.sh --census`
------------------------------------------------------------
The goal permits either. The scanner greps RECURSIVELY ONCE PER TERM PER SCAN
DIR: at 32 terms that is already a >2min full-tree scan (which is why --staged
exists). A census runs hundreds of candidates, so the scanner's shape is
O(terms x corpus) and would take hours. This script inverts the loop -- one pass
over the corpus, every term tested against each file's token set -- and measures
3,304 files in under 4 seconds.

That inversion has a cost, and it is the one thing to watch: this file carries
its OWN copy of the scan scope and the exemption cascade. Those are pinned
against the scanner's literal text by
`core/scripts/tests/test_domain_term_census.py`, so a change to SCAN_DIRS or to
a filter in `domain-leak-check.sh` that is not mirrored here FAILS A TEST rather
than silently censusing a different corpus than the wall enforces (rb-1915: pin
both sides to a shared fixture). The marker predicate is NOT re-derived -- it is
imported from `_domain_leak_marker`, the single source of truth built for
exactly this reason (g-115-10246, guard-6989).

COUNTING DISCIPLINE
-------------------
* `grep -w` word constituents are [A-Za-z0-9_], so for a SINGLE-TOKEN term,
  membership in a file's token set is EXACTLY equivalent to `grep -w`. That
  equivalence is what makes the fast path legitimate rather than approximate.
  Multi-token terms ("widget service", "Acme-Service" -- generic placeholders;
  this file must stay domain-free, see the closing note) fall back to a real
  word-boundary regex, pre-filtered by a necessary condition on their first
  sub-token so the slow path runs on few files.
* Nothing here reports a zero it has not positive-controlled. `--self-test`
  plants a sentinel inside the real scan scope and asserts the count rises,
  which separates "the term is absent" from "the scan never looked"
  (guard-1465, guard-6233).
* A registry that cannot be read VOIDS the census rather than silently shrinking
  it (guard-2081). There is no partial mode.

THIS FILE CARRIES NO EXEMPTION MARKER, DELIBERATELY. It names no domain term:
every term it handles arrives at runtime from a registry or the blocklist, so
under the wall's own predicate it is clean, and stays clean as the domain
changes (guard-6087, the same discipline `domain-term-classes.md` applies to
itself).

That property was nearly lost at first commit and had to be repaired: the
docstring above illustrated multi-token matching with two REAL blocklist terms,
which made this file a genuine leak while it asserted the opposite two
paragraphs down. Caught by scanning the new file against the blocklist before
committing it. This is guard-3518 — documentation ABOUT a scanner gets scanned
BY it — and it is a standing hazard for anyone editing this file: illustrate
with generic placeholders ("widget service", "Acme-Service"), never with a term
the wall actually looks for. The sibling hazard for the exemption marker is in
`domain-term-classes.md`; this is the same trap through the example door.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent.parent
sys.path.insert(0, str(SCRIPTS))

from _domain_leak_marker import claims_exemption  # noqa: E402

BLOCKLIST_PATH = PROJECT_ROOT / "core" / "config" / "domain-term-blocklist.txt"
ENVIRONMENTS_DIR = PROJECT_ROOT / "core" / "config" / "environments"

# ---------------------------------------------------------------------------
# Scope -- MIRRORS domain-leak-check.sh. Pinned by test_domain_term_census.py.
# ---------------------------------------------------------------------------
CORE_SCAN_DIRS = ("core/config", "core/scripts", ".claude/rules")
EXTRA_SCAN_DIRS = (".claude/skills", "mind_api/src", "mind_api/tests")
INCLUDE_SUFFIXES = (".md", ".yaml", ".yml", ".sh", ".py", ".txt")

# Hit-filter cascade, in the scanner's own order.
SELF_REFERENTIAL = (
    "domain-term-blocklist.txt",
    "domain-leak-check.sh",
    "domain-free-examples.md",
)
TEST_FIXTURE_RX = re.compile(r"/test[-_][A-Za-z0-9_-]+\.(sh|py)$")
META_DOC = "verify-learning/SKILL.md"
FORGED_FRONT_MATTER_RX = re.compile(r"^forged:[ \t]*true[ \t]*$", re.MULTILINE)

WORD_RX = re.compile(r"[A-Za-z0-9_]+")
PROPER_NOUN_RX = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")

MIN_TOKEN_LEN = 3
# Hides NOTHING by default. See the module docstring: no share threshold
# separates domain terms from framework vocabulary (measured, finding 1), so a
# default filter here would discard real terms while looking principled.
DEFAULT_MAX_SHARE = 1.0


class CensusVoid(RuntimeError):
    """A required registry was unreadable. The whole aggregate is void."""


def _read(path: Path, *, required: bool) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if required:
            raise CensusVoid(f"cannot read required registry {path}: {exc}") from exc
        return ""


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def forged_skill_dirs(world_dir: Path | None) -> set[str]:
    """Skill names from world/forged-skills.yaml, matching the scanner's sed.

    The scanner passes each as `--exclude-dir=<name>`, which excludes a
    directory of that name at ANY depth -- so this returns names, not paths.
    CRLF-tolerant: the registry syncs from Windows boxes (g-115-1934). Fails
    OPEN (scans everything) when the registry is absent, exactly as the
    scanner does.
    """
    if world_dir is None:
        return set()
    registry = world_dir / "forged-skills.yaml"
    if not registry.is_file():
        return set()
    names: set[str] = set()
    in_skills = False
    for raw in _read(registry, required=True).splitlines():
        line = raw.rstrip("\r")
        if line.startswith("skills:"):
            in_skills = True
            continue
        if in_skills:
            if line and not line[0].isspace():
                break
            m = re.match(r"^ {2}([a-z][A-Za-z0-9_-]*):[ \t]*$", line)
            if m:
                names.add(m.group(1))
    return names


def iter_scope_files(core_only: bool, forged: set[str]):
    dirs = list(CORE_SCAN_DIRS) + ([] if core_only else list(EXTRA_SCAN_DIRS))
    for rel in dirs:
        root = PROJECT_ROOT / rel
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in forged]
            for name in filenames:
                if name.endswith(INCLUDE_SUFFIXES):
                    yield Path(dirpath) / name


def exemption_reason(rel_posix: str, text: str) -> str | None:
    """None when the file's hits would be REPORTED; else why they are dropped.

    Mirrors the scanner's filter cascade in order. An exempt file is still IN
    SCOPE -- the gap between those two columns is the whole point of this
    census, because that gap is where the wall's real coverage lives.
    """
    for marker in SELF_REFERENTIAL:
        if marker in rel_posix:
            return "self-referential"
    if TEST_FIXTURE_RX.search("/" + rel_posix):
        return "test-fixture"
    if META_DOC in rel_posix:
        return "meta-documentation"
    if claims_exemption(text):
        return "exempt-marker"
    if FORGED_FRONT_MATTER_RX.search(text):
        return "forged-front-matter"
    return None


# ---------------------------------------------------------------------------
# Term derivation from the registries
# ---------------------------------------------------------------------------

def _tokens(s: str) -> list[str]:
    return WORD_RX.findall(s)


def _shape_ok(tok: str) -> bool:
    """The ONLY shape rule. Everything else is decided by measured share."""
    return len(tok) >= MIN_TOKEN_LEN and not tok.isdigit()


class Candidates:
    """Accumulates term -> (class, sources), deduped case-insensitively."""

    def __init__(self) -> None:
        self.by_key: dict[str, dict] = {}

    def add(self, term: str, cls: str, source: str) -> None:
        term = term.strip()
        if not term:
            return
        key = term.lower()
        entry = self.by_key.setdefault(
            key, {"term": term, "class": cls, "sources": set()})
        entry["sources"].add(source)
        # class B (a deployment identity) outranks A: the ruling differs.
        if cls == "B":
            entry["class"] = "B"

    def add_tokens(self, text: str, cls: str, source: str) -> None:
        for tok in _tokens(text):
            if _shape_ok(tok):
                self.add(tok, cls, source)


def derive_candidates(world_dir: Path | None) -> Candidates:
    c = Candidates()

    # --- class B: deployment identities (ids + file basenames) ---
    if not ENVIRONMENTS_DIR.is_dir():
        raise CensusVoid(f"class-B registry missing: {ENVIRONMENTS_DIR}")
    env_files = sorted(ENVIRONMENTS_DIR.glob("*.yaml"))
    if not env_files:
        raise CensusVoid(f"class-B registry empty: {ENVIRONMENTS_DIR}")
    for f in env_files:
        c.add(f.stem, "B", "environments/basename")
        for line in _read(f, required=True).splitlines():
            m = re.match(r"^environment_id:[ \t]*['\"]?([^'\"\s#]+)", line)
            if m:
                c.add(m.group(1), "B", "environments/id")

    if world_dir is None:
        raise CensusVoid("world path unresolved -- class-A registries unreadable")

    # --- class A: forged skill NAMES (not triggers -- see module docstring) ---
    registry = world_dir / "forged-skills.yaml"
    if not registry.is_file():
        raise CensusVoid(f"class-A registry missing: {registry}")
    in_skills = False
    names_seen = 0
    for raw in _read(registry, required=True).splitlines():
        line = raw.rstrip("\r")
        if line.startswith("skills:"):
            in_skills = True
            continue
        if not in_skills:
            continue
        if line and not line[0].isspace():
            break
        m = re.match(r"^ {2}([a-z][A-Za-z0-9_-]*):[ \t]*$", line)
        if m:
            names_seen += 1
            c.add_tokens(m.group(1), "A", "forged-skills/name")
    if names_seen == 0:
        raise CensusVoid(f"class-A registry parsed to ZERO skill names: {registry}")

    # --- class A: program.md proper nouns ---
    program = world_dir / "program.md"
    if not program.is_file():
        raise CensusVoid(f"class-A registry missing: {program}")
    for noun in PROPER_NOUN_RX.findall(_read(program, required=True)):
        if _shape_ok(noun):
            c.add(noun, "A", "program.md/proper-noun")

    # --- class A: world/conventions/*.md headings ---
    conv_dir = world_dir / "conventions"
    if not conv_dir.is_dir():
        raise CensusVoid(f"class-A registry missing: {conv_dir}")
    conv_files = sorted(conv_dir.glob("*.md"))
    if not conv_files:
        raise CensusVoid(f"class-A registry empty: {conv_dir}")
    for f in conv_files:
        for line in _read(f, required=True).splitlines():
            if re.match(r"^#{1,6}[ \t]", line):
                for noun in PROPER_NOUN_RX.findall(line):
                    if _shape_ok(noun):
                        c.add(noun, "A", "conventions/heading")
    return c


def read_blocklist() -> list[str]:
    terms = []
    for line in _read(BLOCKLIST_PATH, required=True).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

class TermCounter:
    """Counts, per term, files that contain it -- case-insensitively and not.

    Single-token terms use set membership, which is EXACTLY `grep -w` semantics
    because grep's word constituents are [A-Za-z0-9_]. Multi-token terms use a
    word-boundary regex gated on a cheap necessary condition (their first
    sub-token must be present), so the regex runs on few files.
    """

    def __init__(self, terms: list[str]) -> None:
        self.terms = terms
        self.single: dict[str, list[str]] = {}
        self.single_cs: dict[str, list[str]] = {}
        self.multi: list[tuple[str, str, re.Pattern, re.Pattern]] = []
        for t in terms:
            toks = _tokens(t)
            if len(toks) == 1 and toks[0] == t:
                self.single.setdefault(t.lower(), []).append(t)
                self.single_cs.setdefault(t, []).append(t)
            elif toks:
                pat = r"\b" + re.escape(t) + r"\b"
                self.multi.append(
                    (t, toks[0].lower(), re.compile(pat, re.IGNORECASE), re.compile(pat)))
        self.in_scope = {t: 0 for t in terms}
        self.unmarked = {t: 0 for t in terms}
        self.unmarked_cs = {t: 0 for t in terms}

    def feed(self, text: str, exempt: bool) -> None:
        tokset_cs = set(WORD_RX.findall(text))
        tokset = {t.lower() for t in tokset_cs}

        hit: set[str] = set()
        hit_cs: set[str] = set()
        for tok in tokset & self.single.keys():
            hit.update(self.single[tok])
        for tok in tokset_cs & self.single_cs.keys():
            hit_cs.update(self.single_cs[tok])
        for term, first, rx_i, rx_cs in self.multi:
            if first not in tokset:
                continue           # necessary condition failed -- cannot match
            if rx_i.search(text):
                hit.add(term)
                if rx_cs.search(text):
                    hit_cs.add(term)

        for t in hit:
            self.in_scope[t] += 1
            if not exempt:
                self.unmarked[t] += 1
        for t in hit_cs:
            if not exempt:
                self.unmarked_cs[t] += 1


def run_census(core_only: bool, world_dir: Path | None,
               extra_terms: list[str] | None = None,
               max_share: float = DEFAULT_MAX_SHARE):
    cands = derive_candidates(world_dir)
    blocklist = read_blocklist()
    blocklist_lower = {b.lower() for b in blocklist}

    universe: list[str] = []
    meta: dict[str, dict] = {}
    seen: set[str] = set()
    for entry in cands.by_key.values():
        universe.append(entry["term"])
        seen.add(entry["term"].lower())
        meta[entry["term"]] = {"class": entry["class"],
                               "sources": sorted(entry["sources"])}
    for b in blocklist:
        if b.lower() not in seen:
            universe.append(b)
            seen.add(b.lower())
            meta[b] = {"class": "C?", "sources": ["blocklist-only"]}
    for x in extra_terms or []:
        if x.lower() not in seen:
            universe.append(x)
            seen.add(x.lower())
            meta[x] = {"class": "probe", "sources": ["--term"]}

    counter = TermCounter(universe)
    forged = forged_skill_dirs(world_dir)
    files_scanned = 0
    exempt_counts: dict[str, int] = {}

    for path in iter_scope_files(core_only, forged):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_scanned += 1
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        reason = exemption_reason(rel, text)
        if reason:
            exempt_counts[reason] = exempt_counts.get(reason, 0) + 1
        counter.feed(text, exempt=reason is not None)

    rows, vocabulary = [], []
    for t in universe:
        share = (counter.in_scope[t] / files_scanned) if files_scanned else 0.0
        row = {
            "term": t,
            "class": meta[t]["class"],
            "sources": meta[t]["sources"],
            "share": round(share, 4),
            "in_scope": counter.in_scope[t],
            "unmarked": counter.unmarked[t],
            "unmarked_cs": counter.unmarked_cs[t],
            "blocklisted": t.lower() in blocklist_lower,
        }
        # Blocklisted terms are ALWAYS reported: they are already decided, and
        # hiding one would make this census disagree with the wall it measures.
        if share > max_share and not row["blocklisted"]:
            vocabulary.append(row)
        else:
            rows.append(row)

    key = lambda r: (-r["unmarked"], -r["in_scope"], r["term"].lower())  # noqa: E731
    rows.sort(key=key)
    vocabulary.sort(key=key)

    # Per-source precision, computed LIVE. Never hardcode this table: it is a
    # property of the registries on the day the census runs, and the whole
    # reason this script exists is that a hardcoded list cannot be audited.
    by_source: dict[str, dict] = {}
    for r in rows + vocabulary:
        for s in r["sources"]:
            b = by_source.setdefault(s, {"terms": 0, "shares": [], "blocklisted": 0})
            b["terms"] += 1
            b["shares"].append(r["share"])
            b["blocklisted"] += 1 if r["blocklisted"] else 0
    for s, b in by_source.items():
        sh = sorted(b.pop("shares"))
        b["median_share"] = sh[len(sh) // 2] if sh else 0.0
        b["pct_under_5"] = round(100 * sum(1 for x in sh if x < 0.05) / len(sh)) if sh else 0

    # How much of what the WALL looks for can the registries even see? A
    # blocklist term no registry derives is, by outcome 3's ruling, either a
    # class-C identity (no registry BY DESIGN) or an unregistered term.
    not_derivable = [r["term"] for r in rows + vocabulary
                     if r["blocklisted"] and r["sources"] == ["blocklist-only"]]

    return {
        "files_scanned": files_scanned,
        "core_only": core_only,
        "max_share": max_share,
        "forged_dirs_excluded": len(forged),
        "exempt_files_by_reason": exempt_counts,
        "blocklist_terms": len(blocklist),
        "derived_terms": len(cands.by_key),
        "by_source": by_source,
        "blocklist_not_derivable": sorted(not_derivable),
        "vocabulary_excluded": len(vocabulary),
        "vocabulary": vocabulary,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Positive control (guard-1465, guard-6233)
# ---------------------------------------------------------------------------

# Assembled at runtime, NEVER written as one contiguous literal. This file is
# itself in scope (core/scripts/*.py), so a spelled-out sentinel would be found
# in THIS source and the control would report the term present before planting
# anything -- guard-6087, a check falsified by its own installation. The first
# run of this control did exactly that and failed loudly, which is the control
# working. Same discipline `_domain_leak_marker.py` applies to its own token.
SENTINEL = "Zz" + "census" + "probe"


def self_test(world_dir: Path | None) -> int:
    """Plant a sentinel inside the REAL scan scope and prove the count rises.

    Without this, a zero is indistinguishable from a scan that never looked --
    precisely the failure that made the hand-typed blocklist's clean scans
    meaningless. ABSENT-before / PRESENT-after, on the real corpus.
    """
    probe = PROJECT_ROOT / "core" / "config" / "zz-census-positive-control.txt"
    if probe.exists():
        print(f"FAIL: probe path already exists, refusing to clobber: {probe}")
        return 1

    before = run_census(core_only=True, world_dir=world_dir, extra_terms=[SENTINEL])
    b = next(r for r in before["rows"] if r["term"] == SENTINEL)
    if b["in_scope"] != 0:
        print(f"FAIL: sentinel {SENTINEL!r} is not absent before planting "
              f"(in_scope={b['in_scope']}) -- pick a fresh sentinel")
        return 1
    print(f"  ABSENT before:  {SENTINEL} in_scope=0 unmarked=0 "
          f"({before['files_scanned']} files scanned)")

    try:
        probe.write_text(
            f"positive control for domain-term-census: {SENTINEL}\n", encoding="utf-8")
        after = run_census(core_only=True, world_dir=world_dir, extra_terms=[SENTINEL])
        a = next(r for r in after["rows"] if r["term"] == SENTINEL)
    finally:
        try:
            probe.unlink()
        except OSError:
            print(f"WARNING: could not remove probe file {probe} -- remove it by hand")

    print(f"  PRESENT after:  {SENTINEL} in_scope={a['in_scope']} "
          f"unmarked={a['unmarked']} ({after['files_scanned']} files scanned)")
    if a["in_scope"] != 1 or a["unmarked"] != 1:
        print("FAIL: planting the sentinel did not produce exactly one counted file. "
              "The counter is not measuring the corpus it claims to measure.")
        return 1
    if after["files_scanned"] != before["files_scanned"] + 1:
        print(f"FAIL: file count did not rise by exactly 1 "
              f"({before['files_scanned']} -> {after['files_scanned']})")
        return 1
    print("POSITIVE CONTROL PASSED: an absent term reads 0 and a planted term reads 1.")
    return 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(result: dict, *, top: int, show_vocabulary: bool, only_gaps: bool,
           sources: list[str] | None = None) -> None:
    rows = result["rows"]
    if sources:
        rows = [r for r in rows if any(s in sources for s in r["sources"])]
    if only_gaps:
        rows = [r for r in rows if r["unmarked"] > 0 and not r["blocklisted"]]

    print(f"Corpus: {result['files_scanned']} in-scope files "
          f"({'core-only' if result['core_only'] else 'full'}), "
          f"{result['forged_dirs_excluded']} forged-skill dirs excluded")
    exempt = result["exempt_files_by_reason"]
    print("Exempt files: " + (", ".join(f"{k}={v}" for k, v in sorted(exempt.items()))
                              or "none"))
    print(f"Terms: {result['derived_terms']} derived from registries, "
          f"{result['blocklist_terms']} on the blocklist, "
          f"{len(result['rows'])} reported")
    if result["max_share"] < 1.0:
        print(f"Share filter: {result['vocabulary_excluded']} term(s) exceeded "
              f"share>{result['max_share']:.0%} and were withheld (a usability knob, "
              f"NOT a classifier)"
              + ("" if show_vocabulary else " -- see --show-vocabulary"))

    # Registry precision, computed live -- the reviewer's map for where to look.
    print()
    print(f"{'registry source':24s} {'terms':>6} {'median share':>13} {'share<5%':>9} "
          f"{'blocked':>8}")
    for s, b in sorted(result["by_source"].items(), key=lambda kv: -kv[1]["terms"]):
        print(f"{s:24s} {b['terms']:6d} {b['median_share']*100:12.1f}% "
              f"{b['pct_under_5']:8d}% {b['blocklisted']:8d}")

    nd = result["blocklist_not_derivable"]
    print()
    print(f"REGISTRY COVERAGE OF THE WALL: {result['blocklist_terms'] - len(nd)} of "
          f"{result['blocklist_terms']} blocklist terms are derivable from a registry; "
          f"{len(nd)} are not.")
    if nd:
        print("  not derivable (class C by design, or unregistered): "
              + ", ".join(nd))
    print()

    shown = rows[:top] if top > 0 else rows
    w = min(max(max((len(r["term"]) for r in shown), default=4), 4), 30)
    print(f"{'term'.ljust(w)}  cls  share  in_scope  unmarked  unmk_cs  blocked  source")
    print(f"{'-' * w}  ---  -----  --------  --------  -------  -------  ------")
    for r in shown:
        src = ",".join(s.split("/")[-1] for s in r["sources"][:2])
        print(f"{r['term'][:w].ljust(w)}  {r['class']:<3}  {r['share']*100:4.1f}%  "
              f"{r['in_scope']:>8}  {r['unmarked']:>8}  {r['unmarked_cs']:>7}  "
              f"{'yes' if r['blocklisted'] else 'NO':>7}  {src}")
    if top > 0 and len(rows) > top:
        print(f"... {len(rows) - top} more row(s); use --top 0 for all")

    gaps = [r for r in result["rows"] if r["unmarked"] > 0 and not r["blocklisted"]]
    case_gap = [r for r in result["rows"]
                if r["blocklisted"] and r["unmarked"] > r["unmarked_cs"]]
    print()
    print(f"RATCHET (outcome 5): {len(gaps)} term(s) present in core but NOT blocklisted")
    print(f"CASE GAP: {len(case_gap)} blocklisted term(s) whose case-insensitive count "
          f"exceeds their case-sensitive count"
          + (": " + ", ".join(f"{r['term']} {r['unmarked_cs']}->{r['unmarked']}"
                              for r in case_gap) if case_gap else ""))
    if show_vocabulary:
        print()
        print("Classified framework vocabulary (share > "
              f"{result['max_share']:.0%}): "
              + ", ".join(f"{r['term']}({r['share']*100:.0f}%)"
                          for r in result["vocabulary"]))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--core-only", action="store_true",
                    help="scan only core/config, core/scripts, .claude/rules")
    ap.add_argument("--json", action="store_true", help="emit the full result as JSON")
    ap.add_argument("--top", type=int, default=40, help="rows to print (0 = all)")
    ap.add_argument("--max-share", type=float, default=DEFAULT_MAX_SHARE,
                    help="share of in-scope files above which a term is classified "
                         "framework vocabulary rather than a domain term "
                         f"(default {DEFAULT_MAX_SHARE}; re-measure the band first)")
    ap.add_argument("--show-vocabulary", action="store_true",
                    help="list the terms the share filter withheld")
    ap.add_argument("--source", default="",
                    help="comma-separated registry sources to show, e.g. "
                         "'environments/id,forged-skills/name' (the precise end)")
    ap.add_argument("--only-gaps", action="store_true",
                    help="show only terms present in core but absent from the blocklist")
    ap.add_argument("--term", action="append", default=[],
                    help="also count this literal term (repeatable)")
    ap.add_argument("--self-test", action="store_true",
                    help="planted positive control: prove the counter can go non-zero")
    args = ap.parse_args(argv)

    world_dir = None
    for var in ("WORLD_PATH", "WORLD_DIR", "MIND_WORLD"):
        v = os.environ.get(var)
        if v and Path(v).is_dir():
            world_dir = Path(v)
            break
    if world_dir is None:
        default = PROJECT_ROOT / ".mind-data" / "world"
        if default.is_dir():
            world_dir = default

    try:
        if args.self_test:
            return self_test(world_dir)
        result = run_census(args.core_only, world_dir, extra_terms=args.term,
                            max_share=args.max_share)
    except CensusVoid as exc:
        sys.stderr.write(f"CENSUS VOID: {exc}\n")
        sys.stderr.write("A missing registry makes the whole aggregate meaningless; "
                         "no partial census is emitted (guard-2081).\n")
        return 2

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        render(result, top=args.top, show_vocabulary=args.show_vocabulary,
               only_gaps=args.only_gaps,
               sources=[s.strip() for s in args.source.split(",") if s.strip()])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
