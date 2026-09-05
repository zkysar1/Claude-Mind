"""Gate firing telemetry — single append-only log of every gate decision.

Read by the retirement evaluator (aspirations-evolve), per-gate tuning corpus
generation, and the gate-stats dashboard. Storage: `{META_DIR}/gate-firings.jsonl`
(legacy, append-only) plus `gate-firings-YYYY-MM-DD.jsonl` date segments once
GATE_FIRINGS_SEGMENTED is on (store_name() is the one writer rule; firings_paths()
the one reader rule).

Decision taxonomy — exact strings, treat as enum:
  noop       Gate invoked but no trigger matched. Counted for invocation
             totals but NOT counted as "fired".
  pass       Trigger matched and evidence/check passed. Fired but did not block.
  block      Trigger matched, evidence insufficient, no override. Caller stopped.
  override   Trigger matched, would have blocked, but caller passed --override.
  fail_open  Gate raised an exception. Caller proceeds; investigate offline.

Retirement signal: count(decision != "noop") in window.
FP signal:         count(override) / (count(block) + count(override)).

Contract: log() never raises. Telemetry must not break gates.

Pytest suppression (g-248-102): log() is a silent no-op when
PYTEST_CURRENT_TEST is set (pytest exports it for the duration of each test)
UNLESS GATE_LOG_ALLOW_PYTEST is also set. Without this guard, any test that
imports a gate module and exercises a classifier writes SYNTHETIC firings into
the production gate-firings.jsonl, contaminating the noop/pass ratios the
retirement evaluator scores (observed: test_target_state_check_positional.py
leaked ~16 read-intent-verbs records per suite run since 2026-05-17; the first
run of test_target_state_removal_intent.py wrote 17 removal-intent-verbs
records — g-248-101 discovery). Tests that POSITIVELY assert on firing records
(test_layer_d_telemetry.py) opt out via GATE_LOG_ALLOW_PYTEST=1 with their
destination redirected to a tmp meta dir. The env guard covers in-process
imports and subprocess children (both inherit pytest's environment); hermetic
daemon fixtures are covered separately by their tmp project-root isolation.
"""

import datetime as _dt
import hashlib as _hashlib
import json as _json
import os as _os
import re as _re
import sys as _sys
from pathlib import Path as _Path

# Single source of truth for META_DIR — same resolver every other script uses.
# Do NOT re-implement local-paths.conf parsing here.
from _paths import META_DIR
from _fileops import locked_append_jsonl

_SCHEMA_VERSION = 1
_VALID_DECISIONS = ("noop", "pass", "block", "override", "fail_open")

# Own-cloud spool lane (). Under STORAGE_BACKEND=own-cloud a direct
# locked_append_jsonl on gate-firings.jsonl is a whole-object S3
# read-modify-write — measured 3.8-10.1s per append at 38-40MB/~118k records,
# paid by EVERY instrumented gate on EVERY decision including noop. The spool
# makes the hot path O(1): append one line to a machine-local spool file
# (lockless O_APPEND, same idiom as _fileops._record_fallback_hit — sub-4KB
# single-line writes; a torn line is harmless, the flusher skips it), and
# gate-firings-flush.py (iteration-close maintenance tick) batches the spool
# into the shared store with ONE locked RMW per flush. The spool basename is
# in owncloud_sync._EXCLUDE_NAMES — never synced, never refresh-clobbered.
# Local/other backends keep the direct locked append: it is a cheap raw local
# append there, and tests (GATE_LOG_ALLOW_PYTEST) assert on the store file.
_SPOOL_NAME = "gate-firings.spool.jsonl"

