#!/usr/bin/env python3
"""Escalate dependency-blocked goals approaching the dependency timeout.

Bash-enforces aspirations-precheck Phase 0.5b.2 (g-115-3124). Third and last
member of the precheck escalation-sweep family; its two siblings were already
scripted:

    0.5b.1b  inbox-alert-age-check.py      (g-115-848)
    0.5b.2b  handoff-aging-check.py        (g-115-1524)
    0.5b.11  reason-less-blocked-check.py  (g-115-2595)
    0.5b.2   THIS SCRIPT                   (g-115-3124)

WHY IT EXISTS: Phase 0.5b.2 was LLM-iterated pseudocode AND absent from the
precheck phase table entirely — no sweep name, no tier, no budget-meter row —
so it was invisible to the mechanism that decides what survives context
pressure. Measured consequence (zeta, 2026-07-25): the escalation log was
COMPLETELY EMPTY fleet-wide while 8+ dependency-blocked goals sat past the 36h
threshold (ages up to 1689h). This phase is the fleet's ONLY automated path
from a stuck dependency to the human, and it had never fired.

=== DELIBERATE DEVIATION FROM THE GOAL'S PRESCRIBED COOLDOWN (read before
=== "fixing" this back)

g-115-3124 specifies idempotency "via the proactive_escalation_log cooldown
keyed dep_<goal_id>". This script does NOT use that slot, because BOTH siblings
deliberately ABANDONED it (g-115-1531) for two production bugs that apply with
MORE force here, not less:

  1. N-AGENT DUPLICATE. `proactive_escalation_log` is a PER-AGENT working-memory
     slot, but dependency-blocked goals are WORLD goals every agent sees. Six
     agents each keep their own log, so each escalates the same goal
     independently (observed 2026-06-18 on the handoff sibling: ~30 posts for
     ~7 handoffs). Here the escalation action is EMAIL THE HUMAN — so the
     duplicate lands in the user's inbox six times per stuck dependency, which
     is strictly worse than the board-post duplicate the siblings suffered.
     Live proof the bug is already active: zeta appended dep_g-335-211 /
     dep_g-335-212 entries to ZETA's WM on 2026-07-25; those entries cannot
     suppress any other agent's escalation of the same two goals.
  2. NON-DURABLE. A working-memory reset between iterations wipes the log, so
     even the SAME agent re-fires.

Instead this script uses the siblings' SHARED, DURABLE cooldown: the
coordination-board post IS the cooldown record. It is shared (every agent reads
the same board) and durable (board posts persist in world/board/, surviving WM
resets). Single source of truth — the escalation and its cooldown are ONE
artifact, with no second ledger to keep in sync (communication-clarity rule 5).

SPLIT OF RESPONSIBILITY (why this script does not send the email itself):
core/ must stay domain-agnostic (.claude/rules/domain-free-examples.md), and
notification transport is a forged, domain-owned skill. So:
  * THIS SCRIPT owns the durable half — the board post (the cooldown record)
    and the priority boost. Those happen deterministically under --apply.
  * THE SKILL.md PHASE owns the transport half — it reads
    `needs_user_notification` from this script's JSON and invokes the forged
    notification skill per entry.
Because the cooldown is written by the script, a failure to send the email
never causes a duplicate-escalation storm: the next sweep sees the board post
and stands down regardless.

FAIL-OPEN at every layer: a bad config read, an unreadable queue, a failed
board scan, or a failed boost degrades to "escalate fewer things this run"
(recoverable next sweep) — never to aborting the precheck.

Usage:
  dependency-timeout-check.py [--apply] [--threshold-hours N] [--agent NAME]
                              [--board-escalation-log PATH]  # tests only
                              [--no-board]                   # tests only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _rt  # canonical Python -> daemon client (post-cutover)
from _runtime_bash import bash_cmd  # : Windows-safe bash resolution

DEFAULT_DEPENDENCY_TIMEOUT_HOURS = 48.0
ESCALATE_AT_FRACTION = 0.75  # notify at 75% of the timeout (36h of 48h)
BOARD_TAG = "dependency-aged"
# Marks WHICH goal a post is about. The post also carries the bare goal_id
# and root_id tags for human board search; only this prefixed tag is the
# cooldown key, so a post about one goal can never suppress its root.
SUBJECT_TAG_PREFIX = "dep-subject:"


def _parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "").strip())
    except Exception:
        return None


def _age_hours(iso_ts, now):
    ts = _parse_iso(iso_ts)
    if ts is None:
        return None
    return (now - ts).total_seconds() / 3600.0


def _load_threshold_hours(args) -> float:
    """Escalation threshold = 0.75 * multi_agent.dependency_timeout_hours."""
    if args.threshold_hours is not None:
        return float(args.threshold_hours)
    timeout = DEFAULT_DEPENDENCY_TIMEOUT_HOURS
    try:
        import yaml
        with open(CORE_ROOT / "config" / "aspirations.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        val = (cfg.get("multi_agent") or {}).get("dependency_timeout_hours")
        if val is not None:
            timeout = float(val)
    except Exception as e:
        sys.stderr.write(
            "dependency-timeout-check: config read failed (%s) — using default %.0fh\n"
            % (e, timeout))
    return timeout * ESCALATE_AT_FRACTION


def _resolve_self_agent(args) -> str:
    return args.agent or os.environ.get("MIND_AGENT") or ""


def _read_goal_index() -> dict:
    """goal_id -> goal dict (with _source/_aspiration_id), across both queues.

    FAIL-OPEN per source: a missing source means fewer escalations this run,
    which is recoverable. Aborting the precheck would not be.
    """
    index = {}
    for source in ("world", "agent"):
        try:
            out = _rt.aspirations_read(source=source, active=True)
        except Exception as e:
            sys.stderr.write(
                "dependency-timeout-check: %s read failed (%s) — fail-open\n"
                % (source, e))
            continue
        data = _rt.tolerant_decode_aggregate(
            "[dependency-timeout-check] %s" % source, out)
        if data is None:
            continue
        asps = (data.get("aspirations") if isinstance(data, dict) else data) or []
        for asp in asps:
            for g in asp.get("goals", []) or []:
                if isinstance(g, dict) and g.get("id"):
                    g["_source"] = source
                    g["_aspiration_id"] = asp.get("id")
                    index[g["id"]] = g

    # ARCHIVE PASS (, sibling audit of ). The reads above are
    # active=True, so a blocked_by root that COMPLETED inside a since-archived
    # aspiration is absent from this index. `_root_of` then hits
    # `root = index.get(rid); if root is None: return rid, None`, the root-walk
    # stops, and the chain is reported unresolved when it is in fact satisfied —
    # the blocked_by-edge instance of the same class. Same family as guard-1715:
    # the all-clear was bounded by the population this index declared.
    #
    # LIVE WINS on collision (`not in index`): an id in both stores means the live
    # record is current (re-opened goal, or a mid-archive race) and the archive
    # copy is a stale snapshot that must never shadow it. Do NOT simplify this to
    # an unconditional assignment.
    #
    # `_archived` marks origin so "resolved via the archive" stays distinguishable
    # from "resolved live" and from "in neither store" (which still yields
    # root None, correctly).
    #
    # FAIL-OPEN per source, matching the live loop above: losing the archive
    # degrades to exactly the pre-fix behavior (fewer resolutions this run), which
    # is recoverable, whereas aborting the precheck is not.
    for source in ("world", "agent"):
        try:
            out = _rt.aspirations_read(source=source, archive=True)
        except Exception as e:
            sys.stderr.write(
                "dependency-timeout-check: %s archive read failed (%s) — "
                "fail-open, archived roots stay unresolved this run\n"
                % (source, e))
            continue
        data = _rt.tolerant_decode_aggregate(
            "[dependency-timeout-check] %s archive" % source, out)
        if data is None:
            continue  # empty archive is a valid state, not a failure
        # ?archive=1 returns a BARE list, not the {"aspirations": [...]} envelope.
        asps = (data.get("aspirations") if isinstance(data, dict) else data) or []
        for asp in asps:
            for g in asp.get("goals", []) or []:
                if isinstance(g, dict) and g.get("id") and g["id"] not in index:
                    g["_source"] = source
                    g["_aspiration_id"] = asp.get("id")
                    g["_archived"] = True
                    index[g["id"]] = g
    return index


def _read_blocked() -> dict:
    """goal-selector blocked view. Returns {} on any failure (fail-open)."""
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "goal-selector.sh", "blocked"),
            capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            sys.stderr.write(
                "dependency-timeout-check: goal-selector blocked rc=%s — fail-open\n"
                % proc.returncode)
            return {}
        return json.loads(proc.stdout)
    except Exception as e:
        sys.stderr.write(
            "dependency-timeout-check: goal-selector blocked failed (%s) — fail-open\n" % e)
        return {}


def _window_str(hours: float) -> str:
    """board-read --since wants int+unit; round up + 1h margin so the read
    window safely covers the whole cooldown window."""
    return "%dh" % (int(math.ceil(hours)) + 1)


def _decode_board(raw: str) -> list:
    """board-read.sh --json emits JSONL — ONE object per line, NOT an array.

    A single json.loads() on the whole stream raises "Extra data: line 2" and,
    under this module's fail-open policy, silently yields an EMPTY cooldown set
    — which re-arms the very N-agent escalation storm this sweep is designed to
    avoid (every agent would escalate every eligible goal, every sweep). Caught
    by the g-115-3124 dry run before first --apply. Array form is still
    accepted so a future board-read shape change degrades gracefully.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("messages") or data.get("posts") or []
    except Exception:
        pass
    posts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            posts.append(obj)
    return posts


