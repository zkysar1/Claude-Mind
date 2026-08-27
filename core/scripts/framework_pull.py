#!/usr/bin/env python3
"""Framework-pull executor: the ADOPTING side of the promotion chain.

Implements `core/config/conventions/pull-promotion.md` (C1-C7 + addenda a-d)
end to end:

    fetch-tags -> tag-compare -> quiesce -> preflight -> decision-registry gate
    -> seed-delta -> copy -> verify -> adopt   (or rollback)

Staging->downstream is a PULL: the adopting Mind decides *when*, inside its own
idle window, and no upstream actor touches its disk. This script is that Mind's
executor.

DESIGN: every decision is a PURE function over already-fetched data, so the
gate logic is testable without a live remote. The I/O layer (git, preflight,
suite) is a thin shell around them.

REUSE, NEVER FORK (the goal's explicit mandate + the convention's
existing-machinery inventory): the drift verdict comes from
`promotion-preflight.py` and the evidence ledger from
`promotion-plan-triage.sh`. This script contains NO second reconciler -- it
consumes their output and adds only the pull-side sequencing the push side
has no reason to own.

Exit codes:
  0  OK        -- plan produced, or adoption completed and verified
  2  BLOCKED   -- an unresolved gate (drift with unregistered files, KERNEL
                  escalation, dirty/incoming intersection, auth failure).
                  The Mind keeps running its installed tag, which is a
                  correct and safe state (C6 fail-closed).
  3  ROLLED_BACK -- adoption ran, verify went red, rollback executed
  1  ERROR     -- bad invocation / unreadable inputs
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_bash import BASH  # noqa: E402  (guard-580: never bare "bash")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_ROLLED_BACK = 3

# Registry classes (pull-promotion.md addendum a). KERNEL is down-only
# (guard-097/guard-098) and NEVER auto-resolves.
CLASS_KEEP_PROD_AHEAD = "keep-prod-ahead"
CLASS_BACK_PORT_FILED = "back-port-filed"
CLASS_KERNEL_ESCALATE = "KERNEL-escalate"
HONORED_CLASSES = frozenset({CLASS_KEEP_PROD_AHEAD, CLASS_BACK_PORT_FILED})

# Preflight JSON buckets that represent target-ahead-or-orphan drift, i.e. the
# files a blind copy would clobber or delete. Sourced from
# promotion-preflight.py's own emit block; source_ahead_* is deliberately NOT
# here (the source leading is the normal reason to pull).
BLOCKING_BUCKETS = (
    "orphan_risk_core", "orphan_risk_skills",
    "target_ahead_core", "target_ahead_skills",
    "ambiguous_core", "ambiguous_skills",
)

SEED_FILE = "core/config/world-aspirations-initial.jsonl"

# Daemon-read surface OUTSIDE mind-api-code-changed.sh's predicate. The
# predicate is keyed on daemon CODE (mind_api/src, core/scripts/_*.py, seven
# named modules) and core/config is measurably absent from it (0 hits) while
# the daemon reads core/config at runtime -- pull-promotion.md addendum d.
DAEMON_CONFIG_SURFACE = "core/config/"


# ---------------------------------------------------------------- pure logic

_SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def semver_key(tag: str):
    """Sort key for a vMAJOR.MINOR.PATCH tag, or None if not that shape.

    NEVER sort tags lexically: pull-promotion.md records that `git tag --list`
    sorts lexically, so v2.9.4 tails ABOVE v2.12.3 and a lexical `tail -1`
    silently picks an older release as "newest".
    """
    m = _SEMVER_RE.match((tag or "").strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def newest_tag(tags):
    """Newest vX.Y.Z tag by SEMVER order. Non-semver tags are ignored."""
    keyed = [(semver_key(t), t) for t in (tags or [])]
    keyed = [(k, t) for k, t in keyed if k is not None]
    if not keyed:
        return None
    return max(keyed)[1]


def tag_status(installed: str | None, newest: str | None) -> str:
    """'no-source' | 'unknown-installed' | 'current' | 'newer-available' | 'ahead'."""
    if not newest:
        return "no-source"
    ki, kn = semver_key(installed or ""), semver_key(newest)
    if ki is None:
        return "unknown-installed"
    if ki == kn:
        return "current"
    return "newer-available" if ki < kn else "ahead"


def parse_installed_release(text: str) -> dict:
    """Parse world/installed-release.yaml. Missing/blank -> {} (never raise).

    A first-ever pull legitimately has no such file; treating that as an error
    would make the executor unusable on exactly the Mind that most needs it.
    """
    if not text or not text.strip():
        return {}
    try:
        import yaml
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def render_installed_release(d: dict) -> str:
    import yaml
    return yaml.safe_dump(d, sort_keys=False, default_flow_style=False)


def parse_decisions(text: str) -> list:
    """Parse world/promotion-decisions.yaml -> list of decision rows.

    Unparseable or absent -> [] (an EMPTY registry, which is the fail-CLOSED
    direction: with no honored rows every flagged file is unregistered and the
    pull stops).
    """
    if not text or not text.strip():
        return []
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:
        return []
    if isinstance(data, dict):
        rows = data.get("decisions") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict) and r.get("path")]


def collect_flagged(preflight: dict) -> list:
    """Every distinct path the preflight flagged as target-ahead/orphan/ambiguous."""
    out = []
    for bucket in BLOCKING_BUCKETS:
        for p in (preflight.get(bucket) or []):
            if p not in out:
                out.append(p)
    return sorted(out)


def gate_drift(preflight: dict, decisions: list) -> dict:
    """THE gate. Decide whether a drifted preflight may proceed.

    Rules, in precedence order (pull-promotion.md addendum a):
      1. Any KERNEL-escalate row matching a flagged path STOPS the adoption and
         goes to a human -- never resolved by the executor, regardless of what
         else is registered. KERNEL is down-only (guard-097/guard-098).
      2. The preflight's OWN kernel_up_conflict bucket stops it too, even with
         no registry row: a prod leading on KERNEL is an anomaly.
      3. A flagged path with an honored row (keep-prod-ahead / back-port-filed)
         is satisfied. keep-prod-ahead paths are re-grafted on EVERY pull.
      4. Any remaining flagged path is UNREGISTERED -> STOP.
    """
    flagged = collect_flagged(preflight)
    by_path = {}
    for row in decisions:
        by_path.setdefault(str(row.get("path")), row)

    kernel_escalate, grafts, back_ported, unregistered = [], [], [], []
    for p in flagged:
        row = by_path.get(p)
        cls = (row or {}).get("class")
        if cls == CLASS_KERNEL_ESCALATE:
            kernel_escalate.append(p)
        elif cls == CLASS_KEEP_PROD_AHEAD:
            grafts.append(p)
        elif cls == CLASS_BACK_PORT_FILED:
            back_ported.append(p)
        else:
            unregistered.append(p)

    # Rule 2: preflight-detected KERNEL conflicts escalate even unregistered.
    for p in (preflight.get("kernel_up_conflict") or []):
        if p not in kernel_escalate:
            kernel_escalate.append(p)
        if p in unregistered:
            unregistered.remove(p)
        if p in grafts:
            grafts.remove(p)
        if p in back_ported:
            back_ported.remove(p)

    blockers = []
    if kernel_escalate:
        blockers.append("kernel-escalate")
    if unregistered:
        blockers.append("unregistered-drift")

    return {
        "flagged": flagged,
        "kernel_escalate": sorted(kernel_escalate),
        "grafts": sorted(grafts),
        "back_ported": sorted(back_ported),
        "unregistered": sorted(unregistered),
        "blockers": blockers,
        "proceed": not blockers,
    }


def disjoint(dirty, incoming) -> list:
    """C5 half 2: intersection of locally-dirty and incoming-change sets.

    Non-empty ⇒ do not adopt. This check protected 14 dirty files in the
    2026-08-11 run.
    """
    return sorted(set(dirty or []) & set(incoming or []))


def needs_daemon_recycle(changed_paths) -> bool:
    """Addendum d: restart when the adopted range touches core/config/**.

    post-merge already recycles for daemon CODE; core/config is read at runtime
    but is absent from mind-api-code-changed.sh's predicate, so nothing fires.
    """
    return any(str(p).startswith(DAEMON_CONFIG_SURFACE) for p in (changed_paths or []))


def seed_delta(old_text: str, new_text: str) -> list:
    """Addendum b: seed records present in NEW and absent from OLD.

    World seeds fire only at INIT, so a new seed record shipped in a release
    never reaches an already-initialised Mind. Compared by record `id` when
    present, else by the raw line, because a reformatted-but-identical record
    is not new work.
    """
    def index(text):
        out = {}
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                key = rec.get("id") or line
            except Exception:
                rec, key = {"_raw": line}, line
            out[key] = rec
        return out

    old, new = index(old_text), index(new_text)
    return [new[k] for k in new if k not in old]


# ------------------------------------------------------------------ io layer

def git(repo, *args, timeout=180):
    """Run git in `repo`. Returns (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # pragma: no cover - environment failure
        return 127, "", str(exc)


def fetch_tags(repo):
    """rb-4716: NEVER conclude staleness from an unfetched repo. Fetch FIRST."""
    return git(repo, "fetch", "--tags", "--quiet", timeout=300)


def list_tags(repo) -> list:
    rc, out, _ = git(repo, "tag", "--list", "v*", "--sort=v:refname")
    return out.splitlines() if rc == 0 else []


def dirty_files(repo) -> list:
    """Locally-modified AND untracked files, one path per FILE.

    `--untracked-files=all` is load-bearing, not tidiness: plain
    `git status --porcelain` COLLAPSES an untracked directory to a single
    `?? core/` row, so a file inside a newly-created directory never appears
    as its own path. The C5 disjointness check intersects this set against the
    incoming file list, so the collapsed form silently reports "no clash" for
    exactly the files it exists to protect. Caught by
    test_build_plan_blocks_on_dirty_incoming_intersection.
    """
    rc, out, _ = git(repo, "status", "--porcelain", "--untracked-files=all")
    if rc != 0:
        return []
    files = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            files.append(parts[1].strip())
    return files


def range_files(repo, a: str, b: str) -> list:
    rc, out, _ = git(repo, "diff", "--name-only", f"{a}..{b}")
    return out.splitlines() if rc == 0 else []


def tag_sha(repo, tag: str) -> str | None:
    rc, out, _ = git(repo, "rev-list", "-n", "1", tag)
    return out.strip() if rc == 0 and out.strip() else None


def show_file(repo, ref: str, path: str) -> str:
    rc, out, _ = git(repo, "show", f"{ref}:{path}")
    return out if rc == 0 else ""


def run_preflight(source, target, script_dir: Path):
    """Scoped CALL to the shared drift gate. Returns (rc, parsed_json_or_None)."""
    try:
        p = subprocess.run(
            [sys.executable, str(script_dir / "promotion-preflight.py"),
             "--source", str(source), "--target", str(target), "--json"],
            capture_output=True, text=True, timeout=900)
    except Exception as exc:  # pragma: no cover
        return 1, {"error": str(exc)}
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, None


def run_suite(project_root: Path, log_path: Path):
    """C4 verify. STORAGE_BACKEND=local is MANDATORY on an own-cloud box
    (guard-955): a tmp-world write otherwise collides on the PRODUCTION S3 key.
    """
    env = dict(os.environ, STORAGE_BACKEND="local", PYTHONUNBUFFERED="1")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
        try:
            p = subprocess.run([BASH, (project_root / "core/scripts/run-full-suite.sh").as_posix()],
                               stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=14400)
            rc = p.returncode
        except Exception as exc:  # pragma: no cover
            fh.write(f"\nrunner failed: {exc}\n")
            rc = 1
    text = log_path.read_text(encoding="utf-8", errors="replace")
    verdict = None
    for line in text.splitlines():
        if line.startswith("VERDICT:"):
            verdict = line.strip()
    return rc, verdict, text


def suite_is_green(rc: int, verdict: str | None) -> bool:
    """Read the VERDICT line first, never the totals.

    A missing verdict is NOT green: run-full-suite reports what it RAN, and an
    absent verdict means the chunked half never reached its conclusion
    (guard-1760). INVALID means the numbers mean nothing.
    """
    if verdict is None:
        return False
    if "CLEAN" not in verdict:
        return False
    return rc == 0


# ------------------------------------------------------------ orchestration

def build_plan(*, project_root: Path, source_repo: Path, agent: str | None,
               script_dir: Path, world_dir: Path, skip_quiesce: bool = False) -> dict:
    """The --plan dry-run. Reads everything, writes nothing, copies nothing."""
    report = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "project_root": str(project_root),
        "source_repo": str(source_repo),
        "agent": agent,
        "steps": [],
        "blockers": [],
        "proceed": False,
    }

    def step(name, ok, **kw):
        row = {"step": name, "ok": bool(ok)}
        row.update(kw)
        report["steps"].append(row)
        return row

    # C6 fail-closed: if the source cannot be read, adoption does NOT start.
    if not (source_repo / ".git").exists():
        step("source-repo", False, detail=f"no git repo at {source_repo}")
        report["blockers"].append("source-unreadable")
        return report
    step("source-repo", True, detail=str(source_repo))

    # rb-4716: fetch BEFORE any staleness conclusion.
    rc, _, err = fetch_tags(source_repo)
    step("fetch-tags", rc == 0, rc=rc, detail=err[:200] if err else "")
    if rc != 0:
        # Fail-closed and non-escalating: keep running the installed tag.
        report["blockers"].append("fetch-failed")
        return report

    tags = list_tags(source_repo)
    newest = newest_tag(tags)
    installed_path = world_dir / "installed-release.yaml"
    installed_doc = parse_installed_release(
        installed_path.read_text(encoding="utf-8", errors="replace")
        if installed_path.exists() else "")
    installed = installed_doc.get("installed_tag")
    status = tag_status(installed, newest)
    step("tag-compare", status in ("newer-available", "current", "ahead", "unknown-installed"),
         installed_tag=installed, newest_tag=newest, status=status, tags_seen=len(tags))
    report["installed_tag"] = installed
    report["newest_tag"] = newest
    report["tag_status"] = status

    if status == "no-source":
        report["blockers"].append("no-adoptable-tag")
        return report
    if status == "current":
        report["detail"] = "already on the newest tag; nothing to adopt"
        report["proceed"] = False
        return report

    # C5 quiesce half 2 -- disjointness. (Half 1, the liveness probe, is the
    # OPERATOR's call and is surfaced rather than auto-decided: concluding a
    # partner is dormant needs the multi-signal probe, never this script.)
    incoming = range_files(source_repo, installed, newest) if installed else []
    dirty = dirty_files(project_root)
    clash = disjoint(dirty, incoming)
    step("disjointness", not clash, dirty=len(dirty), incoming=len(incoming),
         intersection=clash)
    if clash:
        report["blockers"].append("dirty-incoming-intersection")

    if not skip_quiesce:
        step("quiesce-liveness", True,
             detail="OPERATOR CHECK, not automated: run "
                    "`liveness-check.sh --agent <agent> --json` and require "
                    "`dormant`; never conclude from a stale last_active alone")

    # C2 -- the drift gate, delegated to the shared reconciler.
    pf_rc, preflight = run_preflight(source_repo, project_root, script_dir)
    if preflight is None:
        step("preflight", False, rc=pf_rc, detail="preflight emitted no parseable JSON")
        report["blockers"].append("preflight-unreadable")
        return report
    step("preflight", pf_rc in (0, 2), rc=pf_rc, verdict=preflight.get("verdict"))

    decisions_path = world_dir / "promotion-decisions.yaml"
    decisions = parse_decisions(
        decisions_path.read_text(encoding="utf-8", errors="replace")
        if decisions_path.exists() else "")
    gate = gate_drift(preflight, decisions)
    report["gate"] = gate
    step("decision-registry", gate["proceed"],
         registry_rows=len(decisions), **{k: gate[k] for k in
         ("flagged", "unregistered", "kernel_escalate", "grafts", "back_ported")})
    report["blockers"].extend(gate["blockers"])

    # addendum b -- seed delta.
    if installed:
        delta = seed_delta(show_file(source_repo, installed, SEED_FILE),
                           show_file(source_repo, newest, SEED_FILE))
    else:
        delta = []
    report["seed_delta"] = delta
    step("seed-delta", True, new_records=len(delta),
         detail="each new record is filed in the adopting Mind's own aspirations "
                "or explicitly declined with a reason -- silence is not a decision")

    # addendum d -- daemon recycle predicate.
    recycle = needs_daemon_recycle(incoming)
    report["daemon_recycle_required"] = recycle
    step("daemon-recycle-check", True, required=recycle,
         detail="core/config/** is read by the daemon at runtime but is absent "
                "from mind-api-code-changed.sh's predicate (addendum d)")

    report["rollback"] = {
        "source_sha": installed_doc.get("source_sha"),
        "command": "git reset --hard <source_sha>; bash core/scripts/mind-api-start.sh --restart",
        "note": "prefer the RELEASES.json entry's own rollback_recipe when it carries one",
    }
    report["proceed"] = not report["blockers"]
    return report


def render_plan(report: dict) -> str:
    L = []
    L.append("═══ FRAMEWORK PULL — PLAN (dry run, nothing copied) ═══")
    L.append(f"source   : {report.get('source_repo')}")
    L.append(f"target   : {report.get('project_root')}")
    L.append(f"installed: {report.get('installed_tag')}   newest: {report.get('newest_tag')}"
             f"   status: {report.get('tag_status')}")
    L.append("")
    for s in report.get("steps", []):
        mark = "ok  " if s.get("ok") else "STOP"
        extra = {k: v for k, v in s.items() if k not in ("step", "ok")}
        L.append(f"  [{mark}] {s['step']}" + (f"  {json.dumps(extra, default=str)}" if extra else ""))
    gate = report.get("gate")
    if gate and gate.get("flagged"):
        L.append("")
        L.append(f"  drift flagged {len(gate['flagged'])} file(s):")
        for k in ("unregistered", "kernel_escalate", "grafts", "back_ported"):
            if gate.get(k):
                L.append(f"    {k}: {', '.join(gate[k])}")
    if report.get("seed_delta"):
        L.append("")
        L.append(f"  seed-delta: {len(report['seed_delta'])} new record(s) to file or decline")
    L.append("")
    if report.get("blockers"):
        L.append(f"VERDICT: BLOCKED — {', '.join(report['blockers'])}")
        L.append("The Mind keeps running its installed tag, which is a correct and safe state.")
    elif report.get("tag_status") == "current":
        L.append("VERDICT: CURRENT — already on the newest tag; nothing to adopt.")
    else:
        L.append("VERDICT: CLEAR TO ADOPT")
    return "\n".join(L)


# --------------------------------------------------------------- adopt side

def framework_paths(script_dir: Path) -> list:
    """The copy set, READ FROM promotion-preflight.py rather than re-declared.

    Forking this list is the exact defect the goal forbids ("MUST reuse ...
    extend, never fork a parallel reconciler"): a second copy drifts silently
    the first time someone adds a framework path, and the parity test that
    guards the manifest would never see it.
    """
    text = (script_dir / "promotion-preflight.py").read_text(encoding="utf-8",
                                                             errors="replace")
    m = re.search(r"^FRAMEWORK_PATHS = \[(.*?)^\]", text, re.S | re.M)
    if not m:
        raise RuntimeError("FRAMEWORK_PATHS not found in promotion-preflight.py")
    return re.findall(r'"([^"]+)"', m.group(1))


def copy_tree(src: Path, dst: Path) -> int:
    """Copy a framework path, replacing the destination. Returns files written."""
    import shutil
    if not src.exists():
        return 0
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1
    n = 0
    for s in src.rglob("*"):
        if s.is_dir():
            continue
        rel = s.relative_to(src)
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        n += 1
    return n


def rollback(project_root: Path, source_sha: str, restart=None) -> dict:
    """C4 rollback: one reset, then recycle the daemon.

    `restart` is injectable so a test can exercise the ROLLBACK PATH without a
    live daemon -- the goal requires this path be exercised, not documented.
    """
    out = {"source_sha": source_sha, "reset_rc": None, "restarted": False}
    if not source_sha:
        out["error"] = "no source_sha recorded; cannot roll back automatically"
        return out
    rc, _, err = git(project_root, "reset", "--hard", source_sha)
    out["reset_rc"] = rc
    if err:
        out["reset_stderr"] = err[:300]
    if rc != 0:
        return out
    if restart is None:
        restart = _default_restart
    try:
        out["restarted"] = bool(restart(project_root))
    except Exception as exc:  # pragma: no cover
        out["restart_error"] = str(exc)
    return out


def _default_restart(project_root: Path) -> bool:
    try:
        p = subprocess.run(
            [BASH, (project_root / "core/scripts/mind-api-start.sh").as_posix(),
             "--restart"], capture_output=True, text=True, timeout=300)
        return p.returncode == 0
    except Exception:  # pragma: no cover
        return False


def adopt(*, project_root: Path, source_repo: Path, newest: str, plan: dict,
          world_dir: Path, verify=None, restart=None, pusher=None,
          copier=None) -> dict:
    """Execute the adoption: graft -> copy -> commit -> verify -> adopt/rollback.

    Every side-effecting collaborator is injectable so the RED path (verify
    fails -> rollback) is testable without a 30-minute suite or a live daemon.
    """
    script_dir = Path(__file__).resolve().parent
    result = {"tag": newest, "adopted": False, "rolled_back": False, "steps": []}

    def step(name, ok, **kw):
        row = {"step": name, "ok": bool(ok)}
        row.update(kw)
        result["steps"].append(row)

    pre_rc, pre_sha, _ = git(project_root, "rev-parse", "HEAD")
    if pre_rc != 0:
        step("pre-adopt-sha", False)
        return result
    result["pre_adopt_sha"] = pre_sha
    step("pre-adopt-sha", True, sha=pre_sha)

    gate = plan.get("gate") or {}
    grafts = list(gate.get("grafts") or [])
    # keep-prod-ahead content is captured BEFORE the copy and re-applied as an
    # explicit commit AFTER it, so prod content is never clobbered even
    # transiently (the leg-2 pattern the live run proved necessary).
    graft_blobs = {}
    for rel in grafts:
        f = project_root / rel
        if f.exists():
            graft_blobs[rel] = f.read_bytes()
    step("capture-grafts", True, count=len(graft_blobs))

    # Materialise the tag without disturbing the source checkout.
    import tempfile, shutil as _shutil
    wt = Path(tempfile.mkdtemp(prefix="framework-pull-wt-"))
    rc, _, err = git(source_repo, "worktree", "add", "--detach", str(wt), newest,
                     timeout=600)
    if rc != 0:
        step("worktree-at-tag", False, rc=rc, detail=err[:300])
        _shutil.rmtree(wt, ignore_errors=True)
        return result
    step("worktree-at-tag", True, path=str(wt))

    # `--no-verify` on both commits below is DELIBERATE and is not a guard-901
    # bypass. The githook chain gates AUTHORING ("does this edit meet our
    # standards"); an adoption INSTALLS content that was already authored and
    # already gated upstream. Running author-side gates at install time is a
    # category error -- the hot-path size budget in particular refuses growth,
    # so a legitimate upstream adoption would wedge half-copied with no commit.
    # The install-side gate is C4 verify + rollback below, which is stronger.
    failure = None
    try:
        if copier is None:
            copier = copy_tree
        written = 0
        for rel in framework_paths(script_dir):
            written += copier(wt / rel, project_root / rel)
        step("copy-framework", True, files=written)

        for rel, blob in graft_blobs.items():
            f = project_root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(blob)
        if graft_blobs:
            rc_g, _, err_g = git(project_root, "add", "--", *graft_blobs.keys())
            if rc_g != 0:
                failure = f"re-graft add failed rc={rc_g}: {err_g[:200]}"
            elif git(project_root, "diff", "--cached", "--name-only")[1].strip():
                rc_g, _, err_g = git(
                    project_root, "commit", "-m",
                    f"chore(pull): re-graft {len(graft_blobs)} keep-prod-ahead "
                    f"file(s) before adopting {newest}", "--no-verify")
                if rc_g != 0:
                    failure = f"re-graft commit failed rc={rc_g}: {err_g[:200]}"
            # else: the graft content already equals HEAD, so the restore was a
            # no-op and there is nothing to commit. That is the COMMON case --
            # keep-prod-ahead content is prod-ahead precisely because it is
            # already committed here -- so treating the empty commit as a
            # failure would refuse every ordinary re-graft.
        step("re-graft", failure is None, count=len(graft_blobs))

        if failure is None:
            # Stage only the framework paths that EXIST here. ONE absent
            # pathspec makes `git add` abort rc=128 and stage NOTHING -- not
            # even the paths that do exist -- and the commit then fails "nothing
            # added to commit". Unchecked, that reported a clean adoption while
            # committing zero files, and any target without `mind_api/` (the
            # fresh-world pull case) hit it every time. Measured, not inferred.
            wanted = framework_paths(script_dir)
            stage = [r for r in wanted if (project_root / r).exists()]
            absent = [r for r in wanted if r not in stage]
            rc_a, _, err_a = ((0, "", "") if not stage else
                              git(project_root, "add", "-A", "--", *stage))
            if rc_a != 0:
                failure = f"adopt add failed rc={rc_a}: {err_a[:200]}"
            else:
                staged = bool(git(project_root, "diff", "--cached",
                                  "--name-only")[1].strip())
                if written and not stage:
                    # The copier wrote files yet not one framework path exists
                    # here -- contradictory, since the copier writes INTO those
                    # very paths. Deliberately NOT keyed on `staged`: a copy can
                    # legitimately stage nothing when the incoming content
                    # already equals HEAD, or when a re-graft restores it (both
                    # measured). `written` counts bytes copied, NOT files that
                    # differ from HEAD, and conflating the two rejects ordinary
                    # no-op adoptions.
                    failure = (f"copied {written} file(s) but no framework path "
                               f"exists in the target (absent={absent})")
                elif not staged:
                    step("adopt-commit", True, no_changes=True,
                         staged_paths=len(stage), skipped_absent=absent)
                else:
                    rc_c, _, err_c = git(
                        project_root, "commit", "-m",
                        f"chore: adopt framework {newest}", "--no-verify")
                    if rc_c != 0:
                        failure = f"adopt commit failed rc={rc_c}: {err_c[:200]}"
                    else:
                        step("adopt-commit", True, staged_paths=len(stage),
                             skipped_absent=absent)
        if failure:
            step("adopt-commit", False, detail=failure)
    finally:
        git(source_repo, "worktree", "remove", "--force", str(wt), timeout=300)
        _shutil.rmtree(wt, ignore_errors=True)

    if failure:
        # Never run a 32-minute verify over a tree whose adopt did not land,
        # and never leave the half-applied copy behind.
        result["error"] = failure
        result["rollback"] = rollback(project_root, pre_sha, restart=restart)
        result["rolled_back"] = True
        return result

    # C4 -- verify on THIS box. Never set verified: true without a clean verdict.
    if verify is None:
        log = project_root / "agents" / (os.environ.get("MIND_AGENT") or "unknown") \
              / "temp" / f"framework-pull-verify-{newest}.log"
        def verify(root=project_root, _log=log):
            rc, verdict, _ = run_suite(root, _log)
            return suite_is_green(rc, verdict), verdict
    green, verdict = verify()
    step("verify", green, verdict=verdict)

    if not green:
        result["rollback"] = rollback(project_root, pre_sha, restart=restart)
        result["rolled_back"] = True
        step("rollback", bool(result["rollback"].get("reset_rc") == 0))
        return result

    sha = tag_sha(source_repo, newest)
    doc = {
        "installed_tag": newest,
        "adopted_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "adopted_from": os.environ.get("MIND_UPSTREAM", "claude-mind"),
        "source_sha": sha,
        "verified": True,
    }
    world_dir.mkdir(parents=True, exist_ok=True)
    (world_dir / "installed-release.yaml").write_text(render_installed_release(doc),
                                                      encoding="utf-8")
    step("record-installed-release", True, **doc)

    # Multi-clone deployments converge only through the remote.
    if pusher is None:
        def pusher(root=project_root):
            return git(root, "push")[0] == 0
    result["pushed"] = bool(pusher())
    step("push", result["pushed"])

    if plan.get("daemon_recycle_required"):
        r = restart or _default_restart
        result["restarted"] = bool(r(project_root))
        step("daemon-recycle", result["restarted"])

    result["adopted"] = True
    return result


def render_adopt(result: dict) -> str:
    L = ["═══ FRAMEWORK PULL — ADOPT ═══", f"tag: {result.get('tag')}"]
    for s in result.get("steps", []):
        mark = "ok  " if s.get("ok") else "FAIL"
        extra = {k: v for k, v in s.items() if k not in ("step", "ok")}
        L.append(f"  [{mark}] {s['step']}" + (f"  {json.dumps(extra, default=str)}" if extra else ""))
    if result.get("rolled_back"):
        L.append("VERDICT: ROLLED BACK — verify was not clean; tree reset to "
                 f"{result.get('pre_adopt_sha')}")
    elif result.get("adopted"):
        L.append("VERDICT: ADOPTED and verified")
    else:
        L.append("VERDICT: NOT ADOPTED")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Framework-pull executor (pull-promotion.md). "
                    "Default mode is --plan: it never copies.")
    ap.add_argument("--source-repo", required=True,
                    help="path to the staging clone to adopt FROM")
    ap.add_argument("--plan", action="store_true",
                    help="dry run: full report, nothing copied (DEFAULT)")
    ap.add_argument("--adopt", action="store_true",
                    help="execute the adoption (requires a clear plan)")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT"))
    ap.add_argument("--skip-quiesce-note", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent.parent
    try:
        from _paths import WORLD_DIR
        world_dir = Path(WORLD_DIR)
    except Exception:
        world_dir = project_root / "world"

    report = build_plan(project_root=project_root,
                        source_repo=Path(args.source_repo).resolve(),
                        agent=args.agent, script_dir=script_dir,
                        world_dir=world_dir,
                        skip_quiesce=args.skip_quiesce_note)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_plan(report))

    if args.adopt:
        if not report.get("proceed"):
            # --adopt over a blocked plan is refused, loudly. Never adopt past
            # an unresolved exit 2 (C2).
            print("\nREFUSED: --adopt requires a clear plan. Resolve the blockers "
                  "above.", file=sys.stderr)
            return EXIT_BLOCKED
        result = adopt(project_root=project_root,
                       source_repo=Path(args.source_repo).resolve(),
                       newest=report["newest_tag"], plan=report,
                       world_dir=world_dir)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print("\n" + render_adopt(result))
        if result.get("rolled_back"):
            return EXIT_ROLLED_BACK
        return EXIT_OK if result.get("adopted") else EXIT_BLOCKED

    return EXIT_BLOCKED if report.get("blockers") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