# Store-composition seam (). The spool above fixed the WRITE-FREQUENCY
# axis of own-cloud write amplification; the OBJECT-SIZE axis is untouched.
# Measured 2026-07-31 (cc-04): the shared store is 44.75MB/127k records and is
# re-PUT whole on every iteration-close flush -- ~42MB written to add ~1KB, an
# amplification near 42,000:1, producing 26,610 S3 versions / 968 GB, which is
# 65% of the entire bucket and 2.2x aspirations.jsonl + reasoning-bank.jsonl
# COMBINED. Retention cannot absorb it: `retention_days: 40` in
# store-hygiene.yaml is already the floor (all 4 readers window by time and
# gate-retirement-eval defaults to --days 30), and a sweep would drop only 5.8%.
#
# The fix is to segment the store by date so a flush touches only the live
# segment. The blocker was that three consumers each hardcoded the filename
# (gate-stats.py, gate-retirement-eval.py, override-ledger-consume.py), so a
# segmented writer would silently starve them -- they would read a few hours of
# data and report it as a 30-day window, i.e. a gate looks unfired and therefore
# RETIRABLE. A false all-clear is the worst available failure direction, which is
# why this seam lands BEFORE any writer change.
#
# This returns PATHS rather than records deliberately: the three consumers have
# genuinely different parse/filter needs (since-windowing, override counting,
# recency cutoffs), so a shared record-reader would mean rewriting three working
# parse loops. A path list leaves them untouched and makes segmentation a change
# to this function alone.
# Segments are matched by a STRICT date-shaped pattern, not a loose
# `gate-firings-*.jsonl` glob. The loose form admitted `gate-firings-spool.jsonl`
# while the accompanying name-prefix check keyed on the DOTTED production spool
# (`gate-firings.spool.jsonl`) -- a form the glob could never produce, so that
# check was structurally dead and the exclusion it claimed to provide did not
# exist. Caught by test_spool_excluded_even_when_hyphenated. Matching the exact
# segment shape means anything that is not a real segment is excluded by
# construction rather than by an enumerated denylist that must be kept in sync
# with every future sibling file.
_SEGMENT_RE = _re.compile(r"^gate-firings-\d{4}-\d{2}-\d{2}\.jsonl$")


def segment_name(day=None):
    """Basename of the date segment covering `day` (default: today).

    Deliberately defined HERE, immediately beside `_SEGMENT_RE`, rather than in
    the writer: the writer's filename and the reader's matcher are the two
    halves of one contract, and the failure mode when they drift is silent --
    the writer keeps producing files the reader does not recognise, so
    consumers read a short window and report it as the full retention window.
    One definition, imported by both, makes that drift impossible rather than
    merely unlikely.

    Dates are UTC wall clock (TZ=UTC fleet-wide), matching the `ts` field the
    consumers window on.
    """
    day = day or _dt.datetime.now().date()
    return f"gate-firings-{day.isoformat()}.jsonl"


# Writer flag for the segmented store ( / ). Per-box on purpose:
# a box flips it only after the fleet's readers understand segments. Defined HERE,
# beside segment_name(), so that EVERY writer lane — the spool flush
# (gate-firings-flush.py) AND the direct locked append below — resolves the same
# target from the same rule. Until 2026-08-18 the rule lived only in the flush
# script, so the direct lane (`log()` on a process whose env did not carry
# STORAGE_BACKEND=own-cloud) kept appending to the legacy file after the flip:
# measured ~1000 whole-object RMWs on the 68 MB legacy object in the 12h after the
# flag went fleet-wide, from CLI gate scripts (capability-gate, origin-signal-gate,
# goal-duplication-gate, store-dupe-warn) on registry-native boxes. Every one of
# those re-heated the object for every box's next pull sweep — the exact egress the
# flip existed to remove.
SEGMENTED_ENV = "GATE_FIRINGS_SEGMENTED"
LEGACY_STORE_NAME = "gate-firings.jsonl"


def segmented_enabled():
    """True when this box writes new firings to today's date segment."""
    return _os.environ.get(SEGMENTED_ENV, "").strip().lower() in ("1", "true", "yes")


def store_name(day=None):
    """Basename every writer lane appends to: today's segment when the flag is
    on, the legacy file otherwise. Readers accept both (firings_paths)."""
    return segment_name(day) if segmented_enabled() else LEGACY_STORE_NAME


