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

WHAT IT CANNOT DO. g-115-3223 outcome 4 asked that the guard-1486-vs-guard-1485
case reproduce a warning. It cannot, at ANY lexical threshold. Measured: those two
score jaccard 0.112, and guard-1485 ranks **20th** among guard-1486's neighbours —
BELOW the store's median nearest-neighbour similarity (0.155). Nineteen unrelated
guardrails are lexically closer than the true duplicate. They share a root cause
(NUL bytes in pytest logs defeating grep) expressed in almost disjoint vocabulary.
Catching that pair needs SEMANTIC similarity (embeddings), not token overlap, and
is tracked separately. This module detects near-VERBATIM restatement — the
rb-4038-vs-rb-615 class the goal also names — and says so rather than implying the
broader capability.

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
        "label": "guardrail",
    },
    "reasoning-bank": {
        "filename": "reasoning-bank.jsonl",
        "fields": ("title",),
        "threshold": 0.55,
        "label": "reasoning-bank entry",
    },
    "pattern-signatures": {
        "filename": "pattern-signatures.jsonl",
        "fields": ("name", "description"),
        "threshold": 0.55,
        "label": "pattern signature",
    },
}

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
    near_id, sim, near_text = mdl_gate.nearest(candidate, corpus)
    if near_id is None:
        _mark("noop", reason="no comparable neighbour", corpus_size=len(corpus))
        return None
    if sim < cfg["threshold"]:
        # The scan RAN and cleared the candidate. Recorded as `noop` (no trigger
        # matched) rather than `pass`, so `count(decision != "noop")` — the
        # retirement evaluator's fired-count — equals the number of times this
        # actually WARNED. Calling every silent add a `pass` would make the
        # helper look permanently useful and un-retirable.
        _mark("noop", corpus_size=len(corpus), nearest_id=near_id,
              similarity=round(sim, 4), threshold=cfg["threshold"])
        return None
    # Warned. `pass` in the _gate_log taxonomy is "trigger matched, fired but did
    # NOT block the caller" — exactly an advisory. NOT `block`: this helper never
    # stops an add and never recommends stopping one, so a `block` record would
    # overstate it. (Contrast goal-pickup-coordination-check, where race_risk IS
    # a yield recommendation and `block` is the honest label. Same mechanism, two
    # different verdicts — an inherited mapping would misreport.)
    _mark("pass", corpus_size=len(corpus), nearest_id=near_id,
          similarity=round(sim, 4), threshold=cfg["threshold"])
    return format_warning(store, cfg["label"], near_id, sim, cfg["threshold"], near_text)


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
        _gate_log.log(GATE_ID, decision, caller="store_dupe_warn.main",
                      payload={"store": store}, extra=d)
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
        detail = {"decision": "fail_open", "reason": type(exc).__name__}
        return 0
    finally:
        # In the `finally` so every return path above is covered — including the
        # two bare `return 0`s and the exception handlers.
        if not _suppress:
            _emit(detail, store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
