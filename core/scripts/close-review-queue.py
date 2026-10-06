#!/usr/bin/env python3
"""close-review-queue.py — the POST-HOC close-review lane for closers whose model has no
measured closure pass rate (g-375-09, stage 1).

WHAT IT IS. Two read-only views over the same artifacts the blocking close-review gate
reads (`close-review-gate.py`, g-357-40) and its producer writes (`close-review-verdict.py`,
g-357-41):

  list   the open REVIEW REQUESTS no verdict answers yet (REVIEW REQUESTS below), then
         the closures by an UNCALIBRATED closer role (config: `close_review_gate.
         review_closer_roles`, plus the sample of each role in `review_sampled_roles`,
         where worker sits since 2026-10-04) that no independent verdict covers yet,
         ranked so the reviewer spends its budget where a defect costs most — tier 2
         first (`goal_close_risk_tier.classify`, the gate's own classifier), then HIGH
         priority, then the newest — and capped per run.
  stats  the pass rate over every verdict artifact, grouped by the CLOSER'S ROLE (see
         WHERE THE ROLE COMES FROM below), plus the relax-rule verdict written into
         `core/config/aspirations.yaml` next to the list.

WHY POST-HOC AND NOT THE BLOCKING GATE (an execution decision, alpha 2026-09-24). Both
Qwen-Body closures of 2026-09-23 carried a defect a reviewer catches in one step, and a
third was caught in flight (the goal's evidence). Enabling the gate for those closers
would REFUSE their close, and a lesser model handles a refusal the way zc-02 handled the
uncommitted-work gate the same day: it passes the override "exactly as the reducer
would". A refusal it can wave through teaches overriding, and one it cannot wave through
stalls the Body on a branch the worker loop does not have. Reviewing AFTER the close
keeps the Body's loop untouched, spends a stronger mind's time on the finished work (the
standard way small-model output is lifted), files a Fix goal on a REJECT (fix-forward
over already-merged work — guard-2852: the world, not the status field, is what is
wrong), and accumulates the pass rate the blocking decision needs BEFORE anyone is
blocked on it. The gate's tiering, artifact path and verdict format are reused unchanged
(rb-4452: one verdict format), so flipping to blocking later is a config change, not a
second system.

WHO MAY REVIEW. Independence is at the MIND level — the agent NAME, never the session
(coordination.md § "Independence Is At The MIND Level"). Every zc Body closes as `alpha`,
so alpha's own reducer must not review them: `list` partitions candidates by the running
reviewer and leaves same-mind closures for another agent's cycle, and the count it
reports for them is the honest "coverage I do not have" (guard-1760).

REVIEW REQUESTS (g-375-116). coordination.md's Review Gate has a closer post a
`review-request` and set `review_requested` on the goal, and has a peer pick it up. The
only reader was aspirations-all-blocked Step B0, which runs only when a peer's whole queue
is blocked and reads 12 hours of posts. Measured 2026-10-02: 40 goals carried
review_requested and none had a verdict, while this lane had written 160 for worker
closures. So `list` reads the goal field, the durable half of a request, and offers the
requests first, sharing the cap: an open one holds its tier-2 close until a verdict exists,
and a closed one was closed on the promise of a later review. A request has no age window,
since it stands until a verdict answers it, and it drops out when its goal is skipped,
superseded, expired or decomposed. A verdict answers only a request made at or before it
(g-375-119): while any verdict counted, a REJECT, then rework, then a fresh request was
never offered again, and the tier-2 close it held waited for good. Its closer is the mind
that did the work (completed_by, else executed_by, else claimed_by), and the reviewer
passes it as `--closer`. A request that names none is declined and counted, never listed:
independence that cannot be established is unproven.

RELAXING NEVER MEANS ZERO (g-375-82). A role that passes the relax rule MOVES to
`review_sampled_roles`: a deterministic `review_sample_rate` share of its closures (default
0.2) stays in `list`, and `stats` keeps reporting its pass rate. Dropping the role outright
would end the measurement that justified dropping it, and a later regression in that
closer would go unseen. The sample is a hash of the goal id, so every run and every box
agrees on which closures are in it and a sampled closure stays queued until reviewed.

WHERE THE ROLE COMES FROM (g-375-55). A verdict written since g-375-55 names its closer:
close-review-verdict.py copies `completed_by`, `completed_by_role` and `completed_by_sid`
from the goal record when it writes. Earlier verdicts named neither the closer nor the
role (their keys are checks, direction, fidelity, findings, goal_id, produced_by,
reviewed_at, reviewer and verdict); g-375-84 copied the closer onto those whose goal
record still existed on 2026-09-29, marking each `closer_backfilled_at`. A verdict that
still names no closer is joined to the goal record: the live closures first, then every
store through goal-resolve.py. A goal leaves the live query
when its aspiration is archived and when it is evicted, and a pass rate that loses those
verdicts measures the archival cadence, not the closer. An evicted goal survives only as
an id in its aspiration's census, so its role is gone: `stats` counts such a verdict in
its own bucket, beside "closer's role not reviewed" and "goal record not found
anywhere", and none of the three is folded into a role's rate.

COVERAGE AND WHAT AGED OUT (g-375-38). A pass rate says nothing about the closures nobody
reviewed, and `list` cannot count those: a closure past --since-hours lands in
`skipped.too_old` beside history the lane never could have seen, and once eviction takes
it (`aspirations_eviction.age_days`, 3 days, the same 72 hours) or its aspiration is
archived, the live query drops it without a trace. So `stats` reports, per role:
`coverage`, the reviewed share of every closure inside the lane's lifetime, printed beside
approve_rate so a relax_ok reading carries its denominator, and the unreviewed rest split
by where each one went: still in the window, aged out, archived, evicted. The lane starts
at the role's first verdict, and its reach `--since-hours` before that, because the first
run already listed that far back. An unreviewed closure from before the reach predates the
lane and is counted apart from the misses. A role with no verdict yet has no start, so
nothing it holds is called pre-lane. Evicted closures are counted from the census key
`evicted_by_closer_role`, which only goals evicted since g-375-38 carry: an earlier
eviction kept no role, and no count here includes those.

DELIVERY (g-306-558). A worker closes a goal and pushes its carrier ref, and the reducer
consumes the ref later. Until then the closer's HEAD at close (the `commit_sha`
iteration-close stamps, g-306-442) is on refs/workers/** and not on the target branch, so
a reviewer who looks now judges a fix that is absent from it. Measured on cc-08,
2026-10-05, over the live worker closures (120 with a sha, 106 resolvable here): this lane
runs every 0.89 h while a worker close waited a median 5.18 h for origin/main to contain
its sha (96 waited, p90 23.2 h, max 37.2 h; the 37 code closes among them a median 2.19 h,
p90 16.9 h, max 30.8 h). So `list` asks the ONE shared predicate
(`_delivery_gate.blocker_delivery_state`, the one the dependent-release paths ask) about
every worker closure that carries a commit_sha, code or not: the record holds no code
signal (files touched are an execution-time fact, goal_close_risk_tier.classify) and the
prose test the sweep uses missed 127 of 136 goals that had commits (g-115-3476):

  pending    definitively not on origin/main: NOT offered while it waits. It is reported under
             WAITING TO LAND with its age. Once that age reaches --strand-hours it is
             STRANDED, and is offered FIRST, marked as such: a carrier ref left unconsumed
             that long is a finding for the reviewer, whose delivery check (fresh-eyes-close
             check 1) decides APPROVE_WITH_NOTES or REJECT, and a REJECT's Fix goal is the
             follow-up goal. Withholding it would let it age out of --since-hours unreviewed.
  delivered, unknown, or not asked (no worker role, no sha): offered as before.

The stamped sha is the closer's HEAD, which can be a merge or another goal's commit, so a
pending state is a first screen on the carrier and never a verdict on the goal's own commits:
the reviewer still probes those one by one (close-review-inputs.py prints each).

THE FAIL DIRECTION IS THE PREDICATE'S, AND IT IS DELIBERATE (guard-2275). Only a definitive
"not reachable" defers. A probe that cannot run, a sha this clone never fetched and a
verdict it does not recognise all LIST the closure, because a false defer is a review that
never happens and a false list is the status quo. A failed refresh of origin/main and the
worker refs turns the deferral off for the run and says so, because a stale target reads
landed work as stranded (guard-5797). STRAND_HOURS is 36, not 24: of the 96 measured waits
9 reached 24 h, 2 reached 30 h and 1 reached 36 h (37.2 h, a close with no code commit), and
every one landed on its own, so 24 h would mark 9% of them STRANDED and 36 h about 1%. The
mark is a screen for the reviewer's own probe: a wrong one costs a review slot, never a
verdict. A closure waits inside the 72 h default of --since-hours for its whole 36 h wait;
a --since-hours below --strand-hours would age it out unreviewed, and `stats` would count
it under aged out.

REPORT-ONLY, ALWAYS. This never mutates a goal, never writes a verdict, never blocks
anything. (The delivery check's one side effect is `git fetch`, which refreshes this
clone's remote-tracking refs.) The verdict is the reviewer's to assert through
`/fresh-eyes-close`.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _goal_census import census_closer_role_ids  # noqa: E402

#: The config key (under `close_review_gate:` in core/config/aspirations.yaml) naming the
#: closer roles whose closures this lane reviews. One list, one concept: the roles listed
#: here are "closures reviewed after the fact"; if the blocking gate is ever scoped by
#: role, it reads THIS list — two lists for one class would drift (communication-clarity
#: rule 5).
CONFIG_KEY = "review_closer_roles"

#: Relax rule for the routing (outcome 2 of ): a role leaves FULL review only on
#: evidence — at least this many reviewed closures, an approval rate at or above the
#: floor, and no REJECT among the most recent `RELAX_RECENT` verdicts. Leaving full review
#: means moving to the sampled class below, never to none ().
RELAX_MIN_REVIEWED = 10
RELAX_MIN_APPROVE_RATE = 0.8
RELAX_RECENT = 5

#: The relaxed class: roles whose closures are reviewed by a deterministic sample of
#: `review_sample_rate` (config keys beside CONFIG_KEY). A rate outside (0, 1] reads as the
#: default, loudly, because a zero rate is exactly the drop-to-none this class replaces.
SAMPLED_KEY = "review_sampled_roles"
SAMPLE_RATE_KEY = "review_sample_rate"
DEFAULT_SAMPLE_RATE = 0.2
SAMPLE_SEED = "close-review-sample-v1"

TERMINAL_NOT_CLOSED = ("pending", "in-progress", "blocked", "skipped", "expired",
                       "decomposed", "superseded")

#: The statuses in which a review request still wants its verdict: open (the close waits on
#: it) or completed (closed on the promise of a later review). On any other status the
#: request is moot.
REQUEST_STATUSES = ("pending", "candidate", "in-progress", "blocked", "completed")

#: The closer role whose closures carry a commit_sha and can wait on a carrier ref. Only a
#: worker Body stamps one (iteration-close, ), so only its closures are asked.
WORKER_ROLE = "worker"

#: Hours a worker closure may wait to land before `list` reports it STRANDED. Measured, not
#: chosen (, cc-08, 2026-10-05, 96 worker waits): 9 reached 24 h, 2 reached 30 h and
#: 1 reached 36 h, and every one landed on its own, so 24 h would mark 9% STRANDED and 36 h
#: about 1%. Re-measure before moving it.
STRAND_HOURS = 36.0

#: The repository the delivery probe reads: this one, whose HEAD the stamp names.
REPO_ROOT = SCRIPT_DIR.parent.parent


# ─── shared definitions, loaded from the gate (one definition each) ────────────

def _gate():
    """close-review-gate.py by path (its filename is hyphenated): `verdict_path`,
    `read_verdict` and `releases_close` keep ONE definition in the tree."""
    cached = sys.modules.get("close_review_gate")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "close_review_gate", SCRIPT_DIR / "close-review-gate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["close_review_gate"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _tier():
    import goal_close_risk_tier  # type: ignore
    return goal_close_risk_tier


def _resolver():
    """goal-resolve.py by path (hyphenated too): the one lookup of a goal id across the
    live store, the archive and the eviction census."""
    cached = sys.modules.get("goal_resolve")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("goal_resolve", SCRIPT_DIR / "goal-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["goal_resolve"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _NoFetchProber:
    """commit-reachability.py's `triage` with its per-call fetch off. `list` refreshes the
    refs once for the whole run (refresh_delivery_refs); a fetch per closure would turn a
    report of a second or two into minutes."""

    def __init__(self, module):
        self._module = module

    def triage(self, repo, sha, target_ref="origin/main"):
        return self._module.triage(repo, sha, target_ref=target_ref, do_fetch=False)


def refresh_delivery_refs(repo=REPO_ROOT, timeout: int = 300) -> tuple[bool, str]:
    """Fetch origin's branches and the worker refs once, with the refspecs the
    /fresh-eyes-close delivery probe uses. Returns (ok, detail). A stale origin/main reads
    landed work as stranded (guard-5797), so the caller turns the deferral off on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--prune", "origin",
             "+refs/heads/*:refs/remotes/origin/*",
             "+refs/workers/*:refs/remotes/_reach_workers/*"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, f"rc={proc.returncode} {proc.stderr.strip()[:200]}"
    return True, ""


