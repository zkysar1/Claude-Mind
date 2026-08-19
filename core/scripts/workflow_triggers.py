# Does any checked-out workflow file declare a given trigger for a given ref?
#
# WHY THIS IS A SHARED MODULE (, and  which needs the same
# parse pointed the other way):
#   * product-pr-flow.sh step 6b asks "is a run EXPECTED for this PR base?" so it
#     can WAIT instead of accepting an empty check set and merging unverified.
#   * deploy-verify.sh asks the inverse — "is this ref untriggerable?" — so it can
#     exit at once instead of polling for a run that can never appear.
# One predicate answers both. It lives here, in framework scope, because both
# callers are separate scripts in separate trees and neither can import the other.
#
# WHY FILES AND NOT THE API. `gh api repos/<o>/<r>/actions/workflows` reports
# workflows with state=active for a repo whose `.github/` no longer exists — a
# live registration outliving its file (guard-119, measured). An API-only probe
# therefore claims triggers that no file declares. The checked-out files are the
# authoritative declaration, and every caller already has them on disk. The API
# is corroboration, never the source.
#
# THE YAML 1.1 TRAP THAT MAKES A NAIVE PARSE REPORT ZERO TRIGGERS ESTATE-WIDE:
# `on` is a YAML 1.1 boolean, so a bare `on:` key loads as the Python bool True,
# NOT the string "on". `doc.get("on")` returns None for every ordinary workflow
# file ever written. Read `doc.get("on", doc.get(True))`. Measured across 56
# repos: with the bool fallback, 7 declare a pull_request arm and 0 files fail to
# parse; without it, the answer is 0 everywhere and looks like a clean negative.

from __future__ import annotations

import fnmatch
import glob
import json
import os
from typing import Any

WORKFLOW_GLOBS = ("*.yml", "*.yaml")


def _load_yaml(path: str) -> Any:
    import yaml  # deferred: callers that never parse should not pay the import

    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _on_block(doc: Any) -> Any:
    """Return the `on:` mapping, tolerating the YAML 1.1 bool-key form."""
    if not isinstance(doc, dict):
        return None
    return doc.get("on", doc.get(True))


def _ref_matches(ref: str, patterns: Any) -> bool:
    """GitHub branch filters are globs. An absent filter matches every ref."""
    if patterns is None:
        return True
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list):
        return True
    for pat in patterns:
        if not isinstance(pat, str):
            continue
        # GitHub `**` crosses `/`; fnmatch `*` already does, so the collapse is
        # safe in the permissive direction and never invents a match for a ref
        # that shares no prefix.
        if fnmatch.fnmatch(ref, pat.replace("**", "*")):
            return True
    return False


def _event_spec(on_block: Any, event: str) -> tuple[bool, Any]:
    """(event_present, its config). Handles the str / list / dict forms of `on`."""
    if on_block is None:
        return (False, None)
    if isinstance(on_block, str):
        return (on_block == event, None)
    if isinstance(on_block, list):
        return (event in on_block, None)
    if isinstance(on_block, dict):
        if event not in on_block:
            return (False, None)
        return (True, on_block.get(event))
    return (False, None)


def declares_trigger(workflow_dir: str, event: str, ref: str) -> dict:
    """Does any workflow under `workflow_dir` fire `event` for `ref`?

    Returns a dict with `declared` (bool), `matches` (list of per-file records),
    `files_scanned` (int) and `unparseable` (list). An unparseable file is
    REPORTED rather than silently skipped: a caller deciding whether to wait on
    CI must be able to tell "no trigger declared" from "I could not read the
    declaration" (guard-1760 — a checker and a clean result must never look the
    same).
    """
    files: list[str] = []
    for pattern in WORKFLOW_GLOBS:
        files.extend(glob.glob(os.path.join(workflow_dir, pattern)))
    files = sorted(set(files))

    matches: list[dict] = []
    unparseable: list[dict] = []

    for path in files:
        try:
            doc = _load_yaml(path)
        except Exception as exc:  # unreadable / invalid YAML / no pyyaml
            unparseable.append({"file": os.path.basename(path), "error": str(exc)[:160]})
            continue

        present, spec = _event_spec(_on_block(doc), event)
        if not present:
            continue

        branches = None
        ignore = None
        if isinstance(spec, dict):
            branches = spec.get("branches")
            ignore = spec.get("branches-ignore")

        if ignore is not None and _ref_matches(ref, ignore):
            continue  # explicitly excluded for this ref
        if not _ref_matches(ref, branches):
            continue

        matches.append(
            {
                "file": os.path.basename(path),
                "event": event,
                "branches": branches if branches is not None else "*",
            }
        )

    return {
        "declared": bool(matches),
        "matches": matches,
        "files_scanned": len(files),
        "unparseable": unparseable,
        "workflow_dir": workflow_dir,
        "event": event,
        "ref": ref,
    }


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Report whether a checked-out workflow declares an event trigger for a ref."
    )
    ap.add_argument("--repo-root", default=".", help="repo root containing .github/workflows")
    ap.add_argument("--event", default="pull_request", help="workflow event name")
    ap.add_argument("--ref", required=True, help="branch the trigger must match (the PR BASE)")
    ap.add_argument("--json", action="store_true", help="emit the full result as JSON")
    args = ap.parse_args(argv)

    wf_dir = os.path.join(args.repo_root, ".github", "workflows")
    try:
        if not os.path.isdir(wf_dir):
            result = {
                "declared": False,
                "matches": [],
                "files_scanned": 0,
                "unparseable": [],
                "workflow_dir": wf_dir,
                "event": args.event,
                "ref": args.ref,
                "detail": "no workflows directory",
            }
        else:
            result = declares_trigger(wf_dir, args.event, args.ref)
    except Exception as exc:
        # AN INTERNAL ERROR MUST NOT LOOK LIKE "no trigger declared".
        # Python exits 1 on an uncaught exception, and 1 is this CLI's
        # "not declared" verdict -- so a crash would be byte-identical to a
        # clean negative. The caller (product-pr-flow.sh step 6b.1) reads 1 as
        # "no run is expected" and merges immediately, with stderr suppressed,
        # which silently restores the exact race this module exists to close.
        # Measured 2026-08-12 by injecting a RuntimeError: crash rc=1, healthy
        # rc=0. Degrade to 2 (unknown) instead, so the caller waits.
        # This is the same unknown-is-not-absent rule the docstring states for
        # unparseable files; it was missing for the module's own failure.
        print(json.dumps({
            "declared": False,
            "matches": [],
            "files_scanned": 0,
            "unparseable": [{"file": "<internal>", "error": str(exc)[:160]}],
            "workflow_dir": wf_dir,
            "event": args.event,
            "ref": args.ref,
            "detail": "internal error -- verdict unknown, not absent",
        }))
        return 2

    if args.json:
        print(json.dumps(result))
    else:
        print("declared" if result["declared"] else "not-declared")

    # Exit 2 when the answer is unknowable (a file exists but could not be read)
    # AND nothing else matched — the caller must not read that as "not declared".
    if result["unparseable"] and not result["declared"]:
        return 2
    return 0 if result["declared"] else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
