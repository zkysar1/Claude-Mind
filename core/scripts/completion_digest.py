#!/usr/bin/env python3
"""completion_digest.py -- the USER-FACING digest that the completion report emails.

Why this exists (user, 2026-08-17): "I do like receiving what goals are blocked or
assigned to me through the completion report email ... make them easier to read,
and be sure everything is in there I need to quickly understand how it has been
going ... as long as I receive one every day or two that is good."

The on-disk COMPLETION-REPORT.md is written BY an agent FOR agents -- forensic,
trap-numbered, thousands of words before the first fact the user cares about,
and it never LISTS the goals that need him (it says "52 goals carry `user`").
This script builds the email instead: short, deterministic, ordered by what the
reader needs first, with the specific items named. Framework scripts read the
stores here; the LLM never does. Domain-free: no transport, no product names.

Order (the asks first -- the user's 2026-08-03 feedback on the digest lane was
that an email opening with our own archaeology "caused anxiety"):

  1. TL;DR            5 lines max
  2. Needs you        goals with `user` in participants (SSOT predicate shared
                      with audit-user-to-agent.py + the 72h digest), human-gated
                      defers, and open pending questions -- with the NEEDS FROM
                      YOU line and age, oldest first
  3. Blocked          what is stuck, why, and what it holds up
  4. Done             this window, by agent and by aspiration
  5. In progress      active aspirations, progress fractions
  6. Outcome + health fleet pulse, hypotheses, product signals (when configured)
  7. Notes            optional agent-written lines (--notes-file), bounded

Usage:
  completion-digest.sh --agent alpha [--since ISO] [--notes-file F] [--out F]
                       [--world DIR] [--max-items N] [--json]
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _paths import WORLD_DIR, PROJECT_ROOT, agents_root  # noqa: E402

# The owner-decided park exemption () — the SAME predicate the 72h
# escalation digest uses. Two consumers put human-gated goals in front of the
# owner; guard-4015's measured corollary is that two correct-looking copies of
# one exemption set diverge in their MATCHING SEMANTICS, not their inputs, so
# the predicate is imported here rather than re-derived (guard-2275).
# GUARDED: this script emails the owner and must never die on an import; the
# fallback returns None, which renders every human-gated goal exactly as before.
# The degraded mode is ANNOUNCED, not silent — a dead predicate here would render
# every park as an ordinary ask, i.e. the pre-fix behaviour restored on the
# owner-facing report, and a reader could not tell that from "the owner has no
# parked goals". Same shape as `owner_decided_predicate_loaded` in the 72h
# escalation check, so the two consumers of this predicate degrade alike.
try:
    from gates.owner_decided_park import owner_decided_ref  # noqa: E402
    _OWNER_DECIDED_LOADED = True
except Exception as _exc:  # noqa: BLE001
    sys.stderr.write(
        "completion-digest: could not import gates.owner_decided_park (%s) — "
        "fail-open, owner-decided parks will render as ordinary asks\n" % (_exc,))

    def owner_decided_ref(goal):  # type: ignore[misc]
        return None

    _OWNER_DECIDED_LOADED = False

TERMINAL = {"completed", "skipped", "expired", "archived", "retired"}
BATCH_CLOSE_MIN = 30  # >= this many closes by one session inside ~10 min = a batch close


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def _ts(s):
    try:
        return datetime.fromisoformat(str(s)[:19])
    except Exception:
        return None


def _hours(a: datetime | None, b: datetime | None):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 1)


def _age_str(h) -> str:
    if h is None:
        return "age unknown"
    if h < 48:
        return f"{int(h)}h"
    return f"{int(h // 24)}d"


def _load_jsonl_checked(path: Path, *, count_lines: bool = True) -> tuple[list, bool]:
    """Return (rows, read_ok). `read_ok` is False when the file is MISSING or when
    any line failed to parse -- so a caller can announce the degradation instead of
    rendering the resulting [] as an affirmative zero (guard-160: do not synthesize
    a default for a read the contract says must succeed).

    The distinction rb-2073 requires is between FAILED-TO-READ and legitimately
    empty, NOT between empty and non-empty: a file that exists and parses cleanly is
    `ok` even when it yields zero rows. That is deliberate -- a genuinely empty world
    must still be able to render its all-clear. It also means the one case this flag
    CANNOT catch is a present-but-transiently-empty read on the synced mount
    (rb-2970); nothing readable from the file alone separates that from a new world.
    """
    if not path.exists():
        return [], False
    try:
        from _fileops import read_jsonl_with_recovery  # noqa: WPS433

        # COUNT FIRST, READ SECOND. The ORDER is the fix (), and it is
        # the whole fix -- no lock, no slack, no new machinery.
        #
        # `ok` compares two observations of a file NOTHING here locks (grep for
        # with_lock|locked_|flock|fcntl over this function, and over
        # read_jsonl_with_recovery in _fileops.py: zero hits in both), and five
        # agents append to world/aspirations.jsonl continuously. So the two
        # observations can legitimately disagree, and the only thing this code gets
        # to choose is WHICH WAY the disagreement leans (guard-2537: when two views
        # of one thing arrive by different paths, the tolerance must be DIRECTIONAL).
        #
        # Read-then-count leaned the wrong way. One peer append between the two made
        # `rows` the OLD short list and `n_lines` the NEW long count, so 100 >= 101
        # was False and the digest told the owner "the aspirations store READ FAILED"
        # about a store that read perfectly -- on ordinary fleet activity, i.e. the
        # one thing guaranteed to keep happening.
        #
        # Count-then-read leans safe on BOTH concurrent mutations:
        #   append between -> n_lines old+small, rows new+big  -> passes. Correct:
        #                     growth is healthy, and it is the common case.
        #   shrink between -> n_lines old+big, rows new+small  -> alarms. Correct,
        #                     and read-then-count was SILENT here -- so this reorder
        #                     also closes a false PASS; it does not trade one error
        #                     for another. (guard-2496: do not assume a long file is
        #                     append-only in every region.)
        # The detection the flag exists for is untouched: a recovery read that DROPS
        # lines still yields len(rows) < n_lines and still alarms.
        #
        # The count keeps its OWN try (): it is a second open() of a file
        # on the synced mount that rb-2970 records as transiently misbehaving, and a
        # failed measurement is not a measurement (guard-1091) -- degrade the FLAG,
        # never the DATA. `>=` not `==`: a recovery from a history snapshot
        # legitimately returns MORE rows than the file has lines, which is the same
        # direction growth moves, so one comparison covers both.
        #
        # `n_lines is None` is the single UNVERIFIED channel -- the caller opted out
        # (`count_lines=False`, the flag-discarding `_load_jsonl` wrapper below), or
        # the measurement failed. It never means "corrupt" (guard-7131).
        n_lines = None
        if count_lines:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    n_lines = sum(1 for ln in fh if ln.strip())
            except Exception:
                n_lines = None

        # The recovery reader does NOT raise on corruption -- it WARNs to stderr and
        # returns the parseable SUBSET -- so the except-branch below is never reached
        # for a corrupt store and `ok` cannot be inferred from control flow. Deriving
        # it by count is the only signal available. Streaming above, so a multi-MB
        # store costs one pass and no second copy in memory.
        rows = list(read_jsonl_with_recovery(path) or [])
        if n_lines is None:
            return rows, False
        return rows, len(rows) >= n_lines
    except Exception:
        # This branch re-parses the SAME path with a plain json.loads per line. It
        # buys PARSER independence, never PATH independence (guard-6944: a defence
        # that re-runs the same reader cannot detect what it exists to prevent). So
        # when the exception that landed here was caused by the PATH rather than by
        # the parse, this read fails too -- and until  it failed by
        # RAISING, because the read sat outside any try. Neither caller guards it
        # (`gather()` and `main()` both call straight through), so a store that
        # vanished or rotated mid-read killed the whole digest. That is strictly
        # worse than the thing `ok` exists for: the owner got a traceback instead of
        # a digest saying the store read FAILED. Degrade the FLAG, never the DATA.
        #
        # `OSError` only -- the documented class for a path that cannot be opened
        # (missing, rotated, a directory, permission-denied). NOT a blanket
        # `except Exception` (guard-2441/guard-373): a bug in the loop below must
        # stay visible instead of reading as a bad file. UnicodeDecodeError is a
        # ValueError and is deliberately absent because `errors="replace"` makes it
        # unreachable HERE -- measured: read_jsonl_with_recovery does raise it on
        # undecodable bytes, and this fallback is what rescues that case.
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Say WHY. `not path.exists()` above returns this same tuple for a
            # different reason, and two causes behind one silent value make the
            # step undiagnosable from its own output (guard-2586: a fallback path
            # and a failure path must never be indistinguishable). stderr is the
            # channel read_jsonl_with_recovery already WARNs on for this class.
            print(f"[completion-digest] fallback read of {path.name} failed: "
                  f"{type(exc).__name__} -- reporting the store as unread",
                  file=sys.stderr)
            return [], False
        out, ok = [], True
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                ok = False
        return out, ok


def _load_jsonl(path: Path) -> list:
    # Flag-free caller: skip the count pass it would only discard. Measured
    # 840,626 B of redundant reads per digest run across the five agent queues,
    # and those files only grow.
    return _load_jsonl_checked(path, count_lines=False)[0]


def _bash(script: str, *args: str, timeout: int = 60):
    try:
        from _runtime_bash import bash_cmd  # noqa: WPS433
        return subprocess.run(bash_cmd(script, *args), capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def load_population_predicate():
    """Import audit-user-to-agent._find_user_participant_goals -- the SSOT for
    'goals that need the user' (guard-1802: a second copy of this predicate is
    how the narrow-predicate hole appeared). Fail-open to None."""
    target = HERE / "audit-user-to-agent.py"
    try:
        spec = importlib.util.spec_from_file_location("_aut_population", target)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return getattr(mod, "_find_user_participant_goals", None)
    except Exception:
        return None


def gather(world: Path, agent: str, since: datetime | None, now: datetime, max_items: int) -> dict:
    asp_path = world / "aspirations.jsonl"
    # This one read feeds the aspiration table, the blocked tally AND the needs-you
    # list, so its silent fail-to-[] rendered as "Blocked: 0" + "Nothing is waiting
    # on you right now." -- an affirmative all-clear the read cannot support
    # ().  flagged the four reads that SHELL OUT and left this
    # file read, the largest consumer of the three, unflagged.
    asps, asps_read_ok = _load_jsonl_checked(asp_path)
    agent_files = sorted(Path(p) for p in glob.glob(str(agents_root() / "*" / "aspirations.jsonl")))

    # ---- completed in window (world queue) --------------------------------
    done = []
    active_asps = []
    for asp in asps:
        goals = asp.get("goals") or []
        total = len(goals)
        n_done = sum(1 for g in goals if g.get("status") == "completed")
        if asp.get("status") in ("active", None):
            # LIFETIME fraction: read the authoritative `progress` counter, NOT the
            # `goals` array (, zeta F1 msg-20260920-040010). That array is
            # retention-pruned -- completed goals are archived out on a lag -- so a
            # fraction computed from it understates the lane, and PLAUSIBLY, which is
            # why nobody checks it. Measured 2026-09-20:  rendered 107/2607 (4%)
            # against an authoritative 6727/10333 (65%), and  rendered 0/13 (0%)
            # against 117/131 (89%). It inverts the consolidate-before-expand signal at
            # the exact surface the owner reads: the lane with the MOST completed work
            # reports as the LEAST progressed. The same pruning is already bounded ~30
            # lines below for the WINDOW denominator (the  coverage floor) --
            # this is the second call site that never got the treatment, not a new
            # hazard. Three guardrails say the same thing about this array: guard-3410,
            # guard-4963, guard-3833.
            #
            # NOT a blind swap. The counter has two documented failure modes of its own,
            # and the pre-apply consult is what surfaced them:
            #   guard-5368 -- its denominator counts every TERMINAL-BUT-NOT-COMPLETE
            #     goal (skipped/expired/superseded) while its numerator excludes them,
            #     so it is an honest-but-pessimistic "of all goals ever filed here, how
            #     many reached completed". render() labels the column with that meaning
            #     rather than silently presenting it as a completion rate.
            #   guard-3602 -- the counter CAN read 0/0 while the goals array holds real
            #     goals. Swapping blind would send those rows to total=0, and render()'s
            #     `if a["total"]` filter would then DROP them from the owner's table
            #     entirely -- a disappearance, not a wrong number. So the array is kept
            #     as a fallback for exactly that case, and the row is LABELLED rather
            #     than mixing two populations invisibly in one column (guard-5689's
            #     two-writers-one-ratio shape, which is self-consistent and therefore
            #     invisible).
            _pr = asp.get("progress") if isinstance(asp.get("progress"), dict) else {}
            _ctr_total, _ctr_done = _pr.get("total_goals"), _pr.get("completed_goals")
            if isinstance(_ctr_total, int) and _ctr_total > 0 and isinstance(_ctr_done, int):
                _total, _done, _src = _ctr_total, _ctr_done, "counter"
            else:
                _total, _done, _src = total, n_done, "array"
            # THE ONE PLACE total==0 LANES ARE FILTERED (guard-4392: when a
            # population feeds two consumers, a filter applied to one reads as
            # applied to both). Until  the markdown renderer filtered
            # `if a["total"]` and the HTML twin did not, so a lane whose counter is
            # 0/0 AND whose goals array is empty rendered in the owner's EMAIL as
            # "0/0 (0%)" -- an affirmative "no progress" -- while the markdown
            # deliberately suppressed it. Filtering here, at construction, is what
            # makes the two twins consume one population; do NOT re-add a
            # per-renderer filter, which is the defect class, not the remedy.
            # A dropped row cannot lose window_done: _total==0 only via the array
            # branch (the counter branch requires _ctr_total > 0), and an empty
            # goals array contributes nothing to `done`.
            if _total:
                active_asps.append({"id": asp.get("id"), "title": asp.get("title") or "", "done": _done,
                                    "total": _total, "source": _src, "window_done": 0})
        for g in goals:
            if g.get("status") != "completed":
                continue
            ct = _ts(g.get("completed_at"))
            if since and (not ct or ct < since):
                continue
            if not since and not ct:
                continue
            done.append({"id": g.get("id"), "asp": asp.get("id"), "asp_title": asp.get("title") or "",
                         "title": g.get("title") or "", "by": g.get("completed_by") or g.get("executed_by") or "?",
                         "deep": (g.get("outcome_class") == "deep"), "at": ct.isoformat() if ct else "",
                         "sid": str(g.get("completed_by_sid") or "")[:8], "batch": False})
    # Coverage floor (). `done` above is drawn ONLY from the live world
    # queue, which is retention-pruned: completed goals are archived out on a lag.
    # So the requested `since` can predate anything the queue still holds, and the
    # window label + /day denominator would then describe a span the data does not
    # cover -- reporting EVICTION as a low rate (guard-4085). Record the oldest
    # completed_at the queue actually retains so render() can bound the denominator
    # by the covered span and SAY it is doing so (guard-2131: never present a window
    # whose recency you have not measured; a silent clamp is worse than the bug).
    _all_completed_ts = [
        _ts(g.get("completed_at"))
        for asp in asps for g in (asp.get("goals") or [])
        if g.get("status") == "completed" and _ts(g.get("completed_at"))
    ]
    _queue_oldest = min(_all_completed_ts) if _all_completed_ts else None
    _covered_from = max(since, _queue_oldest) if (since and _queue_oldest) else (_queue_oldest or since)
    coverage = {
        "requested_since": since.isoformat() if since else None,
        "queue_oldest_completed": _queue_oldest.isoformat() if _queue_oldest else None,
        "covered_from": _covered_from.isoformat() if _covered_from else None,
        "clamped": bool(since and _queue_oldest and _queue_oldest > since),
        "covered_hours": (round((now - _covered_from).total_seconds() / 3600.0, 2)
                          if _covered_from else None),
        "source": "live world queue only; archive sibling not read",
    }

    for a in active_asps:
        a["window_done"] = sum(1 for d in done if d["asp"] == a["id"])
    # Batch closes: the reducer formally closing many worker-executed goals in one
    # sweep stamps them all with the SAME session + a few minutes -- real work, but
    # done EARLIER; counting it as "today's throughput" misleads the reader
    # (measured 2026-08-16: 220 of 499 window closes landed in one 16:22 sweep).
    by_sid = {}
    for d in done:
        if d["sid"] and d["at"]:
            by_sid.setdefault(d["sid"], []).append(d)
    batches = []
    for sid, items in by_sid.items():
        items.sort(key=lambda d: d["at"])
        cluster = [items[0]]
        for d in items[1:] + [None]:
            prev = _ts(cluster[-1]["at"])
            cur = _ts(d["at"]) if d else None
            if d and prev and cur and (cur - prev) <= timedelta(minutes=10):
                cluster.append(d)
                continue
            if len(cluster) >= BATCH_CLOSE_MIN:
                for c in cluster:
                    c["batch"] = True
                batches.append({"by": cluster[0]["by"], "at": cluster[0]["at"][:16],
                                "until": cluster[-1]["at"][11:16], "n": len(cluster)})
            if d:
                cluster = [d]

    # recurring firings (WORLD + agent queues) in window.
    # THE WORLD QUEUE WAS MISSING UNTIL 2026-09-18 ( occ103, zeta/cc-02).
    # This loop iterated agent_files ONLY, so every world-level sensor was invisible
    # and the digest published an AGENT-PRIVATE count as a claim about the FLEET --
    # exactly guard-3156. The localisation was clean and is worth keeping: applying
    # this loop's OWN predicate to each store separately over a 22.03h window gave
    # 10 firings in the agent queues -- EXACTLY what the digest printed, so the
    # predicate and the timestamp logic were never at fault -- against 26 in the
    # world queue, where 101 of the fleet's 109 recurring sensors live. True total
    # 36 against a printed 10: a 72% understatement of the fleet's own monitoring
    # work, in the one number the user reads.
    # `asps` is the world queue, already loaded at the top of gather(), so covering
    # it costs no extra read. If this ever regresses, do NOT repair it by attaching
    # a "lower bound" caveat -- that trains every later reader to discount the
    # number instead of fixing the scan (guard-3103).
    recurring = 0
    _recurring_scan = list(asps) + [a for f in agent_files for a in _load_jsonl(f)]
    for asp in _recurring_scan:
        for g in asp.get("goals") or []:
            if not g.get("recurring"):
                continue
            la = _ts(g.get("lastAchievedAt") or g.get("last_achieved_at"))
            if la and (not since or la >= since):
                recurring += 1

    # ---- needs you ----------------------------------------------------------
    pred = load_population_predicate()
    needs = []
    seen = set()
    if pred:
        sources = [("world", asp_path)] + [(f.parent.name, f) for f in agent_files]
        for label, path in sources:
            try:
                cands = pred(label, path)
            except Exception:
                cands = []
            for c in cands:
                g = c.get("goal") or {}
                gid = g.get("id")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                created = _ts(g.get("created_at") or g.get("created"))
                needs.append({"id": gid, "title": g.get("title") or "", "asp": c.get("aspiration_id"),
                              "scope": (g.get("user_leg_scope") or "").strip(),
                              "age_h": _hours(created, now), "kind": "assigned to you",
                              "new": bool(created and since and created >= since),
                              "deliberate": bool(c.get("deliberate")), "priority": g.get("priority") or ""})
    # human-gated defers (defer_reason 'human_blocked:' prefix) not already listed
    for asp in asps:
        if asp.get("status") in TERMINAL:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL:
                continue
            dr = str(g.get("defer_reason") or "")
            if dr.lower().startswith("human_blocked") and g.get("id") not in seen:
                seen.add(g.get("id"))
                created = _ts(g.get("created_at") or g.get("created"))
                # An owner-decided park is STILL LISTED — this report is the
                # status summary the owner asked for, not an unsolicited nag, and
                # hiding it would make the report disagree with the queue. What
                # changes is the FRAMING: "deliberately parked with you", citing
                # the decision record, instead of a NEEDS FROM YOU line that
                # re-asks a settled question (guard-6754). `deliberate: True`
                # selects that existing tag in both renderers, so no render
                # branch is added for this ().
                od_ref = owner_decided_ref(g)
                if od_ref:
                    scope = "parked on your own decision — see %s. Nothing needed from you." % od_ref
                else:
                    scope = _clip(dr.split(":", 1)[-1], 140)
                needs.append({"id": g.get("id"), "title": g.get("title") or "", "asp": asp.get("id"),
                              "scope": scope, "age_h": _hours(created, now),
                              "kind": "human-gated" if not od_ref else "owner-decided-park",
                              "deliberate": bool(od_ref), "priority": g.get("priority") or "",
                              "owner_decision_ref": od_ref,
                              "new": bool(created and since and created >= since)})
    needs.sort(key=lambda x: -(x["age_h"] or 0))

    # pending questions (fleet)
    # ANNOUNCED degradation, not a silent zero (, zeta F2
    # msg-20260920-040028). `pqs` falls to [] on three indistinguishable failures --
    # _bash returned None (import error / spawn failure / 90s timeout), a non-zero
    # returncode, or a JSON parse raise -- and the renderer then states
    # "0 open question(s)" and "Nothing is waiting on you right now." to the OWNER,
    # by email. A degraded read must never render as an affirmative all-clear;
    # verify-before-assuming.md rule 4: a try/except around a parse is ZERO signals,
    # not one. The module already demonstrates the right treatment exactly once
    # (`_OWNER_DECIDED_LOADED`); these four reads are the ones that never got it, and
    # that asymmetry is what made it hard to see. Same laundering the sibling
    # agent-completion-report already forbids at its step 10 (720 real board messages
    # reported as 0).
    pqs = []
    pqs_read_ok = False
    r = _bash(str(HERE / "pending-questions-read.sh"), "--all-agents", timeout=90)
    if r and r.returncode == 0 and r.stdout.strip():
        try:
            rows = json.loads(r.stdout)
            for q in rows:
                if str(q.get("status", "")).lower() != "pending":
                    continue
                d = _ts(q.get("date") or q.get("created"))
                pqs.append({"id": q.get("id"), "agent": q.get("agent") or "", "age_h": _hours(d, now),
                            "question": q.get("question") or "", "default_action": q.get("default_action") or ""})
            pqs_read_ok = True
        except Exception:
            pqs = []
    elif r and r.returncode == 0:
        # Ran clean and returned nothing: a genuinely empty queue, not a failure.
        pqs_read_ok = True
    pqs.sort(key=lambda x: (x["agent"] != agent, -(x["age_h"] or 0)))

    # ---- blocked ----------------------------------------------------------------
    blocked = []
    downstream = {}
    for asp in asps:
        if asp.get("status") in TERMINAL:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL:
                continue
            bb = g.get("blocked_by")
            if isinstance(bb, str):
                bb = [bb]
            for dep in bb or []:
                downstream[str(dep)] = downstream.get(str(dep), 0) + 1
    for asp in asps:
        if asp.get("status") in TERMINAL:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL:
                continue
            cause = None
            dr = str(g.get("defer_reason") or "")
            if g.get("status") == "blocked":
                cause = "blocked" + (f": {_clip(dr, 90)}" if dr else "")
            elif g.get("blocker_ref"):
                cause = f"blocker {g.get('blocker_ref')}"
            elif dr:
                cause = "deferred: " + _clip(dr, 90)
            elif g.get("blocked_by"):
                bb = g.get("blocked_by")
                cause = "waits on " + ", ".join(map(str, bb if isinstance(bb, list) else [bb]))[:60]
            if not cause:
                continue
            blocked.append({"id": g.get("id"), "title": g.get("title") or "", "asp": asp.get("id"), "cause": cause,
                            "downstream": downstream.get(str(g.get("id")), 0),
                            "owner": g.get("claimed_by") or g.get("intended_agent") or "-",
                            "human": dr.lower().startswith("human_blocked")})
    blocked_total = len(blocked)
    by_cause = {}
    for b in blocked:
        k = b["cause"].split(":")[0].split(" ")[0]
        by_cause[k] = by_cause.get(k, 0) + 1
    blocked.sort(key=lambda b: (-b["downstream"], b["human"], b["id"] or ""))

    # ---- hypotheses ------------------------------------------------------------
    hyp = {}
    hyp_read_ok = False
    r = _bash(str(HERE / "pipeline-read.sh"), "--accuracy", timeout=60)
    if r and r.returncode == 0:
        try:
            a = json.loads(r.stdout)
            hyp = {"lifetime_pct": a.get("accuracy_pct"), "resolved": a.get("total_resolved")}
            hyp_read_ok = True
        except Exception:
            hyp = {}
    win_conf = win_corr = 0
    corrected = []
    r = _bash(str(HERE / "pipeline-read.sh"), "--stage", "resolved", timeout=60)
    if r and r.returncode == 0:
        try:
            for rec in json.loads(r.stdout):
                od = _ts(rec.get("outcome_date") or rec.get("resolved_at"))
                if since and (not od or od < since - timedelta(days=1)):
                    continue
                o = str(rec.get("outcome") or "").upper()
                if o == "CONFIRMED":
                    win_conf += 1
                elif o == "CORRECTED":
                    win_corr += 1
                    corrected.append({"id": rec.get("id"), "title": rec.get("title") or rec.get("claim") or "",
                                      "at": (od.isoformat() if od else "")[:10]})
        except Exception:
            pass
    corrected.sort(key=lambda c: c["at"], reverse=True)
    hyp.update({"window_confirmed": win_conf, "window_corrected": win_corr, "corrected": corrected[:5]})

    # ---- fleet pulse -----------------------------------------------------------
    pulse = []
    pulse_read_ok = False
    r = _bash(str(HERE / "team-state-read.sh"), "--json", timeout=60)
    if r and r.returncode == 0:
        try:
            st = json.loads(r.stdout).get("agent_status") or {}
            for name, row in sorted(st.items()):
                la = _ts(row.get("last_active"))
                inf = row.get("in_flight") if isinstance(row.get("in_flight"), dict) else {}
                pulse.append({"agent": name, "age_h": _hours(la, now),
                              "in_flight": inf.get("goal_id"), "in_flight_title": inf.get("title") or ""})
            pulse_read_ok = True
        except Exception:
            pulse = []

    # ---- outcome signals -------------------------------------------------------
    outcome = {}
    om = world / "outcome-metrics.yaml"
    outcome_read_ok = not om.exists()  # absent by design is not a degraded read
    if om.exists():
        try:
            import yaml  # noqa: WPS433
            outcome = yaml.safe_load(om.read_text(encoding="utf-8")) or {}
            outcome_read_ok = True
        except Exception:
            outcome = {}

    # ---- spend (domain hook slot: world/scripts/digest-cost.sh) ------------------
    cost = _cost_from_hook(world, agent, since, now)

    return {"done": done, "batches": batches, "recurring": recurring, "needs": needs, "pqs": pqs, "blocked": blocked,
            "blocked_total": blocked_total, "by_cause": by_cause, "active_asps": active_asps, "hyp": hyp,
            "pulse": pulse, "outcome": outcome, "cost": cost, "coverage": coverage,
            # Degraded-mode visibility, not a renderer input: presence in the
            # structured output is what makes a dead exemption auditable.
            "owner_decided_predicate_loaded": _OWNER_DECIDED_LOADED,
            # ...and the four fail-to-empty reads that had no flag until .
            # `pqs_read_ok` IS a renderer input (it gates the affirmative all-clear);
            # the other three are auditability, same as the predicate flag above.
            "pqs_read_ok": pqs_read_ok, "pulse_read_ok": pulse_read_ok,
            "hyp_read_ok": hyp_read_ok, "outcome_read_ok": outcome_read_ok,
            # ...and the FILE read that feeds the aspiration table, the blocked
            # tally and the needs-you list. Like pqs_read_ok this one IS a renderer
            # input in BOTH twins (guard-5392: a flag is unmet while it lives
            # somewhere the reader does not look), not just auditability.
            "asps_read_ok": asps_read_ok}


COST_SLOT = "scripts/digest-cost.sh"


def _cost_from_hook(world: Path, agent: str, since: datetime | None, now: datetime) -> dict:
    """Ask the domain what things cost (see domain-hooks.md `digest-cost`).
    Contract: JSON object on stdout with optional headline/tiles/lines/note/
    as_of/stale. Any failure => {} and the Spend card is omitted; the digest
    never fails on cost."""
    slot = Path(world) / COST_SLOT
    if not slot.exists():
        return {}
    try:
        from _runtime_bash import bash_cmd  # noqa: WPS433
        env = dict(os.environ, DIGEST_SINCE=since.isoformat() if since else "", DIGEST_NOW=now.isoformat(),
                   DIGEST_AGENT=agent)
        r = subprocess.run(bash_cmd(str(slot)), capture_output=True, text=True, timeout=60, env=env)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        d = json.loads(r.stdout)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def render(data: dict, *, agent: str, since: datetime | None, now: datetime, notes: str, max_items: int) -> str:
    L = []
    win_h = _hours(since, now) if since else None
    win_label = f"last {int(win_h)}h ({since:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M} UTC)" if since else "lifetime"
    # : the counted rows come only from the retention-pruned live queue,
    # so when `since` predates what the queue retains, the requested window is NOT
    # covered. Bound the /day denominator by the span actually covered and make the
    # clamp VISIBLE -- a silent clamp is worse than the original overstatement.
    _cov = data.get("coverage") or {}
    rate_h = win_h
    if _cov.get("clamped") and _cov.get("covered_hours"):
        rate_h = _cov["covered_hours"]
        win_label += (f" — DATA COVERS ONLY last {int(rate_h)}h "
                      f"(from {_cov.get('covered_from', '?')[:16]}; live queue retains no "
                      f"completed_at older than that, archive not read) — rates below use the "
                      f"COVERED span, not the requested one")
    done, needs, pqs, blocked = data["done"], data["needs"], data["pqs"], data["blocked"]
    deep = sum(1 for d in done if d["deep"])
    quiet = [p["agent"] for p in data["pulse"] if p["age_h"] is not None and p["age_h"] > 6]

    L.append(f"# Fleet digest — {now:%Y-%m-%d} (from {agent})")
    L.append(f"Window: {win_label}")
    L.append("")
    L.append("## TL;DR")
    organic = [d for d in done if not d["batch"]]
    n_batch = len(done) - len(organic)
    rate = f" (~{round(len(organic) / (rate_h / 24), 1)}/day)" if rate_h and rate_h > 0 else ""
    by_agent = {}
    for d in organic:
        by_agent[d["by"]] = by_agent.get(d["by"], 0) + 1
    line = f"- Done: **{len(organic)}** goals{rate}, {sum(1 for d in organic if d['deep'])} deep"
    if by_agent:
        line += " — " + ", ".join(f"{('unattributed' if k == '?' else k)} {v}" for k, v in sorted(by_agent.items(), key=lambda x: -x[1]))
    if data["recurring"]:
        line += f"; +{data['recurring']} recurring sweeps"
    L.append(line)
    if n_batch:
        L.append(f"- Also **{n_batch}** batch-closed: " + "; ".join(f"{b['n']} by {b['by']} on {b['at'][:10]} {b['at'][11:16]}–{b['until']}" for b in data["batches"]) + " (work done earlier, formally closed in one sweep — not today's throughput)")
    # A degraded pending-questions read must not render as "0" (zeta F2). Say the
    # number is UNKNOWN rather than asserting an all-clear the read cannot support.
    _pq_ok = bool(data.get("pqs_read_ok"))
    _pq_txt = f"**{len(pqs)}** open question(s)" if _pq_ok else "**an unknown number of** open question(s) (READ FAILED — see below)"
    L.append(f"- Needs you: **{len(needs)}** goal(s) + {_pq_txt} — listed below")
    # Same treatment for the aspirations-store read (): a store that
    # could not be read makes this tally 0, and "Blocked: 0" is the sentence the
    # owner acts on. NO DEFAULT -- an absent key reads False, exactly like the
    # `pqs_read_ok` sibling six lines up. gather() writes this key unconditionally
    # (assigned at the top, returned in the one return dict), so no live path
    # reaches the default today; the point is that a fail-OPEN default on a flag
    # whose only job is to suppress an unsupportable all-clear poisons every
    # assertion of its own value (guard-1718), and the next hand-built `data`
    # would inherit the reassuring answer silently.
    _asps_ok = bool(data.get("asps_read_ok"))
    if _asps_ok:
        L.append(f"- Blocked: **{data['blocked_total']}** goal(s)" + (" (" + ", ".join(f"{v} {k}" for k, v in sorted(data['by_cause'].items(), key=lambda x: -x[1])) + ")" if data["by_cause"] else ""))
    else:
        L.append("- Blocked: **an unknown number of** goal(s) — the aspirations store READ FAILED (see below)")
    h = data["hyp"]
    if h:
        L.append(f"- Learning: {h.get('window_confirmed', 0)} hypotheses confirmed / {h.get('window_corrected', 0)} corrected this window; lifetime accuracy {h.get('lifetime_pct', '?')}% over {h.get('resolved', '?')}")
    if data["pulse"]:
        L.append("- Fleet: " + ", ".join(f"{p['agent']} {'active' if (p['age_h'] is not None and p['age_h'] <= 6) else ('quiet ' + _age_str(p['age_h']))}" for p in data["pulse"]))
    n_new = sum(1 for n in needs if n.get("new"))
    if n_new:
        L.append(f"- New asks this window: **{n_new}** (marked NEW below)")
    L.append("")

    # ---- needs you
    L.append(f"## Needs you ({len(needs)} goals, {len(pqs) if _pq_ok else '?'} questions)")
    if not _asps_ok:
        L.append("⚠ The aspirations store could not be read (missing, or lines that would not "
                 "parse). The goal count above is NOT zero — it is UNKNOWN, and so is the "
                 "blocked tally. Do not read this digest as an all-clear.")
    if not _pq_ok:
        L.append("⚠ The fleet pending-questions read FAILED (subprocess error, non-zero exit, or "
                 "unparseable output). The question count above is NOT zero — it is UNKNOWN. "
                 "Do not read this digest as an all-clear.")
    elif not needs and not pqs and _asps_ok:
        L.append("Nothing is waiting on you right now.")
    for i, n in enumerate(needs[:max_items], 1):
        tag = "human-gated" if n["kind"] == "human-gated" else ("deliberately parked with you" if n["deliberate"] else "assigned to you")
        L.append(f"{i}. **{n['id']}**{' NEW' if n.get('new') else ''} {_clip(n['title'], 90)}  _( {tag}, {_age_str(n['age_h'])} old, {n['asp']} )_")
        L.append(f"   NEEDS FROM YOU: {n['scope'] or 'not recorded on the goal (our bug — reply and we will fix it)'}")
    if len(needs) > max_items:
        rest = needs[max_items:]
        L.append(f"   Also waiting ({len(rest)} more, oldest first):")
        for n in rest[:30]:
            L.append(f"   - {n['id']} {_clip(n['title'], 70)} ({_age_str(n['age_h'])}{', ' + _clip(n['scope'], 40) if n['scope'] else ''})")
        if len(rest) > 30:
            L.append(f"   - … +{len(rest) - 30} more")
    if pqs:
        L.append("")
        L.append("Open questions (each was already acted on with the stated default — override if you disagree):")
        for q in pqs[:max_items]:
            L.append(f"- **{q['id']}** ({q['agent']}, {_age_str(q['age_h'])}): {_clip(q['question'], 220)}")
            if q["default_action"]:
                L.append(f"  default taken: {_clip(q['default_action'], 160)}")
        if len(pqs) > max_items:
            L.append(f"- … +{len(pqs) - max_items} more (`/open-questions`)")
    L.append("")

    # ---- blocked
    L.append(f"## Blocked ({data['blocked_total'] if _asps_ok else '?'})")
    if not _asps_ok:
        L.append("⚠ Unknown — the aspirations store could not be read. This is NOT "
                 "\"nothing blocked\".")
    elif not blocked:
        L.append("Nothing blocked.")
    for b in blocked[:max_items]:
        holds = f" → holds up {b['downstream']} goal(s)" if b["downstream"] else ""
        L.append(f"- **{b['id']}** {_clip(b['title'], 80)}{holds} — {b['cause']} (owner: {b['owner']})")
    if len(blocked) > max_items:
        L.append(f"- … +{len(blocked) - max_items} more")
    L.append("")

    # ---- done
    L.append(f"## Done this window ({len(organic)}" + (f" + {n_batch} batch-closed" if n_batch else "") + ")")
    if not done:
        L.append("No goals completed in this window.")
    by_asp = {}
    for d in done:
        by_asp.setdefault(d["asp"], []).append(d)
    for asp_id, items in sorted(by_asp.items(), key=lambda kv: -len(kv[1]))[:8]:
        items = sorted(items, key=lambda d: (d["batch"], not d["deep"], d["at"]))
        L.append(f"**{asp_id} — {_clip(items[0]['asp_title'], 60)}** ({len(items)})")
        for d in items[:4]:
            L.append(f"  - {d['id']} {_clip(d['title'], 85)} ({d['by']}{', deep' if d['deep'] else ''}{', batch-closed' if d['batch'] else ''})")
        if len(items) > 4:
            L.append(f"  - … +{len(items) - 4} more")
    if len(by_asp) > 8:
        L.append(f"… and {len(by_asp) - 8} more aspirations touched")
    L.append("")

    # ---- in progress
    # No per-renderer filter here: gather() already filtered total==0 lanes out of
    # active_asps, once, for both twins (guard-4392, ). Re-adding
    # `if a["total"]` would restore the two-copies shape that let the twins diverge.
    act = list(data["active_asps"])
    act.sort(key=lambda a: (-a["window_done"], -(a["done"] / a["total"])))
    L.append("## In progress")
    # Say what the fraction MEANS (guard-5368): the authoritative counter's
    # denominator includes goals that ended skipped/expired/superseded while its
    # numerator counts only `completed`, so this is "of all goals ever filed in this
    # lane, how many reached completed" -- honest, and deliberately pessimistic. It is
    # NOT "how much of the remaining work is done".
    L.append("_Goals that reached `completed`, over all goals ever filed in the lane "
             "(so goals that ended skipped/expired/superseded sit in the denominator)._")
    for a in act[:8]:
        pct = int(100 * a["done"] / a["total"]) if a["total"] else 0
        # Label the fallback rather than mixing two populations in one column
        # (guard-3602): a lane whose counter reads 0/0 falls back to the pruned goals
        # array, which UNDERSTATES. Unlabelled, the two are indistinguishable.
        src = " — from the live queue only (lane counter unset); UNDERSTATED" if a.get("source") == "array" else ""
        L.append(f"- {a['id']} {_clip(a['title'], 60)}: {a['done']}/{a['total']} ({pct}%)" + (f", +{a['window_done']} this window" if a["window_done"] else "") + src)
    L.append("")

    # ---- learning: what we got wrong
    corr = (data.get("hyp") or {}).get("corrected") or []
    if corr:
        L.append("## What we got wrong (hypotheses corrected this window)")
        for c in corr:
            L.append(f"- {c['at']} {_clip(c['title'], 120)}")
        L.append("")

    # ---- right now
    if data["pulse"]:
        L.append("## Each agent right now")
        for p in data["pulse"]:
            state = "active" if (p["age_h"] is not None and p["age_h"] <= 6) else f"quiet {_age_str(p['age_h'])}"
            on = f" — on {p['in_flight']} {_clip(p['in_flight_title'], 70)}" if p.get("in_flight") else " — between goals"
            L.append(f"- {p['agent']}: {state}{on}")
        L.append("")

    # ---- spend
    cost = data.get("cost") or {}
    if cost:
        L.append("## Spend" + (f" — {cost['headline']}" if cost.get("headline") else ""))
        for t in cost.get("tiles") or []:
            L.append(f"- {t.get('label', '')}: **{t.get('value', '')}**" + (f" ({t['sub']})" if t.get("sub") else ""))
        for line in cost.get("lines") or []:
            L.append(f"- {line}")
        if cost.get("note"):
            L.append(f"  _{cost['note']}_")
        if cost.get("as_of"):
            L.append(f"  (as of {cost['as_of']}{'; STALE' if cost.get('stale') else ''})")
        L.append("")

    # ---- outcome + health
    L.append("## Product signals")
    src = (data["outcome"] or {}).get("sources") or {}
    if not src:
        L.append("- no outcome signal configured (outcome-observation hook)")
    else:
        ci = src.get("ci") or {}
        if ci:
            L.append(f"- CI: {ci.get('runs_passed', '?')}/{ci.get('runs_total', '?')} runs passed" + (f" (pass rate {ci.get('pass_rate')})" if ci.get("pass_rate") is not None else ""))
        op = src.get("operator") or {}
        if op:
            L.append(f"- Service: {'reachable' if op.get('reachable') else 'UNREACHABLE'} ({op.get('status', '?')})")
        gt = src.get("git") or {}
        if gt:
            L.append(f"- Git: product estate {'present' if gt.get('prod_repo_present') else 'absent'} on the reporting box ({gt.get('status', '?')})")
        L.append(f"  (as of {(data['outcome'] or {}).get('updated_at', '?')})")
    L.append("")

    if notes.strip():
        L.append("## Notes from the agent")
        _note_lines = notes.strip().splitlines()
        for line in _note_lines[:_NOTES_MAX_LINES]:
            L.append(line.rstrip())
        _dropped = len(_note_lines) - _NOTES_MAX_LINES
        if _dropped > 0:
            L.append(f"[... {_dropped} more line(s) TRUNCATED — full text in "
                     f"agents/{agent}/COMPLETION-REPORT.md]")
        L.append("")

    L.append("---")
    L.append(f"Full agent-side report: agents/{agent}/COMPLETION-REPORT.md (git history is the archive). "
             "Reply to this email to reach the fleet mailbox.")
    return "\n".join(L) + "\n"



# --------------------------------------------------------------------------
# render (HTML email)
# --------------------------------------------------------------------------

import html as _html  # noqa: E402

# Max operator-note lines rendered in the digest. The notes block is the ONLY
# free-text agent->principal channel, and truncation deletes the TAIL, where
# authors put the newest/most urgent item (: a production email
# announced "TWO DEADLINES NEED YOU", delivered one, and ended mid-word).
# Raising the cap alone is NOT the fix -- an unannounced cut is the defect
# (guard-3976, guard-3698), so both renderers below MUST emit the marker.
_NOTES_MAX_LINES = 40


def _e(x) -> str:
    return _html.escape(str(x if x is not None else ""))


def _pill(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;'
            f'font-weight:600;color:#fff;background:{color};vertical-align:middle">{_e(text)}</span>')


def _card(title: str, inner: str, border: str = "#1e90ff", bg: str = "#fff") -> str:
    return (f'<div style="margin:0 0 18px;border-left:4px solid {border};background:{bg};'
            f'border-radius:6px;padding:14px 16px">'
            f'<h2 style="margin:0 0 10px;font-size:17px;color:#222">{_e(title)}</h2>{inner}</div>')


def _tile(label: str, value: str, sub: str = "", color: str = "#222") -> str:
    return (f'<td style="padding:8px 10px;vertical-align:top;border:1px solid #eee;background:#fafafa;min-width:90px">'
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#888">{_e(label)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{color};line-height:1.2">{_e(value)}</div>'
            f'<div style="font-size:12px;color:#666">{_e(sub)}</div></td>')


TD = 'style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4"'
TH = 'style="padding:6px 8px;border-bottom:2px solid #ddd;text-align:left;font-size:12px;color:#666;text-transform:uppercase"'


def render_html(data: dict, *, agent: str, since: datetime | None, now: datetime, notes: str, max_items: int) -> str:
    """Email-safe HTML twin of render(): inline styles, tables, no scripts, no
    remote assets, single 680px column. Same data, same order, same numbers."""
    done, needs, pqs, blocked = data["done"], data["needs"], data["pqs"], data["blocked"]
    organic = [d for d in done if not d["batch"]]
    n_batch = len(done) - len(organic)
    win_h = _hours(since, now) if since else None
    by_agent = {}
    for d in organic:
        k = "unattributed" if d["by"] == "?" else d["by"]
        by_agent[k] = by_agent.get(k, 0) + 1
    h = data.get("hyp") or {}
    n_new = sum(1 for n in needs if n.get("new"))
    quiet = [p["agent"] for p in data["pulse"] if p["age_h"] is None or p["age_h"] > 6]

    out = []
    out.append(f'<html><head><meta name="viewport" content="width=device-width, initial-scale=1.0">'
               f'<title>{_e("Fleet digest — " + now.strftime("%Y-%m-%d"))}</title></head>')
    out.append('<body style="margin:0;padding:0;background:#f4f4f4;font-family:-apple-system,BlinkMacSystemFont,'
               "'Segoe UI',Roboto,Arial,sans-serif;font-size:14px;line-height:1.5;color:#333\">")
    out.append('<div style="max-width:680px;margin:0 auto;padding:16px">')
    out.append('<div style="background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">')
    # header
    win_txt = (f'{since.strftime("%Y-%m-%d %H:%M")} → {now.strftime("%Y-%m-%d %H:%M")} UTC · last {int(win_h)}h'
               if since and win_h is not None else f'as of {now.strftime("%Y-%m-%d %H:%M")} UTC')
    out.append(f'<div style="padding:20px 22px 14px;border-bottom:2px solid #1e90ff">'
               f'<h1 style="margin:0;font-size:22px;color:#222">Fleet digest — {_e(now.strftime("%Y-%m-%d"))}</h1>'
               f'<p style="margin:6px 0 0;font-size:12px;color:#888">{_e(win_txt)} · from {_e(agent)}</p></div>')
    out.append('<div style="padding:18px 22px">')

    # ---- TL;DR tiles
    tiles = []
    rate = f"~{round(len(organic) / (win_h / 24), 1)}/day" if win_h else ""
    tiles.append(_tile("Done", str(len(organic)), f"{sum(1 for d in organic if d['deep'])} deep · {rate}", "#28a745"))
    # Same degraded-read gate as the markdown renderer (zeta F2): never render an
    # unknown question count as a green "+0 open questions" tile.
    _pq_ok = bool(data.get("pqs_read_ok"))
    tiles.append(_tile("Needs you", str(len(needs)),
                       (f"+{len(pqs)} open questions" if _pq_ok else "open questions: READ FAILED")
                       + (f" · {n_new} new" if n_new else ""),
                       "#fd7e14" if (needs or not _pq_ok) else "#28a745"))
    # Same aspirations-store gate as the markdown twin (, guard-5392):
    # a green "0" tile is the most affirmative surface in the whole email. NO
    # DEFAULT, for the same reason as the twin -- see the comment there.
    _asps_ok = bool(data.get("asps_read_ok"))
    tiles.append(_tile("Blocked", str(data["blocked_total"]) if _asps_ok else "?",
                       (", ".join(f"{v} {k}" for k, v in sorted(data["by_cause"].items(), key=lambda x: -x[1])[:3]) if _asps_ok
                        else "aspirations store: READ FAILED"),
                       "#dc3545" if (data["blocked_total"] or not _asps_ok) else "#28a745"))
    if h:
        acc = h.get("lifetime_pct")
        tiles.append(_tile("Learning", f"{h.get('window_confirmed', 0)}✓ {h.get('window_corrected', 0)}✗",
                           f"lifetime {acc}% of {h.get('resolved', '?')}" if acc is not None else "hypotheses this window"))
    if data["pulse"]:
        tiles.append(_tile("Fleet", f"{len(data['pulse']) - len(quiet)}/{len(data['pulse'])}",
                           "all active" if not quiet else "quiet: " + ", ".join(quiet), "#28a745" if not quiet else "#fd7e14"))
    tl = '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%"><tr>' + "".join(tiles) + "</tr></table>"
    extra = []
    if by_agent:
        extra.append("Done by agent: " + ", ".join(f"<b>{_e(k)}</b> {v}" for k, v in sorted(by_agent.items(), key=lambda x: -x[1])))
    if n_batch:
        extra.append(f"Also <b>{n_batch}</b> batch-closed (" + "; ".join(f"{b['n']} by {_e(b['by'])} {_e(b['at'][:10])} {_e(b['at'][11:16])}–{_e(b['until'])}" for b in data["batches"])
                     + ") — work done earlier, formally closed in one sweep; not counted above.")
    if data["recurring"]:
        extra.append(f"+{data['recurring']} recurring sweeps ran.")
    if extra:
        tl += '<p style="margin:10px 0 0;font-size:12px;color:#555">' + " &nbsp;·&nbsp; ".join(extra) + "</p>"
    out.append(_card("TL;DR", tl))

    # ---- Needs you
    if not _asps_ok and not needs:
        inner = ('<p style="margin:0;color:#b26a00">⚠ The aspirations store could not be read '
                 '(missing, or lines that would not parse). The goal count is <b>UNKNOWN</b>, not '
                 'zero, and so is the blocked tally — do not read this digest as an all-clear.</p>')
    elif not _pq_ok and not needs:
        inner = ('<p style="margin:0;color:#b26a00">⚠ The fleet pending-questions read FAILED '
                 '(subprocess error, non-zero exit, or unparseable output). The question count is '
                 '<b>UNKNOWN</b>, not zero — do not read this digest as an all-clear.</p>')
    elif not needs and not pqs and _asps_ok:
        inner = '<p style="margin:0;color:#28a745">Nothing is waiting on you right now.</p>'
    else:
        rows = []
        for i, n in enumerate(needs[:max_items], 1):
            tag = ("human-gated" if n["kind"] == "human-gated" else ("parked with you" if n["deliberate"] else "assigned to you"))
            pills = _pill(tag, "#6c757d") + (" " + _pill("NEW", "#1e90ff") if n.get("new") else "")
            scope = n["scope"] or '<i style="color:#999">not recorded on the goal — our bug, reply and we will fix it</i>'
            if n["scope"]:
                scope = _e(scope)
            rows.append(f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;color:#999;font-size:12px">{i}</td>'
                        f'<td {TD}><b>{_e(n["id"])}</b> {pills}<br>{_e(_clip(n["title"], 110))}'
                        f'<div style="font-size:12px;color:#c25400;margin-top:2px"><b>Needs from you:</b> {scope}</div></td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap;font-size:12px;color:#666">{_e(_age_str(n["age_h"]))}<br>{_e(n["asp"] or "")}</td></tr>')
        inner = ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">'
                 f'<tr><th {TH}>#</th><th {TH}>Goal · what it needs from you</th><th {TH}>Age</th></tr>' + "".join(rows) + "</table>")
        rest = needs[max_items:]
        if rest:
            items = "".join(f'<li>{_e(n["id"])}{" " + _pill("NEW", "#1e90ff") if n.get("new") else ""} {_e(_clip(n["title"], 80))} '
                            f'<span style="color:#888">({_e(_age_str(n["age_h"]))}{", " + _e(_clip(n["scope"], 40)) if n["scope"] else ""})</span></li>'
                            for n in rest[:30])
            more = f'<li>… +{len(rest) - 30} more</li>' if len(rest) > 30 else ""
            inner += (f'<details style="margin-top:8px"><summary style="cursor:pointer;color:#1e90ff;font-size:13px">Also waiting — {len(rest)} more (oldest first)</summary>'
                      f'<ul style="margin:6px 0 0;padding-left:18px;font-size:12px;line-height:1.5">{items}{more}</ul></details>')
        if pqs:
            qs = []
            for q in pqs[:max_items]:
                qs.append(f'<li style="margin-bottom:8px"><b>{_e(q["id"])}</b> <span style="color:#888">({_e(q["agent"])}, {_e(_age_str(q["age_h"]))})</span><br>'
                          f'{_e(_clip(q["question"], 260))}'
                          + (f'<div style="font-size:12px;color:#2a7d2a;margin-top:2px"><b>Default taken:</b> {_e(_clip(q["default_action"], 200))}</div>' if q["default_action"] else "")
                          + "</li>")
            inner += (f'<h3 style="margin:14px 0 6px;font-size:14px;color:#444">Open questions ({len(pqs)}) — each already acted on with the stated default; override if you disagree</h3>'
                      f'<ul style="margin:0;padding-left:18px;font-size:13px">{"".join(qs)}</ul>')
    if not _pq_ok and needs:
        # There ARE goals to show, so the degraded branch above did not fire — but the
        # question half is still unknown and must say so beside them.
        inner += ('<p style="margin:14px 0 0;font-size:13px;color:#b26a00">⚠ The fleet '
                  'pending-questions read FAILED; any open questions are NOT listed above and '
                  'their count is <b>UNKNOWN</b>, not zero.</p>')
    out.append(_card(f"Needs you ({len(needs)} goals, {len(pqs) if _pq_ok else '?'} questions)", inner, "#fd7e14", "#fffaf5"))

    # ---- Blocked
    if not _asps_ok:
        inner = ('<p style="margin:0;color:#b26a00">⚠ Unknown — the aspirations store could not be '
                 'read. This is <b>not</b> "nothing blocked".</p>')
    elif not blocked:
        inner = '<p style="margin:0;color:#28a745">Nothing blocked.</p>'
    else:
        rows = []
        for b in blocked[:max_items]:
            holds = f'<br><span style="color:#dc3545;font-size:12px">holds up {b["downstream"]} goal(s)</span>' if b["downstream"] else ""
            rows.append(f'<tr><td {TD}><b>{_e(b["id"])}</b> {_e(_clip(b["title"], 90))}{holds}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;color:#555">{_e(_clip(b["cause"], 120))}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;color:#888;white-space:nowrap">{_e(b["owner"])}</td></tr>')
        inner = ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">'
                 f'<tr><th {TH}>Goal</th><th {TH}>Why</th><th {TH}>Owner</th></tr>' + "".join(rows) + "</table>")
        if len(blocked) > max_items:
            inner += f'<p style="margin:6px 0 0;font-size:12px;color:#888">… +{len(blocked) - max_items} more (sorted by how much each holds up)</p>'
    out.append(_card(f"Blocked ({data['blocked_total'] if _asps_ok else '?'})", inner, "#dc3545", "#fff8f8"))

    # ---- Done
    if not done:
        inner = '<p style="margin:0;color:#888">No goals completed in this window.</p>'
    else:
        by_asp = {}
        for d in done:
            by_asp.setdefault(d["asp"], []).append(d)
        parts = []
        for asp_id, items in sorted(by_asp.items(), key=lambda kv: -len(kv[1]))[:8]:
            items = sorted(items, key=lambda d: (d["batch"], not d["deep"], d["at"]))
            org = sum(1 for d in items if not d["batch"])
            cnt = f"{org}" + (f" + {len(items) - org} batch" if len(items) - org else "")
            lis = "".join(f'<li>{_e(d["id"])} {_e(_clip(d["title"], 90))} <span style="color:#888">({_e(d["by"])}{", deep" if d["deep"] else ""}{", batch-closed" if d["batch"] else ""})</span></li>'
                          for d in items[:4])
            more = f'<li style="color:#888">… +{len(items) - 4} more</li>' if len(items) > 4 else ""
            parts.append(f'<div style="margin-bottom:8px"><b>{_e(asp_id)}</b> — {_e(_clip(items[0]["asp_title"], 70))} <span style="color:#888">({cnt})</span>'
                         f'<ul style="margin:3px 0 0;padding-left:18px;font-size:12px;line-height:1.45">{lis}{more}</ul></div>')
        if len(by_asp) > 8:
            parts.append(f'<p style="margin:0;font-size:12px;color:#888">… and {len(by_asp) - 8} more aspirations touched</p>')
        inner = "".join(parts)
    out.append(_card(f"Done this window ({len(organic)}" + (f" + {n_batch} batch-closed" if n_batch else "") + ")", inner, "#28a745", "#f6fff8"))

    # ---- In progress (bars)
    asps = sorted(data["active_asps"], key=lambda a: (-a["window_done"], -a["done"]))[:12]
    if asps:
        rows = []
        for a in asps:
            pct = int(100 * a["done"] / a["total"]) if a["total"] else 0
            bar = (f'<div style="background:#eee;border-radius:4px;height:8px;width:100%"><div style="background:#1e90ff;height:8px;'
                   f'border-radius:4px;width:{pct}%"></div></div>')
            rows.append(f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;font-size:13px"><b>{_e(a["id"])}</b> {_e(_clip(a["title"], 60))}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;width:34%">{bar}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap;font-size:12px;color:#555">{a["done"]}/{a["total"]} ({pct}%)'
                        + (f'<br><span style="color:#28a745">+{a["window_done"]} this window</span>' if a["window_done"] else "")
                        + ('<br><span style="color:#b26a00">live queue only — understated</span>' if a.get("source") == "array" else "")
                        + "</td></tr>")
        out.append(_card("In progress", '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">' + "".join(rows) + "</table>"))

    # ---- learning + right now
    corr = h.get("corrected") or []
    if corr:
        lis = "".join(f'<li>{_e(c["at"])} — {_e(_clip(c["title"], 130))}</li>' for c in corr)
        out.append(_card("What we got wrong (hypotheses corrected this window)",
                         f'<ul style="margin:0;padding-left:18px;font-size:13px">{lis}</ul>', "#6f42c1", "#faf7ff"))
    if data["pulse"]:
        rows = []
        for p in data["pulse"]:
            active = p["age_h"] is not None and p["age_h"] <= 6
            state = _pill("active", "#28a745") if active else _pill(f"quiet {_age_str(p['age_h'])}", "#fd7e14")
            on = f'{_e(p["in_flight"])} {_e(_clip(p["in_flight_title"], 80))}' if p.get("in_flight") else '<span style="color:#888">between goals</span>'
            rows.append(f'<tr><td {TD}><b>{_e(p["agent"])}</b></td><td {TD}>{state}</td><td {TD}>{on}</td></tr>')
        out.append(_card("Each agent right now", '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">' + "".join(rows) + "</table>"))

    # ---- spend
    cost = data.get("cost") or {}
    if cost:
        inner = ""
        tiles = [_tile(t.get("label", ""), str(t.get("value", "")), str(t.get("sub", "")), "#222") for t in (cost.get("tiles") or [])[:5]]
        if tiles:
            inner += '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%"><tr>' + "".join(tiles) + "</tr></table>"
        if cost.get("lines"):
            inner += '<ul style="margin:8px 0 0;padding-left:18px;font-size:13px">' + "".join(f"<li>{_e(x)}</li>" for x in cost["lines"][:12]) + "</ul>"
        if cost.get("note"):
            inner += f'<p style="margin:8px 0 0;font-size:12px;color:#666">{_e(cost["note"])}</p>'
        if cost.get("as_of"):
            inner += (f'<p style="margin:4px 0 0;font-size:11px;color:{"#dc3545" if cost.get("stale") else "#999"}">as of {_e(cost["as_of"])}'
                      + (" — STALE" if cost.get("stale") else "") + "</p>")
        out.append(_card("Spend" + (f" — {cost['headline']}" if cost.get("headline") else ""), inner or "<p style='margin:0;color:#888'>no figures</p>", "#20c997", "#f3fffb"))

    # ---- product signals
    src = (data["outcome"] or {}).get("sources") or {}
    sig = []
    ci = src.get("ci") or {}
    if ci.get("status") and ci.get("status") != "unavailable":
        sig.append(f'CI: {ci.get("passed", "?")}/{ci.get("runs", "?")} runs passed (pass rate {ci.get("pass_rate", "?")})')
    for name in ("service", "git"):
        row = src.get(name) or {}
        if row.get("status") and row.get("status") != "unavailable":
            sig.append(f'{name.title()}: {row.get("status")}' + (f' ({row.get("note")})' if row.get("note") else ""))
    if sig:
        stamp = (data["outcome"] or {}).get("computed_at") or ""
        out.append(_card("Product signals", '<ul style="margin:0;padding-left:18px;font-size:13px">' + "".join(f"<li>{_e(x)}</li>" for x in sig) + "</ul>"
                         + (f'<p style="margin:6px 0 0;font-size:11px;color:#999">as of {_e(stamp)}</p>' if stamp else "")))

    # ---- notes
    if notes.strip():
        _note_lines = notes.strip().splitlines()
        lines = _note_lines[:_NOTES_MAX_LINES]
        _dropped = len(_note_lines) - len(lines)
        _body = "".join(f'<p style="margin:0 0 4px;font-size:13px">{_e(l)}</p>' for l in lines)
        if _dropped > 0:
            _body += (f'<p style="margin:6px 0 0;font-size:12px;color:#b00020">'
                      f'[... {_dropped} more line(s) TRUNCATED — full text in '
                      f'agents/{_e(agent)}/COMPLETION-REPORT.md]</p>')
        out.append(_card(f"Notes from {agent}", _body, "#6c757d", "#fafafa"))

    out.append("</div>")  # padding
    out.append('<div style="padding:12px 22px;background:#f8f9fa;border-top:1px solid #eee;font-size:12px;color:#999">'
               f'Full agent-side report: agents/{_e(agent)}/COMPLETION-REPORT.md (git history is the archive). Reply to this email to reach the fleet mailbox.</div>')
    out.append("</div></div></body></html>")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", "") or "agent")
    ap.add_argument("--since", default="", help="ISO timestamp; default: last-report-timestamp or 48h")
    ap.add_argument("--notes-file", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--html-out", default="", help="also write the HTML email twin here")
    ap.add_argument("--world", default="")
    ap.add_argument("--max-items", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="emit the gathered data instead of markdown")
    args = ap.parse_args(argv)
    world = Path(args.world) if args.world else Path(WORLD_DIR)
    now = _now()
    since = _ts(args.since) if args.since else None
    if not since:
        try:
            lr = agents_root() / args.agent / "session" / "last-report-timestamp"
            since = _ts(lr.read_text().strip()) if lr.exists() else None
        except Exception:
            since = None
    if not since:
        since = now - timedelta(hours=48)
    data = gather(world, args.agent, since, now, args.max_items)
    if args.json:
        print(json.dumps(data, indent=1, default=str))
        return 0
    notes = Path(args.notes_file).read_text(encoding="utf-8", errors="replace") if args.notes_file and Path(args.notes_file).exists() else ""
    md = render(data, agent=args.agent, since=since, now=now, notes=notes, max_items=args.max_items)
    if args.html_out:
        hp = render_html(data, agent=args.agent, since=since, now=now, notes=notes, max_items=args.max_items)
        Path(args.html_out).write_text(hp, encoding="utf-8")
        print(f"[completion-digest] wrote {args.html_out} ({len(hp)} bytes)")
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[completion-digest] wrote {args.out} ({len(md)} bytes)")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
