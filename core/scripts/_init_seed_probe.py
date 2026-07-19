#!/usr/bin/env python3
"""_init_seed_probe.py — store-of-record existence probe for init seed-backfill (4).

Answers ONE question for init-world.sh / init-meta.sh `seed_needed()` (and
meta-init.py --missing-only) when a candidate seed target is absent from the
LOCAL mirror during a BACKFILL pass under STORAGE_BACKEND=own-cloud: is the
file genuinely absent from the store of record (S3), or merely cache-absent
on this box?

Local absence is NOT authoritative under own-cloud (read-through cache,
guard-980 class): seeding a pristine stub over a store-present evolved file
would be pushed by the next sweep — the exact clobber the per-file guards
exist to prevent (g-115-2313). Verdicts:

  "seed"         — absent in the store too; safe to seed.
  "materialized" — present in the store; pulled into the local cache
                   (read-through) so direct-filesystem readers see it.
                   Do NOT seed.
  "skip-error"   — probe failed (credentials / network / backend). The SAFE
                   direction is DO NOT SEED: a missed backfill self-heals on
                   the next run; a clobber does not.

CLI:    python3 _init_seed_probe.py <absolute-target-path>   → prints verdict, exit 0
Import: probe_path(Path) -> str  (same verdicts)
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent


def probe_path(target: Path) -> str:
    try:
        if str(_SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(_SCRIPT_DIR))
        from owncloud_backend import OwnCloudBackend

        b = OwnCloudBackend.from_env()
        if not b.exists(target):
            return "seed"
        # Present in the store — materialize into the local cache so direct
        # bash/filesystem readers see it. Best-effort: store-presence alone
        # already decides the verdict.
        try:
            b.read_bytes(target)
        except Exception:
            pass
        return "materialized"
    except Exception:
        return "skip-error"


def main() -> int:
    if len(sys.argv) != 2:
        print("skip-error")
        return 0
    print(probe_path(Path(sys.argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
