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

INDEPENDENCE IS THE GATE'S TO DEFINE. `--closer` is checked through the gate's
own `independence_defect`, imported rather than reimplemented, so "who counts as
independent" has exactly one definition in the tree.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

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


def source_fidelity(source_text: str, artifact_text: str) -> dict:
    """Check 2, mechanised: every entity enumerated in the source, verbatim.

    Returns both directions, because they diagnose different faults and a
    reviewer needs to tell them apart:

      ``missing``  — enumerated in the source, ABSENT from the artifact. The
                     work was not done, or was done under a different identity.
      ``invented`` — present in the artifact, absent from the source. Where
                     ``missing`` and ``invented`` are both non-empty and equal
                     in size, that is the SUBSTITUTION signature of the founding
                     incident, not two unrelated faults.

    ``counts_match`` is reported deliberately: it is the criterion the coach
    goal actually shipped with, and recording that it was GREEN beside a failing
    diff is what shows a future reader why a count-based criterion was not
    enough.
    """
    src = named_entities(source_text)
    art = named_entities(artifact_text)
    missing = sorted(src - art)
    invented = sorted(art - src)
    return {
        "source_entities": sorted(src),
        "artifact_entities": sorted(art),
        "missing": missing,
        "invented": invented,
        "counts_match": len(src) == len(art),
        "substitution_signature": bool(missing) and len(missing) == len(invented),
        "passed": not missing,
    }


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
        out.append(
            f"{FIDELITY_CHECK}: equal counts missing and invented "
            f"({len(fid['missing'])}) is the SUBSTITUTION signature — the artifact "
            f"kept the shape and replaced the identities, which a count-based "
            f"criterion reports as green (counts_match={fid['counts_match']}).")
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
    if str(verdict).upper() == "APPROVE_WITH_NOTES":
        text = (f"[close-review APPROVE_WITH_NOTES by {reviewer}] the close was "
                f"APPROVED; these are non-blocking observations recorded for "
                f"whoever picks this up next:\n{body}")
    else:
        text = (f"[close-review REJECT by {reviewer}] the close is blocked until "
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
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"close-review-verdict: ROUTING FAILED ({exc.__class__.__name__}: {exc}) "
              f"— the REJECT is on disk but the goal record was NOT annotated. "
              f"Append the findings by hand.", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"close-review-verdict: ROUTING FAILED (rc={proc.returncode}) — the "
              f"REJECT is on disk but the goal record was NOT annotated:\n"
              f"{proc.stderr.strip()}", file=sys.stderr)
        return False
    print(f"close-review-verdict: findings routed into {goal_id} progress_note "
          f"({route_marker(findings, verdict)})")
    return True


def write_verdict(goal_id: str, payload: dict) -> Path:
    p = _gate().verdict_path(goal_id)
    if p is None:
        raise SystemExit("close-review-verdict: cannot resolve the verdict path "
                         "(no CLOSE_REVIEW_LEDGER_DIR and no WORLD_DIR)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")
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
    ap.add_argument("--route-to-goal", choices=("world", "agent"), default=None,
                    help="on a WRITTEN REJECT, append the findings to the goal's "
                         "progress_note via goal-field-append.sh, so the rework "
                         "lands in the record instead of only in this artifact")
    ap.add_argument("--write", action="store_true",
                    help="write the artifact; without this the verdict is only reported")
    args = ap.parse_args(argv)

    source = _read(args.source_file, args.source_text, "source")
    artifact = _read(args.artifact_file, args.artifact_text, "artifact")
    fid = source_fidelity(source, artifact)
    # citations-MATCH (). Computed unconditionally beside the id-diff:
    # the two are complements, and the one that catches a reversed claim is the
    # one the id-diff is blind to.
    dir_fid = direction_fidelity(source, artifact)
    mechanical_ok = fid["passed"] and dir_fid["passed"]

    # A verdict is never invented. Refusing here rather than defaulting is what
    # keeps "the reviewer did not say" distinguishable from "the reviewer said no".
    approving = args.approve or args.approve_with_notes
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