def delivery_probe(repo=REPO_ROOT, prober=None):
    """goal -> (state, detail), where state is the shared predicate's delivered, pending or
    unknown. `prober` is injectable (a commit-reachability module); the default is the
    real one, loaded by the predicate's own loader."""
    import _delivery_gate as dg  # type: ignore  # the ONE reachability predicate ()
    real = prober if prober is not None else dg._load_prober(str(SCRIPT_DIR))
    if real is None:
        return lambda goal: (dg.UNKNOWN, "commit-reachability prober unavailable")
    shim = _NoFetchProber(real)
    return lambda goal: dg.blocker_delivery_state(
        goal, repo=str(repo), target_ref="origin/main", prober=shim)


def setup_delivery(enabled: bool = True, repo=REPO_ROOT) -> tuple[Any, dict]:
    """(probe or None, what `list` says about it). The probe is off when asked off and when
    the refs could not be refreshed. Both say so, and neither defers anything."""
    if not enabled:
        return None, {"probe": "off", "reason": "--no-delivery"}
    ok, detail = refresh_delivery_refs(repo)
    if not ok:
        return None, {"probe": "skipped",
                      "reason": f"refresh of origin and the worker refs failed: {detail}"}
    return delivery_probe(repo), {"probe": "on", "reason": ""}


