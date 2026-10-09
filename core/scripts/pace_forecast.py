#!/usr/bin/env python3
"""pace_forecast: should THIS session take a HIGH goal, or leave it to faster ones? ()

THE PROBLEM, MEASURED 2026-10-07 over the world queue's completed goals since 2026-09-30, first
claim to close: zakcode Body workers took a median 21.7 h per goal (25 closes) and Claude Code
worker sessions 1.2 h (74 closes). Every Body walks the same ranking, so a slow Body that claims
an urgent goal holds it for about a day while a faster one would have reached it and closed it
within hours. That morning one zakcode Body had gone 30 h without a close.

THE RULE. Before a session claims a HIGH goal it compares two finish times:

    fast path = k / R + P_fast          own path = P_own

  k       the goal's place among the rows a worker may take, in the scorer's order (1 = next)
  R       goals per hour CLOSED over the last P_own hours that the faster sessions live NOW
          ran, counting only goals a worker may take (the goal is marked for a worker, or a
          worker closed it), each close credited to the session that ran the goal
  P_fast  the slowest of those faster harnesses' median hours per goal, so the fast path is
          never flattered
  P_own   the median hours per goal of this session's own harness

It yields the goal when the fast path finishes first and takes it otherwise. It also takes it
when any input is missing: its own harness unknown, a pace measured from fewer than
MIN_PACE_SAMPLES closes, no faster session live, or none of their closes inside the window. That
is how every Body behaved before this module, so a blind input can only restore the old
behaviour, never strand a goal. Every input is measured and recomputed at each walk: there is no
flag, no timeout and nothing to tune by hand.

ONE RULE FOR EVERY BODY (g-375-157). The rule never asks the deciding session's role or a
harness's name: its inputs are each harness's pace, measured on worker closes, and the sessions
live now. So a Claude Code Body and a zakcode Body run the same code, and whichever harness
measures faster is the one deferred to. Two walks call it, and both write the select census
whose pace-yield sanction the claim gate reads:
  - a worker's select-walk (worker_execute.py), which drops reducer-only rows before its cut;
  - the reducer's pick, which pipes goal-selector.sh through `walk` below. It keeps the rows only
    it can take and never forecasts one, because no faster worker could take it, and it
    forecasts a HIGH row only when the goal is marked for a worker: most goals carry no mark,
    and leaving unmarked reducer-only work to the workers would leave it to nobody.
    The all-blocked handler's queue re-checks run the same pipe, so a queue that holds only
    yields reads as all-blocked there too (all_yielded_report). A session's first action, which
    the session before it took from a bare selector run (aspirations-consolidate), is not
    walked.
Neither walk is inside goal-selector, which stays role-blind.

WHY R COUNTS ONLY SESSIONS LIVE NOW (g-375-157). The first version armed on any live faster Body
but counted every faster worker close in the window. On 2026-10-07 seven Claude Code worker
sessions stopped between 03:59 and 04:13Z while the Claude Code reducer stayed up: the live test
would have passed on the reducer while R kept counting the stopped workers' closes, for up to
P_own hours. Crediting each close to a session live now makes the two count one population, and a
stopped session's closes stop counting once it stops being live (the live-set bias below says
when that is).

WHY A CLOSE COUNTS FOR THE SESSION THAT RAN IT. A close is credited to `executed_by_sid`, which
every claim rewrites, not to the session that closed it. Measured over the week before
2026-10-07T06:19Z: 88 of 89 worker closes were made by the session that had run the goal, but 30
of the 103 other closes that name a closing session closed work another session ran, 16 of them
in sweeps of closes under two minutes apart. Credited to the closer, a live reducer's sweep would
raise R by every goal it swept. Credited to the session that ran it, a swept goal counts for that
session while it is live, and a goal no session ran counts for none.

WHY R COUNTS ONLY GOALS A WORKER MAY TAKE. k counts places among the rows a worker may take, so R
must be the rate at which those places are used up. Reducer-only work uses none of them, and with
workers live, reducer_selection_policy hoists reducer-only goals to the reducer's top slot. Most
goals carry neither a skill nor a role (about 98%, worker_execute), so which role may take them
is unknown. A goal counts when it is marked for a worker, meaning worker_execute judges it
eligible (a worker role, or a skill a worker may run, with nothing contradicting it), or when a
worker closed it; a reducer's or a sweep's close of an unmarked goal counts for nobody. So a
live reducer's own closes count only for goals marked for a worker, and an unknown lowers R.
Counting every goal that is not reducer-only instead would have credited the sessions that
closed goals with no worker role in the 21.7 h before 2026-10-07T04:13Z with 31 closes; this
rule credits them none, and the workers' 36 closes count under both.

WHY R IS COUNTED, NOT MODELLED. The first draft modelled R as live Bodies divided by median hours
per goal. Measured 2026-10-07T05:25Z that gave 11 / 1.19 = 9.2 goals an hour for the Claude Code
workers, who had in fact closed 33 worker goals in the previous 21.7 h, 1.5 an hour, from 7
distinct closers. A worker's median goal takes 1.2 h, but reviews, parks, released claims and
selection sit between its closes, so it closes one about every 5 h. The model would have left
every HIGH goal in the first ~190 places to workers that reach about 31 places in that time. The
window is P_own because that is the question being asked: what will the faster sessions have
finished by the time this one would finish?

WORKED EXAMPLE, those 05:25Z numbers. R = 33 / 21.7 = 1.52 an hour, so a zakcode Body (P_own
21.7 h) leaves a HIGH goal to the Claude Code workers (P_fast 1.2 h) at places 1 to 31, where
k / 1.52 + 1.2 < 21.7, and takes one at place 32 or deeper. With no faster session live, or none
of their closes inside the window, every Body takes.

MEDIUM and LOW goals are never forecast. Pace is keyed on the HARNESS, from the close stamp
`completed_by_harness` and the carrier field `harness` (both resolved by _runtime.sh
rt_judge_provenance). Every walk that forecasts a HIGH row logs one gate-firings row (GATE_ID)
carrying the basis and each decision's k and finish times, so a reader can recompute it, and
the row's caller names the walk.

WHAT THE INPUTS MEASURE, and their known biases:
  - A pace runs from `started`, the goal's FIRST claim, to `completed_at`. A goal released and
    re-claimed carries its whole history into the closer's harness, so the median, not the mean,
    is used: it shrugs off those long tails.
  - Only WORKER closes stamped with a harness set a pace. iteration-close do_verify writes both
    stamps (`completed_by_role` and `completed_by_harness`) once per close of the goal the worker
    ran, so a sweep that closes many goals through another path sets no pace (the batch drain of
    guard-4237). Over the 33 closes above the busiest hour held 6 against a typical 2.5. The
    reducer's closes carry neither stamp, because BODY_ROLE is unset on the reducer, so a
    reducer's own pace is its harness's worker pace. The goals marked for a worker that it ran
    still count toward R, which reads the harness from the live session's carrier, not from the
    close.
  - R counts closes, not starts, from the world queue's live and archive records; agent-queue
    closes are not read. It lags a faster fleet that is growing, it loses a faster Body's earlier
    closes when the Body restarts under a new sid, it drops a close of a goal no session ran (no
    executed_by_sid), and it drops a close no worker made of a goal not marked for a worker.
    Each of these lowers R, which can only make this session take more. One raises it: a claim
    rewrites executed_by_sid, so a session that re-claims a goal another session ran and then
    closes it is credited with it. Only a worker's close or a goal marked for a worker counts
    at all, which bounds it.
  - The live set reads each Body's heartbeat carrier as body_hold.evaluate_carrier judges it, so
    a Body that died less than CARRIER_FRESH_MINUTES ago still counts as live for that long.
  - Only Bodies of the SAME agent count: they walk the same queue. A faster Body of another agent
    may not be allowed the goal, and leaving it out can only make this session take more, the old
    behaviour. What a Body may take can still differ by box, which is the next bullet.
  - NOT MODELLED: a HIGH goal that only the slower class can do. Two kinds are known: a goal
    whose requires_capability only this box meets, which the faster Bodies' selectors never rank
    (goal-selector collect_candidates), and reducer-only work that no field marks, which a worker
    releases unmarked. The faster sessions pass it by, R stays high, and a slower worker keeps
    leaving it to them. The second kind still reaches the reducer, which never leaves an
    unmarked goal to the workers; the first can wait walk after walk. Its signature in the log
    is the same goal_id yielded at a small k walk after walk (g-375-159).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE / "gates"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# Every other import is lazy, so the reducer's `walk` starts on the standard library alone and
# a module that will not import costs the forecast, inside walk_main's guard, never the pick.

HIGH = "HIGH"
WORKER_ROLE = "worker"
# A median over fewer closes is one goal's accident, not a pace.
MIN_PACE_SAMPLES = 5
# The id in core/config/gates.yaml this module's gate-firings rows carry.
GATE_ID = "pace-forecast"
# The firing's decision per path, named by its effect on the walk (guard-1743): a yield
# withholds rows from this worker, an armed walk with no yield passed every HIGH row, a disarmed
# forecast never fired, and a failed measurement let the walk proceed unchecked.
DECISION_BY_PATH = {"yielded": "block", "all_taken": "pass", "disarmed": "noop",
                    "measure_failed": "fail_open"}
_BASIS_LOGGED = ("own_harness", "own_pace_hours", "window_hours", "rate_per_hour",
                 "fast_pace_hours", "faster", "armed", "reason", "carriers_read_via", "error")
# The selector's full rows carry a goal's priority only as raw["priority"], the number
# goal-selector's PRIORITY_MAP gives it (pinned equal by a test); its brief rows name it.
_RAW_PRIORITY_NAMES = {3: "HIGH", 2: "MEDIUM", 1: "LOW"}
# How many kept rows the reducer's census lists for the claim gate. The reducer reads about a
# dozen full rows (its tool output keeps 30,000 chars), and a deeper claim names its own code.
CENSUS_ROWS = 40
# The caller the reducer's walk logs under; a worker's select-walk logs under its own name.
WALK_CALLER = "pace_forecast.py walk"
# The reason the walk's all-blocked report gives each row, beside goal-selector's own reasons.
YIELD_REASON = "pace_yield"


def _stamp(value):
    """A goal or carrier timestamp as a naive UTC datetime, or None.

    Goal timestamps are naive UTC (TZ=UTC fleet-wide). An offset-carrying value is
    converted, never compared aware against naive, which raises.
    """
    text = str(value or "").strip()
    if text.endswith(("Z", "z")):
        text = text[:-1]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _completed_goals(world_dir: Path):
    """Completed goals from the world queue's live AND archive records.

    The archive is read because a completed goal leaves the live file when its aspiration is
    archived (worker-artifact-rate.py, guard-676): without it a harness that closes rarely
    would lose its pace exactly when it is slowest. A goal in both is counted once and the live
    record wins, since archival is a move.
    """
    seen = set()
    for name in ("aspirations.jsonl", "aspirations-archive.jsonl"):
        for rec in _jsonl(Path(world_dir) / name):
            for goal in rec.get("goals") or []:
                if not isinstance(goal, dict) or goal.get("status") != "completed":
                    continue
                gid = str(goal.get("id") or goal.get("goal_id") or "")
                if gid and gid in seen:
                    continue
                if gid:
                    seen.add(gid)
                yield goal


def _worker_word(row: dict) -> str:
    """worker_execute's one-word verdict on `row` ("eligible", "undetermined" or
    "reducer-only"), as a worker on ANOTHER box sees it.

    It is the judgement a worker's select-walk makes, not a second copy of it (guard-2783), with
    the claim probe answering "not held": an agent-queue goal is open only to the box holding
    that agent's runner claim, and the faster workers this decision defers to are elsewhere.
    """
    import worker_execute  # lazy: worker_execute imports this module lazily in turn
    verdict = worker_execute.goal_eligibility(
        row.get("skill"), row.get("executable_by_role"), source=row.get("source"),
        claim_probe=lambda _agent: (False, "pace forecast: judged as a worker on another box"))
    return worker_execute._verdict_word(verdict)


def read_closes(world_dir: Path, sids=()) -> tuple:
    """(pace closes, session closes) from one pass over the world queue's closes.

    pace closes: {harness: [(started or None, completed_at), ...]} over the WORKER closes
    stamped with a harness, the input harness_paces turns into medians.
    session closes: {sid: [completed_at, ...]} over the closes of goals a worker may take, each
    credited to the session that RAN the goal (executed_by_sid, rewritten at every claim)
    whoever closed it. A goal a worker may take is one marked for a worker, or one a worker
    closed: most goals carry neither a skill nor a role, and a reducer's or a sweep's close of
    such a goal says nothing about it, so it counts for nobody. That is R's input, and `sids` is
    the live set, so nothing else is judged.

    Both stamps are naive UTC on the closer's own clock (guard-2613): completed_at is written by
    the status change to completed that do_verify makes at the close itself, not by a later
    sweep, so a trailing window over it counts closes when they happened.
    """
    closes, by_session = {}, {}
    wanted = {str(s) for s in sids or () if s}
    for goal in _completed_goals(world_dir):
        end = _stamp(goal.get("completed_at"))
        if end is None:
            continue
        harness = str(goal.get("completed_by_harness") or "")
        if goal.get("completed_by_role") == WORKER_ROLE and harness:
            closes.setdefault(harness, []).append((_stamp(goal.get("started")), end))
        sid = str(goal.get("executed_by_sid") or "")
        if sid not in wanted:
            continue
        word = _worker_word({"skill": goal.get("skill"), "source": "world",
                             "executable_by_role": goal.get("executable_by_role")})
        # An unknown must lower R, never raise it: an unmarked goal counts only as a worker's close.
        if word == "eligible" or (word == "undetermined"
                                  and goal.get("completed_by_role") == WORKER_ROLE):
            by_session.setdefault(sid, []).append(end)
    return closes, by_session


def harness_paces(closes: dict) -> dict:
    """{harness: {"median_hours": float, "n": int}}, first claim to close.

    A close with no first-claim stamp, or one not before its close, has no duration: it still
    counts as throughput in `basis`, but not here.
    """
    hours = {}
    for harness, pairs in closes.items():
        spans = [(end - start).total_seconds() / 3600.0
                 for start, end in pairs if start is not None and end > start]
        if spans:
            hours[harness] = spans
    return {h: {"median_hours": round(statistics.median(v), 3), "n": len(v)}
            for h, v in sorted(hours.items())}


def live_sessions(carrier_rows, agent: str, now: datetime) -> dict:
    """{harness: [sid, ...]}: the live, active Bodies of `agent` by the harness each publishes,
    from worker_stall.enumerate_carriers rows.

    A Body counts when its carrier is live by body_hold's own test and its state is `active`:
    a parked Body takes nothing until it resumes, and a session with no Body manifest (an
    interactive one) publishes an empty state. Workers and the reducer count alike, and R then
    counts each one's own closes.
    """
    from body_hold import evaluate_carrier  # lazy: see the note on the imports
    live = {}
    for row in carrier_rows or ():
        doc = row.get("doc") if isinstance(row, dict) else None
        if not isinstance(doc, dict) or row.get("agent") != agent:
            continue
        sid, harness = str(row.get("sid") or ""), str(doc.get("harness") or "")
        if not sid or not harness or doc.get("body_state") != "active":
            continue
        if evaluate_carrier(doc, sid=sid, now=now)["live"]:
            live.setdefault(harness, set()).add(sid)
    return {harness: sorted(sids) for harness, sids in sorted(live.items())}


def own_harness(carrier_rows, sid: str) -> str:
    """The harness this session's own carrier publishes, or "" when it publishes none."""
    for row in carrier_rows or ():
        if isinstance(row, dict) and sid and row.get("sid") == sid:
            doc = row.get("doc")
            return str(doc.get("harness") or "") if isinstance(doc, dict) else ""
    return ""


