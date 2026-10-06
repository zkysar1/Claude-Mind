#!/usr/bin/env python3
"""Shipped-claim store-content check — CLI over gates/shipped_claim.py.

Reads a goal's closing outcome_note (stdin, or --note), extracts the
shipped-symbol claims it makes about store-backed artifacts, reads each
artifact's CONTENT FROM THE STORE, and reports every claimed symbol that
occurs zero times there.

Canonical incident: g-326-585 closed `completed` claiming a `--direct` mode
and a `probe_direct()` had been added to `zakpod1-pp-aging-probe.py`. The
store's `world/scripts/zakpod1-pp-aging-probe.py` (24,976 B) contains neither,
and its docstring argues the opposite design. A downstream acceptance goal
(g-326-586) was filed against a mode that does not exist.

WHY THE STORE AND NOT THE DISK. Under `STORAGE_BACKEND=own-cloud` the local
tree is a read-through CACHE (rb-2636), so a local read can show content the
store does not hold, and vice versa. `read_authoritative_bytes` is the same
pure to-memory store read `backend-cat.sh cat` performs — the canonical
governed-store probe (probe-with-canonical-code-path.md) — reached here
in-process instead of through a subprocess per artifact.

WHY NOT AT `cmd_update_goal(status=completed)`. Measured 2026-08-22 before
building this: the outcome_note is frequently NOT on the record at the moment
the status flips. `gates.completion_artifact` runs there, and `goal` in hand
at that call site carries `outcome_note` — but g-326-469's own note records
"Completed by alpha 2026-08-19T23:14:05 with an EMPTY outcome_note", filled in
by a second agent the following day, and 5 of 21 completed goals sampled that
day carried a 0-byte note. A close-time note gate is therefore inert on an
unknown fraction of closes. `iteration-close.sh` do_state_update is a LATER
phase that already re-reads the record's current note via
`_probe_goal_outcome_note()` for the metric gate (g-115-5157) — that is the
moment the note is most likely populated, and this check is wired beside it.

REPORT-ONLY BY DESIGN — it never blocks. The goal is already closed by the
time this runs, so blocking is not available; and paraphrase in closing prose
makes a hard refusal the wrong instrument. Detection is the deliverable
(learning-philosophy.md: prefer reducing time-to-detection).

Usage:
  bash core/scripts/shipped-claim-store-check.sh --goal <id> [--source world]
  # note on stdin, or --note "<text>"

  # Mark a firing a reader has adjudicated (append-only: a later `kind:
  # verdict` row, never an edit). rc 0 recorded, 2 refused (no reason, or the
  # goal never fired):
  bash core/scripts/shipped-claim-store-check.sh --goal <id> \
      --verdict false_positive|confirmed --reason "<why>" [--basis "<where>"] \
      [--row-ts <the firing's ts>]

Output JSON (stdout): the gates/shipped_claim.evaluate() payload plus
`resolved` (artifact -> store virtual path or null).

Exit codes: 0 = clean or no claim · 1 = mismatch found (report, not a block)
· 2 = usage error. Fail-open: every internal error exits 0 with an `error`
key, because a broken detector must never disturb a close.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402
reconfigure_stdio()

from gates.shipped_claim import evaluate  # type: ignore  # noqa: E402

# Store-backed roots a bare filename is resolved against, in order. Scoped
# deliberately to the governed external stores: this check exists for the
# "world/ artifact absent from the store" class, and `core/scripts` /
# `.claude/` are git-tracked, where `git log` already answers the question.
_STORE_ROOTS = ("world/scripts", "world/conventions", "world/knowledge",
                "world", "meta")


def _backend():
    from storage_backend import get_backend  # type: ignore
    return get_backend()


# The env-var spelling of a store root ("$WORLD_PATH/scripts/x.sh" reads as the
# token "WORLD_PATH/scripts/x.sh"): the same root under another name.
_STORE_ALIASES = (("WORLD_PATH/", "world/"), ("META_PATH/", "meta/"))


def _resolve_virtual(token: str) -> "list[str]":
    """Candidate store virtual paths for an artifact token, best first.

    A token that already carries a `world/` or `meta/` prefix (or the
    WORLD_PATH/META_PATH spelling of one) is taken verbatim (one candidate). A
    bare basename is tried under each store root. A relative fragment
    ("scripts/foo.py") is a store root plus that fragment — never the
    fragment's basename: reducing `core/config/aspirations.yaml` to
    `aspirations.yaml` bound it to an unrelated 47-byte store file of the same
    name and reported a zero about the wrong file (g-115-7429). Anything not
    under a store root yields no candidates and is skipped — never guessed at.
    """
    tok = token.strip().lstrip("./")
    for alias, real in _STORE_ALIASES:
        if tok.startswith(alias):
            tok = real + tok[len(alias):]
    if tok.startswith("world/") or tok.startswith("meta/"):
        return [tok]
    if "/" in tok:
        return [f"{root}/{tok}" for root in ("world", "meta")]
    return [f"{root}/{tok}" for root in _STORE_ROOTS]


def _read_store_text(backend, virtual_path: str) -> "str | None":
    """Store-authoritative read of one virtual path, or None if unreadable.

    None means "the reader could not see it", never "it is absent" — the
    caller must treat it as a skip, not as a mismatch
    (verify-before-assuming.md: a failed read is zero signals).
    """
    from _paths import META_DIR, WORLD_DIR  # type: ignore
    if virtual_path.startswith("world/"):
        if WORLD_DIR is None:
            return None
        full = Path(WORLD_DIR) / virtual_path[len("world/"):]
    elif virtual_path.startswith("meta/"):
        if META_DIR is None:
            return None
        full = Path(META_DIR) / virtual_path[len("meta/"):]
    else:
        return None
    try:
        if not backend.exists(full):
            return None
        return backend.read_authoritative_bytes(full).decode("utf-8", "replace")
    except Exception:
        return None


VERDICTS = ("false_positive", "confirmed")


def _ledger_path() -> "Path | None":
    from _paths import WORLD_DIR  # type: ignore
    if WORLD_DIR is None:
        return None
    return Path(WORLD_DIR) / "shipped-claim-mismatches.jsonl"


def _append_ledger(record: dict) -> bool:
    """Append one record to world/shipped-claim-mismatches.jsonl.

    Fail-open: a ledger write error is printed and swallowed. The durable
    trail matters, but never more than the close it is observing.
    """
    try:
        from _fileops import locked_append_jsonl  # type: ignore
        ledger = _ledger_path()
        if ledger is None:
            return False
        locked_append_jsonl(str(ledger), record)
        return True
    except Exception as exc:  # pragma: no cover - ledger is best-effort
        print(f"[shipped-claim] ledger write failed: {exc}", file=sys.stderr)
        return False


def _log_ledger(goal_id: str, agent: str, payload: dict) -> None:
    _append_ledger({
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "agent": agent or "unknown",
        "goal_id": goal_id,
        "mismatches": payload.get("mismatches") or [],
        "resolved": payload.get("resolved") or {},
    })


def _firing_ts(goal_id: str) -> "list[str]":
    """`ts` of every FIRING row for a goal (verdict rows carry `kind`)."""
    ledger = _ledger_path()
    if ledger is None or not ledger.exists():
        return []
    out = []
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("goal_id") == goal_id and not row.get("kind"):
            out.append(str(row.get("ts")))
    return out


def _record_verdict(args) -> int:
    """Append a disposition row for a firing a reader has adjudicated.

    The ledger is append-only, so a verified false positive cannot be edited
    out of it; it is MARKED by a later row (`kind: verdict`) naming the goal
    and, optionally, the firing's `ts`. Rows keep an empty `mismatches` and
    `resolved` so a reader that treats every row as a firing sees a row with
    nothing in it, not a KeyError. Refuses a goal that never fired.
    """
    if not (args.reason or "").strip():
        print("[shipped-claim] --verdict requires --reason", file=sys.stderr)
        return 2
    fired = _firing_ts(args.goal)
    if not fired:
        print(f"[shipped-claim] no firing recorded for {args.goal}", file=sys.stderr)
        return 2
    if args.row_ts and args.row_ts not in fired:
        print(f"[shipped-claim] {args.goal} has no firing at ts {args.row_ts} "
              f"(firings: {', '.join(fired)})", file=sys.stderr)
        return 2
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "agent": os.environ.get("MIND_AGENT", "") or "unknown",
        "kind": "verdict",
        "goal_id": args.goal,
        "row_ts": args.row_ts or None,
        "verdict": args.verdict,
        "reason": args.reason.strip(),
        "basis": (args.basis or "").strip() or None,
        "mismatches": [],
        "resolved": {},
    }
    ok = _append_ledger(rec)
    print(json.dumps({"verdict_recorded": ok, "goal_id": args.goal,
                      "verdict": args.verdict, "row_ts": rec["row_ts"],
                      "firings_for_goal": len(fired)}))
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--note", default=None,
                    help="outcome_note text; read from stdin when omitted")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--output", default="json", choices=("json",))
    ap.add_argument("--verdict", default=None, choices=VERDICTS,
                    help="record a disposition for a firing already in the ledger")
    ap.add_argument("--reason", default=None, help="why (required with --verdict)")
    ap.add_argument("--basis", default=None,
                    help="where the evidence is (a path, goal id or board post)")
    ap.add_argument("--row-ts", default=None,
                    help="the firing's ts; omit to mark every firing of the goal")
    args = ap.parse_args()

    if args.verdict:
        return _record_verdict(args)

    note = args.note
    if note is None:
        note = sys.stdin.read() if not sys.stdin.isatty() else ""

    try:
        from gates.shipped_claim import extract_claims  # type: ignore
        claims = extract_claims(note)
        resolved: "dict[str, str | None]" = {}
        content_by_artifact: "dict[str, str | None]" = {}
        backend = _backend() if claims else None
        for claim in claims:
            token = str(claim["artifact"])
            hit_path, hit_text = None, None
            for cand in _resolve_virtual(token):
                text = _read_store_text(backend, cand)
                if text is not None:
                    hit_path, hit_text = cand, text
                    break
            resolved[token] = hit_path
            content_by_artifact[token] = hit_text

        payload = evaluate(goal_id=args.goal, outcome_note=note,
                           content_by_artifact=content_by_artifact)
        payload["resolved"] = resolved
    except Exception as exc:
        print(json.dumps({"fired": False, "goal_id": args.goal,
                          "mismatches": [], "claims_checked": 0,
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 0

    print(json.dumps(payload))
    if payload.get("fired") and not args.no_ledger:
        _log_ledger(args.goal, os.environ.get("MIND_AGENT", ""), payload)
    return 1 if payload.get("fired") else 0


if __name__ == "__main__":
    sys.exit(main())
