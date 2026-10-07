#!/usr/bin/env python3
"""Independent-signal liveness gate for `worker-ref-consume.sh --retire` ( unit (d), guard-3660).

A carrier ref whose tip is on origin/main has had its CONTENT delivered. Whether the HANDLE can
go depends on whether a running Body still pushes to it, and the in_flight_bodies row cannot say:
it is written per CLAIM, so every live Body has none between two units. This module is the second,
independently written opinion, asked only when that row is absent. RETIRE needs ALL of:

  1. origin's tip of the ref is the local copy (a ref that moved is a Body pushing, and a stale
     local copy would make the next two reads describe a ref that no longer exists)
  2. the tip commit is at least DEFAULT_REAP_STALE_MINUTES old (that is the age of the Body's last
     PUSH, not of the Body: a Body whose units edit no framework file never pushes)
  3. body_row_reaper's own decision for a row-less Body with that heartbeat carrier is REAP

Anything else is CARRY, and so is every read that failed: keeping a ref costs one more sweep,
deleting a live Body's push target is a handle lost. The threshold and the dead-Body opinion are
body_row_reaper's, imported and never restated, so the retire decision and the reaper hold ONE
opinion about one Body. The carrier is read through stranded-claim-sweep._body_carrier_verdict,
which reads the store of record (a local ls of the mirror can lag).
Rationale (WHY these signals and this fail direction): core/config/rationale/worker-ref-retire-gate.md

CLI (used by worker-ref-consume.sh --retire and, with --no-remote, by the --drain plan):
    check --repo R --ref refs/workers/<agent>/<sid> --agent A --sid S [--no-remote] [--json]
One line on stdout: `RETIRE: <why>` or `CARRY <code>: <why>`. Exit 0 only for RETIRE; every other
outcome, a crash included, is non-zero, and the caller must treat non-zero as a refusal.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REF_RE = re.compile(r"^refs/workers/([^/\s]+)/([^/\s]+)$")
# TEST-ONLY seam, same contract as WORKER_REF_TEAM_STATE_READER: a python script run as
# `<script> <agent> <sid>` that prints one JSON object {"verdict": ..., "evidence": {...}}.
# Production NEVER sets the env; a test pins that the default reads the real sweep function.
CARRIER_READER_ENV = "WORKER_REF_CARRIER_READER"
GIT_TIMEOUT_S = 60
CARRIER_TIMEOUT_S = 120


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=GIT_TIMEOUT_S)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def local_tip(repo, ref):
    rc, out = _git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}")
    return out if rc == 0 and OID_RE.match(out) else None


def remote_tip(repo, ref):
    """origin's current tip of `ref`; None when the read failed OR origin no longer has the ref."""
    rc, out = _git(repo, "ls-remote", "origin", ref)
    if rc != 0:
        return None
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref and OID_RE.match(sha):
            return sha
    return None


def tip_age_minutes(repo, tip, now):
    rc, out = _git(repo, "log", "-1", "--format=%ct", tip)
    return (now - int(out)) / 60.0 if rc == 0 and out.isdigit() else None


