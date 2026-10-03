#!/usr/bin/env python3
"""groom.py — the candidate-tier grooming engine (, B4).

Implements world/conventions/goal-intake-management.md §5-6 (BINDING) and the §9
kill switch. Four subcommands, each fronted by a thin wrapper:

  bite     groom-bite.sh     the <= groom_bite oldest candidates this agent owns
  verdict  groom-verdict.sh  ONE ledgered verdict on ONE candidate
  ledger   groom-ledger.sh   read / sample the §5 verdict ledger (oversight, I4)
  drain    drain-candidates-to-pending.sh
                             the §9 kill switch: every candidate -> pending

I4 — the LLM chooses verdicts; this script enforces every bound:
  * the promote cap is counted HERE, from the §5 ledger, never taken from the
    caller;
  * a verdict lands only on a goal that is still a live `candidate` at the
    store of record, so a stale bite never writes;
  * a merge survivor, an rb-route id and a re-home target are validated live
    BEFORE anything is written.

ONE LEDGER. gates/candidate_transition.py (B1c) owns the §5 ledger: the
update-goal write path appends a row for every COMMITTED candidate transition
(promote / merge / close-moot). Status therefore goes through update-goal, and
this engine appends only `keep` rows itself — keep is not a transition, so
nothing else would record it — through that module's own append_ledger. An
rb-route row carries the write path's documented default label, close-moot,
with the rb-id in its outcome_note evidence (candidate_transition.py).

THE FIRING WINDOW IS DERIVED, NEVER SUPPLIED. A caller-supplied firing id
would let a caller mint a fresh promote budget — the honor system §5 forbids
("enforced by the promote script, not honor"). A firing is the trailing
touch_stale_hours span: §5 already treats a touch as one firing's for that
long, and the 24h grooming interval is longer, so each firing gets exactly one
budget and no argument can reset it. Count-then-write runs under a per-agent
lock, so two Bodies of one agent cannot both spend the last slot.

THE DRAIN (§9) IS NOT A VERDICT AND CARRIES NO CAP. Switching
candidate_tier.enabled off stops NEW candidates, but the ones already filed stay
selector-invisible (§2) and reachable only through the groomers' capped promotes,
so the flag alone does not restore the status quo. `drain` promotes every
candidate in one pass through the same status write a grooming promote uses. Each
§5 row is stamped promoted_by=kill-switch-drain: promotes_in_window excludes that
stamp, so a drain cannot spend a groomer's budget, and it is NOT the starvation
stamp, which metric (e) counts. A dry run unless --apply; it never edits the
flag, and it reads the flag back so the operator is told when it is still on.

Exit codes. Clear of python's 1 and argparse's 2 (guard-5093); an unconfirmed
follow-up leg is distinct from success (guard-946):
  0  applied (or would apply, under --dry-run)
  4  STALE    not a live candidate at the store of record — re-bite
  5  CAP      this firing's promote budget is spent
  6  INVALID  an argument failed live validation; nothing was written
  7  PARTIAL  the verdict landed, a follow-up leg (survivor pointer,
              re-home, keep ledger row) did not confirm; drain: a
              candidate is still there after the pass
  1  error    daemon unreachable, unreadable config, unexpected failure
"""

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from operator import itemgetter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _rt  # noqa: E402  canonical Python -> daemon client
from _dt import parse_naive_iso  # noqa: E402
from _paths import AGENT_NAME, META_DIR, WORLD_DIR  # noqa: E402
from _sweep_write_guard import PROV_LOCAL_MIRROR, stale_candidate_reason  # noqa: E402
from gates import candidate_transition as ct  # noqa: E402
from gates import intake_route  # noqa: E402

RC_OK, RC_ERROR, RC_STALE, RC_CAP, RC_INVALID, RC_PARTIAL = 0, 1, 4, 5, 6, 7

