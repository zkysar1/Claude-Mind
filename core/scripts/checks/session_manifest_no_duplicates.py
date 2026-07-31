#!/usr/bin/env python3
"""verify-learning check: no duplicate `file:` names in session-manifest.yaml ().

WHY THIS EXISTS. Three entries were duplicated, and the two copies DISAGREED on
recovery_action (clear vs preserve). That is not a cosmetic redundancy, because
the manifest's two consumers resolve a duplicate in OPPOSITE directions:

  owncloud_sync._load_session_tiers  builds a dict {basename: tier} -> LAST wins
  session-manifest-clear.sh          iterates the LIST and acts on every entry
                                     with recovery_action == "clear" -> the CLEAR
                                     copy wins regardless of position

So a name carrying both `clear` and `preserve` was simultaneously "cleared on
recovery" (per the clearer) and "machine_local, preserved" (per the sync tier).
Measured 2026-07-31: loop-state-bump-failures.jsonl — an APPEND-ONLY log kept for
trend analysis — was being wiped on every recovery because one of its two copies
said clear. Nothing in the file signalled that a second entry existed.

Exits 0 always (advisory), like its sibling temp_durability_invariant.py: a
manifest problem should surface in the verify sweep, not fail the suite.
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import PROJECT_ROOT  # noqa: E402

MANIFEST = Path(PROJECT_ROOT) / "core" / "config" / "session-manifest.yaml"


def main():
    if not MANIFEST.is_file():
        print("SKIP: session-manifest.yaml not found")
        return 0
    try:
        import yaml
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    except Exception as exc:
        print("SKIP: manifest unreadable (%s)" % type(exc).__name__)
        return 0

    entries = [e for e in (data.get("files") or []) if isinstance(e, dict)]
    names = [e.get("file") for e in entries if e.get("file")]
    dupes = sorted(n for n, c in Counter(names).items() if c > 1)

    if not dupes:
        print("PASS: session-manifest has no duplicate file entries (%d entries)" % len(entries))
        return 0

    print("WARN: %d duplicated file entry name(s) in session-manifest.yaml — "
          "one copy's settings are silently ignored, and the clearer and the sync "
          "reader resolve duplicates in OPPOSITE directions: %s" % (len(dupes), ", ".join(dupes)))
    for n in dupes:
        copies = [e for e in entries if e.get("file") == n]
        for field in ("recovery_action", "sync_tier"):
            vals = {str(c.get(field)) for c in copies}
            if len(vals) > 1:
                print("      %s: %s DISAGREES -> %s" % (n, field, sorted(vals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
