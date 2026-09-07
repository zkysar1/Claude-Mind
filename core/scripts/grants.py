#!/usr/bin/env python3
"""Grant store CLI — the script API for the GRANT entity (,  P3).

`_grants.py` is the PURE policy SSOT: it decides, it never touches a store, and
it deliberately imports no `_fileops` so a gate evaluating policy can never bind
a caller's storage backend. This file is the other half — the one that DOES the
IO — and the split is why `_grants` can stay pure while writes still get
locking, history and changelog like every other governed store.

Subcommands:
  add          create a grant (JSON record on STDIN) — G1-G5 validated in-lock
  list         query grants as data (--from-env / --to-env / --status / --covering)
  memberships  the environment-worlds a base world is a member of
  check        evaluate whether from_env may influence to_env (optionally at a node)
  reach        membership joined against the deployment registry (seed list + dangling)

MEMBERSHIP IS NOT A SECOND CONCEPT. An agent's base is a world, and holding an
active grant into an environment-world IS membership in it. `memberships` is a
projection of the same rows `list` and `check` read, so there is no parallel
store to drift against — see core/config/conventions/grants.md.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

import _grants as G  # noqa: E402
from _fileops import locked_append_jsonl_with_allocator, next_id_for_prefix  # noqa: E402
from _paths import WORLD_DIR, assert_world_dir  # noqa: E402


def _store_path(args) -> Path:
    """Resolve the store. --store overrides (tests, audits of a peer's export)."""
    if getattr(args, "store", None):
        return Path(args.store)
    assert_world_dir("grants.py")
    return G.default_store_path(WORLD_DIR)


def _load(path):
    grants, err = G.load_grants(path)
    if err:
        # A dependency failure, not a policy answer. Never let it read as "no
        # grants" — that is exactly the empty-vs-unreadable conflation
        # _grants.load_grants documents, and here it would understate access.
        print(json.dumps({"error": "store_unreadable", "detail": err}), file=sys.stderr)
        sys.exit(1)
    return grants


def cmd_add(args) -> int:
    # STDIN, never argv (guard-979 / guard-2037). A TTY here means the caller
    # passed the record as a flag or positionally and this process would block
    # forever on a stdin that never closes — the measured 14-minute wedge. Fail
    # fast and name the correct call shape instead of hanging.
    if sys.stdin.isatty():
        print(json.dumps({
            "error": "no_stdin",
            "detail": "grants.py add reads the grant record as JSON on STDIN. "
                      "Use: grants.sh add < record.json  (never a --flag or a "
                      "positional payload).",
        }), file=sys.stderr)
        return 2
    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"error": "invalid_body", "detail": "empty body"}),
              file=sys.stderr)
        return 2
    try:
        rec = json.loads(raw)
    except Exception as e:
        print(json.dumps({"error": "invalid_body", "detail": str(e)}),
              file=sys.stderr)
        return 2
    if not isinstance(rec, dict):
        print(json.dumps({"error": "invalid_body",
                          "detail": "grant record must be a JSON object"}),
              file=sys.stderr)
        return 2

    path = _store_path(args)
    violations_box = []

    def _build(items):
        # EVERYTHING that depends on existing state happens HERE, inside the
        # lock (guard-480): the id allocation, the G3 first-grant-from-this-
        # source test, and the G4 cycle check all read `items`. Validating
        # before the lock would decide against a snapshot a concurrent writer
        # can invalidate — the read-then-write race that lets two callers each
        # believe they are the first grant from a source, so neither needs the
        # human approval G3 exists to require.
        out = dict(rec)
        out.setdefault("status", "active")
        if out.get("from_env") and not out.get("origin_env"):
            out["origin_env"] = out["from_env"]      # G5 default, still validated
        if not out.get("grant_id"):
            # id_field="grant_id" is LOAD-BEARING, not decoration: this store's
            # key is `grant_id`, while next_id_for_prefix defaults to `id`. With
            # the default it scans for a field no grant record has, matches
            # nothing, and returns "grant-1" on EVERY call — measured here on
            # the second grant into a store that already held grant-1. It fails
            # silently (a plausible id, rc=0, the row lands), so only a
            # multi-row test catches it.
            out["grant_id"] = next_id_for_prefix(items, "grant",
                                                 id_field="grant_id")
        # Dup-check UNCONDITIONALLY, on the final id, whoever produced it. The
        # earlier form checked only caller-supplied ids, so an allocator bug —
        # exactly the one above — could mint a collision that nothing refused.
        # An allocator is not a trusted source; it is just another writer.
        if any(isinstance(g, dict) and g.get("grant_id") == out["grant_id"]
               for g in items):
            violations_box.append("G0: grant_id %r already exists"
                                  % out["grant_id"])
            return None
        out.setdefault("scope", G.ROOT_SCOPE)
        out.setdefault("created", datetime.now().isoformat(timespec="seconds"))
        v = G.validate_new_grant(out, items)
        if v:
            violations_box.extend(v)
            return None
        return out

    try:
        written = locked_append_jsonl_with_allocator(path, _build)
    except Exception as e:
        if violations_box:
            print(json.dumps({"error": "grant_refused",
                              "violations": violations_box}, indent=2))
            return 2
        print(json.dumps({"error": "write_failed", "detail": str(e)}),
              file=sys.stderr)
        return 1
    if violations_box or written is None:
        print(json.dumps({"error": "grant_refused",
                          "violations": violations_box}, indent=2))
        return 2
    print(json.dumps(written, indent=2, ensure_ascii=False))
    return 0