#: §6 — the aspiration that involuntarily played inbox. Its NON-recurring goals
#: drain out move-on-touch; its recurring goals never move.
LEGACY_INBOX_ASP = "asp-115"
VERDICTS = ("promote", "merge", "rb-route", "close-moot", "keep")
#: verdict -> the status update-goal writes (keep writes no status)
VERDICT_STATUS = {"promote": "pending", "merge": "superseded",
                  "rb-route": "skipped", "close-moot": "skipped"}
#: §8 values, used only while the candidate_tier block is absent (B2 adds it)
SPEC_DEFAULTS = {"groom_bite": 20, "promote_cap_per_firing": 3,
                 "touch_stale_hours": 20}
CONFIG_PATH = SCRIPT_DIR.parent / "config" / "aspirations.yaml"
#: §4 starvation fail-safe rows and §9 kill-switch drain rows are not grooming
#: promotes, so neither spends a groomer's cap. Two stamps, not one: metric (e)
#: reads the starvation stamp alone, and a drain stamped as one would corrupt it.
STARVATION_PROMOTER = "starvation-failsafe"
DRAIN_PROMOTER = "kill-switch-drain"
UNCAPPED_PROMOTERS = (STARVATION_PROMOTER, DRAIN_PROMOTER)
TERMINAL_GOAL = ("completed", "skipped", "expired", "superseded")
DEAD_ASP = ("completed", "retired")
TIER_NAMES = {0: "intended_agent", 1: "role_top_class", 2: "hash_partition"}
#: update-goal refusals that mean "the goal moved under you", not a fault
STALE_REFUSALS = ("candidate_transition_forbidden", "invalid_status_transition",
                  "field_shrink_blocked")


class GroomError(Exception):
    def __init__(self, rc, message):
        super().__init__(message)
        self.rc = rc


def _now():
    return datetime.datetime.now()


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _agent_part(name):
    return str(name or "").split("@", 1)[0].strip().lower()


def _agent():
    if not AGENT_NAME:
        raise GroomError(RC_ERROR, "no agent bound (MIND_AGENT unset)")
    return AGENT_NAME


# --- config -----------------------------------------------------------------