def _measured(paces: dict, harness: str):
    pace = (paces or {}).get(harness) or {}
    if int(pace.get("n") or 0) < MIN_PACE_SAMPLES:
        return None
    median = pace.get("median_hours")
    return float(median) if isinstance(median, (int, float)) and median > 0 else None


def basis(own: str, closes: dict, live: dict, session_closes: dict, now: datetime) -> dict:
    """What every HIGH pick in one walk shares.

    `closes` sets the paces (read_closes' first half), `live` is live_sessions' map, and
    `session_closes` is read_closes' second half for those sessions. The forecast is ARMED only
    when this session's harness and pace are known, at least one faster harness has a measured
    pace and a live session, and those live sessions closed at least one goal a worker may take
    in the last P_own hours. `reason` names what disarmed it.
    """
    paces = harness_paces(closes or {})
    own_pace = _measured(paces, own) if own else None
    out = {"own_harness": own or None, "own_pace_hours": own_pace, "window_hours": own_pace,
           "faster": {}, "rate_per_hour": None, "fast_pace_hours": None, "armed": False}
    if not own:
        return dict(out, reason="this session's harness is unknown")
    if own_pace is None:
        return dict(out, reason=f"the {own} pace has fewer than {MIN_PACE_SAMPLES} worker closes")
    since = now - timedelta(hours=own_pace)
    faster = {}
    for harness, sids in sorted((live or {}).items()):
        pace = _measured(paces, harness)
        if harness != own and sids and pace is not None and pace < own_pace:
            # Only the closes of sessions live NOW: the live test and R count one population.
            closed = sum(1 for sid in sids for end in (session_closes or {}).get(sid, ())
                         if end >= since)
            faster[harness] = {"live": len(sids), "pace_hours": pace, "closed_in_window": closed}
    if not faster:
        return dict(out, reason="no live session is measured faster")
    closed = sum(f["closed_in_window"] for f in faster.values())
    if not closed:
        return dict(out, faster=faster,
                    reason=(f"no live faster session closed a goal a worker may take in the "
                            f"last {own_pace:g} h"))
    # R is NOT rounded: closed >= 1 and own_pace > 0 make it positive, and a rounded
    # value could reach 0 and divide by zero in forecast(). JSON keeps the float exactly.
    return dict(out, faster=faster, rate_per_hour=closed / own_pace,
                fast_pace_hours=max(f["pace_hours"] for f in faster.values()), armed=True,
                reason=(f"live faster sessions closed {closed} goal(s) a worker may take in the "
                        f"last {own_pace:g} h, {sum(f['live'] for f in faster.values())} live now"))


