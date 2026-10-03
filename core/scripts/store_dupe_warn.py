#!/usr/bin/env python3
# domain-leak-exempt: framework store-hygiene infra — generic text similarity, no domain strings.
"""store_dupe_warn — ADVISORY add-time near-duplicate warning for the memory stores.

Built for g-115-3223 (which unblocked g-115-3035, closed deep with verification:null
while the capability did not exist).

WHAT IT DOES
------------
Reads a candidate record as JSON on stdin, compares its discriminating text against
the ACTIVE records already in the same store, and prints ONE stderr line naming the
nearest existing entry when similarity crosses that store's threshold.

NON-BLOCKING BY CONSTRUCTION. It always exits 0. Every failure path — unreadable
store, malformed stdin, missing field, import error — is swallowed and yields
silence. A false positive that refuses a legitimate entry is worse than the
duplicate it would have prevented (g-115-3223 SCOPE), so this never gates an add.
The wrappers additionally append `|| true`; the belt and the braces are both
deliberate.

SIMILARITY IS LEXICAL, AND THAT IS A REAL LIMIT (measured, not assumed)
-----------------------------------------------------------------------
Reuses `mdl_gate.tokenize/jaccard/nearest` rather than reimplementing overlap math
(implementation-discipline: three call sites, one primitive, already unit-tested).

Measured on the live corpus 2026-07-28, and this bounds what the warning can claim:

  * guardrails, nearest-neighbour rule-jaccard over 220 sampled ACTIVE entries:
        p50=0.155  p75=0.177  p90=0.214  p95=0.258  max=0.409
  * reasoning-bank, nearest-neighbour TITLE-jaccard over 200 sampled ACTIVE entries:
        p50=0.182  p75=0.208  p90=0.250  p95=0.300  p99=0.545  max=1.000

So the mdl_gate default `dup_threshold=0.80` would fire on almost nothing here —
adopting it unexamined would have shipped a warning that cannot fire, which is the
guard-1465 vacuous-check failure this goal was filed to prevent. Thresholds below
sit just above each store's measured p99 instead: rare enough to stay advisory,
low enough to actually fire.

SEMANTIC TIER (g-306-574 unit C, 2026-10-02)
--------------------------------------------
A second, strictly advisory tier runs alongside the lexical one: it queries the
per-box retrieval embedding index (_embedding_retrieval.cosine_scores, the same
query path retrieval uses), partitions the rows to this store by the BUILDER's
own per-row doc-type (the index meta.json is the SSOT — never re-derived), and
prints the top 3 matches with scores when the TOP match crosses the store's
calibrated cosine threshold.

Advisory-only and fail-open like the lexical tier: missing index, unavailable
model, bad meta, any runtime error -> lexical-only, exit 0, never blocks. The
candidate is embedded on the SAME supplementary_text_parts surface the builder
embedded the rows (a score on a different surface is not a measurement), and the
thresholds (_dupe_semantic_thresholds.py) are model-specific — a rebuilt index
with a different model voids them (guard-1511: re-sweep, record the margins).

WHAT IT CANNOT DO — MEASURED, BOTH TIERS. The guard-1486-vs-guard-1485 reworded
case scores jaccard 0.112 (rank 20 of 220 lexical neighbours — no lexical
threshold catches it) AND cosine 0.6227 on all-MiniLM-L6-v2 (ranks 28th/13th of
7,086 guardrails — BELOW the unrelated-record nearest-neighbour floor's median
0.6788, p99 0.8832). No threshold on this model separates that reworded twin
from the noise floor, so the guardrails tier is flagged THIN-MARGIN and its twin
recall is UNVALIDATED; reasoning-bank and pattern-signatures have no surviving
live twin to validate against (p99 rarity bound only). The class that DOES fire
is the near-verbatim one (the goal's own cc-12 copies: cosine 0.694/0.659 at
rank 1–2) — and the reworded case remains in the recurring near-dup
consolidation review's hands. The module says this in the warning text rather
than implying the broader capability.

TITLE, NOT TITLE+CONTENT, for reasoning-bank: measured 0.529 title-only vs 0.368
title+content on the rb-3927/rb-4038 known-duplicate pair. Long content dilutes
shared vocabulary and moves true duplicates toward the noise floor.

TELEMETRY. Every invocation emits one `store-dupe-warn` record to
`meta/gate-firings.jsonl` (see GATE_ID below) — including the SILENT ones, so
fired/invoked is a measurable rate rather than a count nobody can interpret.
Measured cost of that record: +3 ms on an 87 ms baseline (~3%), because the
own-cloud hot path spools a single local line rather than doing an S3 RMW.

COST. Measured 2026-07-28 against the live corpus: ~75 ms per guardrails add,
~111 ms per reasoning-bank add (5.3k active entries), including interpreter
startup. The scan is O(corpus); it is paid on every add, against an operation
that already makes a daemon round-trip. If a store grows to where this stops
being negligible, bucket the comparison by category/tag rather than sampling —
sampling would reintroduce the silent-miss failure this module exists to remove.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

_SELF = Path(__file__).resolve().parent
if str(_SELF) not in sys.path:
    sys.path.insert(0, str(_SELF))

# Per-store config: which JSONL, which fields carry the discriminating signal, and
# the similarity at which a warning is worth the reader's attention. Thresholds are
# calibrated from the measured nearest-neighbour distributions in the docstring —
# each sits just above its store's p99, so a firing is genuinely unusual.
STORES = {
    "guardrails": {
        "filename": "guardrails.jsonl",
        "fields": ("rule",),
        "threshold": 0.45,
        "refuse_threshold": 0.75,
        "label": "guardrail",
    },
    "reasoning-bank": {
        "filename": "reasoning-bank.jsonl",
        "fields": ("title",),
        "threshold": 0.55,
        "refuse_threshold": 0.75,
        "label": "reasoning-bank entry",
    },
    "pattern-signatures": {
        "filename": "pattern-signatures.jsonl",
        "fields": ("name", "description"),
        "threshold": 0.55,
        "refuse_threshold": 0.75,
        "label": "pattern signature",
    },
}

# refuse_threshold (g-115-6948): the ENFORCEMENT tier above the advisory
# `threshold`. Past it the daemon append REFUSES with a pointer to the existing
# entry (409 near_duplicate; body field allow_near_dup:true / wrapper
# --allow-near-dup bypasses). Calibrated from the full gate-firings history
# 2026-08-20 (n=6,550 invocations across the fleet): the nearest-neighbour
# similarity of every add that was NOT warned maxes at 0.500 (p99 0.294), while
# 20 of the 26 warned adds were verbatim twins at exactly 1.0 (guard-4090
# measured two of them landing anyway — the advisory tier cannot stop what it
# warns about). 0.75 sits 0.25 above the highest legitimate-add collision ever
# measured and 0.25 below the twin cluster: on the full history it refuses 22
# of 26 warned adds and zero of the ~6,500 clean ones. The reworded-duplicate
# class (guard-1486-vs-1485, jaccard 0.112) stays out of reach of ANY lexical
# threshold — that class belongs to the recurring near-dup consolidation
# review, not this gate.

# Records in these states are not live knowledge; warning about them is noise.
_INACTIVE_STATUSES = {"retired", "superseded", "archived"}


def signal_text(record: dict, fields: Tuple[str, ...]) -> str:
    """Join the configured discriminating fields of one record."""
    parts = []
    for f in fields:
        v = record.get(f)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts)


def load_corpus(path: Path, fields: Tuple[str, ...]) -> List[Tuple[str, str]]:
    """(id, signal_text) for every ACTIVE record. Malformed lines are skipped —
    one bad line must not silence the whole advisory."""
    out: List[Tuple[str, str]] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if str(d.get("status", "")).lower() in _INACTIVE_STATUSES:
            continue
        text = signal_text(d, fields)
        if text:
            out.append((str(d.get("id", "")), text))
    return out


def format_warning(store: str, label: str, nearest_id: str, similarity: float,
                   threshold: float, nearest_text: str) -> str:
    """The advisory line. Names the existing entry so the reader can go look at it,
    and states the action — strengthen or supersede, per the Phase 6.5 protocol the
    add-scripts' callers already follow."""
    snippet = " ".join(nearest_text.split())[:100]
    return (
        f"[store-dupe-warn] ADVISORY: this {label} closely resembles {nearest_id} "
        f"(similarity {similarity:.2f} >= {threshold:.2f}).\n"
        f"[store-dupe-warn]   existing: {snippet}...\n"
        f"[store-dupe-warn]   The add was NOT blocked. If it restates {nearest_id}, prefer "
        f"strengthening that entry (utilization increment) or retiring it as superseded, "
        f"rather than carrying both. Lexical check only — it cannot see a duplicate "
        f"phrased in different words."
    )


