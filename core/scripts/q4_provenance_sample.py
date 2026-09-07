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

from ground_truth_citation import PARTIAL, analyze, iter_clusters  # noqa: E402

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


def added_line_numbers(diff_text: str) -> set:
    """NEW-side line numbers that a unified diff ADDS. Pure; no git, no I/O.

    Split out from the CLI on purpose: the git invocation is environment, the
    hunk arithmetic is the part that can be wrong, and only the second half is
    worth pinning with fixtures.

    Counts ONLY '+' lines. A context or removed line is not something this goal
    authored, and the whole point of the scope is "lines this unit produced".
    """
    added, new_ln = set(), None
    for line in (diff_text or "").splitlines():
        if line.startswith("@@"):
            # @@ -a,b +c,d @@  -- c is the NEW-side start; d defaults to 1.
            try:
                plus = [t for t in line.split() if t.startswith("+")][0]
            except IndexError:
                new_ln = None
                continue
            body = plus[1:].split(",")[0]
            try:
                new_ln = int(body)
            except ValueError:
                new_ln = None
            continue
        if new_ln is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.add(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            pass                      # removed: consumes no NEW-side number
        elif line.startswith("\\"):
            pass                      # "\ No newline at end of file"
        else:
            new_ln += 1               # context line

    return added


def scope_text_to_lines(text: str, allowed: set) -> str:
    """``text`` with every line NOT in ``allowed`` (1-based) blanked to "".

    BLANKED, NOT DELETED, and that is the load-bearing choice: every finding
    carries start_line/end_line, and deleting lines would renumber them so the
    reported location pointed at the wrong place in the file a reviewer opens.
    Blanking also breaks cluster CONTIGUITY at each boundary, which is exactly
    the intent -- an authored claim must not absorb a neighbouring line some
    earlier commit wrote.
    """
    if not allowed:
        return ""
    out = [ln if (i + 1) in allowed else ""
           for i, ln in enumerate((text or "").splitlines())]
    return "\n".join(out)


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
        # CANCEL THE SELF-DESTRUCT WATCHDOG (guard-2138). context-reads.py arms
        # `threading.Timer(10, lambda: os._exit(0))` at module scope — correct
        # for the millisecond-lived hook subprocess it was written for, fatal
        # anywhere long-running. os._exit bypasses the interpreter entirely, so
        # there is no exception to catch, no atexit, no pytest epilogue, and the
        # OS sees status 0. This function is called once per analyzed citation,
        # so it armed a FRESH timer per call: measured 4 live 10s timers after
        # one 29-test file, and suite chunk 09 died at 88% with rc=0 and no
        # summary line for exactly this reason ().
        #
        # CANCEL DEFENSIVELY, DO NOT ASSERT, and the difference matters here.
        # guard-2138 says to assert `_timer` exists so a rename fails loudly —
        # correct for the TEST helper, wrong at this call site for two reasons
        # measured while writing this fix: (1) tests legitimately point this
        # loader at a STUB context-reads.py that defines no timer, and (2) the
        # assert lands inside the `except Exception: return None` below, which
        # swallows it — so it cannot fail loudly, it just makes the function
        # return None and the caller SKIP its check. Both directions are the
        # guard-1760 alarm direction. The rename guarantee is kept where it can
        # actually fire: _context_reads_helper.load_context_reads() asserts it
        # against the REAL module, on every test run.
        _t = getattr(mod, "_timer", None)
        if _t is not None:
            _t.cancel()
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
        # The PARTIAL set, for MESSAGE FIDELITY ONLY. It never makes a citation
        # pass: read_tracker() excludes ranged reads by contract ("peeking at
        # one region of a file is not evidence you read the claim's source")
        # and that exclusion is deliberately unchanged here. All this buys is
        # the ability to say WHICH failure it is -- never opened, or opened in
        # part -- because the single message asserted the former for both and
        # sent readers hunting for a read that had already happened.
        # Private accessor because it is the only one exposing partials; a
        # rename degrades to the previous wording rather than raising, which
        # keeps a cosmetic dependency from breaking the whole check.
        _split = getattr(mod, "_read_tracker_split", None)
        partial_paths = set()
        if _split is not None:
            try:
                _full_unused, partial_paths = _split(session_id=session_id)
            except Exception:
                partial_paths = set()
    except Exception:
        return None
    values = [str(e[2] if isinstance(e, (tuple, list)) and len(e) >= 3 else e)
              for e in entries]
    values += [str(p) for p in paths]
    values = [v for v in values if v]
    # UNCHANGED GATE: an empty FULL set still returns None (skip), so a session
    # whose manifest holds only partials behaves exactly as before rather than
    # flagging every citation. Partials refine a message; they never open one.
    if not values:
        return None
    partial_values = [str(x) for x in (partial_paths or set()) if x]

    def _retrieved(kind, value):
        v = str(value).rstrip("/.,);")
        if any(v in got or got in v for got in values):
            return True
        if any(v in got or got in v for got in partial_values):
            return PARTIAL
        return False
    return _retrieved


# Findings that make the verdict FAIL. `unadjudicable-citation` is deliberately
# ABSENT: it reports a citation the provenance manifest structurally cannot record,
# which is a check that never ran, not a check that failed ().
BLOCKING_FINDING_KINDS = (
    "missing-citation", "decorative-citation", "direction-contradiction")


def expressible_predicate(session_id: Optional[str] = None) -> Optional[Callable]:
    """(kind, value) -> COULD the manifest ever have recorded this citation?

    Distinct from `retrieved_predicate`, which answers "was it recorded". This
    answers "was the question even askable", and the split exists because the two
    were conflated into one FAIL: a citation outside the recorder's scope reported
    `decorative-citation`, asserting the session never fetched a source when in
    truth nothing was ever asked. Measured 2026-09-05: 76.9% of git-tracked files
    (9,727 / 12,657) are outside `is_in_scope_advisory`, `.claude/rules/*.md`
    included -- a LOWER bound, since product repos and most of world/ are not in
    the repo at all.

    EVERY UNCERTAIN CASE RETURNS True (expressible), because True keeps the
    decorative check ON. A citation is demoted only where the token positively
    resolves to a real file that the recorder's own scope predicate excludes. In
    particular a bare tree-node key ("system/daemon-only-architecture") resolves to
    no file and therefore stays adjudicable -- it is recordable via a `#prov: node`
    row, so demoting it would suppress a real alarm. Non-`node-key` kinds are all
    recordable (`PROVENANCE_KINDS` carries url / search / node / board, fed by the
    WebFetch/WebSearch-bound hook) and are never demoted.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_ctx_reads_expr_q4", SCRIPTS / "context-reads.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                      # type: ignore
        # Same guard-2138 defensive cancel as retrieved_predicate: context-reads.py
        # arms a module-scope threading.Timer(10, os._exit(0)) that would kill any
        # long-running host with status 0 and no traceback.
        _t = getattr(mod, "_timer", None)
        if _t is not None:
            try:
                _t.cancel()
            except Exception:
                pass
        in_scope = getattr(mod, "is_in_scope_advisory", None)
        if in_scope is None:
            return None
    except Exception:
        return None

    root = SCRIPTS.parent.parent
    # The SAME WORLD_DIR the scope predicate itself was built from -- context-reads.py
    # derives its world-side TRACKED_PREFIXES from this exact value, so resolution and
    # scope can never disagree about where `world/` is (one source of truth, not two).
    # None on an UNINITIALIZED first run; `_candidates` skips the world forms then.
    world = getattr(mod, "WORLD_DIR", None)

    def _candidates(tok):
        """Every on-disk path this token could name, across BOTH governed roots.

        THE WORLD ROOT IS NOT OPTIONAL, and omitting it is why an entire measured
        citation class kept blocking. `world/` is an EXTERNAL path (it is NOT under
        the repo), so `root / "world/telemetry/x.jsonl"` does not exist however real
        the file is -- resolution found nothing, the token fell through to the
        default-True fail-safe, and the citation reported `decorative-citation`.
        Measured g-306-401: a `world/telemetry/**` file read IN FULL (15,804 B) and
        cited for a claim it directly supports was still reported decorative.
        """
        yield root / tok
        yield root / ("." + tok)
        if world is not None and tok.startswith("world/"):
            yield world / tok[len("world/"):]
        # DELIBERATELY NOT a `world/knowledge/tree/<tok>` candidate for a BARE node
        # key. It would resolve `system/daemon-only-architecture` to a real in-scope
        # file and return True -- the SAME answer the default already gives, so it
        # buys no verdict and no test could tell the two apart (guard-1866: a control
        # returning the test's own value has no resolving power). It would also
        # falsify this predicate's docstring, which explains that case as staying
        # adjudicable BECAUSE it resolves to nothing. Left out on purpose.

    def _expressible(kind, value):
        if kind != "node-key":
            return True
        tok = str(value).strip().rstrip("/.,);")
        if not tok or tok.startswith("/"):
            return True
        # The dotted candidate is not an edge case. `_NODE_KEY` starts at a \b, so a
        # citation to `.claude/rules/read-before-edit` is tokenized WITHOUT its
        # leading dot -- and `.claude/rules/**` is both outside advisory scope and
        # among the most-cited evidence classes in framework goals, so skipping the
        # dotted retry would leave the single largest demotable class undemoted while
        # the code looked correct.
        hits = []
        for cand in _candidates(tok):
            try:
                if cand.exists():
                    hits.append(cand)
                elif cand.parent.is_dir():
                    # _NODE_KEY does not capture a non-.md extension, so a citation
                    # to `core/scripts/context-reads.py` arrives as
                    # `core/scripts/context-reads`. Resolve the stem first.
                    hits.extend(sorted(cand.parent.glob(cand.name + ".*")))
            except OSError:
                return True
        if not hits:
            return True
        try:
            return any(in_scope(str(h).replace("\\", "/")) for h in hits)
        except Exception:
            return True
    return _expressible


def run(goal_id: str, artifacts, n: int = DEFAULT_SAMPLE_N,
        session_id: Optional[str] = None, source_text: Optional[str] = None,
        authored_ranges: Optional[dict] = None) -> dict:
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
    expressible = expressible_predicate(session_id)
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
        "unadjudicable_count": 0,
        "authored_scoped": [],
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
        # AUTHORED SCOPE (class 5, ). `--artifact` means "a produced
        # artifact", but for a code-change goal the produced thing is a HUNK, not
        # the whole long-lived file -- so on any established file the sampler
        # graded every prior author's lines as this goal's. Measured on :
        # a one-hunk change at @@ -462,6 +462,28 @@ drew a FAIL on line 153, written
        # two weeks earlier by a different goal. This is the SAME trust model the
        # module already has -- the caller picks the artifacts, and "a caller that
        # names a clean file gets a clean sample" -- narrowed from file to line.
        # OPT-IN ONLY: absent `authored_ranges`, every byte below is unchanged.
        allowed = (authored_ranges or {}).get(str(a))
        if allowed is not None:
            text = scope_text_to_lines(text, allowed)
            result["authored_scoped"].append(
                {"artifact": str(a), "authored_lines": len(allowed)})
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
            for f in analyze(blob, retrieved=retrieved,
                             expressible=expressible):
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

    blocking = [f for f in result["findings"]
                if f.get("kind") in BLOCKING_FINDING_KINDS]
    unadjudicable = [f for f in result["findings"]
                     if f.get("kind") == "unadjudicable-citation"]
    result["unadjudicable_count"] = len(unadjudicable)
    if blocking:
        result["verdict"] = "fail"
    elif result["sampled_count"] == 0:
        result["verdict"] = "skipped"
        if not result["artifacts_read"]:
            result["skip_reason"] = "no artifact could be read"
        elif result["authored_scoped"]:
            # Say SCOPING did this. A scoped-empty sample and a genuinely
            # clean one are the two answers most easily confused, and the
            # caller chose the narrowing -- so the reason has to name it or
            # the narrowing becomes an invisible way to empty the check.
            result["skip_reason"] = (
                "no entity-bearing fact clusters in the AUTHORED lines of the "
                "artifact(s) (%s). The sample was scoped to lines this goal "
                "produced, so this is NOT a statement about the rest of the "
                "file and NOT a pass (guard-1760)."
                % ", ".join("%s: %d line(s)" % (d["artifact"], d["authored_lines"])
                            for d in result["authored_scoped"]))
        else:
            result["skip_reason"] = "no entity-bearing fact clusters in the artifact(s)"
    elif retrieved is None:
        result["verdict"] = "skipped"
        result["skip_reason"] = (
            "provenance manifest unreadable or empty — the decorative-citation "
            "test could not run, so this is NOT a pass (guard-1760). Note that "
            "reads performed with `cat` in a Bash call are invisible to the "
            "manifest by construction (guard-4407).")
    elif unadjudicable:
        # NEITHER pass NOR fail, and the asymmetry is the whole point of the
        # third verdict. Not FAIL: nothing was adjudicated, so "the source went
        # unread" was never measured. Not PASS either: guard-1760 forbids
        # reporting what a checker declined to look at as a pass, and that is the
        # ORIGINAL direction of the guard, unchanged here. `skipped` is the
        # module's existing name for "nothing checkable", and it exits 0, so an
        # unrecordable citation stops blocking closes without ever being called
        # verified ().
        result["verdict"] = "skipped"
        result["skip_reason"] = (
            "%d of the sampled citation(s) are NOT ADJUDICABLE from the "
            "provenance manifest -- the manifest structurally cannot record that "
            "citation class, so the decorative test could not run on them. This is "
            "NOT a pass (guard-1760) and NOT evidence any source went unread. "
            "Read the unadjudicable-citation finding(s) for which tokens."
            % len(unadjudicable))
    else:
        result["verdict"] = "pass"
    return result