def load_config():
    """(knobs, source). Falls back to §8 values ONLY while the candidate_tier
    block is absent, and every output names the source — never silent."""
    try:
        import yaml
        with open(CONFIG_PATH, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001 — an unreadable config is loud
        raise GroomError(RC_ERROR, f"cannot read {CONFIG_PATH}: {e}")
    block = doc.get("candidate_tier") if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        return dict(SPEC_DEFAULTS), "spec-defaults (candidate_tier block absent)"
    cfg = {}
    for key, default in SPEC_DEFAULTS.items():
        val = block.get(key, default)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise GroomError(RC_ERROR, f"candidate_tier.{key} must be a "
                                       f"non-negative integer, got {val!r}")
        cfg[key] = val
    return cfg, "core/config/aspirations.yaml"


# --- store reads (seams: tests patch these) ---------------------------------

def read_aspirations(source):
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        raise GroomError(RC_ERROR, f"{source} aspirations read failed: {e.body or e}")
    data = _rt.tolerant_decode_aggregate(source, raw)
    if data is None:
        return []
    asps = data.get("aspirations") if isinstance(data, dict) else data
    return [a for a in (asps or []) if isinstance(a, dict)]


def reread(source, goal_id):
    """(goal, provenance) from the STORE OF RECORD, not the local mirror."""
    from _sweep_write_guard import reread_goal_authoritative
    from _team_state import _is_owncloud_backend
    return reread_goal_authoritative(source, goal_id, read_aspirations=read_aspirations,
                                     is_owncloud=_is_owncloud_backend, label="groom")


def live_copies(asps, goal_id):
    """(aspiration, goal) for every non-superseded copy of goal_id — a rehome
    leaves a superseded pointer under the same id, and the pointer is not the
    goal."""
    return [(a, g) for a in asps for g in (a.get("goals") or [])
            if isinstance(g, dict) and g.get("id") == goal_id
            and g.get("status") != "superseded"]


def load_role_multipliers():
    try:
        import yaml
        with open(Path(META_DIR) / "goal-selection-strategy.yaml", encoding="utf-8") as f:
            val = (yaml.safe_load(f) or {}).get("agent_role_multipliers")
    except Exception as e:  # noqa: BLE001 — degrade to no class tier, loudly
        print(f"[groom] role multipliers unreadable ({e}): class tier disabled",
              file=sys.stderr)
        return {}
    return val if isinstance(val, dict) else {}


# --- bite -------------------------------------------------------------------

def top_classes(mults):
    """Work classes at an agent's highest multiplier. A tie keeps every tied
    class; `unclassified` is never a top class."""
    vals = {}
    for key, val in (mults or {}).items():
        if key == "unclassified" or isinstance(val, bool):
            continue
        try:
            vals[key] = float(val)
        except (TypeError, ValueError):
            continue
    if not vals:
        return set()
    top = max(vals.values())
    return {k for k, v in vals.items() if v == top}


def partition_owner(goal_id, roster):
    """hash(goal_id) % live roster with sha1 — never Python's salted hash() —
    so every agent on every box computes the same owner."""
    digest = hashlib.sha1(str(goal_id).encode("utf-8")).hexdigest()
    return roster[int(digest, 16) % len(roster)]


def affinity_tier(goal, agent, mults, roster):
    """0 intended_agent match, 1 role top-class match, 2 hash partition,
    None when another agent owns it (by intent, by class, or by partition).
    "either" is the capability router's no-opinion value, not a pin: it falls
    through to the class and partition tiers like an unset field."""
    intended = goal.get("intended_agent")
    if intended and intended != "either":
        return 0 if _agent_part(intended) == _agent_part(agent) else None
    work_class = goal.get("work_class")
    if work_class and work_class in top_classes(mults.get(agent)):
        return 1
    if work_class and any(work_class in top_classes(mults.get(a))
                          for a in roster if a != agent):
        return None
    if roster and agent in roster and partition_owner(goal.get("id"), roster) == agent:
        return 2
    return None


def promote_budget(cfg, agent, now):
    rows, malformed = read_ledger()
    since = now - datetime.timedelta(hours=cfg["touch_stale_hours"])
    used = promotes_in_window(rows, agent, since)
    cap = cfg["promote_cap_per_firing"]
    return {"cap": cap, "used": used, "remaining": max(0, cap - used),
            "window_hours": cfg["touch_stale_hours"], "since": _stamp(since),
            "ledger_malformed_rows": malformed}


def build_bite(asps, agent, roster, mults, cfg, now):
    stale_before = now - datetime.timedelta(hours=cfg["touch_stale_hours"])
    picked, total, visible, touched = [], 0, 0, 0
    for asp in asps:
        for goal in asp.get("goals") or []:
            if not isinstance(goal, dict) or goal.get("status") != "candidate":
                continue
            total += 1
            tier = affinity_tier(goal, agent, mults, roster)
            if tier is None:
                continue
            visible += 1
            touched_at = parse_naive_iso(goal.get("groom_touched_at"))
            if touched_at is not None and touched_at > stale_before:
                touched += 1  # another groomer (or this one) holds it this firing
                continue
            # "~" sorts after every digit: an undated candidate is never "oldest"
            picked.append((tier, str(goal.get("created") or "~"), str(goal.get("id")), asp, goal))
    picked.sort(key=itemgetter(0, 1, 2))  # tier, then age, then id
    rows = []
    for tier, _created, _gid, asp, goal in picked[:cfg["groom_bite"]]:
        rows.append({
            "goal_id": goal.get("id"),
            "aspiration_id": asp.get("id"),
            "title": str(goal.get("title") or "")[:160],
            "work_class": goal.get("work_class"),
            "intended_agent": goal.get("intended_agent"),
            "created": goal.get("created"),
            "tier": TIER_NAMES[tier],
            "product_outcome": bool(goal.get("product_outcome")),
            "recurring": bool(goal.get("recurring")),
            "moves_on_touch": (asp.get("id") == LEGACY_INBOX_ASP
                               and not goal.get("recurring")),
        })
    # §5: promote picks product_outcome-bearing candidates first
    promote_order = ([r["goal_id"] for r in rows if r["product_outcome"]]
                     + [r["goal_id"] for r in rows if not r["product_outcome"]])
    return {"bite": rows, "promote_order": promote_order, "candidates_total": total,
            "visible_to_agent": visible, "skipped_fresh_touch": touched}


def cmd_bite(args):
    cfg, cfg_source = load_config()
    agent = _agent()
    now = _now()
    mults = load_role_multipliers()
    if args.roster:
        roster = [a.strip() for a in args.roster.split(",") if a.strip()]
    else:
        roster = sorted(mults)
    out = {"agent": agent, "source": args.source, "config": cfg,
           "config_source": cfg_source, "roster": roster,
           "in_roster": agent in roster,
           "top_classes": sorted(top_classes(mults.get(agent)))}
    out.update(build_bite(read_aspirations(args.source), agent, roster, mults, cfg, now))
    out["promote_budget"] = promote_budget(cfg, agent, now)
    print(json.dumps(out, indent=2))
    return RC_OK


# --- ledger -----------------------------------------------------------------

def ledger_path():
    return ct.ledger_path(Path(WORLD_DIR))


def read_ledger():
    """(rows, malformed_count) — every parseable §5 row, in file order."""
    rows, malformed = [], 0
    try:
        with open(ledger_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    malformed += 1
    except FileNotFoundError:
        return [], 0
    return rows, malformed


def promotes_in_window(rows, agent, since):
    """This agent's grooming promotes at or after `since`. A row whose ts does
    not parse COUNTS — the cap fails closed, never open."""
    used = 0
    for row in rows:
        if row.get("verdict") != "promote" or _agent_part(row.get("agent")) != _agent_part(agent):
            continue
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        if evidence.get("promoted_by") in UNCAPPED_PROMOTERS:
            continue
        ts = parse_naive_iso(row.get("ts"))
        if ts is None or ts >= since:
            used += 1
    return used


def _row_digest(row):
    return hashlib.sha1(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()


def sample_rows(rows, n):
    """A deterministic sample: the n rows with the lowest sha1 of their
    canonical JSON, so two readers of one ledger draw the same sample."""
    return sorted(rows, key=_row_digest)[:n]


def cmd_ledger(args):
    rows, malformed = read_ledger()
    since = parse_naive_iso(args.since) if args.since else None
    if args.since and since is None:
        raise GroomError(RC_INVALID, f"--since {args.since!r} is not an ISO timestamp")
    picked = []
    for row in rows:
        if args.agent and _agent_part(row.get("agent")) != _agent_part(args.agent):
            continue
        if args.verdict and row.get("verdict") != args.verdict:
            continue
        if since is not None:
            ts = parse_naive_iso(row.get("ts"))
            if ts is None or ts < since:
                continue
        picked.append(row)
    counts = {}
    for row in picked:
        counts[row.get("verdict")] = counts.get(row.get("verdict"), 0) + 1
    out = {"ledger": str(ledger_path()), "total_rows": len(rows),
           "malformed_rows": malformed, "matched": len(picked),
           "verdict_counts": counts,
           "rows": sample_rows(picked, args.sample) if args.sample else picked[-args.limit:]}
    print(json.dumps(out, indent=2))
    return RC_OK


# --- verdict ----------------------------------------------------------------

def validate_survivor(asps, survivor, goal_id):
    if survivor == goal_id:
        raise GroomError(RC_INVALID, "a candidate cannot merge into itself")
    copies = live_copies(asps, survivor)
    if not copies:
        raise GroomError(RC_INVALID, f"survivor {survivor} has no live copy in this store")
    status = copies[0][1].get("status")
    if status in TERMINAL_GOAL:
        raise GroomError(RC_INVALID, f"survivor {survivor} is {status}: merge folds into "
                                     f"a LIVE goal; a candidate whose work is already "
                                     f"done is close-moot")


def validate_rb(rb_id):
    """The rb entry must EXIST: a constructed id is refused ()."""
    if not re.fullmatch(r"rb-\d+", rb_id or ""):
        raise GroomError(RC_INVALID, f"--rb-id {rb_id!r} is not an rb-NNN id")
    try:
        rec = json.loads(_rt.rt_call("GET", "/v1/rb/read", query="id=" + rb_id))
    except _rt.RtError as e:
        if e.status == 404:
            raise GroomError(RC_INVALID, f"{rb_id} is not in the reasoning bank — "
                                         f"encode it first, then route")
        raise GroomError(RC_ERROR, f"reasoning-bank read failed: {e.body or e}")
    if not (isinstance(rec, dict) and rec.get("id") == rb_id):
        raise GroomError(RC_INVALID, f"reasoning-bank read did not return {rb_id}")


def plan_rehome(asps, holder, goal, verdict, rehome_to):
    """None when nothing moves; else {"to": asp-id, "lane": how} or
    {"deferred": why}. Move-on-touch (I6) is for a NON-keep verdict on a
    NON-recurring goal currently in the legacy inbox — nothing else moves."""
    movable = (verdict != "keep" and holder == LEGACY_INBOX_ASP
               and not goal.get("recurring"))
    if not movable:
        if rehome_to:
            raise GroomError(RC_INVALID, f"--rehome-to applies only to a non-keep verdict "
                                         f"on a non-recurring {LEGACY_INBOX_ASP} goal "
                                         f"(this one: {holder}, verdict {verdict})")
        return None
    live = {a.get("id"): a for a in asps
            if a.get("status") not in DEAD_ASP and not a.get("archived")}
    if rehome_to:
        if rehome_to == LEGACY_INBOX_ASP or rehome_to not in live:
            raise GroomError(RC_INVALID, f"--rehome-to {rehome_to} is not a live aspiration "
                                         f"other than {LEGACY_INBOX_ASP}")
        return {"to": rehome_to, "lane": "lane-matched"}
    inboxes = sorted(aid for aid, a in live.items() if a.get("triage_inbox") is True)
    if not inboxes:
        return {"deferred": "no-triage-inbox"}
    return {"to": inboxes[0], "lane": "triage-inbox"}


def note_block(verdict, agent, ts, evidence, survivor=None, rb_id=None):
    head = f"GROOM {verdict} ({ts}, {agent})"
    if verdict == "merge":
        return f"{head}: merged into survivor {survivor}. {evidence}"
    if verdict == "rb-route":
        return f"{head}: routed to {rb_id}. {evidence}"
    return f"{head}: {evidence}"


def _refusal_kind(err):
    try:
        body = json.loads(err.body) if isinstance(err.body, str) else (err.body or {})
    except ValueError:
        body = {}
    return body.get("error") if isinstance(body, dict) else None


def write_status(goal_id, source, goal, new_status, block, verdict=None, evidence=None):
    """Status and note land in ONE write (the update-goal companion body), so
    a refusal leaves neither; the note APPENDS — the field-shrink guard refuses
    a note that would drop prior text (guard-5228).

    `verdict` names the §5 ledger label when it is not the table default for
    this status (rb-route lands as `skipped`, whose default is close-moot); it
    rides the same body as ledger_verdict (g-353-166). `evidence` rides it as
    ledger_evidence: the drain stamps its promoted_by that way."""
    prior = goal.get("outcome_note") or ""
    note = f"{prior}\n\n{block}" if prior else block
    body = {"value": new_status, "outcome_note": note}
    if verdict and verdict != ct.ALLOWED_TRANSITIONS.get(new_status):
        body["ledger_verdict"] = verdict
    if evidence:
        body["ledger_evidence"] = evidence
    try:
        resp = _rt.aspirations_update_goal(goal_id, "status", body, source=source)
    except _rt.RtError as e:
        if _refusal_kind(e) in STALE_REFUSALS:
            raise GroomError(RC_STALE, f"{goal_id}: the write path refused {new_status} "
                                       f"({_refusal_kind(e)}) — re-bite")
        raise GroomError(RC_ERROR, f"{goal_id}: update-goal failed: {e.body or e}")
    landed = ((resp or {}).get("goal") or {}).get("status") if isinstance(resp, dict) else None
    if landed != new_status:
        raise GroomError(RC_ERROR, f"{goal_id}: update-goal answered but the stored "
                                   f"status reads {landed!r}, not {new_status!r}")


def append_survivor_pointer(survivor, source, goal, evidence, ts):
    """I5 — the survivor gains an evidence pointer (idempotent per candidate)."""
    text = (f"MERGED IN by grooming ({ts}): candidate {goal.get('id')} "
            f"({str(goal.get('title') or '')[:120]}) folds into this goal. "
            f"Evidence: {evidence}")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "goal-field-append.py"), "--source", source,
         survivor, "progress_note", f"groom-merge-{goal.get('id')}", text],
        capture_output=True, text=True, cwd=str(SCRIPT_DIR.parent.parent))
    return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()[-300:]


