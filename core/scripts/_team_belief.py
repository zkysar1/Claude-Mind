#!/usr/bin/env python3
"""Theory-of-Mind partner-belief supersede/cap logic ().

Pure helper for the write half of the partner-belief loop documented in
`core/config/conventions/coordination.md` Team State Protocol (g-306-18 proved
the storage; g-306-28 wires the write+consume loop).

`supersede_beliefs` is the single source of truth for the hygiene rule
"supersede the prior belief about a given partner rather than growing the list
unbounded" (coordination.md "Hygiene"). It is pure (no I/O) so it can be unit
tested in isolation; `team-belief-write.sh` calls `main()` between a daemon
read and a daemon `--operation set` write.

Race safety: each agent is the SOLE writer of `agent_status.<self>.beliefs`
(one runner per agent; partners write their OWN beliefs sublists), so the
read-modify-write the wrapper performs is race-free at the field level even
though the read and the set are separate daemon calls. The daemon's
`--operation set` only touches the `agent_status.<self>.beliefs` path, so a
concurrent partner write to a different field is preserved under the shared
team-state lock.
"""
import argparse
import json
import math
import sys
from datetime import datetime

# Hard cap on total beliefs an agent holds. Supersede-by-`about` already bounds
# the list to one entry per partner; this is a safety net so a malformed/empty
# `about` value cannot grow the list without bound. Sized for a small team
# (5 partners) plus headroom.
MAX_BELIEFS = 10


def supersede_beliefs(current, about, belief, confidence, now_iso, max_total=MAX_BELIEFS, domain=None):
    """Return a new beliefs list with the belief about `about` superseded.

    - Drops every prior entry whose `about` matches (case-exact) — the new
      observation replaces the old one rather than appending.
    - Clamps `confidence` to [0.0, 1.0]; a non-numeric value falls back to 0.5
      (the calibrated single-observation default per coordination.md).
    - Appends `{about, belief, confidence, last_observed, domain, valid_from,
      valid_to}` and caps the result to the most-recent `max_total` entries.
      valid_from is stamped to `now_iso`; valid_to is None (current belief) —
      see the append site for the bi-temporal rationale (g-306-35).

    `domain` (g-306-29) is the OPTIONAL structured focus-domain this belief
    asserts the partner is working in (e.g. "framework-architecture"). When set,
    `_belief_contradiction.py` can compare it against the partner's freshly
    observed `current_focus` and trigger a forced reflection on a sustained
    mismatch. `None` (the default) means the belief is free-form and not
    contradiction-checkable — the detector skips it, so the field is purely
    additive and breaks no existing consumer.

    `current` may be None, a non-list (treated as empty), or a list with
    non-dict junk entries (those are preserved unless they match `about`).
    """
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.5
    if not math.isfinite(conf):  # nan / inf are not valid confidences
        conf = 0.5
    conf = max(0.0, min(1.0, conf))

    # Coerce a non-list `current` to empty (matches main()'s guard) so the
    # docstring contract holds for direct callers, not just the CLI path.
    if not isinstance(current, list):
        current = []
    kept = [
        b for b in current
        if not (isinstance(b, dict) and b.get("about") == about)
    ]
    kept.append({
        "about": about,
        "belief": belief,
        "confidence": conf,
        "last_observed": now_iso,
        "domain": domain,
        # valid_from / valid_to (, BRD Gap 5): bi-temporal validity
        # interval, making each current belief reader-compatible across the Mind
        # stores. valid_from = now_iso (this belief version became the current
        # observation now; coincides with last_observed for a single-obs belief);
        # valid_to = None (this IS the current belief). The snapshot keeps the
        # one-per-partner supersede-drop + cap model — close-old-insert-new
        # falsification history lives in the append-only RB store, not the
        # bounded belief snapshot.
        "valid_from": now_iso,
        "valid_to": None,
    })
    if len(kept) > max_total:
        kept = kept[-max_total:]
    return kept


def main(argv=None):
    """CLI compute step: read current beliefs JSON from stdin, print the
    superseded list to stdout. Params arrive as argv (safe — not interpolated
    into a python -c source string, so guard-165 does not apply)."""
    ap = argparse.ArgumentParser(description="Compute a superseded partner-belief list")
    ap.add_argument("--about", required=True, help="Partner agent the belief concerns")
    ap.add_argument("--belief", required=True, help="One-line observed claim")
    ap.add_argument("--confidence", default="0.5", help="0.0-1.0 (single obs ~0.5)")
    ap.add_argument("--now", default=None, help="ISO timestamp (default: now, local)")
    ap.add_argument("--domain", default=None,
                    help="Optional structured focus-domain this belief asserts "
                         "(enables g-306-29 contradiction detection)")
    args = ap.parse_args(argv)

    raw = sys.stdin.read().strip()
    try:
        current = json.loads(raw) if raw and raw != "null" else []
    except (json.JSONDecodeError, ValueError):
        current = []
    if not isinstance(current, list):
        current = []

    now_iso = args.now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    out = supersede_beliefs(current, args.about, args.belief, args.confidence, now_iso,
                            domain=args.domain)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