def forecast(k: int, walk_basis: dict) -> dict:
    """Take or yield the HIGH goal at place `k` (1 = the next row a worker would take).

    Computed from the basis's own values, so a reader of the logged basis gets the same decision.
    """
    if not walk_basis.get("armed"):
        return {"decision": "take", "k": k, "reason": walk_basis.get("reason")}
    fast = k / walk_basis["rate_per_hour"] + walk_basis["fast_pace_hours"]
    own = walk_basis["own_pace_hours"]
    return {"decision": "yield" if fast < own else "take", "k": k,
            "fast_finish_hours": round(fast, 2), "own_finish_hours": own,
            "reason": ("faster sessions finish it first" if fast < own
                       else "this session finishes it first")}


def measure_basis(agent: str, sid: str, now: "datetime | None" = None) -> dict:
    """The live basis for one walk: carriers from the store of record, closes from the world queue.

    Called once per walk, and only when the walk forecasts a HIGH row. Both walks run right after a
    selector run, which has just refreshed the local aspirations cache (goal-selector
    refresh_aspiration_caches), so the world files read here are that fresh.
    """
    from _paths import WORLD_DIR, agents_root  # lazy: the pure functions above need neither
    import worker_stall  # lazy: the store-of-record carrier enumeration
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    rows, meta = worker_stall.enumerate_carriers(agents_root())
    live = live_sessions(rows, agent, now)
    closes, session_closes = read_closes(Path(WORLD_DIR),
                                         [sid_ for sids in live.values() for sid_ in sids])
    walk_basis = basis(own_harness(rows, sid), closes, live, session_closes, now)
    walk_basis["carriers_read_via"] = meta.get("read_via")
    return walk_basis