# ── Semantic tier (g-306-574 unit C) ─────────────────────────────────────────
# STRICTLY ADVISORY and fail-open, exactly like the lexical tier: it can add a
# second advisory line, and nothing in it can change the return code, refuse an
# add, or even suppress the lexical line. The model in _dupe_semantic_thresholds
# is the calibration source of truth — a rebuilt index under a DIFFERENT model
# voids the thresholds (guard-1511), so rather than silently scoring with
# numbers that no longer mean anything, the tier stays off and records WHY.
SEMANTIC_TOP_K = 3


def _semantic_text(record: dict) -> str:
    """The candidate's EMBEDDING surface: retrieve.supplementary_text_parts —
    the SAME surface embedding-index-build.match_text embedded the index rows
    on. A hand-maintained second field list here would be the g-306-45
    anti-pattern (two halves of a join written twice), so the builder's shared
    helper is the source, not a copy. Deliberately NOT signal_text(): that is
    the lexical tier's surface, and a cosine measured on one surface against
    rows written on another is not a measurement."""
    try:
        import retrieve as _r
        return " ".join(_r.supplementary_text_parts(record)).strip()
    except Exception:
        return ""


# Which builder doc-type in the index's meta.json belongs to which store.
# Mirrors embedding-index-build.load_corpus (guardrail / rb / signature rows
# are written by it); the query side reads the per-row `type` FROM the index
# (the SSOT) and uses this map only to pick out this store's rows.
STORE_DOC_TYPE = {
    "guardrails": "guardrail",
    "reasoning-bank": "rb",
    "pattern-signatures": "signature",
}


