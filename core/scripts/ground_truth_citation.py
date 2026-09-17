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
_GOAL_ID = re.compile(r"\bg-\d{3}-\d{1,5}\b")
# The framework's OWN durable rule ids (). A line whose only citation
# was `guard-2024` reported `missing-citation` -- so citing the governing rule
# for a disposition read as UNCITED, which is the alarm direction: the author
# did the right thing and the gate punished it. A guardrail / reasoning-bank id
# is a durable, retrievable record (`guardrails-read.sh --id`,
# `reasoning-bank-read.sh --id`), which is exactly the property that separates a
# source token from "a bare publication name" in the module docstring.
#
# THIS DELIBERATELY DOES NOT MAKE THEM `checkable`. `analyze` adjudicates only
# url / node-key, so a rule-id-only cluster now falls into `if not checkable:
# continue` -- the SAME treatment goal-id-only clusters already get, which the
# _RATIO_RUN note below records as pre-existing policy rather than a new hole.
# Making them checkable would be WORSE: `retrieved_predicate` matches manifest
# values, retrieve.sh writes `#prov:` rows carrying the QUERY text and not the
# ids it returned, so every rule-id would come back unretrieved and BLOCK --
# re-creating this defect one layer down.
#
# THE GAMING VECTOR, stated rather than left for a reader to find: an author can
# now silence `missing-citation` by appending "(guard-NNNN)". That is true of
# `g-NNN-NN` today and is the same pre-existing question about `checkable`;
# this change adds no new class of evasion, it removes an asymmetry where the
# framework's own ids were the one durable id shape that did not count.
# No overlap with _GOAL_ID: `\bg-\d{3}` cannot match inside "guard-2024".
_RULE_ID = re.compile(r"\b(?:guard|rb)-\d{2,6}\b")
# A tree-node key is a slash-joined slug ("system/daemon-only-architecture"),
# optionally the full store path. Anchored on the slug shape so ordinary prose
# containing a slash ("and/or") cannot satisfy a citation requirement.
_NODE_KEY = re.compile(
    r"\b(?:world/knowledge/tree/)?[a-z0-9]+(?:-[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+(?:\.md)?\b")

# A slash-joined run of BARE NUMBERS is a ratio, not a citation -- "6/6 suites
# passed", "124/125", "20/40/80", "0/0/0". _NODE_KEY's segment class is
# [a-z0-9]+, which admits all-digit segments, so every pass-count in every
# closure note was being extracted as a node-key and then adjudicated: it names
# no file, no tree node and no URL, so it could only ever come back uncited.
#
# MEASURED before tightening (, guard-3086 -- a pattern specified from
# the few instances one author saw encodes those instances, not the defect):
# over the live world store, 3,033 goals / 1,584 outcome+progress notes yielding
# 11,507 node-key tokens, 2,055 of them (17.86%, 939 distinct) are this shape.
#
# THE EXCLUSION IS SAFE IN THE guard-1901 DIRECTION, which is the only reason it
# is done at extraction rather than by demoting to `unadjudicable`. Tightening an
# extractor weakens a negative assertion: a dropped token can take a cluster from
# a blocking finding to `if not checkable: continue` -- no finding at all. That
# is alarm suppression, and it is why the broader "require a hyphen or a known
# root" tightening was REJECTED here: it would also drop genuine unhyphenated
# node keys (guard-6054 tells authors to cite path-qualified keys, and 3,585
# path-ish-but-unresolved tokens were measured). An all-digit run is different in
# kind, not degree: no file path, tree-node key or URL can consist only of digits
# and slashes, so the excluded set provably contains no GENUINE citation.
#
# THAT LAST CLAUSE USED TO READ "and no negative assertion is weakened", AND THE
# MEASUREMENT FALSIFIED IT -- recorded here rather than quietly reworded, because
# the overclaim is the more instructive half. Diffing FINDINGS (not token counts;
# the risk is a lost alarm, so tokens are the wrong unit) pre/post across all
# 1,587 notes at retrieved=False -- the worst case for suppression -- gives
# 1,321 identical, 248 changed-kind, and 18 notes that lose a blocking finding
# entirely. The 248 are strict improvements: decorative-citation -> the honest
# missing-citation, since the ratio was the only thing being "cited".
#
# The 18 are NOT guard-1901 suppression, and the reason is measured, not argued:
# every one of the 32 affected clusters retains ONLY (goal-id x28, board-msg +
# goal-id x4) -- zero retain a url or node-key. `checkable` is url/node-key only,
# so `if not checkable: continue` already declines to adjudicate goal-id-only
# clusters CORPUS-WIDE. The phantom ratio was pulling these 18 OUT of that
# pre-existing policy and into a verdict that could only ever fail. Removing it
# returns them to the same treatment every other goal-id-only cluster gets. The
# alarm lost was never real; that is the defect this fix exists to remove.
#
# Whether goal-id-only clusters SHOULD escape adjudication is a separate,
# pre-existing question about `checkable` -- deliberately not touched here.
_RATIO_RUN = re.compile(r"^\d+(?:/\d+)+$")
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


# Third state for the ``retrieved`` predicate in :func:`analyze`. A file that
# was read with an offset/limit is recorded by context-reads.py behind
# PARTIAL_PREFIX and is DELIBERATELY excluded from read_tracker()'s full set
# ("a ranged peek is not evidence you read the claim's source"). That exclusion
# is correct and is NOT changed here -- a partial read still fails. What it
# could not express is the DIFFERENCE between "never opened" and "opened, in
# part", and the finding text asserted the former for both.
PARTIAL = "partial"


