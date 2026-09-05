#!/usr/bin/env python3
"""Entity-fact citation lint for ground-truth knowledge writes ().

USER DIRECTIVE 2026-08-31, "enforce no-publish-from-memory": priors MAY generate
candidates but MUST NOT publish as fact. Every factual assertion about the
EXTERNAL WORLD written to a ground-truth store carries either

  (a) a SOURCE TOKEN -- a URL, a tree-node key, a board msg-id, or a goal-id
      whose description carries the ground truth; or
  (b) an explicit ``[UNVERIFIED -- model prior]`` tag.

An unmarked entity-bearing fact line is the violation.

MOTIVATING INCIDENT (coach g-012-02). A node published 6 of 16 identities that
were famous-name priors displacing the real entities, with bare PUBLICATION NAMES
("Reuters", "Bloomberg") standing in for sources. The deployment's own guard-1
held on the node where URLs were mandatory and failed on the node where sources
were prose -- so the rule was right and only its ENFORCEMENT was missing. That is
what this module supplies, and it is why a bare publication name is deliberately
NOT a source token: accepting one would reproduce the incident exactly.

TWO KINDS OF FAILURE, ONE SEVERITY (the goal's own wording). A cluster with no
token at all, and a cluster whose cited URL/node was NEVER RETRIEVED THIS SESSION,
are flagged the same. The second is the DECORATIVE citation -- the shape that
makes a fabricated claim look sourced -- and treating it as milder would leave
the incident's most persuasive half unguarded.

DESIGN CONSTRAINT, stated by the goal and binding on every heuristic here:
"deterministic, testable, tolerant: better to under-flag than spam". So a line is
a candidate ONLY when it carries an ENTITY signal AND an ASSERTION signal, and
every structural line (heading, fence, table rule, front matter) is excluded
before either test runs. A gate that cried wolf on prose would be turned off, and
a gate that is off flags nothing at all.

READ THE PROVENANCE NEGATIVE CORRECTLY (guard-4407, inherited from
provenance-check): "not in the manifest" means no TOOL-fetch record this session.
A page pulled with curl in a Bash call is invisible to it by construction. That is
why this module is ADVISORY by default -- the finding is a prompt to go verify,
never by itself proof of invention.
"""
from __future__ import annotations

import re
from typing import Iterable, NamedTuple

# ─── what counts as an ENTITY ────────────────────────────────────────────────
# Two or more consecutive Capitalized words ("Acme Corporation"), a bare year, a
# number carrying a unit, or a currency amount. Single capitalized words are
# deliberately NOT an entity signal: sentence-initial words would match every
# line in the corpus, which is the spam direction.
_PROPER_RUN = re.compile(r"\b[A-Z][A-Za-z]{2,}(?:\s+[A-Z][A-Za-z]{2,})+\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_NUM_UNIT = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s?(?:%|percent|bn|billion|million|trillion|"
    r"kg|km|mi|MW|GW|TWh|USD|EUR|GBP)\b", re.IGNORECASE)
_CURRENCY = re.compile(r"[$€£]\s?\d")
_ENTITY_PATTERNS = (_PROPER_RUN, _YEAR, _NUM_UNIT, _CURRENCY)

# ─── what counts as an ASSERTION ─────────────────────────────────────────────
# A copula or a reporting/movement verb, or a numeric comparison. Requiring this
# ALONGSIDE an entity is what keeps headings, name lists and cross-reference
# lines out of the candidate set.
_COPULA = re.compile(r"\b(?:is|are|was|were|has|have|had|will|does|did)\b")
_REPORTING = re.compile(
    r"\b(?:reported|announced|found|showed|shows|said|stated|confirmed|"
    r"published|launched|acquired|filed|ruled|rose|fell|grew|declined|"
    r"increased|decreased|reached|totall?ed|employs|owns|operates)\b",
    re.IGNORECASE)
_COMPARISON = re.compile(r"(?:>=|<=|[<>=])\s*\d")
_ASSERTION_PATTERNS = (_COPULA, _REPORTING, _COMPARISON)

# ─── what counts as a SOURCE TOKEN ───────────────────────────────────────────
# Exactly the four the directive names. A bare publication name is NOT here, on
# purpose -- see the module docstring.
_URL = re.compile(r"https?://[^\s<>()\[\]]+")
_BOARD_MSG = re.compile(r"\bmsg-\d{8}-\d{6}-[a-z0-9]+-\d+\b")
_GOAL_ID = re.compile(r"\bg-\d{3}-\d{1,4}\b")
# A tree-node key is a slash-joined slug ("system/daemon-only-architecture"),
# optionally the full store path. Anchored on the slug shape so ordinary prose
# containing a slash ("and/or") cannot satisfy a citation requirement.
_NODE_KEY = re.compile(
    r"\b(?:world/knowledge/tree/)?[a-z0-9]+(?:-[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+(?:\.md)?\b")