def cmd_list(args) -> int:
    grants = _load(_store_path(args))
    rows = G.query_grants(grants, from_env=args.from_env, to_env=args.to_env,
                          covering=args.covering,
                          status=(None if args.status == "any" else args.status))
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def cmd_memberships(args) -> int:
    grants = _load(_store_path(args))
    envs = G.memberships(grants, args.base_env,
                         status=(None if args.status == "any" else args.status))
    # Report the GATE's verdict beside each membership, from the same rows.
    # Outcome[2] of  asks that membership not become a second truth to
    # drift against, and the drift this prevents is REAL, not hypothetical: an
    # active edge and an authorized edge are not the same set today, because
    # `validate_new_grant` requires a human approver only for the FIRST grant
    # out of a source while `check_influence` requires one on EVERY grant at use
    # time. So a legitimately-created second grant is a membership the gate
    # denies. That asymmetry is _grants.py's to settle, not this resolver's —
    # and it fails CLOSED, which is the safe direction — but a resolver that
    # printed only the edge would let a reader believe access exists that the
    # gate refuses. Printing both makes any disagreement visible instead of
    # silent, which is the property outcome[2] is actually asking for.
    verdicts = {e: G.check_influence(args.base_env, e, grants) for e in envs}
    print(json.dumps({
        "base_env": args.base_env,
        "environments": envs,
        "count": len(envs),
        # The scopes each membership carries, so a reader can tell a whole-tree
        # membership from a subtree one without a second call.
        "scopes": {e: G.readable_scopes(grants, args.base_env, e) for e in envs},
        "influence": {e: {"verdict": v["verdict"], "guardrail": v.get("guardrail"),
                          "reason": v.get("reason")}
                      for e, v in verdicts.items()},
        "authorized": sorted([e for e, v in verdicts.items()
                              if v["verdict"] == G.ALLOW]),
    }, indent=2, ensure_ascii=False))
    return 0


def _registered_envs(args):
    """The environment ids this deployment can address, plus where they came from.

    --registered-env overrides the registry read, mirroring --store exactly: a
    test, or an audit of a peer's export, must not depend on THIS box's
    `core/config/environments/`. Returns every registered id including this
    world's own -- `G.reach` owns the "a world is not its own peer" exclusion, so
    it is applied in one place rather than two.
    """
    override = getattr(args, "registered_env", None)
    if override:
        return sorted(set(override)), "flag"
    from _peer_registry import load_env_registry  # noqa: E402
    return sorted(load_env_registry().keys()), "registry"


def cmd_reach(args) -> int:
    grants = _load(_store_path(args))
    registered, registry_source = _registered_envs(args)
    r = G.reach(grants, args.base_env, registered,
                status=(None if args.status == "any" else args.status))
    # Gate verdict beside membership, for the same reason cmd_memberships prints
    # it: an ACTIVE edge and an AUTHORIZED edge are not the same set today (the
    # G3 creation-vs-use asymmetry documented in grants.md), and a reach report
    # that showed only edges would let a reader believe in access the gate
    # refuses. Only member_of is evaluated -- an ungranted or dangling env has no
    # edge to evaluate, and calling the gate on one would report DENY as though a
    # decision had been made about it.
    verdicts = {e: G.check_influence(args.base_env, e, grants)
                for e in r["member_of"]}
    print(json.dumps({
        "base_env": args.base_env,
        "registry_source": registry_source,
        "registered_count": len(registered),
        "member_of": r["member_of"],
        "ungranted": r["ungranted"],
        "dangling": r["dangling"],
        "authorized": sorted([e for e, v in verdicts.items()
                              if v["verdict"] == G.ALLOW]),
        # The count world-contract.md's "seed, THEN arm" order keys on: while
        # this is non-zero, arming refusal severs relationships that are live.
        "seed_required": len(r["ungranted"]),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_check(args) -> int:
    result = G.evaluate(args.from_env, args.to_env, _store_path(args),
                        node_key=args.node)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    # 0 allow, 3 deny, 4 unavailable (fail-open: the caller proceeds and logs).
    # DENY and UNAVAILABLE get DIFFERENT codes on purpose — collapsing them is
    # the guard-142-vs-G1 confusion _grants.py warns about, and a caller that
    # cannot tell them apart must either ignore a real refusal or block on its
    # own plumbing fault.
    return {G.ALLOW: 0, G.DENY: 3}.get(result.get("verdict"), 4)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="grants.py",
        description="GRANT store script API (schema: core/config/conventions/grants.md)")
    ap.add_argument("--store", help="override the store path (tests, peer exports)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="create a grant; JSON record on STDIN")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("list", help="query grants as data")
    p.add_argument("--from-env", dest="from_env")
    p.add_argument("--to-env", dest="to_env")
    p.add_argument("--covering", help="only grants whose scope covers this node key")
    p.add_argument("--status", default="active",
                   help="'active' (default), any literal status, or 'any'")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("memberships",
                       help="environment-worlds a base world is a member of")
    p.add_argument("--base-env", dest="base_env", required=True)
    p.add_argument("--status", default="active",
                   help="'active' (default) or 'any' for an audit read")
    p.set_defaults(fn=cmd_memberships)

    p = sub.add_parser("reach",
                       help="membership joined against the deployment registry")
    p.add_argument("--base-env", dest="base_env", required=True)
    p.add_argument("--status", default="active",
                   help="'active' (default) or 'any' for an audit read")
    p.add_argument("--registered-env", dest="registered_env", action="append",
                   help="override the registry (repeatable; tests, peer exports)")
    p.set_defaults(fn=cmd_reach)

    p = sub.add_parser("check", help="may from_env influence to_env?")
    p.add_argument("--from-env", dest="from_env", required=True)
    p.add_argument("--to-env", dest="to_env", required=True)
    p.add_argument("--node", help="knowledge-tree node key to scope-check")
    p.set_defaults(fn=cmd_check)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
