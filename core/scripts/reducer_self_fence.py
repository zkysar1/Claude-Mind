#!/usr/bin/env python3
"""Reducer self-fencing: step down when the lease says someone else holds it.

The DDB runner claim is a LEASE. `OWNERSHIP_STALE_SECONDS` (T_takeover, 3900s)
lets a peer break a claim whose heartbeat has aged out — that half is correct
and is what a lease is for. The missing half is that the HOLDER never checked
whether it still holds. T_stepdown was effectively INFINITY while T_takeover was
finite, which is the inversion of the safe ordering: any renewal gap longer than
T_takeover produces a ZOMBIE LEADER instead of a clean handover.

Measured 2026-08-05 (g-306-225, alpha, cc-04/DESKTOP/cc-07): cc-04 lost its claim
at 14:38 and kept executing goals as reducer for 2.5+ hours, unaware, while two
other bodies acquired the claim. No split-brain occurred only because one session
died on its own and one operator paused theirs — luck and judgment, not mechanism.

This module is the decision half. `decide()` is pure so every branch is testable;
`check()` performs the poll and returns the same dict.

THE SIGNAL ASYMMETRY IS THE WHOLE DESIGN — do NOT collapse the two cases:

  * "my renewal FAILED"                  AMBIGUOUS. A broken writer and a dead
                                         agent are indistinguishable from here.
  * "the claim is held by a DIFFERENT    UNAMBIGUOUS. Positive evidence that this
    machine"                             runner has been superseded.

Self-fencing on the second is safe and correct. Self-fencing on the first ALONE
would convert every transient DDB hiccup into a stopped loop, which is worse than
the disease (guard-1562). So a bare failure never stands this reducer down; only a
failure that has been CONTINUOUS for `t_stepdown` does, and `t_stepdown` is held
strictly below T_takeover so the yield always precedes the seize.

FAIL-SAFE DIRECTION — the MIRROR of worker_reducer_liveness.py, and the reason
this is a separate module rather than a flag on that one. A worker resolves every
ambiguity toward WIND-DOWN (it must never promote itself). A reducer resolves
every ambiguity toward HOLD (it must never stop a healthy loop on a plumbing
fault). Same poll, same rc contract, opposite defaults; fusing them would put the
two fail-safe directions behind one branch and guarantee that a future edit to one
silently inverts the other.

rc contract of `runner-claim.sh status` (measured; see worker_reducer_liveness.py
for the same table, established 2026-08-03):

    rc 0  LIVE       claim row is RUNNING with a fresh heartbeat
    rc 4  NOT LIVE   ABSENT | NOT-RUNNING | STALE | REFUSE (unverifiable)
    rc 2  FAILED     daemon returned an error
    rc 1  daemon error (bash layer maps the daemon's rc=2 to 1)
    rc 3  no daemon

Note rc=0 is NOT "I hold the claim" — it is "SOMEONE holds a live claim for this
agent". The status summary names the machine but the verdict never compares it to
self, which is exactly why a superseded reducer reads all-clear today. Extracting
that machine and comparing it here is the fix.

rc=4 conflates "definitely not live" with "cannot establish", and for THIS
consumer that conflation is harmless in the safe direction: both readings mean no
peer has been observed holding the claim, so the reducer holds. (The same rc=4 is
decisive for a worker. Copying either treatment across is the bug.)

THE SAME-BOX RESTART AXIS (g-306-302, 2026-08-17). A same-box reducer restart —
new token, unchanged machine_id — is invisible to the different-holder read,
because :func:`parse_machine` reads only the machine id. That gap is now closed
by a SECOND decisive trigger, `superseded-token`.

Since g-306-224 the LIVE line of `runner-claim.sh status` — the exact command
:func:`check` already runs — carries `token-fp <digest>`, a non-reversible
fingerprint of the row's runner_token. The signal was already on this module's
wire, unread.

WHY THIS IS DECISIVE ON THE FIRST POLL, where the worker's same axis is not.
`runner-claim.sh` sources the claim's runner_token from
`agents/<agent>/session/runner-token` (its line ~101, the acquire path), so a
REDUCER can hash the very input its own claim was minted from and compare. A
mismatch is positive evidence that the claim row holds a token this process did
not mint — and the token is written only on acquire, so a different token means a
different acquisition happened after this one. That is structurally the same
class of evidence as `different-holder`, which is already decisive.

`worker_reducer_liveness` cannot do this: a worker never minted a token, so it
must LEARN a baseline from its first LIVE poll and can only detect that the fp
MOVED while it watched. This module needs no history, which is why the two
implementations differ rather than being shared.

FALSE-POSITIVE ARGUMENT, owed because this trigger stops a HEALTHY loop
(guard-1562). Both operands must be known or the branch does not fire, so the
only way to mismatch is for the claim to hold a token differing from the one on
this box's disk. Every such case is a re-mint, and a re-mint happens only when a
new runner acquired the claim — i.e. exactly when this reducer IS superseded. The
dangerous readings are the UNKNOWN ones, and all of them hold: an unreadable or
empty token file, a fp the emitter printed as `unknown` (a daemon predating the
field — a mixed-version fleet must degrade to machine-only behaviour, never fence
every reducer), and an unparsable LIVE line all leave the comparison
non-discriminating.

Do NOT close any of this by exposing the raw token. `runner_token` is the
ConditionExpression bearer credential for `heartbeat` and `release_runner`:
publishing it lets any reader forge a heartbeat (defeating `reclaim_if_stale`) or
release a live claim — the very failures the lease exists to prevent. Only
digests cross this module's boundary; :func:`read_own_token_fp` hashes in place
and never returns, stores, or prints the value it read. See
`owncloud_backend.runner_token_fingerprint`.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

VERDICT_HOLD = "hold"
VERDICT_STAND_DOWN = "stand-down"

# Renewal-failure rcs. These are plumbing faults, not evidence about ownership,
# so they only ever matter via the SUSTAINED-duration branch below.
FAILURE_RCS = (1, 2, 3)

# Fallback only. The caller passes the configured value; this exists so a
# decide() call in a test or an inspection seam has a sane number.
DEFAULT_STEPDOWN_SECONDS = 1950

# Wire markers for the `runner-claim.sh status` LIVE line. DERIVED LOCALLY rather
# than imported from worker_reducer_liveness, for the same reason the reducer/
# worker role predicate in main() is (guard-2445): these two modules are
# deliberate MIRRORS with opposite fail-safe directions, and a shared import
# would let a future edit to one silently change the other's meaning. The real
# join is a contract test against the emitter, which is a bash script with
# embedded python that cannot import from either module (guard-920 — pin the
# production shape, never a hand-copy that drifts with it).
LIVE_MARKER = "is RUNNING on "
TOKEN_FP_MARKER = "token-fp "

# What the emitter prints when the daemon supplied no fingerprint. Parsed back to
# None so it is non-discriminating, rather than a fingerprint literally spelled
# "unknown" — which would compare EQUAL between two different runners and read as
# "no takeover", the one direction this axis must never fail in.
TOKEN_FP_UNKNOWN = "unknown"


def decide(rc, observed_machine, self_machine, failure_elapsed_s,
           t_stepdown=DEFAULT_STEPDOWN_SECONDS,
           observed_token_fp=None, self_token_fp=None):
    """Pure decision — no I/O. Returns {verdict, reason, trigger}.

    `failure_elapsed_s` is seconds of CONTINUOUS renewal failure (0 when the
    last renewal succeeded). heartbeat-tick.sh already maintains exactly this
    via the claim-heartbeat-failure marker's first_failed_at, and clears the
    marker on any success — so "continuous" is a property of the input, and a
    blip followed by a success can never accumulate (rb-4842).

    `trigger` is the machine-readable branch name, so a caller can act on WHICH
    condition fired without re-parsing the prose reason.

    `observed_token_fp` / `self_token_fp` are runner-token FINGERPRINTS, never
    tokens. Both are KEYWORD-with-default so every pre-existing positional call
    (including the `decide-only` argv seam and the whole existing unit suite)
    stays valid and keeps its exact prior verdict.
    """
    if rc == 0:
        if observed_machine and self_machine and observed_machine != self_machine:
            return {
                "verdict": VERDICT_STAND_DOWN,
                "trigger": "different-holder",
                "reason": (f"superseded: the live claim for this agent is held by "
                           f"{observed_machine!r}, but this reducer is running on "
                           f"{self_machine!r} — positive evidence of takeover"),
            }
        if (self_token_fp and observed_token_fp
                and observed_token_fp != self_token_fp):
            # SAME-BOX reducer restart — the axis machine_id structurally cannot
            # see, and the reason this branch exists at all. The claim is LIVE,
            # the holder id matches (or is unreadable), but the runner_token
            # behind it is not the one this process minted.
            #
            # Decisive on the FIRST poll, unlike the worker's mirror of this
            # axis: `self_token_fp` is the digest of THIS box's runner-token
            # file, which is the exact input runner-claim.sh acquires with, so a
            # mismatch is positive evidence rather than a learned baseline. The
            # token is written only on acquire, so a token that is not mine means
            # an acquisition happened after mine.
            #
            # Both operands are digests, so printing them is safe AND is the
            # diagnostic value of the axis — a reader sees the identity moved
            # without ever holding the credential
            # (owncloud_backend.runner_token_fingerprint).
            return {
                "verdict": VERDICT_STAND_DOWN,
                "trigger": "superseded-token",
                "reason": (f"superseded: the live claim is on "
                           f"{observed_machine or 'an unreadable machine'!r} but its "
                           f"runner token was re-minted (mine {self_token_fp} -> "
                           f"claim {observed_token_fp}) — a new runner acquired the "
                           f"claim, so this reducer no longer holds it even though "
                           f"the machine id is unchanged"),
            }
        if observed_machine and self_machine:
            # Name the token axis's state rather than staying silent about it. An
            # unreadable fp on either side makes `superseded-token` INERT for this
            # poll, and an inert check that reports the same clean verdict as an
            # armed one is exactly what guard-1760 forbids.
            if self_token_fp and observed_token_fp:
                fp_note = f"; runner token matches ({self_token_fp})"
            else:
                fp_note = (f"; token axis non-discriminating this poll "
                           f"(mine={self_token_fp!r}, claim={observed_token_fp!r}) "
                           f"— holder comparison is the only armed axis")
            return {
                "verdict": VERDICT_HOLD,
                "trigger": "holding",
                "reason": (f"this box ({self_machine!r}) still holds the live claim"
                           f"{fp_note}"),
            }
        # One or both machine ids unknown. The comparison is NON-DISCRIMINATING,
        # not negative — treating an unreadable id as a takeover would stand a
        # healthy reducer down on a parse miss.
        return {
            "verdict": VERDICT_HOLD,
            "trigger": "holder-unreadable",
            "reason": (f"claim is LIVE but the holder comparison is unreadable "
                       f"(observed={observed_machine!r}, self={self_machine!r}) — "
                       f"non-discriminating, so hold"),
        }

    if rc in FAILURE_RCS or rc == 4:
        # Everything below rc=0 is AMBIGUOUS about ownership. Duration is the
        # only thing that makes it decisive.
        if failure_elapsed_s >= t_stepdown:
            return {
                "verdict": VERDICT_STAND_DOWN,
                "trigger": "sustained-renewal-gap",
                "reason": (f"renewal has failed continuously for {failure_elapsed_s}s, "
                           f"at or past T_stepdown={t_stepdown}s (rc={rc}). Stepping "
                           f"down BEFORE a peer may legally take the claim keeps the "
                           f"yield and the seize from overlapping"),
            }
        return {
            "verdict": VERDICT_HOLD,
            "trigger": "ambiguous-not-yet-decisive",
            "reason": (f"rc={rc} says nothing about who holds the claim "
                       f"({failure_elapsed_s}s of failure, T_stepdown={t_stepdown}s) — "
                       f"a transient fault must not stop a healthy loop"),
        }

    # An rc this module has not seen. A reducer must not stop on a signal it
    # cannot interpret — that is the fail-open direction for THIS consumer.
    return {
        "verdict": VERDICT_HOLD,
        "trigger": "unrecognised-rc",
        "reason": (f"unrecognised poll rc={rc} — holding, because an "
                   f"uninterpretable signal is not evidence of supersession"),
    }


def parse_machine(stdout):
    """Pull the holder machine id out of runner-claim.sh's LIVE summary line.

    Format: ... — 'zeta' is RUNNING on 'cc-02', heartbeat 272s old ...
    Returns None when absent; decide() treats an unknown machine as
    non-discriminating rather than as a takeover.
    """
    marker = "is RUNNING on "
    i = stdout.find(marker)
    if i < 0:
        return None
    rest = stdout[i + len(marker):]
    if not rest.startswith("'"):
        return None
    j = rest.find("'", 1)
    return rest[1:j] if j > 0 else None


def parse_token_fp(stdout):
    """Pull the runner-token FINGERPRINT off that SAME LIVE summary line.

    Format: ... heartbeat 272s old (threshold 3900s), token-fp 1f4c0a9b2e6d8035

    LINE-SCOPED deliberately: the marker is searched only AFTER LIVE_MARKER and
    only to the end of THAT line, so a `token-fp` string appearing anywhere else
    in captured stderr — a traceback quoting this module, a peer's diagnostic —
    cannot be mistaken for this claim's fingerprint. A mis-scoped read is worse
    than no read here: it would manufacture a spurious supersession and stand a
    HEALTHY reducer down, which is the one failure this module must not have.

    Returns None when absent, unparsable, or literally TOKEN_FP_UNKNOWN.
    decide() treats a None fp as NON-discriminating, never as a change.
    """
    i = stdout.find(LIVE_MARKER)
    if i < 0:
        return None
    line = stdout[i:].split("\n", 1)[0]
    j = line.find(TOKEN_FP_MARKER)
    if j < 0:
        return None
    rest = line[j + len(TOKEN_FP_MARKER):].strip()
    if not rest:
        return None
    fp = rest.split()[0].strip(",.;:'\"")
    if not fp or fp == TOKEN_FP_UNKNOWN:
        return None
    return fp


def read_own_token_fp(token_path):
    """Fingerprint of THIS box's runner token. The raw value never escapes.

    The token is read, hashed, and dropped inside this function — no caller ever
    receives it, and nothing returned from here can be used as a
    ConditionExpression. That containment is the security property
    (`owncloud_backend.runner_token_fingerprint`), not a stylistic preference.

    Whitespace is stripped the way `runner-claim.sh` strips it before sending the
    token (`tr -d '[:space:]'`, its line ~103) — ALL whitespace, not just the
    ends. The digests must agree byte-for-byte or the comparison is meaningless,
    so this normalization is part of the contract, not an implementation detail.

    The digest is SHA-256 truncated to 16 hex chars, matching
    `owncloud_backend.runner_token_fingerprint`. Computed locally rather than
    imported because importing that module drags the whole storage backend
    (and its cloud SDK) into a per-tick fence; a contract test pins the two to
    the same value.

    Returns None on anything unreadable or empty — an unknown self fp must leave
    the comparison non-discriminating, never guess.
    """
    try:
        raw = Path(token_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    token = "".join(raw.split())
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def read_failure_elapsed(marker_path, now_s):
    """Seconds of continuous renewal failure, from heartbeat-tick.sh's marker.

    Absent marker means the last renewal SUCCEEDED (heartbeat-tick removes it on
    any success), so 0. A corrupt marker also yields 0 — an unreadable duration
    must not be treated as a long one.
    """
    try:
        text = Path(marker_path).read_text(encoding="utf-8")
    except Exception:
        return 0
    for line in text.splitlines():
        if line.startswith("first_failed_at="):
            raw = line.split("=", 1)[1].strip()
            if raw.isdigit():
                return max(0, int(now_s) - int(raw))
            return 0
    return 0


def _machine_id_from_env_file(env_path):
    """MACHINE_ID from .env.local, for boxes that do not export it.

    Deliberately a grep-shaped single-key read, mirroring how heartbeat-tick.sh
    resolves STORAGE_BACKEND: NO secret sourcing, no dotenv parse of the whole
    file. Last assignment wins (same as that shell one-liner's `tail -1`), and an
    inline `# comment` is stripped. Returns None on anything unreadable — an
    unknown self id must leave the comparison non-discriminating, never guess.
    """
    try:
        text = Path(env_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    found = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("MACHINE_ID"):
            continue
        key, sep, val = stripped.partition("=")
        if not sep or key.strip() != "MACHINE_ID":
            continue
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        if val:
            found = val
    return found


def resolve_self_machine(env_path, environ=None):
    """The machine id THIS box compares against the claim holder's.

    MACHINE_ID from the environment, then from .env.local (own-cloud boxes always
    carry it — owncloud_backend.from_env fail-closes without it), then the SAME
    default the local-backend claim WRITER uses: git_ref_claim._default_machine_id
    (MIND_MACHINE_ID, else the hostname). The third rung is the one that makes
    the comparison discriminating on the local backend at all. Measured
    2026-08-28 on a local-backend box (coach, zc-03): .env.local carried no
    MACHINE_ID, the claim read `RUNNING on 'zc-03'` (the writer's hostname
    default), and every tick reported `holder-unreadable` — inert by design, but
    inert on exactly the backend class that never sets the variable, so a lost
    claim would have gone undetected there. Importing the writer's default rather
    than re-deriving it keeps the two arms from drifting to different ids.

    Still not a guess: a self id is only ever the value the claim writer on this
    box would have written. Returns None when even the hostname is unreadable.
    """
    env = os.environ if environ is None else environ
    val = (env.get("MACHINE_ID") or "").strip()
    if val:
        return val
    val = _machine_id_from_env_file(env_path)
    if val:
        return val
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from git_ref_claim import _default_machine_id
    return (_default_machine_id() or "").strip() or None


def load_stepdown_seconds(config_path):
    """runner_heartbeat.stepdown_seconds from aspirations.yaml.

    No hardcoded default on the read path — a missing/invalid block returns None
    so the caller can fail visibly rather than silently fencing on a magic
    number (the rb-313 rule the surrounding config block already follows).
    """
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return None
    val = (data.get("runner_heartbeat") or {}).get("stepdown_seconds")
    return val if isinstance(val, int) and val > 0 else None


def check(agent, scripts_dir, self_machine, marker_path, now_s, t_stepdown,
          token_path=None):
    """Run the real poll and return decide()'s dict plus the raw evidence.

    `token_path` is keyword-with-default so pre-existing callers keep working and
    keep their exact prior verdict: with no path, both fps are None and the
    `superseded-token` branch cannot fire.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _runtime_bash import bash_cmd  # guard-580/581: never a bare "bash" argv[0]

    proc = subprocess.run(
        bash_cmd(str(Path(scripts_dir) / "runner-claim.sh"), "status", "--agent", agent),
        capture_output=True, text=True,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    observed = parse_machine(combined)
    observed_fp = parse_token_fp(combined)
    self_fp = read_own_token_fp(token_path) if token_path else None
    elapsed = read_failure_elapsed(marker_path, now_s)

    result = decide(proc.returncode, observed, self_machine, elapsed, t_stepdown,
                    observed_token_fp=observed_fp, self_token_fp=self_fp)
    result["rc"] = proc.returncode
    result["observed_machine"] = observed
    result["self_machine"] = self_machine
    # Digests only. There is deliberately no raw-token field on this dict for the
    # same reason RunnerClaim has none: the result is printed as JSON to stdout
    # and read by heartbeat-tick.sh, so making the token unrepresentable here
    # means a future caller cannot leak it by adding one line.
    result["observed_token_fp"] = observed_fp
    result["self_token_fp"] = self_fp
    result["failure_elapsed_s"] = elapsed
    result["t_stepdown"] = t_stepdown
    result["poll_output"] = combined.strip()[:400]
    return result


def main(argv):
    if len(argv) > 1 and argv[1] == "decide-only":
        # Test/inspection seam: decide() over argv, no daemon, no marker file.
        # rc observed self elapsed [t_stepdown] [observed_token_fp] [self_token_fp]
        # The two fps are trailing-optional so every pre-existing 5- and 6-arg
        # invocation keeps its exact prior verdict. They are DIGESTS — this seam
        # must never be handed a raw token.
        rc = int(argv[2])
        observed = argv[3] or None
        self_machine = argv[4] or None
        elapsed = int(argv[5])
        t_stepdown = int(argv[6]) if len(argv) > 6 else DEFAULT_STEPDOWN_SECONDS
        observed_fp = (argv[7] or None) if len(argv) > 7 else None
        self_fp = (argv[8] or None) if len(argv) > 8 else None
        out = decide(rc, observed, self_machine, elapsed, t_stepdown,
                     observed_token_fp=observed_fp, self_token_fp=self_fp)
        print(json.dumps(out))
        return 0 if out["verdict"] == VERDICT_HOLD else 1

    scripts_dir = Path(__file__).resolve().parent
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print(json.dumps({
            "verdict": VERDICT_HOLD,
            "trigger": "no-agent",
            "reason": "no MIND_AGENT — not a bound runner context",
        }))
        return 0

    # MACHINE_ID is the same SSOT owncloud_backend.from_env reads. Measured
    # present in the agent shell on cc-02 (2026-08-05) — but heartbeat-tick.sh,
    # this module's ONLY caller, grep-resolves STORAGE_BACKEND from .env.local
    # precisely because that variable is NOT exported into the agent shell, and
    # nothing guarantees MACHINE_ID is exported on every box either. Falling back
    # to the file closes a coverage gap that would otherwise be SILENT: with no
    # self id the holder comparison is non-discriminating, so decide() HOLDS and
    # the fence is inert on that box — the safe direction, but inert is not
    # covered (guard-1760), and a fence that does nothing on some boxes does not
    # fix a fleet-wide split-brain. The third rung — the local claim writer's own
    # default — is what makes the fence discriminating on local-backend boxes,
    # which never set MACHINE_ID at all (coach/zc-03, 2026-08-28).
    self_machine = resolve_self_machine(Path(__file__).resolve().parents[2] / ".env.local")

    t_stepdown = load_stepdown_seconds(scripts_dir.parent / "config" / "aspirations.yaml")
    if t_stepdown is None:
        print(json.dumps({
            "verdict": VERDICT_HOLD,
            "trigger": "config-unreadable",
            "reason": ("runner_heartbeat.stepdown_seconds missing or invalid in "
                       "aspirations.yaml — holding rather than fencing on a "
                       "magic number"),
        }))
        return 0

    sys.path.insert(0, str(scripts_dir))
    import _paths
    agent_dir = Path(_paths.agent_dir(agent))

    # ── THE FENCE GOVERNS THE REDUCER ONLY. ─────────────────────────────────
    # Without this guard the different-holder branch MISFIRES ON EVERY
    # CROSS-BOX WORKER, which is the exact population the lease is supposed to
    # let coexist: a worker on box B runs precisely BECAUSE the reducer holds
    # the claim on box A, so `status` reports LIVE on A, MACHINE_ID is B, and
    # decide() would stand the worker down on its first heartbeat tick. The
    # unit suite cannot catch that — decide() is correct in isolation; the
    # defect would be applying it to a Body it does not govern.
    #
    # Predicate: a Body with NO forked per-session working-memory.yaml is the
    # REDUCER. Same predicate bash-agent-inject uses and the same one
    # worker_reducer_liveness.main derives (guard-2445, derived locally rather
    # than imported so neither module can quietly change the other's meaning).
    # The two fences are complements: this one stands a superseded REDUCER
    # down, that one winds a stranded WORKER down. They must never both fire.
    sid = os.environ.get("MIND_SID", "").strip()
    if not sid:
        # Cannot establish which Body this is. The safe direction for a REDUCER
        # fence is to hold: standing down a Body whose role is unknown risks
        # killing a healthy worker, while holding only delays a stand-down that
        # the next tick (with a SID present) will make. Coverage caveat, stated
        # rather than implied (guard-1760): the fence is INERT on any invocation
        # without MIND_SID. bash-agent-inject exports it on every Bash call, so
        # in the loop it is present.
        print(json.dumps({
            "verdict": VERDICT_HOLD,
            "trigger": "body-role-unreadable",
            "reason": ("no MIND_SID — cannot tell reducer from worker, and a "
                       "fence that governs only the reducer must not act on an "
                       "unknown role"),
        }))
        return 0
    if (agent_dir / "sessions" / sid / "working-memory.yaml").exists():
        print(json.dumps({
            "verdict": VERDICT_HOLD,
            "trigger": "not-the-reducer",
            "reason": ("this Body has a forked per-session working memory, so it "
                       "is a WORKER — a live claim held by another machine is the "
                       "NORMAL condition for a worker, not supersession. Winding "
                       "down is worker_reducer_liveness.py's job, not this one's"),
        }))
        return 0

    marker = agent_dir / "session" / "claim-heartbeat-failure"
    # Same file runner-claim.sh acquires with (its line ~101), which is what makes
    # the fp comparison decisive rather than a learned baseline. Read and hashed
    # inside read_own_token_fp; the raw value never reaches this scope.
    token_file = agent_dir / "session" / "runner-token"

    import time
    out = check(agent, scripts_dir, self_machine, marker, int(time.time()), t_stepdown,
                token_path=token_file)
    print(json.dumps(out))
    return 0 if out["verdict"] == VERDICT_HOLD else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
