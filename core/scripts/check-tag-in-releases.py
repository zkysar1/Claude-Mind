#!/usr/bin/env python3
"""check-tag-in-releases.py — M2 pre-push gate kernel (omni#3).

Pure-logic gate: given a bare semver VERSION (positional arg, e.g. '1.0.0' —
NOT the 'v1.0.0' tag form; the pre-push hook strips the 'v' prefix before
calling) and a RELEASES.json path (RELEASES_PATH env, default repo-root
RELEASES.json), exit 0 iff that version is recorded in RELEASES.json.

This is the testable kernel behind `core/githooks/pre-push`. It is FAIL-CLOSED
(the OPPOSITE of the post-commit daemon-recycle hook, which is fail-open): any
malformed or empty/missing manifest, or any unrecorded version, exits non-zero
so the push is refused. Rationale: a false-positive push block is annoying but
recoverable (RELEASE_FORCE_PUSH_TAG=1); a false-negative lets an unrecorded v*
tag escape to the remote — the exact M2 invariant violation this gate prevents.

It imports `_release_lib` directly (sibling in this dir) — NO daemon, NO
MIND_AGENT, NO _paths.sh. Git hooks fire in cold contexts where the full agent
environment may be absent, so this kernel must stand alone.

Exit codes: 0 = version recorded (push the tag); 1 = not recorded, empty/missing
manifest, malformed manifest, or usage error (all fail-closed → push refused).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# _release_lib is the sibling pure-logic module in this same directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _release_lib as L  # noqa: E402


def main(argv: list) -> int:
    if len(argv) != 1:
        print("usage: check-tag-in-releases.py <bare-semver>  "
              "(RELEASES_PATH env optional)", file=sys.stderr)
        return 1
    version = argv[0].strip()

    releases_path = os.environ.get("RELEASES_PATH", "").strip()
    if not releases_path:
        # repo root = this file's parent.parent.parent (core/scripts -> core -> root)
        releases_path = str(Path(__file__).resolve().parent.parent.parent / "RELEASES.json")

    try:
        releases = L.load_releases(releases_path)
    except ValueError:
        # Present-but-malformed manifest: fail closed — we cannot verify the tag.
        print(f"ERROR: RELEASES.json malformed ({releases_path}) — cannot verify "
              f"tag v{version}; refusing push", file=sys.stderr)
        return 1

    if not releases:
        # Empty/missing manifest. load_releases() treats this as a legitimate
        # bootstrap state (returns []), but the pre-push gate's job is different:
        # zero recorded releases means there should be zero v* tags being pushed.
        print(f"ERROR: RELEASES.json empty/missing ({releases_path}) — no release "
              f"recorded, so tag v{version} must not be pushed; refusing", file=sys.stderr)
        return 1

    recorded = {e["version"] for e in releases if e.get("version")}
    if version in recorded:
        return 0

    print(f"ERROR: tag v{version} has no entry in RELEASES.json — cut the release "
          f"first (bash core/scripts/release.sh); refusing push", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
