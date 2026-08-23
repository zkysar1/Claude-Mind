#!/usr/bin/env python3
"""Advisory WORKING-TREE lock for co-resident Bodies ().

THE DEFECT THIS EXISTS FOR. Two Bodies of one agent share ONE git checkout with
no mutual exclusion. The claim protocol writes `claimed_by` / `claimed_by_sid`
to the GOAL RECORD, so it is advisory AT THE STORE -- and the collision happens
AT THE FILESYSTEM. A Body that never consults the record before touching files
is not gated by it, which is why (quoting the goal) "any fix that strengthens
claim semantics without touching the working-tree layer will pass its own tests
and change nothing here."

Two measured instances, cc-07 2026-08-21:
  A (18:23) a sibling Body swept another live Body's uncommitted files into a
    commit. The content happened to be byte-identical and the tests happened to
    pass, so nothing looked wrong -- which is the problem. The adjacent cases are
    identical in shape and not benign: a Body mid-edit with syntactically
    incomplete code on disk, or one between a source change and its test.
  B (19:49) a peer had a full-suite run live; this box's worker-loop Phase -0.3
    merged 19 origin commits into the tree mid-run, and ~40 minutes of a
    1127-file run produced `VERDICT: INVALID (tree-moved)` -- a number that means
    nothing.

`.git/index.lock` does NOT cover this. It is held for the instant of a single git
operation, while the window that needs protecting spans a whole unit: from "a
Body starts editing" to "that Body commits", or the ~32 minutes a suite runs.
Both instances above happened with index.lock free, via entirely legal git ops.

IDENTITY IS THE SESSION, NOT THE AGENT -- the same reason unit-claim.sh gives:
a worker Body and its reducer are both `alpha`, so an agent-name comparison
cannot separate them. MIND_SID is the discriminator.

THE LOCK IS PER-CHECKOUT, and that is why it lives in `mind_api/state/`
(gitignored, per-box, already the home of daemon.pid/daemon.port). It must NOT
be synced: a lock that travels to another box would block Bodies that do not
share the tree it protects.

=== THE FAIL-SAFE DIRECTION IS THE LOAD-BEARING PROPERTY ===

This lock sits in front of `iteration-push.sh`, which EVERY Body runs EVERY
cycle and which is fail-soft by contract. A lock that wrongly refuses does not
cause one bad merge -- it silently freezes framework sync for a whole box, and
"resume on local code" then becomes permanent staleness (the g-306-315 /
g-115-6934 shape, where cc-08 ran 85 commits behind while a blocked merge
concealed a peer's fix). Stopping a healthy loop on a plumbing fault is worse
than the disease (guard-1562).

So EVERY ambiguous signal resolves to PROCEED. `check` refuses on exactly one
condition -- a lock that is present, parseable, unexpired, held by a DIFFERENT
sid, AND whose holder process is still alive. Absent, unreadable, malformed,
expired, mine, or dead-holder all return 0.

THE DEAD-HOLDER PROBE IS WHY THE TTL CAN BE GENEROUS. Because the lock is
per-checkout, the holder is by construction a process on THIS box, so
`os.kill(pid, 0)` is a real liveness test rather than a guess. A killed suite
therefore frees the tree immediately instead of after the TTL, and the TTL is
only the backstop for a holder whose pid was recycled or never recorded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

LOCK_FILENAME = "tree-lock.json"
# 90 minutes. Sized from the measured full-suite runtime (~32 min) with room for
# a contended box, since a suite is the longest thing that legitimately holds the
# tree. It is a BACKSTOP, not the primary release: a live holder refreshes and a
# dead one is detected by pid probe, so this only matters when a pid is missing
# or recycled.
DEFAULT_TTL_SECONDS = 5400

# Exit codes. Deliberately the same shape as unit-claim.sh so the two primitives
# read alike: 0 = go, 1 = refused, 2 = plumbing. rc=2 exists so a caller can tell
# "I could not determine the state" from "the tree is free" -- but note `check`
# never returns 2, because for THAT verb an indeterminate state must still mean
# proceed (see the fail-safe direction above).
RC_OK, RC_REFUSED, RC_PLUMBING = 0, 1, 2


def _state_dir(project_root: Path) -> Path:
    return project_root / "mind_api" / "state"


def lock_path(project_root: Path) -> Path:
    return _state_dir(project_root) / LOCK_FILENAME


def _now() -> float:
    return time.time()


def _pid_alive(pid) -> bool | None:
    """True / False / None when it cannot be determined.

    None (not False) for a missing or non-integer pid: an absent pid is an
    UNKNOWN liveness, and reporting it as dead would let any malformed lock be
    stolen. The caller folds None into "assume alive" and falls back to the TTL.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by another user. Signal-permission is not liveness info
        # we are entitled to act on, but the process demonstrably exists.
        return True
    except OSError:
        return None