def semantic_scores(record: dict, store: str, cand_id: str = ""):
    """Pure half of the semantic tier: this store's same-store rows scored
    against the candidate's embedding surface, ranked descending, own id
    excluded.

    Always returns the 2-tuple (payload, meta): ((matches, threshold,
    model), None) where matches is a ranked list of (doc_id, cosine) — or
    (None, reason) on ANY of: unknown store, no calibrated threshold, no
    semantic text, index absent or unreadable, index built under a model
    other than the calibrated one (guard-1511: the numbers would be
    meaningless, so the tier stays off and says WHY), empty same-store
    partition, or encoder/index runtime error. ONE shape, both halves — the
    success path's 3-tuple used to make `out, meta = ...` raise, and this
    file swallows that into `error:ValueError` (a reason that names nothing).
    Pure in the retrieval-module surface it is tested against: the tests
    monkeypatch `_embedding_retrieval.cosine_scores / doc_types / index_model`,
    so no model loads and no real index is touched."""
    import _dupe_semantic_thresholds as _dst
    thr = _dst.SEMANTIC_THRESHOLDS.get(store)
    doc_type = STORE_DOC_TYPE.get(store)
    if thr is None or doc_type is None:
        return None, "no-calibrated-threshold"
    query = _semantic_text(record)
    if not query:
        return None, "no-semantic-text"
    import _embedding_retrieval as _er
    model = _er.index_model()
    if model is None:
        # Absent vs present-but-unreadable are different operator actions:
        # build the index, versus fix a torn/partial meta.json write.
        return None, ("index-meta-unreadable" if _er.index_available()
                      else "index-absent")
    if model != _dst.CALIBRATED_MODEL:
        return None, f"model-mismatch:{model}"
    scores = _er.cosine_scores(query)
    if not scores:
        reason = (_er.last_degradation() or {}).get("reason") or "degraded"
        return None, reason
    types = _er.doc_types()
    if not types:
        return None, "doc-types-unreadable"
    matches = sorted(((sid, sc) for sid, sc in scores.items()
                      if types.get(sid) == doc_type and sid != cand_id),
                     key=lambda p: p[1], reverse=True)
    if not matches:
        return None, "no-same-store-rows"
    return (matches, thr, model), None