def walk_firing(walk_basis: dict, forecasts: list) -> tuple:
    """(decision_path, gate decision, extra) for one walk's gate-firings row.

    `forecasts` is the walk's census list, one entry per HIGH row it forecast, in walk order
    (`high_rows` counts them). An armed walk logs every entry as [goal_id, k,
    fast_finish_hours, decision]; with the basis's rate_per_hour, fast_pace_hours and
    own_pace_hours a reader recomputes each one. A disarmed walk took every HIGH row it
    forecast, for the logged reason, so it logs only how many it forecast.
    """
    yielded = [f for f in forecasts if f.get("decision") == "yield"]
    if walk_basis.get("error"):
        path = "measure_failed"
    elif not walk_basis.get("armed"):
        path = "disarmed"
    else:
        path = "yielded" if yielded else "all_taken"
    extra = {"decision_path": path,
             "basis": {k: walk_basis.get(k) for k in _BASIS_LOGGED if k in walk_basis},
             "high_rows": len(forecasts), "yielded": len(yielded)}
    if walk_basis.get("armed"):
        extra["forecasts"] = [[f.get("goal_id"), f.get("k"), f.get("fast_finish_hours"),
                               f.get("decision")] for f in forecasts]
    return path, DECISION_BY_PATH[path], extra