# ─── inputs ───────────────────────────────────────────────────────────────────

def _gate_section() -> dict:
    """The `close_review_gate` section of core/config/aspirations.yaml, or {} (loudly)."""
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load((SCRIPT_DIR.parent / "config" / "aspirations.yaml")
                             .read_text(encoding="utf-8")) or {}
        section = cfg.get("close_review_gate") or {}
        if not isinstance(section, dict):
            raise TypeError(f"close_review_gate is a {type(section).__name__}, not a mapping")
        return section
    except Exception as exc:  # loud, never a silent empty (guard-2298)
        print(f"close-review-queue: config read failed ({type(exc).__name__}: {exc}); "
              f"no roles from config", file=sys.stderr)
        return {}


def _roles(section: dict, key: str) -> list[str]:
    return [str(r).strip().lower() for r in (section.get(key) or []) if str(r).strip()]


def roles_from_config() -> list[str]:
    """`close_review_gate.review_closer_roles`: the roles whose every closure is reviewed."""
    return _roles(_gate_section(), CONFIG_KEY)


def sample_plan_from_config() -> tuple[list[str], float]:
    """`review_sampled_roles` and `review_sample_rate`: the relaxed roles, and the share of
    their closures that stays reviewed. A rate outside (0, 1] reads as the default, loudly."""
    section = _gate_section()
    raw = section.get(SAMPLE_RATE_KEY, DEFAULT_SAMPLE_RATE)
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        rate = -1.0
    if not 0.0 < rate <= 1.0:
        print(f"close-review-queue: {SAMPLE_RATE_KEY}={raw!r} is not in (0, 1]; using "
              f"{DEFAULT_SAMPLE_RATE} (a zero rate is the drop-to-none it replaces)",
              file=sys.stderr)
        rate = DEFAULT_SAMPLE_RATE
    return _roles(section, SAMPLED_KEY), rate


def in_sample(goal_id: str, rate: float) -> bool:
    """Whether a relaxed role's closure is in the reviewed sample. A hash of the goal id
    under SAMPLE_SEED, so the answer is the same on every run and every box."""
    digest = hashlib.sha256(f"{SAMPLE_SEED}:{goal_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") < rate * 2 ** 64


def load_closures(role: str) -> list[dict]:
    """Every goal record carrying completed_by_role == role, through the query API
    (never a direct JSONL read). Loud on failure: an unreadable store and a role with no
    closures are the same empty list downstream, and only one of them is news."""
    script = SCRIPT_DIR / "aspirations-query.sh"
    try:
        from _runtime_bash import bash_cmd  # type: ignore
        res = subprocess.run(
            bash_cmd(script, "--goal-field", "completed_by_role", role, "--full"),
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0 or not res.stdout.strip():
            print(f"close-review-queue: store read failed rc={res.returncode} "
                  f"{res.stderr.strip()[:200]}", file=sys.stderr)
            return []
        rows = json.loads(res.stdout)
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except Exception as exc:
        print(f"close-review-queue: store read error {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []


def load_requests() -> tuple[list[dict], str | None]:
    """Every goal carrying `review_requested` in a status that still wants the review
    (REQUEST_STATUSES), through the query API, and the error text if the read failed. The
    API has no has-field filter, so this reads those statuses in full and keeps the
    requested goals (measured 2026-10-02: one call, under 3 s, 4,339 goals). Loud, like
    load_closures, and the error also reaches the listing, because an unreadable store and
    a world with no open request must not print the same."""
    script = SCRIPT_DIR / "aspirations-query.sh"
    try:
        from _runtime_bash import bash_cmd  # type: ignore
        res = subprocess.run(
            bash_cmd(script, "--goal-status", ",".join(REQUEST_STATUSES), "--full"),
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0 or not res.stdout.strip():
            err = f"store read failed rc={res.returncode} {res.stderr.strip()[:200]}"
            print(f"close-review-queue: requests {err}", file=sys.stderr)
            return [], err
        rows = json.loads(res.stdout)
        if not isinstance(rows, list):
            raise TypeError(f"the query returned a {type(rows).__name__}, not a list")
        return [r for r in rows if isinstance(r, dict) and r.get("review_requested")], None
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"close-review-queue: requests store read error {err}", file=sys.stderr)
        return [], err


def resolve_unattributed(goal_ids: list[str],
                         world: str | None = None) -> tuple[dict[str, dict], str | None]:
    """goal_id -> goal-resolve.py's answer (disposition live | archived | evicted | unknown,
    plus the closer fields of a found record) for the verdicts the live closures did not
    cover, and the error text if the lookup could not run. Loud, like load_closures: a
    lookup that failed and a goal that is nowhere must not read the same."""
    if not goal_ids:
        return {}, None
    try:
        world = world or str(_gate().WORLD_DIR or "")
        if not world:
            raise RuntimeError("no world directory")
        mod = _resolver()
        return {gid: mod.resolve(gid, world) for gid in goal_ids}, None
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"close-review-queue: goal resolution failed ({err}); {len(goal_ids)} "
              f"verdict(s) left unresolved", file=sys.stderr)
        return {}, err


def load_out_of_live(roles: list[str], world: str | None = None
                     ) -> tuple[list[dict], dict[str, set[str]], str | None]:
    """What load_closures cannot see (): the closures of `roles` still listed in an
    archived aspiration, and per role the evicted ids the census kept, over the live store
    and the archive. -> (archived closures, {role: evicted ids}, error text or None). Read
    through goal-resolve.py's reader, the one this script already uses for both stores.
    Loud, like load_closures: a read that failed must not look like a lane with no misses."""
    if not roles:
        return [], {}, None
    try:
        world = world or str(_gate().WORLD_DIR or "")
        if not world:
            raise RuntimeError("no world directory")
        mod = _resolver()
        # The eager pull never re-pulls an archive (goal-resolve.py, ).
        from _fresh_read import refresh_for_read  # type: ignore
        refresh_for_read(Path(world, "aspirations-archive.jsonl"), label="close-review-queue")
        archived: list[dict] = []
        evicted: dict[str, set[str]] = {r: set() for r in roles}
        for fname in ("aspirations.jsonl", "aspirations-archive.jsonl"):
            for asp in mod._iter_aspirations(world, fname):
                if not isinstance(asp, dict):
                    continue
                for role, ids in census_closer_role_ids(asp).items():
                    if role in evicted:
                        evicted[role].update(ids)
                if fname == "aspirations-archive.jsonl":
                    archived.extend(g for g in asp.get("goals") or []
                                    if isinstance(g, dict) and _closer_role(g) in evicted)
        return archived, evicted, None
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"close-review-queue: archive and census read failed ({err}); coverage not "
              f"computed", file=sys.stderr)
        return [], {}, err