def format_semantic_lines(matches, threshold: float, model: str, label: str,
                          top_k: int = SEMANTIC_TOP_K) -> str:
    """The advisory block for a firing semantic tier (top match already crossed
    `threshold`). Pure formatting — no I/O, no retrieval."""
    lines = ["[store-dupe-warn] SEMANTIC (advisory, not blocking): top "
             f"{min(top_k, len(matches))} existing {label} entries by "
             f"embedding cosine against this {label}"]
    for sid, sc in matches[:top_k]:
        lines.append(f"[store-dupe-warn]   {sc:.3f}  {sid}")
    lines.append(f"[store-dupe-warn]   (top cosine >= {threshold} on the {model} "
                 "index; the add was NOT blocked; near-verbatim twins score "
                 "highest — a duplicate reworded in different words may still "
                 "sit in the unrelated-record floor; calibration: "
                 "_dupe_semantic_thresholds.py)")
    return "\n".join(lines)


def semantic_check(record: dict, store: str, cfg: dict,
                   top_k: int = SEMANTIC_TOP_K):
    """The ADVISORY semantic tier, one call. Always returns the 4-tuple
    (lines, top_id, top_cosine, reason): exactly one of the two halves is
    populated — (lines, top_id, top_cosine, None) when the top match crossed
    the store's calibrated cosine threshold, else (None, None, None, reason),
    where reason is the semantic_scores verdict so the telemetry lane can say
    WHY the tier served nothing on a given add. NEVER raises — the lexical
    tier's fail-open contract applies to this one too, and nothing in here
    may change the return code or suppress the lexical line."""
    try:
        out, meta = semantic_scores(record, store, str(record.get("id") or ""))
        if out is None:
            return None, None, None, meta
        matches, thr, model = out
        top_id, top_score = matches[0]
        if top_score < thr:
            return None, None, None, "below-threshold"
        return (format_semantic_lines(matches, thr, model,
                                      cfg.get("label", store), top_k),
                top_id, round(top_score, 4), None)
    except Exception as exc:
        return None, None, None, f"error:{type(exc).__name__}"