def _read_recent_escalations(threshold_hours, board_log_path=None) -> set:
    """Set of goal_ids already escalated by ANY agent inside the window.

    THE SHARED, DURABLE COOLDOWN — see the module docstring for why this
    replaces the per-agent `proactive_escalation_log` the goal text specifies.
    FAIL-OPEN: an unreadable board yields an empty set, so eligible goals
    re-escalate (additive and recoverable) rather than being silently dropped.
    """
    posts = []
    if board_log_path is not None:
        try:
            with open(board_log_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            posts = data if isinstance(data, list) else []
        except Exception:
            posts = []
    else:
        try:
            proc = subprocess.run(
                bash_cmd(SCRIPT_DIR / "board-read.sh",
                         "--channel", "coordination",
                         "--since", _window_str(threshold_hours),
                         "--json"),
                capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                posts = _decode_board(proc.stdout)
        except Exception as e:
            sys.stderr.write(
                "dependency-timeout-check: board scan failed (%s) — no cooldown\n" % e)
            posts = []

    seen = set()
    for p in posts:
        if not isinstance(p, dict):
            continue
        tags = p.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        if BOARD_TAG not in tags:
            continue
        # Key ONLY on the SUBJECT of the escalation. Each post is tagged with
        # the subject AND its root (both for human discoverability), so
        # harvesting every "g-" tag would let a post ABOUT one goal suppress
        # its ROOT's own future escalation — silent non-escalation, the exact
        # failure this phase exists to prevent. Measured before the fix: 14
        # suppressed ids of which 6 were roots that had never been escalated
        # in their own right (, , , ,
        # , ).
        subject = None
        for t in tags:
            if isinstance(t, str) and t.startswith(SUBJECT_TAG_PREFIX):
                subject = t[len(SUBJECT_TAG_PREFIX):]
                break
        if subject is None:
            # Legacy post (pre-marker): recover the subject from the body,
            # whose first line this script controls: "Dependency aged Nh:
            # <goal_id> is blocked on <root_id>." Avoids a one-off
            # re-escalation round for posts already on the board.
            body = p.get("body") or p.get("message") or p.get("text") or ""
            mm = re.search(r"Dependency aged [\d.]+h:\s*(g-[\w-]+)\s+is blocked on", body)
            if mm:
                subject = mm.group(1)
        if subject:
            seen.add(subject)
    return seen


def _root_of(goal, index):
    """First unresolved blocker of `goal`, as (root_id, root_goal_or_None)."""
    bb = goal.get("blocked_by") or []
    if isinstance(bb, str):
        bb = [bb]
    for rid in bb:
        root = index.get(rid)
        if root is None:
            return rid, None  # cross-queue / unknown — still the named root
        if root.get("status") not in ("completed", "skipped", "expired"):
            return rid, root
    return (bb[0] if bb else None), (index.get(bb[0]) if bb else None)


# ---------------------------------------------------------------------------
# STALE-DEPENDENCY DETECTION ()
#
# `_root_of` above returns the FIRST UNRESOLVED blocker. When every blocker has
# gone terminal its loop falls through and returns `bb[0]` — a COMPLETED goal —
# and run() then escalates that as though it were a live blocker. So a goal
# whose dependencies are all satisfied stays blocked forever, and because
# goal-selector path (d) treats a non-empty `blocked_by` as a dependency wait,
# it never reaches the scorer either. Measured instances in the originating
# goal:  sat blocked 7 days on  (completed);  named a
# completed unblock_goal with an expires_at two days past. Both had to be
# unblocked BY HAND.
#
# WHY A SEPARATE TERMINAL SET. `_root_of` tests
# ("completed", "skipped", "expired") and is deliberately left alone — widening
# it would change WHICH root gets escalated on live chains, which is a
# different behavior change than this goal asks for. This tuple is the
# complete terminal set per CLAUDE.md § Status Values, and the divergence is
# pinned by test_terminal_sets_diverge_deliberately so a future fusion of the
# two fails loudly rather than silently altering escalation.
TERMINAL_FOR_UNBLOCK = (
    "completed", "skipped", "expired", "superseded", "decomposed",
)


def _stale_dependency(goal, index):
    """All of `goal`'s blocked_by targets are terminal -> the receipt, else None.

    FAIL-SAFE ON THE UNKNOWN. An id absent from `index` returns None, NEVER a
    stale verdict: the index is assembled fail-open per source (a failed read
    just yields fewer entries), so "not found" means "could not be resolved",
    not "finished". Treating an unresolvable id as terminal would let one
    degraded read clear a live dependency — the guard-1715 shape, where an
    all-clear is bounded by the population the index happens to declare.
    """
    edges = _blocking_edges(goal)
    if not edges:
        return None  # no edges — not this class (prose-only lives in the census)
    resolved = []
    for rid, origin in edges:
        root = index.get(rid)
        if root is None:
            return None  # unresolvable != terminal
        st = root.get("status")
        if st not in TERMINAL_FOR_UNBLOCK:
            return None  # at least one live blocker — genuinely still blocked
        resolved.append({"root_id": rid, "status": st, "via": origin,
                         "archived": bool(root.get("_archived"))})
    return {"edges": resolved,
            "edge_count": len(resolved),
            "summary": ", ".join("%s=%s(%s)" % (r["root_id"], r["status"], r["via"])
                                 for r in resolved)}


def _blocking_edges(goal):
    """Every goal-id this goal is blocked ON, as [(id, "blocked_by"|"blocker_ref")].

    BOTH FIELDS, because the class this detector serves is defined over both.
    g-115-3201's title says "blocked_by/blocker_ref targets have ALL gone
    terminal", and its SECOND measured instance is a blocker_ref one:
    g-250-258 "named a completed unblock_goal with an expires_at two days
    past". A blocked_by-only reader would have covered exactly one of the two
    incidents the goal was filed from, while its report line still said
    "stale_dependency", so the miss would have read as a clean result —
    reclaim-routed-work.md rule 7 (a reclaim predicate narrower than the
    population that creates it) reproduced inside the remedy for it.

    Deduped, first origin wins, order preserved: the same id commonly appears
    as BOTH a blocked_by edge and the blocker_ref's unblock_goal, and counting
    it twice would inflate edge_count and read as two independent blockers.
    """
    out, seen = [], set()
    bb = goal.get("blocked_by") or []
    if isinstance(bb, str):
        bb = [bb]
    for rid in bb:
        if rid and rid not in seen:
            seen.add(rid)
            out.append((rid, "blocked_by"))
    ref = goal.get("blocker_ref")
    if isinstance(ref, dict):
        # `unblock_goal` is the Blocker Reference Schema's pointer at the goal
        # that clears this blocker. An `external_id` is NOT a goal id and is
        # deliberately not followed — it names infrastructure, which this
        # detector cannot resolve and must not guess at.
        rid = ref.get("unblock_goal")
        if rid and rid not in seen:
            seen.add(rid)
            out.append((rid, "blocker_ref"))
    return out


def _clear_blocked_by(goal_id, source):
    """Drop satisfied blocked_by edges so the goal reaches the scorer again.

    Called ONLY after _post_board has recorded the edges and their statuses, so
    the cleared relation stays recoverable from the board (and from the queue's
    git history) rather than being destroyed silently.
    """
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-update-goal.sh",
                     goal_id, "blocked_by", "[]", "--source", source),
            capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return True, "cleared satisfied blocked_by on %s" % goal_id
        return False, "clear rc=%s %s" % (proc.returncode, (proc.stderr or "")[:160])
    except Exception as e:
        return False, "clear raised: %s" % e


# The widening note on  (bravo, 2026-08-26): this sweep's predicate
# reads blocked_by, so a dependency stated ONLY in prose is outside the
# population and the sweep reports clean on it forever — reclaim-routed-work.md
# rule 7. Measured there:  said "DEPENDS ON  — do not start
# until that spec exists" in prose with no edge;  completed 2026-08-25
# and nothing re-surfaced , which then ranked TOP of 1163 candidates
# for 5 consecutive iterations and was declined each time by readers re-reading
# the stale prose. So the census below is reported BESIDE the filtered count,
# per guard-2298: a zero from this sweep means "no edge-recorded stale
# dependency", never "no stale dependency".
DEP_PHRASE_RE = re.compile(
    r"(depends?\s+on|blocked\s+(?:on|by)|waiting\s+(?:on|for)|"
    r"do\s+not\s+start\s+until|gated\s+on|prerequisite)", re.I)


def _prose_only_dependency_census(index, sample=10):
    """Non-terminal goals stating a dependency in prose with NO structured edge."""
    hits = []
    for gid, g in index.items():
        if g.get("status") in TERMINAL_FOR_UNBLOCK or g.get("status") == "blocked-archived":
            continue
        if g.get("blocked_by") or g.get("blocker_ref") or g.get("defer_reason"):
            continue  # has SOME structured signal — a sweep can reach it
        text = "%s %s" % (g.get("title") or "", g.get("description") or "")
        m = DEP_PHRASE_RE.search(text)
        if m:
            hits.append({"goal_id": gid, "status": g.get("status"),
                         "phrase": m.group(0).lower(),
                         "title": (g.get("title") or "")[:70]})
    hits.sort(key=lambda h: h["goal_id"])
    return {"count": len(hits), "sample": hits[:sample],
            "note": "EXCLUDED from this sweep by construction (no structured "
                    "edge to read). A zero in `stale_dependency` is not "
                    "evidence these are unblocked — guard-2298."}


# A root can be genuinely user-gated while its `participants` still reads
# ["agent"] — the gate lives in defer_reason instead. Observed on 
# (defer_reason "credentials-required: ...", participants ["agent"]), the very
# root that left /-212 stuck 93h with no human ever told. Routing on
# participants ALONE therefore stays silent on exactly the blocker class this
# phase exists to escalate. These prefixes mirror the HUMAN_ONLY_BLOCKER_TYPES
# used by blocker-create-gate.py. ()
HUMAN_GATED_DEFER_PREFIXES = (
    "credentials-required", "human_blocked", "user_action",
    "security-trust", "physical-hardware",
)


def _human_gated(goal) -> bool:
    """True when the goal's defer_reason names a human-only blocker class."""
    dr = (goal or {}).get("defer_reason")
    if not isinstance(dr, str):
        return False
    return dr.strip().lower().startswith(HUMAN_GATED_DEFER_PREFIXES)


# ---------------------------------------------------------------------------
# Re-probe before escalating ()
#
# THE DEFECT THIS CLOSES. The notify_user branch below renders the ROOT's
# STORED defer_reason/title/description into an email and sends it to a human.
# It never re-ran the probe that produced that verdict. Measured on
# : alpha filed a credentials-required blocker on 2026-07-22
# concluding the fleet key mint needed a human; zeta's escalation 89.5h later
# relayed that conclusion faithfully AND inherited its confidence, asserting
# to the user that this was "a genuine human-only class ... not work an agent
# routed around". A ~5-minute read-only re-probe falsified it three days
# later. A blocker good enough to FILE is not automatically good enough to
# INTERRUPT A PERSON WITH, and escalation is exactly the moment the verdict is
# (a) days old, (b) about to consume human attention, and (c) cheap to re-test
# relative to the cost of being wrong. (guard-3161, guard-2286.)
#
# SCOPE IS DELIBERATELY NARROW. Only `credentials-required` is probeable here,
# because it is the one class with a real read-only predicate the fleet already
# owns. `user_action` / `security-trust` / `physical-hardware` have NO probe
# and escalate UNCHANGED — no subprocess is spawned for them, so they take no
# added latency. `human_blocked:` is deliberately NOT probed: it is the one
# STRUCTURED_DEFER_PREFIXES member that never auto-clears
# (probe-before-defer.md), and credential-defer-recheck.py owns that lane under
# its own rules.
PROBEABLE_DEFER_PREFIXES = ("credentials-required",)


def _cred_probe_module():
    """The sibling that OWNS the credential probe, imported lazily.

    NOT a copy of its regexes (communication-clarity rule 5: one source of
    truth). credential-defer-recheck.py is hyphenated so it cannot be a plain
    import, and it does real work at module scope, so this is lazy — it runs
    only for a root that is actually credentials-gated — and fail-open: an
    import error yields None, which lands on the unprobeable branch and
    escalates exactly as before.
    """
    import importlib.util
    try:
        path = SCRIPT_DIR / "credential-defer-recheck.py"
        spec = importlib.util.spec_from_file_location("_dtc_cred_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _reprobe_root(root):
    """Re-test a root's stored blocker verdict. NEVER raises.

    Returns {"outcome", "detail", "at", "key"} where outcome is one of:
      unprobeable  — no defer_reason, or a class with no probe. Escalate
                     unchanged; nothing was spawned.
      cleared      — the probe now SUCCEEDS. The stored verdict is stale:
                     suppress the notification.
      still_blocked— the probe still fails. Escalate, and carry this verdict's
                     OWN timestamp so the human can see the claim is fresh
                     rather than inherited.
      error        — the probe could not be run or raised. FAIL-OPEN: treated
                     exactly like still_blocked so the escalation still fires
                     rather than being silently swallowed. A broken probe must
                     never become a way for a real blocker to go unreported.

    rb-7672 applies to the READER of this verdict, not to this function: a
    probe answers "is the condition true NOW", which is the PREMISE axis only.
    A standing grant that retires the excuse while the condition stays true is
    the RULE axis, and no probe can see it (reclaim-routed-work.md). So
    `still_blocked` means "re-measured, still failing" — never "correctly
    routed to a human", and the escalation body says so.
    """
    at = dt.datetime.now().replace(microsecond=0).isoformat()
    dr = (root or {}).get("defer_reason")
    if not isinstance(dr, str) or not dr.strip().lower().startswith(
            PROBEABLE_DEFER_PREFIXES):
        return {"outcome": "unprobeable", "detail": "no probeable blocker class",
                "at": at, "key": None}
    mod = _cred_probe_module()
    if mod is None:
        return {"outcome": "error", "at": at, "key": None,
                "detail": "credential probe module unavailable"}
    try:
        key = mod._extract_env_key(dr)
    except Exception as e:
        return {"outcome": "error", "at": at, "key": None,
                "detail": "key extraction raised: %s" % e}
    if not key:
        # A credentials-required defer whose text names no key gives the probe
        # nothing to test. That is UNPROBEABLE, not clear — inventing a verdict
        # from an absent measurement is the failure this whole change removes.
        return {"outcome": "unprobeable", "at": at, "key": None,
                "detail": "credentials-required defer names no env key"}
    try:
        present = mod._probe_env_key(key)
    except Exception as e:
        return {"outcome": "error", "at": at, "key": key,
                "detail": "probe raised: %s" % e}
    if present:
        return {"outcome": "cleared", "at": at, "key": key,
                "detail": "env-read.sh has %s -> present" % key}
    return {"outcome": "still_blocked", "at": at, "key": key,
            "detail": "env-read.sh has %s -> absent" % key}


def _reprobe_line(reprobe):
    """The freshness paragraph for the escalation body. '' when unprobeable.

    Returning EMPTY for the unprobeable classes is what makes outcome 4 true
    literally rather than approximately: a user_action / security-trust /
    physical-hardware escalation body stays byte-identical to what it was
    before this change, so nothing about those escalations moved.

    For the two probed outcomes the point is that the human can SEE the claim
    is fresh. The stale-verdict incident this fixes was not caused by a wrong
    blocker — it was caused by a three-day-old blocker being restated in the
    present tense with no indication of its age.
    """
    if not reprobe or reprobe.get("outcome") == "unprobeable":
        return ""
    if reprobe.get("outcome") == "error":
        return ("Re-probe of the root's blocker could not be run at %s (%s), so "
                "this escalation is sent WITHOUT fresh evidence — the stored "
                "verdict below has not been re-tested.\n\n"
                % (reprobe.get("at"), reprobe.get("detail")))
    return ("Re-probed at %s: %s. This is a fresh measurement, not the stored "
            "verdict. Note it re-tests only whether the CONDITION still holds; "
            "if a standing grant has since retired this class of blocker, no "
            "probe can see that.\n\n"
            % (reprobe.get("at"), reprobe.get("detail")))


def _clear_defer(root_id, source):
    """Clear a falsified defer_reason. Mirrors _boost_priority's shape."""
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-update-goal.sh",
                     root_id, "defer_reason", "", "--source", source),
            capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return True, "cleared defer on %s" % root_id
        return False, "clear rc=%s %s" % (proc.returncode, (proc.stderr or "")[:160])
    except Exception as e:
        return False, "clear raised: %s" % e


def _participants(goal):
    p = (goal or {}).get("participants") or ["agent"]
    if isinstance(p, str):
        p = [p]
    return [str(x).lower() for x in p]


def _post_board(goal_id, root_id, age_hours, detail, no_board):
    if no_board:
        return True, "skipped (--no-board)"
    body = ("Dependency aged %.0fh: %s is blocked on %s.\n\n%s"
            % (age_hours, goal_id, root_id, detail))
    tags = "%s,%s%s,%s" % (BOARD_TAG, SUBJECT_TAG_PREFIX, goal_id, goal_id)
    if root_id and str(root_id).startswith("g-"):
        tags += ",%s" % root_id
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "board-post.sh",
                     "--channel", "coordination", "--type", "status",
                     "--tags", tags),
            input=body, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return True, (proc.stdout or "").strip()[:120]
        return False, "board-post rc=%s %s" % (proc.returncode, (proc.stderr or "")[:160])
    except Exception as e:
        return False, "board-post raised: %s" % e