def log_walk(walk_basis: dict, forecasts: list, *, caller: str) -> None:
    """Append this walk's gate-firings row ( check 1). Best-effort, NEVER raises.

    `caller` names the walk. `extra` and not `payload`: _gate_log stores `extra` verbatim and
    reduces `payload` to a hash.
    """
    try:
        import _gate_log
        path, decision, extra = walk_firing(walk_basis, forecasts)
        _gate_log.log(GATE_ID, decision, caller=caller, trigger_matched=path, extra=extra)
    except Exception:  # noqa: BLE001 -- telemetry must never cost the walk
        pass


class WalkPace:
    """pace(k) for one walk: a worker's select-walk or the reducer's `walk` ().

    The forecast's basis is measured ONCE, at the first HIGH row the walk forecasts, so a walk
    that forecasts none reads no store. A measurement or forecast that fails disarms the
    forecast for the rest of the walk and every later HIGH row is taken, which is how every
    Body behaved before it existed: the forecast can only ever leave work to others, never cost
    the walk. `caller` names the walk in its gate-firings row.
    """

    def __init__(self, agent, sid, caller):
        self.agent, self.sid, self.caller, self.basis = agent, sid, caller, None

    def __call__(self, k):
        try:
            if self.basis is None:
                self.basis = measure_basis(self.agent, self.sid)
            if self.basis.get("armed"):
                return forecast(k, self.basis)
        except Exception as exc:  # noqa: BLE001 -- advisory input; the walk must stand
            # Keep whatever was measured beside the error, so the census and the
            # log show both what the forecast knew and why it stopped.
            error = f"{type(exc).__name__}: {exc}"
            measured = self.basis if isinstance(self.basis, dict) else {}
            self.basis = dict(measured, armed=False, error=error,
                              reason=f"forecast unavailable ({error})")
        return {"decision": "take", "k": k, "reason": self.basis.get("reason")}

    def record(self, forecasts):
        """Log the walk's HIGH decisions to gate-firings, once per walk that forecast a
        HIGH row. Best-effort: telemetry must never cost the walk."""
        if self.basis is None:
            return
        try:
            log_walk(self.basis, forecasts, caller=self.caller)
        except Exception:  # noqa: BLE001
            pass


