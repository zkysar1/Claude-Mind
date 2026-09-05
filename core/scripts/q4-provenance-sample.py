#!/usr/bin/env python3
"""CLI for the Q4 provenance sampler ().

Separate from `q4_provenance_sample.py` for the reason the sibling gate splits
the same way: the module stays PURE and importable (the reviewer's Step 4 calls
`direction_fidelity` in-process), while this file owns argv, stdout and the exit
code. A test can then exercise the logic without a subprocess, and this entry
without re-deriving the logic.

EXIT CODE IS THE ANSWER (guard-1150): 0 = pass or skipped, 1 = at least one
sampled claim is uncited, decoratively cited, or reversed against its source.
2 = usage. Never wrap a call to this in a trailing pipe — the pipe's exit code
would replace the verdict.

`--session-id` DEFAULTS TO $MIND_SID rather than to None, and that default is
load-bearing on a worker Body: see `retrieved_predicate`'s docstring for the
measured control. Pass it explicitly only to inspect another session.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from q4_provenance_sample import DEFAULT_SAMPLE_N, run  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Q4: sample entity-fact claims from a goal's artifact and "
                    "resolve their citations against the session provenance manifest.")
    ap.add_argument("--goal", required=True,
                    help="goal id — seeds the deterministic sample")
    ap.add_argument("--artifact", action="append", default=[], required=True,
                    help="path to a produced artifact (repeatable)")
    ap.add_argument("-n", "--sample-n", type=int, default=DEFAULT_SAMPLE_N,
                    help=f"max clusters to sample per artifact (default {DEFAULT_SAMPLE_N})")
    ap.add_argument("--session-id", default=None,
                    help="session whose provenance manifest to read "
                         "(default: $MIND_SID — required for a worker Body)")
    ap.add_argument("--source-file", default=None,
                    help="cited source text; enables the direction-fidelity check")
    ap.add_argument("--json", action="store_true",
                    help="emit the full result as JSON instead of the summary")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    session_id = args.session_id or os.environ.get("MIND_SID") or None

    source_text = None
    if args.source_file:
        try:
            source_text = Path(args.source_file).read_text(encoding="utf-8",
                                                           errors="replace")
        except OSError as exc:
            print(f"q4-provenance-sample: cannot read --source-file: {exc}",
                  file=sys.stderr)
            return 2

    result = run(args.goal, args.artifact, n=args.sample_n,
                 session_id=session_id, source_text=source_text)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Q4 {result['verdict'].upper()}  goal={result['goal_id']}  "
              f"sampled {result['sampled_count']} of {result['clusters_total']} "
              f"cluster(s) across {len(result['artifacts_read'])} artifact(s); "
              f"manifest={result['provenance_manifest']}")
        for m in result["artifacts_missing"]:
            print(f"  UNREADABLE ARTIFACT: {m}")
        if result["skip_reason"]:
            print(f"  skipped: {result['skip_reason']}")
        for f in result["findings"][:10]:
            print(f"  L{f['start_line']}-{f['end_line']} {f['kind']}: {f['detail']}")
            print(f"      > {f['sample']}")
        if len(result["findings"]) > 10:
            print(f"  ... and {len(result['findings']) - 10} more finding(s).")

    return 1 if result["verdict"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