def check(record: dict, store: str, corpus: Optional[List[Tuple[str, str]]] = None,
          world_dir: Optional[Path] = None,
          detail: Optional[dict] = None) -> Optional[str]:
    """Return the warning string, or None when there is nothing to say.

    Pure given `corpus`; reads the live store only when corpus is not supplied.

    `detail`, when a dict is passed in, is POPULATED in place with the outcome
    of this call (`decision`, plus whatever was computed). It is an out-param
    rather than a changed return type so the existing contract is untouched,
    and it costs no second scan of the corpus. main() uses it to emit telemetry
    for the SILENT cases too — see the GATE_ID note below.
    """
    def _mark(decision, **kw):
        # clear() first: each _mark is a COMPLETE verdict and exactly one fires
        # per call. Without the clear, main()'s seed value (`reason: unreached`)
        # survived into the firing record of a call that plainly WAS reached —
        # telemetry that contradicts itself is worse than none.
        if detail is not None:
            detail.clear()
            detail["decision"] = decision
            detail.update(kw)

    def _finish(lex, semantic):
        # The single exit for every REACHED verdict. `lex` is (warning,
        # mark_kwargs) or (None, noop_kwargs); `semantic` is the
        # semantic_check 4-tuple. The decision follows what the USER SAW: a
        # line on stderr from EITHER tier is `pass` — "trigger matched, fired
        # but did not block" is the taxonomy's definition of an advisory, and
        # the semantic tier is a trigger of this same gate. Recording a
        # line-emitting call as `noop` would be the g-115-3093 misdescription
        # class (the record says "no trigger matched" while a warning sits in
        # the caller's stderr). Silence from both tiers is `noop`; nothing in
        # `semantic` can block, and the extra fields keep the two tiers
        # separable for the retirement evaluator (semantic_fired /
        # semantic_top1 / semantic_reason).
        lex_warning, mark_kw = lex
        sem_lines, sem_top_id, sem_cos, sem_reason = semantic
        mark_kw = dict(mark_kw)
        mark_kw.pop("decision", None)   # the decision is RE-DERIVED below
        if sem_lines is not None:
            mark_kw["semantic_fired"] = True
            mark_kw["semantic_top1"] = sem_top_id
            mark_kw["semantic_top1_cosine"] = sem_cos
        else:
            mark_kw["semantic_reason"] = sem_reason
        decision = "pass" if (lex_warning is not None or sem_lines is not None) else "noop"
        _mark(decision, **mark_kw)
        if lex_warning is None:
            return sem_lines
        if sem_lines is None:
            return lex_warning
        return lex_warning + "\n" + sem_lines

    cfg = STORES.get(store)
    if cfg is None:
        _mark("noop", reason="unknown store")
        return None
    candidate = signal_text(record, cfg["fields"])
    if not candidate:
        _mark("noop", reason="candidate carries no signal text")
        return None
    try:
        import mdl_gate
    except ImportError:
        _mark("fail_open", reason="mdl_gate unavailable")
        return None
    if corpus is None:
        if world_dir is None:
            _mark("fail_open", reason="no corpus and no world_dir")
            return None
        corpus = load_corpus(Path(world_dir) / cfg["filename"], cfg["fields"])
    if not corpus:
        _mark("noop", reason="empty corpus", corpus_size=0)
        return None
    # Exclude the candidate's own id so a re-run (or a record already appended by a
    # concurrent writer) never reports the entry as a duplicate of itself.
    cand_id = str(record.get("id") or "")
    if cand_id:
        corpus = [(i, t) for i, t in corpus if i != cand_id]
    # The semantic tier runs for EVERY reached verdict (warn or not): it is a
    # separate advisory signal with its own calibrated bar, and a lexical miss
    # is exactly the case it was filed for. It is lazy-imported, fail-open, and
    # cannot flip the decision or the exit code — only add lines and a field.
    semantic = semantic_check(record, store, cfg)
    near_id, sim, near_text = mdl_gate.nearest(candidate, corpus)
    if near_id is None:
        return _finish((None, {"decision": "noop",
                               "reason": "no comparable neighbour",
                               "corpus_size": len(corpus)}), semantic)
    if sim < cfg["threshold"]:
        # The scan RAN and cleared the candidate. Recorded as `noop` (no trigger
        # matched) rather than `pass`, so `count(decision != "noop")` — the
        # retirement evaluator's fired-count — equals the number of times this
        # actually WARNED. Calling every silent add a `pass` would make the
        # helper look permanently useful and un-retirable.
        return _finish((None, {"decision": "noop",
                               "corpus_size": len(corpus), "nearest_id": near_id,
                               "similarity": round(sim, 4),
                               "threshold": cfg["threshold"]}), semantic)
    # Warned. `pass` in the _gate_log taxonomy is "trigger matched, fired but did
    # NOT block the caller" — exactly an advisory. NOT `block`: this helper never
    # stops an add and never recommends stopping one, so a `block` record would
    # overstate it. (Contrast goal-pickup-coordination-check, where race_risk IS
    # a yield recommendation and `block` is the honest label. Same mechanism, two
    # different verdicts — an inherited mapping would misreport.)
    return _finish((format_warning(store, cfg["label"], near_id, sim,
                                   cfg["threshold"], near_text),
                    {"decision": "pass",
                     "corpus_size": len(corpus), "nearest_id": near_id,
                     "similarity": round(sim, 4),
                     "threshold": cfg["threshold"]}), semantic)