def read_lock(project_root: Path) -> dict | None:
    """The lock record, or None when absent/unreadable/malformed.

    All three collapse to None on purpose: every one of them means "no evidence
    that anyone holds this tree", and the caller must proceed on that.
    """
    p = lock_path(project_root)
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- unreadable is indistinguishable from absent, by design
        return None
    return data if isinstance(data, dict) else None


def evaluate(record: dict | None, my_sid: str, now: float | None = None) -> dict:
    """Pure decision. Returns {'blocked': bool, 'reason': str, ...}.

    Split out from the IO so the whole truth table is unit-testable without a
    filesystem, and so the one branch that BLOCKS is visible in one place.
    """
    now = _now() if now is None else now
    if record is None:
        return {"blocked": False, "reason": "no lock present", "state": "free"}

    holder = record.get("holder_sid")
    if not isinstance(holder, str) or not holder:
        return {"blocked": False, "reason": "lock has no holder_sid — malformed, treated as free",
                "state": "malformed"}
    if my_sid and holder == my_sid:
        return {"blocked": False, "reason": "lock is held by THIS body", "state": "mine",
                "holder_sid": holder}

    acquired = record.get("acquired_at")
    ttl = record.get("ttl_seconds")
    if not isinstance(acquired, (int, float)) or not isinstance(ttl, (int, float)):
        return {"blocked": False, "reason": "lock has no usable acquired_at/ttl — treated as free",
                "state": "malformed", "holder_sid": holder}
    age = now - acquired
    if age > ttl:
        return {"blocked": False, "state": "expired", "holder_sid": holder, "age_seconds": round(age),
                "reason": f"lock EXPIRED ({round(age)}s old > ttl {int(ttl)}s) — proceeding"}

    alive = _pid_alive(record.get("holder_pid"))
    if alive is False:
        return {"blocked": False, "state": "dead-holder", "holder_sid": holder,
                "age_seconds": round(age),
                "reason": f"holder pid {record.get('holder_pid')} is gone — proceeding"}

    # THE ONLY BLOCKING BRANCH.
    return {
        "blocked": True, "state": "held", "holder_sid": holder,
        "holder_pid": record.get("holder_pid"), "age_seconds": round(age),
        "holder_reason": record.get("reason"),
        "reason": (f"tree held by sid {holder[:8]} for {round(age)}s "
                   f"({record.get('reason') or 'no reason given'}); "
                   f"pid_alive={alive if alive is not None else 'unknown'}"),
    }


def acquire(project_root: Path, my_sid: str, agent: str, reason: str,
            ttl: int = DEFAULT_TTL_SECONDS, force: str | None = None,
            holder_pid: int | None = None) -> tuple[int, dict]:
    """`holder_pid` is the LONG-LIVED process the lock protects, or None.

    IT MUST NOT DEFAULT TO os.getpid(), and the first version of this function
    did exactly that — which made the whole gate inert. `acquire` runs as a
    short-lived CLI invocation that exits within milliseconds, so the recorded
    pid was always already gone by the time anyone called `check`; every lock
    read as `dead-holder` and the one blocking branch could never be reached.
    Caught by the first end-to-end smoke test, where a foreign sid was expected
    to be refused and sailed through instead.

    So the pid is the CALLER'S to supply (`--holder-pid`), because only the
    caller knows which process actually holds the tree: `$$` for run-full-suite,
    the agent process for a Body holding across a unit. When it is absent the
    record simply carries no pid, `_pid_alive` returns None ("unknown"), and the
    TTL governs alone — the safe direction, since unknown liveness must not let
    a lock be stolen.
    """
    if not isinstance(my_sid, str) or not my_sid:
        # An empty sid would write `holder_sid: ""`, which evaluate() correctly
        # reads back as MALFORMED and treats as free -- so the caller would hold
        # a lock that blocks nobody while believing it was protected. That is the
        # silent-inertness class this module already carries a regression test
        # for (see the holder_pid note below), so refuse LOUDLY instead. Callers
        # are fail-open by contract, so refusing here costs a warning, not a run.
        return RC_PLUMBING, {"blocked": False, "state": "no-sid",
                             "reason": "MIND_SID is unset — refusing to write a lock "
                                       "no reader would honour"}
    rec = read_lock(project_root)
    verdict = evaluate(rec, my_sid)
    if verdict["blocked"] and not force:
        return RC_REFUSED, verdict
    payload = {
        "holder_sid": my_sid, "holder_agent": agent,
        "reason": reason, "acquired_at": _now(), "ttl_seconds": ttl,
    }
    if isinstance(holder_pid, int) and holder_pid > 0:
        payload["holder_pid"] = holder_pid
    if force:
        payload["forced_over"] = {"holder_sid": verdict.get("holder_sid"), "why": force}
    try:
        d = _state_dir(project_root)
        d.mkdir(parents=True, exist_ok=True)
        tmp = lock_path(project_root).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(lock_path(project_root))
    except OSError as exc:
        # NEVER silently "acquired": a caller that believes it holds a lock it
        # does not is worse off than one that knows it failed.
        return RC_PLUMBING, {"blocked": False, "state": "write-failed", "reason": str(exc)}
    return RC_OK, {"blocked": False, "state": "acquired", "holder_sid": my_sid,
                   "reason": f"acquired for {reason} (ttl {ttl}s)"}


