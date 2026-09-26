#!/usr/bin/env python3
"""stop-handoff-check — graceful-stop D7 refuses the mode flip until THIS stop
has written the continuation handoff (g-373-128).

WHY. Measured on a live vessel run (g-373-94; instance terminated after the
run): the FAST consolidation path's only writer of
agents/<agent>/session/handoff.yaml is "Step 9: Continuation Handoff" in
core/config/consolidation-housekeeping.md. The mind Read that digest with a
200-line limit, and Step 9 sits past line 200, so it was never read and never
run. D7 then flipped agent-mode and printed a fixed "... and handoff saved"
line anyway. 3/3 vessel graceful endings had no handoff.yaml.
session-summary.yaml DID land every time, because D6.5 writes it by SCRIPT.
That is the rule this check applies (guard-399): a step a model may skip is
guarded by a script on the path it cannot skip. D7 is that path — the stop is
not finished until the mode flips, and the vessel sidecar ends the run seconds
after it does. So this check runs FIRST in D7's command, chained with `&&`
ahead of session-mode-set.sh.

WHAT "THIS STOP" MEANS. handoff.yaml must be newer than the start of the
current stop. Two markers, and when both exist the LATER one is the reference:
  1. stop-checkpoint.json `stop_started_at` — the GS-0 stamp. The field, not
     the file mtime: GS-0 re-writes the checkpoint on --resume but preserves
     `stop_started_at`, so a handoff written before an interruption still
     counts after the resume.
  2. stop-target-mode's mtime — every stop requester writes it before it sets
     stop-requested, and D7 deletes it only AFTER this check. Needed because
     the measured vessel run skipped GS-0's checkpoint write entirely.
  3. neither exists -> "unverifiable": refused, with a remedy that can be
     satisfied (re-stamp, write, re-run). Passing it would let a handoff left
     by an EARLIER session back the "handoff saved" claim.
Why the later one (g-373-141): each marker is a lower bound on when this stop
began, but write_checkpoint also preserves `stop_started_at` from a checkpoint
that an EARLIER stop left behind without signing off (it never reached the
clear: D7.1 before g-373-140, D7's mode-flip call since). Trusting the checkpoint
first then passed a 50-minute-old handoff from the previous stop. The fresh
stop-target-mode is newer and wins. On a genuine --resume, stop-target-mode
was written before GS-0, so the checkpoint stamp still wins.

TWO CALL SITES. D4.1 runs this check with `--step D4.1`, right after D4
writes the handoff. D7.0 runs it again as the gate on the mode flip. D4.1 is
there because a handoff first written at a D7.0 refusal comes after D6.62's
commit and D6.7's own-cloud flush. The periodic sweep skips an agent dir once
its claim is no longer RUNNING, so on own-cloud that handoff stayed local-only
(g-373-141). `--step` changes only the refusal wording and the telemetry
caller. The verdict logic is the same at both steps.
The handoff is dated by its file mtime, never by its embedded `timestamp`:
the model writes that field by hand on the fast path, and a wrong clock in it
would refuse a real handoff. An mtime cannot be typed.

A handoff that is empty or not a YAML mapping does not count: `touch` is not a
handoff.

EXITS. 0 = pass (a fresh handoff exists), or override, or fail-open (the
agent could not be resolved, or the check itself crashed — the mode flip is
chained behind this rc, and a plumbing fault must not strand a stop).
1 = refused. On refusal the Step 9 section is printed VERBATIM, extracted by
header range from the digest, so the instructions arrive with the refusal.
`--proceed-without-handoff "<why>"` is the escape hatch: it prints the reason,
logs it to gate telemetry (decision=override) and exits 0. Every decision is
logged under gate id `stop-handoff-check` (core/config/gates.yaml).

Only the pass line says "Handoff saved". D7's fixed text no longer claims it,
so the claim can only appear when this check has passed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

GATE_ID = "stop-handoff-check"
HANDOFF_NAME = "handoff.yaml"
CHECKPOINT_NAME = "stop-checkpoint.json"
TARGET_MODE_NAME = "stop-target-mode"
DIGEST = SCRIPT_DIR.parent / "config" / "consolidation-housekeeping.md"
STEP9_HEADER = "## Step 9:"


def _iso(epoch: float) -> str:
    # Local naive wall time: the same clock stop_checkpoint._now_iso() writes.
    return dt.datetime.fromtimestamp(epoch).replace(microsecond=0).isoformat()


def resolve_reference(session_dir: Path) -> Optional[Dict[str, Any]]:
    """When did THIS stop begin? The later of the two markers (see the module
    docstring); None when nothing marks it."""
    marks = []
    cp = session_dir / CHECKPOINT_NAME
    try:
        started = json.loads(cp.read_text(encoding="utf-8")).get("stop_started_at")
        if started:
            marks.append(("stop-checkpoint",
                          dt.datetime.fromisoformat(str(started)).timestamp()))
    except (OSError, ValueError, AttributeError, TypeError):
        pass  # absent or unreadable checkpoint -> the other marker alone
    try:
        marks.append(("stop-target-mode", (session_dir / TARGET_MODE_NAME).stat().st_mtime))
    except OSError:
        pass
    if not marks:
        return None
    kind, epoch = max(marks, key=lambda m: m[1])
    return {"kind": kind, "epoch": epoch, "iso": _iso(epoch)}


def inspect_handoff(session_dir: Path) -> Optional[Dict[str, Any]]:
    """None when absent; else its mtime and whether it is a non-empty mapping."""
    path = session_dir / HANDOFF_NAME
    try:
        mtime = path.stat().st_mtime
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Not UTF-8 text, so not a YAML handoff. Left uncaught it reached
        # main()'s crash handler, which fails OPEN ().
        return {"mtime": mtime, "iso": _iso(mtime), "valid": False}
    except OSError:
        return None
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(text)
        valid = isinstance(doc, dict) and bool(doc)
    except ImportError:
        valid = bool(text.strip())  # no parser: non-empty is the best available
    except Exception:
        valid = False
    return {"mtime": mtime, "iso": _iso(mtime), "valid": valid}


def decide(handoff: Optional[Dict[str, Any]], ref: Optional[Dict[str, Any]],
           override: Optional[str]) -> Dict[str, Any]:
    """Pure verdict. Returns {verdict, decision, rc}."""
    if handoff is None:
        verdict = "absent"
    elif not handoff["valid"]:
        verdict = "invalid"
    elif ref is None:
        verdict = "unverifiable"
    elif handoff["mtime"] < ref["epoch"]:
        verdict = "stale"
    else:
        verdict = "fresh"
    if verdict == "fresh":
        return {"verdict": verdict, "decision": "pass", "rc": 0}
    if override:
        return {"verdict": verdict, "decision": "override", "rc": 0}
    return {"verdict": verdict, "decision": "block", "rc": 1}


def extract_step9(digest: Path) -> Optional[str]:
    """The Step 9 section, header to the next `## ` header outside a fence."""
    try:
        lines = digest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out, inside, fenced = [], False, False
    for line in lines:
        if inside and not fenced and line.startswith("## "):
            break
        if line.startswith(STEP9_HEADER):
            inside = True
        if inside:
            out.append(line)
            if line.lstrip().startswith("```"):
                fenced = not fenced
    return "\n".join(out).rstrip() if out else None


def _detail(verdict: str, handoff_rel: str, handoff, ref) -> str:
    if verdict == "absent":
        return f"{handoff_rel} does not exist"
    if verdict == "invalid":
        return f"{handoff_rel} is empty or not a YAML mapping"
    if verdict == "stale":
        return (f"{handoff_rel} was last written {handoff['iso']}, BEFORE this stop "
                f"began at {ref['iso']} ({ref['kind']}) — it is an earlier session's")
    return (f"nothing marks when this stop began (no {CHECKPOINT_NAME}, no "
            f"{TARGET_MODE_NAME}), so {handoff_rel} ({handoff['iso']}) cannot be dated")


def _refusal(verdict: str, detail: str, agent: str, digest: Path,
             step: str = "D7") -> str:
    steps = []
    if verdict == "unverifiable":
        steps.append(f"Re-stamp the stop start: MIND_AGENT={agent} bash core/scripts/"
                     "stop-checkpoint.sh write --target-mode <the target_mode cached at GS-0>")
    steps.append("Write the continuation handoff exactly as the section below says.")
    steps.append(f"Re-run the {step} command unchanged.")
    if step == "D7":
        steps.append("Once that re-run passes, run D7.05 and D7.1 — they finish the stop.")
    numbered = "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1))
    step9 = extract_step9(digest)
    body = step9 if step9 else (f"(Step 9 was not found in {digest} — open it and "
                                "follow its Continuation Handoff step.)")
    if step != "D7":
        # Pre-flush site: nothing is refused yet, so no D7/D7.1 wording here.
        return (
            f"⛔ NO HANDOFF YET — none was written during this stop: {detail}.\n"
            f"Write it now, at {step}, so D6.62's commit and D6.7's flush carry it.\n\n"
            f"Do this now:\n{numbered}\n"
            "Then continue the stop. If a handoff genuinely cannot be written, continue "
            "anyway: D7 checks again and carries the --proceed-without-handoff escape hatch.\n\n"
            f"──── Step 9, verbatim from {digest.name} ────\n{body}\n────"
        )
    return (
        f"⛔ STOP NOT FINISHED — no handoff was written during this stop: {detail}.\n"
        "D7 refused to set the post-stop mode.\n\n"
        f"Do this now:\n{numbered}\n"
        "If a handoff genuinely cannot be written, re-run D7 with "
        "--proceed-without-handoff \"<why>\" added right after stop-handoff-check.sh.\n"
        "That records the reason and lets the stop finish without a handoff.\n\n"
        f"──── Step 9, verbatim from {digest.name} ────\n{body}\n────"
    )


def _log(decision: str, verdict: str, payload: Dict[str, Any],
         override: Optional[str], step: str = "D7") -> None:
    try:
        import _gate_log  # type: ignore
        _gate_log.log(GATE_ID, decision, caller=f"aspirations-graceful-stop {step}",
                      trigger_matched=verdict, payload=payload,
                      override_reason=override if decision == "override" else None)
    except Exception:
        pass  # telemetry must never change the verdict


def main(argv=None) -> int:
    """A crash here must not strand the stop: the mode flip is chained behind
    this rc (guard-409), so any unexpected error fails OPEN — loudly, and
    without claiming a handoff. A bad command line still exits 2 (argparse)."""
    try:
        return _main(argv)
    except Exception as e:  # noqa: BLE001 — every internal fault fails open
        print(f"Handoff NOT VERIFIED — stop-handoff-check crashed "
              f"({type(e).__name__}: {e}); failing open.")
        _log("fail_open", "crash", {"error": f"{type(e).__name__}: {e}"}, None)
        return 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", default=None)
    ap.add_argument("--session-dir", default=None,
                    help="the agent's session dir (default: resolved from the agent)")
    ap.add_argument("--digest", default=str(DIGEST))
    ap.add_argument("--proceed-without-handoff", dest="override", default=None,
                    metavar="WHY")
    ap.add_argument("--step", choices=("D4.1", "D7"), default="D7",
                    help="the graceful-stop step running the check (wording + telemetry)")
    args = ap.parse_args(argv)
    override = (args.override or "").strip() or None

    agent = (args.agent or os.environ.get("MIND_AGENT")
             or os.environ.get("MIND_AGENT") or "").strip()
    if args.session_dir:
        session_dir = Path(args.session_dir)
    elif agent:
        os.environ["MIND_AGENT"] = agent  # before _paths resolves anything
        from _paths import agent_state_dir  # type: ignore
        session_dir = Path(agent_state_dir(agent))
    else:
        print("Handoff NOT VERIFIED — stop-handoff-check could not resolve the agent "
              "(no --agent, MIND_AGENT or MIND_AGENT); failing open.")
        _log("fail_open", "no-agent", {}, None, args.step)
        return 0

    handoff = inspect_handoff(session_dir)
    ref = resolve_reference(session_dir)
    result = decide(handoff, ref, override)
    verdict = result["verdict"]
    handoff_path = session_dir / HANDOFF_NAME
    try:
        handoff_rel = str(handoff_path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        handoff_rel = str(handoff_path)
    payload = {"agent": agent or None, "handoff": handoff, "reference": ref}

    if result["decision"] == "pass":
        print(f"Handoff saved — {handoff_rel} written {handoff['iso']}, after this "
              f"stop began at {ref['iso']} ({ref['kind']}).")
    else:
        detail = _detail(verdict, handoff_rel, handoff, ref)
        if result["decision"] == "override":
            print(f"Handoff NOT saved — {detail}. Proceeding without it: {override} "
                  f"(logged as gate {GATE_ID} decision=override).")
        else:
            print(_refusal(verdict, detail, agent or "<agent>", Path(args.digest),
                           args.step))
    _log(result["decision"], verdict, payload, override, args.step)
    return result["rc"]


if __name__ == "__main__":
    sys.exit(main())
