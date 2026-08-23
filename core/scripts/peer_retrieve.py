#!/usr/bin/env python3
"""Cross-world retrieval -- answer from the union of what the fleet knows.

Retrieval in this framework is single-world by construction: `retrieve.py` reads
`WORLD_PATH` and nothing else. An answer that lives in a peer deployment is
invisible here even when both deployments are perfectly healthy. This module is
the READ counterpart to `peer_board_post.py`: that one WRITES to a peer, this one
READS across every world in the registry.

THE ONE INVARIANT THAT MAKES THE OUTPUT USABLE
----------------------------------------------
"The peer had nothing" and "I could not reach the peer" are DIFFERENT ANSWERS and
must never render the same way. Peer reachability is box-dependent, so a
cross-world retrieval that reports absence indistinguishably from unreachability
is worse than no cross-world retrieval at all -- it manufactures confident
negatives (`verify-before-assuming.md`). Therefore every world carries TWO
orthogonal fields:

    status        hit | empty | unreachable    -- did I find anything?
    completeness  complete | partial           -- did I see everything?

`completeness == "partial"` means at least one lane could not be read, so
absence in that world is NOT evidence of absence. The renderer prints the word
UNREACHABLE for those worlds; `render()` is pinned by tests so a partial world
can never be rendered as a plain "no matches".

READ-ONLY, NOT IMPORT/MERGE (world-contract.md G1-G5)
-----------------------------------------------------
This proposes READ-ONLY peer retrieval. Nothing read here is written into this
world's stores -- no tree node, no reasoning-bank entry, no guardrail, no goal.
Peer content is returned to the CALLER, attributed to its origin world, and
discarded when the process exits. That choice follows from the guardrails:

  G1 (default-private)      New worlds start with zero GRANTs. Import would need
                            an inbound influence GRANT; reading what a peer
                            already published to a shared channel does not.
  G2 (goal sandboxing)      Injected goals execute in the TARGET world's sandbox.
                            Merging peer knowledge into local stores would let a
                            peer's claims execute as OUR premises, outside any
                            sandbox -- exactly what G2 forbids for tasks.
  G3 (human approval gate)  The first INFLUENCE grant from A to B needs explicit
                            human approval from B's owner. No agent may consent
                            on its owner's behalf, so an autonomous import is
                            unauthorizable by construction.
  G4 (rate/depth/cycle)     Import makes influence TRANSITIVE: peer knowledge
                            merged here would propagate onward as if native
                            (A->B->C without an A->C grant). A read that stays in
                            the caller's context has depth 1 and cannot cycle.
  G5 (provenance)           Every returned record keeps `origin_env` and its
                            source ref. Merging strips origin at the moment it
                            matters most -- once a peer claim is a local tree
                            node, nothing downstream can tell it from ours.

The GRANT entity does not exist in any hosted-store schema yet; G1-G5 are design
artifacts, not built enforcement. Read-only is the posture that stays correct
whether or not enforcement ever lands -- it needs no grant to be legitimate.

STORAGE BACKEND IS NEVER INHERITED (guard-955 / rb-2983 class)
-------------------------------------------------------------
This module NEVER imports `_fileops`, and `assert_no_fileops()` proves it at
runtime. `_fileops` binds a storage backend at import time, so a peer read that
imported it would silently transact against the CALLER's bucket using the PEER's
key shape -- the truncation class of 2026-07-09. A peer whose world is not
reachable as a filesystem path on this box is reported `unreachable` with its
DECLARED backend named in the reason. It is never read through ours.
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _peer_registry import load_env_registry, peer_envs  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PARTIAL = 3  # ran fine, but >=1 world could not be fully read

STATUS_HIT = "hit"
STATUS_EMPTY = "empty"
STATUS_UNREACHABLE = "unreachable"

COMPLETE = "complete"
PARTIAL = "partial"

# Stores searched per world -- all world-contract elements: board channels are
# "Signals/events"; the knowledge tree, conventions, reasoning bank and
# guardrails are "State / data objects". The two JSONL stores joined
# 2026-08-21: they hold the hardest-won lessons (incidents, prescriptive
# rules) and were previously invisible here, so a peer's incident knowledge
# could only arrive if the peer happened to publish it to a board.
SKIP_BOARD_SUFFIXES = ("-reads.jsonl",)
BOARD_TEXT_FIELDS = ("text", "subject", "summary")
WORLD_JSONL_STORES = (("reasoning-bank.jsonl", "reasoning-bank"),
                      ("guardrails.jsonl", "guardrails"))
RECORD_TEXT_FIELDS = ("title", "rule", "content", "when_to_use", "trigger_condition")


def _die(code, msg):
    sys.stderr.write("[peer-retrieve] %s\n" % msg)
    sys.exit(code)


def assert_no_fileops():
    """Runtime proof that no storage-backend client was constructed.

    `_fileops` resolves STORAGE_BACKEND at import. If it is in sys.modules after
    a peer read, the read may have transacted against the caller's backend. This
    is the non-inheritance proof the design constraint asks for -- checkable, not
    asserted in prose.
    """
    return "_fileops" not in sys.modules


def _terms(query):
    return [t for t in str(query or "").lower().split() if t]


def _matches(haystack, terms):
    return _matches_low(haystack.lower(), terms)


def _matches_low(low, terms):
    return all(t in low for t in terms)


def _score(low, terms):
    """Rank a matched doc. AND-matching decides WHAT is a hit (unchanged --
    the `empty`-licenses-a-negative contract must neither widen nor narrow);
    this decides only ORDER. Pre-2026-08-21 the order was glob order with an
    early break at the limit, so the alphabetically-first N matches
    masqueraded as the best N. Term frequency is capped at 3 per term (a
    spammy doc must not swamp the slate); the whole query appearing as an
    adjacent phrase earns a flat bonus."""
    s = 0.0
    for t in terms:
        s += min(low.count(t), 3)
    if len(terms) > 1 and " ".join(terms) in low:
        s += 2.0
    return s


def _snippet(text, terms, width=180):
    flat = " ".join(str(text or "").split())
    low = flat.lower()
    pos = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
    start = max(0, pos - 40)
    out = flat[start:start + width]
    return ("..." + out) if start else out


def _iter_board_rows(world_dir, include_archives=False, errors=None):
    for path in sorted(glob.glob(os.path.join(str(world_dir), "board", "*.jsonl"))):
        base = os.path.basename(path)
        if any(base.endswith(s) for s in SKIP_BOARD_SUFFIXES):
            continue
        if not include_archives and "-archive" in base:
            continue
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            # A source we could not read is NOT a source with nothing in it.
            # Swallowing this silently is the module's own thesis violated
            # (guard-4093) -- record it so the lane can report itself INCOMPLETE.
            if errors is not None:
                errors.append(os.path.basename(path))
            continue
        with fh:
            parsed = 0
            failed = 0
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    failed += 1
                    continue
                if isinstance(row, dict):
                    parsed += 1
                    yield base, row
                else:
                    failed += 1
            # A file that OPENED but whose every line was unparseable is a source
            # we could not READ -- not a source with nothing in it. The OSError
            # branch above never fires here because the open SUCCEEDED, so this is
            # the same collapse one layer in: before this, a wholly-corrupt board
            # file reported `empty` / `complete` / `unreadable: []` / rc=0.
            # The predicate is `failed and not parsed` deliberately. A single torn
            # tail line is normal in an append-only JSONL store and MUST NOT flag
            # (it always has >=1 good row before it), and any ratio threshold would
            # be arbitrary. An EMPTY file is parsed=0 failed=0 and correctly stays
            # clean -- it genuinely has nothing. This runs only when the
            # generator is EXHAUSTED; since the 2026-08-21 rank-then-truncate
            # change every caller exhausts it (no early break), so the check
            # now fires on every scan.
            if failed and not parsed and errors is not None:
                errors.append("%s (no parseable rows in %d line(s))" % (base, failed))


def _board_text(row):
    return " ".join(str(row.get(f) or "") for f in BOARD_TEXT_FIELDS)


def _search_docs(world_dir, terms, subdir, pattern, store, errors=None):
    hits = []
    root = os.path.join(str(world_dir), subdir)
    if not os.path.isdir(root):
        return hits
    for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
        try:
            body = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            if errors is not None:
                errors.append(os.path.relpath(path, str(world_dir)))
            continue
        low = body.lower()
        if _matches_low(low, terms):
            hits.append({
                "store": store,
                "ref": os.path.relpath(path, str(world_dir)),
                "author": None,
                "snippet": _snippet(body, terms),
                "score": _score(low, terms),
            })
    return hits


def _search_jsonl_store(world_dir, terms, filename, store, errors=None):
    """Record-wise search of a top-level JSONL store (reasoning bank,
    guardrails). Matching runs on the RAW line -- a JSONL line IS the
    record's full text, so category/tags fields can hit without a field
    list; every line is parsed regardless for the torn-line accounting.
    Non-active records are dropped after matching. Torn-line policy mirrors
    _iter_board_rows: a single torn tail line is normal in an append-only
    store; a file that OPENED but parsed nothing is UNREADABLE, not empty.
    An ABSENT store file is legitimately empty (fresh worlds carry no
    reasoning bank yet) -- absence is not flagged."""
    path = os.path.join(str(world_dir), filename)
    hits = []
    if not os.path.exists(path):
        return hits
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        if errors is not None:
            errors.append(filename)
        return hits
    with fh:
        parsed = 0
        failed = 0
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                failed += 1
                continue
            if not isinstance(rec, dict):
                failed += 1
                continue
            parsed += 1
            low = line.lower()
            if not _matches_low(low, terms):
                continue
            if str(rec.get("status", "active")) != "active":
                continue
            text = " ".join(str(rec.get(f) or "") for f in RECORD_TEXT_FIELDS)
            hits.append({
                "store": store,
                "ref": rec.get("id"),
                "author": None,
                "snippet": _snippet(text or line, terms),
                "score": _score(low, terms),
            })
        if failed and not parsed and errors is not None:
            errors.append("%s (no parseable rows in %d line(s))" % (filename, failed))
    return hits


def search_world_dir(world_dir, terms, limit=5, include_archives=False):
    """Search one world directory. Pure filesystem read -- no backend client.

    Returns (results, unreadable) where `unreadable` names every source that
    could not be opened. Callers MUST treat a non-empty `unreadable` as making
    the lane INCOMPLETE: a world whose files were partly unreadable has not been
    searched, however many hits the readable part happened to yield.
    """
    cands = []
    unreadable = []
    for base, row in _iter_board_rows(world_dir, include_archives, errors=unreadable):
        blob = _board_text(row)
        low = blob.lower()
        if blob and _matches_low(low, terms):
            cands.append({
                "store": "board/" + base,
                "ref": row.get("id"),
                "author": row.get("author"),
                "snippet": _snippet(blob, terms),
                "score": _score(low, terms),
            })
    cands += _search_docs(world_dir, terms, os.path.join("knowledge", "tree"),
                          "**/*.md", "knowledge-tree", errors=unreadable)
    cands += _search_docs(world_dir, terms, "conventions", "*.md",
                          "conventions", errors=unreadable)
    for filename, store in WORLD_JSONL_STORES:
        cands += _search_jsonl_store(world_dir, terms, filename, store,
                                     errors=unreadable)
    # Rank globally across stores, THEN truncate. Pre-2026-08-21 the limit was
    # applied per-store in scan order (board first), so five early board rows
    # could shut out an exact tree/convention/reasoning-bank match entirely.
    # Deterministic tie-break so identical corpora rank identically everywhere.
    cands.sort(key=lambda r: (-r["score"], r["store"], str(r["ref"] or "")))
    return cands[:limit], unreadable


def resolve_peer_world_dir(env_id, registry_entry):
    """Filesystem path to a peer's world, or None.

    Two sources, env var first so a box can point at a peer mount without editing
    the shared registry. Mirrors peer_board_post.peer_world_path deliberately --
    the two directions must agree on what "reachable" means.
    """
    var = "PEER_WORLD_" + env_id.upper().replace("-", "_")
    raw = os.environ.get(var, "").strip()
    if not raw:
        raw = str((registry_entry or {}).get("peer_world_path", "") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def direct_lane(env_id, registry_entry, terms, limit, include_archives):
    """Read the peer's own world store, or explain precisely why we cannot.

    NEVER falls back to the caller's storage backend. When the peer's world is
    not a directory on this box, the peer's DECLARED backend is named in the
    reason and `backend_used` stays null -- the read does not happen at all.
    """
    declared = str((registry_entry or {}).get("backend", "") or "") or None
    world_dir = resolve_peer_world_dir(env_id, registry_entry)
    if world_dir is not None:
        results, unreadable = search_world_dir(world_dir, terms, limit, include_archives)
        return {
            "lane": "direct",
            "status": STATUS_HIT if results else STATUS_EMPTY,
            "complete": not unreadable,
            "unreadable": unreadable,
            "count": len(results),
            "results": results,
            "access": "filesystem",
            "world_dir": str(world_dir),
            "backend_declared": declared,
            "backend_used": None,
            "reason": (None if not unreadable else
                       "%d source(s) in this world could not be opened (%s) -- the world was "
                       "reached but NOT fully searched" % (len(unreadable), ", ".join(unreadable[:5]))),
        }
    if declared is None:
        reason = ("registry entry for '%s' declares no `backend:` -- refusing to guess "
                  "(peer_board_post.py takes the same position on the write side)" % env_id)
    elif declared == "local":
        reason = ("peer '%s' declares backend 'local' and no peer_world_path is configured "
                  "on this box -- a local-backend peer's world lives on ITS machine and is "
                  "not addressable from here" % env_id)
    else:
        reason = ("peer '%s' declares backend '%s'; reading it needs that backend pinned in a "
                  "separate process (peer_board_post.py's _force_peer_backend shape). This "
                  "reader does not construct store clients, so it refuses rather than read "
                  "the peer's key shape through THIS world's backend (guard-955)"
                  % (env_id, declared))
    return {
        "lane": "direct",
        "status": STATUS_UNREACHABLE,
        "complete": False,
        "unreadable": [],
        "count": 0,
        "results": [],
        "access": None,
        "world_dir": None,
        "backend_declared": declared,
        "backend_used": None,
        "reason": reason,
    }


def channel_lane(self_world_dir, env_id, terms, limit, include_archives):
    """Peer-origin content that already arrived HERE via the cross-deployment channel.

    Authored `<agent>@<env-id>` on this world's board. Always reachable (it is a
    local read), and it is genuinely the peer's content -- but it is only what the
    peer CHOSE to publish, so it can never stand in for the direct lane.
    """
    suffix = "@" + env_id
    cands = []
    unreadable = []
    for base, row in _iter_board_rows(self_world_dir, include_archives, errors=unreadable):
        author = str(row.get("author") or "")
        if not author.endswith(suffix):
            continue
        blob = _board_text(row)
        low = blob.lower()
        if blob and _matches_low(low, terms):
            cands.append({
                "store": "board/" + base,
                "ref": row.get("id"),
                "author": author,
                "snippet": _snippet(blob, terms),
                "score": _score(low, terms),
            })
    cands.sort(key=lambda r: (-r["score"], r["store"], str(r["ref"] or "")))
    results = cands[:limit]
    return {
        "lane": "channel",
        "status": STATUS_HIT if results else STATUS_EMPTY,
        "complete": not unreadable,
        "unreadable": unreadable,
        "count": len(results),
        "results": results,
        "access": "local-inbound",
        "reason": (None if not unreadable else
                   "%d local board file(s) could not be opened (%s) -- inbound peer traffic "
                   "was NOT fully searched" % (len(unreadable), ", ".join(unreadable[:5]))),
    }


def _aggregate(lanes):
    """status answers 'did I find anything'; completeness answers 'did I see everything'.

    Keeping them separate is the whole point: a world can legitimately be `hit` and
    `partial` at once, and collapsing that into one field is how 'nothing found'
    starts impersonating 'could not look'.

    THE ORDER OF THE ELIF MATTERS AND THE OBVIOUS ORDER IS WRONG. The first draft
    read `elif ALL lanes unreachable -> unreachable`, which looks right and is not:
    a peer whose direct lane was unreachable and whose channel lane was merely
    quiet then aggregated to `empty` -- a world nobody could read, reporting a
    clean negative. That is the exact ambiguity this module exists to prevent,
    reproduced inside its own aggregator, and only a test caught it.

    So: `empty` is the ONLY status that licenses a negative conclusion, and it is
    unreachable-free BY CONSTRUCTION. Finding nothing while any lane was blind is
    not emptiness -- it is unreachability that happened to find nothing elsewhere.
    """
    # A lane is BLIND if it could not be read at all (unreachable) OR if it was
    # reached but some of its sources would not open (`complete: False`). Both
    # mean the same thing to a reader: this lane's silence proves nothing. The
    # second case was found by fresh-eyes on this very module -- an unreadable
    # file inside a REACHABLE peer world was silently skipped, so the world
    # reported `empty` / `complete` / rc=0 over content nobody had read.
    blind = [l for l in lanes if l["status"] == STATUS_UNREACHABLE or not l.get("complete", True)]
    if any(l["status"] == STATUS_HIT for l in lanes):
        status = STATUS_HIT
    elif blind:
        status = STATUS_UNREACHABLE
    else:
        status = STATUS_EMPTY
    completeness = PARTIAL if blind else COMPLETE
    return status, completeness


def retrieve(query, self_env=None, self_world=None, registry=None,
             limit=5, include_archives=False):
    terms = _terms(query)
    if not terms:
        raise ValueError("query is empty")
    # A limit below 1 stops every lane before it reads its first row, and the
    # result was `status: empty` / `completeness: complete` / rc=0 -- precisely
    # what retrieval-escalation.md Tier 2.5 tells readers is an EARNED negative.
    # Measured 2026-08-17 (bravo, cc-05) against three reachable worlds holding
    # matching content: `--limit 5` returned 11 matches, and `--limit 0` on the
    # same box, worlds and query rendered "read OK, no matches" for every world,
    # both at rc=0. Refusing is the only honest answer -- there is no result to
    # report from a search that was never permitted to look. This guard sits
    # BEFORE any I/O so the CLI usage exit stays hermetic and instant.
    if limit < 1:
        raise ValueError("--limit must be >= 1 (got %r): a limit below 1 reads nothing, so "
                         "any 'empty' it reported would be a negative conclusion drawn from "
                         "a search that never ran" % (limit,))
    registry = registry if registry is not None else load_env_registry()
    self_env = self_env or os.environ.get("ENVIRONMENT_ID", "").strip() or None
    self_world = self_world or os.environ.get("WORLD_PATH", "").strip() or None

    worlds = []

    self_entry = registry.get(self_env) if self_env else None
    if self_world and os.path.isdir(str(self_world)):
        res, unreadable = search_world_dir(self_world, terms, limit, include_archives)
        lane = {"lane": "direct", "status": STATUS_HIT if res else STATUS_EMPTY,
                "complete": not unreadable, "unreadable": unreadable,
                "count": len(res), "results": res, "access": "filesystem",
                "world_dir": str(self_world),
                "backend_declared": str((self_entry or {}).get("backend", "") or "") or None,
                "backend_used": None,
                "reason": (None if not unreadable else
                           "%d source(s) in THIS world could not be opened (%s)"
                           % (len(unreadable), ", ".join(unreadable[:5])))}
    else:
        lane = {"lane": "direct", "status": STATUS_UNREACHABLE,
                "complete": False, "unreadable": [], "count": 0, "results": [],
                "access": None, "world_dir": None,
                "backend_declared": str((self_entry or {}).get("backend", "") or "") or None,
                "backend_used": None,
                "reason": "WORLD_PATH is unset or not a directory -- this world is unreadable"}
    status, completeness = _aggregate([lane])
    worlds.append({"env_id": self_env or "<unknown>", "role": "self", "status": status,
                   "completeness": completeness, "lanes": [lane],
                   "count": lane["count"], "results": lane["results"]})

    # The `else sorted(registry)` branch DELIBERATELY diverges from peer_envs(),
    # which returns NO peers on an unresolvable self_env. That posture is right for
    # its own callers -- they ROUTE work, and claiming this world as a peer would
    # push local work at someone else. Inheriting it HERE would be wrong in the one
    # direction this module exists to prevent: a retrieval that consulted zero peers
    # would report `verdict: complete`, an EARNED negative, having looked at nothing.
    # Enumerating every env instead makes them unreachable and the verdict PARTIAL --
    # which is the safe direction for a READ. Share I/O, never share policy between
    # consumers whose wrong answers cost different things (_peer_registry.py docstring).
    for env_id in sorted(peer_envs(registry, self_env)) if self_env else sorted(registry):
        entry = registry.get(env_id) or {}
        lanes = [direct_lane(env_id, entry, terms, limit, include_archives)]
        if self_world and os.path.isdir(str(self_world)):
            lanes.append(channel_lane(self_world, env_id, terms, limit, include_archives))
        status, completeness = _aggregate(lanes)
        results = [r for l in lanes for r in l["results"]]
        for r in results:
            r["origin_env"] = env_id  # G5 provenance -- never stripped
        worlds.append({"env_id": env_id, "role": "peer", "status": status,
                       "completeness": completeness, "lanes": lanes,
                       "count": len(results), "results": results})

    partial = [w["env_id"] for w in worlds if w["completeness"] == PARTIAL]

    # REGISTRY-UNREADABLE GUARD (fresh-eyes F-001, the severest of the three
    # instances of this collapse found in this module). `load_env_registry()` is
    # fail-open by contract: ANY yaml/dir/parse error yields FEWER entries, and a
    # total failure yields {}. With an empty registry `peer_envs()` returns no
    # peers, so the loop above enumerates NOTHING, `partial` stays empty, and the
    # verdict is COMPLETE at rc=0 -- which retrieval-escalation.md Tier 2.5 tells
    # readers means "every registered world was fully read, so an empty here is an
    # EARNED negative". A broken registry therefore produced the most confident
    # possible cross-world all-clear having consulted zero peers, silently.
    #
    # An empty registry cannot happen legitimately: this deployment's own env-id
    # is registered, so {} means the registry could not be read, never that no
    # worlds exist. A registry holding ONLY self IS legitimate (a single-world
    # deployment) and stays COMPLETE -- do not widen this to `len(registry) < 2`.
    registry_unreadable = not registry
    if registry_unreadable:
        worlds.append({
            "env_id": "<registry-unreadable>", "role": "registry",
            "status": STATUS_UNREACHABLE, "completeness": PARTIAL,
            "lanes": [{"lane": "registry", "status": STATUS_UNREACHABLE,
                       "complete": False, "unreadable": [], "count": 0, "results": [],
                       "reason": "core/config/environments/*.yaml yielded ZERO entries. "
                                 "load_env_registry() is fail-open, so this means the "
                                 "registry could not be read -- NOT that no peer worlds "
                                 "exist. No peer was consulted; absence proves nothing."}],
            "count": 0, "results": [],
        })
        partial.append("<registry-unreadable>")

    return {
        "query": query,
        "self_env": self_env,
        "posture": "read-only",
        "worlds": worlds,
        "partial_envs": partial,
        "registry_unreadable": registry_unreadable,
        "verdict": PARTIAL if partial else COMPLETE,
        "fileops_imported": not assert_no_fileops(),
    }


def render(result):
    """Human-readable render. An UNREACHABLE lane MUST be visible here.

    Pinned by test_peer_retrieve.py: render(empty world) and render(unreachable
    world) must not be equal, and the unreachable render must carry the word
    UNREACHABLE. This function is where the design constraint becomes observable
    to a reader who never looks at the JSON.
    """
    out = []
    out.append("cross-world retrieval | query: %s" % result["query"])
    out.append("posture: READ-ONLY (no peer content is written into this world's stores)")
    for w in result["worlds"]:
        head = "  [%s] %s (%s) -- %s" % (w["status"].upper(), w["env_id"], w["role"], w["completeness"])
        out.append(head)
        for lane in w["lanes"]:
            if lane["status"] == STATUS_UNREACHABLE:
                out.append("    - %s lane: UNREACHABLE -- %s" % (lane["lane"], lane["reason"]))
            elif not lane.get("complete", True):
                # Reached but not fully searched. This MUST NOT render as
                # "read OK" -- that phrasing is what makes a partial read
                # indistinguishable from a complete one.
                out.append("    - %s lane: INCOMPLETE (%d match(es) from a partial read) -- %s"
                           % (lane["lane"], lane["count"], lane["reason"]))
            elif lane["status"] == STATUS_EMPTY:
                out.append("    - %s lane: read OK, no matches" % lane["lane"])
            else:
                out.append("    - %s lane: %d match(es)" % (lane["lane"], lane["count"]))
        for r in w["results"][:5]:
            who = (" <%s>" % r["author"]) if r.get("author") else ""
            out.append("      * %s %s%s" % (r["store"], r.get("ref") or "", who))
            out.append("        %s" % r["snippet"])
    if result["partial_envs"]:
        out.append("")
        out.append("  ! PARTIAL: %s could not be fully read from this box. Absence of a"
                   % ", ".join(result["partial_envs"]))
        out.append("    match in those worlds is NOT evidence of absence -- do not conclude")
        out.append("    a negative from this run (verify-before-assuming.md).")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Retrieve across all registered worlds (read-only).")
    ap.add_argument("query", nargs="*", help="free-text query; all terms must appear")
    ap.add_argument("--limit", type=int, default=5, help="max results per world lane")
    ap.add_argument("--include-archives", action="store_true", help="also search *-archive board files")
    ap.add_argument("--json", action="store_true", help="emit the raw result object")
    args = ap.parse_args(argv)

    query = " ".join(args.query).strip()
    if not query:
        _die(EXIT_USAGE, "no query given")

    try:
        result = retrieve(query, limit=args.limit, include_archives=args.include_archives)
    except ValueError as exc:
        _die(EXIT_USAGE, str(exc))

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render(result))
    return EXIT_PARTIAL if result["partial_envs"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
