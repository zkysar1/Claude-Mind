#!/usr/bin/env python3
"""durability-property-check.py — assert DURABILITY PROPERTIES, not mechanisms.

g-306-103 (D5). Parent pattern this exists to close: a durability or safety
property is guaranteed by a mechanism that is only CONDITIONALLY ACTIVE, and
nothing verifies the mechanism is active. A check that greps for the mechanism
passes whenever the mechanism is PRESENT, including when it is present and
switched off, and it goes blind entirely if a DIFFERENT mechanism was the one
actually assumed. Asserting the property is invariant to both.

Read-only sub-checks (verify-learning is minimum_mode: reader, so nothing here
may write to world/, meta/, or any agent dir):

  cited-temp-not-purged   A temp file cited by a durable record is never purged.
  temp-durable-copy       Every temp file has >=1 durable copy (git OR remote).
  tree-node-recoverable   Tree nodes have >=1 recoverable prior version.
  held-key-still-listed   A key claimed as HELD is still listed right now.
  deadman-armed           No live agent has disarmed its resurrection net.
  agent-binding-effective The agent binding actually reached this process.

The last three are g-306-116 (D7 follow-on), covering three catalogued entries
D5 left with nothing asserting the property. Each prints one PASS:/FAIL: line in
the verify-learning idiom and exits 0/1.

THE VACUOUS-PASS GUARD IS THE POINT OF THIS FILE (rb-245, guard-1802). Every
sub-check distinguishes "measured, and the property holds" from "could not
measure". The second is reported as FAIL, never PASS: a property check that
silently degrades to an empty result set is worse than no check, because it
reports the reassuring answer forever. `cited-temp-not-purged` is the sharpest
case — under citation_lookup=="failed" the purge lane degrades to the legacy
allow-list, the cited set is unknown, and the intersection is empty for the
wrong reason.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# guard-580: never a bare "bash" argv[0]. guard-581: never str(WindowsPath) as an
# argv element either — bash treats backslashes as escape introducers and strips
# them, so the script path silently becomes nonexistent. bash_cmd enforces BOTH,
# and it is the shape check-no-bare-bash.py's own fix hint prescribes for
# production code. Use it for every wrapper invocation in this file.
from _runtime_bash import BASH, bash_cmd  # noqa: E402,F401

SCRIPTS = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent.parent


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, cwd=str(PROJECT_ROOT), **kw)


def _world_dir():
    sys.path.insert(0, str(SCRIPTS))
    from _paths import WORLD_DIR  # noqa: E402
    return pathlib.Path(WORLD_DIR)


def _agents_root():
    sys.path.insert(0, str(SCRIPTS))
    from _paths import agents_root  # noqa: E402
    return pathlib.Path(agents_root())


def _cited_basenames():
    """Cited temp basenames, or None when the cited set is UNKNOWN.

    None and set() are different answers and must not be collapsed: the ratchet
    exits non-zero rather than printing an empty list precisely so an unreadable
    world cannot read as "nothing is cited".
    """
    p = _run([sys.executable, str(SCRIPTS / "temp-citation-ratchet.py"), "--cited-paths"])
    if p.returncode != 0:
        return None
    out = set()
    for line in p.stdout.splitlines():
        line = line.strip().rstrip("/")
        if line:
            out.add(line.rsplit("/", 1)[-1])
    return out


# ── check 1 ───────────────────────────────────────────────────────────────────
def check_cited_temp_not_purged(_args):
    p = _run(bash_cmd(SCRIPTS / "temp-drain-purge.sh", "--dry-run"))
    if p.returncode != 0:
        print("FAIL: temp-drain-purge.sh --dry-run exited %d — the delete-side property "
              "could NOT be measured. This is NOT a clean pass. stderr: %s"
              % (p.returncode, (p.stderr or "").strip()[:300]))
        return 1
    try:
        d = json.loads(p.stdout[p.stdout.find("{"):])
    except Exception as exc:
        print("FAIL: temp-drain-purge.sh --dry-run emitted unparseable JSON (%s) — "
              "delete-side property unmeasured, NOT clean." % exc)
        return 1

    lookup = d.get("citation_lookup")
    would = d.get("files") or []

    # Degraded lane: Lane 1 fell back to the pre-inversion allow-list, so the
    # cited exemptions were never applied. An empty intersection here says
    # nothing about the property — and a cited .py/.log/.txt WOULD be deleted,
    # since the legacy allow-list carries no ! -name exemptions at all.
    if lookup == "failed":
        print("FAIL: temp-drain-purge reports citation_lookup=\"failed\" — the purge ran "
              "DEGRADED against the pre-inversion allow-list, so cited-file exemptions were "
              "NOT applied and a cited .py/.log/.txt would be deleted. Lane 2 (drained/ GC) "
              "was SKIPPED outright for the same reason, so its empty file list is a "
              "not-run, not a clean run. The delete-side "
              "reference guard is INACTIVE; an empty intersection under this condition is "
              "unmeasured, not clean (g-306-111 / g-306-103 / g-306-102).")
        return 1

    # Third unmeasured door (). "n/a" means temp-drain-purge found no
    # temp dir at all, so it scanned NOTHING and every file list it returned is
    # empty by construction — including the intersection this check reads. The
    # two branches above refuse the other two ways of being unmeasured; without
    # this one the function fell through to PASS and printed "the delete-side
    # reference guard is ACTIVE ... and the property holds", which a run that
    # scanned nothing cannot support. rc was 0, so an automated consumer read it
    # as clean. That is the exact vacuous-zero shape the module docstring calls
    # the point of this file (rb-245 / guard-1802 / guard-1922 / rb-1961).
    #
    # FAIL (rc=1), not a new SKIP verdict, and the exit code is chosen from
    # evidence rather than by analogy with the siblings: the docstring fixes a
    # binary "exits 0/1" contract and says could-not-measure "is reported as
    # FAIL, never PASS", and check_temp_durable_copy already answers this exact
    # input condition — zero files under any agents/*/temp — with FAIL. A SKIP
    # returning 0 would reproduce the defect verbatim, since 0 IS the clean claim.
    # It fires on the boxes with the least history (fresh agent, fresh clone,
    # transplanted seed), where a false all-clear is least likely to be noticed.
    if lookup == "n/a":
        print("FAIL: temp-drain-purge reports citation_lookup=\"n/a\" — no temp dir "
              "exists, so NOTHING was scanned and the empty file lists (and the empty "
              "intersection) are empty BY CONSTRUCTION. This is a not-run, not a clean "
              "run: the delete-side reference guard was never exercised, so this run "
              "carries no evidence either way about the property (g-306-117).")
        return 1

    cited = _cited_basenames()
    if cited is None:
        print("FAIL: could not determine the cited set (temp-citation-ratchet.py "
              "--cited-paths failed) — the delete-side property is unmeasured, NOT clean. "
              "Check WORLD_DIR resolution before reading this as no-drift.")
        return 1

    # Lane 2 (drained/ GC by age) joined in as of . Before it,
    # gc_drained_archive carried NO cited-exemption and returned a bare COUNT, so
    # this check was Lane-1-only BY CONSTRUCTION and a cited artifact became
    # age-deletable the moment /drain-temp archived it into drained/. It now
    # applies the same basename exemption and publishes `drained_gc_files`, so
    # both delete-side lanes are intersectable here.
    #
    # `.get(...) or []` is deliberate: an OLDER temp-drain-purge.sh that predates
    # the field yields [] and this degrades to exactly the prior Lane-1 behaviour
    # rather than raising.
    lane2 = d.get("drained_gc_files") or []
    overlap = sorted(set(would) & cited)
    overlap2 = sorted(set(lane2) & cited)
    if overlap or overlap2:
        parts = []
        if overlap:
            parts.append("Lane 1 (temp/): %s" % (overlap[:8],))
        if overlap2:
            parts.append("Lane 2 (temp/drained/): %s" % (overlap2[:8],))
        print("FAIL: %d temp file(s) cited by a durable record are scheduled for purge — "
              "the next drain would orphan the citing record's evidence: %s. Fold the "
              "evidence inline or re-point the citation at a durable path; do NOT delete "
              "the citing text (guard-952/731/712/667)."
              % (len(overlap) + len(overlap2), " | ".join(parts)))
        return 1

    # SCOPE, still narrower than the check's name: Lane 3 (stray DIRS) remains
    # count-only and is not covered. That is not a gap of the same kind — Lane 3
    # deletes directories, so there is no file basename to intersect against the
    # cited set, which is keyed on basenames.
    print("PASS: cited-temp-not-purged [Lanes 1+2] — %d cited basename(s) vs %d Lane-1 + %d "
          "Lane-2 would-purge file(s), intersection 0, citation_lookup=%s (delete-side "
          "reference guard ACTIVE on both file lanes and the property holds)"
          % (len(cited), len(would), len(lane2), lookup))
    return 0


# ── check 2 ───────────────────────────────────────────────────────────────────
def _git_tracked(rel_paths):
    if not rel_paths:
        return set()
    p = _run(["git", "ls-files", "--"] + [str(r) for r in rel_paths])
    if p.returncode != 0:
        return set()
    return {line.strip() for line in p.stdout.splitlines() if line.strip()}


def check_temp_durable_copy(args):
    root = _agents_root()
    files = []
    for tempdir in sorted(root.glob("*/temp")):
        for f in sorted(tempdir.glob("*")):
            if f.is_file() and not f.name.startswith("."):
                files.append(f)
    if not files:
        print("FAIL: found ZERO files under any agents/*/temp — the durability property "
              "could not be measured. A vacuous zero here is not a pass (rb-245); check "
              "agents_root() resolution.")
        return 1

    sample = files[:: max(1, len(files) // args.sample)][: args.sample]
    rels = [f.relative_to(PROJECT_ROOT) for f in sample]
    tracked = _git_tracked(rels)

    # Probe REMOTE CAPABILITY once, before probing any object. Distinguishing
    # "no remote is configured" from "the remote is configured and this object is
    # absent" is load-bearing: collapsing them makes every file on a
    # local-backend box read as naked, and the check then reports "not present in
    # the configured remote" about a remote that does not exist. Measured
    # 2026-08-01 — a bare STORAGE_BACKEND=local run failed 7 of 8 with exactly
    # that wording while the own-cloud run passed 8 of 8 on the same files.
    # remote_ok is None === "no remote to consult"; a SET means the remote was
    # consulted and these are the objects it holds.
    remote_ok, remote_err = None, ""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "mind_api" / "src"))
        sys.path.insert(0, str(SCRIPTS))
        from storage_backend import get_backend  # noqa: E402
        backend = get_backend()
        if not (hasattr(backend, "s3") and hasattr(backend, "_s3_key")):
            raise RuntimeError(
                "%s exposes no remote object surface — durability here comes from "
                "git or the local filesystem, not a remote" % type(backend).__name__)
        remote_ok = set()
        probe_errors, last_err = 0, ""
        for f in sample:
            try:
                key = backend._s3_key(f.resolve())
                backend.s3.head_object(Bucket=backend.bucket, Key=key)
                remote_ok.add(str(f.relative_to(PROJECT_ROOT)))
            except Exception as exc:
                # "absent" and "could not ask" are different answers. Swallowing
                # both makes a network outage indistinguishable from a genuine
                # durability hole, and this check would then blame durability for
                # a connectivity fault — loudly, on every file. Measured: a
                # backend whose head_object always raises produced
                # "1 of 1 have ZERO durable copies".
                probe_errors += 1
                last_err = "%s: %s" % (type(exc).__name__, exc)
        if probe_errors == len(sample) and not remote_ok:
            # Every single probe failed => treat the remote as UNCONSULTABLE
            # rather than as empty. A real all-absent result is possible, but it
            # is indistinguishable from an outage here, and the fail-safe
            # direction is to report that we could not measure.
            raise RuntimeError(
                "every remote probe failed (%d/%d), last: %s — cannot distinguish "
                "absent from unreachable" % (probe_errors, len(sample), last_err))
    except Exception as exc:  # local backend, no creds, no cloud SDK — all legitimate
        remote_ok, remote_err = None, "%s: %s" % (type(exc).__name__, exc)

    naked = []
    for f in sample:
        rel = str(f.relative_to(PROJECT_ROOT))
        if rel in tracked:
            continue
        if remote_ok is not None and rel in remote_ok:
            continue
        naked.append(rel)

    if naked:
        # Name the durability layers that were ACTUALLY consulted. Saying "not
        # present in the configured remote" on a box with no remote sends the
        # reader hunting for a sync failure that cannot exist; the real finding
        # there is that this deployment has no durability layer for temp/ at all.
        if remote_ok is None:
            where = ("not git-tracked, and NO remote was consultable (%s), so this "
                     "deployment currently has no durability layer for temp/ at all"
                     % (remote_err or "reason unrecorded"))
        else:
            where = ("neither git-tracked nor present in the configured remote")
        print("FAIL: %d of %d sampled temp file(s) have ZERO durable copies — %s, so a "
              "purge or a lost box destroys them irrecoverably: %s"
              % (len(naked), len(sample), where, naked[:6]))
        return 1

    via = []
    if tracked:
        via.append("%d git-tracked" % len(tracked))
    if remote_ok:
        via.append("%d in remote" % len(remote_ok))
    print("PASS: temp-durable-copy — all %d sampled file(s) of %d total under agents/*/temp "
          "have >=1 durable copy (%s)" % (len(sample), len(files), ", ".join(via) or "none"))
    return 0


# ── check 3 ───────────────────────────────────────────────────────────────────
def _node_has_prior_version(world, hist, rel):
    """A prior version is recoverable from EITHER .history layout.

    Two layouts coexist and only one was obvious: bulk stores snapshot to
    .history/<relpath>/, while tree bodies land under .history/snapshots/<relpath>/.
    Probing only the first reports 0/1321 — a false 100% that measurement caught
    (2026-08-01). Check both; the positive control is that the two agree.
    """
    for base in (hist / "snapshots", hist):
        d = base / rel
        try:
            if d.is_dir() and any(x.is_file() for x in d.iterdir()):
                return True
        except OSError:
            continue
    return False


def check_tree_node_recoverable(args):
    world = _world_dir()
    tree = world / "knowledge" / "tree"
    hist = world / ".history"
    if not tree.is_dir():
        print("FAIL: knowledge tree not readable at %s — recoverability unmeasured, "
              "NOT clean (check WORLD_DIR resolution)." % tree)
        return 1

    nodes = sorted(p for p in tree.rglob("*.md") if p.is_file())
    if not nodes:
        print("FAIL: found ZERO tree node .md files — a vacuous zero, not a pass (rb-245).")
        return 1

    missing = [n.relative_to(world) for n in nodes
               if not _node_has_prior_version(world, hist, n.relative_to(world))]
    n_missing, n_total = len(missing), len(nodes)

    baseline = _read_baseline(args.baseline_key)
    if baseline is None:
        print("SEEDED: tree-node-recoverable — %d of %d tree node(s) (%.1f%%) have NO "
              "recoverable prior version. No baseline yet; seed "
              "meta/audit-baselines.yaml key '%s' at %d (ratchets DOWN as node writes "
              "gain history coverage, never up)."
              % (n_missing, n_total, 100.0 * n_missing / n_total, args.baseline_key, n_missing))
        return 0

    if n_missing > baseline:
        print("FAIL: tree-node-recoverable REGRESSED — %d of %d node(s) now have no "
              "recoverable prior version, up from baseline %d. A node edited without a "
              "prior version cannot be rolled back; a bad overwrite is permanent. "
              "Newly-unrecoverable sample: %s"
              % (n_missing, n_total, baseline, [str(m) for m in missing[:4]]))
        return 1

    verdict = "RATCHETED" if n_missing < baseline else "STABLE"
    print("PASS: tree-node-recoverable %s — %d of %d node(s) lack a prior version "
          "(baseline %d)" % (verdict, n_missing, n_total, baseline))
    return 0


def _read_baseline(key):
    try:
        import yaml
        sys.path.insert(0, str(SCRIPTS))
        from _paths import META_DIR
        p = pathlib.Path(META_DIR) / "audit-baselines.yaml"
        if not p.exists():
            return None
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        entry = (d.get("baselines") or d).get(key)
        if isinstance(entry, dict):
            return entry.get("baseline")
        return entry
    except Exception:
        return None


# ── check 4 ( A) ─────────────────────────────────────────────────────
# The deployment supplies the inbound-lane lister; the PROPERTY is generic. Naming
# a script here rather than a bucket/prefix keeps the core file free of any
# deployment's storage layout, and a world that has no such lister degrades to a
# named FAIL instead of a silent skip.
LANE_LISTER = "agent-inbox-list.sh"


def _lane_keys():
    """(keys, error) — keys currently listed in the inbound lane, or (None, why).

    None and set() are different answers, for the same reason _cited_basenames
    keeps them apart: an unlistable lane must never read as an empty lane.
    """
    try:
        lister = _world_dir() / "scripts" / LANE_LISTER
    except Exception as exc:
        return None, "could not resolve WORLD_DIR (%s: %s)" % (type(exc).__name__, exc)
    if not lister.is_file():
        return None, "this deployment has no inbound-lane lister at %s" % lister
    p = _run(bash_cmd(lister))
    if p.returncode != 0:
        return None, "%s exited %d: %s" % (LANE_LISTER, p.returncode,
                                           (p.stderr or "").strip()[:200])
    keys = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            keys.add(parts[2])
    return keys, ""


def check_held_key_still_listed(args):
    """ENTRY 6 — a key claimed as HELD is still listed at the moment of the claim.

    guard-3380 declines to archive an unconsumed key from a shared lane, on the
    premise that declining keeps it listed. The peer FALSIFIED that premise
    (msg-20260731-080319-omni-6335): a manifest deliberately held for exactly
    this reason was 404 in the lane and present under processed/ by its poll,
    because ANOTHER world's drain archived it first — the processed/ marker is
    global in the RETENTION direction, not only the handled direction. Measured
    both ways inside one 24h window under identical correct holds: one survived
    ~17h, one did not survive at all. So a hold guarantees neither delivery NOR
    retention, and a held key must never be reported as still-available without
    re-listing AT THE MOMENT of the claim. That re-list is what this asserts.
    """
    keys, err = _lane_keys()
    if keys is None:
        print("FAIL: the inbound lane could not be listed (%s) — no hold claim can be "
              "verified while this is true, so the property is UNMEASURED, not clean. "
              "A hold reported without a same-moment re-list is unverified (guard-3380)."
              % err)
        return 1

    claimed = list(args.held_key or [])
    if claimed:
        missing = [k for k in claimed
                   if k not in keys and not any(x.endswith("/" + k) for x in keys)]
        if missing:
            print("FAIL: %d of %d key(s) claimed as HELD are NO LONGER LISTED — another "
                  "consumer's drain archived them, so the hold did not keep them "
                  "available and any relay depending on the hold is already broken: %s. "
                  "The relay is the only leg that carries content; re-send it "
                  "(guard-3380 third half)." % (len(missing), len(claimed), missing[:6]))
            return 1
        print("PASS: held-key-still-listed — all %d claimed held key(s) re-listed and "
              "present in a lane of %d live key(s)" % (len(claimed), len(keys)))
        return 0

    # SCOPE, stated because the check's name is broader than what it can see.
    # No hold is durably recorded anywhere: guard-3380's own second half measures
    # that a hold is "counted only in a per-run conservation identity that is
    # forgotten at close". So this run audited NO PAST HOLD — it establishes only
    # that the lane IS listable, i.e. that a claim made right now is verifiable.
    # The live key count is the positive control (guard-2173): a broken lister
    # cannot produce it, so this line cannot be printed by a blind check.
    print("PASS: held-key-still-listed [verifiability only — 0 hold claims supplied] "
          "— the inbound lane is listable RIGHT NOW (%d live key(s)), so a hold "
          "claimed at this moment can be re-listed. NO PAST HOLD WAS AUDITED: holds "
          "are not durably recorded (guard-3380 second half), so pass --held-key K "
          "at claim time to assert the property for a specific key." % len(keys))
    return 0


# ── check 5 ( B) ─────────────────────────────────────────────────────
def _live_roster():
    """(agents, error) — non-retired agents from team-state, or (None, why)."""
    p = _run(bash_cmd(SCRIPTS / "team-state-read.sh", "--json"))
    if p.returncode != 0:
        return None, "team-state-read.sh exited %d: %s" % (p.returncode,
                                                           (p.stderr or "").strip()[:200])
    try:
        status = (json.loads(p.stdout) or {}).get("agent_status") or {}
    except Exception as exc:
        return None, "team-state JSON unparseable (%s)" % exc
    if not status:
        return None, "team-state carries an EMPTY agent_status roster"
    return sorted(n for n, row in status.items()
                  if not (isinstance(row, dict) and row.get("retired_at"))), ""


def check_deadman_armed(_args):
    """ENTRY 9 — no live agent has disarmed its deadman resurrection net.

    The net is disarmed by the MERE PRESENCE of session/deadman-disabled, and
    prior silent multi-hour loop deaths are documented in return-protocol.md. The
    property therefore holds today purely by absence — which is exactly the shape
    that makes a naive zero worthless, so the examined set is named.

    RESIDENCY IS LOAD-BEARING AND IS WHY THIS DOES NOT GLOB agents/*. The sentinel
    is classified machine-local by the storage backend (measured 2026-08-01), so
    it is never synced: a non-resident agent's file lives only on the box that
    runs it, and no store of record holds it. A local glob across every agent dir
    therefore answers "armed" for four of five live agents by pure cache
    geometry — the flattering reading, produced by a probe that never looked.
    That is guard-2193's spatial axis: a fleet-scoped condition read through a
    per-agent instrument. This check measures only the agents resident HERE and
    names the rest as unmeasured-from-this-box.
    """
    roster, err = _live_roster()
    if roster is None:
        print("FAIL: the live agent roster could not be read (%s) — 'no agent has "
              "disarmed its net' is UNMEASURED, not clean. Without a roster there is "
              "nothing to be zero OF (rb-245)." % err)
        return 1

    root = _agents_root()
    resident, remote, disarmed = [], [], []
    for name in roster:
        # Same residency signal _paths.py uses: only the box an agent runs on
        # carries its local-paths.conf.
        if not (root / name / "local-paths.conf").is_file():
            remote.append(name)
            continue
        resident.append(name)
        if (root / name / "session" / "deadman-disabled").exists():
            disarmed.append(name)

    if not resident:
        print("FAIL: none of the %d live agent(s) (%s) is resident on this box, so ZERO "
              "were examined — a vacuous zero, not a pass. The sentinel is machine-local "
              "and unsynced, so it can only be read where the agent runs."
              % (len(roster), ", ".join(roster)))
        return 1

    if disarmed:
        print("FAIL: %d agent(s) carry session/deadman-disabled — their resurrection net "
              "is DISARMED, so a text-only turn-end kills the loop silently with nothing "
              "to revive it (return-protocol.md): %s. Remove the file, or record why the "
              "opt-out is deliberate." % (len(disarmed), ", ".join(disarmed)))
        return 1

    print("PASS: deadman-armed [%d of %d live agent(s) examined] — armed (no "
          "deadman-disabled) for: %s. NOT MEASURED FROM THIS BOX: %s — the sentinel is "
          "machine-local and unsynced, so this PASS is NOT a fleet-wide all-clear "
          "(guard-2193)." % (len(resident), len(roster), ", ".join(resident),
                             ", ".join(remote) or "none"))
    return 0


# ── check 6 ( C) ─────────────────────────────────────────────────────
SHIM_DIRNAME = ".python-shim"


def check_agent_binding_effective(_args):
    """ENTRY 7 — the agent binding actually reached this process.

    bash-agent-inject fails OPEN on any error, with hook timeouts measured in
    tens of seconds, so a silent failure yields an unbound agent — the
    wrong-agent-write class. The hook being registered proves nothing about any
    particular call, so this observes the injected values from INSIDE the process
    the hook was supposed to reach.

    NOT VACUOUS BY CONSTRUCTION (guard-1718): the fail-open default here is
    ABSENCE, not a plausible value, and the observed name is cross-checked
    against an INDEPENDENT source — the on-disk session binding — so 'ran and
    produced the right name' is distinguishable from 'never ran'. A disagreement
    is reported separately from an absence because it is the more dangerous
    state: a bound-but-WRONG agent writes to a partner's private store.
    """
    agent = (os.environ.get("MIND_AGENT") or "").strip()
    sid = (os.environ.get("MIND_SID") or "").strip()

    if not agent:
        print("FAIL: MIND_AGENT is unset inside this process — the PreToolUse Bash "
              "injection did not take effect and it fails OPEN, so every script in this "
              "call resolves an unbound agent. This is the wrong-agent-write precondition, "
              "not a cosmetic gap.")
        return 1
    if not sid:
        print("FAIL: MIND_SID is unset, so the observed MIND_AGENT=%r cannot be checked "
              "against the on-disk binding — the binding is UNVERIFIED, not confirmed. An "
              "environment variable that agrees with nothing is not evidence." % agent)
        return 1

    try:
        sys.path.insert(0, str(SCRIPTS))
        from _session_binding import resolve_binding  # noqa: E402
        binding = resolve_binding(sid, PROJECT_ROOT)
    except Exception as exc:
        print("FAIL: could not resolve the session binding for SID %s (%s: %s) — the "
              "injected MIND_AGENT=%r is UNCORROBORATED, which is not the same as correct."
              % (sid[:12], type(exc).__name__, exc, agent))
        return 1

    if binding is None:
        print("FAIL: no session binding exists on disk for SID %s, yet MIND_AGENT=%r was "
              "injected — the value cannot be corroborated by any independent source, so a "
              "stale or ambient export is indistinguishable from a live binding."
              % (sid[:12], agent))
        return 1
    if binding.agent != agent:
        print("FAIL: injected MIND_AGENT=%r DISAGREES with the on-disk binding for SID %s "
              "(=%r) — writes in this call land in the wrong agent's private store. This is "
              "worse than an unset binding, because everything downstream succeeds."
              % (agent, sid[:12], binding.agent))
        return 1

    shim = SCRIPTS / SHIM_DIRNAME
    path_entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
    try:
        idx = next(i for i, e in enumerate(path_entries)
                   if pathlib.Path(e).resolve() == shim.resolve())
    except (StopIteration, OSError):
        print("FAIL: MIND_AGENT=%r is bound, but the python shim dir (%s) is NOT on PATH — "
              "the other half of the same injection failed open, so a direct `python3 -c` "
              "in this call reaches the platform stub instead of a real interpreter "
              "(rb-370, guard-335)." % (agent, shim))
        return 1

    print("PASS: agent-binding-effective — MIND_AGENT=%r observed inside this process and "
          "CONFIRMED against the on-disk binding for SID %s (source=%s); shim dir present at "
          "PATH position %d of %d" % (agent, sid[:12], binding.source, idx + 1,
                                      len(path_entries)))
    return 0


CHECKS = {
    "cited-temp-not-purged": check_cited_temp_not_purged,
    "temp-durable-copy": check_temp_durable_copy,
    "tree-node-recoverable": check_tree_node_recoverable,
    "held-key-still-listed": check_held_key_still_listed,
    "deadman-armed": check_deadman_armed,
    "agent-binding-effective": check_agent_binding_effective,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("check", choices=sorted(CHECKS) + ["all"])
    ap.add_argument("--sample", type=int, default=8,
                    help="temp-durable-copy: how many files to probe (default 8)")
    ap.add_argument("--baseline-key", default="tree_nodes_without_prior_version")
    ap.add_argument("--held-key", action="append", metavar="KEY",
                    help="held-key-still-listed: a key being claimed as HELD; "
                         "repeatable. Each is re-listed against the live lane.")
    args = ap.parse_args()

    if args.check == "all":
        rc = 0
        for name in sorted(CHECKS):
            rc |= CHECKS[name](args)
        return rc
    return CHECKS[args.check](args)


if __name__ == "__main__":
    sys.exit(main())