def firings_paths(meta_dir=None):
    """Ordered paths comprising the gate-firings store, oldest-first.

    The store is the legacy file PLUS every `gate-firings-YYYY-MM-DD.jsonl`
    segment, appended in lexical (== chronological, ISO dates) order. Reading
    the legacy file alone is NOT equivalent and has not been since the segment
    writer flipped on: measured 2026-09-05 (alpha, cc-04) the legacy file held
    174,493 of 354,624 rows across 21 paths, so a consumer that reads it
    directly silently drops 51% of the corpus. This docstring said the
    opposite until then and cost a real measurement (g-115-5311, which scanned
    only the legacy file on its first pass). Enumerate through this function;
    never hand-roll the path.

    Excludes the machine-local spool files, which share the `gate-firings-`
    stem but are NOT part of the shared store (they are drained into it by
    gate-firings-flush.py and are in owncloud_sync._EXCLUDE_NAMES).
    """
    # Guard the module constant, not just the parameter (). META_DIR is
    # None whenever paths are unresolved, so the no-arg call -- the shape this
    # docstring invites -- raised TypeError from _Path(None). Return [] instead,
    # matching the writer: gate-firings-flush.py:191 treats an unresolved
    # META_DIR as a real runtime state with nothing to do, not an error.
    resolved = meta_dir if meta_dir is not None else META_DIR
    if resolved is None:
        # Say so on stderr. Returning [] silently would make "paths unresolved"
        # indistinguishable from "store is genuinely empty", and a consumer
        # reading zero firings concludes a gate never fired and is therefore
        # RETIRABLE -- the false all-clear this module's own ORDERING
        # CONSTRAINT (gate-firings-flush.py:77-83) calls the worst direction
        # this system can fail in. The writer prints for the identical
        # condition; "matching the writer" means matching its loudness too.
        print("[_gate_log] META_DIR unresolved — gate-firings store not "
              "enumerable; returning no paths", file=_sys.stderr)
        return []
    base = _Path(resolved)
    paths = []
    legacy = base / "gate-firings.jsonl"
    if legacy.is_file():
        paths.append(legacy)
    for seg in sorted(base.glob("gate-firings-*.jsonl")):
        if _SEGMENT_RE.match(seg.name) and seg.is_file():
            paths.append(seg)
    return paths


def _spool_active():
    """Spool (own-cloud) or append directly (any other backend)?

    Decided from the RESOLVED backend, not from a raw env read. A bare
    subprocess on a registry-native box (ENVIRONMENT_ID set, STORAGE_BACKEND
    not exported — every CLI gate script spawned from the loop's Bash lands
    here) starts with STORAGE_BACKEND unset. The old env-only test then said
    "not own-cloud", took the direct lane, and locked_append_jsonl's own
    get_backend() bootstrapped the env from the registry and performed a
    whole-object S3 RMW on the shared store — the very cost the spool exists
    to avoid (measured 2026-08-18: the dominant writer of the legacy 68 MB
    object after the segment flip). Resolve the same way get_backend() will,
    then re-read: an explicit pin (guard-955 STORAGE_BACKEND=local, or any
    deliberate override) is untouched because the bootstrap only setdefaults.
    """
    explicit = _os.environ.get("STORAGE_BACKEND", "").strip().lower()
    if explicit:
        return explicit == "own-cloud"
    try:
        from storage_backend import _bootstrap_env_defaults  # lazy: hot path stays cheap
        _bootstrap_env_defaults()
    except Exception:
        return False
    return _os.environ.get("STORAGE_BACKEND", "").strip().lower() == "own-cloud"


def _hash_payload(payload):
    if payload is None:
        return None
    if not isinstance(payload, str):
        # default=str handles non-serializable types (datetime, Path, etc.).
        # If json.dumps still raises (e.g. circular refs), the outer log()
        # try/except drops the record — telemetry is best-effort.
        payload = _json.dumps(payload, sort_keys=True, default=str)
    return _hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:12]


def _truncate(value, limit):
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "..."