def _priority(row: dict):
    """A ranked row's priority name: brief rows carry it, full rows only as raw["priority"]."""
    name = row.get("priority")
    if isinstance(name, str):
        return name.upper()
    return _RAW_PRIORITY_NAMES.get((row.get("raw") or {}).get("priority"))


def reducer_view(rows: list, pace) -> tuple:
    """The reducer's pass over the selector's ranking: (the ranking less its yields, census).

    The forecast a worker's select-walk applies, from the reducer's side of the queue. A row no
    worker may take is kept, and never forecast or counted in k: only this reducer can take it,
    so leaving it would leave it to nobody. Every other row is a place k, and a HIGH one marked
    for a worker is forecast there and dropped when it yields, as in worker_execute.worker_view.
    A HIGH row with no such mark, most of them, is taken unforecast: if it is reducer-only work
    no field marks, a worker releases it, and leaving it would leave it to nobody too. The walk
    stops judging at its first FORECAST take: forecast() only grows with k, so every later HIGH
    row would be taken too, and the rest of the ranking passes through untouched. An unforecast
    take proves nothing about the rows below it, so the walk goes on past one. Nothing is
    re-sorted (guard-5135).

    The census carries what the claim gate reads (scorer_top, scorer_top_yielded, rows), so a
    claim of a kept row over a yielded top is sanctioned exactly as a worker's is.
    """
    kept, forecasts = [], []
    takeable = walked = 0
    top_yielded = False
    for i, row in enumerate(rows):
        walked = i + 1
        word = _worker_word(row) if isinstance(row, dict) else "reducer-only"
        if word == "reducer-only":
            kept.append(row)
            continue
        takeable += 1
        if _priority(row) != HIGH or word != "eligible":
            kept.append(row)
            continue
        verdict = pace(takeable)
        forecasts.append({"goal_id": row.get("goal_id"), "k": takeable,
                          "decision": verdict.get("decision"),
                          "fast_finish_hours": verdict.get("fast_finish_hours")})
        if verdict.get("decision") == "yield":
            top_yielded = top_yielded or i == 0
            continue
        kept.extend(rows[i:])
        break
    census = {"walker": "reducer", "ranked_total": len(rows), "walked": walked,
              "scorer_top": rows[0].get("goal_id") if rows and isinstance(rows[0], dict) else None,
              "scorer_top_dropped": False, "scorer_top_yielded": top_yielded,
              "pace_yielded": sum(f["decision"] == "yield" for f in forecasts),
              "forecasts": forecasts,
              "rows": [{"goal_id": r.get("goal_id")} for r in kept[:CENSUS_ROWS]
                       if isinstance(r, dict)]}
    return kept, census


