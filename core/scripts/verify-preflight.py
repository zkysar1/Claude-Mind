#!/usr/bin/env python3
"""verify-preflight.py — a goal's mechanical verify checks in ONE call ().

THE COST IT REMOVES. aspirations-verify had the closer run each mechanical check
as its own tool call, most of them followed by a checkpoint write and a diary
line. None of them needs judgement to RUN; the judgement is reading their
verdicts and choosing the status. On a worker Body every tool call is a model
call, and the close phase measured a median of 16 of them (10 closes,
2026-09-27; the per-script counts are in g-375-48's progress note).

WHAT IT RUNS. The canonical scripts, never a re-implementation, so each gate
keeps its own semantics and its own ledger rows:

  checks            verify-check-eval.sh --goal <id> --all
  closure-evidence  closure-evidence-gate.py --goal <id> --source <s> [--summary-file]
  artifact          each --artifact is a file on this box
  positive-state    positive-state-gate.py --claim --evidence    (only with --claim)
  q4                q4-provenance-sample.sh --goal --artifact... --json    (only with --artifact)
  q1                derived: the table passed, every artifact exists, and
                    positive-state did not fail

Every check runs even when an earlier one failed, so one verdict lists every
finding and the closer fixes them all before a single re-run.

STATE WRITES. guard-1867: a step replaced by a script keeps every write the step
made. So this makes the writes the replaced skill steps made, through the same
wrappers: the phase_progress checkpoint keys (loop-state-save.sh update), their
diary lines (execution-diary.sh append), and a verification_gap in the sensory
buffer (wm-append.sh) for a positive-state or Q4 failure. The checkpoint keys are
not only for a resumed verify: iteration-close.sh --phase verify copies them onto
the goal as verify_verdict, which the review of worker closures reads. A failed
write is reported and never changes a verdict. A checkpoint that anchors ANOTHER
goal is left alone, since those keys would land on that goal's record, and
--no-write runs everything with no write at all (a reader, a reviewer, a goal
you are not closing). This writes NO goal status: the
status is the closer's judgement, and its write belongs to
iteration-close.sh --phase verify (guard-2523).

VERDICTS. PASS; FAIL; SKIPPED, which means the check did not apply and is NOT
evidence (a Q4 "skipped" is not a pass); OPEN, for Q1 when the table check did
not apply, so the closer judges Q1; ERROR, which means the check could not run
and is never a pass. Each gate's verdict is read from its output, never from its
exit code alone (the Q4 sampler exits 0 for pass AND for skipped).

rc: 0 = no FAIL and no ERROR, 3 = at least one FAIL, 4 = no FAIL but at least
one ERROR, 2 = usage. Never 1 for a refusal: Python exits 1 on an uncaught
exception, and a refusal must not look like a crash (the closure-evidence gate's
contract, guard-5430).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# No fallbacks on these imports (guard-391): a failed import is a crash, rc 1.
from _paths import PROJECT_ROOT, resolve_file_path  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  guard-580/581: never a bare "bash"

PASS, FAIL, SKIPPED, OPEN, ERROR = "PASS", "FAIL", "SKIPPED", "OPEN", "ERROR"
RC_FAIL, RC_ERROR, RC_USAGE = 3, 4, 2
TIMEOUT_S = 300  # per check; the Q4 sampler is the slowest, at seconds

Runner = Callable[[List[str], Optional[str]], Tuple[Optional[int], str, str]]


def run_script(argv: List[str], stdin_text: Optional[str] = None) -> Tuple[Optional[int], str, str]:
    """(rc, stdout, stderr) of one script, run from the project root as the close
    path runs it. A spawn failure or a timeout is rc None, which reads as ERROR."""
    try:
        p = subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT),
                           timeout=TIMEOUT_S)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", f"{type(e).__name__}: {e}"


def parse_json(out: str):
    """The JSON object a gate printed: its whole stdout (pretty-printed), else its
    last line that parses. None when there is none, which reads as ERROR."""
    text = (out or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def verdict(check: str, state: str, summary: str, findings=None, remedy=None, **data) -> dict:
    return {"check": check, "state": state, "summary": summary,
            "findings": list(findings or []), "remedy": remedy, "data": data}


def unreadable(check: str, rc, out: str, err: str, why: str = "") -> dict:
    """ERROR for output this script cannot read. It prints the SHAPE it got rather
    than guessing at it (guard-2298)."""
    d = parse_json(out)
    shape = (f"keys {sorted(d)[:8]}" if isinstance(d, dict)
             else f"{type(d).__name__}" if d is not None else f"{len(out or '')} bytes, no JSON")
    tail = (err or "").strip().splitlines()[-1:] or [""]
    return verdict(check, ERROR, f"could not read its verdict (rc={rc}, {shape}){why}",
                   findings=[tail[0][:200]] if tail[0] else [],
                   remedy="run the check directly and read its output")


def check_structured(goal_id: str, run: Runner) -> dict:
    rc, out, err = run(bash_cmd(SCRIPT_DIR / "verify-check-eval.sh", "--goal", goal_id, "--all"), None)
    d = parse_json(out)
    if not isinstance(d, dict) or not isinstance(d.get("flags"), list):
        return unreadable("checks", rc, out, err)
    flags = d["flags"]
    total, passed = d.get("checks_total") or 0, d.get("checks_passed") or 0
    strings = len(d.get("string_checks") or [])
    tail = (f"; {strings} string check(s) are yours to judge in Q1-Q3"
            if "has_string_checks" in flags else "")
    if "goal_not_found" in flags:
        return verdict("checks", ERROR, "goal not found in world, agent or archive")
    if "checks_failed" in flags:
        failing = [f"check {i} ({r.get('type')}): {r.get('reason')}"
                   for i, r in enumerate(d.get("results") or [], start=1)
                   if not r.get("passed") and r.get("evaluable", True)]
        return verdict("checks", FAIL, f"{len(failing)} of {total} structured check(s) failed{tail}",
                       failing, "finish the work each failing check names. A check that is itself "
                       "wrong is corrected in the goal's verification.checks, never skipped",
                       passed=passed, total=total)
    if "checks_empty" in flags:
        return verdict("checks", SKIPPED, f"no structured checks[], so Q1-Q4 decide{tail}")
    if "checks_unevaluatable" in flags:
        return verdict("checks", SKIPPED, f"{d.get('unevaluatable_count')} of {total} structured "
                       f"check(s) could not be evaluated (schema, not failure), so Q1-Q4 decide{tail}",
                       passed=passed, total=total)
    if d.get("all_passed") is True:
        return verdict("checks", PASS, f"{passed}/{total} structured check(s) passed{tail}",
                       passed=passed, total=total)
    return unreadable("checks", rc, out, err, why=f", flags {flags}")


def check_closure_evidence(goal_id: str, source: str, summary_file: Optional[str], run: Runner) -> dict:
    argv = [sys.executable, str(SCRIPT_DIR / "closure-evidence-gate.py"), "--goal", goal_id,
            "--source", source]
    if summary_file:
        argv += ["--summary-file", summary_file]
    rc, out, err = run(argv, None)
    d = parse_json(out)
    if not isinstance(d, dict) or "decision" not in d:
        return unreadable("closure-evidence", rc, out, err)
    note = d.get("note_source") or "?"
    warns = [f"warning: {w}" for w in (d.get("warnings") or [])]
    if rc == 3 and d["decision"] == "block":
        return verdict("closure-evidence", FAIL, f"refused. Note checked: {note}",
                       list(d.get("problems") or []) + warns,
                       "write or correct the evidence table in the outcome note: one `OUTCOME <n>: "
                       "MET — <measured value>. Source: <...>` row per outcome, or `NOT MET — <gap>; "
                       "deferred to <live goal>` (goal-schemas.md § Closure Evidence Table)")
    if rc == 0 and d["decision"] == "pass":
        return verdict("closure-evidence", PASS, f"every outcome row is evidenced. Note checked: {note}",
                       warns)
    if rc == 0 and d["decision"] == "noop":
        return verdict("closure-evidence", SKIPPED, f"did not apply: {d.get('reason')}")
    if rc == 0 and d["decision"] == "error":
        return verdict("closure-evidence", ERROR, f"not checked: {d.get('reason')}")
    return unreadable("closure-evidence", rc, out, err, why=f", decision {d['decision']!r}")


def resolve(path: str) -> Path:
    """An absolute path as given; world/ and meta/ to their configured dirs, and
    anything else under the project root (the Mind's one path contract)."""
    p = Path(path)
    return p if p.is_absolute() else resolve_file_path(path)


def check_artifacts(artifacts: List[str]) -> dict:
    if not artifacts:
        return verdict("artifact", SKIPPED, "no --artifact given (Q4 needs one)")
    missing, found = [], []
    for a in artifacts:
        try:
            p = resolve(a)
        except Exception as e:  # an unconfigured world/ or meta/ is a finding, not a crash
            missing.append(f"{a}: {e}")
            continue
        if p.is_file():
            found.append(str(p))
        else:  # a directory is refused too, but "no such file" would send the closer looking for it
            missing.append(f"{a}: {'a directory, not a file' if p.is_dir() else 'no such file'} ({p})")
    if missing:
        return verdict("artifact", FAIL, f"{len(missing)} of {len(artifacts)} artifact(s) missing or not a file",
                       missing, "pass the file this goal produced: an absolute path, or one under "
                       "world/, meta/ or the project root", paths=found)
    return verdict("artifact", PASS, f"{len(found)} file(s) exist: {', '.join(found)}", paths=found)


def check_positive_state(claim: Optional[str], evidence: str, override: Optional[str], run: Runner) -> dict:
    if not claim:
        return verdict("positive-state", SKIPPED, "no --claim given (not evidence)")
    argv = [sys.executable, str(SCRIPT_DIR / "positive-state-gate.py"), "--claim", claim,
            "--evidence", evidence or ""]
    if override:
        argv += ["--override", override]
    rc, out, err = run(argv, None)
    d = parse_json(out)
    if not isinstance(d, dict) or "would_block" not in d:
        return unreadable("positive-state", rc, out, err)
    if rc == 1 and d["would_block"]:
        return verdict("positive-state", FAIL, "the claim names a file your evidence never read",
                       [f"not in the evidence: {p}" for p in d.get("paths_unverified") or []],
                       "Read each named file in this turn and pass that output as --evidence, or drop "
                       "the claim. A false positive: --override-positive-state \"<why>\"",
                       reason=d.get("reason"))
    if rc == 0 and d.get("gate_error"):
        return verdict("positive-state", ERROR, f"not checked: {d['gate_error']}")
    if rc == 0 and not d.get("trigger_matched"):
        return verdict("positive-state", SKIPPED, "the claim binds no file to a state (not evidence)")
    if rc == 0:
        return verdict("positive-state", PASS, d.get("reason") or "verified")
    return unreadable("positive-state", rc, out, err)


def check_q4(goal_id: str, paths: List[str], source_file: Optional[str], run: Runner) -> dict:
    if not paths:
        return verdict("q4", SKIPPED, "no artifact file to sample (not evidence)")
    argv = bash_cmd(SCRIPT_DIR / "q4-provenance-sample.sh", "--goal", goal_id,
                    *[x for p in paths for x in ("--artifact", p)], "--json",
                    *(["--source-file", source_file] if source_file else []))
    rc, out, err = run(argv, None)
    d = parse_json(out)
    v = d.get("verdict") if isinstance(d, dict) else None
    if v not in ("pass", "fail", "skipped") or (rc == 1) != (v == "fail"):
        return unreadable("q4", rc, out, err, why=f", verdict {v!r}")
    counts = f"{d.get('sampled_count')}/{d.get('clusters_total')} cluster(s)"
    extra = [f"unreadable artifact: {m}" for m in d.get("artifacts_missing") or []]
    if v == "fail":
        found = [f"L{f.get('start_line')}-{f.get('end_line')} {f.get('kind')}: {f.get('detail')}"
                 for f in d.get("findings") or []]
        return verdict("q4", FAIL, f"sampled {counts}, {len(found)} finding(s)", found + extra,
                       "for each named claim: cite a source this session fetched, mark it "
                       "[UNVERIFIED -- <why>], or correct it where its source says otherwise. A "
                       "source read with cat or curl is invisible to the manifest: re-read it with "
                       "the Read tool. No override, by design", verdict=v, counts=counts)
    if v == "skipped":
        return verdict("q4", SKIPPED, f"skipped, NOT a pass: {d.get('skip_reason')}", extra,
                       verdict=v, counts=counts)
    return verdict("q4", PASS, f"sampled {counts}, no finding", extra, verdict=v, counts=counts)


def derive_q1(ce: dict, art: dict, ps: dict) -> dict:
    """Q1's mechanical half: the closure table is its evidence, so Q1 stands when
    the table passed and no artifact or file-state claim failed."""
    failed = [c["check"] for c in (ce, art, ps) if c["state"] == FAIL]
    if failed:
        return verdict("q1", FAIL, f"not established: {', '.join(failed)} failed")
    if ce["state"] != PASS:
        return verdict("q1", OPEN, f"the table check {ce['state'].lower()}, so Q1 is yours to judge "
                       "(and to checkpoint)")
    paths = art["data"].get("paths") or []
    return verdict("q1", PASS, "the closure table passed" + (f"; artifact {', '.join(paths)}" if paths else ""),
                   artifact=",".join(paths) or "closure-evidence table")


class Writer:
    """The skill's state writes, through the same wrappers the skill named."""

    def __init__(self, goal_id: str, run: Runner):
        self.goal_id, self.run, self.done, self.failed, self.held = goal_id, run, [], [], []
        # The checkpoint is ONE slot reused across goals, and iteration-close copies
        # its phase_progress onto whichever goal it anchors. So a key written while it
        # anchors another goal lands on THAT goal's record as its verdict. Absent is
        # different: the update still runs, because its missing-checkpoint warning and
        # ledger row are how a skipped anchor is detected (loop-state-save.cmd_update).
        rc, out, _err = run(bash_cmd(SCRIPT_DIR / "loop-state-save.sh", "read"), None)
        d = parse_json(out) if rc == 0 else None
        self.anchor = d.get("goal_id") if isinstance(d, dict) else None

    def _do(self, what: str, argv: List[str], stdin: Optional[str] = None) -> None:
        rc, _out, err = self.run(argv, stdin)
        if rc == 0:
            self.done.append(what)
        else:
            self.failed.append(f"{what}: rc={rc} {(err or '').strip()[-160:]}")

    def checkpoint(self, **kv) -> None:
        what = "checkpoint " + " ".join(f"{k}={v}" for k, v in kv.items())
        if self.anchor not in (None, self.goal_id):
            self.held.append(f"{what}: the checkpoint anchors {self.anchor}, not {self.goal_id}")
            return
        args = [x for k, v in kv.items() for x in ("--set", f"phase_progress.{k}={v}")]
        self._do(what, bash_cmd(SCRIPT_DIR / "loop-state-save.sh", "update", *args))

    def diary(self, entry_type: str, content: str) -> None:
        row = {"entry_type": entry_type, "goal_id": self.goal_id, "content": content}
        self._do(f"diary {content[:40]!r}", bash_cmd(SCRIPT_DIR / "execution-diary.sh", "append"),
                 json.dumps(row, ensure_ascii=False))

    def gap(self, check: str, detail: str) -> None:
        row = {"type": "verification_gap", "source_goal": self.goal_id,
               "observation": f"verify pre-flight {check} FAIL: {detail}"[:600]}
        self._do(f"sensory_buffer {check} gap", bash_cmd(SCRIPT_DIR / "wm-append.sh", "sensory_buffer"),
                 json.dumps(row, ensure_ascii=False))


def record(results: Dict[str, dict], w: Writer) -> None:
    """The writes each replaced step made, keyed exactly as the skill keys them."""
    c = results["checks"]
    if c["data"].get("total"):
        n = f"{c['data']['passed']}/{c['data']['total']}"
        w.checkpoint(standard_checks_passed=n)
        w.diary("finding", f"Verify: {n} standard checks passed")
    q1 = results["q1"]
    if q1["state"] == PASS:
        w.checkpoint(q1_passed="true", q1_artifact=q1["data"]["artifact"])
        w.diary("finding", f"Q1 passed: artifact={q1['data']['artifact']}")
    ps = results["positive-state"]
    if ps["state"] == FAIL:
        w.gap("positive-state", ps["data"].get("reason") or "; ".join(ps["findings"]))
    q4 = results["q4"]
    if q4["data"].get("verdict"):
        v = q4["data"]["verdict"]
        # Both keys: the bool cannot say "skipped", and a reader must never see a
        # skip as a pass (worker-closure-audit.q4_state_of).
        w.checkpoint(q4_passed="true" if v == "pass" else "false", q4_verdict=v)
        w.diary("finding", f"Q4 provenance: {v}, {q4['data']['counts']}")
        if v == "fail":
            for f in q4["findings"]:
                w.gap("q4", f)


def preflight(goal_id: str, source: str, artifacts: List[str], *, source_file=None, claim=None,
              evidence="", override_ps=None, summary_file=None, write=True,
              run: Runner = run_script) -> dict:
    results: Dict[str, dict] = {}
    results["checks"] = check_structured(goal_id, run)
    results["closure-evidence"] = check_closure_evidence(goal_id, source, summary_file, run)
    results["artifact"] = check_artifacts(artifacts)
    results["positive-state"] = check_positive_state(claim, evidence, override_ps, run)
    results["q4"] = check_q4(goal_id, results["artifact"]["data"].get("paths") or [], source_file, run)
    results["q1"] = derive_q1(results["closure-evidence"], results["artifact"], results["positive-state"])
    wrote, failures, held = [], [], ["every write (--no-write)"]
    if write:
        w = Writer(goal_id, run)
        record(results, w)
        wrote, failures, held = w.done, w.failed, w.held
    states = [r["state"] for r in results.values()]
    overall = FAIL if FAIL in states else ERROR if ERROR in states else PASS
    return {"goal_id": goal_id, "source": source, "verdict": overall, "results": results,
            "wrote": wrote, "write_failures": failures, "not_written": held,
            "rc": RC_FAIL if overall == FAIL else RC_ERROR if overall == ERROR else 0}


def render(r: dict) -> str:
    n = sum(len(x["findings"]) for x in r["results"].values() if x["state"] in (FAIL, ERROR))
    head = {PASS: "PASS", FAIL: f"FAIL: fix every finding below, then re-run this once ({n} finding(s))",
            ERROR: "INCOMPLETE: a check could not run, and that is not a pass"}[r["verdict"]]
    lines = [f"VERIFY PRE-FLIGHT {r['goal_id']} ({r['source']}): {head}"]
    for x in r["results"].values():
        lines.append(f"  {x['check']:<17} {x['state']:<8} {x['summary']}")
        lines += [f"      - {f}" for f in x["findings"][:12]]
        if len(x["findings"]) > 12:
            lines.append(f"      ... and {len(x['findings']) - 12} more")
        if x["remedy"] and x["state"] in (FAIL, ERROR):
            lines.append(f"      fix: {x['remedy']}")
    lines.append("  wrote: " + ("; ".join(r["wrote"]) or "nothing"))
    lines += [f"  WRITE FAILED: {f}" for f in r["write_failures"]]
    lines += [f"  not written: {f}" for f in r["not_written"]]
    lines.append("  still yours: Q1.5 checklist, Q2 negative check, Q3 scope"
                 + (", Q1" if r["results"]["q1"]["state"] == OPEN else "")
                 + ", then the status judgement")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal", required=True)
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--artifact", action="append", default=[],
                    help="a file this goal produced (repeatable): Q1 checks it exists, Q4 samples it")
    ap.add_argument("--source-file", default=None, help="Q4: the source the artifact must be faithful to")
    ap.add_argument("--claim", default=None, help="Q1: a file-state claim for the positive-state gate")
    ap.add_argument("--evidence", default="", help="the in-turn Read/ls output that backs --claim")
    ap.add_argument("--override-positive-state", default=None, help="justification for a false positive")
    ap.add_argument("--summary-file", default=None,
                    help="the closure note, when the goal record does not hold one yet")
    ap.add_argument("--no-write", action="store_true",
                    help="run and report only: for a reader, a reviewer, or a goal you are not closing")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return RC_USAGE if e.code else 0
    if args.evidence and not args.claim:
        print("verify-preflight: --evidence backs a --claim; pass both", file=sys.stderr)
        return RC_USAGE
    r = preflight(args.goal, args.source, args.artifact, source_file=args.source_file,
                  claim=args.claim, evidence=args.evidence,
                  override_ps=args.override_positive_state, summary_file=args.summary_file,
                  write=not args.no_write)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else render(r))
    return r["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
