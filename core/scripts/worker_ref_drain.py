#!/usr/bin/env python3
"""Read-only drain PLAN for outstanding worker carrier tips (, unit 1).

`worker-ref-consume.sh --drain` is the one-invocation form of the g-306-284
carrier drain. This module is its first slice, the PLAN: it measures every
outstanding TIP the way the hand protocol does and says what a drain would do,
in order, and where it would stop for a reader's judgment. It changes nothing:
no merge, no push, no retire, no ref, index or working-tree write. (git itself
writes unreferenced objects: the merge-tree result trees and one throw-away
preview commit per planned tip.)

Input is the `--json` report of worker-ref-consume.sh. TIP selection, carry
state, supersession and the per-ref counts stay with that instrument and are
re-derived nowhere here. What the report does not carry is measured here,
against the merge each tip would actually be part of:

  * the merge-tree preview, CHAINED: a tip is previewed against the tree that
    merging the earlier planned tips would leave, not against HEAD, because two
    tips that touch one file can conflict only after the first has merged
  * the changed paths of that preview, the framework paths among them, and the
    paths on the daemon code surface (mind-api-code-changed.sh's own pathspec)
  * the by-record audit (carrier_merge_audit.py) of every changed *.jsonl path
    that deletes lines
  * overlap between the changed paths and this tree's uncommitted files
  * base freshness (HEAD against origin/main), a pin of the worker-ref store for
    a later terminating re-read, and the refs already reachable from origin/main,
    each with what the retire gate says (worker_ref_retire_gate.py: tip clock and
    heartbeat carrier, the same gate --retire asks when a Body's row is absent)

Verdicts, one per tip. Every applicable reason is listed; the strongest decides:

  STOP                a judgment point, or a read that did not complete: a
                      conflict, a failed preview, dirty overlap, a ledger audit
                      that is not CLEARS (UNMEASURED included)
  CARRY               merging is not wanted as it stands: the preview changes no
                      path (already at HEAD by another route), or re-adds an
                      ignored path
  MERGE-VERIFY-FIRST  merges cleanly, but a changed path is on the daemon code
                      surface (or could not be classified): verify the merged
                      tree before the merge commit deploys it
  MERGE               merges cleanly and nothing above applies

A read that failed is never a clean read. rc 1 from merge-tree is trusted as
"conflict" only when stdout opens with a tree id: a bad ref also exits 1, with
nothing on stdout. The plan assumes STOP and CARRY tips stay unmerged.
Rationale (WHY a chained read-only plan): core/config/rationale/worker-ref-drain-plan.md

CLI (used by worker-ref-consume.sh --drain):
    plan --repo R --refs-json FILE|- --audit-py PATH [--json]
Exit 0 whenever a plan was produced; 2 when the report does not parse.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SURFACE_SH = os.path.join(HERE, "mind-api-code-changed.sh")
# The same framework predicate worker-ref-consume.sh counts with.
FRAMEWORK_RE = re.compile(r"^(core/|\.claude/|CLAUDE\.md|mind_api/src/|mind_api/tests/)")
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# jsonl paths audited per tip: the bound worker-ref-consume.sh's own audit uses.
AUDIT_CAP = 6
RANK = {"MERGE": 0, "MERGE-VERIFY-FIRST": 1, "CARRY": 2, "STOP": 3}
_IDENT = {"GIT_AUTHOR_NAME": "drain-plan", "GIT_AUTHOR_EMAIL": "drain-plan@localhost",
          "GIT_COMMITTER_NAME": "drain-plan", "GIT_COMMITTER_EMAIL": "drain-plan@localhost"}


def _run(argv, env=None):
    p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env)
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace").strip()


def git(repo, *args, env=None):
    return _run(["git", "-C", repo, *args], env=env)


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def rev_commit(repo, name):
    rc, out, _ = git(repo, "rev-parse", "--verify", "-q", name + "^{commit}")
    sha = out.decode("ascii", "replace").strip()
    return sha if rc == 0 and OID_RE.match(sha) else None


def count_range(repo, rng):
    rc, out, _ = git(repo, "rev-list", "--count", rng)
    text = out.decode("ascii", "replace").strip()
    return int(text) if rc == 0 and text.isdigit() else None


def parse_merge_tree_z(rc, out):
    """Classify one `git merge-tree --write-tree --name-only -z` run.

    -> (status, tree_oid, conflicted_paths) with status clean | conflict | error.
    Measured on git 2.43: clean is `<oid>\\0`; a conflict is
    `<oid>\\0<name>\\0...\\0\\0<messages>` with rc 1; a bad ref exits 1 with
    NOTHING on stdout; unrelated histories exit 128. So the tree id on stdout,
    not the rc alone, is what licenses calling a run clean or conflicted.
    """
    fields = out.split(b"\0")
    oid = fields[0].decode("ascii", "replace")
    if not OID_RE.match(oid):
        return "error", None, []
    names = []
    for field in fields[1:]:
        if not field:
            break
        names.append(field.decode("utf-8", "replace"))
    if rc == 0 and not names:
        return "clean", oid, []
    if rc == 1:
        # Status over parse: rc 1 with no parsed name is still a conflicted merge.
        return "conflict", oid, names
    return "error", None, []


def parse_numstat_z(out):
    """`git diff --numstat -z --no-renames` -> [(added|None, deleted|None, path)].

    None is a binary row (`-\\t-\\tpath`): unmeasured, never zero."""
    rows = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        parts = rec.split(b"\t", 2)
        if len(parts) != 3:
            raise ValueError("numstat record without three fields: %r" % rec[:60])
        a, d, p = (x.decode("utf-8", "replace") for x in parts)
        rows.append((int(a) if a.isdigit() else None, int(d) if d.isdigit() else None, p))
    return rows


def dirty_paths(repo):
    """Uncommitted paths of this tree -> (set, "") or (None, why): unreadable is not clean."""
    rc, out, err = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if rc != 0:
        return None, err or "git status rc=%d" % rc
    recs = out.split(b"\0")
    paths, i = set(), 0
    while i < len(recs):
        rec = recs[i]
        i += 1
        if len(rec) < 4:
            continue
        paths.add(rec[3:].decode("utf-8", "replace"))
        if rec[:2].find(b"R") >= 0 or rec[:2].find(b"C") >= 0:
            i += 1  # rename/copy: the origin path is the next record
    return paths, ""


def surface_specs():
    """The daemon-surface pathspec, from the detector that owns it -> (list, "") or (None, why)."""
    try:
        from _runtime_bash import bash_cmd
        rc, out, err = _run(list(bash_cmd(SURFACE_SH, "--print-pathspec")))
    except Exception as e:  # noqa: BLE001 - unreadable is UNMEASURED, never an empty list
        return None, "%s: %s" % (type(e).__name__, e)
    specs = [x.strip() for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
    if rc != 0 or not specs:
        # An EMPTY spec list would make `git diff -- ` match every path.
        return None, err or "mind-api-code-changed.sh --print-pathspec returned no entries"
    return specs, ""


def run_audit(repo, audit_py, base, tip, tree, path):
    cmd = [sys.executable, audit_py, "--repo", repo, "--path", path,
           "--pre", base, "--tip", tip, "--merged", tree, "--json"]
    rc, out, err = _run(cmd)
    try:
        rep = json.loads(out.decode("utf-8", "replace"))
        verdict = rep["verdict"]
    except (ValueError, KeyError, TypeError):
        return {"path": path, "verdict": "UNMEASURED",
                "reasons": ["audit helper rc=%d gave no verdict%s" % (rc, (": " + err[:80]) if err else "")]}
    if verdict not in ("CLEARS", "NOT CLEARED", "UNMEASURED"):
        return {"path": path, "verdict": "UNMEASURED", "reasons": ["unknown verdict %r" % (verdict,)]}
    return {"path": path, "verdict": verdict, "reasons": list(rep.get("reasons") or [])}


def audit_ledgers(repo, audit_py, base, tip, tree, rows):
    """By-record audit of each changed *.jsonl path that deletes lines (a path
    deleting none cannot have lost or altered a record). Paths past the cap are
    reported UNMEASURED, so a skipped audit can never read as a clear one."""
    paths = [p for _, d, p in rows if p.endswith(".jsonl") and d]
    out = [run_audit(repo, audit_py, base, tip, tree, p) for p in paths[:AUDIT_CAP]]
    if len(paths) > AUDIT_CAP:
        out.append({"path": "(%d more *.jsonl path(s))" % (len(paths) - AUDIT_CAP),
                    "verdict": "UNMEASURED", "reasons": ["not audited: cap %d per tip" % AUDIT_CAP]})
    return out


def measure_tip(repo, base, tip, specs, audit_py):
    """Preview merging `tip` into `base` (HEAD, or the preview commit of the
    earlier planned tips). Never raises on a git failure: it lands in `error`."""
    m = {"status": "error", "error": "", "tree": None, "conflicts": [], "paths": [],
         "added": 0, "deleted": 0, "binary": 0, "framework": 0, "fw_deleting": 0,
         "surface": [], "ledger": []}
    rc, out, err = git(repo, "merge-tree", "--write-tree", "--name-only", "-z", base, tip)
    status, tree, conflicts = parse_merge_tree_z(rc, out)
    if status == "error":
        m["error"] = err or "merge-tree rc=%d with no tree id on stdout" % rc
        return m
    m.update(status=status, tree=tree, conflicts=conflicts)
    if status == "conflict":
        return m  # the result tree embeds conflict markers: counts over it would mislead
    rc, out, err = git(repo, "diff", "--numstat", "-z", "--no-renames", base, tree)
    try:
        if rc != 0:
            raise ValueError("git diff --numstat rc=%d: %s" % (rc, err))
        rows = parse_numstat_z(out)
    except ValueError as e:
        m.update(status="error", error=str(e))
        return m
    m["paths"] = [p for _, _, p in rows]
    m["added"] = sum(a for a, _, _ in rows if a is not None)
    m["deleted"] = sum(d for _, d, _ in rows if d is not None)
    m["binary"] = sum(1 for a, _, _ in rows if a is None)
    fw = [(d, p) for _, d, p in rows if FRAMEWORK_RE.match(p)]
    m["framework"] = len(fw)
    m["fw_deleting"] = sum(1 for d, _ in fw if d)
    if specs is None:
        m["surface"] = None
    else:
        rc, out, _ = git(repo, "diff", "--name-only", "-z", "--no-renames", base, tree, "--", *specs)
        m["surface"] = [x.decode("utf-8", "replace") for x in out.split(b"\0") if x] if rc == 0 else None
    m["ledger"] = audit_ledgers(repo, audit_py, base, tip, tree, rows)
    return m


def _names(items, cap=3):
    shown = ", ".join(items[:cap])
    return shown + (" (+%d more)" % (len(items) - cap) if len(items) > cap else "")


def decide(m, retrack, overlap, surface_why="", chain_err=""):
    """Pure verdict for one measured tip -> (verdict, reasons).

    retrack: the --check report's merge_retrack_real (-1 = UNMEASURED).
    overlap: sorted dirty paths this merge changes, or None when `git status` failed.
    Reasons are [{"code", "verdict", "text"}]; the strongest verdict decides."""
    reasons = []

    def add(code, verdict, text):
        reasons.append({"code": code, "verdict": verdict, "text": text})

    if chain_err:
        add("chain-broken", "STOP", "the preview chain is broken (%s): this preview is against HEAD "
            "only and may miss a conflict with the earlier planned tips" % chain_err)
    if m["status"] == "error":
        add("preview-failed", "STOP", "merge-tree preview failed (UNMEASURED, not a conflict): %s" % m["error"])
    elif m["status"] == "conflict":
        n = len(m["conflicts"])
        add("conflict", "STOP", "merge conflicts in %d path(s): %s; resolve by hand (--merge stops "
            "mid-merge) or carry" % (n, _names(m["conflicts"]) if n else "not listed by git"))
    else:
        if not m["paths"]:
            add("phantom", "CARRY", "the merge changes no path: the content is already at HEAD by "
                "another route. Do not merge; retirability is decided by the live row and ancestry "
                "(guard-3660)")
        if retrack > 0:
            add("retrack", "CARRY", "the merge re-adds %d ignored path(s) HEAD does not track "
                "(RE-TRACK, see --check): default disposition CARRY" % retrack)
        elif retrack < 0 and m["paths"]:
            add("retrack-unmeasured", "STOP", "the re-track check is UNMEASURED in the --check "
                "report: not read as clean")
        if overlap is None:
            add("dirty-unmeasured", "STOP", "uncommitted files could not be listed: overlap with "
                "this merge is UNMEASURED")
        elif overlap:
            add("dirty-overlap", "STOP", "the merge changes %d uncommitted file(s): %s; commit your "
                "own churn with explicit pathspecs (step 0) and re-plan" % (len(overlap), _names(overlap)))
        for rep in m["ledger"]:
            if rep["verdict"] != "CLEARS":
                add("ledger", "STOP", "ledger audit %s: %s (%s)" % (
                    rep["path"], rep["verdict"], "; ".join(rep["reasons"]) or "no reason given"))
        if m["surface"] is None:
            add("surface-unmeasured", "MERGE-VERIFY-FIRST", "daemon-surface classification is "
                "UNMEASURED (%s): verify the merged tree first" % (surface_why or "git diff failed"))
        elif m["surface"]:
            add("surface", "MERGE-VERIFY-FIRST", "the merge changes %d daemon-surface path(s): %s; "
                "verify the merged tree before the merge commit deploys it" % (
                    len(m["surface"]), _names(m["surface"])))
    verdict = max((r["verdict"] for r in reasons), key=RANK.get, default="MERGE")
    return verdict, reasons


def preview_commit(repo, tree, base, tip):
    """An unreferenced commit standing in for the merge, so the next tip is
    previewed against what the real merge would leave (its own parents)."""
    env = dict(os.environ, **_IDENT)
    rc, out, err = git(repo, "commit-tree", tree, "-p", base, "-p", tip,
                       "-m", "drain-plan preview (no ref points here)", env=env)
    sha = out.decode("ascii", "replace").strip()
    return (sha, "") if rc == 0 and OID_RE.match(sha) else (None, err or "git commit-tree rc=%d" % rc)


def pin_of(repo):
    """The worker-ref store as one digest: a later terminating re-read compares it."""
    rc, out, err = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/workers/")
    if rc != 0:
        return {"refs": None, "digest": None, "error": err or "for-each-ref rc=%d" % rc}
    lines = sorted(x for x in out.decode("utf-8", "replace").splitlines() if x)
    return {"refs": len(lines), "digest": hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]}


def base_state(repo, now):
    om = rev_commit(repo, "refs/remotes/origin/main")
    b = {"origin_main": om, "behind": None, "ahead": None, "fetched_min_ago": None, "fresh": None}
    if om:
        b["behind"] = count_range(repo, "HEAD..refs/remotes/origin/main")
        b["ahead"] = count_range(repo, "refs/remotes/origin/main..HEAD")
    if b["behind"] is not None:
        b["fresh"] = b["behind"] == 0
    rc, out, _ = git(repo, "rev-parse", "--git-path", "FETCH_HEAD")
    if rc == 0:
        p = out.decode("utf-8", "replace").strip()
        try:
            b["fetched_min_ago"] = int((now - os.path.getmtime(p if os.path.isabs(p) else os.path.join(repo, p))) // 60)
        except OSError:
            pass
    return b


def _is_ancestor(repo, sha, ref):
    rc, _, _ = git(repo, "merge-base", "--is-ancestor", sha, ref)
    return True if rc == 0 else (False if rc == 1 else None)


def _tip_ct(repo, sha):
    rc, out, _ = git(repo, "log", "-1", "--format=%ct", sha)
    text = out.decode("ascii", "replace").strip()
    return int(text) if rc == 0 and text.isdigit() else None


def partition(repo, refs_doc):
    """Split the report's rows -> (tips, held, unreadable, others). A row is a TIP
    exactly when the report says so: commits ahead, not this body, not superseded."""
    tips, held, unreadable, others = [], [], [], []
    for r in refs_doc.get("refs") or []:
        ref, tip = str(r.get("ref") or ""), str(r.get("tip_sha") or "")
        if not ref.startswith("refs/workers/") or not OID_RE.match(tip):
            unreadable.append({"ref": ref, "tip": tip, "why": "ref name or tip id is not usable"})
            continue
        if _int(r.get("unreadable"), 0):
            unreadable.append({"ref": ref, "tip": tip, "why": "the --check report could not read it"})
            continue
        row = {"ref": ref, "tip": tip, "commits_ahead": _int(r.get("commits_ahead"), 0),
               "age_h": _int(r.get("oldest_unlanded_age_h"), 0), "goal_ids": str(r.get("goal_ids") or ""),
               "retrack": _int(r.get("merge_retrack_real"), -1), "is_self": bool(_int(r.get("is_self"), 0))}
        if row["commits_ahead"] > 0 and not row["is_self"] and not r.get("superseded_by"):
            if r.get("carry") == "held":
                held.append({"ref": ref, "tip": tip, "owner_goal": str(r.get("carry_owner_goal") or "")})
            else:
                row["tip_ct"] = _tip_ct(repo, tip)
                tips.append(row)
        else:
            others.append(row)
    tips.sort(key=lambda t: (t["tip_ct"] is None, t["tip_ct"] or 0, t["ref"]))
    return tips, held, unreadable, others


def _default_retire_gate(repo, ref, now):
    """What `--retire`'s independent-signal gate says about this ref, read WITHOUT the origin
    reads: --retire re-reads those inside its own call (guard-5952) and stays the authority.
    A gate that cannot run reads CARRY, never RETIRE."""
    try:
        import worker_ref_retire_gate as rg
        parts = ref.split("/")
        res = rg.gate(repo, ref, parts[-2], parts[-1], now=now, check_remote=False)
        return {"verdict": res["verdict"], "code": res["code"], "line": rg.line_of(res)}
    except BaseException as e:  # noqa: BLE001 - SystemExit from a CLI-shaped helper included
        return {"verdict": "CARRY", "code": "gate-error", "line": "CARRY gate-error: " + type(e).__name__}


def build_plan(repo, refs_doc, audit_py, now=None, retire_gate=None):
    now = time.time() if now is None else now
    head = rev_commit(repo, "HEAD")
    tips, held, unreadable, others = partition(repo, refs_doc)
    plan = {"head": head, "base": base_state(repo, now), "pin": pin_of(repo), "tips": [],
            "held": held, "unreadable": unreadable, "retire_candidates": [], "warnings": []}
    specs, specs_why = surface_specs()
    dirty, dirty_why = dirty_paths(repo)
    if dirty is None:
        plan["warnings"].append("uncommitted files could not be listed (%s)" % dirty_why)
    om = plan["base"]["origin_main"]
    stale = plan["base"]["fresh"] is False
    base_rev, chain_err, chained = "HEAD", "", []
    for t in tips:
        m = measure_tip(repo, base_rev, t["tip"], specs, audit_py)
        overlap = None if dirty is None else sorted(set(m["paths"]) & dirty)
        verdict, reasons = decide(m, t["retrack"], overlap, specs_why, chain_err)
        shared = sorted({p for _, ps in chained for p in ps} & set(m["paths"]))
        t.update(verdict=verdict, reasons=reasons, preview=m, shared=shared,
                 landed_upstream=bool(stale and om and _is_ancestor(repo, t["tip"], om)))
        plan["tips"].append(t)
        if verdict in ("MERGE", "MERGE-VERIFY-FIRST"):
            sha, why = preview_commit(repo, m["tree"], base_rev, t["tip"])
            if sha:
                base_rev = sha
                chained.append((t["ref"], set(m["paths"])))
            else:
                chain_err = "no preview commit for %s: %s" % (t["ref"], why)
    if om:
        ask = retire_gate or _default_retire_gate
        for r in tips + others:
            if not r["is_self"] and _is_ancestor(repo, r["tip"], om):
                ct = _tip_ct(repo, r["tip"])
                plan["retire_candidates"].append({
                    "ref": r["ref"], "tip": r["tip"],
                    "tip_age_min": None if ct is None else max(0, int((now - ct) // 60)),
                    "gate": ask(repo, r["ref"], now)})
    plan["head_end"] = rev_commit(repo, "HEAD")
    plan["head_moved"] = plan["head_end"] != head
    counts = {v: 0 for v in RANK}
    for t in plan["tips"]:
        counts[t["verdict"]] += 1
    plan["summary"] = dict(counts, tips=len(plan["tips"]), held=len(held), unreadable=len(unreadable),
                           retire_candidates=len(plan["retire_candidates"]))
    return plan


def render(plan):
    b, s, pin = plan["base"], plan["summary"], plan["pin"]
    out = ["drain plan (READ-ONLY: nothing is merged, pushed or retired). HEAD %s, origin/main %s, "
           "behind=%s ahead=%s%s" % ((plan["head"] or "unresolved")[:12], (b["origin_main"] or "unresolved")[:12],
                                     b["behind"], b["ahead"],
                                     "" if b["fetched_min_ago"] is None else ", last fetch %d min ago" % b["fetched_min_ago"])]
    if b["fresh"] is False:
        out.append("  WARN STALE BASE: HEAD is %d commit(s) behind origin/main, so a tip's payload can read "
                   "larger than it is (guard-6051). Run `bash core/scripts/iteration-push.sh --no-push` and "
                   "re-plan before acting on this." % b["behind"])
    elif b["fresh"] is None:
        out.append("  WARN base freshness is UNMEASURED (origin/main does not resolve or could not be counted): "
                   "do not read this plan as current.")
    if plan["head_moved"]:
        out.append("  WARN HEAD moved from %s to %s while planning: re-run." % (
            (plan["head"] or "?")[:12], (plan["head_end"] or "?")[:12]))
    for w in plan["warnings"]:
        out.append("  WARN " + w)
    if not plan["tips"]:
        out.append("no outstanding TIP to plan.")
    else:
        out.append("tips in merge order (oldest tip first; each preview is chained onto the earlier MERGE tips):")
    for i, t in enumerate(plan["tips"], 1):
        m = t["preview"]
        out.append("  %d. %-18s %s  tip=%s  age=%dh%s" % (i, t["verdict"], t["ref"], t["tip"][:12], t["age_h"],
                                                         "  (already on origin/main: pull first)" if t["landed_upstream"] else ""))
        if m["status"] == "clean":
            ledger = "none" if not m["ledger"] else ", ".join("%s %s" % (r["verdict"], r["path"]) for r in m["ledger"])
            surface = "UNMEASURED" if m["surface"] is None else str(len(m["surface"]))
            out.append("       changes %d path(s) +%d/-%d lines; framework=%d (%d deleting lines); "
                       "daemon-surface=%s; ledger: %s" % (len(m["paths"]), m["added"], m["deleted"],
                                                          m["framework"], m["fw_deleting"], surface, ledger))
        if t["shared"]:
            out.append("       shares %d path(s) with an earlier planned tip (previewed chained): %s" % (
                len(t["shared"]), _names(t["shared"])))
        if t["goal_ids"]:
            out.append("       carries work naming: %s" % t["goal_ids"])
        for r in t["reasons"]:
            out.append("       - %s: %s" % (r["code"], r["text"]))
    for h in plan["held"]:
        out.append("held on their exact tip (not previewed): %s tip=%s owner=%s" % (h["ref"], h["tip"][:12], h["owner_goal"] or "-"))
    for u in plan["unreadable"]:
        out.append("UNREADABLE (not planned): %s (%s)" % (u["ref"] or "?", u["why"]))
    if plan["retire_candidates"]:
        out.append("retire candidates (tip reachable from origin/main). gate = --retire's independent-signal gate "
                   "(tip clock + heartbeat carrier) read without the origin reads; --retire re-reads everything "
                   "inside its own call and is the authority, and an absent in_flight row alone is no licence "
                   "(guard-3660):")
        for c in plan["retire_candidates"]:
            out.append("  %s tip=%s age=%s gate: %s" % (
                c["ref"], c["tip"][:12], "?" if c["tip_age_min"] is None else "%dmin" % c["tip_age_min"],
                c["gate"]["line"]))
    ready = sum(1 for c in plan["retire_candidates"] if c["gate"]["verdict"] == "RETIRE")
    out.append("drain plan: %d tip(s): MERGE=%d MERGE-VERIFY-FIRST=%d CARRY=%d STOP=%d | held=%d unreadable=%d | "
               "retire-candidates=%d (gate RETIRE=%d) | base fresh=%s behind=%s ahead=%s | pin refs=%s sha256:%s | HEAD %s" % (
                   s["tips"], s["MERGE"], s["MERGE-VERIFY-FIRST"], s["CARRY"], s["STOP"], s["held"],
                   s["unreadable"], s["retire_candidates"], ready,
                   {True: "yes", False: "NO", None: "UNMEASURED"}[b["fresh"]], b["behind"], b["ahead"],
                   pin["refs"], pin["digest"], (plan["head"] or "unresolved")[:12]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", required=True)
    p.add_argument("--refs-json", required=True)
    p.add_argument("--audit-py", required=True)
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read() if args.refs_json == "-" else open(args.refs_json, "rb").read()
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("refs"), list):
            raise ValueError("no refs list")
    except (OSError, ValueError) as e:
        print("drain plan REFUSED: the --json report did not parse (%s); nothing was planned" % e, file=sys.stderr)
        return 2
    plan = build_plan(args.repo, doc, args.audit_py)
    print(json.dumps(plan, sort_keys=True) if args.json else "\n".join(render(plan)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
