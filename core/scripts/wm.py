#!/usr/bin/env python3
"""Working memory script — dedicated access layer for <agent>/session/working-memory.yaml.

All shell scripts (wm-*.sh) are thin wrappers around this. Subcommands managed via argparse.

Provides slot-level read/write/append/clear with automatic timestamp tracking via slot_meta,
mid-session pruning, and template initialization/reset.

Slot addressing:
  - Slots live under 'slots:' key: wm.py read active_context → data["slots"]["active_context"]
  - Top-level keys (encoding_queue, session_id, etc.) addressed directly: wm.py read encoding_queue
  - Dot-path subfields: wm.py read active_context.retrieval_manifest → navigates into slot
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import AGENT_DIR, CONFIG_DIR, WORLD_DIR, assert_agent_dir
from _fileops import acquire_lock, release_lock

# : fail loud at import time if MIND_AGENT unset; replaces the
# opaque `None / "session"` TypeError class the next line would otherwise raise.
assert_agent_dir("wm")

def wm_path():
    """Effective working-memory path for this process (Phase 1A Mind/Body routing, ).

    BODY_WM_PATH (injected by bash-agent-inject.py when the bound session has a
    body-manifest) routes WM ops to the per-Body file. Unset -> the agent-wide
    WM at agents/<agent>/session/working-memory.yaml (today's behavior).
    Backward-compatible: with no body-manifest (one Body) the routing collapses
    to the agent-wide default. Resolved per-call (not cached at import) so the
    env the bash hook injects is always honored.
    """
    body = os.environ.get("BODY_WM_PATH", "").strip()
    if body:
        return Path(body)
    return AGENT_DIR / "session" / "working-memory.yaml"


def wm_lock_path():
    """Advisory-lock sibling of the effective WM path (per-Body aware)."""
    return wm_path().with_suffix(".lock")


CONFIG_PATH = CONFIG_DIR / "memory-pipeline.yaml"


# Backward-compat: WM_PATH / WM_LOCK_PATH were module constants for years and
# several importers reference them (compact-restore-slots, precompact-checkpoint,
# goal-selector). PEP 562 module __getattr__ keeps those names working — each
# resolves through the per-Body-aware functions above at access time, so both
# `from wm import WM_PATH` (bound after bash-agent-inject sets the env) and
# `wm.WM_PATH` honor BODY_WM_PATH.
def __getattr__(name):
    if name == "WM_PATH":
        return wm_path()
    if name == "WM_LOCK_PATH":
        return wm_lock_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Cross-writer advisory lock for working-memory.yaml read-modify-write
# cycles. See  / rb-508 / . wm.py's commands and the
# direct-writer paths (e.g. tree-encoding-drift-gate.py) MUST acquire
# this lock around any sequence that reads WM, mutates state in memory,
# then writes back — without it, two writers can race and one overwrites
# the other's update with stale data, and a reader can observe a slot
# mid-rewrite (which is what the productivity-gate "Expecting value..."
# noise pointed at). The lock file is the SAME logical resource as the
# .lock used by `_fileops.locked_modify_yaml`; so wm.py-mediated writes
# and any direct writer that uses locked_modify_yaml will mutually
# exclude as long as both target the same `<file>.lock` path.
import contextlib

@contextlib.contextmanager
def wm_lock():
    """Hold the WM advisory lock across a read-modify-write cycle.

    stale_seconds=10: WM RMW is sub-100ms; 30s default would block 6+ aspiration
    iterations on a crashed mid-RMW writer. 10s is still 100x cycle time.
    """
    lock = wm_lock_path()
    acquire_lock(lock, stale_seconds=10)
    try:
        yield
    finally:
        release_lock(lock)

# Top-level keys (not inside slots:)
TOP_LEVEL_KEYS = {
    "encoding_queue", "session_id", "session_start",
    "goals_completed_this_session", "aspiration_touched_last",
    "last_goal_category",
}

# Session-identity fields: survive `wm reset` (which runs mid-session at
# autocompact via consolidate Step 5), cleared only by `wm clear-identity`
# (which runs post-consolidate from /stop's graceful-stop D4.5). Add a field
# here only when its semantic is "describes the current session" — not
# "holds ephemeral slot state." Wrong classification either loses data
# across autocompact (identity→slot) or leaks stale state across sessions
# (slot→identity).
SESSION_IDENTITY_FIELDS = {"session_start"}

# Default slot types — used by init/reset when config is unavailable
DEFAULT_SLOT_TYPES = [
    "active_constraints", "active_context", "active_hypothesis", "active_strategy",
    "archived_context", "cross_domain_transfer", "domain_data",
    "ephemeral_observation", "knowledge_debt", "known_blockers",
    "micro_hypotheses", "pending_resolutions", "recent_violations",
    "sensory_buffer", "session_goal", "conclusions",
]

# Slots that are arrays (not scalars or maps)
ARRAY_SLOTS = {
    "knowledge_debt", "known_blockers", "micro_hypotheses",
    "recent_violations", "sensory_buffer", "conclusions",
    # spark_capture (): the worker->reducer spark bridge. Membership
    # here is load-bearing for SURVIVAL, not just for clear-to-[] semantics —
    # cmd_maintain's scalar-eviction predicate is `slot_name not in ARRAY_SLOTS
    # and ... slot_val is not None`, and a non-empty list is not None, so an
    # unregistered spark_capture would be nulled at evict_threshold_minutes
    # (120) while its Body waited for the next consolidation.
    "spark_capture",
    # exp_capture (): the worker->reducer EXPERIENCE bridge, sibling of
    # spark_capture and registered for the identical survival reason above. The
    # two are deliberately separate slots rather than one: a spark is a reusable
    # lesson the reducer ENCODES (rb/guardrail/tree), an exp_capture entry is the
    # execution narrative it encodes an experience .md FROM, and merging them
    # would force one consumer to re-classify what the writer already knew.
    # Membership here also buys body-merge's _dedup_append content-hash dedup for
    # free — that helper keys off ARRAY_SLOTS, so no body-merge edit is needed.
    "exp_capture",
    # hyp_capture (): the worker->reducer HYPOTHESIS-EVIDENCE bridge, third
    # sibling, registered for the identical survival reason above. Separate from
    # exp_capture for the same writer-knows-best reason: an exp_capture entry is a
    # narrative the reducer encodes an experience FROM, while a hyp_capture entry is
    # EVIDENCE INPUT to the existing /review-hypotheses resolution protocol, keyed to
    # a real pipeline.jsonl hypothesis_id. It is never a second resolver — the worker
    # supplies evidence, the reducer runs the full protocol, which is also what makes
    # the no-double-resolution guard expressible at all.
    "hyp_capture",
    # encoding_capture (): the worker->reducer TREE bridge, fourth and
    # last sibling, registered for the identical survival reason above. Separate
    # from spark_capture on the learning-routing.md axis, not on a stylistic one:
    # a spark is a LESSON about how to work (the reducer routes it to rb or a
    # guardrail), while this is a DOMAIN FACT about the world the agent operates
    # in (the reducer routes it to a knowledge-tree node). Merging them would
    # force the reducer to re-derive a classification the executing session
    # already knew — the same argument that separates 3.6 and 3.65.
    # EXPECT THIS SLOT TO BE EMPTY MOST UNITS, and do not read that as breakage.
    # Measured over one full worker session (alpha, cc-08, 2026-08-11, 5 units):
    # 0 of 6 spark observations were tree-worthy domain facts — the domain facts
    # that session DID learn (an alarm re-point, a mail-lane bucket scope) flowed
    # through goal records instead. That is the conditional-slot pattern 3.5 and
    # 3.65 already describe, not evidence the lane is dead; but if a later audit
    # finds it still empty across many sessions AND finds tree nodes being
    # encoded from goal records anyway, the honest conclusion is that goal
    # records are the real bridge and this slot should be RETIRED rather than
    # defended (learning-philosophy.md rule 5 — subtraction is learning too).
    "encoding_capture",
}

# The capture lanes proper — the four worker->reducer bridges above, as an
# ordered tuple so every consumer iterates them the same way. ARRAY_SLOTS is a
# set and contains non-capture members (knowledge_debt, sensory_buffer, ...),
# so it is the wrong thing to iterate when the question is "what did the worker
# capture". .
CAPTURE_SLOTS = ("spark_capture", "exp_capture", "hyp_capture", "encoding_capture")

# : FIFO eviction at cap drops the OLDEST entry first, which is exactly
# backwards for a capture the worker marked load-bearing — the longer a Body
# runs, the more certain it is that its most important early finding is the one
# destroyed. Measured on one ACTIVE Body (alpha, cc-08, 2026-08-15, 21 units):
# 237 entries evicted (spark 144, exp 74, hyp 19) against caps of 50/20/10,
# i.e. ~74% of everything spark_capture was ever handed. Second instance of the
#  measurement (215 on cc-07), so this is the rule, not an outlier.
#
# A priority-merge lane whose entries are gone before the lane runs is
# decorative, which is why the flag has to bite HERE and not only at merge time.
# Flagged entries sort LAST and are therefore popped LAST: unflagged entries
# absorb the whole cap pressure first. When a lane is ALL load-bearing the cap
# still holds and the oldest flagged entry goes — a cap that could be defeated
# by a field the writer controls is not a cap.
#
# MIRROR: wm_write.py::append_slot carries a byte-identical key. The DAEMON copy
# is the live one (wrappers are daemon-only), so a wm.py-only edit changes
# nothing at runtime — the  bug class.
def _eviction_sort_key(x):
    if not isinstance(x, dict):
        return (0, "0000")
    return (1 if x.get("load_bearing") else 0, x.get("_item_ts", "0000"))


def _is_flagged(x) -> bool:
    """The flag half of _eviction_sort_key, as a predicate.

    Non-dicts sort as UNFLAGGED in the key above, so they must count as unflagged
    here too — if the two disagreed about where the flagged block starts, the
    floor below would index into the wrong entry.
    """
    return isinstance(x, dict) and bool(x.get("load_bearing"))


# Share of a capped lane held open for UNFLAGGED entries. 0.2 is the value
#  specified; it is deliberately a constant and not config, because the
# daemon carries a mirror of this policy (see MIRROR note above) and a config key
# read by only one of the two copies is a worse failure than a tuned number.
UNFLAGGED_FLOOR_RATIO = 0.2


def _unflagged_floor(limit: int) -> int:
    """How many slots of `limit` are reserved for UNFLAGGED entries.

    Zero below limit=2: a lane that holds one item cannot reserve a share of
    itself without inverting the priority key outright. Capped at `limit - 1` so
    the reservation can never starve flagged entries completely — the floor is a
    guarantee that the lane keeps *hearing* unflagged content, not a demotion of
    load_bearing.
    """
    if not limit or limit < 2:
        return 0
    return min(limit - 1, max(1, int(limit * UNFLAGGED_FLOOR_RATIO)))


def enforce_slot_limit(arr, limit, item=None) -> int:
    """Evict from `arr` IN PLACE until it fits `limit`; return how many were
    dropped. Zero when `limit` is falsy or the list already fits.

    `item`, when given, is the entry the CALLER just added and must survive
    this call (g-306-308 / g-115-6541): an unflagged newcomer sorts to index 0
    behind a slot full of load_bearing peers and would otherwise be popped by
    the very write that created it. When the head of the sorted list IS that
    entry (identity, not equality) the victim is the next-oldest peer instead.
    The merge path (body-merge.py) passes nothing — every entry there is a
    peer, so the plain oldest-first policy is right.

    A RESERVED FLOOR (`_unflagged_floor`) keeps `limit * 0.2` slots open for
    unflagged entries, and it applies on BOTH paths including the merge one,
    where it is the thing that stops a reducer's generalize-down from discarding
    a worker Body's routine observations wholesale. Above the floor the priority
    key is untouched: unflagged peers are still evicted before flagged ones.
    Below it the order inverts on purpose and the oldest FLAGGED entry goes
    instead — the honest cost, since a lane at 100% flagged has no variance left
    in the key and has stopped triaging anything at all.

    SINGLE SOURCE OF TRUTH for the eviction POLICY, and it exists because there
    are two entry points and only one of them ever enforced anything.
    `cmd_append` is the WRITE path; `body-merge.py::merge_wm` is the
    GENERALIZE-DOWN path, and generalize-down is how a worker Body's capture
    entries actually reach a reducer's WM. Measured 2026-08-17 (g-306-309):
    body-merge.py contained ZERO references to array_limits,
    _eviction_sort_key or limit, so every cap in memory-pipeline.yaml was
    unapplied to the traffic that fills these lanes — spark_capture 69/50 and
    exp_capture 40/20 on the reducer. `git log -S` over that file finds no
    commit that ever added OR removed those tokens, so this was never a
    regression: the merge path simply never enforced.

    Note the two paths fail in OPPOSITE directions, which is why this helper
    fixes only half the problem by itself. Same day, same fleet: the reducer
    (cc-04) sat OVER cap because merge never evicts, while a worker Body
    (cc-08) sat exactly AT cap — spark 50/50, exp 20/20, both 100%
    load_bearing — where an unflagged append is sorted to index 0 and popped
    by the write that created it (g-306-308 / g-115-6541). Restoring
    enforcement here makes that selection defect start to bite on this path
    too; land them together or land this one knowing it goes live.

    Kept deliberately free of config, slot names and counters so the two
    callers can differ on all three: the caller decides WHICH slots are capped
    and where the eviction tally is recorded.
    """
    if not limit or len(arr) <= limit:
        return 0
    arr.sort(key=_eviction_sort_key)
    floor = _unflagged_floor(limit)
    # Sorted (flag, ts) => unflagged occupy the PREFIX, so this count is ALSO the
    # index of the oldest FLAGGED entry. Maintained incrementally in the loop
    # rather than recomputed, so eviction stays O(n) rather than O(n^2).
    n_unflagged = sum(1 for x in arr if not _is_flagged(x))
    evicted = 0
    while len(arr) > limit:
        # TWO protections, and they are scoped to different UNITS (guard-4236).
        # The floor is PER-WINDOW: it is a property of the LANE, so it still
        # holds on the NEXT call. The `item` guard below is PER-CALL: it protects
        # this newcomer only while this call runs. The per-call guard alone is
        # what made N consecutive unflagged appends into a saturated lane keep
        # exactly ONE — every previous newcomer became an unprotected peer that
        # sorted first and was popped, a 90% loss at N=10 that no single-call
        # test could see ( measured it;  is this fix).
        _victim = n_unflagged if (len(arr) - n_unflagged) > limit - floor else 0
        if item is not None and arr[_victim] is item and len(arr) > 1:
            _victim = _victim + 1 if _victim + 1 < len(arr) else _victim - 1
        if _victim < n_unflagged:
            n_unflagged -= 1
        arr.pop(_victim)
        evicted += 1
    return evicted


# Slots that are maps with specific structure (not scalars)
MAP_SLOTS = {
    "active_context": {"summary": None, "experience_refs": [], "retrieval_manifest": None},
    "archived_context": {"summary": None, "experience_refs": []},
}

# Structured-dict slots: top-level writes must be a dict or None (clear).
# Non-JSON stdin that falls through cmd_set's int/float scalar fallbacks
# (e.g. a Python traceback piped via `echo "$(py -3 ... 2>&1)"`) would
# otherwise land as a raw string in the slot, breaking downstream consumers
# (loop-state-bump-counters.py, productivity-gate.sh, compact-restore-slots.sh).
#  traced the exact pattern;  is the structural refusal.
STRUCTURED_DICT_SLOTS = {"loop_state"}

# Cadence-tracker slot patterns — stale by design (fire every N goals or N hours,
# often much longer than evict_threshold_minutes). wm-prune must not evict these
# or the cadence memory is destroyed and the next cadence-check duplicate-fires.
# Discovered  (2026-04-21): wm-prune evicted last_fresh_eyes_review at
# 132 min age; cadence-check then read last=0 and would have fired a duplicate
# briefing.
#
# MATCH THE CLASS, NOT A LIST OF VERBS (, 2026-08-18). This was eight
# patterns of the shape `^last_.*_<verb>$`, enumerating the suffixes that
# happened to exist when  was written — so protection depended on which
# verb a slot's author chose, and every stamp named with an unenumerated verb was
# evicted at evict_threshold_minutes. Measured live on this box: of 17
# cadence/dispatch stamps, NINE were unprotected and three were nulled 8 minutes
# before the measurement — `last_curriculum_eval` (verb not in the list),
# `last_completed_not_closed_triage` (same), and `fresh_eyes_last_dispatch`
# (whose name does not START with `last_`, so no `^last_` pattern could ever
# reach it). `^last_.*_fire$` was written for a name shape that does not exist:
# the real slot is `fresh_eyes_last_fire`.
#
# The sharpest casualty was the `*_last_dispatch` family — all six of the
# consumption-aware stamps that stale-sentinel-canary uses as its ONLY
# discriminator between "consumer kept up" and "consumer bypassed the gate."
# Nulling them every 120 min defeats that protection on a 2-hour cycle and
# false-fires "stale sentinel set for N iterations" Investigates.
#
# The class is: a slot recording WHEN SOMETHING LAST HAPPENED. Its name either
# begins `last_` or carries `_last_` before the event name. Both anchored (a
# prefix and a delimited infix) — deliberately not a bare `last` substring, which
# would match unrelated slots. Measured across all 75 live slots: 10 newly
# protected (every one a genuine stamp), 0 protection lost, 0 unrelated slots
# swept in. Adding a stamp no longer requires editing this tuple, which is what
# let the population drift out from under it.
#
# Over-protecting a scalar costs a stale value lingering; under-protecting one
# destroys cadence memory and re-fires expensive rituals. guard-362 requires
# class-(b) entities be allowlisted, and this direction is the safe one.
#
# EVERY PATTERN HERE MUST BE ^-ANCHORED. `_is_cadence_tracker` applies
# `p.match()`, which anchors at position 0 regardless of the pattern — so a bare
# infix `_last_` compiles fine, reads correctly, and matches NOTHING. All eight
# original patterns opened with `^`, so the constraint was satisfied by accident
# and never stated. Caught here only because the measurement above was written
# with `.search()` while production uses `.match()`; write the infix as
# `^.*_last_` so the two agree.
CADENCE_TRACKER_PATTERNS = (
    re.compile(r"^last_"),
    re.compile(r"^.*_last_"),
)

# Slots that survive cmd_reset BY NAME: their writer and reader sit on opposite
# sides of the aspirations-consolidate Step-5 wm-reset boundary (Step 0.65
# writes journal_cluster_summaries pre-reset; Step 9 consumes it one-shot
# post-reset for handoff key_outcomes, then clears it). Content payloads, not
# timestamps — CADENCE_TRACKER_PATTERNS cannot cover them. Staleness
# self-heals: the next consolidation's Step 0.65 overwrites the slot, and
# cmd_maintain's evict path cleans a crashed-consolidation leftover.
# MIRRORED in mind_api/src/endpoints/wm_write.py (the LIVE runtime path —
# wm-reset.sh routes to POST /v1/wm/reset, not to cmd_reset). Keep both in
# sync; parity asserted by test_wm_reset_cadence.py. ()
#
# spark_capture () is the second member, for the same reason and a
# tighter window: body-merge generalize-down delivers a worker's captured spark
# observations into the reducer WM at aspirations-consolidate Step -1, and
# wm-reset runs at Step 5 of the SAME consolidation. Without this exemption the
# transport would land the payload and wipe it ~5 steps later, before any
# aspirations-spark Phase 6.5 could consume it — the merge would report success
# and the learning would still be lost. Cleared by its consumer (Phase 6.5), not
# by reset.
#
# exp_capture () is the third member and inherits that reasoning
# unchanged: it rides the SAME body-merge transport into the reducer WM at
# Step -1, and its consumer (the retrospective, which calls the existing
# experience writers over the entries) also runs after the Step-5 reset. Same
# transport + same boundary = same exemption. Omitting it would make the merge
# report success while the narratives were wiped ~5 steps later, which is the
#  failure verbatim, and silent for the same reason.
# hyp_capture () is the fourth member, same transport and same boundary:
# it rides body-merge into the reducer WM at Step -1, and its consumer is the
# /review-hypotheses resolution protocol, which runs well after the Step-5 reset.
# The failure mode of omitting it is worse here than for its siblings, because the
# evidence is keyed to a specific hypothesis_id: a wiped entry does not merely lose
# a narrative, it lets the reducer resolve that hypothesis WITHOUT the worker's
# evidence while every signal reports success — a silently under-informed
# resolution rather than an obviously missing one.
#
# encoding_capture () is the fifth member and was MISSING here for its
# whole life until  (2026-08-12) — it rides the identical body-merge
# transport into the reducer WM at Step -1, and its INTENDED consumer (tree
# encoding at aspirations-state-update Step 8) would run after the Step-5 reset,
# so it needed this exemption from the day it shipped. Three siblings each got a
# paragraph above when they were added; the fourth lane got ARRAY_SLOTS
# registration in both files and nothing here, so a reset destroyed it while its
# siblings survived.
#
# THAT CONSUMER DOES NOT EXIST — measured 2026-08-15 (alpha worker, cc-07), and
# the sentence above said "its consumer ... runs" in both this file and the
# wm_write.py twin until then. `encoding_capture` appears 0 times in
# aspirations-state-update/SKILL.md (71,534 bytes; positive control in that same
# file: encoding_queue 2, wm-read 6), and nothing bridges it to encoding_queue,
# which state-update both writes and drains ITSELF for coordination-deferred
# encodings. Full census outside agents/ is producer + registration + tests only:
# wm.py 4, worker-loop SKILL.md 4, wm_write.py 3, tests 3. All three siblings do
# have real drains (aspirations-spark Phase 6.5; worker_retrospective.py
# RUN_LANES "experience" -> _lane_experience; /review-hypotheses +
# hyp_capture_guard.py). This lane has none, so entries ride the transport into
# the reducer WM correctly and are then read by nothing. Building the consumer is
#  (HIGH, pending, unclaimed since 2026-08-06), which is HALF-shipped:
# its worker/producer half landed, its reducer half did not.
#
# DO NOT apply the L167-176 "expect it empty / consider RETIRING it" branch on
# the strength of that: measured the same day, alpha's agent-wide WM held 132
# encoding_capture entries (spark 162, exp 72, hyp 28) — the second-largest
# capture lane, not an empty one. The retirement branch is conditioned on the
# lane being unused AND tree nodes flowing from goal records instead; the first
# conjunct is false. This is a live, heavily-written, unread lane.
#
# WHY THE OMISSION WAS INVISIBLE, which is the part worth carrying: the parity
# test (test_wm_reset_cadence.py) pins the CLI<->daemon mirror, so both copies
# agreed — on the same wrong set. And its survive-assertion exercises exactly one
# representative member (journal_cluster_summaries), so it passes for ANY subset
# of the capture family. Two green checks, neither of which could see a missing
# lane. The survive test now iterates all four capture lanes by name for that
# reason; a fifth lane must be added there too, or it inherits this same silence.
# capture_consumed_hashes () is the durable consumed-watermark for the
# four capture lanes. It MUST survive the reset for the same reason the lanes do,
# and for one more: the watermark exists precisely because the live slot's dedup
# basis is destroyed by consumption, so a watermark that is itself wiped restores
# the original bug while looking fixed. Written by capture_fast_lane._merge_flagged
# at MERGE time; read back as _dedup_append(extra_seen=...).
RESET_SURVIVING_SLOTS = {"journal_cluster_summaries", "spark_capture", "exp_capture",
                         "hyp_capture", "encoding_capture", "capture_consumed_hashes"}

def _is_cadence_tracker(slot_name):
    """True if slot name matches a cadence-tracker pattern — do not evict.

    TOP_LEVEL_KEYS are excluded FIRST (g-115-6697). They hold session VALUES,
    not cadence bookkeeping, so they are stale-from-neglect (guard-362 class a)
    and must stay evictable. Exactly one of them — `last_goal_category`, which
    holds a category string, not a timestamp — matches the `^last_` class
    pattern, and it is the deliberate negative control in
    `test-wm-prune-cadence-protection.sh` ("ensuring eviction still works").
    Reusing the already-declared TOP_LEVEL_KEYS set keeps this a class rule:
    an exception ENUMERATION here would be the mirror of the suffix-verb
    enumeration this fix removed. Verified: no cadence stamp is a TOP_LEVEL_KEY.

    KEEP THIS BODY IDENTICAL TO THE DAEMON MIRROR in
    mind_api/src/endpoints/wm_write.py. The parity test compares the PATTERNS
    tuple only, so a divergence in this FUNCTION is not currently caught.
    """
    if slot_name in TOP_LEVEL_KEYS:
        return False
    return any(p.match(slot_name) for p in CADENCE_TRACKER_PATTERNS)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    """Local ISO timestamp."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def read_yaml(path):
    """Read a YAML file, return parsed dict. Returns {} if missing.

    Detective layer (g-001-44): a non-empty file whose bytes are all 0x00 is
    the NTFS metadata-journaled-but-data-not-flushed crash signature — return
    empty with a stderr WARN instead of feeding null bytes to the YAML parser.
    """
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if raw and not raw.strip(b"\x00"):
        print(f"[wm] WARN: {path} is all-null content ({len(raw)} bytes) — "
              f"post-crash corruption signature (g-001-44); treating as empty. "
              f"Rebuild via wm-init.sh if slots are expected.", file=sys.stderr)
        return {}
    data = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    return data if data is not None else {}

def write_yaml(path, data):
    """Atomically write data as YAML (fsync before rename — )."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)

def read_wm():
    """Read working memory file."""
    return read_yaml(wm_path())

def write_wm(data):
    """Write working memory file atomically."""
    write_yaml(wm_path(), data)

def read_config():
    """Read memory pipeline config."""
    return read_yaml(CONFIG_PATH)


def _default_wm_data():
    """Build a fresh working-memory dict from the memory-pipeline config.

    Single source of truth for the canonical empty-WM shape — consumed by
    cmd_init, cmd_reset, and cmd_set (g-115-748 self-heal path). Extracting
    this helper closes the cmd_read/cmd_set asymmetry from g-115-737: a
    missing or empty working-memory.yaml previously caused cmd_set to exit 1
    with "Working memory not initialized" while cmd_read returned {} exit 0.
    The asymmetry made fresh-eyes-cadence-check seed-stagger (g-270-02)
    fail-open and fire all 4 sibling rituals on the same iteration.
    Single-writer rule: any future change to the empty-WM shape edits this
    helper (and its consumers automatically inherit it).
    """
    config = read_config()
    wm_config = config.get("working_memory", {})
    slot_types = wm_config.get("slot_types", DEFAULT_SLOT_TYPES)

    slots = {}
    slot_meta = {}
    for st in slot_types:
        if st in ARRAY_SLOTS:
            slots[st] = []
        elif st in MAP_SLOTS:
            slots[st] = dict(MAP_SLOTS[st])  # shallow copy
        else:
            slots[st] = None
        slot_meta[st] = {"updated_at": None, "accessed_at": None, "update_count": 0}

    return {
        "encoding_queue": [],
        "session_id": None,
        "session_start": None,
        "goals_completed_this_session": [],
        "aspiration_touched_last": "",
        "last_goal_category": "",
        "slots": slots,
        "slot_meta": slot_meta,
    }

def resolve_slot(data, slot_path):
    """Resolve a slot path to (parent_dict, final_key, is_top_level).

    'known_blockers' → data["slots"]["known_blockers"]
    'encoding_queue' → data["encoding_queue"]
    'active_context.retrieval_manifest' → data["slots"]["active_context"]["retrieval_manifest"]
    """
    parts = slot_path.split(".")
    root_key = parts[0]

    if root_key in TOP_LEVEL_KEYS:
        # Top-level key
        current = data
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None, None, True
        return current, parts[-1], True
    else:
        # Slot key — lives under slots:
        slots = data.get("slots", {})
        if len(parts) == 1:
            return slots, root_key, False
        # Navigate deeper
        current = slots
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, dict):
                current[part] = {}
                current = current[part]
            else:
                return None, None, False
        return current, parts[-1], False

def get_slot_meta(data, slot_name):
    """Get or create slot_meta entry for a slot."""
    meta = data.setdefault("slot_meta", {})
    root = slot_name.split(".")[0]
    if root not in meta:
        meta[root] = {"updated_at": None, "accessed_at": None, "update_count": 0}
    return meta[root]

def update_accessed(data, slot_name):
    """Mark a slot as accessed."""
    m = get_slot_meta(data, slot_name)
    m["accessed_at"] = now_iso()

def update_modified(data, slot_name):
    """Mark a slot as modified."""
    m = get_slot_meta(data, slot_name)
    m["updated_at"] = now_iso()
    m["update_count"] = m.get("update_count", 0) + 1

def get_pruning_config(config):
    """Get pruning configuration with defaults."""
    defaults = {
        "stale_threshold_minutes": 30,
        "evict_threshold_minutes": 120,
        "array_limits": {
            "encoding_queue": 20,
            "sensory_buffer": 20,
            "micro_hypotheses": 30,
            "knowledge_debt": 15,
            "known_blockers": 10,
            "recent_violations": 5,
        },
        "item_stale_minutes": {
            "micro_hypotheses": 180,
            "sensory_buffer": 60,
            "ephemeral_observation": 60,
        },
        "protected_slots": ["known_blockers", "knowledge_debt"],
    }
    return config.get("working_memory_pruning", defaults)

# ---------------------------------------------------------------------------
# Schema gates
# ---------------------------------------------------------------------------

def _validate_knowledge_debt_entry(item):
    """Reject knowledge_debt entries with unresolvable node_keys.

    Valid forms:
      A) node_key resolves to a real _tree.yaml entry (string key)
      B) priority == "housekeeping" (debt is framework-scoped, not node-scoped)
      C) node_key is explicitly null AND reason is present

    See rb-248 and g-115-59. Rejects loudly rather than silently tagging —
    silent coercion would violate rb-215 single-source-of-truth.
    """
    priority = item.get("priority")
    node_key = item.get("node_key")
    reason = item.get("reason")

    if priority == "housekeeping":
        if not reason:
            print("ERROR: knowledge_debt housekeeping entry requires 'reason'", file=sys.stderr)
            sys.exit(1)
        return  # valid — housekeeping form

    if node_key is None:
        if not reason:
            print("ERROR: knowledge_debt entry with node_key=null requires 'reason'", file=sys.stderr)
            sys.exit(1)
        return  # valid — explicit null with reason

    if not isinstance(node_key, str) or not node_key.strip():
        print(f"ERROR: knowledge_debt node_key must be non-empty string or null, got {node_key!r}", file=sys.stderr)
        sys.exit(1)

    # Must resolve against _tree.yaml
    tree_path = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
    try:
        with open(tree_path, encoding="utf-8") as f:
            tree = yaml.safe_load(f) or {}
    except OSError as e:
        print(f"ERROR: cannot read {tree_path} for knowledge_debt validation: {e}", file=sys.stderr)
        sys.exit(1)

    nodes = tree.get("nodes", {})
    if node_key not in nodes:
        print(
            f"ERROR: knowledge_debt node_key '{node_key}' does not resolve to a tree node.\n"
            f"       Valid forms: (A) real node_key from _tree.yaml, (B) priority='housekeeping' + reason,\n"
            f"       (C) node_key=null + reason. See core/config/conventions/working-memory.md.",
            file=sys.stderr,
        )
        sys.exit(1)

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_set(args):
    """Set a slot value from stdin (JSON).

    Reads stdin OUTSIDE the WM lock (no I/O on WM yet), then acquires the
    advisory lock for the read-modify-write cycle on working-memory.yaml.
    g-115-206 — closes the race that surfaced as productivity-gate noise.

    rb-715 subdict-clobber gate (g-275-02): when slot == 'loop_state' and the
    incoming value is a dict, run loop_state_merge_gate.check() against the
    on-disk loop_state.signals subkeys. If incoming.signals would clobber
    bash-written subkeys (quiescence, goals_since_last_tree_update, etc.),
    refuse the write unless --override-merge-gate <justification> is passed.
    """
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    # Parse value — try JSON, fall back to scalar
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Treat as scalar string
        if raw == "null":
            value = None
        elif raw == "true":
            value = True
        elif raw == "false":
            value = False
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw

    # : refuse non-dict-or-null writes to structured-dict slots.
    # Without this, a Python traceback piped via `echo "$(py -3 ... 2>&1)"`
    # falls through the scalar fallbacks above and lands as a 700+ char
    # string in loop_state, breaking productivity-gate / bump-counters /
    # compact-restore. The merge-gate at line 410 catches subdict-clobber
    # (rb-715) but pass-throughs non-dict, so this writer-side check is
    # the only structural defense against type-transition.
    if args.slot in STRUCTURED_DICT_SLOTS and value is not None and not isinstance(value, dict):
        shape = type(value).__name__
        preview = (raw[:80] + "...") if len(raw) > 80 else raw
        print(
            f"BLOCKED: structured-dict slot '{args.slot}' refuses non-dict-or-null "
            f"write (got {shape}, len={len(raw)}, prefix={preview!r})",
            file=sys.stderr,
        )
        sys.exit(1)

    with wm_lock():
        data = read_wm()
        if not data:
            #  self-heal: a missing or empty working-memory.yaml is
            # not a writer's failure mode — it is a fresh agent dir, a wiped
            # session, or a runner that called wm-set BEFORE wm-init had a
            # chance to fire. Seed the canonical empty-WM shape and proceed
            # with the requested set. The cmd_read counterpart already
            # returns {} exit 0 on empty WM; cmd_set previously diverged
            # by exit 1 (rb-748 / ), which made
            # fresh-eyes-cadence-check seed-stagger () fail-open
            # and fire all 4 sibling rituals simultaneously on a fresh dir.
            # cmd_append (~line 473) keeps the exit-1 behavior — array
            # writes against a missing WM are ambiguous (append-to-what?)
            # and the goal description scopes the self-heal to cmd_set only.
            data = _default_wm_data()

        parent, key, is_top = resolve_slot(data, args.slot)
        if parent is None:
            print(f"ERROR: Cannot resolve path '{args.slot}'", file=sys.stderr)
            sys.exit(1)

        # rb-715 subdict-clobber gate: only fires for top-level loop_state writes.
        # Subfield writes (e.g. loop_state.signals.quiescence) are bash-side
        # surgical updates by design — those are exactly what the gate protects.
        if args.slot == "loop_state":
            on_disk_loop_state = parent.get(key)
            override = getattr(args, "override_merge_gate", None)
            try:
                # Import via importlib because the module filename uses
                # hyphens (kebab-case is the project convention for *.py
                # under core/scripts/), which prevents `import loop_state_merge_gate`.
                import importlib.util
                gate_path = Path(__file__).resolve().parent / "loop-state-merge-gate.py"
                spec = importlib.util.spec_from_file_location("loop_state_merge_gate", gate_path)
                gate_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(gate_mod)
                gate_result = gate_mod.check(value, on_disk_loop_state, override=override)
            except (ImportError, OSError, AttributeError) as e:
                # Fail-open on gate-load failure (don't block the loop on a broken gate)
                print(f"[loop-state-merge-gate] WARN: gate load failed ({e}) — fail-open", file=sys.stderr)
                gate_result = {"would_block": False, "reason": "gate load failed", "missing_subkeys": []}

            if gate_result.get("would_block"):
                print(f"BLOCKED: {gate_result['reason']}", file=sys.stderr)
                sys.exit(1)
            if gate_result.get("override_applied"):
                # Echo override to stderr for audit trail
                print(f"[loop-state-merge-gate] {gate_result['reason']}", file=sys.stderr)
            # : monotonic-counter preservation. When the gate floored
            # stale-lower top-level counters (goals_completed/productive_goals)
            # or unioned a subset counted_goals_this_session against the on-disk
            # committed state, write the PRESERVED value, not the raw incoming —
            # the Mechanism C backstop for the non-CAS collateral writers.
            if gate_result.get("counters_preserved"):
                value = gate_result.get("preserved_value", value)
                print(
                    f"[loop-state-merge-gate] preserved monotonic counters "
                    f"(g-115-1418): {gate_result.get('preserved_counters')}",
                    file=sys.stderr,
                )

        parent[key] = value

        if not is_top:
            update_modified(data, args.slot)

        write_wm(data)

def cmd_append(args):
    """Append an item to an array slot from stdin (JSON).

    Read-modify-write protected by wm_lock — see g-115-206.
    """
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    item = json.loads(raw)

    # Auto-add _item_ts
    if isinstance(item, dict):
        item["_item_ts"] = now_iso()

    # knowledge_debt schema gate (, rb-248): node_key must either resolve
    # to a real tree node OR the entry must tag itself as housekeeping. Brittle
    # placeholders like "multiple" or "tree_maintenance" produce false positives
    # in debt-closure matchers that use simple string containment.
    root_slot_for_validation = args.slot.split(".")[0]
    if root_slot_for_validation == "knowledge_debt" and isinstance(item, dict):
        _validate_knowledge_debt_entry(item)

    # : initialized OUTSIDE the lock so the post-lock push below can
    # read it on every path — the same NameError trap the _evicted counter
    # documents in the daemon twin.
    _carrier_path = None

    with wm_lock():
        data = read_wm()
        if not data:
            print("ERROR: Working memory not initialized. Run wm-init.sh first.", file=sys.stderr)
            sys.exit(1)

        parent, key, is_top = resolve_slot(data, args.slot)
        if parent is None:
            print(f"ERROR: Cannot resolve path '{args.slot}'", file=sys.stderr)
            sys.exit(1)

        arr = parent.get(key)
        if arr is None:
            parent[key] = []
            arr = parent[key]
        # SELF-HEAL the int-in-LIST-slot collision — TWIN of the daemon
        # mind_api/src/endpoints/wm_write.py append (the LIVE path, guard-742);
        # keep in sync. The TOP-LEVEL goals_completed_this_session is a LIST of
        # hand-off rows; an int there is the loop_state counter's NAME leaking
        # into the wrong key (2026-08-16, worker-loop Phase 4b outage — 3 of 3
        # forked Bodies checked). An int carries no rows, so it is always
        # corruption: heal to [] loudly and continue. Scoped to this one slot.
        _healed_int = None
        if (is_top and key == "goals_completed_this_session"
                and isinstance(arr, (int, float)) and not isinstance(arr, bool)):
            _healed_int = arr
            parent[key] = []
            arr = parent[key]
            print(f"WARN: 'goals_completed_this_session' was {type(_healed_int).__name__}:"
                  f"{_healed_int} (loop_state counter name collided with the top-level "
                  f"hand-off LIST); reset to [] before appending — find the writer",
                  file=sys.stderr)
        if not isinstance(arr, list):
            print(f"ERROR: '{args.slot}' is {type(arr).__name__}, not a list", file=sys.stderr)
            sys.exit(1)

        # knowledge_debt UPSERT-BY-node_key (, 2026-08-06). The reflect
        # tree-lint (reflect/SKILL.md, "stale-high-retrieval") re-flags the TOP 5
        # nodes by retrieval_count on every maintain pass, and that set is stable
        # by construction — a high-retrieval node stays high-retrieval. Appending
        # blind made the slot re-record the same nodes each scan: measured 10
        # entries for 5 distinct node_keys across 3 scans, against a limit of 15.
        # At saturation the FIFO eviction below drops the OLDEST entries, which
        # under that duplication are re-recordings of nodes still present — so
        # the slot can never hold more than ~5 distinct nodes, and genuine debt
        # from the OTHER writers is evicted within ~3 scans. That silent loss is
        # exactly what  ("prevents data loss") exists to stop.
        #
        # Enforced HERE, not as a "skip if already present" line in the skill
        # pseudocode: rb-121 — LLM template instructions are insufficient for
        # structural constraints, they need code-level enforcement. This sits
        # beside the existing _validate_knowledge_debt_entry gate ( /
        # rb-248), the precedent for slot-specific enforcement on this store.
        #
        # Upsert, not skip: the newest scan carries fresher retrieval_count /
        # days_since_update, so replacing in place keeps the better measurement.
        # Position is preserved so the entry keeps its original _item_ts ordering
        # for eviction — a node that keeps re-flagging must not indefinitely
        # renew its own priority over older, never-serviced debt.
        # SELF-HEALING (fresh-eyes-code, same day): collapse ALL matches, not
        # just the first. The first version replaced the first match and broke,
        # which prevents NEW duplicates but never converges a slot that ALREADY
        # holds them — it refreshes one twin and leaves the other pinning an
        # eviction slot forever. My own slot only converged because I cleaned it
        # by hand; any slot still duplicated when this ships would stay that way
        # permanently. Collapsing on write makes the first append after the fix
        # heal the slot, with no migration step.
        _upserted = False
        if root_slot_for_validation == "knowledge_debt" and isinstance(item, dict):
            _nk = item.get("node_key")
            if _nk:
                _matches = [_i for _i, _e in enumerate(arr)
                            if isinstance(_e, dict) and _e.get("node_key") == _nk]
                if _matches:
                    # Keep the OLDEST match's _item_ts: a node that keeps
                    # re-flagging must not renew its own eviction priority over
                    # older, never-serviced debt.
                    _oldest = min(
                        (arr[_i].get("_item_ts") for _i in _matches
                         if arr[_i].get("_item_ts")),
                        default=item.get("_item_ts"),
                    )
                    item["_item_ts"] = _oldest
                    arr[_matches[0]] = item
                    for _i in reversed(_matches[1:]):
                        arr.pop(_i)
                    _upserted = True
        if not _upserted:
            arr.append(item)

        # Enforce array limits
        config = read_config()
        pruning = get_pruning_config(config)
        limits = pruning.get("array_limits", {})
        root_slot = args.slot.split(".")[0]
        limit = limits.get(root_slot)
        # Remove oldest items (those without _item_ts first, then by _item_ts),
        # but EVICT UNFLAGGED BEFORE LOAD-BEARING (). Mirror of the
        # daemon key in wm_write.py::append_slot — keep the two identical.
        # The sort+pop itself now lives in enforce_slot_limit () so the
        # merge path can apply the SAME policy instead of growing a second copy.
        # : `item=` is the entry THIS call just added; the helper never
        # picks it as its own eviction victim -- see the TWIN in
        # wm_write.py::append_slot for the measurement and the full rationale.
        # That daemon copy is the LIVE path (wrappers are daemon-only), so an
        # edit here alone would be inert at runtime; both halves must move
        # together (guard-742/547, the  bug class).
        _evicted = enforce_slot_limit(arr, limit, item=item)
        if _evicted:
            # : mirror of the daemon counter in wm_write.py::append_slot.
            # The DAEMON copy is the live one (wrappers are daemon-only), so this
            # exists for parity — pinned by the shared-constants parity test, which
            # is what makes forgetting one half loud instead of silent.
            # TOP-LEVEL, not slot_meta: body-merge merges slot_meta reducer-wins, so
            # a counter there is dropped at generalize-down.
            _ev = data.get("capture_evictions")
            if not isinstance(_ev, dict):
                _ev = {}
                data["capture_evictions"] = _ev
            _prev = _ev.get(root_slot)
            _ev[root_slot] = (_prev if isinstance(_prev, int) else 0) + _evicted

        if not is_top:
            update_modified(data, args.slot)

        write_wm(data)
        # : mirror a LOAD-BEARING capture into this Body's
        # session/-rooted carrier so capture_fast_lane can reach it from ANOTHER
        # box (sessions/ is sync-excluded and machine-local, so the lane could
        # otherwise only ever see same-box Bodies). Local append INSIDE the lock
        # so carrier order matches WM order. TWIN of the daemon copy in
        # wm_write.py::append_slot — the DAEMON one is the LIVE path
        # (wrappers are daemon-only); this exists for parity, same as the
        # eviction counter above.
        if (root_slot_for_validation in CAPTURE_SLOTS
                and isinstance(item, dict) and item.get("load_bearing")):
            try:
                import body_capture_carrier as _bcc
                _carrier_path = _bcc.record_local(
                    wm_path(), root_slot_for_validation, item)
            except Exception:  # noqa: BLE001 — never fail a WM append
                _carrier_path = None
        if _evicted:
            _out = {"ok": True, "slot": args.slot, "evicted": _evicted}
            if _healed_int is not None:
                _out["healed_from"] = f"{type(_healed_int).__name__}:{_healed_int}"
            print(json.dumps(_out),
                  file=sys.stderr)

    # Push OUTSIDE the lock: this is a network round trip and wm_lock is
    # stale-bounded, so holding it here would let a slow store look like a
    # crashed writer. A failed push self-heals — the next append re-pushes the
    # WHOLE carrier file, not a delta.
    if _carrier_path is not None:
        try:
            import body_capture_carrier as _bcc
            _bcc.push(_carrier_path)
        except Exception:  # noqa: BLE001
            pass

def cmd_clear(args):
    """Clear (null out) a slot. RMW protected by wm_lock — see ."""
    with wm_lock():
        data = read_wm()
        if not data:
            print("ERROR: Working memory not initialized.", file=sys.stderr)
            sys.exit(1)

        parent, key, is_top = resolve_slot(data, args.slot)
        if parent is None:
            print(f"ERROR: Cannot resolve path '{args.slot}'", file=sys.stderr)
            sys.exit(1)

        # Clear to [] if currently a list, None otherwise — handles both
        # slot arrays (known_blockers) and top-level arrays (encoding_queue)
        current_val = parent.get(key) if isinstance(parent, dict) else None
        root_slot = args.slot.split(".")[0]
        if isinstance(current_val, list) or root_slot in ARRAY_SLOTS:
            parent[key] = []
        else:
            parent[key] = None

        if not is_top:
            update_modified(data, args.slot)

        write_wm(data)

def cmd_ages(args):
    """Report slot ages (minutes since last update/access)."""
    data = read_wm()
    if not data:
        if args.json:
            print("{}")
        else:
            print("Working memory not initialized.")
        return

    now = datetime.now()
    meta = data.get("slot_meta", {})
    slots = data.get("slots", {})
    result = {}

    for slot_name in slots:
        m = meta.get(slot_name, {})
        updated = m.get("updated_at")
        accessed = m.get("accessed_at")
        update_count = m.get("update_count", 0)

        mins_since_update = None
        mins_since_access = None
        if updated:
            try:
                dt = datetime.fromisoformat(updated)
                mins_since_update = int((now - dt).total_seconds() / 60)
            except (ValueError, TypeError):
                pass
        if accessed:
            try:
                dt = datetime.fromisoformat(accessed)
                mins_since_access = int((now - dt).total_seconds() / 60)
            except (ValueError, TypeError):
                pass

        # Count items for array slots
        slot_val = slots.get(slot_name)
        item_count = len(slot_val) if isinstance(slot_val, list) else None

        result[slot_name] = {
            "minutes_since_update": mins_since_update,
            "minutes_since_access": mins_since_access,
            "update_count": update_count,
            "item_count": item_count,
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        for name, info in result.items():
            upd = f"{info['minutes_since_update']}m" if info['minutes_since_update'] is not None else "never"
            acc = f"{info['minutes_since_access']}m" if info['minutes_since_access'] is not None else "never"
            items = f", {info['item_count']} items" if info['item_count'] is not None else ""
            print(f"  {name}: updated {upd} ago, accessed {acc} ago, {info['update_count']} writes{items}")

def cmd_prune(args):
    """Mid-session pruning based on config thresholds.

    RMW protected by wm_lock — see g-115-206. Prune touches every slot, so
    a concurrent wm-set from any source could clobber pruning output (or
    vice versa) without the lock.
    """
    with wm_lock():
        _do_prune(args)

def _do_prune(args):
    data = read_wm()
    if not data:
        print("Working memory not initialized.", file=sys.stderr)
        sys.exit(1)

    config = read_config()
    pruning = get_pruning_config(config)
    now = datetime.now()
    meta = data.get("slot_meta", {})
    slots = data.get("slots", {})
    protected = set(pruning.get("protected_slots", []))
    report = {"pruned_items": [], "stale_slots": [], "evicted_slots": []}

    stale_mins = pruning.get("stale_threshold_minutes", 30)
    evict_mins = pruning.get("evict_threshold_minutes", 120)
    item_stale = pruning.get("item_stale_minutes", {})
    limits = pruning.get("array_limits", {})

    for slot_name, slot_val in list(slots.items()):
        m = meta.get(slot_name, {})
        updated_str = m.get("updated_at")

        mins_since = None
        if updated_str:
            try:
                dt = datetime.fromisoformat(updated_str)
                mins_since = (now - dt).total_seconds() / 60
            except (ValueError, TypeError):
                pass

        # Flag stale slots
        if mins_since is not None and mins_since > stale_mins:
            report["stale_slots"].append({
                "slot": slot_name,
                "minutes_stale": int(mins_since),
            })

        # Evict stale scalar slots (non-protected, non-array, non-map,
        # non-cadence-tracker). Cadence trackers are stale BY DESIGN — fire
        # every N goals or N hours; eviction destroys cadence memory and
        # causes duplicate firings. See CADENCE_TRACKER_PATTERNS above.
        if (slot_name not in protected
                and slot_name not in ARRAY_SLOTS
                and slot_name not in MAP_SLOTS
                and not _is_cadence_tracker(slot_name)
                and slot_val is not None
                and mins_since is not None
                and mins_since > evict_mins):
            if not args.dry_run:
                slots[slot_name] = None
                update_modified(data, slot_name)
            report["evicted_slots"].append({
                "slot": slot_name,
                "minutes_stale": int(mins_since),
            })

        # Array item pruning
        if isinstance(slot_val, list) and slot_name in item_stale:
            max_age = item_stale[slot_name]
            to_remove = []
            for i, item in enumerate(slot_val):
                if not isinstance(item, dict):
                    continue
                ts = item.get("_item_ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts)
                    age_mins = (now - dt).total_seconds() / 60
                except (ValueError, TypeError):
                    continue
                if age_mins > max_age:
                    # Protected slots: only prune resolved items
                    if slot_name in protected:
                        if slot_name == "known_blockers" and item.get("resolution") is not None:
                            to_remove.append(i)
                        elif slot_name == "knowledge_debt" and item.get("resolved"):
                            to_remove.append(i)
                    else:
                        # For micro_hypotheses: only prune resolved ones by age
                        if slot_name == "micro_hypotheses" and item.get("outcome") is not None:
                            to_remove.append(i)
                        elif slot_name != "micro_hypotheses":
                            to_remove.append(i)

            for i in reversed(to_remove):
                removed = slot_val.pop(i)
                report["pruned_items"].append({
                    "slot": slot_name,
                    "item_summary": str(removed.get("claim", removed.get("reason", removed.get("observation", "?"))))[:80],
                    "reason": "item_stale",
                })

            if to_remove and not args.dry_run:
                update_modified(data, slot_name)

        # Array size cap enforcement
        if isinstance(slot_val, list) and slot_name in limits:
            limit = limits[slot_name]
            if len(slot_val) > limit:
                # Sort by _item_ts, remove oldest
                slot_val.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
                while len(slot_val) > limit:
                    removed = slot_val.pop(0)
                    report["pruned_items"].append({
                        "slot": slot_name,
                        "item_summary": str(removed.get("claim", removed.get("reason", "?")))[:80] if isinstance(removed, dict) else "?",
                        "reason": "array_limit",
                    })
                if not args.dry_run:
                    update_modified(data, slot_name)

    # Also check encoding_queue (top-level)
    eq = data.get("encoding_queue", [])
    eq_limit = limits.get("encoding_queue", 20)
    if isinstance(eq, list) and len(eq) > eq_limit:
        eq.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
        while len(eq) > eq_limit:
            removed = eq.pop(0)
            report["pruned_items"].append({
                "slot": "encoding_queue",
                "item_summary": str(removed.get("observation", "?"))[:80] if isinstance(removed, dict) else "?",
                "reason": "array_limit",
            })

    if not args.dry_run:
        write_wm(data)

    print(json.dumps(report, ensure_ascii=False, default=str))

def cmd_init(args):
    """Initialize working memory from template.

    Lock-protected (g-115-206): pure write, but a concurrent wm-set could
    otherwise clobber init or vice versa during the rare window where init
    fires while the loop is starting another writer.
    """
    data = _default_wm_data()
    slot_count = len(data.get("slots", {}))

    with wm_lock():
        write_wm(data)
    print(f"Working memory initialized with {slot_count} slots.")

def cmd_reset(args):
    """Reset working memory to template state. Preserves SESSION_IDENTITY_FIELDS.

    RMW protected by wm_lock — see g-115-206. The read of `existing` for
    identity-field preservation must happen INSIDE the same lock as the
    write, otherwise a concurrent writer's update to those fields could
    be observed-then-clobbered.
    """
    data = _default_wm_data()
    slot_types = data.get("slots", {}).keys()

    with wm_lock():
        existing = read_wm()
        preserved = []
        for k in SESSION_IDENTITY_FIELDS:
            v = existing.get(k)
            if v is not None:
                data[k] = v
                preserved.append(k)

        # Preserve cadence-tracker slots — they hold "last X fired at"
        # timestamps that drive iteration cadences (last_felt_sense_checkin,
        # last_strategic_scan, etc). Eviction at reset causes duplicate
        # firings the next time their gate evaluates. Mirrors the cadence-
        # tracker exemption in maintain prune (see _is_cadence_tracker and
        # the _is_cadence_tracker check in cmd_maintain). Iterates existing
        # slots (not slot_types) because cadence-trackers are typically
        # added dynamically and may not appear in slot_types config.
        # RESET_SURVIVING_SLOTS members survive the same way — their reader
        # runs AFTER this reset by design ().
        existing_slots = existing.get("slots", {})
        existing_meta = existing.get("slot_meta", {})
        cadence_preserved = []
        surviving_preserved = []
        for slot_name, slot_val in existing_slots.items():
            is_cadence = _is_cadence_tracker(slot_name)
            if (is_cadence or slot_name in RESET_SURVIVING_SLOTS) and slot_val is not None:
                data["slots"][slot_name] = slot_val
                if slot_name in existing_meta:
                    data["slot_meta"][slot_name] = existing_meta[slot_name]
                (cadence_preserved if is_cadence else surviving_preserved).append(slot_name)

        write_wm(data)
    status_parts = []
    if preserved:
        status_parts.append(", ".join(sorted(preserved)))
    if cadence_preserved:
        status_parts.append(f"{len(cadence_preserved)} cadence trackers")
    if surviving_preserved:
        status_parts.append("reset-surviving: " + ", ".join(sorted(surviving_preserved)))
    if status_parts:
        print(f"Working memory reset to template state ({len(slot_types)} slots; preserved: {'; '.join(status_parts)}).")
    else:
        print(f"Working memory reset to template state ({len(slot_types)} slots).")

def cmd_clear_identity(args):
    """Clear SESSION_IDENTITY_FIELDS. Authorized caller: /stop graceful-stop D4.5.

    RMW protected by wm_lock — see g-115-206.
    """
    with wm_lock():
        data = read_wm()
        if not data:
            print("Working memory not initialized; nothing to clear.")
            return
        cleared = []
        for k in SESSION_IDENTITY_FIELDS:
            if data.get(k) is not None:
                data[k] = None
                cleared.append(k)
        if not cleared:
            print("Session-identity fields already clear.")
            return
        write_wm(data)
    print(f"Cleared session-identity fields: {', '.join(sorted(cleared))}.")

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Working memory access layer")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- set ---
    p_set = sub.add_parser("set", help="Set a slot value (JSON from stdin)")
    p_set.add_argument("slot", help="Slot path (e.g. 'active_context', 'active_context.retrieval_manifest')")
    p_set.add_argument(
        "--override-merge-gate",
        default=None,
        help="rb-715 subdict-clobber gate override. Pass a justification string "
             "(e.g. 'session boundary signal reset') to bypass the merge gate "
             "when intentionally clearing on-disk loop_state.signals subkeys. "
             "Each override is appended to world/loop-state-merge-overrides.jsonl.",
    )

    # --- append ---
    p_app = sub.add_parser("append", help="Append item to array slot (JSON from stdin)")
    p_app.add_argument("slot", help="Slot name (e.g. 'micro_hypotheses', 'encoding_queue')")

    # --- clear ---
    p_clr = sub.add_parser("clear", help="Clear a slot (null for scalars, [] for arrays)")
    p_clr.add_argument("slot", help="Slot name")

    # --- ages ---
    p_ages = sub.add_parser("ages", help="Report slot ages")
    p_ages.add_argument("--json", action="store_true", help="Output as JSON")

    # --- prune ---
    p_prune = sub.add_parser("prune", help="Mid-session pruning")
    p_prune.add_argument("--dry-run", action="store_true", help="Report what would be pruned without modifying")

    # --- init ---
    sub.add_parser("init", help="Initialize working memory from template")

    # --- reset ---
    sub.add_parser("reset", help="Reset working memory to template state (preserves session-identity)")

    # --- clear-identity ---
    sub.add_parser("clear-identity", help="Clear SESSION_IDENTITY_FIELDS (session_id, session_start). Called by /stop post-consolidate.")

    return parser

DISPATCH = {
    "set": cmd_set,
    "append": cmd_append,
    "clear": cmd_clear,
    "ages": cmd_ages,
    "prune": cmd_prune,
    "init": cmd_init,
    "reset": cmd_reset,
    "clear-identity": cmd_clear_identity,
}

def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)

if __name__ == "__main__":
    main()
