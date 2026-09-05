#!/usr/bin/env python3
"""Q4 — close-time entity-fact provenance sampling + direction fidelity ().

USER DIRECTIVE 2026-08-31 ("double-check everything against sources before
use"). This is the CLOSE-time layer of the DoD spine; its siblings are the
g-357-40 gate, the g-357-41 reviewer, the g-357-42 fixture and the g-357-45
WRITE-time gate. Two capabilities, one module, because both answer "does this
claim actually rest on its cited source?" and both are consumed by the same two
callers (aspirations-verify Q4, close-review Step 3/4):

  1. SAMPLE  — pick entity-bearing claims out of a produced artifact by a rule
     the executor does not control, and resolve each cluster's citations
     against the session provenance manifest.
  2. DIRECTION — the mechanical half of "citations must MATCH, not merely
     exist": a claim asserting A -> B where its source asserts B -> A.

WHY THE SAMPLING IS SCRIPTED (the goal's own words: "script-gated sample
selection so the executor cannot cherry-pick"). An executor asked to "check a
few claims" checks the few it already knows are cited. Sampling here is a
sha256 over (goal_id, artifact path, cluster text), sorted ascending — so it is
DETERMINISTIC (same artifact, same sample, every run and every reviewer),
REPRODUCIBLE by anyone holding the artifact, and NOT re-rollable: the only way
to change which claims are sampled is to change the artifact's text.

THE MANIFEST'S SCOPE IS NARROWER THAN THE UNIVERSE OF CITABLE PATHS, so the
decorative test has a STRUCTURAL blind spot and it fails in the ALARM direction.
`context-reads.is_in_scope` tracks only some path classes; measured 2026-09-03 on
this repo with absolute paths (a relative path answers False for everything, which
is a probe bug, not a scope fact -- positive-control with a path you KNOW was
recorded before trusting any answer here):

    .claude/skills/**       True        core/config/**          True
    core/scripts/**         advisory-only
    .claude/rules/**        False       agents/**               False

A Read of an OUT-OF-SCOPE file is never recorded, so a citation to such a path is
reported `decorative-citation` however genuinely it was fetched. Found by running
this check against its own goal's closure note, which cited
`agents/<agent>/sessions/<sid>/body-context-reads.txt` -- a file that had just been
opened with the Read tool -- and was flagged anyway. Do NOT read a decorative
finding on an `agents/**` or `.claude/rules/**` citation as evidence of anything;
widening the manifest, or teaching this module which path classes are
unverifiable, is a design change and is deliberately NOT done here.

A SECOND ALARM-DIRECTION LIMIT, same lineage: the analyzer cannot tell a path named
as the SUBJECT of a sentence from one cited as its SOURCE. A sentence ABOUT a file
-- even one asserting the file does not exist -- carries a path-shaped token inside
an entity-bearing cluster and so reads as a citation.

WHAT THE SAMPLER DOES NOT COVER, stated because a check whose limits are
unstated gets read as total (guard-1760, guard-3489). It removes CLAIM-level
cherry-picking. It does not remove ARTIFACT-level cherry-picking: the caller
supplies the artifact paths, so a caller that names a clean file gets a clean
sample. The coverage counts in the result (`artifacts_read`, `artifacts_missing`,
`clusters_total`) exist so that substitution is visible to the reader rather
than silent.

WHY DIRECTION IS A SEPARATE CHECK FROM `source_fidelity`, and not a widening of
it. `close-review-verdict.source_fidelity` diffs `goal_close_risk_tier
.named_entities`, which is deliberately NARROW — id-shaped tokens (g-NNN,
guard-NNN, shas), with a standing comment that widening it would push ordinary
prose into tier 2 and "make the gate the thing people route around". MEASURED on
the goal's own fixture: for the claim "Miami sent the first-round pick to Denver"
against the source "Denver sent ... to Miami", `named_entities` returns the EMPTY
SET for BOTH sides, `passed` is True, and `build_verdict(approve=True)` returns
APPROVE. So the existing check is not merely tied on this input — it is blind to
it, by design. This module therefore carries its OWN prose-entity notion, uses it
ONLY for the direction comparison, and never feeds the tier classifier. Tiering
stays the cost control it was built to be.

UNDER-FLAG BY CONTRACT, inherited from ground_truth_citation: a check that cried
wolf would be switched off, and a check that is off catches nothing. Every
heuristic here prefers a miss to a false alarm, and the known misses are pinned
by tests rather than described in prose.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Callable, Optional

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ground_truth_citation import analyze, iter_clusters  # noqa: E402

DEFAULT_SAMPLE_N = 5

# ─── direction fidelity ──────────────────────────────────────────────────────
# A closed family of verbs whose subject->object direction is unambiguous when
# the object is introduced by "to". "sold" and "bought" are deliberately ABSENT:
# "A sold X to B" and "B bought X from A" describe the same transfer in opposite
# syntactic directions, so admitting them would manufacture contradictions out
# of correct paraphrase.
_DIRECTIONAL_VERB = re.compile(
    r"\b(sent|sends|send|traded|trades|trade|gave|gives|give|paid|pays|pay"
    r"|transferred|transfers|transfer|shipped|ships|ship|awarded|awards|award"
    r"|delivered|delivers|deliver|passed|passes|pass)\b", re.IGNORECASE)

# A prose entity for the DIRECTION check only: a capitalized token that is not a
# sentence-opening function word. Narrow on purpose — a pair only counts when it
# appears in BOTH texts, which is what keeps this from firing on ordinary prose.
_CAP_TOKEN = re.compile(r"\b[A-Z][A-Za-z]{2,}\b")
_STOPWORDS = frozenset("""
The This That These Those There Then Their They Them It Its And But For Nor Yet
So Because Although Though While When Where Which Who Whom Whose What Why How
After Before During Since Until Unless If Else Also However Moreover Therefore
Thus Hence Meanwhile Both Either Neither Each Every All Any Some None One Two
Three Four First Second Third Fourth Note Read Per See Use Using Given With
Without From Into Onto Over Under Above Below About Against Between Among
""".split())


def _entities(text: str) -> list:
    return [m.group(0) for m in _CAP_TOKEN.finditer(text)
            if m.group(0) not in _STOPWORDS]


def directed_pairs(text: str) -> set:
    """{(giver, receiver)} for every "<A> ... <verb> ... to <B>" in ``text``.

    Sentence-scoped so a pair can never be assembled across a full stop. The
    giver is the LAST qualifying entity before the verb (the nearest subject);
    the receiver is the FIRST qualifying entity after a following " to ".
    Case-normalised, because an identity check must not read a capitalisation
    difference as a different party.

    A SOFT WRAP IS NOT A SENTENCE BOUNDARY, and getting that wrong made this
    function silently blind rather than wrong-in-a-visible-way. The first
    version split on ``\\n`` as well as on sentence punctuation; real markdown
    wraps prose mid-sentence, so the fixture's own claim — "Miami sent the
    first-round pick\\nto Denver" — put the verb in one fragment and its "to
    <B>" in the next, and BOTH pair sets came back EMPTY. The end-to-end run
    through close-review-verdict.py returned APPROVE on the reversed claim while
    every unit-level smoke test on single-line strings passed. Paragraph breaks
    (blank lines) and sentence punctuation break; a lone newline is whitespace.
    """
    pairs = set()
    paragraphs = re.split(r"\n\s*\n", text or "")
    sentences = [s for para in paragraphs
                 for s in re.split(r"(?<=[.!?;])\s+", re.sub(r"\s+", " ", para))]
    for sentence in sentences:
        for vm in _DIRECTIONAL_VERB.finditer(sentence):
            before = _entities(sentence[:vm.start()])
            if not before:
                continue
            tail = sentence[vm.end():]
            tm = re.search(r"\bto\b", tail, re.IGNORECASE)
            if not tm:
                continue
            after = _entities(tail[tm.end():])
            if not after:
                continue
            giver, receiver = before[-1].lower(), after[0].lower()
            if giver != receiver:
                pairs.add((giver, receiver))
    return pairs


def direction_contradictions(claim_text: str, source_text: str) -> list:
    """Pairs the claim asserts as A->B while the source asserts B->A.

    Returns [{"claim": [a, b], "source": [b, a]}]. Empty when the source is
    silent about the pair — SILENCE IS NOT CONTRADICTION, and conflating them
    would flag every claim whose source phrases the relation differently.
    """
    claim_pairs = directed_pairs(claim_text)
    source_pairs = directed_pairs(source_text)
    out = []
    for a, b in sorted(claim_pairs):
        if (b, a) in source_pairs and (a, b) not in source_pairs:
            out.append({"claim": [a, b], "source": [b, a]})
    return out


def direction_fidelity(source_text: str, artifact_text: str) -> dict:
    """Direction check in the shape `close-review-verdict.source_fidelity` uses.

    `passed` is False only on a positive contradiction, never on absence of
    evidence — so this can veto an approval but can never grant one, matching
    the asymmetry the verdict producer already enforces (guard-2564).
    """
    contradictions = direction_contradictions(artifact_text, source_text)
    return {
        "claim_pairs": sorted(list(p) for p in directed_pairs(artifact_text)),
        "source_pairs": sorted(list(p) for p in directed_pairs(source_text)),
        "contradictions": contradictions,
        "passed": not contradictions,
    }


def direction_findings(fid: dict) -> list:
    """Human-readable findings, quoting both directions verbatim.

    Both directions rather than a count, for the reason `fidelity_findings`
    gives: the founding incident was concealed by a number.
    """
    out = []
    for c in fid.get("contradictions") or []:
        a, b = c["claim"]
        out.append(
            f"direction-fidelity: the artifact asserts {a} -> {b} while the cited "
            f"source asserts {b} -> {a}. The citation EXISTS and the entity set "
            f"MATCHES, so a citations-exist check passes; the claim is backwards.")
    return out


# ─── provenance sampling ─────────────────────────────────────────────────────

def sample_key(goal_id: str, artifact: str, cluster_text: str) -> str:
    """The deterministic sort key. Separator is NUL so no field can impersonate
    the boundary between two fields."""
    blob = "\x00".join((goal_id or "", artifact or "", cluster_text or ""))
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def sample_clusters(text: str, goal_id: str, artifact: str, n: int = DEFAULT_SAMPLE_N):
    """Up to ``n`` clusters from ``text``, chosen by ascending sample_key.

    Returns (sampled, total). ``total`` is reported separately and is NOT
    len(sampled): a caller that printed only the sampled count would hide how
    much of the artifact went unexamined (guard-3489 — a clean verdict must
    carry the coverage it is clean over).
    """
    clusters = list(iter_clusters(text or ""))
    keyed = [(sample_key(goal_id, artifact, "\n".join(f.text for f in c.fact_lines)), c)
             for c in clusters]
    keyed.sort(key=lambda kc: kc[0])
    return [c for _k, c in keyed[:max(0, int(n))]], len(clusters)


def retrieved_predicate(session_id: Optional[str]) -> Optional[Callable]:
    """(kind, value) -> retrieved this session? None when unanswerable.

    THE session_id IS LOAD-BEARING AND MUST BE PASSED (measured 2026-09-03,
    alpha worker Body on cc-07). `context-reads.tracker_path` routes to the
    per-Body tracker `sessions/<sid>/body-context-reads.txt` when that Body has
    a forked body-WM file, and to the agent-wide `session/context-reads.txt`
    otherwise. On a worker Body the agent-wide file may not exist at all.
    Same-turn positive control on that box: the value "framework-verification",
    written by that session's own retrieve.sh, answered rc=1 without
    --session-id and rc=0 with it. A Q4 that omitted it would report every
    citation on every worker Body as unretrieved — a check that is wrong in the
    ALARM direction, which is how a check gets switched off.

    Returns None — not a permissive lambda — when the manifest is unreadable or
    empty, so `analyze` SKIPS the decorative test instead of manufacturing a pass
    (guard-1760).
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_ctx_reads_for_q4", SCRIPTS / "context-reads.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                      # type: ignore
        entries = mod.read_provenance(session_id=session_id) or []
        # BOTH halves of the tracker, or file citations can never pass.
        # read_provenance() yields ONLY the `#prov:` retrieval-QUERY lines;
        # the paths the session actually opened live in read_tracker(). The
        # first cut consulted provenance alone, so every FILE citation came
        # back `decorative-citation` however genuinely it had been Read --
        # wrong in the ALARM direction. Caught by running this check against
        # its own goal's closure note, which cited a SKILL.md that had been
        # opened with the Read tool minutes earlier (). The
        # positive control that hid it: the first probe used a retrieval
        # QUERY string, which lives in the half that was being read.
        # read_tracker() is the FULLY-read set -- ranged peeks are excluded
        # by its own contract, and that is the right bar here: peeking at
        # one region of a file is not evidence you read the claim's source.
        paths = mod.read_tracker(session_id=session_id) or set()
    except Exception:
        return None
    values = [str(e[2] if isinstance(e, (tuple, list)) and len(e) >= 3 else e)
              for e in entries]
    values += [str(p) for p in paths]
    values = [v for v in values if v]
    if not values:
        return None

    def _retrieved(kind, value):
        v = str(value).rstrip("/.,);")
        return any(v in got or got in v for got in values)
    return _retrieved


