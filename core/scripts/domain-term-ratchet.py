#!/usr/bin/env python3
"""domain-term-ratchet — advisory ratchet over the core/domain border wall
(g-115-10049 outcome 5).

Tracks ONE number: how many peer-deployment ids derived from the authoritative
registry (`core/config/environments/*.yaml`) are present in core files but are
NOT on `core/config/domain-term-blocklist.txt`.

WHY THIS SCOPE AND NOT THE WHOLE CENSUS -- this is the load-bearing design
choice, and taking the bigger number would have made the ratchet useless.
`domain-term-census.py` also reports a whole-universe figure of ~1,358
"terms present in core but not blocklisted". That number MUST NOT be ratcheted:
~89% of it is `conventions/heading` prose (1,345 of 1,492 derived terms come
from that one source), i.e. ordinary English words lifted out of markdown
headings, and two attempts to filter it down were already falsified. A ratchet
over it would alarm constantly and be ignored -- a scanner that fires on
everything is a scanner nobody runs.

Scoped to `environments/id` the number is SMALL, REGISTRY-DERIVED, and every
movement is a real event: a new peer Mind deployment appeared in the registry
and its id is now leaking into framework files. That is exactly the signal the
border wall exists to catch, and nothing else in the fleet watches for it.

VACUOUS-RUN GUARD (rb-245, and the reason this file has a `skipped` verdict at
all). A gap count of 0 has TWO causes that look identical from the outside:
every registry id is blocklisted (genuinely clean), or the registry read
returned nothing at all (broken scope, renamed directory, unreadable yaml) --
in which case 0-minus-0 is 0 and reads as a triumph. The discriminator is the
POPULATION, not the gap: `by_source['environments/id']['terms']` is how many
ids the registry yielded. Zero ids means the instrument is blind, so the run is
`skipped` and the baseline is left UNTOUCHED -- never seeded at 0. A ratchet
only shrinks, so a single blind run seeding 0 would pin the baseline at 0
permanently and every later real regression would sit under a floor it can
never reach.

BASELINE MERGE HAZARD (guard-6633 / guard-7161): `merge_audit_baselines` merges
`baseline` by MIN -- one-way shrink, never grow. Do NOT hand-raise this value
when it regresses; the merge silently reverts it and every peer then reads a
false REGRESSED. Fix the leak, or record the decision, but leave the number
alone.

Exit 0 always (advisory) unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _paths import META_DIR  # type: ignore  # noqa: E402
from _fileops import locked_modify_yaml  # type: ignore  # noqa: E402

import yaml  # type: ignore  # noqa: E402

BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "domain_term_registry_gaps"
CENSUS_PY = _HERE / "domain-term-census.py"
SOURCE = "environments/id"


def _run_census() -> dict:
    """Run the census and return its parsed JSON.

    argv[0] is sys.executable -- never a bare "bash" (guard-580).
    """
    out = subprocess.run(
        [sys.executable, str(CENSUS_PY), "--source", SOURCE, "--json"],
        capture_output=True, text=True, timeout=900,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"census exited {out.returncode}: {(out.stderr or '')[:400]}")
    if not (out.stdout or "").strip():
        raise RuntimeError("census produced ZERO BYTES on stdout")
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args()

    try:
        census = _run_census()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: census failed: {e}", file=sys.stderr)
        return 2

    by_source = census.get("by_source")
    by_source = by_source if isinstance(by_source, dict) else {}
    entry_src = by_source.get(SOURCE)
    entry_src = entry_src if isinstance(entry_src, dict) else {}

    registry_terms = int(entry_src.get("terms") or 0)
    blocklisted = int(entry_src.get("blocklisted") or 0)
    files_scanned = int(census.get("files_scanned") or 0)
    current = max(registry_terms - blocklisted, 0)

    # Name the offenders so a regression is attributable without a re-run.
    gap_terms = sorted(
        r.get("term") for r in (census.get("rows") or [])
        if isinstance(r, dict)
        and SOURCE in (r.get("sources") or [])
        and not r.get("blocklisted")
        and r.get("term")
    )

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # --- VACUOUS-RUN GUARD (see module docstring) ---
    if registry_terms == 0 or files_scanned == 0:
        try:
            existing = yaml.safe_load(
                BASELINES_PATH.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            existing = {}
        prior = (existing.get(KEY) or {}).get("baseline")
        message = (
            f"the census measured {registry_terms} registry id(s) over "
            f"{files_scanned} file(s), so a gap count of {current} is vacuous. "
            f"Baseline left UNTOUCHED at {prior}. Check that "
            f"core/config/environments/*.yaml is readable before trusting any "
            f"number from this run."
        )
        result = {"verdict": "skipped", "baseline": prior,
                  "current": {"gaps": current, "registry_terms": registry_terms,
                              "blocklisted": blocklisted,
                              "files_scanned": files_scanned,
                              "gap_terms": gap_terms},
                  "message": message}
        print(json.dumps(result, indent=2) if args.json
              else f"[domain-term-ratchet] SKIPPED: {message}")
        return 0

    captured: dict = {}

    def _modify(baselines):
        # Read the prior INSIDE the lock so the verdict and the committed write
        # see the same value. Sibling ratchets share this file ().
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior_baseline = entry.get("baseline")

        if prior_baseline is None:
            verdict = "seeded"
            new_baseline = current
            message = (
                f"Seeded baseline at {new_baseline} registry id(s) present in "
                f"core but not blocklisted ({registry_terms} id(s) in the "
                f"registry, {blocklisted} already blocklisted). Gaps: "
                f"{', '.join(gap_terms) or '(none)'}."
            )
        elif current > prior_baseline:
            verdict = "regressed"
            new_baseline = prior_baseline  # never raise the baseline
            message = (
                f"WARN: registry ids present in core but not blocklisted grew "
                f"from {prior_baseline} to {current} "
                f"(+{current - prior_baseline}). Current gaps: "
                f"{', '.join(gap_terms)}. A rise means a NEW peer deployment "
                f"id is leaking into framework files. Do NOT hand-raise this "
                f"baseline (guard-7161: the MIN merge reverts it and every "
                f"peer then reads a false REGRESSED) -- genericize the term or "
                f"relocate the data (guard-6792), then re-run."
            )
        elif current < prior_baseline:
            verdict = "ratcheted"
            new_baseline = current
            message = (
                f"OK: registry-id gaps shrank from {prior_baseline} to "
                f"{current} (-{prior_baseline - current}). Baseline ratcheted "
                f"down. Remaining: {', '.join(gap_terms) or '(none)'}."
            )
        else:
            verdict = "stable"
            new_baseline = prior_baseline
            message = (
                f"OK: registry-id gaps stable at baseline {current} "
                f"({', '.join(gap_terms) or 'none'})."
            )

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": current,
            "verdict": verdict,
            "breakdown": {
                "gaps": current,
                "registry_terms": registry_terms,
                "blocklisted": blocklisted,
                "files_scanned": files_scanned,
                "gap_terms": gap_terms,
            },
        })
        history = history[-50:]
        baselines[KEY] = {
            "baseline": new_baseline,
            # WHICH POPULATION this number counts. The census also emits a
            # whole-universe figure ~270x larger; recording the scope here
            # means a future reader who repoints the ratchet cannot silently
            # render the whole history incomparable.
            "scope": SOURCE,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            "history": history,
        }
        captured.update(verdict=verdict, new_baseline=new_baseline,
                        message=message)
        return baselines

    try:
        locked_modify_yaml(BASELINES_PATH, _modify, initial={})
    except Exception as e:  # noqa: BLE001
        print(f"WARN: could not persist baseline to {BASELINES_PATH}: {e}",
              file=sys.stderr)
        # OVERWRITE, never setdefault: _modify populated `captured` before the
        # write, so reporting it now would claim a write that did not happen.
        computed = captured.get("verdict")
        captured["verdict"] = "error"
        captured["new_baseline"] = None
        captured["message"] = (
            f"baseline operation FAILED and nothing was persisted: {e}"
            + (f" (the computed verdict was '{computed}' — it did NOT take "
               f"effect)" if computed else ""))

    result = {
        "verdict": captured["verdict"],
        "baseline": captured["new_baseline"],
        "current": {"gaps": current, "registry_terms": registry_terms,
                    "blocklisted": blocklisted, "files_scanned": files_scanned,
                    "gap_terms": gap_terms},
        "message": captured["message"],
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[domain-term-ratchet] {captured['verdict'].upper()}: "
              f"{captured['message']}")

    if (captured["verdict"] in ("regressed", "error")
            and os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