def _load_sweep():
    spec = importlib.util.spec_from_file_location(
        "stranded_claim_sweep_retire_gate", os.path.join(HERE, "stranded-claim-sweep.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_carrier(agent, sid, stale_minutes):
    """-> (verdict token, evidence dict) from the shared carrier verdict, or the test seam."""
    seam = os.environ.get(CARRIER_READER_ENV, "").strip()
    if seam:
        p = subprocess.run([sys.executable, seam, agent, sid], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=CARRIER_TIMEOUT_S)
        if p.returncode != 0:
            raise RuntimeError("carrier reader seam exited %d" % p.returncode)
        doc = json.loads(p.stdout.decode("utf-8", "replace"))
        return str(doc["verdict"]), dict(doc.get("evidence") or {})
    verdict, evidence = _load_sweep()._body_carrier_verdict(agent, sid, int(stale_minutes))
    return verdict, evidence


def reaper_calls_dead(sid, verdict, evidence):
    """True only when body_row_reaper would REAP a row-less Body that has this carrier.

    No claims census is consulted, and none is needed: a Body holding a live claim has its
    in_flight_bodies row (written at claim time, reaped only without a live claim), and a
    PRESENT row is refused by --retire before this gate is asked."""
    import body_row_reaper as reaper
    out = reaper.decide_row(sid=sid, row={}, carrier_verdict=verdict, carrier_evidence=evidence,
                            holds_live_claim=False)
    return out["verdict"] == reaper.R_REAP


def _result(verdict, code, why, sig):
    return {"verdict": verdict, "code": code, "why": why, "signals": sig}


def _carry(code, why, sig):
    return _result("CARRY", code, why, sig)


def decide(sig, stale_minutes):
    """Pure decision over measured signals; reads nothing.

    `sig` carries local_tip, remote_tip (when check_remote), tip_age_min and, once read,
    `carrier` = {verdict, dead, age_min, why}. A carrier not read yet returns code
    `need-carrier`, so the caller pays the store read only when the cheap signals did not
    already say CARRY."""
    if sig.get("check_remote", True):
        rt, lt = sig.get("remote_tip"), sig.get("local_tip")
        if not rt:
            return _carry("remote-unreadable", "origin's tip of this ref could not be read, or origin "
                          "no longer has the ref", sig)
        if not lt or rt != lt:
            return _carry("ref-moved", "origin's tip " + rt[:12] + " is not the local copy "
                          + (lt or "unresolved")[:12] + ": the ref moved after this copy was made, "
                          "which is a Body still pushing", sig)
    age = sig.get("tip_age_min")
    if age is None:
        return _carry("tip-clock-unreadable", "the tip commit's clock could not be read", sig)
    if age < stale_minutes:
        return _carry("tip-recent", "the tip commit is %.0f min old (under %.0f): its Body pushed "
                      "recently" % (age, stale_minutes), sig)
    c = sig.get("carrier")
    if c is None:
        return _carry("need-carrier", "the heartbeat carrier has not been read", sig)
    verdict, ca = c.get("verdict"), c.get("age_min")
    if verdict is None or c.get("dead") is None:
        return _carry("carrier-unmeasured", "the heartbeat carrier could not be read (%s): an "
                      "unmeasured pulse is not a death certificate" % (c.get("why") or "no reason given"), sig)
    shown = "unknown" if ca is None else "%.0f" % ca
    if c["dead"] is not True:
        if verdict == "fresh-correct":
            return _carry("carrier-alive", "the heartbeat carrier is fresh-correct (%s min old): this "
                          "Body is alive, and a tip %.0f min old is only the age of its last push"
                          % (shown, age), sig)
        return _carry("carrier-not-dead", "the heartbeat carrier reads '%s' (%s min old), and "
                      "body_row_reaper does not call that Body dead" % (verdict, shown), sig)
    return _result("RETIRE", "retire", "tip %.0f min old; heartbeat carrier %s (%s min old)%s" % (
        age, verdict, shown, "; origin's tip is the local copy" if sig.get("check_remote", True) else ""), sig)


def gate(repo, ref, agent, sid, *, now=None, carrier_reader=None, check_remote=True):
    """Measure the signals for one ref and decide. Fail-closed: any error is CARRY."""
    sig = {"check_remote": bool(check_remote)}
    try:
        m = REF_RE.match(ref or "")
        if not m or m.group(1) != agent or m.group(2) != sid:
            return _carry("ref-name-mismatch", "the ref name does not carry the agent and sid it was "
                          "asked about", sig)
        from body_row_reaper import DEFAULT_REAP_STALE_MINUTES as stale
        now = time.time() if now is None else now
        sig["local_tip"] = local_tip(repo, ref)
        if check_remote:
            sig["remote_tip"] = remote_tip(repo, ref)
        sig["tip_age_min"] = tip_age_minutes(repo, sig["local_tip"], now) if sig["local_tip"] else None
        out = decide(sig, stale)
        if out["code"] != "need-carrier":
            return out
        try:
            verdict, evidence = (carrier_reader or read_carrier)(agent, sid, stale)
            dead = reaper_calls_dead(sid, verdict, evidence)
            age = evidence.get("carrier_age_minutes") if isinstance(evidence, dict) else None
            sig["carrier"] = {"verdict": verdict, "dead": dead, "age_min": age}
        except BaseException as e:  # noqa: BLE001 - SystemExit from a CLI-shaped helper included
            sig["carrier"] = {"verdict": None, "dead": None, "age_min": None,
                              "why": type(e).__name__ + ": " + str(e)[:80]}
        return decide(sig, stale)
    except BaseException as e:  # noqa: BLE001 - the gate fails CLOSED, never open
        return _carry("gate-error", "the gate could not finish (" + type(e).__name__ + ": "
                      + str(e)[:120] + "), so it does not permit", sig)


def line_of(res):
    """The one-line form callers print and receipts record: ASCII, no quote, no backslash."""
    text = ("RETIRE: " + res["why"]) if res["verdict"] == "RETIRE" else (
        "CARRY " + res["code"] + ": " + res["why"])
    return re.sub(r"[\"\\\r\n]+", " ", text).encode("ascii", "replace").decode("ascii")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("--repo", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--sid", required=True)
    p.add_argument("--no-remote", action="store_true")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = gate(args.repo, args.ref, args.agent, args.sid, check_remote=not args.no_remote)
    print(json.dumps(res, sort_keys=True) if args.json else line_of(res))
    return 0 if res["verdict"] == "RETIRE" else 1


if __name__ == "__main__":
    sys.exit(main())