def run(goal_id: str, artifacts, n: int = DEFAULT_SAMPLE_N,
        session_id: Optional[str] = None, source_text: Optional[str] = None) -> dict:
    """Sample each artifact and resolve the sampled clusters' citations.

    Verdicts:
      pass    — every sampled cluster carried a source token the session fetched
                (and no direction contradiction, when a source was supplied).
      fail    — at least one sampled cluster is uncited or decoratively cited,
                or a sampled claim reverses its source.
      skipped — nothing checkable. `skip_reason` always says WHICH, because
                "skipped" and "pass" are the two answers most easily confused
                and only one of them is evidence.
    """
    retrieved = retrieved_predicate(session_id)
    result = {
        "goal_id": goal_id,
        "session_id": session_id,
        "sample_n": int(n),
        "artifacts_read": [],
        "artifacts_missing": [],
        "clusters_total": 0,
        "sampled_count": 0,
        "provenance_manifest": "readable" if retrieved else "unreadable-or-empty",
        "findings": [],
        "direction": None,
        "verdict": "skipped",
        "skip_reason": None,
    }
    texts = []
    for a in artifacts or []:
        p = Path(a)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            result["artifacts_missing"].append(str(a))
            continue
        result["artifacts_read"].append(str(a))
        texts.append(text)
        lines = text.splitlines()
        sampled, total = sample_clusters(text, goal_id, str(a), n)
        result["clusters_total"] += total
        result["sampled_count"] += len(sampled)
        for cl in sampled:
            # THE WHOLE CLUSTER, not just its fact lines. `ground_truth_citation`
            # collects source tokens over the ENTIRE contiguous run — that is what
            # lets a citation sit on the line after the claim it supports, which is
            # how prose is actually written. Reconstructing the blob from
            # `fact_lines` alone discards exactly those citations, and every such
            # cluster then reads as `missing-citation`. Measured on this module's
            # own smoke fixture: a claim whose URL wrapped onto the next line was
            # reported uncited. A check that is wrong in the ALARM direction is how
            # a check gets switched off, so this slice is load-bearing, not tidiness.
            blob = "\n".join(lines[cl.start_line - 1:cl.end_line])
            for f in analyze(blob, retrieved=retrieved):
                result["findings"].append({
                    "artifact": str(a), "kind": f.kind,
                    "start_line": cl.start_line, "end_line": cl.end_line,
                    "detail": f.detail, "sample": f.sample,
                })
            if source_text:
                for c in direction_contradictions(blob, source_text):
                    result["findings"].append({
                        "artifact": str(a), "kind": "direction-contradiction",
                        "start_line": cl.start_line, "end_line": cl.end_line,
                        "detail": direction_findings({"contradictions": [c]})[0],
                        "sample": cl.fact_lines[0].text.strip()[:120],
                    })
    if source_text:
        # Reuse the text already read, never a second read_text: a file that
        # vanishes between the two passes would raise OSError out of a function
        # whose whole contract is to return a verdict.
        result["direction"] = direction_fidelity(source_text, "\n".join(texts))

    if result["findings"]:
        result["verdict"] = "fail"
    elif result["sampled_count"] == 0:
        result["verdict"] = "skipped"
        result["skip_reason"] = (
            "no entity-bearing fact clusters in the artifact(s)"
            if result["artifacts_read"] else
            "no artifact could be read")
    elif retrieved is None:
        result["verdict"] = "skipped"
        result["skip_reason"] = (
            "provenance manifest unreadable or empty — the decorative-citation "
            "test could not run, so this is NOT a pass (guard-1760). Note that "
            "reads performed with `cat` in a Bash call are invisible to the "
            "manifest by construction (guard-4407).")
    else:
        result["verdict"] = "pass"
    return result