def all_yielded_report(rows: list, census: dict, walk_basis: dict) -> dict:
    """The selector's all-blocked report, for a ranking whose every row was left to faster sessions.

    An empty list would read as an empty queue. The loop answers an empty queue by generating
    work and then re-checking the queue, which still holds the yielded rows, so it would re-enter
    at once: the dry spin of g-115-2084. The goals exist and this session should not take them,
    which is what all-blocked means, so the loop goes to its all-blocked handler and waits there.
    The keys are goal-selector.py's own, blocked_goals capped at 10 as it caps them; by_reason
    names only this report's reason, and walk_main records the route as the selector does.
    """
    titles = {r.get("goal_id"): r.get("title", "") for r in rows if isinstance(r, dict)}
    own = (walk_basis or {}).get("own_pace_hours")
    yielded = [f for f in census["forecasts"] if f.get("decision") == "yield"]
    return {"candidates": [], "all_blocked": True, "blocked_count": len(yielded),
            "by_reason": {YIELD_REASON: len(yielded)},
            "blocked_goals": [{"goal_id": f["goal_id"], "title": titles.get(f["goal_id"], ""),
                               "reason": YIELD_REASON,
                               "detail": (f"faster sessions finish it first: in "
                                          f"{f['fast_finish_hours']} h, against {own} h here")}
                              for f in yielded[:10]]}


def _mark_all_blocked() -> None:
    """Record that this cycle routed to all-blocked, as goal-selector does when it prints its own
    report (g-357-88), so dry-spin-guard can tell a handler that ran from one that was narrated.

    This mirrors goal-selector's _write_allblocked_marker, which is the source of truth,
    including its refusal under pytest unless GOAL_SELECTOR_ALLBLOCKED_MARKER opts in: a fixture
    marker in a real deployment's loop_state would read as a live route. Fail-open, as there: a
    missing marker costs one slow cycle, so a failure is printed, never raised.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
            "GOAL_SELECTOR_ALLBLOCKED_MARKER"):
        print("[pace-walk] all_blocked marker SUPPRESSED under pytest (g-357-88)", file=sys.stderr)
        return
    try:
        import subprocess
        r = subprocess.run([sys.executable, str(_HERE / "loop-state-bump-counters.py"),
                            "--all-blocked-marker"], capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or "WARN" in (r.stderr or ""):
            print(f"[pace-walk] all_blocked marker NOT written (rc={r.returncode}): "
                  f"{(r.stderr or '').strip()[:300]}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- never cost the pick
        print(f"[pace-walk] all_blocked marker write skipped ({type(exc).__name__}: {exc})",
              file=sys.stderr)


def _write(data: bytes) -> None:
    """stdout as UTF-8 bytes, whatever the console's codec: the selector writes UTF-8, and a
    Windows console's default codec would refuse or rewrite a non-ASCII title."""
    out = getattr(sys.stdout, "buffer", None)
    if out is None:
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        return
    sys.stdout.flush()
    out.write(data)
    out.flush()


