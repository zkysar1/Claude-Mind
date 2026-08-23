#!/usr/bin/env python3
"""Parametrized store-cutover ordering gate with DERIVED attestation ( item 3).

Generalizes gate-firings-cutover-check.py (g-328-39) for any store whose writer
flip must wait until reader-capable code is deployed FLEET-WIDE. The hazard is
the same asymmetric silent one: a shared own-cloud store, a box that flips the
writer while a peer still reads only the legacy shape, and the peer reporting a
few hours of data as the full window — a false all-clear.

What this adds over the template (g-115-6578 mechanism A, attest-by-proof):
the per-box attestation is DERIVED from evidence anyone can read, instead of a
hand-run chore on every box:

    derived proof for agent X =
        git merge-base --is-ancestor <seam-commit> <X's latest iteration
        commit on origin/main (newest commit touching agents/X/)>
      AND that commit is recent (ATTESTATION_MAX_AGE_DAYS)
      AND the consumer files at that commit either
            (tier 1) are byte-identical to origin/main, or
            (tier 2) — only for a store declaring `seam_symbols`, and only for
            the files that DIVERGE — still route to a declared seam symbol when
            read at X's own proof commit.

Tier 2 exists because byte-identity is a TRANSPORT for a narrower property:
every unrelated edit to any consumer breaks the proof without touching whether
the consumer routes to the seam. On a dev tier moving at ~2.9 consumer
commits/day (measured 2026-08-21) that made SAFE close to unsatisfiable. It is
opt-in per store and reports its own reason (`seam_routed_despite_divergence`)
so a reader can always tell which tier carried a verdict; a store that declares
no `seam_symbols` keeps byte-identity as its sole predicate. Decision, the
accepted residual risk, and the explicitly-rejected alternatives:
core/config/rationale/store-cutover-attestation-predicate.md

A box that commits its iteration state after pulling the seam has proven it
carries the seam — a verifiable fact should never be a scheduling problem.
The hand-stamp path (--attest, team-state shard field) is kept ONLY as the
fallback for a box with no commits since the seam. The gate-firings cutover
starved 3 days on exactly that chore (g-115-6243, rb-8202).

FAIL-CLOSED BY CONSTRUCTION, same as the template: unreadable roster, missing
proof AND missing stamp, unparseable timestamps, git errors — all report
UNSAFE. The thing being gated is a silent false all-clear, so an error here
must never read as permission to proceed.

Freshness: origin/main is only as fresh as the last fetch (guard-1382,
rb-4716), so check mode fetches origin main first and reports fetch_ok. A
failed fetch still runs the evaluation — stale refs can only under-report
proofs, which is the UNSAFE direction — but the failure is surfaced.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import PROJECT_ROOT  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581)

ATTESTATION_MAX_AGE_DAYS = 30

# Liveness window for the worker-carrier proof lane (). A carrier ref
# only counts as evidence while a RUNNING Body is still pushing to it, and
# guard-3660 is the authority on that join: "compare claimed_at against now;
# 74h+ is stale, single-digit hours is live". This picks the CONSERVATIVE end of
# that band — 6h, the same partner-liveness threshold
# `.claude/rules/check-team-state-before-silent.md` already uses fleet-wide, so
# the number is an existing constant rather than a new invention.
#
# Erring SHORT is the fail-closed direction HERE, and it is worth stating which
# way that runs because it inverts between the two consumers of guard-3660:
# retirement asks "may I DELETE this ref" (short window = delete a live Body's
# carrier = bad), while this lane asks "may I TRUST this ref as proof" (short
# window = fall back to the hand-stamp = safe). Same guardrail, opposite
# safe directions. Do not import a threshold from the retirement side.
BODY_LIVENESS_MAX_AGE_HOURS = 6

# Known cutovers. Each entry is the full parameter set; --seam-commit /
# --consumers / --field / --flag override or replace an entry ad hoc.
STORES = {
    # /39: date-segment meta/gate-firings.jsonl behind
    # GATE_FIRINGS_SEGMENTED. The ORIGINAL cutover of this family, and the one
    # whose hand-stamp chore starved 3 days (, rb-8202) — the
    # incident that motivated derived attestation. It kept its own 279-line
    # implementation until  folded it in here, so the tool built to
    # end that starvation did not cover the cutover that caused it.
    #
    # Seam = 18e465af1, the single commit where all three consumers gained the
    # firings_paths() call (verified: each consumer's first and only
    # pickaxe hit on 'firings_paths(' is that commit, and it is an ancestor of
    # origin/main). Later /93f11b924 fixes are writer-side and do not
    # move the reader seam.
    #
    # seam_symbols is what makes this migration lossless: this cutover's local
    # predicate was never "match origin/main", it was "the consumers CALL the
    # seam" — a strictly stronger content check that byte-identity cannot
    # express. See _symbol_report / _calls_symbol.
    #
    # ONE-ELEMENT SET (). This store is the reason the field was
    # singular: all 3 consumers call the same symbol, so one string expressed it
    # exactly. Migrated to a set for `utilization`, whose 17 consumers use
    # different parts of one reader API — measured, no single symbol exceeds
    # 12/17. Behaviour here is unchanged: "calls >= 1" over a one-element set is
    # the singular predicate.
    "gate_firings": {
        "seam_commit": "18e465af132584b723cf3d588aa46c5f0506fb08",
        "field": "gate_firings_seam",
        "flag": "GATE_FIRINGS_SEGMENTED",
        "seam_symbols": ["firings_paths"],
        "consumers": [
            "core/scripts/gate-stats.py",
            "core/scripts/gate-retirement-eval.py",
            "core/scripts/override-ledger-consume.py",
        ],
    },
    # : utilization counters out of reasoning-bank.jsonl +
    # guardrails.jsonl into spooled sidecars + date-segmented content.
    # Seam = last reader commit (unit 4-8 + item-1 set all ancestors of it).
    #
    # The 7 reader-API symbols, measured across all 17 consumers at origin/main
    # (2026-08-21, cc-02): load_counters 12/17, utilization_of 12/17,
    # store_paths 4/17, load_all_counters 3/17, counters_path 1/17,
    # segment_name 1/17. NO SINGLE SYMBOL EXCEEDS 12 — which is precisely why a
    # set is required rather than preferred: the one-symbol model cannot express
    # a store whose consumers legitimately use different parts of one API.
    # "calls >= 1" covers 16/17; the 17th (_curation_predicate.py) was a checker
    # false-negative fixed in , not a consumer off the seam.
    #
    # UTILIZATION_COUNTERS_SPOOLED is kind="name": it is the cutover FLAG
    # itself — the most seam-defining token in the set — and a constant is never
    # called, so a call-only predicate would make it permanently invisible
    # ( defect b).
    "utilization": {
        "seam_commit": "0c0bb0073a37d8eef1a69849d3965ebab7f0d004",
        "field": "utilization_seam",
        "flag": "UTILIZATION_COUNTERS_SPOOLED",
        "seam_symbols": [
            "load_counters",
            "utilization_of",
            "store_paths",
            "load_all_counters",
            "counters_path",
            "segment_name",
            {"name": "UTILIZATION_COUNTERS_SPOOLED", "kind": "name"},
        ],
        "consumers": [
            "core/scripts/_curation_predicate.py",
            "core/scripts/_rb_helpers.py",
            "core/scripts/_utilization_store.py",
            "core/scripts/build-agent-context.py",
            "core/scripts/bulk-retire-dead-entries.py",
            "core/scripts/guardrail_retire.py",
            "core/scripts/learning-routing-audit.py",
            "core/scripts/poignancy-ab-probe.py",
            "core/scripts/retrieval_utility_report.py",
            "core/scripts/retrieve.py",
            "core/scripts/scar-tissue-check.py",
            "core/scripts/utilization-feedback.py",
            "core/scripts/utilization-stats.py",
            "core/scripts/weakness-signals.py",
            "mind_api/src/endpoints/retrieve.py",
            "mind_api/src/endpoints/utilization.py",
            "mind_api/src/world/reasoning_bank.py",
        ],
    },
    # : transparent gzip transport for the own-cloud hot stores. The
    # writer flag OWNCLOUD_GZIP_STORES names ENV-IDS (e.g. "ayoai-mind"), never a
    # bare boolean — see core/scripts/_owncloud_codec.py. Seam = the reader
    # landing (codec + backend decode + plain-md5 fence model + every raw
    # get_object reader routed through the codec). The hazard is the mirror of
    # the segmentation one and just as silent: a box whose backend predates the
    # seam pulls the gzip bytes RAW into its local mirror and its parsers /
    # merge lanes then read garbage. The derived proof (an iteration commit
    # after the seam) also covers the DAEMON in practice:  auto-
    # restarts a stale-code daemon on the next wrapper call after a pull, and an
    # iteration cannot complete without wrapper calls.
    "gzip": {
        "seam_commit": "ad2ae3207f7f9f56ae21b6e5468d9a9584492a94",
        "field": "owncloud_gzip_seam",
        "flag": "OWNCLOUD_GZIP_STORES",
        "consumers": [
            "core/scripts/_owncloud_codec.py",
            "core/scripts/owncloud_backend.py",
            "core/scripts/owncloud_sync.py",
            "core/scripts/storage_backend.py",
            "core/scripts/cold-snapshot-tick.py",
            "core/scripts/tree-body-presence-audit.py",
            "core/scripts/worker_stall.py",
            "mind_api/src/endpoints/aspirations_write.py",
        ],
    },
}


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _fetch_origin_main() -> bool:
    try:
        return _git("fetch", "origin", "main", timeout=60).returncode == 0
    except Exception:
        return False


def _fetch_worker_refs() -> bool:
    """Make refs/workers/* readable locally, for the carrier proof lane.

    WITHOUT THIS THE LANE IS A SILENT ZERO. The refs are pushed by worker Bodies
    on OTHER boxes; a box that has never run worker-ref-consume.sh has none of
    them locally, so every carrier candidate would resolve to "ref not readable"
    and the lane would report exactly what an agent with no live Body reports.
    A proof source that quietly finds nothing is worse than no proof source: it
    reads as evidence of absence (guard-1665 class).

    NO --prune, deliberately, and this is the one flag that matters here. Pruning
    DELETES local refs whose remote counterpart is gone, which is a RETIREMENT
    decision that guard-3660 governs (a ref may be gone from origin while its
    Body is mid-goal). A read-only proof lane must never retire anything.
    """
    try:
        return _git("fetch", "origin", "+refs/workers/*:refs/workers/*",
                    timeout=60).returncode == 0
    except Exception:
        return False


def _live_body_sids(bodies: dict | None, now: datetime) -> list[str]:
    """SIDs whose in_flight_bodies row is LIVE (the guard-3660 liveness join).

    This is the substantive half of the carrier lane, not the ancestry check:
    ancestry says the ref CONTENT is good, this says the ref belongs to a Body
    that is still running. Every uncertain row is dropped rather than assumed
    live — an unreadable row falls back to the other lanes, which is safe.
    """
    live: list[str] = []
    if not isinstance(bodies, dict):
        return live
    for sid, row in sorted(bodies.items()):
        if not isinstance(row, dict) or not isinstance(sid, str) or not sid:
            continue
        when = _parse_ts(str(row.get("claimed_at") or "")
                         .split("+")[0].split("Z")[0])
        if when is None:
            continue
        # A FUTURE stamp passes. Negative age means clock skew between boxes,
        # and skew is not staleness — rejecting it would drop a genuinely live
        # Body over a few seconds of drift. The content checks below are what
        # actually guarantee reader-capability; this join only scopes WHICH
        # refs are current.
        if (now - when).total_seconds() / 3600.0 <= BODY_LIVENESS_MAX_AGE_HOURS:
            live.append(sid)
    return live


def _prove_commit(commit: str, ciso: str, seam_commit: str,
                  consumers: list[str], now: datetime,
                  seam_symbols=None) -> dict:
    """Ancestry + consumer routing + recency for ONE candidate commit.

    Extracted from derive_proof so both proof lanes apply the IDENTICAL test —
    a second lane with its own copy of these three checks would drift, and the
    drift would show up as one lane proving a box the other refuses.

    TWO TIERS (g-358-23, implemented g-358-28), tried in order:

      1. BYTE-IDENTITY to origin/main across every consumer. Tried first, still
         wins when it holds: strictly stronger than tier 2 and costs one
         `git diff`.
      2. PER-FILE SEAM ROUTING, reached ONLY on divergence and scoped to the
         DIVERGING FILES ONLY — files that did not diverge were already settled
         by tier 1. Each diverging path is read at THIS box's own proof commit
         (`git show <commit>:<path>`) and must still route to a declared seam
         symbol. Requires the store to declare `seam_symbols`; without them
         tier 1 is the sole predicate and divergence still refuses.

    The two proven shapes are DISTINGUISHABLE ON PURPOSE: tier 2 carries
    `reason="seam_routed_despite_divergence"` where tier 1 carries no reason at
    all, so a reader can always tell which predicate carried a verdict. Never
    make them equal.
    """
    when = _parse_ts(ciso.split("+")[0].split("Z")[0])
    if when is None:
        return {"proven": False, "reason": "unparseable_commit_date",
                "commit": commit[:9]}
    age_days = (now - when).total_seconds() / 86400
    if age_days > ATTESTATION_MAX_AGE_DAYS:
        return {"proven": False, "reason": "iteration_commit_stale",
                "commit": commit[:9], "age_days": round(age_days, 1)}
    anc = _git("merge-base", "--is-ancestor", seam_commit, commit)
    if anc.returncode != 0:
        return {"proven": False, "reason": "seam_not_ancestor",
                "commit": commit[:9]}
    # Rationale (WHY two tiers, and what tier 2 gives up):
    # core/config/rationale/store-cutover-attestation-predicate.md
    # Byte-identity is a TRANSPORT for a narrower property ("the consumers route
    # to the seam"), so every unrelated edit to any consumer breaks the proof
    # without touching the property — measured 2026-08-21 at 2.9 consumer
    # commits/day on origin/main (a DATED observation of a moving repo, not a
    # constant: re-measure before quoting it), which made SAFE close to
    # unsatisfiable for STORES['utilization'].
    diff = _git("diff", "--name-only", commit, "origin/main", "--", *consumers)
    if diff.returncode != 0:
        return {"proven": False, "reason": "diff_failed", "commit": commit[:9]}
    changed = [l for l in diff.stdout.splitlines() if l.strip()]
    if changed:
        specs = _symbol_specs(seam_symbols)
        if not specs:
            # FAIL-CLOSED DEFAULT. A store whose seam nobody has characterised
            # cannot reach the narrower tier — opt-in per store, by design.
            return {"proven": False, "reason": "consumers_diverge_from_main",
                    "commit": commit[:9], "diff_files": changed[:10]}
        # TIER 2, scoped to the DIVERGING files only. Read each at the box's own
        # proof commit — NOT at origin/main, which would prove nothing about
        # this box, and NOT from the working tree, which is a different box's.
        missing, unreadable, routed = [], [], []
        for path in changed:
            out = _git("show", f"{commit}:{path}")
            if out.returncode != 0:
                # guard-487: unreadable input REFUSES. A consumer missing at the
                # box's commit is exactly the pre-seam state this gate exists to
                # catch, and `git show` failing is indistinguishable from it.
                unreadable.append({"consumer": path,
                                   "error": out.stderr.strip()[:160]})
                continue
            matched = _calls_any_symbol(out.stdout, specs)
            if matched:
                routed.append({"consumer": path, "symbol": matched})
            else:
                missing.append(path)
        if missing or unreadable:
            return {"proven": False,
                    "reason": "diverging_consumers_do_not_route_to_seam",
                    "commit": commit[:9], "diff_files": changed[:10],
                    "missing": missing, "unreadable": unreadable}
        return {"proven": True, "reason": "seam_routed_despite_divergence",
                "commit": commit[:9], "committed_at": ciso,
                "age_days": round(age_days, 1),
                "diff_files": changed[:10], "routed": routed}
    return {"proven": True, "commit": commit[:9],
            "committed_at": ciso, "age_days": round(age_days, 1)}


def _agents_on_main() -> list[str]:
    """Agent names present as agents/<name>/ directories on origin/main."""
    try:
        out = _git("ls-tree", "--name-only", "origin/main", "agents/")
        if out.returncode != 0:
            return []
        return sorted(
            p.split("/", 1)[1].rstrip("/")
            for p in out.stdout.split()
            if p.startswith("agents/") and p != "agents/"
        )
    except Exception:
        return []


def derive_proof(agent: str, seam_commit: str, consumers: list[str],
                 bodies: dict | None = None, seam_symbols=None) -> dict:
    """Evidence-derived attestation for one agent, from git alone.

    TWO proof lanes, tried in order; the FIRST that proves wins and the winner
    is named in the result (`lane`, and `ref` for the carrier lane) so a reader
    can audit which candidate carried the verdict:

      agent_namespace   — the newest commit touching agents/<agent>/ on
                          origin/main. The original lane.
      worker_carrier_ref — the tip of refs/workers/<agent>/<sid> for each LIVE
                          Body (g-115-6672). A Body is a checkout too.

    WHY THE SECOND LANE EXISTS. Under one-Mind-many-Bodies, the newest
    agents/<agent>/ commit proves the tree of whichever Body committed LAST and
    says nothing about the others — so an agent whose Bodies run on several
    boxes is proven by exactly one of them. A Body that has not committed
    agent-namespace churn recently falls to the hand-stamp, which is the chore
    this whole file exists to delete. Its carrier ref is a direct statement of
    that Body's HEAD, pushed every work unit.

    `bodies` is the agent's team-state `in_flight_bodies` map. Omitted (the
    default) means the carrier lane does not run at all, so every existing
    caller and test keeps the single-lane behaviour byte-for-byte.

    Every uncertain branch returns proven=False with a reason — the caller
    falls back to the hand-stamp, never to an assumption. When BOTH lanes fail
    the reported `reason` is the agent-namespace lane's, unchanged, with the
    carrier attempts listed under `carrier_candidates` for diagnosis: the
    original reason is what operators and the existing detail keys already key
    on, and a new lane must not relabel a failure it did not cause.
    """
    now = datetime.now()
    try:
        out = _git("log", "-1", "--format=%H|%cI", "origin/main",
                   "--", f"agents/{agent}/")
        line = out.stdout.strip()
        if out.returncode != 0 or "|" not in line:
            primary = {"proven": False, "reason": "no_iteration_commit"}
        else:
            commit, ciso = line.split("|", 1)
            primary = _prove_commit(commit, ciso, seam_commit, consumers, now,
                                    seam_symbols)
        if primary.get("proven"):
            primary["lane"] = "agent_namespace"
            return primary

        attempts = []
        for sid in _live_body_sids(bodies, now):
            ref = f"refs/workers/{agent}/{sid}"
            tip = _git("log", "-1", "--format=%H|%cI", ref)
            tline = tip.stdout.strip()
            if tip.returncode != 0 or "|" not in tline:
                attempts.append({"ref": ref, "reason": "carrier_ref_unreadable"})
                continue
            commit, ciso = tline.split("|", 1)
            proof = _prove_commit(commit, ciso, seam_commit, consumers, now,
                                  seam_symbols)
            if proof.get("proven"):
                proof["lane"] = "worker_carrier_ref"
                proof["ref"] = ref
                return proof
            attempts.append({"ref": ref, "reason": proof.get("reason"),
                             **{k: proof[k] for k in ("commit", "diff_files")
                                if k in proof}})
        if attempts:
            primary["carrier_candidates"] = attempts
        return primary
    except Exception as exc:
        return {"proven": False, "reason": f"git_error: {exc}"}


def _parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", ""))
    except ValueError:
        return None


# Diagnostic keys derive_proof already computes and the roster verdict used to
# DROP on the floor. Only `reason` was carried, so an operator reading UNSAFE
# got "consumers_diverge_from_main" for N agents with no way to tell WHICH
# consumer, or against which commit, without re-deriving each one by hand —
# measured on the utilization cutover 2026-08-18: three agents unproven, and
# finding that all three diverged on the SAME single file took a hand-run
# `git diff <agent-commit> origin/main -- <17 consumers>` per agent. The
# information was in the proof dict the whole time.
#
# `commit` is the one people misread, so it is worth naming: it is the commit
# that was EXAMINED (that agent's newest self-namespace commit on origin/main),
# NOT a commit that proves anything — these are the FAILING branches.
_DERIVATION_DETAIL_KEYS = ("commit", "diff_files", "age_days")


def _derivation_detail(proof: dict) -> dict:
    """Surface derive_proof's per-agent evidence on the FAILING branches."""
    if not isinstance(proof, dict):
        return {}
    return {f"derivation_{k}": proof[k]
            for k in _DERIVATION_DETAIL_KEYS if k in proof}


def evaluate_roster(roster: dict, proofs: dict, field: str,
                    now: datetime, local: dict | None = None) -> dict:
    """Pure decision core: classify every agent, then the fleet verdict.

    proofs: agent -> derive_proof() result. Derived proof outranks the stamp;
    a fresh stamp rescues a box the derivation cannot prove; nothing rescues
    a box with neither. Empty roster is UNSAFE (guard-1665 — a predicate that
    returns clean because it matched nothing).

    local: _local_report() for the box RUNNING this check, or None to skip the
    local half (the roster-only shape every caller had before g-358-05's
    2026-08-18 wiring). Not None means the local box gets a veto — see the
    block below the roster verdict for why an agent-keyed proof cannot answer
    a box-keyed question.
    """
    attested, unattested, stale, retired = [], [], [], []
    for name, row in sorted(roster.items()):
        if not isinstance(row, dict):
            unattested.append({"agent": name, "reason": "unreadable shard"})
            continue
        if row.get("retired_at"):
            retired.append(name)
            continue
        proof = proofs.get(name) or {"proven": False, "reason": "no_proof_run"}
        if proof.get("proven"):
            # `lane` / `ref` name WHICH candidate proved this agent ().
            # Without them a derived attestation is unauditable the moment there
            # is more than one lane: "basis: derived" plus a bare sha does not
            # say whether it came from the agent namespace or some Body's
            # carrier ref, and those are claims about different checkouts.
            attested.append({"agent": name, "basis": "derived", **{
                k: proof[k]
                for k in ("commit", "committed_at", "age_days", "lane", "ref")
                if k in proof}})
            continue
        seam = row.get(field)
        if not isinstance(seam, dict):
            unattested.append({"agent": name,
                               "derivation": proof.get("reason"),
                               **_derivation_detail(proof),
                               "last_active": row.get("last_active")})
            continue
        when = _parse_ts(seam.get("attested_at"))
        if when is None:
            unattested.append({"agent": name,
                               "reason": "unparseable attested_at",
                               "derivation": proof.get("reason"),
                               **_derivation_detail(proof)})
        elif now - when > timedelta(days=ATTESTATION_MAX_AGE_DAYS):
            stale.append({"agent": name, "attested_at": seam.get("attested_at"),
                          "age_days": round((now - when).total_seconds() / 86400, 1),
                          "derivation": proof.get("reason"),
                          **_derivation_detail(proof)})
        else:
            attested.append({"agent": name, "basis": "stamp",
                             "commit": seam.get("commit"),
                             "attested_at": seam.get("attested_at")})

    blockers = len(unattested) + len(stale)
    if not roster or (not attested and not unattested and not stale):
        verdict, reason = "UNSAFE", "empty_roster"
    elif blockers:
        verdict, reason = "UNSAFE", "unattested_or_stale_boxes"
    else:
        verdict, reason = "SAFE", ("every live agent proven by derivation "
                                   "or current stamp")

    # THE BOX RUNNING THIS CHECK IS NOT IN THE ROSTER (, 2026-08-18).
    # Every proof above is AGENT-keyed: derive_proof() reads the newest commit
    # touching agents/<name>/ on origin/main. Under one-Mind-many-Bodies an
    # agent runs a Body on several boxes, so that commit proves the tree of
    # whichever Body committed last and says NOTHING about any other. The box
    # you are standing on can therefore be arbitrarily far behind and still
    # read SAFE off a sibling Body's attestation — and SAFE is read as
    # permission to flip the writer, usually from this very box.
    #
    # Measured on cc-07: 27 commits behind with a merge wedge, missing
    # f258c5693 (a consumer fix belonging to THIS goal), verdict SAFE, alpha
    # "proven" by 1b7af213e — a commit authored on cc-04. The harm is the one
    # the guidance string already names: "a peer without the readers sees
    # partial data and reports it as the full window."
    #
    # _local_report() answers the box question correctly and has existed since
    # this file was written; it was simply never wired into the verdict, only
    # into the --attest path. So this is a wiring gap, not a missing capability
    # (which is why the fix is a veto here rather than a new derivation).
    # Fail-closed like every other branch: a local box that cannot prove itself
    # blocks, and an unreadable local report has seam_present False already.
    if isinstance(local, dict):
        if not local.get("seam_present"):
            verdict = "UNSAFE"
            # Only claim the local box as THE cause when the roster half was
            # otherwise clean — never mask empty_roster or a peer's failure,
            # which are the broader problems.
            if roster and not blockers:
                reason = "local_box_not_reader_capable"

    out = {"verdict": verdict, "reason": reason, "attested": attested,
           "unattested": unattested, "stale": stale,
           "retired_skipped": retired}
    if local is not None:
        out["local_box"] = local
    return out


def _read_team_state() -> tuple[dict, str | None]:
    try:
        out = subprocess.run(
            bash_cmd(PROJECT_ROOT / "core" / "scripts" / "team-state-read.sh",
                     "--json"),
            capture_output=True, text=True, timeout=90,
        )
        if out.returncode != 0:
            return {}, f"team-state-read exit {out.returncode}: {out.stderr.strip()[:200]}"
        return json.loads(out.stdout), None
    except Exception as exc:
        return {}, f"team-state unreadable: {exc}"


def _strip_comments(text: str) -> str:
    """Drop `#` comments so a prose mention of the seam cannot pass as a call.

    Deliberately naive: it ignores `#` inside string literals, so a line like
    `msg = "call seam_fn(x) # like this"` keeps its text. That errs toward
    counting a consumer as OK, which is the wrong direction — but the
    alternative (a real Python parse) buys precision this check does not need.
    If a consumer ever carries such a literal, the honest fix is `ast`, not a
    cleverer regex. (Ported verbatim-in-spirit from the gate-firings check this
    file absorbed; its two mutation proofs came with it.)
    """
    out = []
    for line in text.splitlines():
        idx = line.find("#")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


SYMBOL_KINDS = ("call", "name")


def _symbol_spec(declared: str | dict) -> tuple[str, str]:
    """Normalize a declared `seam_symbol` into (name, kind).

    A STORES entry may declare a bare STRING (the historical form, always a
    CALL) or a DICT {"name": ..., "kind": "call"|"name"}. The dict form exists
    because a module CONSTANT is often the most seam-defining token a cutover
    has — the feature flag itself — and a constant is never called, so a
    call-only predicate makes a flag-named seam PERMANENTLY UNSATISFIABLE
    (g-358-27 defect b). One key rather than two so the name and its mode
    cannot drift apart.
    """
    if isinstance(declared, dict):
        name, kind = declared.get("name"), declared.get("kind", "call")
    else:
        name, kind = declared, "call"
    if not name or kind not in SYMBOL_KINDS:
        raise ValueError(f"bad seam_symbol spec: {declared!r}")
    return name, kind


def _symbol_specs(declared) -> list[tuple[str, str]]:
    """Normalize a declared `seam_symbols` into a list of (name, kind).

    Accepts None/empty (-> []), a bare string or dict (the historical SINGULAR
    form, normalized to a one-element list), or any sequence of those. One
    normalizer rather than two call shapes so no caller can disagree with
    another about what a declaration means.

    An EMPTY result is meaningful and is the fail-closed default: a store that
    declares no seam symbols keeps byte-identity as its sole predicate, so the
    narrower per-file tier is opt-in per store and unreachable for a store whose
    seam nobody has characterised (g-358-23 decision part 4).
    """
    if not declared:
        return []
    if isinstance(declared, (str, dict)):
        declared = [declared]
    return [_symbol_spec(d) for d in declared]


def _calls_any_symbol(text: str, specs: list[tuple[str, str]]) -> str | None:
    """The FIRST declared symbol this source routes to, or None.

    "calls >= 1", not "calls ALL" — measured on the 17 `utilization` consumers,
    requiring all 7 would be unsatisfiable by design, because the consumers use
    different parts of one reader API on purpose. Returns the matching NAME
    rather than a bool so a report can state WHICH symbol carried a consumer;
    a verdict nobody can attribute is the shape this whole change exists to fix.
    """
    for name, kind in specs:
        if _calls_symbol(text, name, kind):
            return name
    return None


def _local_bindings(text: str, symbol: str) -> set[str]:
    """Local names bound to `symbol` by an import in this source.

    `from M import N as A` makes `A()` a genuine call to N, but no literal N
    survives at the call site. The historical predicate stripped the file's one
    `import N` and then found nothing, reporting MISSING — measured on
    core/scripts/_curation_predicate.py, the sole zero-token consumer of 17
    (g-358-27 defect a). ast is the honest fix the old docstring named.

    Unparseable source degrades to {symbol} rather than raising: a syntax error
    is the consumer's problem, not this predicate's, and the caller already
    fails closed on a missing symbol.
    """
    names = {symbol}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return names
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == symbol and alias.asname:
                    names.add(alias.asname)
    return names


def _strip_import_lines(text: str) -> str:
    """Drop whole import statements (NAME mode only).

    In CALL mode an import can never match — the pattern requires a `(` after
    the name and an import has none — but in NAME mode a bare `import SYMBOL`
    would otherwise count as USING it, which is exactly the false all-clear the
    original docstring warns about, one layer over.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return text
    drop: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = getattr(node, "end_lineno", None) or node.lineno
            drop.update(range(node.lineno, end + 1))
    return "\n".join(line for i, line in enumerate(text.splitlines(), 1)
                     if i not in drop)


def _calls_symbol(text: str, symbol: str, kind: str = "call") -> bool:
    """Does this source CALL (or, in NAME mode, USE) `symbol`?

    Two ways a cheaper check reports a false ALL-CLEAR, both observed in the
    real consumers this predicate was written for:
      - `import symbol` that nothing calls leaves the consumer on the legacy
        path — the exact pre-seam state — while a bare symbol grep succeeds;
      - a COMMENT containing `symbol(` does the same. Not hypothetical: two of
        the three gate-firings consumers carry exactly such a comment above the
        call, so reverting the call and leaving the prose passes an uncommented
        check. Same referent trap as guard-1685 — the token survives its own
        removal.

    And a third, reached by a different mechanism than comment-stripping, so
    the two warnings above do not cover it: the old `f"{symbol}(" in text` had
    NO LEFT WORD BOUNDARY, so `_get_backend(` and `maybe_get_backend(` both
    passed as calls to `get_backend`. A revert renaming a public call to a
    private `_`-prefixed sibling still reported symbol_present, defeating
    _symbol_report's stated purpose (g-358-27 addendum). The left-anchored
    pattern below closes it, and closes the PEP8-legal `get_backend (1)`
    false-NEGATIVE in the same edit via `\\s*`.

    Anchor only the LEFT side of the call form. A right anchor is what `\\s*\\(`
    already is, and widening the comment-stripping to reach any of this would
    be the false-all-clear direction.
    """
    if kind not in SYMBOL_KINDS:
        raise ValueError(f"unknown symbol kind: {kind!r}")
    body = _strip_comments(text)
    if kind == "name":
        body = _strip_import_lines(body)
    for name in _local_bindings(text, symbol):
        esc = re.escape(name)
        pattern = (rf"(?<![A-Za-z0-9_]){esc}\s*\("
                   if kind == "call"
                   else rf"(?<![A-Za-z0-9_]){esc}(?![A-Za-z0-9_])")
        if re.search(pattern, body):
            return True
    return False


def _symbol_report(symbol: str, consumers: list[str], ref: str) -> dict:
    """Do the consumers CALL `symbol` at `ref`? Fail-closed on any unreadable.

    WHY THIS EXISTS ALONGSIDE BYTE-IDENTITY, which is the non-obvious half:
    derive_proof() proves an agent's tree is byte-identical to origin/main, and
    _local_report() proves the same for this box. Both are RELATIVE — they say
    "you match main", never "main is reader-capable". If main itself lost the
    call (a revert landing after the seam commit), every box matches a broken
    main and the whole fleet reports proven. Ancestry cannot see it either: the
    seam commit stays an ancestor of a commit that reverts it.

    So this is checked ONCE against `ref` (origin/main) as a fleet-level veto,
    not per agent — byte-identity then carries it to every box that matches.
    """
    specs = _symbol_specs(symbol)
    missing, unreadable, ok = [], [], []
    for path in consumers:
        out = _git("show", f"{ref}:{path}")
        if out.returncode != 0:
            unreadable.append({"consumer": path,
                               "error": out.stderr.strip()[:160]})
            continue
        matched = _calls_any_symbol(out.stdout, specs)
        if matched:
            ok.append({"consumer": path, "symbol": matched})
        else:
            missing.append(path)
    return {"symbol_present": not missing and not unreadable,
            "symbols": [{"name": n, "kind": k} for n, k in specs],
            "ref": ref, "ok": ok, "missing": missing, "unreadable": unreadable}


def _local_report(seam_commit: str, consumers: list[str],
                  seam_symbols=None) -> dict:
    """Is THIS box's tree reader-capable? Ancestry + working-tree identity.

    Working-tree diff (not HEAD diff) so an uncommitted local revert of a
    consumer refuses the attest — deployed bytes are what execute. When the
    store declares `seam_symbols`, the working-tree FILES must also route to one
    of them: the strictest available read, since these are the bytes this box
    executes.

    DELIBERATELY NOT GIVEN THE SECOND TIER that `_prove_commit` gains
    (g-358-23 decision part 2 scopes the tier to per-remote-box proofs only).
    Divergence here is UNCOMMITTED local drift on the box you are standing on —
    the one case where "pull, then re-run" is both available and correct — so
    admitting it would trade a refusal the operator can clear in one command for
    a residual risk nobody needs to accept.
    """
    try:
        anc = _git("merge-base", "--is-ancestor", seam_commit, "HEAD")
        if anc.returncode != 0:
            return {"seam_present": False, "reason": "seam_not_ancestor_of_HEAD"}
        diff = _git("diff", "--name-only", "origin/main", "--", *consumers)
        if diff.returncode != 0:
            return {"seam_present": False, "reason": "diff_failed"}
        changed = [l for l in diff.stdout.splitlines() if l.strip()]
        if changed:
            return {"seam_present": False,
                    "reason": "consumers_differ_from_origin_main",
                    "diff_files": changed[:10]}
        specs = _symbol_specs(seam_symbols)
        if specs:
            missing, unreadable = [], []
            for path in consumers:
                try:
                    text = (PROJECT_ROOT / path).read_text(
                        encoding="utf-8", errors="replace")
                except OSError as exc:
                    unreadable.append({"consumer": path, "error": str(exc)})
                    continue
                if not _calls_any_symbol(text, specs):
                    missing.append(path)
            if missing or unreadable:
                return {"seam_present": False,
                        "reason": "consumers_do_not_route_to_any_seam_symbol",
                        "symbols": [n for n, _ in specs],
                        "missing": missing, "unreadable": unreadable}
        return {"seam_present": True}
    except Exception as exc:
        return {"seam_present": False, "reason": f"git_error: {exc}"}


def _head_commit() -> str:
    try:
        out = _git("rev-parse", "--short", "HEAD", timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def cmd_attest(cfg: dict) -> int:
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print(json.dumps({"verdict": "error",
                          "detail": "MIND_AGENT unset — cannot attest"}, indent=2))
        return 3
    report = _local_report(cfg["seam_commit"], cfg["consumers"],
                           cfg.get("seam_symbols"))
    if not report["seam_present"]:
        report.update({"verdict": "refused", "agent": agent,
                       "detail": "this box is not reader-capable — attesting "
                                 "would assert the precondition this gate "
                                 "exists to verify"})
        print(json.dumps(report, indent=2))
        return 2
    payload = {
        "attested_at": datetime.now().replace(microsecond=0).isoformat(),
        "commit": _head_commit(),
        "seam_commit": cfg["seam_commit"][:9],
    }
    rc = subprocess.run(
        bash_cmd(PROJECT_ROOT / "core" / "scripts" / "team-state-update.sh",
                 "--field", f"agent_status.{agent}.{cfg['field']}",
                 "--value", json.dumps(payload)),
        capture_output=True, text=True, timeout=90,
    )
    if rc.returncode != 0:
        print(json.dumps({"verdict": "error", "agent": agent,
                          "detail": f"team-state write failed: "
                                    f"{rc.stderr.strip()[:300]}"}, indent=2))
        return 3
    print(json.dumps({"verdict": "attested", "agent": agent, **payload}, indent=2))
    return 0


def cmd_check(cfg: dict) -> int:
    fetch_ok = _fetch_origin_main()
    ts, err = _read_team_state()
    if err:
        print(json.dumps({
            "verdict": "UNSAFE", "reason": "roster_unreadable", "detail": err,
            "note": "fail-closed: an unreadable roster cannot show that every "
                    "box is reader-capable (rb-245)",
        }, indent=2))
        return 2
    roster = ts.get("agent_status") or {}
    # Carrier refs live on the remote; a box that has never run
    # worker-ref-consume.sh has none locally, and the lane would then find
    # nothing while looking exactly like "this agent has no live Body".
    # Reported like fetch_ok, for the same reason: a failed fetch can only
    # UNDER-prove, which is the UNSAFE direction, but it must be visible.
    workers_fetch_ok = _fetch_worker_refs()
    proofs = {name: derive_proof(name, cfg["seam_commit"], cfg["consumers"],
                                 bodies=row.get("in_flight_bodies"),
                                 seam_symbols=cfg.get("seam_symbols"))
              for name, row in roster.items()
              if isinstance(row, dict) and not row.get("retired_at")}
    local = _local_report(cfg["seam_commit"], cfg["consumers"],
                          cfg.get("seam_symbols"))
    local["hostname"] = socket.gethostname()
    local["head"] = _head_commit()
    result = evaluate_roster(roster, proofs, cfg["field"], datetime.now(),
                             local=local)

    # FLEET-LEVEL SYMBOL VETO. Every proof above is relative to origin/main, so
    # none of them can see main losing the seam call. Checked once, applied to
    # the whole verdict, and fail-closed like every other branch. It overrides
    # a SAFE and never rescues an UNSAFE — a broken main is strictly worse than
    # whatever else is wrong, so it names itself as the reason.
    if cfg.get("seam_symbols"):
        symbols = _symbol_report(cfg["seam_symbols"], cfg["consumers"],
                                 "origin/main")
        result["seam_symbols"] = symbols
        if not symbols["symbol_present"]:
            result["verdict"] = "UNSAFE"
            result["reason"] = "origin_main_does_not_call_the_seam_symbols"

    result.update({
        "flag": cfg.get("flag"),
        "seam_commit": cfg["seam_commit"][:9],
        "fetch_ok": fetch_ok,
        "worker_refs_fetch_ok": workers_fetch_ok,
        "guidance": (
            "SAFE: every live box is reader-capable AND the box you are "
            "standing on (local_box) carries the seam; the writer flag may be "
            "set per box. UNSAFE: do NOT flip the writer — a peer without the "
            "readers sees partial data and reports it as the full window. "
            "reason=local_box_not_reader_capable means the FLEET is fine and "
            "THIS box is behind: pull, then re-run (the roster cannot see it, "
            "because its proofs are agent-keyed and an agent has a Body on "
            "every box). Boxes listed unattested prove themselves by "
            "committing an iteration after pulling the seam (derived), or run: "
            "bash core/scripts/store-cutover-check.sh --store <name> --attest"
        ),
    })
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "SAFE" else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", choices=sorted(STORES),
                    help="named cutover from the registry")
    ap.add_argument("--seam-commit", help="override/ad-hoc seam commit sha")
    ap.add_argument("--consumers", help="override/ad-hoc comma-separated "
                                        "repo-relative consumer files")
    ap.add_argument("--field", help="override/ad-hoc team-state field name "
                                    "(under agent_status.<agent>.)")
    ap.add_argument("--flag", help="writer env-flag name (reporting only)")
    ap.add_argument("--attest", action="store_true",
                    help="verify this box and record the fallback hand-stamp")
    args = ap.parse_args()

    cfg = dict(STORES.get(args.store) or {})
    if args.seam_commit:
        cfg["seam_commit"] = args.seam_commit
    if args.consumers:
        cfg["consumers"] = [c.strip() for c in args.consumers.split(",") if c.strip()]
    if args.field:
        cfg["field"] = args.field
    if args.flag:
        cfg["flag"] = args.flag
    missing = [k for k in ("seam_commit", "consumers", "field") if not cfg.get(k)]
    if missing:
        print(json.dumps({"verdict": "error",
                          "detail": f"missing parameters: {missing} — pass "
                                    f"--store or the explicit flags"}, indent=2))
        return 3
    return cmd_attest(cfg) if args.attest else cmd_check(cfg)


if __name__ == "__main__":
    sys.exit(main())
