"""Git-ref runner-claim store — the LOCAL-backend arm of single-runner enforcement.

WHY THIS EXISTS
===============
``core/scripts/runner-claim.sh`` advertises itself as BACKEND-POLYMORPHIC, but
until this module there was exactly ONE real implementation (a record-store CAS under
``STORAGE_BACKEND=own-cloud``) and a no-op for everything else: the daemon's
``_runner_preamble`` returned ``{ok: true, noop: true}`` for any non-own-cloud
backend. On a local-backend deployment there was therefore NO cross-machine
claim at all — nothing stopped two boxes running the same agent, and neither
box could detect the other.

That is not hypothetical. Measured 2026-08-20: two boxes ran one agent; one
drifted 457 commits behind, sat on two days of uncommitted work nobody noticed,
and 6 of 6 ids it allocated collided with different real upstream records.

THE PRIMITIVE
=============
Git already provides an atomic compare-and-swap: a push that does not match the
expected remote value is REJECTED by the receiving end. So a claim can live in a
dedicated ref namespace holding machine id, token fingerprint and heartbeat
time, updated with ``push --force-with-lease``. No new service, no schema
migration, and ``main`` is never touched.

All four properties were verified empirically against a real bare repo before
this module was written (g-306-331):

  1. create-race  — two boxes pushing with an expect-absent lease
                    (``<ref>:<zero-oid>``): exactly ONE wins, the loser gets
                    ``! [rejected] ... (stale info)`` and rc=1.
  2. update-CAS   — a push whose lease names a stale value is REJECTED.
  3. correct-CAS  — a push whose lease names the current value is ACCEPTED.
  4. fetch-trap   — see below. This one is why the module exists in this shape.

THE FETCH TRAP (the defect this design most easily hides)
=========================================================
git's default fetch refspec is ``+refs/heads/*:refs/remotes/origin/*``. A ref
living at ``refs/mind/claim/...`` is therefore **NOT** fetched by a plain
``git fetch``. Measured: after A published a claim, B's ``git fetch origin``
left B with no such ref at all, while ``git ls-remote`` proved origin had it.

A reader that forgets the explicit refspec sees "no holder" — every time,
silently, on every box. The whole mechanism would report the opposite of the
truth and a one-box acquire/refuse test would never catch it. So every read
path here goes through :meth:`_fetch_claims`, and there is a regression test
whose only job is to fail if that call is removed.

WHY ONLY A FINGERPRINT IS STORED
================================
``runner_token`` is a BEARER CREDENTIAL: under own-cloud it IS the record store's
``ConditionExpression`` authorising a heartbeat or a release. Anything holding
it can forge a heartbeat (so a crashed runner never looks stale and can never be
reclaimed) or release a LIVE claim mid-flight. A git ref is readable by everyone
with repo access, so the raw token must never be written into one. Only
``runner_token_fingerprint(token)`` — a non-reversible digest — is stored, which
is the same publishable form ``RunnerClaim`` already carries.

SCOPE — WHAT THIS DOES AND DOES NOT GUARANTEE
=============================================
This provides COORDINATION safety between cooperating fleet boxes: two runners
racing for the same agent cannot both win, and a crashed runner's claim can be
broken by a peer after it goes stale.

It does NOT provide AUTHORIZATION against a hostile client. Under own-cloud the
token condition is evaluated SERVER-SIDE by the record store; here that check
is evaluated CLIENT-SIDE before the push, so a client that skips the check can
overwrite a claim it does not own. That is not a regression introduced here —
anyone with push access to the repo can already write anything, including
``main`` — but it is a real difference between the two arms and is stated rather
than smoothed over.

Cross-references: ``core/scripts/runner-claim.sh`` (the wrapper and its rc
contract), ``core/scripts/owncloud_backend.py`` (the own-cloud arm this mirrors),
``core/scripts/reducer_self_fence.py`` (the lease asymmetry this reuses rather
than re-derives).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import List, Optional

__all__ = [
    "GitRefClaimStore",
    "GitRefClaimError",
    "ZERO_OID",
    "DEFAULT_REF_PREFIX",
    "claim_ref",
]

# git's "this ref must not exist" sentinel for --force-with-lease.
ZERO_OID = "0" * 40

DEFAULT_REF_PREFIX = "refs/mind/claim"

def _takeover_default() -> int:
    """T_takeover — IMPORTED from the own-cloud arm, never forked.

    `aspirations.yaml` pins the lease invariant `runner_heartbeat.stepdown_seconds
    < owncloud_backend.DEFAULT_RUNNER_STALE_SECONDS`, and
    `test_reducer_self_fence.py::test_config_invariant_stepdown_precedes_takeover`
    proves it against THAT constant specifically. A private copy here would put
    the local arm outside the only proof the fleet has that a holder stands down
    BEFORE a peer may seize — which is the split-brain this whole module exists
    to prevent, reintroduced by a duplicated number.

    The literal below is reached only where `owncloud_backend` cannot be
    imported at all: it imports the cloud SDK at module scope, so a pure-local
    deployment without the cloud SDK legitimately cannot load it. That is a real
    capability difference rather than a silent fallback, and
    `test_takeover_default_matches_owncloud` pins the two values together on
    every box that CAN import both, so drift fails loudly wherever it is
    detectable at all.
    """
    try:
        from owncloud_backend import DEFAULT_RUNNER_STALE_SECONDS as _v
        return int(_v)
    except Exception:  # noqa: BLE001 — no cloud SDK on a pure-local box
        return 3900


# A claim whose heartbeat is older than this may be broken by a peer.
# Env override: OWNERSHIP_STALE_SECONDS (see _stale_seconds).
DEFAULT_RUNNER_STALE_SECONDS = _takeover_default()

# Bounds every git call. This sits on the PER-ITERATION heartbeat path
# (heartbeat-tick.sh -> runner-claim.sh heartbeat -> this store), so on a local
# backend each loop iteration now costs one real fetch plus one push to the
# remote. That is the same shape the own-cloud arm already pays per heartbeat
# with its record-store write, over SSH rather than HTTPS — named here because it is
# a genuine new cost on the hot path, not hidden behind the abstraction.
# 30s is deliberately well under the 120s Bash bound the orchestrator's own calls
# run against: a claim operation that has not finished in 30s is a network fault,
# and stalling the loop longer is worse than failing the heartbeat — heartbeat-
# tick.sh fails open on a non-zero rc, and one missed beat is 1/130th of the
# 3900s takeover window.
_GIT_TIMEOUT_SECONDS = 30


class GitRefClaimError(Exception):
    """A git operation the claim store depends on failed in a way that leaves
    the claim state UNKNOWN. Callers must treat this as "cannot establish a
    claim", never as "no claim exists" — the distinction is the whole point of
    runner-claim.sh's rc=4 REFUSE contract."""