def completed_at(goal: dict) -> str:
    """The closure stamp: `completed_at` (full timestamp) or `completed_date` (a day)."""
    return str(goal.get("completed_at") or goal.get("completed_date") or "")


def goal_id_of(goal: dict) -> str:
    return str(goal.get("id") or goal.get("goal_id") or "")


# ─── list ─────────────────────────────────────────────────────────────────────

def closure_stamp(goal: dict) -> str:
    """`completed_at`, or a bare `completed_date` read as that day's midnight so a
    day-only stamp compares (and ranks) as the OLDEST moment of its day."""
    stamp = completed_at(goal)
    return stamp + "T00:00:00" if len(stamp) == 10 else stamp


def _is_recurring(goal: dict) -> bool:
    return goal.get("recurring") is True or str(goal.get("recurring") or "").lower() == "true"


def _priority_rank(goal: dict) -> int:
    return {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(str(goal.get("priority") or "").upper(), 3)


def rank_key(goal: dict, tier: int) -> tuple:
    """Tier 2 first, then HIGH priority, then the newest closure."""
    stamp = closure_stamp(goal)
    return (0 if tier == 2 else 1, _priority_rank(goal), "~" if not stamp else
            "".join(chr(0x10FFFF - ord(c)) for c in stamp))


def request_rank_key(goal: dict, tier: int) -> tuple:
    """An open goal first (its close waits on the verdict), then tier 2, then HIGH
    priority, then the oldest request (it has waited longest)."""
    is_open = str(goal.get("status") or "").lower() != "completed"
    return (0 if is_open else 1, 0 if tier == 2 else 1, _priority_rank(goal),
            str(goal.get("review_requested")))


def executor_of(goal: dict) -> str:
    """The mind whose work a review request asks about: who closed the goal, else who
    executed it, else who claimed it. An agent name (independence is the name, never the
    session), or "" when the record names none."""
    for key in ("completed_by", "executed_by", "claimed_by"):
        name = str(goal.get(key) or "").strip()
        if name:
            return name
    return ""


def _row(goal: dict, tier: dict) -> dict:
    """What a listed goal carries: what the reviewer reads and hands to the producer."""
    return {
        "goal_id": goal_id_of(goal),
        "asp_id": goal.get("asp_id") or goal.get("aspiration_id"),
        "title": str(goal.get("title") or "")[:160],
        "priority": goal.get("priority"),
        "tier": tier["tier"],
        "tier_reasons": tier["reasons"],
        "completed_by": goal.get("completed_by"),
        "completed_by_role": goal.get("completed_by_role"),
        "completed_by_sid": goal.get("completed_by_sid"),
        "completed_at": completed_at(goal),
        "commit_sha": goal.get("commit_sha"),
    }


def select_requests(goals: list[dict], *, reviewed: set[str], reviewer: str) -> dict:
    """Pure: the review requests a reviewer may answer, ranked, and the ones it may not.

    A request is a goal carrying `review_requested`. It is listed while no verdict answers it
    (`reviewed`, which answered_ids builds: a verdict answers only a request made at or before
    it) and its status still wants one (REQUEST_STATUSES; any other status is moot), with no
    age window. Its closer is executor_of(goal). A request whose closer is the reviewer is
    partitioned out and counted, as a same-mind closure is, and one that names no closer is
    declined and counted, never listed: independence that cannot be established is
    unproven (coordination.md)."""
    tier_mod = _tier()
    eligible: list[tuple[tuple, dict, dict, str]] = []
    same_mind: list[str] = []
    no_closer: list[str] = []
    skipped = {"moot": 0, "reviewed": 0}
    for g in goals:
        gid = goal_id_of(g)
        if not gid or not g.get("review_requested"):
            continue
        if str(g.get("status") or "").lower() not in REQUEST_STATUSES:
            skipped["moot"] += 1
            continue
        if gid in reviewed:
            skipped["reviewed"] += 1
            continue
        closer = executor_of(g)
        if not closer:
            no_closer.append(gid)
            continue
        if closer.lower() == reviewer.strip().lower():
            same_mind.append(gid)
            continue
        tier = tier_mod.classify(g)
        eligible.append((request_rank_key(g, tier["tier"]), g, tier, closer))
    eligible.sort(key=lambda t: t[0])
    rows = [dict(_row(g, tier), kind="request", closer=closer, status=g.get("status"),
                 review_requested=g.get("review_requested"))
            for _, g, tier, closer in eligible]
    return {"rows": rows, "eligible_total": len(eligible), "same_mind": same_mind,
            "no_closer": no_closer, "skipped": skipped}


def _delivery_of(goal: dict, delivery) -> tuple[str, str]:
    """(state, detail) for one closure. Only a worker closure carrying a commit_sha is asked,
    which is the population that can wait on a carrier ref (iteration-close stamps the sha
    on a worker close alone); any other closure reads "unasked". A probe that raises reads
    "unknown", and "unknown" lists the closure like every state but "pending"."""
    if (delivery is None
            or str(goal.get("completed_by_role") or "").strip().lower() != WORKER_ROLE
            or not str(goal.get("commit_sha") or "").strip()):
        return "unasked", ""
    try:
        state, detail = delivery(goal)
    except Exception as exc:  # one bad record must not stop the listing for every reviewer
        return "unknown", f"delivery probe raised: {type(exc).__name__}: {exc}"
    return str(state), str(detail)


def select_candidates(goals: list[dict], *, reviewed: set[str], now: datetime,
                      since_hours: float, reviewer: str, cap: int,
                      sampled_roles: frozenset[str] | set[str] = frozenset(),
                      sample_rate: float = DEFAULT_SAMPLE_RATE,
                      delivery=None, strand_hours: float = STRAND_HOURS) -> dict:
    """Pure: which closures a reviewer should look at next, and which it may not.

    Filters: status must be `completed` (a recurring goal rests at `pending` with its
    completed_by_role still stamped — those are tier 0 and never reviewed here), closed
    within `since_hours` (the measurement starts when the lane does; history is not
    re-litigated), without a verdict artifact, and, for a relaxed role in
    `sampled_roles`, inside the sample (the rest are counted as `not_sampled`). Same-mind
    closures (completed_by == reviewer) are partitioned out, never ranked, and counted so
    the caller can see the coverage this reviewer cannot provide.

    `delivery` (goal -> (state, detail), see delivery_probe) is asked about every worker
    closure that carries a commit_sha, AFTER the ranking and BEFORE the cap, so a deferred
    row never takes a slot from one that can be reviewed. Only the state "pending" defers
    it, into `waiting_to_land`. Once it has waited `strand_hours` it is `stranded` instead,
    and a stranded row is OFFERED, first (longest wait first) and inside the cap, carrying
    `stranded: true`. With no `delivery`, nothing is deferred."""
    cutoff = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    tier_mod = _tier()
    eligible: list[tuple[tuple, dict, dict]] = []
    same_mind: list[str] = []
    skipped = {"not_completed": 0, "recurring": 0, "too_old": 0, "reviewed": 0,
               "not_sampled": 0}
    for g in goals:
        gid = goal_id_of(g)
        if not gid:
            continue
        if str(g.get("status") or "").lower() != "completed":
            skipped["not_completed"] += 1
            continue
        if _is_recurring(g):
            skipped["recurring"] += 1
            continue
        if closure_stamp(g) < cutoff:
            skipped["too_old"] += 1
            continue
        if gid in reviewed:
            skipped["reviewed"] += 1
            continue
        role = str(g.get("completed_by_role") or "").strip().lower()
        if role in sampled_roles and not in_sample(gid, sample_rate):
            skipped["not_sampled"] += 1
            continue
        if str(g.get("completed_by") or "").strip().lower() == reviewer.strip().lower():
            same_mind.append(gid)
            continue
        tier = tier_mod.classify(g)
        eligible.append((rank_key(g, tier["tier"]), g, tier))
    eligible.sort(key=lambda t: t[0])
    listed: list[tuple[dict, dict]] = []
    waiting: list[dict] = []
    stranded: list[dict] = []
    for _, g, tier in eligible:
        state, detail = _delivery_of(g, delivery)
        if state != "pending":
            listed.append((g, tier))
            continue
        stamp = _instant(closure_stamp(g))
        hours = round((now - stamp).total_seconds() / 3600.0, 1) if stamp else None
        row = dict(_row(g, tier), kind="closure", closer=g.get("completed_by"),
                   delivery=detail, waiting_hours=hours)
        if hours is not None and hours >= strand_hours:
            stranded.append(dict(row, stranded=True))
        else:
            waiting.append(row)
    for bucket in (waiting, stranded):  # the one that has waited longest first
        bucket.sort(key=lambda r: -(r["waiting_hours"] or 0.0))
    # A stranded closure is offered FIRST, inside the cap: it is the lane's finding, and
    # behind the ranked rows it could wait out its window unreviewed.
    rows = (stranded + [dict(_row(g, tier), kind="closure", closer=g.get("completed_by"))
                        for g, tier in listed])[:cap]
    return {
        "candidates": rows,
        "eligible_total": len(eligible),
        "cap": cap,
        "same_mind": same_mind,
        "skipped": skipped,
        "waiting_to_land": waiting,
        "stranded": stranded,
        "strand_hours": strand_hours,
    }


def reviewed_ids(goals: list[dict]) -> set[str]:
    gate = _gate()
    out: set[str] = set()
    for g in goals:
        gid = goal_id_of(g)
        if gid and gate.read_verdict(gate.verdict_path(gid)) is not None:
            out.add(gid)
    return out


def _instant(stamp: Any) -> datetime | None:
    """A stamp as a naive UTC datetime, or None when it is not an ISO timestamp. The fleet
    writes naive UTC (measured 2026-10-02: all 44 review_requested stamps and all 178 verdict
    reviewed_at stamps, to the second). A stamp with a zone is converted to UTC, because
    comparing an aware time with a naive one raises. Any field value reads as a time or as
    None, never as a crash: one bad record must not stop the listing for every reviewer."""
    try:
        when = datetime.fromisoformat(stamp)
        if when.tzinfo is not None:
            when = when.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):  # not a string, not ISO, past year 1..9999
        return None
    return when