def promote_lock_path(agent):
    return Path(WORLD_DIR) / f"candidate-grooming-promote-{_agent_part(agent)}.lock"


def cmd_verdict(args):
    cfg, cfg_source = load_config()
    agent = _agent()
    verdict, goal_id, source = args.verdict, args.goal, args.source
    evidence = (args.evidence or "").strip()
    if verdict != "keep" and not evidence:
        raise GroomError(RC_INVALID, f"--evidence is required for {verdict} (I5)")
    if (verdict == "merge") != bool(args.survivor):
        raise GroomError(RC_INVALID, "--survivor is required for merge, and only merge")
    if (verdict == "rb-route") != bool(args.rb_id):
        raise GroomError(RC_INVALID, "--rb-id is required for rb-route, and only rb-route")

    goal, provenance = reread(source, goal_id)
    if provenance == PROV_LOCAL_MIRROR:  # a plumbing fault, not a stale bite (guard-5093)
        raise GroomError(RC_ERROR, f"{goal_id}: store of record unreachable; the local "
                                   f"mirror cannot prove the goal is still a candidate")
    why = stale_candidate_reason(goal, provenance, open_statuses=("candidate",))
    if why:
        raise GroomError(RC_STALE, f"{goal_id}: {why}")

    asps = read_aspirations(source)
    copies = live_copies(asps, goal_id)
    holder = copies[0][0].get("id") if copies else None
    if verdict == "merge":
        validate_survivor(asps, args.survivor, goal_id)
    if verdict == "rb-route":
        validate_rb(args.rb_id)
    move = plan_rehome(asps, holder, goal, verdict, args.rehome_to)

    now = _now()
    ts = _stamp(now)
    result = {"goal_id": goal_id, "verdict": verdict, "agent": agent, "from_asp": holder,
              "config_source": cfg_source, "rehome": move, "dry_run": args.dry_run,
              "warnings": []}

    if verdict == "keep":
        if not args.dry_run:
            resp = _rt.aspirations_update_goal(goal_id, "groom_touched_at", ts, source=source)
            stored = ((resp or {}).get("goal") or {}).get("groom_touched_at")
            if stored != ts:
                raise GroomError(RC_ERROR, f"{goal_id}: groom_touched_at reads {stored!r} "
                                           f"after the write, not {ts!r}")
            if not ct.append_ledger(Path(WORLD_DIR), goal_id=goal_id, new_status="candidate",
                                    agent=agent, verdict="keep",
                                    evidence={"groom_touched_at": ts, "note": evidence}):
                result["warnings"].append("keep ledger row was not appended")
        return _finish(result)

    block = note_block(verdict, agent, ts, evidence, args.survivor, args.rb_id)
    if verdict == "promote":
        result["product_outcome"] = bool(goal.get("product_outcome"))
        _promote(cfg, agent, now, goal_id, source, goal, block, move, result, args.dry_run)
    elif not args.dry_run:
        _move_then_write(goal_id, source, goal, VERDICT_STATUS[verdict], block, move, verdict, result)
    if args.dry_run:
        return _finish(result)

    if verdict == "merge":
        ok, detail = append_survivor_pointer(args.survivor, source, goal, evidence, ts)
        if not ok:
            result["warnings"].append(f"survivor pointer on {args.survivor} not confirmed: {detail}")
    return _finish(result)