def claim_ref(agent: str, env_id: str, prefix: str = DEFAULT_REF_PREFIX) -> str:
    """Full ref path for one agent's claim.

    ENV-SCOPED deliberately, mirroring how ``OwnCloudBackend._s3_key`` derives
    its key from ``customer_prefix + env_id + filename`` rather than from the
    caller's local paths. The 2026-07-09 truncation incident (guard-955 /
    rb-2983) was exactly a cross-environment key collision, so a claim namespace
    that ignores env identity would re-open that class on a different store.
    """
    return f"{prefix}/{env_id}/{agent}"


def _stale_seconds() -> int:
    """Resolve the staleness threshold, honouring OWNERSHIP_STALE_SECONDS.

    Rejects 0, negatives and non-integers rather than coercing them: a zero
    threshold would make EVERY claim instantly stale and turn the lease into a
    free-for-all, which is worse than the default being wrong.
    """
    raw = (os.environ.get("OWNERSHIP_STALE_SECONDS") or "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return DEFAULT_RUNNER_STALE_SECONDS


class GitRefClaimStore:
    """Runner-claim store backed by a git ref, for the local backend.

    Method names and return shapes intentionally mirror the ``OwnCloudBackend``
    runner methods so the daemon endpoints can call either arm without
    branching on backend beyond the initial dispatch.
    """

    def __init__(
        self,
        repo_root,
        env_id: str,
        remote: str = "origin",
        machine_id: Optional[str] = None,
        runner_stale_seconds: Optional[int] = None,
        ref_prefix: str = DEFAULT_REF_PREFIX,
    ):
        self.repo_root = str(repo_root)
        self.env_id = env_id
        self.remote = remote
        self.machine_id = machine_id or _default_machine_id()
        self.runner_stale_seconds = (
            runner_stale_seconds if runner_stale_seconds is not None
            else _stale_seconds()
        )
        self.ref_prefix = ref_prefix
        #: stderr tail of the most recent FAILED claim-namespace fetch, or None
        #: when the last fetch succeeded. Surfaced by the daemon's claims
        #: listing so an empty ``claims`` after a failed fetch is never read as
        #: "no holder" (2026-08-27 coach-mind: the listing said ``[]`` while
        #: every acquire failed, and the operator's agent chased phantom refs).
        self.last_fetch_error: Optional[str] = None

    @classmethod
    def default_remote(cls, repo_root) -> str:
        """The remote the claim store should arbitrate on.

        Precedence: ``RUNNER_CLAIM_REMOTE`` (explicit operator choice) → a remote
        literally named ``claims`` when the repo has one → ``origin``.

        The ``claims`` convention exists because ``origin`` is the WRONG arbiter
        for a self-contained single-box deployment whose origin is a repo it can
        only read: coach-mind's origin is the staging repo over anonymous HTTPS,
        so every push — and therefore every acquire — fails (measured
        2026-08-27, zc-03). A bare repo on the box, added as ``git remote add
        claims /path/to/claims.git``, gives the store a writable CAS arbiter
        that touches no cloud resource. Every box that runs the agent must
        share the same ``claims`` remote; on a single box that is trivially true.
        """
        explicit = (os.environ.get("RUNNER_CLAIM_REMOTE") or "").strip()
        if explicit:
            return explicit
        if cls.available(repo_root, "claims"):
            return "claims"
        return "origin"

    @classmethod
    def available(cls, repo_root, remote: str = "origin") -> bool:
        """Whether a git-ref claim store is usable here.

        Requires a git repo with a resolvable remote — without a remote there is
        no shared arbiter, and a purely local ref would hand EVERY box its own
        private "claim" that always succeeds. That is strictly worse than the
        no-op it would replace: a fake claim reads as real single-runner
        enforcement. So an unavailable store degrades to the pre-existing no-op
        rather than to a local-only ref.
        """
        try:
            p = subprocess.run(
                ["git", "-C", str(repo_root), "remote", "get-url", remote],
                capture_output=True, text=True,
                timeout=_GIT_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return p.returncode == 0 and bool((p.stdout or "").strip())

    # ---------------------------------------------------------------- git I/O

    def _git(self, *args, check: bool = True):
        """Run a git command in the repo. Returns CompletedProcess.

        ``check=False`` is used where a non-zero rc is MEANINGFUL (a rejected
        CAS push, an absent ref) rather than exceptional.
        """
        try:
            return subprocess.run(
                ["git", "-C", self.repo_root, *args],
                capture_output=True, text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            if check:
                raise GitRefClaimError(f"git {' '.join(args)}: {e}") from e
            raise

    def _fetch_claims(self) -> None:
        """Fetch the claim namespace EXPLICITLY.

        THE LOAD-BEARING CALL. git's default refspec does not include
        ``refs/mind/claim/*``, so without this every read below sees a stale or
        absent ref and the store reports "no holder" while a live claim exists
        on the remote. Measured, not theorised — see the module docstring.

        A fetch failure is swallowed on purpose: an offline box must still be
        able to read its LAST-KNOWN claim state (that is what lets a
        disconnected holder notice it can no longer renew and step down). The
        caller distinguishes fresh from stale by the heartbeat age, not by
        whether the fetch succeeded.
        """
        refspec = f"+{self.ref_prefix}/*:{self.ref_prefix}/*"
        p = self._git("fetch", self.remote, refspec, check=False)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()
            self.last_fetch_error = (
                f"fetch of {self.ref_prefix}/* from {self.remote} failed (rc={p.returncode}): "
                + (tail[-1] if tail else "no output"))
        else:
            self.last_fetch_error = None

    def _read_ref(self, ref: str):
        """Return ``(oid, payload_dict)`` for a claim ref, or ``(None, None)``.

        Reads the LOCAL ref — callers must have run :meth:`_fetch_claims` first.
        A ref that exists but whose blob is unparseable yields ``(oid, None)``:
        the distinction matters, because "present but unreadable" must refuse
        rather than read as absent (guard-487 — an unreadable value is not a
        known-empty one).
        """
        rp = self._git("rev-parse", "--verify", "-q", ref, check=False)
        if rp.returncode != 0:
            return (None, None)
        oid = (rp.stdout or "").strip()
        if not oid:
            return (None, None)
        cat = self._git("cat-file", "-p", oid, check=False)
        if cat.returncode != 0:
            return (oid, None)
        try:
            return (oid, json.loads(cat.stdout))
        except (ValueError, TypeError):
            return (oid, None)

    def _write_ref(self, ref: str, payload: dict, expect_oid: Optional[str]) -> bool:
        """CAS-write ``payload`` to ``ref``. Returns True iff the push landed.

        ``expect_oid=None`` means "expect the ref to be ABSENT" and is encoded
        as the zero-OID lease, which git honours as expect-absent (verified).
        A rejected push returns False — it is the ordinary lost-race outcome,
        not an error.
        """
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        # Not routed through _git: this is the one call site needing stdin, and
        # _git inherits stdin (capture_output does not redirect it), so a
        # stdin-reading command run through it would block on the inherited fd.
        try:
            ho = subprocess.run(
                ["git", "-C", self.repo_root, "hash-object", "-w", "--stdin"],
                input=blob, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise GitRefClaimError(f"hash-object failed: {e}") from e
        if ho.returncode != 0:
            raise GitRefClaimError(f"hash-object failed: {ho.stderr.strip()}")
        new_oid = (ho.stdout or "").strip()
        if not new_oid:
            raise GitRefClaimError("hash-object produced no oid")

        lease = f"{ref}:{expect_oid or ZERO_OID}"
        push = self._git(
            "push", f"--force-with-lease={lease}", self.remote,
            f"{new_oid}:{ref}", check=False,
        )
        if push.returncode == 0:
            # Mirror the accepted value locally so an immediately-following read
            # does not need another network round trip.
            self._git("update-ref", ref, new_oid, check=False)
            return True
        if _is_lease_rejection(push.stderr or ""):
            return False
        # Anything else — no credentials for the remote, an unreachable host, a
        # remote that is not a repository, a server-side hook refusal — is NOT a
        # lost race, and reporting it as one turns an auth failure into
        # "another machine owns a live claim" (measured 2026-08-27, coach-mind
        # on zc-03: anonymous-read origin, no push credential, every acquire
        # answered HELD while the claim store was empty; the operator's agent
        # spent 103 minutes and destroyed the repo's object store chasing a
        # phantom holder). Fail LOUD with the remote's own words instead.
        tail = [ln for ln in (push.stderr or "").strip().splitlines() if ln.strip()]
        raise GitRefClaimError(
            f"push to remote '{self.remote}' failed (rc={push.returncode}) — not a "
            f"lease rejection, so no peer holds the claim; the store is unwritable "
            f"here: {tail[-1] if tail else 'no stderr'}")

    # ------------------------------------------------------------- public API

    def get_runner_state(self, agent_name: str) -> Optional[dict]:
        """Current claim payload for ``agent_name``, or None when absent.

        Always re-fetches: a cached read is exactly the failure the fetch trap
        produces.
        """
        self._fetch_claims()
        ref = claim_ref(agent_name, self.env_id, self.ref_prefix)
        _oid, payload = self._read_ref(ref)
        return payload

    def acquire_runner(self, agent_name: str, token: str) -> bool:
        """Acquire the claim. Returns True on success.

        Raises ``RunnerHeld`` (imported lazily from ``owncloud_backend`` so the
        exception TYPE is shared with the own-cloud arm and a caller's
        ``except RunnerHeld`` works against either) when a live peer holds it.
        """
        RunnerHeld = _runner_held()
        self._fetch_claims()
        ref = claim_ref(agent_name, self.env_id, self.ref_prefix)
        oid, payload = self._read_ref(ref)

        if oid is not None and payload is None:
            # Present but unreadable — refuse rather than clobber. Treating an
            # unparseable claim as absent would let a corrupted blob silently
            # authorise a second runner, which is the exact outcome the lease
            # exists to prevent.
            raise GitRefClaimError(
                f"claim ref {ref} exists but its payload is unreadable; "
                f"refusing to acquire (manual inspection required)")

        if payload and str(payload.get("agent_state", "")).upper() == "RUNNING":
            age = int(time.time()) - int(payload.get("heartbeat_at") or 0)
            if age <= self.runner_stale_seconds:
                raise RunnerHeld(
                    f"{agent_name} is RUNNING on "
                    f"{payload.get('machine_id') or 'unknown-machine'} "
                    f"(heartbeat {age}s old, threshold "
                    f"{self.runner_stale_seconds}s)")
            # Stale — fall through; the CAS below breaks it against the exact
            # oid we just read, so a peer that renewed in between wins and we
            # lose the race cleanly instead of stomping a revived claim.

        if self._write_ref(ref, self._payload(agent_name, token, "RUNNING"), oid):
            return True
        # Lost the CAS: someone else moved the ref between our read and push.
        raise RunnerHeld(
            f"{agent_name}: lost the acquire race (the claim ref moved between "
            f"read and push — a peer acquired it first)")

    def heartbeat(self, agent_name: str, token: str) -> bool:
        """Refresh the heartbeat. Returns True iff this box owns the claim and
        the refresh landed. Returns False when the claim is absent or owned by
        someone else — the caller decides (heartbeat-tick.sh fails open).

        Named ``heartbeat``, NOT ``heartbeat_runner``, to match
        ``OwnCloudBackend.heartbeat`` — ``runner_heartbeat`` calls
        ``get_backend().heartbeat(...)``, so a ``_runner`` suffix here would
        raise AttributeError on every local-backend heartbeat while the other
        three endpoints worked fine. The own-cloud method returns None and this
        one returns bool; the caller discards the value, so the wider return is
        informational only and does not diverge the contract.
        """
        self._fetch_claims()
        ref = claim_ref(agent_name, self.env_id, self.ref_prefix)
        oid, payload = self._read_ref(ref)
        if not payload or not self._is_mine(payload, token):
            return False
        return self._write_ref(
            ref, self._payload(agent_name, token, "RUNNING"), oid)

    def release_runner(self, agent_name: str, token: str) -> bool:
        """Clean RUNNING->IDLE release. Returns True iff THIS call performed the
        transition — matching ``OwnCloudBackend.release_runner``'s contract, on
        which runner-claim.sh's rc=5 "UNCONFIRMED" branch depends."""
        self._fetch_claims()
        ref = claim_ref(agent_name, self.env_id, self.ref_prefix)
        oid, payload = self._read_ref(ref)
        if not payload or not self._is_mine(payload, token):
            return False
        if str(payload.get("agent_state", "")).upper() != "RUNNING":
            return False
        return self._write_ref(
            ref, self._payload(agent_name, token, "IDLE"), oid)

    def reclaim_if_stale(self, agent_name: str) -> bool:
        """Break a crashed peer's claim iff its heartbeat is past the threshold.

        Conditional on the exact oid read, so a just-woken runner and a
        reclaiming peer cannot both win.
        """
        self._fetch_claims()
        ref = claim_ref(agent_name, self.env_id, self.ref_prefix)
        oid, payload = self._read_ref(ref)
        if not payload:
            return False
        if str(payload.get("agent_state", "")).upper() != "RUNNING":
            return False
        age = int(time.time()) - int(payload.get("heartbeat_at") or 0)
        if age <= self.runner_stale_seconds:
            return False
        broken = dict(payload)
        broken["agent_state"] = "IDLE"
        broken["reclaimed_by"] = self.machine_id
        broken["reclaimed_at"] = int(time.time())
        return self._write_ref(ref, broken, oid)

    def list_runner_claims(self) -> List:
        """Every claim under this env-id, as ``RunnerClaim`` tuples.

        Returns the same NamedTuple the own-cloud arm returns so
        ``GET /v1/admin/runner-claims`` and its consumers need no branch.
        """
        RunnerClaim = _runner_claim()
        self._fetch_claims()
        prefix = f"{self.ref_prefix}/{self.env_id}/"
        listing = self._git(
            "for-each-ref", "--format=%(refname)", prefix, check=False)
        if listing.returncode != 0:
            return []
        out = []
        for ref in (listing.stdout or "").splitlines():
            ref = ref.strip()
            if not ref:
                continue
            _oid, payload = self._read_ref(ref)
            if not payload:
                continue
            out.append(RunnerClaim(
                agent=payload.get("agent") or ref.rsplit("/", 1)[-1],
                machine_id=payload.get("machine_id"),
                agent_state=str(payload.get("agent_state") or "IDLE"),
                heartbeat_at=int(payload.get("heartbeat_at") or 0),
                runner_token_fp=payload.get("runner_token_fp"),
            ))
        return out

    # ---------------------------------------------------------------- helpers

    def _payload(self, agent_name: str, token: str, state: str) -> dict:
        now = int(time.time())
        return {
            "agent": agent_name,
            "env_id": self.env_id,
            "machine_id": self.machine_id,
            "agent_state": state,
            "heartbeat_at": now,
            # FINGERPRINT ONLY — never the raw token. See the module docstring.
            "runner_token_fp": _fingerprint(token),
        }

    @staticmethod
    def _is_mine(payload: dict, token: str) -> bool:
        """Whether ``token`` matches the claim's stored fingerprint.

        Compares FINGERPRINTS, never raw tokens, so no comparison path ever
        needs the stored value to be reversible.
        """
        stored = payload.get("runner_token_fp")
        mine = _fingerprint(token)
        return bool(stored) and bool(mine) and stored == mine


_LEASE_REJECTION_MARKERS = ("stale info", "fetch first", "[rejected]")


def _is_lease_rejection(stderr: str) -> bool:
    """True iff a failed push was git refusing the ``--force-with-lease`` CAS
    (the ordinary lost-race outcome). A ``[remote rejected]`` is a server-side
    refusal (hook, protection, permissions) and is deliberately NOT a race.
    """
    text = stderr or ""
    if "[remote rejected]" in text:
        return False
    return any(marker in text for marker in _LEASE_REJECTION_MARKERS)


def _default_machine_id() -> str:
    import socket
    return (os.environ.get("MIND_MACHINE_ID")
            or socket.gethostname()
            or "unknown-machine")


def _fingerprint(token: Optional[str]) -> Optional[str]:
    """Delegate to the own-cloud arm's fingerprint helper when importable so the
    two arms cannot drift to different digests; fall back to the same SHA-256
    prefix when this module is used standalone (tests, a fresh world)."""
    try:
        from owncloud_backend import runner_token_fingerprint
        return runner_token_fingerprint(token)
    except Exception:  # noqa: BLE001 — standalone use is legitimate
        if not token:
            return None
        import hashlib
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _runner_held():
    """Shared exception type with the own-cloud arm; a local stand-in when that
    module is unavailable so this one is importable on its own."""
    try:
        from owncloud_backend import RunnerHeld
        return RunnerHeld
    except Exception:  # noqa: BLE001
        class RunnerHeld(Exception):
            pass
        return RunnerHeld


def _runner_claim():
    try:
        from owncloud_backend import RunnerClaim
        return RunnerClaim
    except Exception:  # noqa: BLE001
        from typing import NamedTuple

        class RunnerClaim(NamedTuple):
            agent: str
            machine_id: Optional[str]
            agent_state: str
            heartbeat_at: int
            runner_token_fp: Optional[str] = None
        return RunnerClaim
