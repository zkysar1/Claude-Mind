#!/usr/bin/env python3
"""completed-not-closed-slate.py — the per-iteration DRAIN slate ().

Prints the OLDEST few goals THIS agent owns that are still OPEN (`in-progress`
or `pending`, non-recurring, no `defer_reason`) yet already carry closure
evidence (`outcome_note`), so the reducer can dispose of them — close,
release-with-defer, or hold — as a bounded obligation at the top of every
iteration (aspirations-precheck Phase 0.5g.7). It is a SLATE, not a verdict.

POPULATION PREDICATE (widened 2026-08-16 — the "predicate gap")
---------------------------------------------------------------
The first cut read ONLY `in-progress` goals whose `claimed_by` was this agent.
That is narrower than the path that CREATES the population (guard-1802 class):
a worker's unit ends at `pending` as often as at `in-progress` (its own claim
released by stranded-claim-sweep, or a pre-4a Body that never wrote status),
and a released row keeps its outcome_note but loses its holder — so the lane
reported clean while zeta measured (cc-02, msg-20260816-195023-zeta-5111) 220
pending goals carrying an outcome_note against a detector population of 7.
Re-measured here the same day: of those, 75 are RECURRING (a note between
cycles is normal) and 118 carry a `defer_reason` (parked on purpose — the
defer/precondition lanes own them); the invisible remainder was 27, 25 of them
unclaimed with `executed_by` set. So the widened predicate is:

  status in {in-progress, pending} AND not recurring AND outcome_note
  AND no defer_reason;  "mine" = claimed_by == me
                        OR (unclaimed AND executed_by == me)

THAT PREDICATE HAS NO EXIT ON ITS OWN, and the hold TTL does not give it one
(g-115-7000). `outcome_note` on an OPEN goal is very often a PROGRESS note, not
a completion note, and nothing in the record distinguishes them — so a row
correctly judged not-cnc re-qualifies the moment its 24h lease lapses, forever.
Measured 2026-08-27 (zeta, cc-02): the `(unattributed)` lane served 3 rows, all
re-serves, 13 prior dispositions between them, every one reaching the same
verdict, while 14 never-looked-at rows queued behind them.

The exit is a CONTENT-KEYED hold: `--hold` stamps `note_sha`, the digest of the
note it was judged against, and `build_slate` suppresses that row only while the
note is UNCHANGED. Because `outcome_note` is REPLACED wholesale rather than
appended (guard-1691), any rewrite — including the completion note this lane
exists to catch — changes the digest and resurfaces the row on the NEXT run,
with no clock to wait out. Suppressed rows are counted
(`mine_held_back_note_unchanged`) and named in the report line, so this is a
visible keep, never the silent one guard-3628 warns about. Pre-existing holds
carry no `note_sha` and behave exactly as before.

`executed_by` is stamped at claim time (E1) and survives release, so an
unclaimed noted row still names the reducer that has the context to judge it;
a row with neither field is counted (`(unattributed)`) and offered to nobody.
Cost: the pending query is ~11 MB / ~2 s per iteration on top of the ~30 KB
in-progress one — accepted, because a lane that cannot see its population is
worth nothing at any speed.

WHY THIS EXISTS BESIDE completed-not-closed-triage.py
-----------------------------------------------------
The triage lane is correct and stays: it is the DEEP census (dead-carrier
resolution through stranded-claim-sweep, fleet denominator, trend, board post),
fires on a completed-goal cadence, and costs a multi-minute sweep. That is the
wrong shape for an obligation that must run EVERY iteration and cost ~1s:
measured 2026-08-16, the cadence lane fired, printed 20 rows and posted to the
board while the population went 305 -> 338 -> 360 of 361 open alpha claims, and
the recurring drain goal (g-115-6337, 12h) lost in the selector to fresher work.
A slate the loop is OBLIGED to act on, bounded to a few rows, is the missing
consumer. Same guard-4000 class as the reaper it drains: a keep that never
consults age grows without bound.

WHAT IT DOES NOT DO — DELIBERATELY
----------------------------------
* No verdict. The note's own first line is printed verbatim (guard-2852c: LENGTH
  IS NOT VERDICT); a note-text classifier was measured at 58% false-positive on
  this exact corpus and 22 of 423 positive-verdict heads also said "NOT DONE".
  The disposition is the LLM's, per goal, after reading the record.
* No mutation. There is no --apply and must never be one; the closer is the
  canonical writer (aspirations-complete-by.sh --key-finding, or
  aspirations-release.sh + a precondition_unmet: defer).
* No dead-carrier resolution. Liveness is expensive and the age gate does the
  same job for an obligation: since worker-loop Phase 4a a live Body closes its
  own unit at end of unit, so a noted goal still open after `min_claim_age_hours`
  is either pre-fix backlog, a crashed Body, or a Phase 3.7 STRANDED hold — and
  the note says which. The reducer's OWN in-flight goal is excluded by SID.

READING THE OUTPUT
------------------
`population` is the FULL count, `slate` is the bounded batch, `dropped` is the
difference (guard-3830: a batch bound must never read as a scan result). A zero
slate with a non-zero population means the age gate is holding fresh rows back,
not that the backlog is gone.

TWO SUB-COMMANDS ADDED BY THE 2026-08-16 FRESH-EYES REVIEW
----------------------------------------------------------
* `--show <goal-id> [--note-from N] [--note-chars N]` — a COMPACT read of one
  record (status / holder / verification / description head / the note PAGED),
  never the raw record. The first drain protocol told the reducer to re-read
  each slate row with `aspirations-query.sh --full`; a triage agent following
  exactly that died of autocompact thrash the same day (10k+ chars per goal,
  three per iteration, twice each). Reading is the reducer's cost centre here,
  so the reader is part of the instrument.
* `--hold <goal-id> --reason "<why>"` — records a HOLD in a per-agent ledger
  (`agents/<agent>/session/cnc-drain-holds.jsonl`). Without it a HOLD wrote
  nothing, so the slate — oldest-first, bounded to 3 — re-served the same held
  rows every iteration and three permanent holds (a Phase 3.7 STRANDED goal is
  one) starved every row behind them forever. A held row is held back for
  `hold_ttl_hours` (config, default 24) and then RESURFACES with its
  `hold_count` shown: the hold is a lease with a release path (guard-3419), not
  a permanent exclusion, and a third hold on one goal is the signal to file an
  Investigate rather than hold again. Report-only w.r.t. goal RECORDS still
  holds: the ledger is the reducer's own bookkeeping and never touches a goal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

_DEFAULT_PER_ITERATION = 3
_DEFAULT_MIN_CLAIM_AGE_HOURS = 6.0
_DEFAULT_HOLD_TTL_HOURS = 24.0
_HOLD_LEDGER_RETENTION_DAYS = 30
_HOLDS_BASENAME = "cnc-drain-holds.jsonl"
_CONFIG_BLOCK = "completed_not_closed_drain"
_NOTE_HEAD_CHARS = 240
_GOAL_ID_RE = re.compile(r"\bg-\d{3}-\d{2,4}\b")


def _load_config() -> Dict[str, Any]:
    """`completed_not_closed_drain` block from aspirations.yaml, soft defaults.

    Prints the reason on a failed read — a knob that silently falls back is a
    knob that advertises a tuning it does not perform.
    """
    cfg: Dict[str, Any] = {"per_iteration": _DEFAULT_PER_ITERATION,
                           "min_claim_age_hours": _DEFAULT_MIN_CLAIM_AGE_HOURS,
                           "hold_ttl_hours": _DEFAULT_HOLD_TTL_HOURS}
    try:
        import yaml
        from _paths import PROJECT_ROOT
        path = os.path.join(str(PROJECT_ROOT), "core", "config", "aspirations.yaml")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                block = (yaml.safe_load(f) or {}).get(_CONFIG_BLOCK) or {}
            cfg["per_iteration"] = int(block.get("per_iteration", cfg["per_iteration"]))
            cfg["min_claim_age_hours"] = float(
                block.get("min_claim_age_hours", cfg["min_claim_age_hours"]))
            cfg["hold_ttl_hours"] = float(
                block.get("hold_ttl_hours", cfg["hold_ttl_hours"]))
    except Exception as e:  # noqa: BLE001 — observability instrument, never a gate
        print(f"[cnc-slate] config read failed, using defaults: {e}", file=sys.stderr)
    return cfg


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except (TypeError, ValueError):
        return None


def note_head(note: str, limit: int = _NOTE_HEAD_CHARS) -> str:
    """The note's OWN first non-empty line, truncated for display.

    First line, not first N chars of the blob: authors put the verdict on line
    one and the narrative below it, and a mid-line cut can end on a topic word
    that reads like a verdict word.
    """
    for line in (note or "").splitlines():
        s = line.strip()
        if s:
            return s if len(s) <= limit else s[: limit - 1] + "…"
    return ""


def cross_record_ids(note: str, own_id: str) -> List[str]:
    """Other goal ids the note names — cheap hint for the guard-3824/3880 check.

    A note that says a residue was FILED AS / FOLDED INTO / RELAYED TO another
    record is making a claim about a DIFFERENT record, which a note head cannot
    settle; the closer must open the named record before letting the claim carry
    a close. This only surfaces the ids; it decides nothing.
    """
    seen: List[str] = []
    for gid in _GOAL_ID_RE.findall(note or ""):
        if gid != own_id and gid not in seen:
            seen.append(gid)
    return seen


def _note_sha(note: str) -> str:
    """Stable digest of an outcome_note, for content-keyed holds ().

    Whitespace-normalised so a reflow is not mistaken for new evidence. Empty
    note -> "" (never a hold key: `is_drain_candidate` already excludes it)."""
    n = " ".join((note or "").split())
    if not n:
        return ""
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:16]


DRAIN_STATUSES = ("in-progress", "pending")


def is_drain_candidate(g: Dict[str, Any]) -> bool:
    """The widened population predicate — SSOT for this lane (module docstring).
    Open (in-progress/pending), non-recurring, carries an outcome_note, and is
    NOT parked behind a defer_reason (a released+deferred row is deliberate;
    the defer/precondition lanes own it and re-probe it)."""
    if (g.get("status") or "") not in DRAIN_STATUSES:
        return False
    if g.get("recurring"):
        return False
    if not (g.get("outcome_note") or "").strip():
        return False
    if (g.get("defer_reason") or "").strip():
        return False
    return True


def holder_of(g: Dict[str, Any]) -> str:
    """Who is expected to drain this row: the claim holder; for an UNCLAIMED
    row the agent that executed it (`executed_by`, stamped at claim time and
    kept across release); neither -> "(unattributed)"."""
    return (g.get("claimed_by") or g.get("executed_by") or "(unattributed)")


def build_slate(rows: List[Dict[str, Any]], agent: str, *, limit: int,
                min_age_hours: float, now: datetime,
                own_sid: str = "",
                holds: Optional[List[Dict[str, Any]]] = None,
                hold_ttl_hours: float = _DEFAULT_HOLD_TTL_HOURS) -> Dict[str, Any]:
    """Pure: filter + rank + bound. `rows` is aspirations-query --full output.

    Population counts are computed BEFORE the age gate and the bound so the
    report always carries the whole backlog beside the batch it hands over.

    `holds` is the hold ledger (list of {goal_id, held_at, reason}). A row whose
    most recent hold is younger than `hold_ttl_hours` is held back and COUNTED
    (`mine_held_back_recent_hold`); older holds expire and the row resurfaces
    carrying `hold_count` + `last_hold_reason` so a repeat is visible.
    """
    hold_by_goal: Dict[str, List[Dict[str, Any]]] = {}
    for h in holds or []:
        gid = h.get("goal_id") if isinstance(h, dict) else None
        if gid:
            hold_by_goal.setdefault(gid, []).append(h)
    fleet_noted = 0            # widened population (see module docstring)
    fleet_noted_in_progress = 0
    fleet_noted_pending = 0
    mine_noted: List[Dict[str, Any]] = []
    mine_noted_in_progress = 0
    # Per-holder view of the FLEET population (2026-08-16 review, "completions
    # across agents"): the drain is holder-scoped by design (the holder's
    # reducer judges its own units), so a DORMANT or RETIRED holder's finished
    # work has no drainer at all — stranded-claim-sweep KEEPs noted claims and
    # SKIP_STATUSES hides them from every selector. Precheck 0.5g.7 reads this
    # to decide whether to run the slate `--agent <peer>` for a peer that
    # liveness-check calls dormant/retired. Counted, never acted on, here.
    # An UNCLAIMED row is keyed by its `executed_by` (the reducer with the
    # context to judge it) and counted under `unclaimed`; a row with neither
    # field lands under "(unattributed)" and is offered to nobody.
    by_holder: Dict[str, Dict[str, Any]] = {}
    for g in rows:
        if not is_drain_candidate(g):
            continue
        fleet_noted += 1
        status = g.get("status") or ""
        if status == "in-progress":
            fleet_noted_in_progress += 1
        else:
            fleet_noted_pending += 1
        holder = holder_of(g)
        h = by_holder.setdefault(holder, {"noted": 0, "oldest_claim_age_h": None,
                                          "unclaimed": 0})
        h["noted"] += 1
        if not g.get("claimed_by"):
            h["unclaimed"] += 1
        hts = _parse_ts(g.get("claimed_at")) or _parse_ts(g.get("last_modified"))
        if hts is not None:
            age = round((now - hts).total_seconds() / 3600.0, 1)
            if h["oldest_claim_age_h"] is None or age > h["oldest_claim_age_h"]:
                h["oldest_claim_age_h"] = age
        if holder == agent:
            mine_noted.append(g)
            if status == "in-progress":
                mine_noted_in_progress += 1

    eligible: List[Dict[str, Any]] = []
    held_back_fresh = 0
    held_back_own_sid = 0
    held_back_recent_hold = 0
    held_back_note_unchanged = 0
    note_unchanged_rows: List[Any] = []
    for g in mine_noted:
        sid = g.get("claimed_by_sid") or ""
        if own_sid and sid == own_sid:
            held_back_own_sid += 1
            continue
        ts = _parse_ts(g.get("claimed_at")) or _parse_ts(g.get("last_modified"))
        age_h = (now - ts).total_seconds() / 3600.0 if ts else None
        if age_h is not None and age_h < min_age_hours:
            held_back_fresh += 1
            continue
        gid = g.get("goal_id") or g.get("id") or ""
        recent = None
        for h in hold_by_goal.get(gid, []):
            hts = _parse_ts(h.get("held_at"))
            if hts is not None and (now - hts).total_seconds() / 3600.0 < hold_ttl_hours:
                recent = h
                break
        if recent is not None:
            held_back_recent_hold += 1
            continue
        # CONTENT-KEYED HOLD (). The TTL above is a LEASE, not an EXIT:
        # `is_drain_candidate` cannot tell a completion note from a PROGRESS note,
        # so a row correctly judged not-cnc re-qualifies the moment its lease
        # lapses -- forever, and no amount of correct disposition removes it.
        # Measured 2026-08-27 (zeta, hostname cc-02, uname -r 6.8.0-137-generic):
        # the (unattributed) lane served 3 rows, ALL re-serves, 13 prior
        # dispositions between them, every one reaching the same verdict, while
        # 14 never-looked-at rows queued behind them (oldest-first + bounded).
        #
        # A hold stamped with the note's digest suppresses that row only while the
        # note is UNCHANGED. guard-1691 is what makes this sound: outcome_note is
        # REPLACED wholesale, never appended, so ANY rewrite -- including the
        # completion note this lane exists to catch -- changes the digest and
        # resurfaces the row on the very next run, with no TTL to wait out. That
        # is why this is not "widen the TTL", which the goal explicitly rejects as
        # trading a recurring read for a SILENT one.
        #
        # Suppressed rows stay COUNTED (`mine_held_back_note_unchanged`) and are
        # named individually in the report line + `note_unchanged` payload, so this
        # is never the silent keep guard-3628 warns
        # about. Holds written before this change carry no `note_sha` and fall
        # through to the TTL path unchanged -- no migration, no behaviour change
        # for existing ledgers.
        cur_sha = _note_sha(g.get("outcome_note") or "")
        if cur_sha and any(h.get("note_sha") == cur_sha
                           for h in hold_by_goal.get(gid, [])):
            held_back_note_unchanged += 1
            note_unchanged_rows.append((gid, g))
            continue
        eligible.append((age_h if age_h is not None else float("inf"), g))

    # Oldest claim first; unknown-age rows sort LAST, not first — an unreadable
    # timestamp is not evidence of age.
    eligible.sort(key=lambda t: (t[0] == float("inf"), -t[0] if t[0] != float("inf") else 0))
    slate = []
    for age_h, g in eligible[:limit]:
        gid = g.get("goal_id") or g.get("id") or ""
        note = g.get("outcome_note") or ""
        prior_holds = hold_by_goal.get(gid, [])
        slate.append({
            "goal_id": gid,
            "asp_id": g.get("asp_id"),
            "source": g.get("source") or g.get("goal_source") or "world",
            "title": (g.get("title") or "")[:120],
            # widened predicate: a PENDING row reached the slate via executed_by
            # (its claim was released) — the reducer closes it the same way, but
            # should know it is not holding a claim on it.
            "status": g.get("status") or "",
            "holder_via": "claim" if g.get("claimed_by") else "executed_by",
            "claimed_by_sid": (g.get("claimed_by_sid") or "")[:8],
            "claim_age_h": None if age_h == float("inf") else round(age_h, 1),
            "note_head": note_head(note),
            "cross_record_ids": cross_record_ids(note, gid),
            "hold_count": len(prior_holds),
            "last_hold_reason": (prior_holds[-1].get("reason") or "")[:160] if prior_holds else "",
        })
    return {
        "agent": agent,
        "limit": limit,
        "min_claim_age_hours": min_age_hours,
        "population": {
            # widened totals (the lane's real population) + the strict
            # in-progress halves the first cut reported, kept for consumers.
            "fleet_noted": fleet_noted,
            "fleet_noted_in_progress": fleet_noted_in_progress,
            "fleet_noted_pending": fleet_noted_pending,
            "mine_noted": len(mine_noted),
            "mine_noted_in_progress": mine_noted_in_progress,
            "mine_noted_pending": len(mine_noted) - mine_noted_in_progress,
            "mine_eligible": len(eligible),
            "mine_held_back_fresh": held_back_fresh,
            "mine_held_back_own_sid": held_back_own_sid,
            "mine_held_back_recent_hold": held_back_recent_hold,
            "mine_held_back_note_unchanged": held_back_note_unchanged,
            "note_unchanged_goal_ids": [gid for gid, _ in note_unchanged_rows],
            "by_holder": by_holder,
        },
        "hold_ttl_hours": hold_ttl_hours,
        "slate": slate,
        "dropped": max(0, len(eligible) - len(slate)),
    }


def holds_path(agent: str):
    """Per-agent hold ledger: agents/<agent>/session/cnc-drain-holds.jsonl.

    Agent-wide `session/` (not a per-Body dir): the drain is reducer-only and a
    hold must survive the reducer's own session boundary, or every /start would
    re-serve yesterday's holds. Resolved through _paths.agent_state_dir — never
    PROJECT_ROOT/<agent> by hand (CLAUDE.md "Agent-dir Resolution").
    """
    from _paths import agent_state_dir
    return agent_state_dir(agent) / _HOLDS_BASENAME


def load_holds(path) -> List[Dict[str, Any]]:
    """Read the ledger; a missing or unreadable ledger is an EMPTY hold set (the
    fail-open direction: a lost ledger re-serves rows, it never hides them)."""
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("goal_id"):
                    out.append(rec)
    except OSError:
        return []
    return out


def record_hold(path, *, goal_id: str, reason: str, agent: str, sid: str,
                now: datetime, note_sha: str = "",
                retention_days: int = _HOLD_LEDGER_RETENTION_DAYS) -> Dict[str, Any]:
    """Append one hold and drop entries older than `retention_days` (the ledger
    is bounded by construction — a hold older than any TTL is dead weight).
    Plain locked-free rewrite is acceptable: the ledger has ONE writer (the
    reducer) by the same argument that makes the drain reducer-only."""
    rec = {"goal_id": goal_id, "held_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
           "reason": (reason or "").strip()[:400], "agent": agent, "sid": (sid or "")[:8]}
    # : the digest of the note this hold was JUDGED AGAINST. Omitted
    # (not written empty) when the note could not be read, so the row falls back
    # to the TTL path rather than being suppressed against a digest of nothing.
    if note_sha:
        rec["note_sha"] = note_sha
    keep = []
    for h in load_holds(path):
        hts = _parse_ts(h.get("held_at"))
        if hts is None or (now - hts).days < retention_days:
            keep.append(h)
    keep.append(rec)
    from pathlib import Path as _P
    _P(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for h in keep:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return rec


def _load_one(goal_id: str, timeout: int) -> Optional[Dict[str, Any]]:
    """One goal via the daemon-routed query — the only single-goal shape that
    works is `--goal-field id <id> --full` (`--goal-id`/`--source` are not query
    flags; their usage+rc!=0 must never be read as 'not found')."""
    from _paths import CORE_ROOT, PROJECT_ROOT
    from _runtime_bash import bash_cmd
    script = os.path.join(str(CORE_ROOT), "scripts", "aspirations-query.sh")
    proc = subprocess.run(
        bash_cmd(script, "--goal-field", "id", goal_id, "--full"),
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(f"REFUSING: aspirations-query.sh --goal-field id {goal_id} --full "
                         f"failed (rc={proc.returncode}); stderr tail: {proc.stderr[-400:]}")
    rows = json.loads(proc.stdout)
    if not isinstance(rows, list):
        raise SystemExit("REFUSING: query returned a non-list; shape changed?")
    return rows[0] if rows else None


def render_show(g: Dict[str, Any], *, note_from: int = 0, note_chars: int = 3500,
                holds: Optional[List[Dict[str, Any]]] = None) -> str:
    """Compact, paged view of ONE goal record — the reader half of the drain.

    Fixed fields first (status/holder/source/asp/recurring/outcome_class/
    completed_by, title, key_finding, defer_reason, blocked_by, description
    head, verification outcomes+checks), then progress_note (if any — a
    SEPARATE, often NEWER field that can retract outcome_note), then the
    outcome_note from `note_from`
    for `note_chars` chars with a "re-run with --note-from N" tail. Never the
    raw record: 10k+ chars per goal, three per iteration, twice each, is what
    thrashed a triage agent's context on 2026-08-16.
    """
    def t(v: Any, n: int) -> str:
        v = "" if v is None else str(v).replace("\n", " ")
        return v if len(v) <= n else v[:n] + "…"
    gid = g.get("goal_id") or g.get("id") or ""
    lines = [
        f"id={gid} status={g.get('status')} claimed_by={g.get('claimed_by')} "
        f"sid={(g.get('claimed_by_sid') or '')[:8]} source={g.get('source') or 'world'} "
        f"asp={g.get('asp_id')} recurring={g.get('recurring')} oc={g.get('outcome_class')} "
        f"claimed_at={g.get('claimed_at')} completed_by={g.get('completed_by')}",
        "title: " + t(g.get("title"), 200),
    ]
    if g.get("key_finding"):
        lines.append("key_finding: " + t(g.get("key_finding"), 200))
    if g.get("defer_reason"):
        lines.append("defer_reason: " + t(g.get("defer_reason"), 240))
    if g.get("blocked_by"):
        lines.append("blocked_by: " + t(g.get("blocked_by"), 120))
    prior = [h for h in (holds or []) if h.get("goal_id") == gid]
    if prior:
        lines.append(f"drain_holds: {len(prior)} (last {prior[-1].get('held_at')}: "
                     + t(prior[-1].get('reason'), 160) + ")")
    lines.append("description: " + t(g.get("description"), 700))
    v = g.get("verification") or {}
    if isinstance(v, dict):
        for k in ("outcomes", "checks", "preconditions"):
            items = v.get(k) or []
            if items:
                lines.append(f"verification.{k} ({len(items)}):")
                for i, it in enumerate(items, 1):
                    lines.append(f"  {i}. " + t(it if isinstance(it, str) else json.dumps(it), 220))
    # progress_note is a SEPARATE field that this reader was blind to until
    # 2026-08-29, and it is frequently NEWER than outcome_note — so a complete,
    # correctly-paged read of outcome_note could still be a read of the stale
    # half. Measured that day (zeta, cc-02): 's outcome_note (echo,
    # 08-25) argued "NOT RESOLVED, AND DELIBERATELY SO" while its progress_note
    # (foxtrot, 08-27) recorded the measurement as DONE and the verdict as
    # CONFIRMED with full numbers. Disposing on the note this reader showed
    # produced a 25-day defer on finished work; it was caught only because
    # update-goal echoed the whole record back. guard-4635/guard-5224 already
    # said "read the note in FULL" and did not help — the reader was complete
    # about the wrong field. Fixing the instrument, per guard-1984 (a guardrail
    # cannot outvote the instrument it guards) and guard-4933 (a superseding
    # field must reach every READER of the old one).
    pnote = g.get("progress_note") or ""
    if pnote:
        lines.append(
            f"\u26a0 progress_note PRESENT ({len(pnote)} chars) — a DIFFERENT field from "
            "outcome_note and often NEWER. It can retract the outcome_note outright. "
            "Read it BEFORE disposing; when the two disagree, the newer one governs."
        )
        phi = max(0, note_chars)
        lines.append(f"progress_note: showing [0:{phi}]")
        lines.append(pnote[:phi])
        if len(pnote) > phi:
            lines.append(f"… ({len(pnote) - phi} more progress_note chars — "
                         f"re-run with --note-chars {phi * 2})")
    note = g.get("outcome_note") or ""
    lo, hi = max(0, note_from), max(0, note_from) + max(0, note_chars)
    lines.append(f"outcome_note: total {len(note)} chars; showing [{lo}:{hi}]")
    lines.append(note[lo:hi])
    if hi < len(note):
        lines.append(f"… ({len(note) - hi} more chars — re-run with --note-from {hi})")
    return "\n".join(lines)


def _load_rows(timeout: int) -> List[Dict[str, Any]]:
    """All in-progress AND pending goals via the daemon-routed query
    (authoritative store) — both DRAIN_STATUSES, one query each. Either query
    failing REFUSES the whole slate: an unreadable half is not an empty half."""
    from _paths import CORE_ROOT, PROJECT_ROOT
    from _runtime_bash import bash_cmd  # resolved bash + posix path (guard-581)
    script = os.path.join(str(CORE_ROOT), "scripts", "aspirations-query.sh")
    rows: List[Dict[str, Any]] = []
    for status in DRAIN_STATUSES:
        proc = subprocess.run(
            bash_cmd(script, "--goal-status", status, "--full"),
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise SystemExit(
                f"REFUSING: aspirations-query.sh --goal-status {status} --full "
                f"failed (rc={proc.returncode}). An unreadable store is NOT an empty "
                f"backlog (guard-2298).\nstderr tail: {proc.stderr[-600:]}"
            )
        part = json.loads(proc.stdout)
        if not isinstance(part, list):
            raise SystemExit(f"REFUSING: {status} query returned a non-list; shape changed?")
        rows.extend(part)
    return rows


def _render(result: Dict[str, Any]) -> None:
    pop = result["population"]
    print(f"[cnc-slate] agent={result['agent']} population: fleet_noted="
          f"{pop['fleet_noted']} (in-progress {pop['fleet_noted_in_progress']} / "
          f"pending {pop['fleet_noted_pending']}) mine={pop['mine_noted']} "
          f"(in-progress {pop['mine_noted_in_progress']} / pending {pop['mine_noted_pending']}) "
          f"eligible(>= {result['min_claim_age_hours']:g}h)={pop['mine_eligible']} "
          f"held_back_fresh={pop['mine_held_back_fresh']} "
          f"own_sid={pop['mine_held_back_own_sid']} "
          f"recent_hold(<{result.get('hold_ttl_hours', _DEFAULT_HOLD_TTL_HOURS):g}h)="
          f"{pop.get('mine_held_back_recent_hold', 0)} "
          f"note_unchanged={pop.get('mine_held_back_note_unchanged', 0)} "
          f"| slate={len(result['slate'])} dropped={result['dropped']}")
    _nu = pop.get("note_unchanged_goal_ids") or []
    if _nu:
        print("[cnc-slate] suppressed on an UNCHANGED note (already judged not-cnc; "
              "resurfaces automatically when the note is rewritten — guard-1691): "
              + ", ".join(_nu))
    others = {h: v for h, v in (pop.get("by_holder") or {}).items() if h != result["agent"]}
    if others:
        parts = [f"{h}:{v['noted']}"
                 + (f"[{v['unclaimed']} unclaimed]" if v.get("unclaimed") else "")
                 + f"(oldest {v['oldest_claim_age_h']}h)" for h, v in sorted(others.items())]
        print("[cnc-slate] other holders' noted-open goals: " + ", ".join(parts)
              + " — a DORMANT/RETIRED holder (liveness-check.sh --agent <peer>) has no drainer of its"
              " own: run this slate with --agent <peer> for it (precheck 0.5g.7 peer leg)."
              " Unclaimed rows are keyed by executed_by; '(unattributed)' has no drainer at all.")
    if not result["slate"]:
        if pop["mine_noted"]:
            print("[cnc-slate] slate EMPTY but population non-zero — the age gate is "
                  "holding fresh rows back; this is NOT a drained backlog.")
        else:
            print("[cnc-slate] no completed-not-closed goals held by this agent.")
        return
    for i, r in enumerate(result["slate"], 1):
        age = f"{r['claim_age_h']}h" if r["claim_age_h"] is not None else "age?"
        xr = f" cross-record:{','.join(r['cross_record_ids'])}" if r["cross_record_ids"] else ""
        hc = r.get("hold_count") or 0
        hold = (f" HELD-BEFORE x{hc} (last: {r.get('last_hold_reason')})"
                if hc else "")
        via = (f" {r.get('status') or '?'}/via-{r.get('holder_via') or '?'}")
        print(f"  {i}. {r['goal_id']} [{r['asp_id']}/{r['source']}]{via} sid={r['claimed_by_sid'] or '-'} "
              f"age={age}{xr}{hold}\n     title: {r['title']}\n     note_head: {r['note_head']}")
    print("[cnc-slate] DISPOSE EACH (LLM judgment, never a predicate). READ with "
          "`completed-not-closed-slate.sh --show <goal-id>` (paged; never --full). CLOSE via "
          "aspirations-complete-by.sh --key-finding + update-goal outcome_class | RELEASE + "
          "precondition_unmet: defer | HOLD via `--hold <goal-id> --reason ...` (held back "
          f"{result.get('hold_ttl_hours', _DEFAULT_HOLD_TTL_HOURS):g}h, then resurfaces with its "
          "count — a 3rd hold means file an Investigate). Report consumed == closed + released + held.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Per-iteration completed-not-closed DRAIN slate. Report-only; "
                    "no --apply by design.")
    parser.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""),
                        help="Holder whose claims are enumerated (default MIND_AGENT).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Rows handed over (default: aspirations.yaml "
                             f"{_CONFIG_BLOCK}.per_iteration, else {_DEFAULT_PER_ITERATION}).")
    parser.add_argument("--min-age-hours", type=float, default=None,
                        help="Claim age below which a row is held back (default: config "
                             f"{_CONFIG_BLOCK}.min_claim_age_hours, else "
                             f"{_DEFAULT_MIN_CLAIM_AGE_HOURS:g}).")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show", metavar="GOAL_ID", default=None,
                        help="Compact paged view of ONE record (the reducer's reader; "
                             "never dumps the raw record).")
    parser.add_argument("--note-from", type=int, default=0,
                        help="--show: outcome_note offset (page a long note).")
    parser.add_argument("--note-chars", type=int, default=3500,
                        help="--show: outcome_note chars per page.")
    parser.add_argument("--hold", metavar="GOAL_ID", default=None,
                        help="Record a HOLD for this goal in the per-agent ledger "
                             "(held back for hold_ttl_hours, then resurfaces with its count).")
    parser.add_argument("--reason", default="",
                        help="--hold: the one-line reason (required).")
    args = parser.parse_args()

    if not args.agent:
        print("[cnc-slate] no agent (pass --agent or set MIND_AGENT)", file=sys.stderr)
        return 2
    cfg = _load_config()
    # THE HOLD LEDGER IS KEYED BY THE ACTING AGENT, NEVER BY THE QUERIED HOLDER
    # (). `args.agent` is the holder whose ROWS are enumerated; on the
    # peer leg and the "(unattributed)" lane that is somebody else, and pairing a
    # read on one lane with a write on another made the hold inert everywhere but
    # the agent's own lane — the write half worked, so the hold_count incremented
    # and the suppression silently did nothing. Deriving BOTH branches from one
    # `acting` value is the fix: read path and write path can no longer diverge,
    # which is a property of the code rather than of the caller's arg shape.
    # Do NOT "fix" this by passing --agent to --hold as well: record_hold
    # mkdir(parents=True)s its ledger's parent, so that route manufactures
    # `agents/(unattributed)/session/` for a bucket key that is not an agent —
    # the L1 cruft class .claude/rules/path-resolution.md exists to prevent.
    # Falls back to args.agent when MIND_AGENT is unset: no other identity
    # exists there, and the read==write invariant still holds.
    acting = os.environ.get("MIND_AGENT", "").strip() or args.agent
    hpath = holds_path(acting)
    if args.hold:
        if not args.reason.strip():
            print("[cnc-slate] --hold requires --reason \"<why>\" (a hold is a decision, "
                  "and the reason is what makes the third one actionable)", file=sys.stderr)
            return 2
        # `agent` is provenance for WHO DECIDED, so it is the acting agent too —
        # not the queried holder. Stamping args.agent here would record a hold as
        # made by "(unattributed)", which is not an agent and decided nothing.
        # Read the note this hold is being judged against so the hold can be
        # CONTENT-KEYED (). Fail-open: an unreadable goal writes a
        # plain TTL hold exactly as before -- a hold must never be blocked by a
        # daemon hiccup, and a missing digest degrades to the old behaviour.
        _hg = _load_one(args.hold, args.timeout)
        _sha = _note_sha((_hg or {}).get("outcome_note") or "")
        # Sampled BEFORE the write on purpose: record_hold appends to the ledger
        # and load_holds re-reads it from disk, returning fresh dicts — so after
        # the append there is no way to exclude the hold just written, and the
        # check would report "prior" for its own row every time.
        _prior_keyed = any(h.get("note_sha") for h in load_holds(hpath)
                           if h.get("goal_id") == args.hold)
        rec = record_hold(hpath, goal_id=args.hold, reason=args.reason, agent=acting,
                          sid=os.environ.get("MIND_SID", ""), now=datetime.now(),
                          note_sha=_sha)
        n = sum(1 for h in load_holds(hpath) if h.get("goal_id") == args.hold)
        _kind = (f"content-keyed on note {_sha} — resurfaces the moment the note "
                 f"CHANGES, not on a clock" if _sha else
                 f"TTL-only ({cfg['hold_ttl_hours']:g}h) — note unreadable, no digest stamped")
        print(f"[cnc-slate] HOLD recorded for {args.hold} (hold #{n}; {_kind}; "
              f"ledger {hpath}) reason: {rec['reason']}")
        if n >= 3:
            # The "a repeat hold means the note was REWRITTEN" reading is only
            # true once a PRIOR hold on this goal carried a digest. During the
            #  migration window it is usually FALSE, and false exactly
            # where it is loudest: the no-migration choice (holds predating the
            # change carry no note_sha) helps the LONGEST-SERVING rows LAST, so
            # the rows with the most prior holds are the ones whose holds have no
            # digest at all. Measured 2026-08-28 (zeta, hostname cc-02, uname -r
            # 6.8.0-137-generic) while executing : live ledger 81 holds,
            # 6 with note_sha; the three lane rows read holds=6/4/6 with sha=0/0/0
            # and went to #7/#5/#7. Telling that reader to "read the note diff"
            # sends them after a rewrite that never happened. ()
            if _prior_keyed:
                print(f"[cnc-slate] {args.hold} has now been held {n}x — and since holds "
                      "became content-keyed (g-115-7000) a REPEAT hold means the note was "
                      "REWRITTEN between judgements, not that the row recycled on a clock. "
                      "That is a different signal: read the note diff before holding again, "
                      "and file an Investigate if the rewrites are not converging.")
            else:
                print(f"[cnc-slate] {args.hold} has now been held {n}x, but NO prior hold "
                      "carried a digest — these are pre-g-115-7000 TTL holds, so the count "
                      "records a CLOCK recycle, not note rewrites. Nothing to diff. This "
                      "hold is the row's first content-keyed one, so it is the LAST: the "
                      "row exits the lane until its note actually changes.")
        return 0
    if args.show:
        g = _load_one(args.show, args.timeout)
        if g is None:
            print(f"[cnc-slate] {args.show}: NOT FOUND (empty result — check the id)")
            return 3
        print(render_show(g, note_from=args.note_from, note_chars=args.note_chars,
                          holds=load_holds(hpath)))
        return 0
    limit = args.limit if args.limit is not None else cfg["per_iteration"]
    min_age = (args.min_age_hours if args.min_age_hours is not None
               else cfg["min_claim_age_hours"])
    rows = _load_rows(args.timeout)
    result = build_slate(rows, args.agent, limit=limit, min_age_hours=min_age,
                         now=datetime.now(), own_sid=os.environ.get("MIND_SID", ""),
                         holds=load_holds(hpath), hold_ttl_hours=cfg["hold_ttl_hours"])
    if args.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        _render(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
