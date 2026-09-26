"""Defer self-artifact check — does a defer wait on the goal's OWN unlanded work?
(g-353-109)

THE HOLE. Every written-record gate that routes work away asks one question —
"is this being handed to a HUMAN?" (participants:[user] and defer_reason through
capability-gate, outbound email through /notify-user Step 1.5). A defer that hands
the work to NOBODY — to an impersonal "external signal" — passes all of them and
freezes the goal anyway. Measured 2026-09-15 on the g-373-12 defer:
capability-gate.py returned would_block=False at intended-participants agent AND
hybrid, True only at user, although its own matcher had extracted
merge/commit/branch (14 matches). That refusal is conditioned on the routing
TARGET, never on the ACTION.

THE CANONICAL INCIDENT (g-373-12, HIGH). A `precondition_unmet:` defer read
"PR #528 ... is open and unmerged ... Neither gate is agent-clearable right now:
the merge is the named external signal." PR #528 was the goal's OWN artifact
(head branch g-373-12-finding-through-vessel), open, non-draft, mergeable and
clean into its integration branch with no required checks. The only actor that
would ever merge it was the goal's own execution — which the defer suspended.
A deadlock by construction: every re-probe correctly answered "still unmerged".

WHAT THIS REFUSES (g-353-109 outcome 1). A defer is refused when BOTH hold, in
the defer's own clause:

  OWNERSHIP  the clause names an artifact that is this goal's own, by one of:
     lane 1  an artifact token carrying the goal id IN THE DEFER — a branch
             (named as one: "branch g-373-12-x", or path-shaped "fix/g-373-12-x")
             or a conventional-commit scope ("fix(g-373-12):");
     lane 2  a PR number or commit sha that THE GOAL'S OWN RECORD binds, inside
             one clause, to such a token. The canonical incident needs this lane:
             its defer says only "PR #528"; the goal's progress_note said
             "PR #528 open (<repo>, base dev, branch g-373-12-finding-through-vessel,
             commit c8c0a23)".
  UNLANDED   the same clause says that artifact has NOT landed: open, unmerged,
             not yet merged/deployed/on main, to be merged, awaiting merge, draft.

WHY BOTH, MEASURED — the goal filing called the ownership match alone
"zero-judgement, no false-positive surface". That was falsified on the live
corpus before this shipped (2026-09-26, cc-08: 110 non-terminal deferred goals,
110 carrying a structured prefix). Ownership alone refused 7, and all 7 were
correct defers:
  * 4 of 7 were "<goal-id>-<slug>" tokens used as progress_note MARKERS and
    verification.preconditions PREDICATE ids (g-370-63-dev-row-window). The same
    naming convention that makes a branch self-identifying names every marker,
    so a token only counts when an artifact noun governs it (lane 1's "named as
    one" rule), it is path-shaped, or it is a conventional-commit scope.
  * 3 of 7 named their own PR while RECOUNTING that it had already landed
    ("PR #523 merged 4bf785a4, deploy run ... success") and waited on something
    genuinely external (a launch, customer sessions, a window). A well-written
    defer cites its own shipped artifact as provenance — the guard-3882
    gradient again. The UNLANDED clause test separates the deadlock ("PR #528 is
    open and unmerged") from provenance ("PR #523 merged").

WHY NO LOOKUP. Resolving "PR #528" through the code host would need the repo
(absent from the defer), credentials and a network round-trip at write time — a
new failure mode inside a durable-write path. The goal record already carries
the binding, offline and deterministic.

DESIGN CONSTRAINTS, inherited from the sibling gates rather than rediscovered:

1. Trigger on the FIELD, never on `is_narrative_defer`: that predicate is False
   for every STRUCTURED_DEFER_PREFIXES value, and every live defer measured
   above carried one — the canonical defer included (`precondition_unmet:`).
2. One override — `--force-defer` / `X-Mind-Force-Defer`, the same one the
   capability and routing-target gates honour. A private bypass teaches callers
   to reach for whichever is nearest.
3. No Layer-D auto-filing. The goal already owns the work; an "Unblock:" goal
   for its own artifact would duplicate it at HIGH (guard-3505's cost).
4. Clause-scoped: a clause ends at a sentence terminator (an ellipsis is NOT
   one — quoted defers elide with "...") or a blank line, and lane 2's binding
   spans at most BIND_WINDOW characters. Wider matching lets an unrelated
   sentence decide (the lane-A lesson in the defer-routing-target registry row).
5. Decision before prose (guard-3803); every failure path is a no-op ALLOW
   (guard-142). No env reads and no paths: the caller passes the record in.

THE GRANT-VERB QUESTION (g-353-109 outcome 4) — decided: ADVISE, never refuse.
When the defer's text names a standing-grant action (merge / push / deploy /
release / roll back) and cites no probe, append an advisory asking WHY NOT NOW.
Refusing on the verb was rejected: guard-3882's gradient — the best-probed
defers quote command output, which is where those verbs live, so a verb refusal
lands hardest on the most diligent authors. Refusing on "verb without probe"
was rejected too: whether a probe is cited is a PROSE judgement, and a prose
refusal is the guard-1470 false-positive shape. The advisory inverts the
gradient the right way — a cited probe silences it — and costs nothing to
ignore. Measured on the same 110 with the shipped predicate: it advises on 11
(10%); the ownership-only draft advised on 9 (g-353-109 sweeps). The
probe-citation vocabulary is imported from gates.defer_routing_target, so the
two gates cannot disagree about what counts as a probe.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

__all__ = [
    "evaluate",
    "own_artifact_tokens",
    "external_refs",
    "record_bindings",
    "REFUSAL_PREFIX",
    "ADVISORY_PREFIX",
    "NARRATIVE_FIELDS",
    "BIND_WINDOW",
]

REFUSAL_PREFIX = "[defer-self-artifact] REFUSED"
ADVISORY_PREFIX = "[defer-self-artifact] ADVISORY"

# The goal's own account of its work. Enumerated, not "every string field":
# verification criteria and titles describe what SHOULD exist, not what did.
NARRATIVE_FIELDS = ("progress_note", "outcome_note", "outcome_notes", "description")
BIND_WINDOW = 160

_GOAL_ID_RE = re.compile(r"^g-\d{3}-\d{1,5}$")
_CC_TYPES = r"(?:feat|fix|chore|docs|refactor|test|tests|perf|build|ci|revert|style)"
# A PR ref: "PR #528", "PR 528", "pull request #528", ".../pull/528", or a bare
# "#528" (two digits minimum, so list ranks like "#1" never count).
_PR_RE = re.compile(
    r"(?:\bPR\s*#?|\bpull request\s*#?|/pull/|(?<![\w&#])#)(\d{2,6})\b", re.I)
# A commit sha: 7-40 hex with at least one letter AND one digit, so neither a
# plain number nor an all-letter word ("deadbeef", "facade") can pass as one.
_SHA_RE = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![0-9A-Za-z])")
# A clause ends at a lone terminator + whitespace or at a blank line. Periods
# inside an ellipsis are not terminators: quoted defers elide with "...".
_CLAUSE_BREAK_RE = re.compile(r"\n\s*\n|(?<!\.)[.;!?](?!\.)(?=\s|$)")
_ID_LIST_PREFIX_RE = re.compile(r"(?:(?:g|asp)-\d{3}(?:-\d+)?[/,_-])+")
# The artifact noun must directly govern a hyphen-suffixed token; markers and
# predicate ids share the "<id>-<slug>" shape and are never named this way.
_ARTIFACT_NOUN_RE = re.compile(
    r"\b(?:branch(?:es)?|head|worktree|checkout|refs/heads)[\s:`'\"(]*$", re.I)
_UNLANDED_RE = re.compile(
    r"\b(?:is|are|still|remains?)\s+open\b|\bopen and\b|\bunmerged\b|\bunlanded\b"
    r"|\bundeployed\b|\bunpushed\b|\bdraft\b|\bnot an ancestor\b"
    r"|\bnot\s+(?:yet\s+)?(?:merged|landed|deployed|released|pushed"
    r"|on\s+(?:main|master|dev))\b"
    r"|\bto\s+be\s+(?:merged|landed|deployed|released|pushed)\b"
    r"|\b(?:awaiting|pending)\s+(?:the\s+)?(?:merge|review|approval|deploy\w*"
    r"|release|push)\b"
    r"|\buntil\b[^.;\n]{0,80}\b(?:merges|lands|is merged|deploys)\b", re.I)
_GRANT_VERB_RE = re.compile(
    r"\b(merge|merges|merging|push|pushes|pushing|deploy|deploys|deploying|"
    r"release|releases|releasing|roll ?back|rolls back|rolling back)\b", re.I)


def _empty(skip_reason: Optional[str] = None) -> Dict[str, Any]:
    return {
        "refuse": False,
        "reason": None,
        "artifacts": [],
        "bindings": [],
        "advisories": [],
        "checked": skip_reason is None,
        "skip_reason": skip_reason,
    }


def _record_text(goal_record: Any) -> List[tuple]:
    """(field, text) for each narrative field present, lists and dicts flattened."""
    out = []
    if not isinstance(goal_record, dict):
        return out
    for field in NARRATIVE_FIELDS:
        v = goal_record.get(field)
        if isinstance(v, (list, tuple)):
            v = "\n\n".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = "\n\n".join(str(x) for x in v.values())
        if isinstance(v, str) and v.strip():
            out.append((field, v))
    return out


def _clause(text: str, start: int, end: int) -> str:
    """The clause of `text` containing [start, end)."""
    left = 0
    for m in _CLAUSE_BREAK_RE.finditer(text, 0, start):
        left = m.end()
    m = _CLAUSE_BREAK_RE.search(text, end)
    return text[left:(m.start() if m else len(text))]


def _token_matches(goal_id: str, text: str) -> List[tuple]:
    """(start, end, token) for every ARTIFACT-shaped token in `text` carrying
    goal_id: a conventional-commit scope, a path-shaped branch, or a
    hyphen-suffixed token that an artifact noun directly governs."""
    gid = re.escape(goal_id)
    found = []
    branch_re = re.compile(
        r"(?<![A-Za-z0-9._/-])"
        r"(?P<pre>[A-Za-z][A-Za-z0-9._-]*[/_-])?"
        + gid +
        r"(?![0-9])"
        r"(?P<suf>[-_.][A-Za-z][A-Za-z0-9._/-]*|/[A-Za-z](?!-\d)[A-Za-z0-9._/-]*)?")
    for m in branch_re.finditer(text):
        pre, suf = m.group("pre"), m.group("suf")
        if pre and _ID_LIST_PREFIX_RE.fullmatch(pre):
            pre = None  # "/" is an id list, not a branch
        if not pre and not suf:
            continue  # a bare id is prose
        start = m.start() + (0 if pre else len(m.group("pre") or ""))
        path_shaped = bool(pre and pre.endswith("/"))
        if not path_shaped and not _ARTIFACT_NOUN_RE.search(text[max(0, start - 48):start]):
            continue  # a marker or predicate id, not an artifact
        token = ((pre or "") + goal_id + (suf or "")).rstrip("._/-")
        found.append((start, m.end(), token))
    cc_re = re.compile(
        r"\b" + _CC_TYPES + r"\((?:[^()\n]*?[,\s/])?" + gid + r"(?![0-9])[^()\n]*\)!?:")
    for m in cc_re.finditer(text):
        found.append((m.start(), m.end(), m.group(0)))
    return sorted(found)


def own_artifact_tokens(goal_id: str, text: Any) -> List[str]:
    """Artifact tokens in `text` that carry `goal_id` (deduplicated)."""
    seen: List[str] = []
    for _s, _e, tok in _token_matches(goal_id, str(text or "")):
        if tok not in seen:
            seen.append(tok)
    return seen


def external_refs(text: Any) -> Dict[str, List[str]]:
    """PR numbers and commit shas a defer names."""
    body = str(text or "")
    prs = sorted({m.group(1) for m in _PR_RE.finditer(body)}, key=int)
    shas = sorted({m.group(0).lower() for m in _SHA_RE.finditer(body)})
    return {"prs": prs, "shas": shas}


def _gap_is_one_clause(text: str, a_end: int, b_start: int) -> bool:
    gap = text[a_end:b_start]
    return len(gap) <= BIND_WINDOW and not _CLAUSE_BREAK_RE.search(gap)


def record_bindings(goal_id: str, refs: Dict[str, List[str]],
                    goal_record: Any) -> List[Dict[str, str]]:
    """Refs the goal's own record binds, inside one clause, to one of its own
    artifact tokens: the goal saying "that PR / commit is mine"."""
    prs = set(refs.get("prs") or [])
    shas = [s.lower() for s in (refs.get("shas") or [])]
    if not prs and not shas:
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for field, text in _record_text(goal_record):
        tokens = _token_matches(goal_id, text)
        if not tokens:
            continue
        hits = []
        for m in _PR_RE.finditer(text):
            if m.group(1) in prs:
                hits.append((m.start(), m.end(), "PR #" + m.group(1), m.group(1)))
        for m in _SHA_RE.finditer(text):
            val = m.group(0).lower()
            for s in shas:
                if val.startswith(s) or s.startswith(val):
                    hits.append((m.start(), m.end(), "commit " + val, s))
        for hs, he, label, key in hits:
            for ts, te, tok in tokens:
                if ts < he and hs < te:
                    continue  # overlapping spans are one token, not a binding
                ok = (_gap_is_one_clause(text, te, hs) if te <= hs
                      else _gap_is_one_clause(text, he, ts))
                if not ok or (key, tok) in seen:
                    continue
                seen.add((key, tok))
                lo, hi = min(hs, ts), max(he, te)
                out.append({"ref": label, "key": key, "token": tok, "field": field,
                            "clause": " ".join(text[lo:hi].split())[:240]})
    return out


def _probe_citation_re():
    try:
        from .defer_routing_target import _PROBE_CITATION_RE
        return _PROBE_CITATION_RE
    except Exception:
        return None


def evaluate(goal_id: str, text: Any, *, goal_record: Any = None) -> Dict[str, Any]:
    """Refuse a defer whose clearing event is the goal's OWN unlanded artifact.

    ALWAYS returns the same shape; every failure path is a no-op ALLOW.

        {"refuse": bool, "reason": str|None,
         "artifacts": [{token, clause}], "bindings": [{ref, token, field,
         clause, defer_clause}], "advisories": [str],
         "checked": bool, "skip_reason": str|None}

    `goal_record` is the goal's stored record (lane 2 reads only
    NARRATIVE_FIELDS). Without it lane 2 is skipped, never guessed.
    """
    try:
        body = str(text or "")
        if not body.strip():
            return _empty("empty defer_reason")
        if not isinstance(goal_id, str) or not _GOAL_ID_RE.match(goal_id):
            return _empty("goal id is not g-NNN-NN shaped")
        result = _empty()
        for s, e, tok in _token_matches(goal_id, body):
            clause = _clause(body, s, e)
            if _UNLANDED_RE.search(clause):
                result["artifacts"].append(
                    {"token": tok, "clause": " ".join(clause.split())[:240]})
        if goal_record is None:
            result["skip_reason"] = "no goal record supplied; lane 2 skipped"
        else:
            bound = {b["key"]: b for b in record_bindings(
                goal_id, external_refs(body), goal_record)}
            for m in list(_PR_RE.finditer(body)) + list(_SHA_RE.finditer(body)):
                key = m.group(1) if m.re is _PR_RE else m.group(0).lower()
                b = bound.get(key)
                if b is None or any(x["key"] == key for x in result["bindings"]):
                    continue
                clause = _clause(body, m.start(), m.end())
                if _UNLANDED_RE.search(clause):
                    result["bindings"].append(
                        dict(b, defer_clause=" ".join(clause.split())[:240]))
        # Decision first, prose second (guard-3803).
        result["refuse"] = bool(result["artifacts"] or result["bindings"])
    except Exception:
        return _empty("self-artifact check failed internally; allowed")

    if result["refuse"]:
        try:
            lines = [REFUSAL_PREFIX + " " + goal_id + ": this defer waits on the "
                     "goal's OWN unlanded artifact, and the goal's own execution "
                     "is the only actor that lands it. The defer suspends that "
                     "execution, so every re-probe will answer 'not yet' forever "
                     "— a deadlock by construction (g-373-12, 2026-09-15)."]
            for a in result["artifacts"]:
                lines.append("  - the defer names " + a["token"] + ", which "
                             "carries " + goal_id + ", as unlanded: \""
                             + a["clause"] + "\"")
            for b in result["bindings"]:
                lines.append("  - the defer names " + b["ref"] + " as unlanded (\""
                             + b["defer_clause"] + "\"), and this goal's own "
                             + b["field"] + " ties it to " + b["token"] + ": \""
                             + b["clause"] + "\"")
            lines.append("Two legal responses: (1) LAND IT — merge, push or "
                         "deploy your own artifact now (standing grants cover a "
                         "clean non-draft PR; see capability-routing.md), then "
                         "write no defer; or (2) STATE WHY YOU CANNOT — re-send "
                         "with --force-defer \"<why this artifact cannot land "
                         "now>\", the same override the capability gate uses.")
            result["reason"] = "\n".join(lines)
        except Exception:
            result["reason"] = (REFUSAL_PREFIX + " " + goal_id
                                + ": the defer waits on the goal's own artifact.")
        return result

    try:
        probe_re = _probe_citation_re()
        verb = _GRANT_VERB_RE.search(body)
        if verb and probe_re is not None and not probe_re.search(body):
            result["advisories"].append(
                ADVISORY_PREFIX + " " + goal_id + ": this defer's clearing event "
                "names a standing-grant action ('" + verb.group(0) + "') and "
                "cites no probe. probe-before-defer rule 1 asks for the probe "
                "before the defer: cite its output, or say WHY NOT NOW. NOT "
                "refused — the defer still applies.")
    except Exception:
        pass
    return result