def walk_main(raw: bytes) -> int:
    """`walk`, the reducer's pace pass (aspirations-select Phase 2).

    stdin is goal-selector.sh's output; stdout is that same output less the HIGH rows faster
    sessions finish first, byte for byte when nothing was yielded. When every row was yielded,
    stdout is the all-blocked report for them (all_yielded_report). A non-list result (the
    selector's own all-blocked report) passes through untouched: there is nothing to pick. Empty
    or unparseable input exits 3 and prints nothing to stdout, so a failed selector run can never
    read as an empty queue; the selector's own stderr, printed above, says why it failed.
    """
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        print("[pace-walk] FATAL: no selector output on stdin. goal-selector.sh's own message "
              "above says why. This is NOT an empty queue: an empty ranking prints '[]'.",
              file=sys.stderr)
        return 3
    try:
        ranked = json.loads(text)
    except ValueError as exc:
        print(f"[pace-walk] FATAL: the selector output is not JSON ({len(raw)} bytes): {exc}",
              file=sys.stderr)
        return 3
    if not isinstance(ranked, list):
        _write(raw)
        return 0
    agent = os.environ.get("MIND_AGENT") or ""
    sid = os.environ.get("MIND_SID") or ""
    try:
        pace = WalkPace(agent, sid, caller=WALK_CALLER)
        kept, census = reducer_view(ranked, pace)
        out = raw
        if census["pace_yielded"]:
            report = kept if kept else all_yielded_report(ranked, census, pace.basis)
            out = (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except Exception as exc:  # noqa: BLE001 -- the forecast must never cost the pick
        # Every row is shown, as before the forecast existed, and no census is written. Only a
        # census an earlier walk wrote, for the same scorer top and inside the claim gate's
        # freshness window, can then sanction a claim past that top.
        print(f"[pace-walk] WARNING: the HIGH-goal pace forecast failed "
              f"({type(exc).__name__}: {exc}); every row is shown", file=sys.stderr)
        _write(raw)
        return 0
    if pace.basis is not None:
        # The forecast ran on a HIGH row: say what it decided and why, BEFORE
        # the ranking, which can run long enough for a reader's view to cut what follows it.
        census["pace"] = pace.basis
        print(f"[pace-walk] HIGH-goal pace forecast: {pace.basis.get('reason')}; "
              f"{census['pace_yielded']} HIGH row(s) left to faster sessions, which are not "
              f"shown and not yours to claim (g-375-157)", file=sys.stderr)
    if census["pace_yielded"] and not kept:
        _mark_all_blocked()
    _write(out)
    if pace.basis is not None:
        pace.record(census["forecasts"])
    if agent and sid:
        try:
            import worker_execute
            from _paths import agent_session_dir
            session_dir = Path(agent_session_dir(agent, sid))
            if not session_dir.is_dir():
                # write_select_census skips a missing dir in silence; the warning belongs here.
                raise FileNotFoundError(f"no session dir {session_dir}")
            worker_execute.write_select_census(census, session_dir)
        except Exception as exc:  # noqa: BLE001 -- the ranking is already out
            print(f"[pace-walk] WARNING: no select census written ({type(exc).__name__}: "
                  f"{exc}); a claim below a yielded top will be refused as a deviation",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["walk"]:
        print("usage: goal-selector.sh | pace_forecast.py walk", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(walk_main(sys.stdin.buffer.read()))