def _move_then_write(goal_id, source, goal, new_status, block, move, verdict, result):
    """MOVE FIRST, then write the verdict onto the moved copy. The other order
    cannot work for merge: the verdict makes the candidate `superseded`, and a
    rehome moves only a live copy. Writing after the move is safe because
    every write-path lookup prefers the non-superseded copy over the pointer
    the move leaves behind (aspirations_write._find_goal). A failed move is a
    warning, not a refusal — the verdict still lands, in place."""
    if move and move.get("to"):
        try:
            moved = _rt.aspirations_rehome_goal(goal_id, move["to"], source=source,
                                                reason=f"move-on-touch on {verdict} (g-353-65)")
            move["result"] = moved
            if not (isinstance(moved, dict) and moved.get("ok")):
                result["warnings"].append(f"re-home to {move['to']} not confirmed: {moved}")
        except _rt.RtError as e:
            result["warnings"].append(f"re-home to {move['to']} failed: {e.body or e}")
    write_status(goal_id, source, goal, new_status, block, verdict)


def _promote(cfg, agent, now, goal_id, source, goal, block, move, result, dry_run):
    """The HARD cap: count, move and write under one per-agent lock, so the
    count a promote is admitted on is the count it is written against."""
    from _fileops import acquire_lock, release_lock
    lock = promote_lock_path(agent)
    acquire_lock(str(lock), timeout=60, stale_seconds=120)
    try:
        rows, _malformed = read_ledger()
        since = now - datetime.timedelta(hours=cfg["touch_stale_hours"])
        used = promotes_in_window(rows, agent, since)
        cap = cfg["promote_cap_per_firing"]
        result["promote_budget"] = {"cap": cap, "used_before": used,
                                    "window_hours": cfg["touch_stale_hours"],
                                    "since": _stamp(since)}
        if used >= cap:
            raise GroomError(RC_CAP, f"promote cap reached: {used}/{cap} promotes by "
                                     f"{agent} since {_stamp(since)} (this firing)")
        if not dry_run:
            _move_then_write(goal_id, source, goal, "pending", block, move, "promote", result)
    finally:
        release_lock(str(lock))


