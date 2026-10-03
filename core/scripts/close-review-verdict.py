#!/usr/bin/env python3
"""close-review-verdict — produce the verdict artifact the close-review gate reads.

THE PRODUCER HALF of the blocking close-review gate (g-357-41). The CONSUMER
(`close-review-gate.py`) has existed since g-357-40 and refuses a tier-2 close
without an APPROVE verdict at a world-scoped, goal-keyed path. Nothing wrote
that artifact, so the gate could only ship dormant. This is the writer.

WHY A SCRIPT AND NOT ONLY A SKILL. Of the four mandatory checks in g-357-41,
three are judgment (traceability, criteria adequacy, and the adversarial
mandate) and belong to a reviewing mind. Check 2 — SOURCE FIDELITY, "diff EVERY
enumerated entity in description/source against the artifact verbatim" — is
mechanical, and it is the one the founding incident turned on. Coach g-012-02
enumerated 16 entities, the artifact carried 16 entities, the count-based
criterion went green, and 6 identities had been silently substituted. A count
cannot see that; a set difference sees it instantly. Mechanising exactly the
check that failed is the point, and mechanising only it is equally the point.

THE LABEL NEVER OUTRUNS THE PREDICATE (guard-2564). A mechanical PASS on check 2
is NOT an approval and this script will not write one from it: `--approve` is an
assertion the REVIEWER makes about the judgment checks, and it is REFUSED
outright when the fidelity diff is non-empty. So the two failure directions are
deliberately asymmetric — the machine may VETO an approval on its own evidence,
and may never GRANT one. A verdict this script emits on its own authority is
always a REJECT.

A REVIEWER MAY NARROW THE VETO — PER ID, ON THE RECORD, NEVER UNDER THE FOUNDING
SHAPE (g-375-26). A source that CITES an id it leans on (a prior goal, a
guardrail) is not asking the artifact to carry it, so a verbatim miss there is
not the founding defect. `--citation <id>=<role reason>` attests exactly that,
per id; the attestation is recorded AND stated in the findings, and it is
refused outright when the diff carries the substitution signature. The machine
still grants nothing: an attested id leaves the veto on the reviewer's recorded
word, and the approval remains the reviewer's own assertion.

THE FOUNDING SHAPE IS KEPT KIND BY KIND, NOT ONLY IN TOTAL (g-375-58). Coach
g-012-02 kept every kind's count (4 goal ids, 4 guardrails, 4 rb, 2 asp, 1 sq,
1 sig) and replaced six ids inside those kinds. The signature used to test only
the totals, `len(missing) == len(invented)`, and an outcome note that ADDS as many
evidence ids (shas, dates, carrier goals) as its source CITES meets that by
coincidence. Every firing on the live ledger was such a coincidence, named so by
its reviewer, and because the signature refuses attestation a sound close could
not be approved (g-335-1719; g-335-1601 and g-335-1730 went unreviewed). It now
fires only when missing and invented hold the same number of ids of EVERY kind,
with the reviewed goal's own id left out of invented: most outcome notes name
their goal, and that one id would unbalance the goal kind. Replayed 2026-10-02
over the 163 ledger entries: the totals fired on 4, kind by kind on 0, and the
coach shape still fires. Firing when ANY one kind balances was measured too and
rejected: 39 firings, most of them on sound approvals.

THE RECORD REPRODUCES ITS OWN VERDICT (guard-3743). The artifact carries the
source set, the artifact set, and both directions of the diff — not just the
conclusion. A later reader can recompute REJECT from the record without the
session that wrote it, which is what makes the ledger auditable rather than
merely present.

ONE REGEX, NOT TWO. Entities come from `goal_close_risk_tier.named_entities`,
the same function whose count routes a goal to tier 2. A private regex here
would let the classifier send a goal to review for entities the reviewer could
not see, and nothing would fail when the two drifted.

A REJECT REACHES THE GOAL, NOT ONLY THE LEDGER. `--route-to-goal` appends the
findings to the goal's `progress_note` via `goal-field-append.sh`. Without it
the defects live only in a verdict artifact that nothing reads at claim time, so
the next Body to pick the goal up re-derives them or misses them — "blocks the
close" without "routes the rework" is a stall, not a review.

A FAILED ROUTING IS RETRIED WITHOUT A SECOND VERDICT (g-375-85). Lock contention on
the shared goal store failed 2 of 3 routings in one review cycle, and re-running the
verdict command, as the writer's own error advised, appended a second identical
entry to the ledger: the marker makes the NOTE idempotent, never the verdict. So
routing retries the writer's transient failures with a bounded backoff, and a
failure that survives them names `--route-only`, which routes the reviewer's
recorded verdict and writes nothing.

INDEPENDENCE IS THE GATE'S TO DEFINE. `--closer` is checked through the gate's
own `independence_defect`, imported rather than reimplemented, so "who counts as
independent" has exactly one definition in the tree.

THE RECORD NAMES ITS CLOSER (g-375-55). A written verdict carries the goal's
`completed_by`, `completed_by_role` and `completed_by_sid`, copied from the goal
record at write time. Without them the pass rate per closer role had to be joined
through the goal record, and a goal leaves the live store when its aspiration is
archived or when it is evicted, so its verdict fell out of its role's rate. A goal
not found live at write time leaves the fields absent, loudly: nothing is guessed.
Older verdicts got the fields once, from goal records that still existed, in the
g-375-84 backfill (each entry marked `closer_backfilled_at`).

A REJECT OF A SANCTIONED DEFERRAL IS TOLD SO (g-375-100). guard-7517 lets a goal
close completed with `OUTCOME n: NOT MET — <gap>; deferred to <goal-id>` while that
goal is live. A reviewer rejected exactly that row twice (g-377-50-a on 2026-09-28,
g-335-1719 on 2026-09-30), the second time two days after the guardrail existed,
because nothing at the moment of the verdict said so. A guard-7517 advisory now
names each such row whose carrier is live. It prints on the read-only probe, where
the reviewer decides, and on `--reject` before anything is written: the documented
REJECT passes `--reject` and `--write` in one call, so advice printed only there
would arrive with the verdict. It never refuses: a deferral can still be wrong.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _fileops import locked_modify_json  # noqa: E402
from gates.closure_evidence import DEFERRED_RE, parse_rows  # noqa: E402
from gates.residual_work import ACTIVE_STATUSES  # noqa: E402
from goal_close_risk_tier import named_entities  # noqa: E402
from q4_provenance_sample import (  # noqa: E402
    direction_fidelity, direction_findings)

#: Verdict values this script can write. The RELEASING subset is the gate's to
#: define (`close_review_gate.RELEASING_VERDICTS`), imported rather than copied —
#: an unknown string must never be writable, because it would read as "not
#: APPROVE" downstream and silently behave as REJECT while looking like a third
#: state.
#:
#: APPROVE_WITH_NOTES (added by the  re-review, finding F3) exists
#: because the binary forced a reviewer with non-blocking observations to either
#: REJECT a sound close or APPROVE and drop the observations on the floor. It is
#: writable ONLY through `--approve-with-notes` and ONLY with at least one
#: finding — a "with notes" verdict carrying no notes asserts more than its
#: content supports, which is the same predicate-honesty rule that makes
#: `--approve` refusable on a failed fidelity diff (guard-2564).
#:
#: guard-334 (add an enum value WITH its writer, then sweep): the writer is the
#: flag above and the gate half landed in the same change. Backfill sweep of the
#: live ledger at add time: 1 record total (), a REJECT with 9 findings
#: — genuinely a rejection, not a mislabelled approval. Backfill set: EMPTY,
#: measured, not assumed.
VERDICTS = ("APPROVE", "APPROVE_WITH_NOTES", "REJECT")

#: The check ids this script mechanises. Named so a reader of a findings list can
#: tell a machine-verified failure from a reviewer's prose judgement.
FIDELITY_CHECK = "source-fidelity"
#: citations-MATCH (), the complement of the id-set diff above: the
#: artifact asserts A -> B where the cited source asserts B -> A.
DIRECTION_CHECK = "direction-fidelity"

#: The goal-record fields a written verdict copies, so a reader attributes it to its
#: closer without a join that archival or eviction can break ().
CLOSER_FIELDS = ("completed_by", "completed_by_role", "completed_by_sid")


def _gate():
    """close-review-gate.py, loaded by path (its filename is hyphenated).

    Imported rather than reimplemented so `verdict_path` and
    `independence_defect` keep ONE definition each — the producer writing to a
    path the consumer does not read, or disagreeing about who is independent,
    are the two ways this pair silently stops working.
    """
    cached = sys.modules.get("close_review_gate")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "close_review_gate", SCRIPT_DIR / "close-review-gate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["close_review_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


_HEX_RE = re.compile(r"[0-9a-f]{7,}")
_HEX_LETTER_RE = re.compile(r"[a-f]")


def _sha_related(x: str, y: str) -> bool:
    """One hex token is a strict prefix of the other, LONGER one.

    The longer must carry a hex letter: ``named_entities`` matches pure digit
    runs as shas (guard-5481), so without it a date would "abbreviate" a longer
    number. Only the longer is required to: an all-digit 7-char prefix of a
    real sha is common (~4%) and must still resolve.
    """
    if len(x) == len(y) or not (_HEX_RE.fullmatch(x) and _HEX_RE.fullmatch(y)):
        return False
    short, long_ = (x, y) if len(x) < len(y) else (y, x)
    return long_.startswith(short) and bool(_HEX_LETTER_RE.search(long_))


def _sha_partner(token: str, own_side: set, other_side: set):
    """The token on the other side that ``token`` abbreviates or is abbreviated
    by — searched over the WHOLE other side, not its set-difference, because a
    short sha whose full form is also on this side is still present over there.

    Several abbreviations of one sha are all that sha. A pair holds only when
    its SHORTER token is unambiguous on the LONGER token's side: a prefix of two
    diverging longer shas identifies neither (git refuses an ambiguous
    abbreviation too), from either end of the pair.
    """
    for cand in sorted(other_side, key=lambda o: (-len(o), o)):
        if not _sha_related(token, cand):
            continue
        short, long_side = (token, other_side) if len(token) < len(cand) else (cand, own_side)
        longer = [o for o in long_side if len(o) > len(short) and _sha_related(short, o)]
        widest = max(longer, key=len)
        if all(widest.startswith(o) for o in longer):
            return cand
    return None


def _kind_profile(ids: list) -> dict:
    """How many ids of each KIND a list holds (): the family prefix of a
    prefixed id (``g``, ``guard``, ``rb`` ...), and ``hex`` for a bare sha or
    digit run. Read off the token itself, so a hyphenated family added to the
    regex is already a kind here, with no table to keep in step."""
    out: dict = {}
    for t in ids:
        kind = t.split("-", 1)[0] if "-" in t else "hex"
        out[kind] = out.get(kind, 0) + 1
    return out


def source_fidelity(source_text: str, artifact_text: str,
                    citations: dict | None = None,
                    goal_id: str | None = None) -> dict:
    """Check 2, mechanised: every entity enumerated in the source, verbatim.

    Returns both directions, because they diagnose different faults and a
    reviewer needs to tell them apart:

      ``missing``  — enumerated in the source, ABSENT from the artifact. The
                     work was not done, or was done under a different identity.
      ``invented`` — present in the artifact, absent from the source. Where
                     ``missing`` is non-empty and ``invented`` holds the same
                     number of ids of every kind, that is the SUBSTITUTION
                     signature of the founding incident, not two unrelated
                     faults. Equal totals alone are not (g-375-58). The
                     reviewed goal's own id (``goal_id``) is not invented: an
                     artifact naming its goal is recorded as ``self_reference``.

    ``counts_match`` is reported deliberately: it is the criterion the coach
    goal actually shipped with, and recording that it was GREEN beside a failing
    diff is what shows a future reader why a count-based criterion was not
    enough.

    Two narrowings of ``missing`` (g-375-26), and they are not alike:

      ``sha_identity`` — an abbreviated sha on one side and the full sha it
                     prefixes on the other are ONE identity. Mechanical, so it
                     always applies. It lives here, in the diff, because
                     ``named_entities`` is also the tier classifier's regex and
                     must keep returning both tokens.
      ``citations`` — the REVIEWER's attestation, per id with a one-line role
                     reason, that a missing id is a CITATION the source leans on
                     rather than a TARGET the work had to produce. Attested ids
                     leave the pass computation and are recorded under
                     ``citations_attested``; ``missing`` + those keys is the
                     pre-attestation miss, so the record still reproduces its
                     verdict (guard-3743). A target miss is never attestable.

    Attestation is REFUSED when the identity-resolved diff carries the
    substitution signature: there the "missing" ids are replaced identities, and
    attesting them would launder the founding incident's defect.
    """
    src = named_entities(source_text)
    art = named_entities(artifact_text)
    # (source_token, artifact_token): a reader recomputes `missing` as the
    # source-side tokens of src - art not listed here, less citations_attested,
    # and `invented` as the artifact-side ones, less self_reference.
    sha_pairs, missing, invented = set(), [], []
    # : 115 of the 163 ledger reviews named their own goal in the artifact
    # only. Counted as invented, that id unbalanced the goal kind and hid a goal-id
    # substitution in exactly the closes this check reviews.
    own, self_reference = str(goal_id or "").strip().lower(), None
    for t in sorted(src - art):
        p = _sha_partner(t, src, art)
        if p:
            sha_pairs.add((t, p))
        else:
            missing.append(t)
    for t in sorted(art - src):
        p = _sha_partner(t, art, src)
        if p:
            sha_pairs.add((p, t))
        elif t == own:
            self_reference = t
        else:
            invented.append(t)
    citations = {str(k).strip().lower(): v for k, v in (citations or {}).items()}
    out = {
        "source_entities": sorted(src),
        "artifact_entities": sorted(art),
        "missing": missing,
        "invented": invented,
        "counts_match": len(src) == len(art),
        # : the shape kept kind by kind, which equal totals alone are not.
        "substitution_signature": (bool(missing)
                                   and _kind_profile(missing) == _kind_profile(invented)),
    }
    if self_reference:
        out["self_reference"] = self_reference
    if sha_pairs:
        out["sha_identity"] = [list(p) for p in sorted(sha_pairs)]
    if citations:
        if out["substitution_signature"]:
            out["citations_refused"] = sorted(citations)
        else:
            attested = {i: r for i, r in citations.items() if i in missing}
            unmatched = sorted(set(citations) - set(attested))
            if attested:
                out["citations_attested"] = attested
                out["missing"] = [m for m in missing if m not in attested]
            if unmatched:
                out["citations_unmatched"] = unmatched
    out["passed"] = not out["missing"]
    return out


def fidelity_findings(fid: dict) -> list:
    """Human-readable findings from the diff, quoting the ids verbatim.

    Verbatim ids rather than a count: the whole lesson of the founding incident
    is that a number ("16 of 16") concealed the defect, so a finding that says
    only "6 mismatches" would repeat the mistake it reports.
    """
    out: list = []
    if fid["missing"]:
        out.append(
            f"{FIDELITY_CHECK}: {len(fid['missing'])} entit"
            f"{'y' if len(fid['missing']) == 1 else 'ies'} enumerated in the source "
            f"are ABSENT from the artifact: {', '.join(fid['missing'])}")
    if fid["invented"]:
        out.append(
            f"{FIDELITY_CHECK}: {len(fid['invented'])} entit"
            f"{'y' if len(fid['invented']) == 1 else 'ies'} appear in the artifact "
            f"but not in the source: {', '.join(fid['invented'])}")
    if fid["substitution_signature"]:
        shape = ", ".join(f"{k} {n}" for k, n in sorted(_kind_profile(fid["missing"]).items()))
        out.append(
            f"{FIDELITY_CHECK}: missing and invented hold the same number of ids of "
            f"every kind ({shape}), the SUBSTITUTION signature — the artifact kept "
            f"the shape and replaced the identities, which a count-based criterion "
            f"reports as green (counts_match={fid['counts_match']}).")
    # : an attestation narrows a veto on the reviewer's word, so it is
    # always SAID — never a silent exemption (guard-6989).
    att = fid.get("citations_attested") or {}
    if att:
        out.append(
            f"{FIDELITY_CHECK}: {len(att)} missing entit"
            f"{'y' if len(att) == 1 else 'ies'} ATTESTED by the reviewer as "
            f"citation-only, not a target: "
            + "; ".join(f"{i} ({r})" for i, r in sorted(att.items())))
    if fid.get("citations_refused"):
        out.append(
            f"{FIDELITY_CHECK}: citation attestation REFUSED for "
            f"{', '.join(fid['citations_refused'])} — the diff carries the "
            f"SUBSTITUTION signature, so a missing id may be a replaced identity "
            f"and attesting it would launder the defect this check exists for.")
    if fid.get("citations_unmatched"):
        out.append(
            f"{FIDELITY_CHECK}: --citation named "
            f"{', '.join(fid['citations_unmatched'])}, not missing from the "
            f"artifact — nothing attested for it.")
    return out


def build_verdict(*, goal_id: str, reviewer: str, fidelity: dict,
                  approve: bool, checks: list, findings: list,
                  notes: bool = False, reviewed_at: str | None = None,
                  direction: dict | None = None) -> dict:
    """Resolve the verdict and assemble the artifact.

    The resolution is ONE rule and it is not symmetric: a failed MECHANICAL
    check forces REJECT no matter what the caller asserted, while a passed one
    grants nothing on its own. See the module docstring. `notes` selects the
    third state and is subject to the SAME machine veto as `approve` — it is an
    approval, so a mechanical check may refuse it for the same reason.

    THERE ARE NOW TWO MECHANICAL CHECKS, and they are complements rather than
    overlaps (g-357-44). `fidelity` is citations-EXIST: the id-set difference of
    `named_entities`, which is deliberately narrow (id-shaped tokens only) so
    the tier classifier it shares a regex with does not drag ordinary prose into
    tier 2. `direction` is citations-MATCH: whether a sampled claim asserts
    A -> B where its cited source asserts B -> A. MEASURED on the goal's own
    trade-direction fixture — claim "Miami sent the first-round pick to Denver",
    source "Denver sent ... to Miami" — `named_entities` returns the EMPTY SET
    for BOTH sides and `fidelity["passed"]` is True, so citations-exist passes a
    claim that is exactly backwards. `direction` is what refuses it.

    `direction` is optional and defaults to None so a caller that only has the
    id-diff keeps its existing behaviour; `main()` always supplies it.
    """
    generated = fidelity_findings(fidelity) + direction_findings(direction or {})
    all_findings = generated + [f for f in findings if f]
    if not fidelity["passed"] or (direction is not None and not direction["passed"]):
        verdict = "REJECT"
    elif notes:
        verdict = "APPROVE_WITH_NOTES"
    elif approve:
        verdict = "APPROVE"
    else:
        verdict = "REJECT"
    return {
        "verdict": verdict,
        "reviewer": reviewer,
        "goal_id": goal_id,
        # F2: WHEN the review happened. Absent from every artifact written
        # before this change (measured: 1 of 1 in the live ledger) and NOT
        # backfilled — inventing a timestamp on another reviewer's attestation
        # would be worse than the gap. Readers must therefore treat it as
        # optional; nothing consumes it as a gate today.
        "reviewed_at": reviewed_at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "checks": list(checks) + [
            f"{FIDELITY_CHECK}: every entity enumerated in the source diffed "
            f"verbatim against the artifact (mechanical)"] + ([
            f"{DIRECTION_CHECK}: every directed relation asserted by the artifact "
            f"compared against the same relation in the source (mechanical)"]
            if direction is not None else []),
        "findings": all_findings,
        # guard-3743: the inputs the verdict is reproducible from, not just the
        # conclusion. A reader can recompute `verdict` from `fidelity` AND
        # `direction` together — it was `fidelity` alone until  added the
        # second mechanical check, and a record carrying only one of the two
        # would no longer reproduce its own veto.
        "fidelity": fidelity,
        "direction": direction,
        "produced_by": "close-review-verdict.py",
    }


#: goal-field-append.sh exit codes worth another attempt (): 6, a write that
#: an independent read shows did not land (how a lock timeout on the shared goal
#: store surfaces), and 9, a concurrent modification. A retry is safe because that
#: writer re-reads the field and skips an append whose marker already stands, and it
#: overwrites the field with a value composed from that read (rb-9350). Any other
#: code is a real refusal and is reported at once.
ROUTE_RETRY_RCS = frozenset({6, 9})
#: Seconds to wait before each retry. The shared lock itself waits 10 s before it
#: gives up, so the worst case spends about 90 s here, against a REJECT left unrouted.
ROUTE_BACKOFF_S = (5, 15, 30)


def route_marker(findings: list, verdict: str = "REJECT") -> str:
    """The idempotency marker for a routed REJECT.

    Keyed on a digest of the FINDINGS, not on the goal or the reviewer. That is
    the whole behaviour: re-running the same review appends nothing (the marker
    already stands), while a re-review after rework that finds DIFFERENT defects
    appends a fresh note. A goal-keyed marker would swallow the second review's
    findings — the exact case this routing exists to serve.
    """
    digest = hashlib.sha1("\n".join(findings).encode("utf-8")).hexdigest()[:10]
    kind = "notes" if str(verdict).upper() == "APPROVE_WITH_NOTES" else "reject"
    return f"close-review-{kind}:{digest}"


def route_command(goal_id: str, source: str, reviewer: str, findings: list,
                  verdict: str = "REJECT") -> list:
    """The argv that routes a REJECT's findings into the goal record.

    A scoped CALL to `goal-field-append.sh` — the framework's one goal-field
    append writer, which owns the CAS read-modify-write every hand-rolled
    version of this got wrong. Re-implementing the append here would be a second
    copy that drifts silently when that writer changes.

    bash is resolved to an absolute path rather than passed as a bare argv[0]
    (guard-580).
    """
    body = "\n".join(f"- {f}" for f in findings)
    # The header must not overstate the verdict. An APPROVE_WITH_NOTES released
    # the close; announcing it as "blocked until reworked" would tell the next
    # Body to stop working on a goal that already passed review.
    #
    # The header must not OPEN with '[' or '{' (guard-6075 Cause A). On an empty
    # progress_note the composed value's first byte is this text's, and
    # goal-field-append refuses a JSON opener with rc=5. So a bracketed header
    # failed routing on every goal with no progress_note (, 2026-09-26),
    # and any field it did seed would be born un-appendable for the next writer.
    if str(verdict).upper() == "APPROVE_WITH_NOTES":
        text = (f"close-review APPROVE_WITH_NOTES by {reviewer}: the close was "
                f"APPROVED; these are non-blocking observations recorded for "
                f"whoever picks this up next:\n{body}")
    else:
        text = (f"close-review REJECT by {reviewer}: the close is blocked until "
                f"these are reworked and re-reviewed:\n{body}")
    return [shutil.which("bash") or "/bin/bash",
            str(SCRIPT_DIR / "goal-field-append.sh"),
            "--source", source, goal_id, "progress_note",
            route_marker(findings, verdict), text]


def route_findings(goal_id: str, source: str, reviewer: str, findings: list,
                   verdict: str = "REJECT") -> bool:
    """Execute the routing. Reports LOUDLY on failure and never raises.

    The verdict artifact is already on disk by this point and is the primary
    record, so a routing failure must not turn a written REJECT into a crash.
    It must also never be silent: an unrouted REJECT looks exactly like a goal
    nobody found defects in.
    """
    if not findings:
        # F4 ( re-review): this used to return silently, which is the
        # exact failure the docstring above names — an unrouted verdict looking
        # like a goal nobody found defects in. A non-APPROVE with no findings is
        # itself the anomaly worth saying out loud: the caller asked to route
        # rework and there is none to route.
        print("close-review-verdict: NOTHING ROUTED — --route-to-goal was given "
              "but the verdict carries no findings, so the goal record was not "
              "annotated. A blocking verdict with no findings tells the next "
              "Body nothing; add --finding, or drop --route-to-goal.",
              file=sys.stderr)
        return False
    cmd = route_command(goal_id, source, reviewer, findings, verdict)
    for attempt, wait in enumerate((0, *ROUTE_BACKOFF_S), start=1):
        if wait:
            print(f"close-review-verdict: routing attempt {attempt - 1} failed "
                  f"(rc={proc.returncode}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"close-review-verdict: ROUTING FAILED ({exc.__class__.__name__}: {exc}) "
                  f"— the {verdict} is on disk but the goal record was NOT annotated. "
                  f"{reroute_advice(goal_id, source, reviewer)}", file=sys.stderr)
            return False
        if proc.returncode not in ROUTE_RETRY_RCS:
            break
    if proc.returncode != 0:
        # The writer's own advice ("Re-run the identical command ...") is right for
        # ITS command and wrong for this one, so that one sentence is cut: the only
        # retry this message names must be the one that writes no second verdict.
        # The rest stays, including the writer's warning against a history restore.
        cause = re.sub(r"\s*Re-run the identical command[^.]*\.", "", proc.stderr.strip())
        after = f" after {attempt} attempts" if attempt > 1 else ""
        print(f"close-review-verdict: ROUTING FAILED (rc={proc.returncode}){after} — the "
              f"{verdict} is on disk but the goal record was NOT annotated:\n{cause}\n"
              f"{reroute_advice(goal_id, source, reviewer)}", file=sys.stderr)
        return False
    print(f"close-review-verdict: findings routed into {goal_id} progress_note "
          f"({route_marker(findings, verdict)})")
    return True


def reroute_advice(goal_id: str, source: str, reviewer: str) -> str:
    """The retry for a failed routing that appends no second verdict ()."""
    return (f"Re-route with: close-review-verdict.py --goal {goal_id} --reviewer "
            f"{reviewer} --route-to-goal {source} --route-only. Do NOT re-run the "
            f"verdict command: it appends a second, identical verdict entry.")


def route_only(goal_id: str, reviewer: str, source: str) -> int:
    """Route the findings of this reviewer's CURRENT verdict, writing no verdict.

    The retry for a ROUTING FAILED (g-375-85). It routes exactly the recorded
    findings, so the marker, a digest of them, is the one the failed attempt would
    have written. Only the current verdict, and only this reviewer's: a newer
    verdict by someone else means this review was superseded, and routing it would
    annotate the goal with a review that no longer stands.
    """
    current = _gate().read_verdict(_gate().verdict_path(goal_id))
    if not isinstance(current, dict):
        print(f"close-review-verdict: NOTHING ROUTED — no verdict is recorded for "
              f"{goal_id}.", file=sys.stderr)
        return 1
    by = str(current.get("reviewer") or "").strip()
    if by.lower() != reviewer.strip().lower():
        print(f"close-review-verdict: NOTHING ROUTED — the current verdict for {goal_id} "
              f"is by {by or 'nobody named'}, not {reviewer}, so yours was superseded.",
              file=sys.stderr)
        return 1
    verdict = str(current.get("verdict") or "")
    if verdict == "APPROVE":
        print(f"close-review-verdict: NOTHING ROUTED — the current verdict for {goal_id} "
              f"is a plain APPROVE, which has nothing to route.", file=sys.stderr)
        return 1
    ok = route_findings(goal_id, source, by, list(current.get("findings") or []),
                        verdict=verdict)
    return 0 if ok else 4


#: The carrier id, lettered child included. DEFERRED_RE stops at the digits, but
#: aspirations.py GOAL_ID_RE admits a `-[a-z]` child, and an id fed to a lookup
#: must keep it (guard-2414):  is a different goal from -b.
#: The residual-work gate's own id pattern still drops it ().
_CARRIER_RE = re.compile(r"\bg-\d{1,4}-\d+\b(?:-[a-z]\b)?")


def deferral_advisories(artifact_text: str) -> list:
    """guard-7517 advisories for a review that is not approving (): one per
    sanctioned deferral row, ``OUTCOME n: NOT MET — <gap>; deferred to <goal-id>``,
    whose carrier is live.

    The rows and the "deferred to" pattern are the closure-evidence gate's own, and
    "live" is the residual-work gate's own status set. A carrier the store cannot
    resolve, or one already terminal, gets no advisory: no live goal stands behind
    that deferral any more.
    """
    out = []
    for row in parse_rows(artifact_text)["rows"]:
        text = row["text"].lower()
        m = DEFERRED_RE.search(text) if row["status"] == "NOT MET" else None
        if not m:
            continue
        # The first id after "deferred to" is the one DEFERRED_RE ended on.
        carrier = _CARRIER_RE.search(text, m.start()).group(0)
        status = _gate().load_goal(carrier, "world").get("status")
        if status in ACTIVE_STATUSES:
            out.append(
                f"close-review-verdict: ADVISORY (guard-7517) — OUTCOME {row['n']} is NOT "
                f"MET and deferred to {carrier}, which is live ({status}). A completed close "
                f"may leave an outcome to a live carrier (goal-schemas.md § Closure Evidence "
                f"Table), so this row is not by itself a defect. Reject on it only for a "
                f"reason beyond the deferral, such as a carrier that does not cover the gap, "
                f"and name that reason in a --finding.")
    return out


def closer_of(goal_id: str) -> tuple[dict, str | None]:
    """The CLOSER_FIELDS of the goal record, read live through the gate's `load_goal`
    (one definition of the lookup), and the record's status; ({}, None) when no live
    record is found. The status is what tells a missing record from a live goal whose
    record names no closer yet, such as an open goal under review (g-375-117)."""
    goal = _gate().load_goal(goal_id, "world")
    if not goal:
        return {}, None
    return ({k: goal[k] for k in CLOSER_FIELDS if goal.get(k)},
            str(goal.get("status") or "unknown"))


def write_verdict(goal_id: str, payload: dict) -> Path:
    """APPEND this verdict to the goal's ledger. NEVER replace a prior one.

    The ledger is an AUDIT TRAIL, not a last-writer cache. This function used to
    be a raw ``Path.write_text`` over a one-object-per-goal key -- the exact call
    guard-996 names -- with no version check, no merge and no warning, so a
    SECOND reviewer silently erased the first (finding F11, g-357-41).

    That matters more than an ordinary lost update because REJECT -> rework ->
    re-review is the NORMAL path this gate is built around: the trail destroyed
    its own history by construction. It also corrupted the measurement that
    decides whether the gate ships -- the override RATE (rb-4452) is computed off
    this store, and keeping only the LAST verdict per goal under-counts exactly
    the goals that needed the most review. Measured 2026-09-03: a 9-finding
    REJECT survived only because it had been hand-archived first, which is not a
    property of the system.

    ``world/audit-reports/close-reviews/*.json`` is a **class (b) fence-only**
    store (``coordination_merge.merge_handler_for`` -> None, checked per path,
    never by grep) and it DOES reach S3, so a stale fence wedges it permanently
    rather than degrading. That is why the write goes through
    ``locked_modify_json``: locked read-modify-write with an in-cycle force-fresh
    read, per ``core/config/conventions/governed-store-write-classes.md``. It
    also buys the history snapshot + changelog row the raw write skipped.
    """
    p = _gate().verdict_path(goal_id)
    if p is None:
        raise SystemExit("close-review-verdict: cannot resolve the verdict path "
                         "(no CLOSE_REVIEW_LEDGER_DIR and no WORLD_DIR)")

    def _append(current):
        # Legacy shape: a bare verdict object, written before this ledger became
        # append-only. PRESERVE it as entry 0 -- migrating must not destroy the
        # prior reviewer's record, which is the very defect being fixed.
        if isinstance(current, dict):
            current = [current] if current else []
        elif not isinstance(current, list):
            current = []
        current.append(payload)
        return current

    locked_modify_json(p, _append, initial=[])
    return p


def _read(path: str | None, inline: str | None, what: str) -> str:
    if inline is not None:
        return inline
    if not path:
        raise SystemExit(f"close-review-verdict: --{what}-file or --{what}-text required")
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SystemExit(f"close-review-verdict: cannot read --{what}-file {path}: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Produce the close-review verdict artifact the gate reads.")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--reviewer", required=True,
                    help="the REVIEWING identity; must differ from the closer")
    ap.add_argument("--closer", default=None,
                    help="the closing agent; when given, self-review is refused here "
                         "rather than at close time")
    ap.add_argument("--source-file", default=None,
                    help="the goal description / source text the artifact must be faithful to")
    ap.add_argument("--source-text", default=None)
    ap.add_argument("--artifact-file", default=None)
    ap.add_argument("--artifact-text", default=None)
    ap.add_argument("--approve", action="store_true",
                    help="assert the JUDGMENT checks passed. Refused when the "
                         "mechanical fidelity diff is non-empty.")
    ap.add_argument("--approve-with-notes", action="store_true",
                    help="approve the close AND record non-blocking observations. "
                         "Releases the close like --approve; requires at least one "
                         "--finding, and is refused on a failed fidelity diff for "
                         "the same reason --approve is.")
    ap.add_argument("--reject", action="store_true",
                    help="record a REJECT (with any --finding you supply)")
    ap.add_argument("--check", action="append", default=[],
                    help="a check you performed, recorded verbatim (repeatable)")
    ap.add_argument("--finding", action="append", default=[],
                    help="an additional finding (repeatable)")
    ap.add_argument("--citation", action="append", default=[], metavar="ID=REASON",
                    help="attest that a MISSING id is a citation the source leans "
                         "on, not a target the artifact had to carry, with a "
                         "one-line role reason (repeatable, one id each). Refused "
                         "when the diff carries the substitution signature; a "
                         "target miss is a REJECT, never an attestation.")
    ap.add_argument("--route-to-goal", choices=("world", "agent"), default=None,
                    help="on a WRITTEN REJECT, append the findings to the goal's "
                         "progress_note via goal-field-append.sh, so the rework "
                         "lands in the record instead of only in this artifact")
    ap.add_argument("--route-only", action="store_true",
                    help="after a ROUTING FAILED: route the findings of your current "
                         "recorded verdict and write NO verdict entry. Needs "
                         "--route-to-goal; takes no verdict flag, --finding or --write")
    ap.add_argument("--write", action="store_true",
                    help="write the artifact; without this the verdict is only reported")
    args = ap.parse_args(argv)
    citations = {}
    for raw in args.citation:
        cid, sep, reason = raw.partition("=")
        if not (sep and cid.strip()) or len(reason.strip().splitlines()) != 1:
            ap.error(f"--citation {raw!r}: expected <id>=<one-line role reason>")
        citations[cid.strip()] = reason.strip()

    if args.route_only:
        if not args.route_to_goal:
            ap.error("--route-only needs --route-to-goal <world|agent>")
        if args.approve or args.approve_with_notes or args.reject or args.write \
                or args.finding:
            ap.error("--route-only routes the verdict already recorded; it takes no "
                     "verdict flag, --finding or --write")
        return route_only(args.goal, args.reviewer, args.route_to_goal)

    source = _read(args.source_file, args.source_text, "source")
    artifact = _read(args.artifact_file, args.artifact_text, "artifact")
    fid = source_fidelity(source, artifact, citations, goal_id=args.goal)
    # citations-MATCH (). Computed unconditionally beside the id-diff:
    # the two are complements, and the one that catches a reversed claim is the
    # one the id-diff is blind to.
    dir_fid = direction_fidelity(source, artifact)
    mechanical_ok = fid["passed"] and dir_fid["passed"]
    approving = args.approve or args.approve_with_notes

    # : on the read-only probe, where the reviewer decides, and on a REJECT
    # before it is written. Advice, never a refusal; an approval needs none.
    if not approving:
        for line in deferral_advisories(artifact):
            print(line, file=sys.stderr)

    # A verdict is never invented. Refusing here rather than defaulting is what
    # keeps "the reviewer did not say" distinguishable from "the reviewer said no".
    if not (approving or args.reject):
        print(json.dumps({"fidelity": fid, "direction": dir_fid, "verdict": None},
                         indent=2, sort_keys=True))
        print("\nclose-review-verdict: no verdict recorded — pass --approve or --reject.\n"
              f"  mechanical {FIDELITY_CHECK}: "
              f"{'PASS' if fid['passed'] else 'FAIL'}\n"
              f"  mechanical {DIRECTION_CHECK}: "
              f"{'PASS' if dir_fid['passed'] else 'FAIL'}"
              f"{'' if mechanical_ok else ' (an APPROVE would be refused)'}",
              file=sys.stderr)
        return 2

    if approving and not mechanical_ok:
        for line in fidelity_findings(fid) + direction_findings(dir_fid):
            print(f"  {line}", file=sys.stderr)
        _label = "APPROVE_WITH_NOTES" if args.approve_with_notes else "APPROVE"
        _failed = ", ".join(
            name for name, ok in ((FIDELITY_CHECK, fid["passed"]),
                                  (DIRECTION_CHECK, dir_fid["passed"])) if not ok)
        print(f"close-review-verdict: REFUSING to write {_label} — the mechanical "
              f"{_failed} check failed. The label may not assert more than "
              f"the predicate supports (guard-2564). Re-run with --reject, or fix "
              f"the artifact.", file=sys.stderr)
        return 1

    if args.approve_with_notes and not [f for f in args.finding if f]:
        print("close-review-verdict: REFUSING to write APPROVE_WITH_NOTES with no "
              "notes — the label would assert an observation the record does not "
              "carry (guard-2564). Pass --finding, or use --approve.",
              file=sys.stderr)
        return 1

    payload = build_verdict(goal_id=args.goal, reviewer=args.reviewer, fidelity=fid,
                            approve=args.approve, checks=args.check,
                            findings=args.finding, notes=args.approve_with_notes,
                            direction=dir_fid)

    # F5 ( re-review): the independence guard is scoped to the RESOLVED
    # verdict, and only to the verdicts that RELEASE a close. It used to run
    # whenever --closer was given, so a reviewer recording a REJECT on their own
    # close was refused — and the code already knew better: it probed
    # independence_defect with a hardcoded {"verdict": "APPROVE"} payload while
    # the gate itself applies the same function only `if approved`
    # (close-review-gate.py, the `defect = ... if approved else None` line), and
    # the function's own docstring opens "Why this APPROVE verdict is not an
    # INDEPENDENT review". Self-REJECT is not a self-approval: finding fault in
    # your own work is the one direction that needs no independence, and
    # refusing it suppressed the record rather than the conflict.
    #
    # The real payload is passed now instead of the probe, so the scope
    # question is asked of the verdict that will actually be written.
    if args.closer and _gate().releases_close(payload["verdict"]):
        defect = _gate().independence_defect(payload, args.closer)
        if defect:
            print(f"close-review-verdict: REFUSING to write — reviewer "
                  f"{args.reviewer!r} vs closer {args.closer!r} is '{defect}'. "
                  f"The close-review gate would refuse this artifact; producing it "
                  f"anyway would only add a doomed record to the ledger.",
                  file=sys.stderr)
            return 1

    if args.write:
        closer, status = closer_of(args.goal)
        if status is None:
            print(f"close-review-verdict: no live goal record for {args.goal} at write "
                  f"time, so the verdict does not name its closer (stats resolves it "
                  f"through the archive)", file=sys.stderr)
        elif not closer and status in ("pending", "candidate", "in-progress", "blocked"):
            # A review request () is often reviewed before its goal closes, so
            # the record names no closer yet. That is expected, not a missing record.
            print(f"close-review-verdict: {args.goal} is still open ({status}), so the "
                  f"verdict names no closer; the goal names one when it closes, and stats "
                  f"reads it from there", file=sys.stderr)
        elif not closer:
            print(f"close-review-verdict: the record of {args.goal} ({status}) names no "
                  f"closer, so neither does the verdict", file=sys.stderr)
        payload.update(closer)
        p = write_verdict(args.goal, payload)
        print(f"close-review-verdict: {payload['verdict']} written -> {p}")
        # REJECT only, and only once the artifact exists. An APPROVE has nothing
        # to rework, and a dry run must leave no trace anywhere.
        # A plain APPROVE has nothing to say and a dry run must leave no trace.
        # APPROVE_WITH_NOTES routes for the same reason a REJECT does: notes that
        # reach only the ledger reach nobody (F3).
        if args.route_to_goal and payload["verdict"] != "APPROVE":
            route_findings(args.goal, args.route_to_goal, args.reviewer,
                           payload["findings"], verdict=payload["verdict"])
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
        print("\nclose-review-verdict: DRY RUN — pass --write to record it.",
              file=sys.stderr)
    return 0 if _gate().releases_close(payload["verdict"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