def release(project_root: Path, my_sid: str) -> tuple[int, dict]:
    """Idempotent, and ONLY releases a lock this sid holds.

    Releasing someone else's lock is the failure this whole module prevents, so
    a non-owner release is a no-op that reports what it saw rather than an error
    — release runs in cleanup paths where raising would mask the real fault.
    """
    rec = read_lock(project_root)
    if rec is None:
        return RC_OK, {"state": "absent", "reason": "nothing to release"}
    holder = rec.get("holder_sid")
    if holder != my_sid:
        return RC_OK, {"state": "not-mine", "holder_sid": holder,
                       "reason": f"lock belongs to {holder} — left untouched"}
    try:
        lock_path(project_root).unlink()
    except OSError as exc:
        return RC_PLUMBING, {"state": "unlink-failed", "reason": str(exc)}
    return RC_OK, {"state": "released", "reason": "released"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("verb", choices=["acquire", "release", "check", "status"])
    ap.add_argument("--reason", default="unspecified",
                    help="what is holding the tree (shown to whoever is blocked)")
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    ap.add_argument("--force", default=None, metavar="WHY",
                    help="take the lock even if another live holder has it; recorded in the lock")
    ap.add_argument("--holder-pid", type=int, default=None, metavar="PID",
                    help="pid of the LONG-LIVED process this lock protects (e.g. $$ from the "
                         "suite runner). Omit it and the TTL governs alone — never pass this "
                         "process's own pid, which exits immediately and would make every lock "
                         "read as a dead holder")
    ap.add_argument("--project-root", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    # .resolve() is load-bearing, not tidiness. The two halves of this lock reach
    # it by different routes -- run-full-suite.sh takes the __file__ default
    # (already resolved), iteration-push.sh passes its own $REPO (a logical
    # `cd .. && pwd`, which does NOT resolve symlinks) -- and two spellings of the
    # same directory would produce two DIFFERENT lock files. The writer would lock
    # one, the reader would find the other absent, and the gate would be silently
    # inert while every hand-test passed. Same failure mode as the holder-pid bug
    # this module already carries a regression test for, arriving by a different
    # door. Normalising here means any spelling of a root maps to one lock.
    root = Path(args.project_root) if args.project_root else Path(
        os.environ.get("MIND_PROJECT_ROOT") or Path(__file__).resolve().parent.parent.parent)
    root = root.resolve()
    sid = os.environ.get("MIND_SID", "")
    agent = os.environ.get("MIND_AGENT", "")

    if args.verb == "acquire":
        rc, info = acquire(root, sid, agent, args.reason, args.ttl, args.force,
                           holder_pid=args.holder_pid)
    elif args.verb == "release":
        rc, info = release(root, sid)
    else:  # check | status
        info = evaluate(read_lock(root), sid)
        # `check` is the gate iteration-push calls; `status` is for a human and
        # never refuses, so a diagnostic read can't be mistaken for a decision.
        rc = RC_REFUSED if (args.verb == "check" and info["blocked"]) else RC_OK

    print(json.dumps(info, indent=1) if args.json
          else f"[tree-lock] {info.get('state', '?')}: {info.get('reason', '')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