def _finish(result):
    print(json.dumps(result, indent=2))
    if result["warnings"]:
        for w in result["warnings"]:
            print(f"[groom] WARN {w}", file=sys.stderr)
        return RC_PARTIAL
    return RC_OK


# --- drain ------------------------------------------------------------------

def _candidates(source):
    """[(aspiration id, goal)] for every candidate-status goal, from one live read."""
    return [(asp.get("id"), goal) for asp in read_aspirations(source)
            for goal in asp.get("goals") or []
            if isinstance(goal, dict) and goal.get("status") == "candidate"]


def cmd_drain(args):
    """§9 kill switch: every candidate -> pending in one pass, under no cap.

    Every queue is read BEFORE the first write, so an unreadable one leaves
    nothing half-drained. A write the store refuses is listed, not fatal, and the
    verdict is a FRESH read after the pass: exit 0 only when no candidate is left
    (one another groomer already moved is not left)."""
    agent = _agent()
    sources = ("world", "agent") if args.source == "all" else (args.source,)
    found = {source: _candidates(source) for source in sources}
    flag_on = bool(intake_route.load_config(SCRIPT_DIR.parent.parent)["enabled"])
    ts = _stamp(_now())
    result = {"agent": agent, "dry_run": not args.apply, "sources": list(sources),
              "flag_enabled": flag_on, "candidates": sum(map(len, found.values())),
              "by_aspiration": {}, "warnings": []}
    for source, rows in found.items():
        for asp_id, _goal in rows:
            key = f"{source}:{asp_id}"
            result["by_aspiration"][key] = result["by_aspiration"].get(key, 0) + 1
    if flag_on:
        print("[groom] NOTE candidate_tier.enabled is still true: new candidates keep "
              "arriving, so a drain now is a flush, not the kill switch. Set it false "
              "in core/config/aspirations.yaml first, then drain again.", file=sys.stderr)
    if not args.apply:
        result["would_promote"] = [g.get("id") for rows in found.values() for _a, g in rows]
        return _finish(result)

    result.update(promoted=[], refused=[], remaining=[])
    block = (f"KILL-SWITCH DRAIN ({ts}, {agent}): candidate_tier was switched off; "
             f"promoted to pending with no grooming verdict (§9)")
    for source, rows in found.items():
        for _asp_id, goal in rows:
            try:
                write_status(goal.get("id"), source, goal, "pending", block, "promote",
                             {"promoted_by": DRAIN_PROMOTER})
                result["promoted"].append(goal.get("id"))
            except GroomError as e:
                result["refused"].append({"goal_id": goal.get("id"), "rc": e.rc,
                                          "reason": str(e)})
    try:
        result["remaining"] = [g.get("id") for s in sources for _a, g in _candidates(s)]
    except GroomError as e:
        result["warnings"].append(f"verification read failed, drain not proven complete: {e}")
    if result["remaining"]:
        result["warnings"].append(
            f"{len(result['remaining'])} candidate(s) remain after the pass: "
            f"{', '.join(map(str, result['remaining'][:10]))}")
    return _finish(result)


