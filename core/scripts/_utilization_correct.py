#!/usr/bin/env python3
"""Audit ledger for utilization-counter corrections ().

WHY A LEDGER AND NOT JUST A DECREMENT. The originating goal offered two shapes
and asked for an explicit choice: a bare ``--by <signed-int>`` on the increment
wrappers, or "a correction that adjusts a counter AND records who/why, since a
counter that can be freely decremented is also a counter that can be quietly
laundered". The audited form was chosen, and NOT on preference -- the bare
signed delta is structurally unsafe here and that is measured:

    ``coordination_merge.merge_utilization_counters`` takes a per-counter MAX
    across boxes. Its own docstring states the property -- "MAX never loses an
    increment, it can only fail to gain one" -- so a decrement is exactly what
    MAX discards. Every layer BELOW the merge already handles a signed delta
    (``_utilization_store.record_increment`` does ``int(delta)``,
    ``utilization-flush.apply_deltas`` does ``base + delta``), which is what
    makes the bare form so tempting: it works perfectly on one box and is
    silently reverted at the next cross-box merge. Silent reversion of a
    correction is the exact failure the goal was filed to prevent.

So a correction is an INCREMENT of a monotone ``<counter>__corrected`` sibling,
which MAX reconciles correctly, and ``_utilization_store.apply_corrections``
subtracts at read time. This file records who/why for each one.

WHY THE WHOLE FLOW LIVES HERE AND THE `.sh` IS A THIN SHIM. The counter write
must go through the store's OWN sanctioned wrapper -- `guardrails-increment.sh`
/ `reasoning-bank-increment.sh` -- and not through a hand-rolled `rt_call` loop
of its own. That is the canonical production code path
(`probe-with-canonical-code-path.md`): it already owns the daemon autospawn, the
error shape and the spool-lane explanation, so re-implementing it here would be
a second copy to drift. It also keeps the shim free of `rt_call`, which is what
`check-no-python-cli-fallback.sh` correctly uses to tell a daemon-aware wrapper
(must never exec python) from a pure-CLI one (legitimately may). Mixing both in
one file trips that gate, and the gate is right to refuse it.

APPEND-ONLY BY CONSTRUCTION. ``locked_append_jsonl`` is the only writer here and
no prune/trim/rotate/rewrite path exists, which is the standard guard-1816 sets
for registering a store with ``merge_append_only_jsonl``. Every row carries a
random ``correction_id``, so two genuinely distinct corrections can never be
byte-identical -- the one way a line-union merge could silently collapse a real
event.
"""

import json
import os
import socket
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import WORLD_DIR  # noqa: E402

LEDGER_REL = "utilization-corrections.jsonl"
SCRIPTS_DIR = Path(__file__).resolve().parent
INCREMENT_WRAPPER = {
    "guardrails": "guardrails-increment.sh",
    "reasoning-bank": "reasoning-bank-increment.sh",
}


def ledger_path(world_dir=None):
    return Path(world_dir or WORLD_DIR) / LEDGER_REL


def valid_counters():
    """The counter names a correction may name -- read from the SSOT, never
    re-typed. ``reasoning-bank.py`` owns ``UTILIZATION_COUNTERS``; the daemon's
    ``store_registry.py`` keeps a verbatim copy for import-safety reasons of its
    own, and the two are pinned in parity by a test."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_rb_counters", Path(__file__).resolve().parent / "reasoning-bank.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sorted(mod.UTILIZATION_COUNTERS)


def append_row(store, record_id, counter, by, reason, outcome, applied,
               world_dir=None):
    """Append one correction row. Returns the row that was written."""
    from _fileops import locked_append_jsonl

    row = {
        "correction_id": "uc-" + uuid.uuid4().hex[:12],
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": os.environ.get("MIND_AGENT", ""),
        "sid": os.environ.get("MIND_SID", ""),
        "box": socket.gethostname(),
        "store": store,
        "record_id": record_id,
        "counter": counter,
        "by": int(by),
        "applied": int(applied),
        "outcome": outcome,
        "reason": reason,
    }
    locked_append_jsonl(ledger_path(world_dir), row)
    return row


def _rb_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rb_for_correct", SCRIPTS_DIR / "reasoning-bank.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def correction_field(counter):
    return "utilization." + counter + _rb_module().UTILIZATION_CORRECTION_SUFFIX


def run_correction(store, record_id, counter, by, reason, world_dir=None):
    """Ledger FIRST, then apply, then record the outcome.

    ORDER IS DELIBERATE. A ledger row with no counter change is VISIBLE and
    self-correcting -- the row claims N and `utilization_of` shows fewer, which
    a reader can spot. An applied-but-unaudited correction is invisible forever,
    and "quietly laundered" is the exact failure this path exists to prevent.
    """
    from _runtime_bash import bash_cmd

    field = correction_field(counter)
    row = append_row(store, record_id, counter, by, reason, "intent", 0,
                     world_dir=world_dir)
    wrapper = str(SCRIPTS_DIR / INCREMENT_WRAPPER[store])
    applied = 0
    last_err = ""
    for _ in range(int(by)):
        proc = subprocess.run(bash_cmd(wrapper, record_id, field),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            last_err = (proc.stderr or proc.stdout or "").strip()
            break
        applied += 1
    outcome = "applied" if applied == int(by) else "partial"
    append_row(store, record_id, counter, by, reason, outcome, applied,
               world_dir=world_dir)
    row.update({"outcome": outcome, "applied": applied, "field": field,
                "where": ("increment spooled on this box; lands in the "
                          "<kind>-utilization.jsonl SIDECAR at flush. Read the "
                          "netted value via utilization_of(), never from the "
                          "record embedded utilization block.")})
    if outcome == "partial":
        row["error"] = last_err
    return row


def _positive_int(value):
    import argparse
    if not str(value).isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(
            "--by must be a POSITIVE integer (got: %s). A correction says how "
            "many credits to REMOVE; the sign is implied by the operation."
            % value)
    return int(value)


def main(argv):
    if len(argv) < 2:
        print("usage: _utilization_correct.py counters|log [...]", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "counters":
        for name in valid_counters():
            print(name)
        return 0
    if cmd == "log":
        import argparse

        ap = argparse.ArgumentParser(prog="_utilization_correct.py log")
        ap.add_argument("--store", required=True)
        ap.add_argument("--id", required=True, dest="record_id")
        ap.add_argument("--counter", required=True)
        ap.add_argument("--by", required=True, type=int)
        ap.add_argument("--reason", required=True)
        ap.add_argument("--outcome", required=True)
        ap.add_argument("--applied", required=True, type=int)
        args = ap.parse_args(argv[2:])
        row = append_row(args.store, args.record_id, args.counter, args.by,
                         args.reason, args.outcome, args.applied)
        print(json.dumps(row, ensure_ascii=False))
        return 0
    if cmd == "correct":
        import argparse

        ap = argparse.ArgumentParser(prog="utilization-correct.sh")
        ap.add_argument("--store", required=True,
                        choices=sorted(INCREMENT_WRAPPER))
        ap.add_argument("--id", required=True, dest="record_id")
        ap.add_argument("--counter", required=True)
        ap.add_argument("--reason", required=True)
        ap.add_argument("--by", type=_positive_int, default=1)
        args = ap.parse_args(argv[2:])
        allowed = valid_counters()
        if args.counter not in allowed:
            ap.error("unknown counter: %s (valid: %s)"
                     % (args.counter, ", ".join(allowed)))
        row = run_correction(args.store, args.record_id, args.counter,
                             args.by, args.reason)
        print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0 if row["outcome"] == "applied" else 1

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