def refuse_check(record: dict, store: str,
                 corpus: List[Tuple[str, str]]) -> Optional[dict]:
    """ENFORCEMENT-tier check (g-115-6948): dict verdict when `record` crosses
    the store's refuse_threshold against `corpus`, else None.

    PURE — corpus is required (the daemon caller already holds the store's
    items via its jsonl cache; re-reading the file here would double the I/O
    and race the cache). Raises nothing on its own inputs by construction:
    any internal surprise is the CALLER's fail-open responsibility (rb-605 —
    anticipation gates fail open; store.py wraps this call in a broad except).

    Returns {"nearest_id", "similarity", "refuse_threshold", "nearest_text"}
    — the caller-verifiable evidence guard-1661 requires a governed-store
    refusal to carry.
    """
    cfg = STORES.get(store)
    if cfg is None:
        return None
    thr = cfg.get("refuse_threshold")
    if not thr:
        return None
    candidate = signal_text(record, cfg["fields"])
    if not candidate:
        return None
    import mdl_gate
    cand_id = str(record.get("id") or "")
    if cand_id:
        corpus = [(i, t) for i, t in corpus if i != cand_id]
    if not corpus:
        return None
    near_id, sim, near_text = mdl_gate.nearest(candidate, corpus)
    if near_id is None or sim < thr:
        return None
    return {
        "nearest_id": near_id,
        "similarity": round(sim, 4),
        "refuse_threshold": thr,
        "nearest_text": " ".join(near_text.split())[:160],
    }


# Its OWN gate id — deliberately not routed through mdl_gate.run_assess to inherit
# that module's existing _gate_log wiring. run_assess logs against assess()'s
# KEEP/DROP semantics; this helper uses nearest()+threshold semantics, so every
# inherited record would misreport what happened. A wrong firing record is worse
# than no firing record, because it reads as authoritative to the retirement
# evaluator. (g-115-3626)
GATE_ID = "store-dupe-warn"