def answers(verdict: Any, requested: Any) -> bool:
    """Whether a goal's current verdict answers its review request (): the verdict
    was written at or after `review_requested`. A closer sets the field again when it asks
    again, so a verdict from before the ask, such as the REJECT that sent the work back, does
    not answer it.

    Two stamps can be unreadable, and each fails toward the request being answered once:
      * the request's: any verdict answers it, the rule before g-375-119. No verdict could
        ever be shown to come after it, so as unanswered it would be listed forever.
      * the verdict's (or an entry that is not a record): it answers nothing. The request is
        listed once more, and the reviewer's next verdict carries a stamp, because
        close-review-verdict.py writes one on every entry."""
    if not isinstance(verdict, dict):
        return False
    asked, done = _instant(requested), _instant(verdict.get("reviewed_at"))
    return asked is None or (done is not None and done >= asked)


def answered_ids(requests: list[dict]) -> set[str]:
    """The requests a verdict answers (`answers`), compared against the CURRENT verdict, the
    gate's own reading: the trail is append-only, so its last entry is its newest."""
    gate = _gate()
    out: set[str] = set()
    for g in requests:
        gid = goal_id_of(g)
        if gid and answers(gate.read_verdict(gate.verdict_path(gid)),
                           g.get("review_requested")):
            out.add(gid)
    return out


