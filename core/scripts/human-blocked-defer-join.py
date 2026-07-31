#!/usr/bin/env python3
"""human-blocked-defer-join.py — surface human_blocked defers whose condition has been met.

g-115-3156. The DEFER-CLEARING asymmetry, measured 2026-07-25 (foxtrot, g-115-3053):
defers are WRITTEN by the agent at block time but CLEARED only by a re-probe sweep.
Precheck's defer-recheck (0.5b.4) re-probes defers naming an AGENT-PROVISIONABLE
capability by running the canonical script; credential-defer-recheck (0.5b.9) handles
the credential class the same way. A `human_blocked` defer has NO script to probe --
the thing that satisfies it is a HUMAN MESSAGE arriving on a channel. Nothing joined
inbound messages against the defers they answer, so those defers were effectively
permanent until a human noticed by hand.

Cost of the gap, measured not inferred: the user granted the exact authorization at
14:23 via a relayed board directive naming the commit by SHA. Nothing cleared the
defer. ~8h later the approved work was still unshipped and the goal was ABSENT from
goal-selector's entire candidate list -- a deferred goal is not a candidate, so no
amount of looping would ever surface it. It also manufactured a spurious Investigate
in a second agent's queue (g-115-3118), which was correct about the mechanism and had
no way to see the work was authorized-but-defer-blocked.

DETECTIVE ONLY -- this script NEVER mutates a goal. That is a deliberate design
decision, not an unfinished half, and guard-1249 is the reason: "match the probe to
the DEFER'S PREMISE, not to the resource it names ... never batch-clear several defers
naming the same external resource on a single probe." A keyword join establishes that
a message MENTIONS a goal; it cannot establish that the message GRANTS that goal's
specific blocking condition. Deciding that is semantic judgment, so the sweep surfaces
evidence and a reader decides. The live population proves the hazard is not theoretical:
of 8 human_blocked defers on 2026-07-31, THREE named the same external resource
(`wsl2_localhost_relay_down` on one Studio host) -- exactly the batch-clear trap.
Sibling detective sweeps 0.5b.12 (blocked-signal-resolution) and 0.5b.13
(reclaim-defer-audit) take the same posture for the same reason.

FOUR SIGNALS of THREE different strengths -- read `confidence`, never mere presence.
Two are deterministic and they demand OPPOSITE actions, which is the whole reason the
strength is a field rather than an implicit ordering:

  pq_answered      DETERMINISTIC. The defer cites a `pq-<slug>` and that pending
                   question now reads `answered` or `resolved`. The blocking condition
                   named by the defer has a RECORDED answer. Strongest signal, because
                   the join key was written by the defer's own author.

  pq_retired       DETERMINISTIC, and the INVERSE verdict. The cited question was
                   WITHDRAWN, so the answer this defer waits on is never coming: the
                   clearing path is DEAD, not satisfied. Re-premise the defer or
                   re-file the question. Must never be read as a grant.

  board_directive  HEURISTIC, EVIDENCE-ONLY. A board post newer than the defer names
                   the goal-id in its tags or text. Says a human spoke about this goal
                   AFTER it was blocked -- NOT that they granted the condition. Never
                   act on this alone; open the post.

  pq_missing       NO CONFIDENCE (`none`). The defer cites a `pq-` id that exists in
                   no agent's file. Nothing arrived; the defer's own citation is
                   broken (guard-1197: confirm a block is really filed before trusting
                   it). Ranked BELOW heuristic deliberately -- collapsing it upward
                   made the precheck renderer announce a board post that was never
                   found, which is this sweep manufacturing exactly the kind of
                   confident unsupported claim it exists to catch.

A vacuous zero is the failure mode this sweep must not have (rb-245). If a source
cannot be read, the verdict is `unreadable`, never a clean 0 -- a sweep that reports
"nothing to surface" because it read nothing would hide the exact class it exists to
catch, forever. Always exits 0: it is a precheck detective and must never block the loop.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _rt  # noqa: E402  canonical daemon client
from _dt import parse_naive_iso  # noqa: E402
from _paths import agents_root  # noqa: E402

DEFER_PREFIX = "human_blocked"
LIVE_STATUSES = ("pending", "in-progress", "blocked")
ANSWERED_STATUSES = ("answered", "resolved")
# Deliberately NOT folded into ANSWERED_STATUSES: `retired` means the question was
# WITHDRAWN, which kills the defer's clearing path rather than satisfying it. Both
# are deterministic and both are actionable, but they call for opposite actions.
RETIRED_STATUSES = ("retired",)
# Signal strength ordering. `none` is a real rung, not a filler: it is what
# `pq_missing` carries, and collapsing it into `heuristic` makes a reader (or a
# renderer) claim evidence arrived when none did.
_CONF_RANK = {"deterministic": 2, "heuristic": 1, "none": 0}

# pq ids are freeform slugs after the `pq-` stem. `.` and `-` are LEGAL INSIDE an id
# (`pq--ci-repo-secret-swap`), which is exactly why the match must be
# right-trimmed: a defer sentence ending "...(pq-fox-wsl-relay-restart)." or
# "... pq-fox-wsl-relay-restart." otherwise captures the trailing period as part of
# the id, the lookup misses, and the sweep emits a CONFIDENT `pq_missing` — a false
# guard-1197 finding asserting the human's pending question was never filed. Caught
# on this script's first live run: 3 of 8 defers reported missing; the id was real
# and one `.` wide. A negative claim produced by a parser is still a negative claim
# (verify-before-assuming.md) — the second signal was a plain grep of the pq files.
PQ_RE = re.compile(r"\bpq-[A-Za-z0-9][A-Za-z0-9_.-]*")
_PQ_TRAILING = ".-_,;:)]}"
# A defer's premise "resource" for the guard-1249 shared-premise grouping: the first
# snake_case-ish token after the prefix, which is how these defers are conventionally
# written (`human_blocked: wsl2_localhost_relay_down on the Studio host`).
PREMISE_RE = re.compile(r"^human_blocked:\s*([a-z0-9]+(?:_[a-z0-9]+){1,})")


def _read_goals(source: str) -> tuple[list[dict], str | None]:
    """Return (goals, error). Never raises -- the caller renders `unreadable`."""
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:  # noqa: BLE001
        return [], f"{source} read failed: {e.body or e}"
    data = _rt.tolerant_decode_aggregate(f"[human-blocked-defer-join] {source}", out)
    if data is None:
        return [], f"{source} decode failed"
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals, None


def _read_pending_questions() -> tuple[dict[str, str], str | None]:
    """Map pq-id -> status across EVERY agent, not just the bound one.

    Routed through agents_root() per the CLAUDE.md cross-agent-glob contract: a
    depth-1 glob matches nothing post-relocation and would silently return {},
    which this sweep would then render as "no pq answered" -- a vacuous zero.
    """
    try:
        import yaml  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return {}, f"pyyaml unavailable: {e}"
    try:
        paths = sorted(agents_root().glob("*/session/pending-questions.yaml"))
    except Exception as e:  # noqa: BLE001
        return {}, f"agents_root glob failed: {e}"
    if not paths:
        return {}, "no pending-questions.yaml found under agents_root"
    out: dict[str, str] = {}
    for p in paths:
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001, S112
            continue  # one unreadable agent file must not blank the whole map
        rows = doc.get("questions") if isinstance(doc, dict) else doc
        for r in rows or []:
            if isinstance(r, dict) and r.get("id"):
                out[str(r["id"])] = str(r.get("status") or "")
    return out, None


def _read_board(channels: list[str], since: str) -> tuple[list[dict], str | None]:
    """Board posts across the named channels via the daemon (the canonical path)."""
    msgs: list[dict] = []
    errs: list[str] = []
    for ch in channels:
        try:
            raw = _rt.rt_call("GET", "/v1/board/read",
                              query=f"channel={ch}&since={since}&json=1")
        except _rt.RtError as e:  # noqa: BLE001
            errs.append(f"{ch}: {e.body or e}")
            continue
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return msgs, ("; ".join(errs) if errs else None)


def _match_board(goal_id: str, defer_set_at: str | None, msgs: list[dict]) -> list[dict]:
    """Board posts NEWER than the defer that name this goal in tags or text."""
    cutoff = parse_naive_iso(defer_set_at) if defer_set_at else None
    hits = []
    for m in msgs:
        tags = [str(t) for t in (m.get("tags") or [])]
        if goal_id not in tags and goal_id not in str(m.get("text") or ""):
            continue
        if cutoff is not None:
            ts = parse_naive_iso(m.get("timestamp"))
            if ts is not None and ts <= cutoff:
                continue  # predates the defer -- cannot have satisfied it
        hits.append({
            "id": m.get("id"), "author": m.get("author"), "type": m.get("type"),
            "channel": m.get("channel"), "timestamp": m.get("timestamp"),
            "text": str(m.get("text") or "")[:180],
        })
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="168h",
                    help="board lookback window (default 168h)")
    ap.add_argument("--channels", default="decisions,coordination,general",
                    help="comma-separated board channels to join against")
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args()

    errors: list[str] = []
    goals: list[dict] = []
    for src in ("world", "agent"):
        g, err = _read_goals(src)
        if err:
            errors.append(err)
        goals.extend(g)

    deferred = [
        g for g in goals
        if g.get("status") in LIVE_STATUSES
        and str(g.get("defer_reason") or "").startswith(DEFER_PREFIX)
    ]

    pq_status, pq_err = _read_pending_questions()
    if pq_err:
        errors.append(pq_err)
    msgs, board_err = _read_board([c.strip() for c in args.channels.split(",") if c.strip()],
                                  args.since)
    if board_err:
        errors.append(board_err)

    # guard-1249: group by shared premise resource so nobody batch-clears a cluster.
    premise_counts: dict[str, int] = {}
    for g in deferred:
        m = PREMISE_RE.match(str(g.get("defer_reason") or ""))
        if m:
            premise_counts[m.group(1)] = premise_counts.get(m.group(1), 0) + 1

    records = []
    for g in deferred:
        reason = str(g.get("defer_reason") or "")
        gid = str(g.get("id"))
        signals = []

        cited = [m.rstrip(_PQ_TRAILING) for m in PQ_RE.findall(reason)]
        for pq in dict.fromkeys(p for p in cited if len(p) > 3):
            st = pq_status.get(pq)
            if st is None:
                signals.append({"signal": "pq_missing", "confidence": "none", "pq": pq,
                                "detail": "cited pending-question not found in any "
                                          "agent's file (guard-1197: verify it is "
                                          "actually filed before trusting the block)"})
            elif st in ANSWERED_STATUSES:
                signals.append({"signal": "pq_answered", "confidence": "deterministic",
                                "pq": pq, "pq_status": st,
                                "detail": f"cited pending-question is now {st}"})
            elif st in RETIRED_STATUSES:
                # A DEAD CLEARING PATH, not a satisfied condition -- the opposite
                # verdict from pq_answered, and it must never be read as one. The
                # question this defer waits on was withdrawn, so the answer it names
                # is never coming and the defer is frozen permanently. Found on the
                # first live run: 3 defers cite the fleet's ONE retired pq, and
                # because `retired` sits in neither the answered nor the missing
                # branch they produced no signal at all -- silently falling through
                # the very sweep written to catch defers with no clearing path.
                # Re-derive the premise or re-file the question; do not just clear.
                signals.append({"signal": "pq_retired", "confidence": "deterministic",
                                "pq": pq, "pq_status": st,
                                "detail": "cited pending-question was RETIRED — the "
                                          "stated clearing path no longer exists, so "
                                          "this defer can never be satisfied as "
                                          "written; re-premise it or re-file the "
                                          "question (do NOT read this as granted)"})

        hits = _match_board(gid, g.get("defer_reason_set_at"), msgs)
        if hits:
            signals.append({"signal": "board_directive", "confidence": "heuristic",
                            "posts": hits[:4], "post_count": len(hits),
                            "detail": "board post(s) newer than the defer name this "
                                      "goal -- evidence a human spoke about it, NOT "
                                      "proof the condition was granted; open the post"})

        if not signals:
            continue

        pm = PREMISE_RE.match(reason)
        premise = pm.group(1) if pm else None
        rec = {
            "goal_id": gid, "source": g.get("_source"),
            "aspiration_id": g.get("_aspiration_id"),
            "status": g.get("status"), "intended_agent": g.get("intended_agent"),
            "title": str(g.get("title") or "")[:110],
            "defer_reason": reason[:240],
            "defer_reason_set_at": g.get("defer_reason_set_at"),
            "premise_resource": premise,
            "shared_premise_count": premise_counts.get(premise, 1) if premise else 1,
            "signals": signals,
        }
        # Rank by the STRONGEST signal actually present. The earlier form was a
        # deterministic/heuristic binary, which mislabelled the one case that has
        # NEITHER: a record whose only signal is `pq_missing` (confidence "none")
        # had nothing arrive at all -- its defer merely cites a question that does
        # not exist. Calling that "heuristic" made the precheck renderer announce
        # a board post that was never found, i.e. this sweep manufacturing a
        # confident claim about evidence it never saw -- the exact failure class
        # the sweep exists to catch, turned on itself. Caught by the mandated
        # re-read of the phase pseudocode (pre-completion-review rule 1).
        rec["best_confidence"] = max((s["confidence"] for s in signals),
                                     key=lambda c: _CONF_RANK.get(c, 0))
        records.append(rec)

    records.sort(key=lambda r: (-_CONF_RANK.get(r["best_confidence"], 0), r["goal_id"]))

    # A read failure must never render as a clean zero (rb-245).
    verdict = "unreadable" if errors and not deferred else ("hits" if records else "clean")
    result = {
        "verdict": verdict,
        "scanned": len(goals),
        "human_blocked_defers": len(deferred),
        "records": records,
        "deterministic_count": sum(1 for r in records
                                   if r["best_confidence"] == "deterministic"),
        "shared_premise_clusters": {k: v for k, v in premise_counts.items() if v > 1},
        "errors": errors,
        "mutates": False,
    }

    if args.output == "json":
        print(json.dumps(result, indent=2))
        return 0

    if errors:
        print(f"[human-blocked-defer-join] WARN unreadable source(s): {'; '.join(errors)}",
              file=sys.stderr)
    print(f"[human-blocked-defer-join] {len(deferred)} human_blocked defer(s) of "
          f"{len(goals)} goals examined; {len(records)} with an arriving signal "
          f"({result['deterministic_count']} deterministic) verdict={verdict}")
    for k, v in result["shared_premise_clusters"].items():
        print(f"    NOTE {v} defers share premise '{k}' — guard-1249: probe each "
              f"premise separately, never batch-clear the cluster")
    for r in records:
        print(f"  {r['goal_id']:<12} {r['source']:<6} [{r['best_confidence']}] {r['title']}")
        print(f"      defer: {r['defer_reason'][:100]}")
        for s in r["signals"]:
            print(f"      - {s['signal']} ({s['confidence']}): {s['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