def log(
    gate_id,
    decision,
    *,
    caller=None,
    trigger_matched=None,
    payload=None,
    override_reason=None,
    gate_error=None,
    extra=None,
    meta_dir=None,
    agent_name=None,
):
    """Append a firing record. Best-effort, NEVER raises.

    Parameters:
      gate_id: stable identifier; MUST match an `id` in core/config/gates.yaml
               or the retirement evaluator will not see this gate's firings.
      decision: one of _VALID_DECISIONS. Invalid values are coerced to
                "fail_open" with a marker in `extra._invalid_decision_received`
                so the bad call surfaces in the log itself rather than crashing
                the caller.
      meta_dir: optional override for the META_DIR destination. Required by the
                daemon (`mind_api/src/endpoints/`) — the daemon process imports
                this module once at startup, so the module-level META_DIR is
                frozen to whichever agent's local-paths.conf the daemon process
                was launched under. Multi-tenant daemon requests pass
                `ctx.paths.meta` explicitly so the firing record lands in the
                CALLING agent's gate-firings.jsonl, not the daemon-launch
                agent's. Omit in legacy CLI / subprocess callers — the
                module-level META_DIR is correct for those (the CLI process
                has its own MIND_AGENT env).
      agent_name: optional override for the `agent` field on the record. Same
                  motivation as meta_dir — env-derived value is wrong in the
                  daemon. Omit elsewhere.
    """
    try:
        # Pytest suppression () — see module docstring. Checked inside
        # the try so the never-raises contract stays airtight.
        if (_os.environ.get("PYTEST_CURRENT_TEST")
                and not _os.environ.get("GATE_LOG_ALLOW_PYTEST")):
            return

        if decision not in _VALID_DECISIONS:
            extra = dict(extra or {})
            extra["_invalid_decision_received"] = str(decision)
            decision = "fail_open"

        record = {
            "schema_version": _SCHEMA_VERSION,
            "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "gate_id": gate_id,
            "decision": decision,
            "agent": agent_name or _os.environ.get("MIND_AGENT", "") or None,
            "session_id": _os.environ.get("MIND_SID", "") or None,
        }
        if caller is not None:
            record["caller"] = _truncate(caller, 120)
        if trigger_matched is not None:
            record["trigger_matched"] = _truncate(trigger_matched, 200)
        if payload is not None:
            record["payload_hash"] = _hash_payload(payload)
        if override_reason is not None:
            record["override_reason"] = _truncate(override_reason, 500)
        if gate_error is not None:
            record["gate_error"] = _truncate(gate_error, 200)
        if extra is not None:
            record["extra"] = extra

        dest = (meta_dir if meta_dir is not None else META_DIR)
        if _spool_active():
            # O(1) hot path: one lockless local append; the flush tick
            # batches records into the shared store (see _SPOOL_NAME note).
            with open(dest / _SPOOL_NAME, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=True) + "\n")
        else:
            # Same target rule as the spool flush (store_name): with the
            # segment flag on, the direct lane must not keep re-heating the
            # legacy object either.
            locked_append_jsonl(dest / store_name(), record)
    except Exception:
        return


def _self_test():
    """Smoke check — run via `py -3 _gate_log.py`. Writes one marker record."""
    print("schema_version:", _SCHEMA_VERSION)
    print("valid decisions:", _VALID_DECISIONS)
    print("META_DIR:", META_DIR)
    print("test payload hash:", _hash_payload({"claim": "smoke"}))
    log(
        "_gate_log_self_test",
        "noop",
        caller="_gate_log.py:_self_test",
        trigger_matched="(self-test marker)",
        payload="self-test payload",
        extra={"smoke_test": True},
    )
    print("self-test log() call completed (check meta/gate-firings.jsonl)")


def _cli_log(argv):
    """CLI entry for SKILL.md-level gates: invoked via core/scripts/gate-log.sh."""
    import argparse as _ap
    p = _ap.ArgumentParser(prog="_gate_log.py log",
                           description="Append one gate-firing record. Best-effort, never raises.")
    p.add_argument("gate_id", help="Stable gate id; MUST match core/config/gates.yaml id.")
    p.add_argument("decision", choices=_VALID_DECISIONS,
                   help="One of " + "/".join(_VALID_DECISIONS))
    p.add_argument("--caller", default=None, help="Callsite label.")
    p.add_argument("--trigger", dest="trigger_matched", default=None,
                   help="Pattern/keyword that matched, or omit for noop.")
    p.add_argument("--payload", default=None, help="String payload to hash for de-dup.")
    p.add_argument("--override-reason", default=None, help="Justification when decision=override.")
    p.add_argument("--gate-error", default=None, help="Exception detail when decision=fail_open.")
    p.add_argument("--extra-json", default=None,
                   help="Optional JSON object merged into the `extra` field.")
    args = p.parse_args(argv)
    extra = None
    if args.extra_json:
        try:
            extra = _json.loads(args.extra_json)
        except Exception:
            extra = {"_extra_json_parse_error": args.extra_json[:200]}
    log(args.gate_id, args.decision,
        caller=args.caller,
        trigger_matched=args.trigger_matched,
        payload=args.payload,
        override_reason=args.override_reason,
        gate_error=args.gate_error,
        extra=extra)


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "log":
        _cli_log(_sys.argv[2:])
    else:
        _self_test()