# ─── stats ────────────────────────────────────────────────────────────────────

def artifacts_dir() -> Path | None:
    p = _gate().verdict_path("_probe_")
    return p.parent if p is not None else None


def read_all_verdicts(directory: Path | None) -> dict[str, dict]:
    """goal_id -> CURRENT verdict (last entry wins, the gate's own reading)."""
    gate = _gate()
    out: dict[str, dict] = {}
    if directory is None or not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.json")):
        v = gate.read_verdict(p)
        if isinstance(v, dict):
            gid = str(v.get("goal_id") or p.stem)
            out[gid] = v
    return out


def relax_rule(n: int, approve_rate: float, recent_verdicts: list[str]) -> dict:
    """The written-down rule for dropping a role from the reviewed class."""
    recent_reject = any(v == "REJECT" for v in recent_verdicts[-RELAX_RECENT:])
    ok = n >= RELAX_MIN_REVIEWED and approve_rate >= RELAX_MIN_APPROVE_RATE and not recent_reject
    return {
        "relax_ok": ok,
        "rule": (f">= {RELAX_MIN_REVIEWED} reviewed, approve rate >= {RELAX_MIN_APPROVE_RATE}, "
                 f"no REJECT in the last {RELAX_RECENT}"),
        "blocking_reasons": [r for r, hit in (
            (f"only {n} reviewed (< {RELAX_MIN_REVIEWED})", n < RELAX_MIN_REVIEWED),
            (f"approve rate {approve_rate:.2f} < {RELAX_MIN_APPROVE_RATE}",
             approve_rate < RELAX_MIN_APPROVE_RATE),
            (f"a REJECT among the last {RELAX_RECENT}", recent_reject),
        ) if hit],
    }


def _closer_role(record: dict | None) -> str:
    return str((record or {}).get("completed_by_role") or "").strip().lower()


def role_stats(verdicts: dict[str, dict], goals_by_id: dict[str, dict],
               roles: list[str], sampled: frozenset[str] | set[str] = frozenset(),
               sample_rate: float = DEFAULT_SAMPLE_RATE,
               resolved: dict[str, dict] | None = None) -> dict:
    """Pure: verdict counts and the relax verdict per closer role.

    The closer comes from the verdict when it names one (written since g-375-55), else
    from the live closure in `goals_by_id`, else from `resolved`, goal-resolve.py's
    answer for the rest (a live or archived record). A verdict attributed to no listed
    role lands in exactly one `unattributed` bucket, never in a role's rate: its closer's
    role is not reviewed, its goal was evicted to an id-only census so no role survives,
    no store holds its goal, or it was never resolved (`resolved` has no answer for it).
    Each role carries its review class: `full`, or `sampled` for a relaxed role in
    `sampled`, whose pass rate is measured over its sample."""
    gate = _gate()
    resolved = resolved or {}
    per: dict[str, list[tuple[str, str, str]]] = {r: [] for r in roles}
    # A role is not a model: a `worker` closure may come from a Claude Body or a local
    # Qwen one, and the goal record carries no model field. The closing SESSION is the
    # finest key it does carry, so the per-sid split lets a reader separate the
    # uncalibrated Bodies (their SIDs are known to their operator) from the rest.
    by_sid: dict[str, dict[str, int]] = {}
    via = {"verdict": 0, "live": 0, "archive": 0}
    unattributed: dict[str, list[str]] = {"role_not_reviewed": [], "evicted_role_unknown": [],
                                          "not_found_anywhere": [], "not_resolved": []}
    for gid, v in verdicts.items():
        if _closer_role(v):
            record, source = v, "verdict"
        elif gid in goals_by_id:
            record, source = goals_by_id[gid], "live"
        elif gid not in resolved:
            unattributed["not_resolved"].append(gid)
            continue
        elif resolved[gid].get("disposition") in ("live", "archived"):
            record = resolved[gid]
            source = "archive" if record["disposition"] == "archived" else "live"
        elif resolved[gid].get("disposition") == "evicted":
            unattributed["evicted_role_unknown"].append(gid)
            continue
        else:
            unattributed["not_found_anywhere"].append(gid)
            continue
        role = _closer_role(record)
        if role not in per:
            unattributed["role_not_reviewed"].append(gid)
            continue
        via[source] += 1
        verdict = str(v.get("verdict") or "")
        per[role].append((str(v.get("reviewed_at") or ""), verdict, gid))
        sid8 = str(record.get("completed_by_sid") or "?")[:8]
        row = by_sid.setdefault(f"{role}/{sid8}", {"reviewed": 0, "approved": 0, "rejected": 0})
        row["reviewed"] += 1
        row["approved"] += 1 if gate.releases_close(verdict) else 0
        row["rejected"] += 1 if verdict == "REJECT" else 0
    out: dict[str, Any] = {"roles": {}, "by_sid": by_sid, "artifacts_total": len(verdicts),
                           "attributed_via": via,
                           "unattributed": {k: sorted(x) for k, x in unattributed.items()}}
    for role, items in per.items():
        items.sort()  # reviewed_at ascending; an absent stamp sorts first (oldest)
        seq = [v for _, v, _ in items]
        n = len(seq)
        approved = sum(1 for v in seq if gate.releases_close(v))
        rejected = sum(1 for v in seq if v == "REJECT")
        rate = (approved / n) if n else 0.0
        out["roles"][role] = {
            "review": "sampled" if role in sampled else "full",
            "sample_rate": sample_rate if role in sampled else 1.0,
            "reviewed": n,
            "approved": approved,
            "rejected": rejected,
            "other": n - approved - rejected,
            "approve_rate": round(rate, 3),
            "recent": seq[-RELAX_RECENT:],
            **relax_rule(n, rate, seq),
            # The lane's start for this role (its first verdict) and the closures it
            # reviewed: coverage_report's inputs.
            "lane_start": next((at for at, _, _ in items if at), None),
            "reviewed_ids": sorted(gid for _, _, gid in items),
        }
    return out