_UNVERIFIED = re.compile(r"\[\s*UNVERIFIED\b", re.IGNORECASE)

# Structural lines that can never be a fact line.
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}")
_FRONT_MATTER_DELIM = re.compile(r"^---\s*$")


class FactLine(NamedTuple):
    lineno: int          # 1-based, within the scanned text
    text: str


class Cluster(NamedTuple):
    """A contiguous run of non-blank lines containing at least one fact line."""
    fact_lines: list
    source_tokens: list      # (kind, value) pairs found anywhere in the cluster
    has_unverified_tag: bool
    start_line: int
    end_line: int


class Finding(NamedTuple):
    kind: str                # "missing-citation" | "decorative-citation"
    start_line: int
    end_line: int
    detail: str
    sample: str


def is_entity_bearing(line: str) -> bool:
    return any(p.search(line) for p in _ENTITY_PATTERNS)


def is_assertion(line: str) -> bool:
    return any(p.search(line) for p in _ASSERTION_PATTERNS)


def is_structural(line: str) -> bool:
    return bool(_HEADING.match(line) or _TABLE_RULE.match(line)
                or _FRONT_MATTER_DELIM.match(line) or not line.strip())


def source_tokens(text: str) -> list:
    """Every source token in ``text``, as (kind, value).

    URLs are matched FIRST and their spans removed before the node-key scan, or
    a URL's own path segments would be miscounted as a tree-node key and a
    fabricated URL would satisfy the citation requirement twice over.
    """
    found = []
    masked = text
    for m in _URL.finditer(text):
        found.append(("url", m.group(0)))
    masked = _URL.sub(" ", masked)
    for kind, pat in (("board-msg", _BOARD_MSG), ("goal-id", _GOAL_ID)):
        for m in pat.finditer(masked):
            found.append((kind, m.group(0)))
    masked = _BOARD_MSG.sub(" ", masked)
    masked = _GOAL_ID.sub(" ", masked)
    for m in _NODE_KEY.finditer(masked):
        found.append(("node-key", m.group(0)))
    return found


def iter_clusters(text: str) -> Iterable[Cluster]:
    """Split ``text`` into contiguous non-blank runs and yield those that carry
    at least one fact line.

    Code fences are skipped wholesale: a fenced block is a transcript or a
    command, not a published claim, and scanning it produced the loudest
    false positives in hand-testing.
    """
    lines = text.splitlines()
    in_fence = False
    in_front_matter = False
    if lines and _FRONT_MATTER_DELIM.match(lines[0]):
        in_front_matter = True
    run: list = []          # list[(lineno, line)]
    start = 0

    def _finish(run, start, end):
        if not run:
            return None
        facts = [FactLine(n, l) for n, l in run
                 if not is_structural(l) and is_entity_bearing(l) and is_assertion(l)]
        if not facts:
            return None
        blob = "\n".join(l for _, l in run)
        return Cluster(facts, source_tokens(blob), bool(_UNVERIFIED.search(blob)),
                       start, end)

    for idx, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            c = _finish(run, start, idx - 1)
            if c:
                yield c
            run, start = [], 0
            continue
        if in_fence:
            continue
        if in_front_matter:
            if idx > 1 and _FRONT_MATTER_DELIM.match(line):
                in_front_matter = False
            continue
        if not line.strip():
            c = _finish(run, start, idx - 1)
            if c:
                yield c
            run, start = [], 0
            continue
        if not run:
            start = idx
        run.append((idx, line))
    c = _finish(run, start, len(lines))
    if c:
        yield c


def analyze(text: str, retrieved=None) -> list:
    """Findings for ``text``.

    ``retrieved`` is a predicate ``(kind, value) -> bool`` answering "was this
    cited thing actually retrieved in THIS session?" -- supplied by the caller so
    this module stays pure and testable with no manifest on disk. When it is
    None the decorative-citation check is SKIPPED rather than assumed-true: a
    check that cannot run must not manufacture a pass (guard-1760).
    """
    findings = []
    for cl in iter_clusters(text):
        sample = cl.fact_lines[0].text.strip()[:120]
        if cl.has_unverified_tag:
            continue
        if not cl.source_tokens:
            findings.append(Finding(
                "missing-citation", cl.start_line, cl.end_line,
                "entity-bearing fact line with no source token and no "
                "[UNVERIFIED -- model prior] tag. A bare publication name is "
                "not a source token (coach g-012-02).", sample))
            continue
        if retrieved is None:
            continue
        checkable = [t for t in cl.source_tokens if t[0] in ("url", "node-key")]
        if checkable and not any(retrieved(k, v) for k, v in checkable):
            cited = ", ".join(v for _, v in checkable[:3])
            findings.append(Finding(
                "decorative-citation", cl.start_line, cl.end_line,
                f"cited but NOT retrieved this session: {cited}. A citation the "
                "session never fetched is DECORATIVE -- same severity as none "
                "(g-357-43 provenance manifest).", sample))
    return findings