def _boost_priority(root_id, source):
    try:
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "aspirations-update-goal.sh",
                     root_id, "priority", "HIGH", "--source", source),
            capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return True, "boosted %s -> HIGH" % root_id
        return False, "boost rc=%s %s" % (proc.returncode, (proc.stderr or "")[:160])
    except Exception as e:
        return False, "boost raised: %s" % e


def run(args) -> dict:
    now = dt.datetime.now()
    threshold = _load_threshold_hours(args)
    self_agent = _resolve_self_agent(args)

    blocked = _read_blocked()
    dep_entries = [e for e in (blocked.get("blocked_goals") or [])
                   if isinstance(e, dict) and e.get("block_reason") == "dependency"]
    index = _read_goal_index()
    already = _read_recent_escalations(threshold, args.board_escalation_log)

    candidates, escalated, boosted, needs_notify = [], [], [], []
    stale_dependency = []
    reprobe_suppressed = []
    skipped_cooldown, skipped_young, skipped_no_ts, failed = [], [], [], []

    for entry in dep_entries:
        gid = entry.get("goal_id")
        goal = index.get(gid)
        if goal is None:
            continue
        # STALE-DEPENDENCY PASS () — deliberately BEFORE the age
        # gates below. A dependency whose targets have ALL gone terminal is not
        # "young" or "old", it is SATISFIED, and the two gates underneath would
        # each hide it: `blocked_since` is frequently null (those goals land in
        # skipped_no_blocked_since and are never looked at again), and the 36h
        # threshold would make an already-satisfied dependency wait out a clock
        # measuring nothing. Putting the check here is what keeps the detector
        # from shipping inert. It is pure in-memory status resolution over an
        # index already built — no extra I/O for the common no-hit case.
        stale = _stale_dependency(goal, index)
        if stale is not None:
            rec = {"goal_id": gid, "title": (goal.get("title") or "")[:90],
                   "source": goal.get("_source", "world"),
                   "edges": stale["edges"], "summary": stale["summary"],
                   "cleared": None, "detail": None}
            if args.apply:
                # Board FIRST: it records the edges and their statuses, so the
                # relation stays recoverable after the clear rather than being
                # destroyed silently (archive-before-delete's proportionate
                # form for a two-token edge).
                ok, note = _post_board(
                    gid, stale["edges"][0]["root_id"], 0.0,
                    "STALE DEPENDENCY: every blocked_by target of %s is "
                    "terminal (%s). Clearing the satisfied edges so the goal "
                    "reaches the scorer again; goal-selector path (d) treats a "
                    "non-empty blocked_by as a live dependency wait, so it "
                    "could not rank while they stood. (g-115-3201)"
                    % (gid, stale["summary"]),
                    args.no_board)
                if ok:
                    cok, cnote = _clear_blocked_by(gid, rec["source"])
                    rec["cleared"], rec["detail"] = cok, cnote
                    if not cok:
                        failed.append({"goal_id": gid, "detail": cnote})
                else:
                    rec["cleared"], rec["detail"] = False, note
                    failed.append({"goal_id": gid, "detail": note})
            stale_dependency.append(rec)
            continue

        # blocked_since lives on the GOAL RECORD, not on the selector entry
        # (every selector entry reports blocked_since=None — verified
        # 2026-07-25). Reading it off the entry would skip 100% of candidates.
        age = _age_hours(goal.get("blocked_since"), now)
        if age is None:
            skipped_no_ts.append(gid)  # fail-open: selector clears it eventually
            continue
        if age < threshold:
            skipped_young.append({"goal_id": gid, "age_hours": round(age, 1)})
            continue
        if gid in already:
            skipped_cooldown.append({"goal_id": gid, "age_hours": round(age, 1)})
            continue

        root_id, root = _root_of(goal, index)
        rparts = _participants(root) if root else []
        # "agent-resolvable" means ANY non-user participant, not the literal
        # string "agent": a root routed to a NAMED agent (participants:
        # ["foxtrot"]) is just as agent-resolvable as ["agent"], and is the
        # common shape for cross-agent roots. Matching only "agent" sent every
        # named-agent root down log_only, so the two oldest chains in the fleet
        # ( / , both rooted on a foxtrot goal, aged >1500h)
        # would never have been boosted. Caught by the  dry run.
        route = "log_only"
        agent_resolvable = bool(rparts) and any(p != "user" for p in rparts)
        if root is not None and (("user" in rparts) or _human_gated(root)):
            route = "notify_user"
        elif (root is not None and agent_resolvable
              and root.get("status") == "pending"
              and root.get("priority") != "HIGH"):
            route = "boost_root"

        c = {"goal_id": gid, "title": (goal.get("title") or "")[:90],
             "age_hours": round(age, 1), "root_id": root_id,
             "root_title": (root.get("title") if root else None),
             "root_participants": rparts, "route": route,
             "source": goal.get("_source", "world")}
        candidates.append(c)

        if not args.apply:
            continue

        # : RE-PROBE BEFORE THE BOARD POST, not after the route
        # branch below. _post_board is what records the durable cooldown, so a
        # suppressed escalation that had already posted would burn its own
        # cooldown slot and stay silent on the NEXT sweep too — turning a
        # correct suppression into an accidental permanent one.
        reprobe = _reprobe_root(root) if route == "notify_user" else None
        if reprobe is not None and reprobe["outcome"] == "cleared":
            # The stored verdict is FALSIFIED. Do not notify. Clear the defer
            # and post a correction naming the stale verdict, so the next
            # reader of that goal does not re-derive the same wrong conclusion
            # (guard-1710: sweep every artifact still asserting the old one).
            cok, cnote = _clear_defer(root_id, (root or {}).get("_source", "world"))
            _post_board(gid, root_id, age,
                        "RE-PROBE FALSIFIED the stored blocker on root %s: %s "
                        "(probed %s). No user notification sent. defer_reason: "
                        "%s. The stored verdict was %s old and was relayed to "
                        "nobody." % (root_id, reprobe["detail"], reprobe["at"],
                                     cnote, _window_str(age)),
                        args.no_board)
            reprobe_suppressed.append({
                "goal_id": gid, "root_id": root_id, "age_hours": round(age, 1),
                "probe": reprobe, "defer_cleared": cok, "detail": cnote})
            continue

        detail = ("Root %s (%s). Route=%s. Threshold %.0fh of %.0fh timeout; "
                  "at timeout the dependency clears fail-open and the goal runs."
                  % (root_id, (root.get("title")[:60] if root else "unknown"),
                     route, threshold, threshold / ESCALATE_AT_FRACTION))
        ok, note = _post_board(gid, root_id, age, detail, args.no_board)
        if not ok:
            failed.append({"goal_id": gid, "detail": note})
            continue
        escalated.append({"goal_id": gid, "age_hours": round(age, 1),
                          "root_id": root_id, "route": route})

        if route == "boost_root":
            bok, bnote = _boost_priority(root_id, root.get("_source", "world"))
            (boosted if bok else failed).append(
                {"goal_id": root_id, "detail": bnote})
        elif route == "notify_user":
            # Transport is the SKILL.md phase's job (forged notification skill);
            # the board post above already recorded the durable cooldown, so a
            # send failure cannot cause a duplicate-escalation storm.
            needs_notify.append({
                "goal_id": gid, "root_id": root_id,
                "age_hours": round(age, 1),
                "subject": "Dependency stale %.0fh: %s waiting on user" % (age, gid),
                "message": (
                    "Goal %s (%s) has been blocked for %.0fh waiting on %s: %s\n\n"
                    "That goal requires user action (participants includes 'user').\n\n"
                    "If not resolved within %.0fh the dependency clears automatically "
                    "(fail-open) and the blocked goal will attempt execution.\n\n"
                    "%s"
                    "The one thing that would help:\n%s"
                    % (gid, c["title"], age, root_id,
                       (root.get("title") if root else "unknown"),
                       max(0.0, threshold / ESCALATE_AT_FRACTION - age),
                       _reprobe_line(reprobe),
                       (root.get("description") or root.get("title") or "")[:400]
                       if root else "")),
                "reprobe": reprobe,
            })

    return {
        "mode": "apply" if args.apply else "dry_run",
        "self_agent": self_agent,
        "threshold_hours": round(threshold, 1),
        "scanned": len(dep_entries),
        "eligible": len(candidates),
        "candidates": candidates,
        "stale_dependency": stale_dependency,
        "prose_only_dependencies": _prose_only_dependency_census(index),
        "escalated": escalated,
        "boosted": boosted,
        "needs_user_notification": needs_notify,
        "reprobe_suppressed": reprobe_suppressed,
        "skipped_cooldown": skipped_cooldown,
        "skipped_below_threshold": skipped_young,
        "skipped_no_blocked_since": skipped_no_ts,
        "failed": failed,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="Post board escalations and boost roots (default: dry-run).")
    p.add_argument("--threshold-hours", type=float, default=None,
                   help="Override escalation threshold (default: 0.75 * "
                        "multi_agent.dependency_timeout_hours).")
    p.add_argument("--agent", default=None, help="Self agent name (default: $MIND_AGENT).")
    p.add_argument("--board-escalation-log", default=None,
                   help="Test-only: JSON list of board posts standing in for the live scan.")
    p.add_argument("--no-board", action="store_true",
                   help="Test-only: skip board-post.sh and pretend it succeeded.")
    args = p.parse_args()
    json.dump(run(args), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