def coverage_report(role: str, *, live: list[dict], archived: list[dict],
                    evicted_ids: set[str], reviewed: set[str], verdict_ids: set[str],
                    lane_start: str | None, now: datetime, since_hours: float,
                    sampled: bool = False, sample_rate: float = DEFAULT_SAMPLE_RATE) -> dict:
    """Pure: the reviewed share of `role`'s closures inside the lane's lifetime, and where
    each unreviewed one went (g-375-38).

    `reviewed` holds the ids role_stats attributed to the role. Every other completed,
    non-recurring closure of the role without a verdict is unreviewed: `in_window` while
    `list` can still offer it, `aged_out` once past the window, `archived` once its
    aspiration left the live query, `evicted` once only the census holds its id. A closure
    from before the lane's reach (`since_hours` before `lane_start`) predates the lane and
    stays out of the coverage. An undated closure and an evicted id (the census keeps no
    time) never predate it, because over-counting a miss is the safe error. For a relaxed
    role only its sample is due a review, so the rest are neither misses nor population."""
    fmt = "%Y-%m-%dT%H:%M:%S"
    cutoff = (now - timedelta(hours=since_hours)).strftime(fmt)
    reach = None
    if lane_start:
        try:
            reach = (datetime.strptime(lane_start[:19], fmt)
                     - timedelta(hours=since_hours)).strftime(fmt)
        except ValueError:
            reach = None  # an unreadable start bounds nothing: no closure predates it

    def due(gid: str) -> bool:
        return gid not in verdict_ids and (not sampled or in_sample(gid, sample_rate))

    unreviewed: dict[str, list[str]] = {"in_window": [], "aged_out": [], "archived": [],
                                        "evicted": []}
    predates: list[str] = []
    seen: set[str] = set()
    for where, goals in (("live", live), ("archived", archived)):
        for g in goals:
            gid = goal_id_of(g)
            if (not gid or gid in seen or _closer_role(g) != role or _is_recurring(g)
                    or str(g.get("status") or "").lower() != "completed" or not due(gid)):
                continue
            seen.add(gid)
            stamp = closure_stamp(g)
            if reach and stamp and stamp < reach:
                predates.append(gid)
            elif where == "archived":
                unreviewed["archived"].append(gid)
            else:
                unreviewed["in_window" if stamp >= cutoff else "aged_out"].append(gid)
    unreviewed["evicted"] = [gid for gid in evicted_ids if gid not in seen and due(gid)]
    population = len(reviewed) + sum(len(ids) for ids in unreviewed.values())
    return {
        "lane_start": lane_start,
        "reach_start": reach,
        "covered": len(reviewed),
        "population": population,
        "coverage": round(len(reviewed) / population, 3) if population else None,
        "unreviewed": {k: sorted(ids) for k, ids in unreviewed.items()},
        "predates_lane": sorted(predates),
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _print_list(result: dict, roles: list[str], reviewer: str, since_hours: float) -> None:
    print(f"close-review-queue list: roles={roles} "
          f"sampled={result['sampled_roles']}@{result['sample_rate']} reviewer={reviewer} "
          f"since={since_hours}h eligible={result['eligible_total']} cap={result['cap']} "
          f"same_mind_left_for_another_agent={len(result['same_mind'])} "
          f"skipped={result['skipped']} "
          f"waiting_to_land={len(result.get('waiting_to_land') or [])} "
          f"stranded={len(result.get('stranded') or [])}")
    req = result.get("requests") or {}
    print(f"  review requests: eligible={req.get('eligible_total', 0)} "
          f"same_mind_left_for_another_agent={len(req.get('same_mind') or [])} "
          f"declined_no_closer={len(req.get('no_closer') or [])} skipped={req.get('skipped')}"
          + (f" READ FAILED ({req['read_error']})" if req.get("read_error") else ""))
    for r in result["candidates"]:
        if r.get("kind") == "request":
            state = "closed" if str(r.get("status") or "").lower() == "completed" else "open"
            print(f"  {r['goal_id']} [{r['asp_id']}] REQUEST {state} tier={r['tier']} "
                  f"{r['priority']} closer={r['closer']} requested={r['review_requested']} "
                  f"— {r['title'][:90]}")
        else:
            print(f"  {r['goal_id']} [{r['asp_id']}] tier={r['tier']} {r['priority']} "
                  f"by={r['completed_by']}/{str(r['completed_by_sid'] or '')[:8]} "
                  f"at={r['completed_at']} — {r['title'][:90]}")
        if r.get("stranded"):
            print(f"      · STRANDED: waited {r['waiting_hours']}h to land — {r['delivery']}")
        for reason in r["tier_reasons"]:
            print(f"      · {reason}")
    if not result["candidates"]:
        print("  (no review request or unreviewed closure this reviewer may review)")
    delivery = result.get("delivery") or {}
    if delivery.get("probe") in ("off", "skipped"):
        print(f"  delivery probe {delivery['probe']} ({delivery.get('reason')}): "
              f"no closure was deferred this run")
    offered = {r["goal_id"] for r in result["candidates"]}
    unseen = [r["goal_id"] for r in result.get("stranded") or [] if r["goal_id"] not in offered]
    if unseen:
        print(f"  STRANDED beyond the cap: {len(unseen)} more ({', '.join(unseen)}) — "
              f"waited {result.get('strand_hours')}h or more; raise --cap to offer them")
    waiting = result.get("waiting_to_land") or []
    if waiting:
        print(f"  WAITING TO LAND (under {result.get('strand_hours')}h): {len(waiting)} — the "
              f"closer's HEAD at close is not on origin/main yet, so these are not offered "
              f"until it is")
        for r in waiting:
            age = "unknown" if r["waiting_hours"] is None else f"{r['waiting_hours']}h"
            print(f"  {r['goal_id']} [{r['asp_id']}] tier={r['tier']} {r['priority']} "
                  f"by={r['completed_by']}/{str(r['completed_by_sid'] or '')[:8]} "
                  f"waiting={age} — {r['title'][:90]}")
            print(f"      · {r['delivery']}")


def _print_stats(stats: dict) -> None:
    for role, s in stats["roles"].items():
        review = "full" if s["review"] == "full" else f"sampled@{s['sample_rate']}"
        cov = s.get("coverage")
        print(f"role={role} review={review}: reviewed={s['reviewed']} "
              f"approved={s['approved']} rejected={s['rejected']} other={s['other']} "
              f"approve_rate={s['approve_rate']} coverage="
              + (f"{cov['covered']}/{cov['population']}={cov['coverage']}" if cov else "unknown")
              + f" recent={s['recent']} relax_ok={s['relax_ok']}")
        if cov:
            u = cov["unreviewed"]
            print(f"    unreviewed in the lane (first verdict {cov['lane_start'] or 'none yet'}, "
                  f"reach from {cov['reach_start'] or 'the first closure'}): "
                  f"in_window={len(u['in_window'])} aged_out={len(u['aged_out'])} "
                  f"archived={len(u['archived'])} evicted={len(u['evicted'])}; "
                  f"predates the lane={len(cov['predates_lane'])}")
            missed = sorted(u["aged_out"] + u["archived"] + u["evicted"])
            if missed:
                more = f" +{len(missed) - 12} more" if len(missed) > 12 else ""
                print(f"      out of list's reach, never reviewed: {' '.join(missed[:12])}{more}")
        if s["review"] == "full" and s["relax_ok"]:
            print(f"    · relaxable: move it to {SAMPLED_KEY}, where a sample stays reviewed; "
                  f"never drop it")
        for reason in s["blocking_reasons"]:
            where = "not relaxable" if s["review"] == "full" else "relax rule no longer met"
            print(f"    · {where}: {reason}")
        print(f"    rule: {s['rule']}")
    via = stats["attributed_via"]
    print(f"artifacts={stats['artifacts_total']}: attributed to a listed role "
          f"{sum(via.values())} (closer named by the verdict {via['verdict']}, "
          f"live record {via['live']}, archived record {via['archive']})")
    for key, label in (("role_not_reviewed", "closer's role not reviewed"),
                       ("evicted_role_unknown", "goal evicted to an id-only census, role unknown"),
                       ("not_found_anywhere", "goal record not found anywhere"),
                       ("not_resolved", "not resolved")):
        ids = stats["unattributed"][key]
        more = f" +{len(ids) - 12} more" if len(ids) > 12 else ""
        print(f"    {label}: {len(ids)}" + (f" [{' '.join(ids[:12])}{more}]" if ids else ""))
    if stats.get("resolution_error"):
        print(f"    goal resolution FAILED ({stats['resolution_error']}): the not-resolved "
              f"verdicts may belong to a listed role")
    if stats.get("coverage_error"):
        print(f"    coverage NOT computed: the archive and census read failed "
              f"({stats['coverage_error']})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="open review requests, then unreviewed closures, this "
                                     "reviewer may review, ranked")
    ls.add_argument("--roles", nargs="*", default=None,
                    help=f"closer roles; default: close_review_gate.{CONFIG_KEY} from config")
    ls.add_argument("--since-hours", type=float, default=72.0)
    ls.add_argument("--cap", type=int, default=3)
    ls.add_argument("--reviewer", default=None, help="defaults to $MIND_AGENT")
    ls.add_argument("--strand-hours", type=float, default=STRAND_HOURS,
                    help="hours a worker closure may wait to land before it is offered "
                         f"first, marked STRANDED (default {STRAND_HOURS:g}, measured)")
    ls.add_argument("--no-delivery", action="store_true",
                    help="skip the delivery probe: no fetch, nothing deferred")
    ls.add_argument("--json", action="store_true")
    st = sub.add_parser("stats", help="pass rate and coverage per closer role + the relax-rule "
                                      "verdict")
    st.add_argument("--roles", nargs="*", default=None)
    st.add_argument("--since-hours", type=float, default=72.0,
                    help="the window `list` uses; past it a closure has aged out")
    st.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    roles = [r.lower() for r in (args.roles or [])] or roles_from_config()
    sampled_cfg, sample_rate = sample_plan_from_config()
    # A role on both lists is reviewed in full: sampling only ever lowers coverage, so a
    # contradiction between the two lists resolves to the safer reading.
    sampled = [r for r in sampled_cfg if r not in roles]
    if not roles and not sampled:
        print("close-review-queue: no closer roles (config lists empty and no --roles)",
              file=sys.stderr)
        return 1
    goals: list[dict] = []
    for role in roles + sampled:
        goals.extend(load_closures(role))
    goals_by_id = {goal_id_of(g): g for g in goals if goal_id_of(g)}

    if args.cmd == "list":
        reviewer = (args.reviewer or os.environ.get("MIND_AGENT") or "").strip()
        if not reviewer:
            print("close-review-queue: reviewer unknown (--reviewer or $MIND_AGENT)",
                  file=sys.stderr)
            return 1
        delivery, delivery_meta = setup_delivery(enabled=not args.no_delivery)
        result = select_candidates(goals, reviewed=reviewed_ids(goals), now=datetime.now(),
                                   since_hours=args.since_hours, reviewer=reviewer,
                                   cap=args.cap, sampled_roles=set(sampled),
                                   sample_rate=sample_rate, delivery=delivery,
                                   strand_hours=args.strand_hours)
        result["delivery"] = delivery_meta
        # Review requests go first and share the cap (): an open one holds its close
        # until a verdict exists, and every one was asked for. A goal that is both a request
        # and a closure is offered once, as the request.
        requests, requests_error = load_requests()
        req = select_requests(requests, reviewed=answered_ids(requests), reviewer=reviewer)
        asked = {r["goal_id"] for r in req["rows"]}
        result["candidates"] = (req["rows"] + [r for r in result["candidates"]
                                               if r["goal_id"] not in asked])[:args.cap]
        result["requests"] = {"eligible_total": req["eligible_total"],
                              "same_mind": req["same_mind"], "no_closer": req["no_closer"],
                              "skipped": req["skipped"], "read_error": requests_error}
        result.update({"roles": roles, "sampled_roles": sampled, "sample_rate": sample_rate,
                       "reviewer": reviewer, "since_hours": args.since_hours})
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            _print_list(result, roles, reviewer, args.since_hours)
        return 0

    verdicts = read_all_verdicts(artifacts_dir())
    # The live closures hold only goals still in the live store. Resolve every other
    # verdict's goal through all stores before attributing it ().
    rest = [gid for gid, v in verdicts.items() if not _closer_role(v) and gid not in goals_by_id]
    resolved, resolution_error = resolve_unattributed(rest)
    stats = role_stats(verdicts, goals_by_id, roles + sampled, sampled=set(sampled),
                       sample_rate=sample_rate, resolved=resolved)
    if resolution_error:
        stats["resolution_error"] = resolution_error
    # The closures nobody reviewed: the live ones above, plus what the live query cannot
    # see ().
    archived, evicted, coverage_error = load_out_of_live(roles + sampled)
    if coverage_error:
        stats["coverage_error"] = coverage_error
    else:
        for role, s in stats["roles"].items():
            s["coverage"] = coverage_report(
                role, live=goals, archived=archived, evicted_ids=evicted.get(role, set()),
                reviewed=set(s["reviewed_ids"]), verdict_ids=set(verdicts),
                lane_start=s["lane_start"], now=datetime.now(), since_hours=args.since_hours,
                sampled=role in sampled, sample_rate=sample_rate)
    stats["artifacts_dir"] = str(artifacts_dir())
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        _print_stats(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