class Finding(NamedTuple):
    kind: str                # "missing-citation" | "decorative-citation"
                             # | "unadjudicable-citation" (advisory, )
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
    for kind, pat in (("board-msg", _BOARD_MSG), ("goal-id", _GOAL_ID),
                      ("rule-id", _RULE_ID)):
        for m in pat.finditer(masked):
            found.append((kind, m.group(0)))
    masked = _BOARD_MSG.sub(" ", masked)
    masked = _GOAL_ID.sub(" ", masked)
    # Masked BEFORE the node-key scan for the same reason as the two above: an
    # unmasked "guard-2024" is harmless to _NODE_KEY (which requires a slash),
    # but leaving it in place would make the ordering contract depend on that
    # accident rather than on the rule every other id kind follows.
    masked = _RULE_ID.sub(" ", masked)
    for m in _NODE_KEY.finditer(masked):
        tok = m.group(0)
        if _RATIO_RUN.match(tok):
            continue          # a pass-count, not a citation
        found.append(("node-key", tok))
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


def analyze(text: str, retrieved=None, expressible=None) -> list:
    """Findings for ``text``.

    ``retrieved`` is a predicate ``(kind, value) -> bool`` answering "was this
    cited thing actually retrieved in THIS session?" -- supplied by the caller so
    this module stays pure and testable with no manifest on disk. When it is
    None the decorative-citation check is SKIPPED rather than assumed-true: a
    check that cannot run must not manufacture a pass (guard-1760).

    ``expressible`` is an optional predicate ``(kind, value) -> bool`` answering a
    DIFFERENT question: "could the provenance manifest EVER have recorded this
    citation?" It exists because guard-1760's contrapositive binds just as hard as
    guard-1760 itself -- a check that could not run must not report a FAIL either,
    and calling an unrecordable citation `decorative-citation` asserts the session
    never fetched a source when the truth is that nothing was ever asked. This
    convention names the FALSE-POSITIVE rate as the binding constraint ("a lint
    that fires on ordinary writes gets switched off"), and that is exactly the rate
    this raises. Measured 2026-09-05: 9,727 of 12,657 git-tracked files (76.9%) sit
    outside the manifest's advisory scope and can never clear the check, .claude/
    rules/*.md among them -- a lower bound, since product repos and most of world/
    are not in the repo at all (g-115-9059).

    It is OPTIONAL and DEFAULTS TO EXPRESSIBLE, which is the fail-safe direction:
    passing nothing preserves the old behaviour exactly, and a citation is demoted
    only when the caller can POSITIVELY show the manifest could never hold it.
    Demoting on doubt would suppress alarms, which is the one direction this gate
    must never fail in.
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
        if not checkable:
            continue
        verdicts = [retrieved(k, v) for k, v in checkable]
        # PARTIAL IS A TRUTHY STRING, so it MUST be excluded before any
        # truthiness test -- a bare `any(verdicts)` reads it as a full
        # retrieval and silently passes a cluster whose source was only
        # peeked at. That is the ALARM-suppressing direction (guard-1760),
        # which is the one this split must never fail in.
        if any(v is not PARTIAL and v for v in verdicts):
            continue
        cited = ", ".join(v for _, v in checkable[:3])
        if any(v is PARTIAL for v in verdicts):
            # Same KIND and same severity -- the verdict is unchanged and no
            # consumer branches on this text. What changes is that the message
            # stops asserting something FALSE: the session DID retrieve this
            # file, just not in full, and a reader told "NOT retrieved this
            # session" goes looking for a read that already happened. The two
            # texts mirror the pre-edit-context-gate advisories that
            # read-before-edit.md Rule 4 already distinguishes ("has not been
            # Read this session" vs "was Read only in part this session
            # (ranged read)"), so the vocabulary is the framework's, not new.
            findings.append(Finding(
                "decorative-citation", cl.start_line, cl.end_line,
                f"cited but retrieved ONLY IN PART this session (ranged read): "
                f"{cited}. A ranged peek is not evidence for the claim -- "
                "re-read the region that supports it. Same severity as "
                "uncited; the difference is what to DO about it.", sample))
        elif expressible is not None and not any(
                expressible(k, v) for k, v in checkable):
            # THE THIRD VERDICT (). Not a softer decorative-citation --
            # a different QUESTION. `decorative-citation` asserts the session never
            # fetched the source; that assertion is only available when the manifest
            # COULD have held the answer. Where it structurally could not, nothing
            # was adjudicated, and saying "NOT retrieved" states as measured fact
            # something never measured.
            # NOTE THE ORDER: this branch sits AFTER the PARTIAL branch on purpose.
            # A ranged read is EXPRESSIBLE and WAS expressed -- the manifest holds a
            # #partial row -- so the check RAN and answered "only in part". That is
            # an answer, not a silence, and it keeps its original severity. Only
            # genuine silence is demoted here.
            findings.append(Finding(
                "unadjudicable-citation", cl.start_line, cl.end_line,
                f"cited, and NOT ADJUDICABLE from the provenance manifest: {cited}. "
                "The manifest structurally cannot record this citation class, so "
                "this is NOT evidence the source went unread -- the check could not "
                "run. Advisory: it does NOT fail the verdict (guard-1760 read in "
                "the FAIL direction; g-115-9059).", sample))
        else:
            findings.append(Finding(
                "decorative-citation", cl.start_line, cl.end_line,
                f"cited but NOT retrieved this session: {cited}. A citation the "
                "session never fetched is DECORATIVE -- same severity as none "
                "(g-357-43 provenance manifest).", sample))
    return findings