def _emit(detail: dict, store: Optional[str]) -> None:
    """Log one firing per INVOCATION — including the silent case, so that
    fired/invoked is a measurable RATE rather than an uninterpretable count.
    Logging only on warn would reproduce the very blind spot g-115-3626 exists
    to close: an advisory nobody can tell is running.

    Best-effort by construction. `log()` already promises never to raise; the
    except is the second guard, for an import failure it cannot cover."""
    try:
        import _gate_log
        d = dict(detail)
        decision = d.pop("decision", "fail_open")
        # Forward `gate_error` as its OWN top-level field rather than leaving it
        # in `extra` (g-001-339). gate-retirement-eval's remediation text for a
        # fail_open reads verbatim "Inspect the gate_error field on those
        # firings and fix" — so a diagnostic parked in `extra` is invisible to
        # the exact instruction that sends a reader looking for it. Measured
        # 2026-08-02: 0 of 30,008 firings carried gate_error and the key was
        # absent from the store's entire key union, across 54 fail_opens
        # fleet-wide — the prescribed diagnostic path was empty for every gate,
        # not just this one.
        gate_error = d.pop("gate_error", None)
        _gate_log.log(GATE_ID, decision, caller="store_dupe_warn.main",
                      payload={"store": store}, gate_error=gate_error, extra=d)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    """ALWAYS returns 0. This is an advisory; a crash here must not fail an add."""
    detail: dict = {"decision": "fail_open", "reason": "unreached"}
    store = None
    _suppress = False   # set only for a clean SystemExit (--help): not an invocation
    try:
        ap = argparse.ArgumentParser(
            description="Advisory add-time near-duplicate warning (never blocks).")
        ap.add_argument("--store", required=True, choices=sorted(STORES))
        ap.add_argument("--world-dir", default=None,
                        help="override the store root (tests); defaults to the resolved WORLD_DIR")
        args = ap.parse_args(argv)
        store = args.store

        body = sys.stdin.read()
        if not body.strip():
            # EMPTY STDIN IS NOT A GATE INVOCATION — emit nothing (g-115-3797).
            # Identical class to the `--help` SystemExit discriminated below,
            # and the same phantom-signal shape g-115-3626 fixed there. Every
            # caller is `printf '%s' "$BODY" | store_dupe_warn.py --store <s>`
            # where BODY="$(cat)", so running any *-add.sh with no stdin (an
            # agent checking the interface, a script whose heredoc did not
            # fire) pipes "" here. json.loads("") then raises JSONDecodeError,
            # the generic handler below records decision=fail_open, and
            # gate-retirement-eval routes ANY fail_open to `investigate`.
            # That is how this goal got filed: 108 recorded fail_opens, all
            # reason=JSONDecodeError, spread evenly across all five agents
            # because every agent occasionally runs a bare *-add.sh.
            #
            # There is nothing to duplicate-check and nothing entered any
            # store unchecked: the daemon rejects an empty body downstream
            # with {"error":"invalid_body"}, so the caller-bug case is already
            # surfaced loudly by the add itself. A second signal here would be
            # redundant, and as fail_open it is actively wrong — it reports the
            # GATE as having failed when the gate was never asked anything.
            #
            # Deliberately NOT a new "skip" decision: _VALID_DECISIONS has no
            # such member, and adding one would change the schema under
            # gate-retirement-eval, gate-stats, and every other consumer for a
            # case that is better described as "did not happen".
            # NON-empty malformed JSON still falls through to fail_open below —
            # that IS a real gate failure and must stay visible.
            _suppress = True
            return 0
        record = json.loads(body)
        if not isinstance(record, dict):
            detail = {"decision": "fail_open", "reason": "stdin is not a JSON object"}
            return 0

        world_dir = args.world_dir
        if world_dir is None:
            try:
                from _paths import WORLD_DIR
                world_dir = WORLD_DIR
            except Exception:
                detail = {"decision": "fail_open", "reason": "WORLD_DIR unresolvable"}
                return 0

        warning = check(record, args.store, world_dir=Path(world_dir), detail=detail)
        if warning:
            print(warning, file=sys.stderr)
    except SystemExit as se:
        # argparse failure on a bad --store must not take the add down with it.
        # But DISCRIMINATE on the exit code. `--help` also raises SystemExit —
        # with code 0 — and logging that as fail_open manufactures a phantom
        # signal: gate-retirement-eval routes any fail_open to `investigate`,
        # so a human simply reading the usage text would file a spurious
        # investigation. That is the g-115-3093 blind-spot class (a decision
        # label that misdescribes what happened) reappearing one layer down,
        # and it was caught by fresh-eyes on this file, not by a test.
        # A help/clean exit is not a gate invocation at all — emit nothing.
        if se.code in (0, None):
            _suppress = True
        else:
            detail = {"decision": "fail_open", "reason": f"bad arguments (exit {se.code})"}
        return 0
    except Exception as exc:
        # gate_error carries the INFORMATIVE TAIL — never a raw format_exc()
        # (g-001-339). `_gate_log._truncate` keeps the HEAD (`s[:200] + "..."`),
        # and a traceback's head is its banner plus the OUTER frames, so 200
        # chars of one is spent before reaching the exception type and message,
        # which sit at the very END. Passing format_exc() here would therefore
        # populate the field while still hiding the cause — the failure mode is
        # a diagnostic that LOOKS present and says nothing. Compose type +
        # message + innermost frame explicitly: it fits the budget and is the
        # part a diagnostician actually needs. `reason` stays the bare exception
        # name so existing consumers that group by it are unaffected.
        # Report THIS FILE's frame first, then the innermost. The innermost
        # frame alone is usually inside a stdlib module and is the SAME for
        # every occurrence of that exception type anywhere — measured here as
        # `decoder.py:356` for JSONDecodeError, which has zero discriminating
        # power about which call site actually failed. The own-code frame is
        # what a fix needs; the innermost is kept after it for depth.
        tb = traceback.extract_tb(exc.__traceback__)
        _here = Path(__file__).name
        own = [f for f in tb if Path(f.filename).name == _here]
        parts = []
        if own:
            parts.append(f"{_here}:{own[-1].lineno}")
        if tb and (not own or tb[-1] is not own[-1]):
            parts.append(f"{Path(tb[-1].filename).name}:{tb[-1].lineno}")
        where = " -> ".join(parts) or "?"
        detail = {"decision": "fail_open", "reason": type(exc).__name__,
                  "gate_error": f"{type(exc).__name__}: {exc} @ {where}"}
        return 0
    finally:
        # In the `finally` so every return path above is covered — including the
        # two bare `return 0`s and the exception handlers.
        if not _suppress:
            _emit(detail, store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
