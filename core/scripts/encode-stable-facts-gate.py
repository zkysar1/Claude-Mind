#!/usr/bin/env python3
"""Encode-Stable-Facts Gate — enforce the three-probe threshold from
.claude/rules/encode-stable-facts.md.

Rule: if an agent runs 3+ discovery commands (SSH, ls, find, describe-*, list-*,
get-*, show-*) to locate a single resource, it should STOP and check whether the
resource has been encoded as a locator in world/conventions/ before probing
further. Re-discovering a stable fact every session is a learning failure.

Called INLINE from skills that know they are doing resource discovery — aspirations-
execute before Phase 4 resource-access steps, research-topic before infrastructure
probing, and any forged skill whose triggers include SSH/AWS CLI/describe-style
commands. The caller supplies the running probe count for the current resource.

Contract
  --resource-id <text>       required; canonical identifier (path fragment, ARN prefix,
                             URL host, service name, etc.)
  --probe-count <int>        required; how many discovery commands the caller has
                             already issued for this resource (pre-increment before
                             the next probe)
  --override "<text>"        fail-open with stderr audit line
  --threshold <int>          optional override of the 3-probe default (for skills
                             that have a documented reason to allow more — e.g.,
                             research-topic that expects broad exploration)

Exit codes
  0: pass — probe_count < threshold, OR the resource already has an encoded locator
     in world/conventions/, OR override was supplied, OR fail-open path (no world dir)
  1: block — probe_count >= threshold AND no locator found AND no override

Output (stdout, JSON)
  resource_id, probe_count, threshold, locator_found (bool),
  locator_file (path or null), would_block (bool), reason (short string)
"""

import argparse
import json
import os
import sys
from pathlib import Path


# --- Path resolution (mirrors capability-gate pattern) --------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

sys.path.insert(0, str(SCRIPT_DIR))
from _paths import agent_dir as _agent_dir  # noqa: E402


def _read_local_paths_conf(agent_name):
    conf = _agent_dir(agent_name) / "local-paths.conf"
    out = {}
    if not conf.is_file():
        return out
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _resolve_world_dir():
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    conf = _read_local_paths_conf(agent)
    world = conf.get("WORLD_PATH")
    return Path(world) if world else None


# --- Locator lookup -------------------------------------------------------------

def _find_locator(resource_id, world_dir):
    """Search world/conventions/*.md for any entry matching resource_id.

    Match heuristic: case-insensitive substring match against file contents.
    Returns (found, path) — path is the first file that contains the id, or None.

    This is deliberately loose: the rule's purpose is "prompt the agent to check
    before probing more," not rigid exact-match enforcement. A loose match + the
    agent's own judgment is the right balance.
    """
    if world_dir is None:
        return False, None
    conv_dir = world_dir / "conventions"
    if not conv_dir.is_dir():
        return False, None
    needle = resource_id.lower().strip()
    if not needle:
        return False, None
    for md in sorted(conv_dir.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if needle in text:
            return True, str(md)
    return False, None


# --- Gate entry -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Encode-stable-facts gate: block the 3rd+ discovery probe for an unencoded resource.",
    )
    parser.add_argument("--resource-id", required=True,
                        help="Canonical identifier for the resource being probed.")
    parser.add_argument("--probe-count", type=int, required=True,
                        help="Running count of discovery probes already issued for this resource.")
    parser.add_argument("--threshold", type=int, default=3,
                        help="Probe count at which the gate starts blocking (default 3).")
    parser.add_argument("--override",
                        help="Fail-open justification; logged to stderr for audit trail.")
    args = parser.parse_args()

    result = {
        "resource_id": args.resource_id,
        "probe_count": args.probe_count,
        "threshold": args.threshold,
        "locator_found": False,
        "locator_file": None,
        "would_block": False,
        "reason": "",
    }

    # Override: emit audit line, pass
    if args.override:
        print(f"[encode-stable-facts-gate] override applied: {args.override}", file=sys.stderr)
        result["reason"] = f"override: {args.override}"
        print(json.dumps(result))
        sys.exit(0)

    # Below threshold: pass
    if args.probe_count < args.threshold:
        result["reason"] = f"probe_count ({args.probe_count}) below threshold ({args.threshold}) — pass"
        print(json.dumps(result))
        sys.exit(0)

    # At/above threshold: check if a locator exists
    world_dir = _resolve_world_dir()
    if world_dir is None:
        # No world dir configured — fail-open (can't verify locator presence)
        result["reason"] = "no world dir resolved (no MIND_AGENT or no WORLD_PATH) — fail-open"
        print(f"[encode-stable-facts-gate] fail-open: world_dir unresolved", file=sys.stderr)
        print(json.dumps(result))
        sys.exit(0)

    found, path = _find_locator(args.resource_id, world_dir)
    result["locator_found"] = found
    result["locator_file"] = path

    if found:
        result["reason"] = (
            f"probe_count ({args.probe_count}) at/above threshold ({args.threshold}) "
            f"BUT locator found in {Path(path).name} — pass (use the encoded value)"
        )
        print(json.dumps(result))
        sys.exit(0)

    # Block: threshold hit, no locator
    result["would_block"] = True
    result["reason"] = (
        f"probe_count ({args.probe_count}) at/above threshold ({args.threshold}) "
        f"and no locator for '{args.resource_id}' in {world_dir}/conventions/. "
        f"STOP probing — encode the discovered value as a locator before continuing. "
        f"See .claude/rules/encode-stable-facts.md and "
        f"core/config/conventions/resource-locators.md."
    )
    print(json.dumps(result))
    sys.exit(1)


if __name__ == "__main__":
    main()