# --- cli --------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="groom.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bite", help="this agent's grooming slice")
    b.add_argument("--source", default="world", choices=("world", "agent"))
    b.add_argument("--roster", help="comma-separated live roster (default: the "
                                    "agent_role_multipliers keys)")

    v = sub.add_parser("verdict", help="apply ONE ledgered verdict to ONE candidate")
    v.add_argument("--goal", required=True)
    v.add_argument("--verdict", required=True, choices=VERDICTS)
    v.add_argument("--evidence", help="required for every verdict except keep")
    v.add_argument("--survivor", help="merge: the live goal this candidate folds into")
    v.add_argument("--rb-id", dest="rb_id", help="rb-route: the reasoning-bank entry")
    v.add_argument("--rehome-to", dest="rehome_to",
                   help="move-on-touch lane target (default: the Triage Inbox)")
    v.add_argument("--source", default="world", choices=("world", "agent"))
    v.add_argument("--dry-run", action="store_true")

    lg = sub.add_parser("ledger", help="read / sample the §5 verdict ledger")
    lg.add_argument("--agent")
    lg.add_argument("--verdict", choices=VERDICTS)
    lg.add_argument("--since", help="ISO timestamp lower bound")
    lg.add_argument("--sample", type=int, default=0,
                    help="deterministic sample of N matching rows (oversight, I4)")
    lg.add_argument("--limit", type=int, default=50,
                    help="without --sample: the most recent N matching rows")

    dr = sub.add_parser("drain", help="§9 kill switch: every candidate -> pending "
                                      "(a dry run unless --apply)")
    dr.add_argument("--apply", action="store_true", help="write; without it, only report")
    dr.add_argument("--source", default="all", choices=("world", "agent", "all"))
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    handler = {"bite": cmd_bite, "verdict": cmd_verdict, "ledger": cmd_ledger,
               "drain": cmd_drain}[args.cmd]
    try:
        return handler(args)
    except GroomError as e:
        print(json.dumps({"error": str(e), "rc": e.rc}), file=sys.stderr)
        return e.rc
    except _rt.RtError as e:
        print(json.dumps({"error": f"daemon: {e.body or e}", "rc": RC_ERROR}), file=sys.stderr)
        return RC_ERROR


if __name__ == "__main__":
    sys.exit(main())
