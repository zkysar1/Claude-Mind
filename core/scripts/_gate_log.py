"""Gate firing telemetry — single append-only log of every gate decision.

Read by the retirement evaluator (aspirations-evolve), per-gate tuning corpus
generation, and the gate-stats dashboard. Storage:
`{META_DIR}/gate-firings.jsonl` (append-only).

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


def _spool_active():
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
            locked_append_jsonl(dest / "gate-firings.jsonl", record)
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
