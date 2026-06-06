#!/usr/bin/env python3
"""pointer_freshness.py -- deterministic pointer-doc freshness checker.

WHY THIS EXISTS
---------------
Some convention docs are *pointers*: a short stub in one world that summarizes
a *canonical* doc living somewhere else (often another repo). Pointers rot
silently -- the canonical changes, the pointer's "Last verified" date ages, and
nobody notices until a reader trusts a stale summary. The previous mitigation
was a recurring "re-verify the pointer" goal that fired on a clock regardless of
whether anything actually changed. That wastes loop turns on no-op verifications
and still misses drift between firings.

This module replaces the clock with content. It scans WORLD_DIR for docs that
carry a freshness-check marker, hashes the canonical they track, and:

  - fresh  (verified within max_age_days): no-op.
  - stale + canonical hash UNCHANGED: AUTO-BUMP the verified date to today.
        The pointer is still accurate; only the clock was stale. Deterministic,
        no loop turn, no goal.
  - stale + canonical hash CHANGED (drift): file ONE deduped Investigate goal so
        an agent re-reads the canonical, re-syncs the pointer summary, and
        re-anchors the marker (`--reanchor`). The date is NOT bumped on drift --
        leaving it stale keeps the signal alive until a human/agent re-verifies.
  - canonical missing/unreadable: log only, NO goal (avoids cross-machine false
        alarms when an absolute path does not resolve on a different host; a real
        deletion still surfaces in the event log).

PURE LOGIC, NO WATCHDOG DEPENDENCY
----------------------------------
This module imports nothing from agent-watchdog.py (whose hyphenated filename is
not importable anyway). The thin FreshnessProbe wrapper that adapts scan() to the
watchdog Event/Probe contract lives in agent-watchdog.py and lazy-imports this
module inside check(), so a syntax error here can never crash the watchdog tick.
That keeps this module identical across repos and unit-testable in isolation.

MARKER FORMAT
-------------
An HTML comment anywhere in a *.md file under WORLD_DIR:

  <!-- freshness-check: canonical="<abs-path>" sha256="<hex>" verified="YYYY-MM-DD"
       max_age_days="30" target_aspiration="asp-NNN" target_source="world" -->

  canonical          (required) absolute path to the doc this pointer tracks.
  sha256             (required) normalized hash of the canonical at last verify.
  verified           (required) ISO date (YYYY-MM-DD) of last verification.
  max_age_days       (required) staleness threshold in days.
  target_aspiration  (optional) aspiration id for the drift Investigate goal.
                     If absent, drift is logged but no goal is filed.
  target_source      (optional) "world" (default) or an agent name -- which
                     aspirations file the drift goal is filed into.

The hash is computed over newline-normalized UTF-8 (CRLF/CR -> LF) so that
editor line-ending churn does not register as drift.

FAIL-OPEN
---------
Every per-pointer operation is wrapped: a bad marker, an unreadable file, or a
daemon-down goal-filing failure is captured into the result and never raised.
One broken pointer cannot stop the scan or crash the watchdog.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

OPEN_STATUSES = {"pending", "in-progress", "blocked"}

# Capture the freshness-check marker body (everything up to the closing -->).
_MARKER_RE = re.compile(r"freshness-check:\s*(.*?)(?:-->|$)", re.DOTALL)
# key="value" pairs inside the marker body.
_KV_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
# Human-facing "Last verified: YYYY-MM-DD" lines (bumped alongside the marker).
_LAST_VERIFIED_RE = re.compile(r"(Last verified:\s*)\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def normalize_and_hash(path) -> Optional[str]:
    """sha256 of a file's newline-normalized UTF-8 content. None if unreadable."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Marker parsing
# ---------------------------------------------------------------------------

def parse_marker(line: str) -> Optional[dict]:
    """Parse one freshness-check marker line into a dict, or None if no marker /
    missing a required key."""
    m = _MARKER_RE.search(line)
    if not m:
        return None
    kv = {k: v for k, v in _KV_RE.findall(m.group(1))}
    if not all(k in kv for k in ("canonical", "sha256", "verified", "max_age_days")):
        return None
    return kv


def discover_pointers(world_dir) -> list[dict]:
    """Find every *.md under world_dir carrying a freshness-check marker.

    Returns a list of {"file": Path, "marker": dict}. Files that error on read
    are skipped silently (fail-open) -- discovery must never raise.
    """
    out: list[dict] = []
    root = Path(world_dir)
    if not root.is_dir():
        return out
    for md in root.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "freshness-check:" not in text:
            continue
        # Match against the FULL text (not per-line) so multi-line markers --
        # the format the module docstring shows -- are not silently dropped.
        # _MARKER_RE is re.DOTALL; the per-line split defeated it.
        for m in _MARKER_RE.finditer(text):
            kv = {k: v for k, v in _KV_RE.findall(m.group(1))}
            if all(k in kv for k in ("canonical", "sha256", "verified", "max_age_days")):
                out.append({"file": md, "marker": kv})
    return out


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _today_str(today: Optional[str]) -> str:
    return today or date.today().isoformat()


def _age_days(verified: str, today: str) -> Optional[int]:
    try:
        v = date.fromisoformat(verified)
        t = date.fromisoformat(today)
    except ValueError:
        return None
    return (t - v).days


# ---------------------------------------------------------------------------
# Auto-bump (stale + hash match)
# ---------------------------------------------------------------------------

def auto_bump(file_path, old_verified: str, today: str) -> bool:
    """Rewrite the marker's verified="OLD" to today, plus any human-facing
    "Last verified: OLD" lines. Preserves original line endings (byte-level
    replace, no universal-newline translation). Atomic .tmp + os.replace.
    Returns True if the file changed."""
    raw = Path(file_path).read_bytes()
    text = raw.decode("utf-8")
    new_text = text.replace(f'verified="{old_verified}"', f'verified="{today}"')
    new_text = _LAST_VERIFIED_RE.sub(lambda mm: mm.group(1) + today, new_text)
    if new_text == text:
        return False
    tmp = str(file_path) + f".tmp.{os.getpid()}"
    Path(tmp).write_bytes(new_text.encode("utf-8"))
    os.replace(tmp, str(file_path))
    return True


# ---------------------------------------------------------------------------
# Drift escalation (stale + hash mismatch)
# ---------------------------------------------------------------------------

def slug_for(file_path) -> str:
    stem = Path(file_path).stem.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return slug or "pointer"


def open_goal_exists(origin_signal: str, world_dir, agent_dir) -> bool:
    """True if an OPEN goal with this origin_signal already exists in the world
    or agent aspirations file. Direct JSONL read (daemon-free) so dedup works
    even when the daemon is down. Fail-open: any error returns False (we then
    attempt to file; the daemon's own duplication gate is the backstop)."""
    candidates = []
    if world_dir:
        candidates.append(Path(world_dir) / "aspirations.jsonl")
    if agent_dir:
        candidates.append(Path(agent_dir) / "aspirations.jsonl")
    for p in candidates:
        try:
            if not p.exists():
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw or '"origin_signal"' not in raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # Production: each JSONL line is an aspiration record whose
                    # goals (which carry origin_signal) are nested in a "goals"
                    # array -- NOT at the top level. Drill in.
                    for goal in obj.get("goals", []):
                        if (goal.get("origin_signal") == origin_signal
                                and goal.get("status") in OPEN_STATUSES):
                            return True
                    # Defensive: also honor a flat goal-shaped line.
                    if (obj.get("origin_signal") == origin_signal
                            and obj.get("status") in OPEN_STATUSES):
                        return True
        except OSError:
            continue
    return False


def file_drift_goal(pointer_file, marker: dict, slug: str, project_root) -> dict:
    """File one Investigate goal via aspirations-add-goal.sh. Returns
    {"filed": bool, "goal_id": str|None, "error": str|None}. Fail-open."""
    target_asp = marker.get("target_aspiration")
    if not target_asp:
        return {"filed": False, "goal_id": None, "error": "no target_aspiration in marker"}
    source = marker.get("target_source", "world")
    canonical = marker.get("canonical", "")
    max_age = marker.get("max_age_days", "?")
    # ASCII-only payload -- the Windows shell emits em-dashes/arrows as cp1252
    # bytes that break the daemon's UTF-8 JSON decode (rb-137).
    body = {
        "title": f"Investigate: re-verify {slug} pointer against drifted canonical",
        "priority": "MEDIUM",
        "participants": ["agent"],
        "description": (
            f"The pointer-freshness probe detected DRIFT: the canonical doc "
            f"'{canonical}' changed (its normalized content hash differs from the "
            f"sha256 recorded in the freshness-check marker of '{pointer_file}'), "
            f"and the pointer has not been re-verified within {max_age} days. "
            f"Re-read the canonical, update this pointer's summary to match, then "
            f"re-anchor the marker by running: python3 core/scripts/pointer_freshness.py "
            f"--reanchor '{pointer_file}'. Auto-filed by the pointer-freshness watchdog probe."
        ),
        "category": "knowledge-management",
        "origin_signal": f"investigate:freshness-drift-{slug}",
    }
    # Repo-agnostic shell resolution (Windows WSL-bash lottery): _paths.sh exports
    # MIND_SHELL (the canonical name); RT_BASH is the watchdog's own var. Bare
    # "bash" can hit the System32 WSL stub and fail rc=127.
    _bash = (os.environ.get("MIND_SHELL")
             or os.environ.get("RT_BASH")
             or "bash")
    try:
        proc = subprocess.run(
            [_bash, "core/scripts/aspirations-add-goal.sh", target_asp, "--source", source],
            input=json.dumps(body, ensure_ascii=True),
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"filed": False, "goal_id": None, "error": f"{type(e).__name__}: {e}"}
    if proc.returncode != 0:
        return {"filed": False, "goal_id": None,
                "error": (proc.stderr or proc.stdout or "non-zero exit").strip()[:300]}
    goal_id = None
    try:
        rec = json.loads(proc.stdout)
        goal_id = rec.get("id")
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"filed": True, "goal_id": goal_id, "error": None}


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------

def scan(world_dir, agent_dir=None, project_root=None,
         today: Optional[str] = None, dry_run: bool = False) -> dict:
    """Scan world_dir for freshness markers and act (bump / escalate).

    Returns {"results": [per-pointer dict], "summary": {...}}. Each result:
      {path, slug, status, canonical, recorded_hash, current_hash, verified,
       age_days, max_age_days, goal_filed, goal_id, dedup_skipped, error}
    status in {fresh, bumped, drift, canonical_missing, error}.
    Never raises -- per-pointer errors become status="error".
    """
    today = _today_str(today)
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent
    results: list[dict] = []

    try:
        entries = discover_pointers(world_dir)
    except Exception as e:  # honor the "Never raises" contract
        return {"results": [], "summary": {
            "checked": 0, "fresh": 0, "bumped": 0, "drift": 0, "drift_filed": 0,
            "drift_deduped": 0, "canonical_missing": 0, "errors": 1},
            "discovery_error": str(e)}

    for entry in entries:
        f = entry["file"]
        marker = entry["marker"]
        r = {
            "path": str(f),
            "slug": slug_for(f),
            "status": "error",
            "canonical": marker.get("canonical"),
            "recorded_hash": marker.get("sha256"),
            "current_hash": None,
            "verified": marker.get("verified"),
            "age_days": None,
            "max_age_days": marker.get("max_age_days"),
            "goal_filed": False,
            "goal_id": None,
            "dedup_skipped": False,
            "error": None,
        }
        try:
            age = _age_days(marker["verified"], today)
            r["age_days"] = age
            try:
                max_age = int(marker["max_age_days"])
            except (ValueError, TypeError):
                r["error"] = f"bad max_age_days={marker.get('max_age_days')!r}"
                results.append(r)
                continue
            if age is None:
                r["error"] = f"bad verified date={marker.get('verified')!r}"
                results.append(r)
                continue

            # Fresh: nothing to do (cheap path -- no canonical hash needed).
            if age < max_age:
                r["status"] = "fresh"
                results.append(r)
                continue

            # Stale: hash the canonical to decide bump vs drift.
            current = normalize_and_hash(marker["canonical"])
            r["current_hash"] = current
            if current is None:
                r["status"] = "canonical_missing"
                results.append(r)
                continue

            if current == marker["sha256"]:
                # Stale but unchanged -- deterministic auto-bump.
                if dry_run:
                    r["status"] = "bumped"  # would bump
                else:
                    changed = auto_bump(f, marker["verified"], today)
                    r["status"] = "bumped" if changed else "fresh"
                results.append(r)
                continue

            # Stale AND changed -- drift. Do not bump; escalate (deduped).
            r["status"] = "drift"
            origin_signal = f"investigate:freshness-drift-{r['slug']}"
            if dry_run:
                results.append(r)
                continue
            if open_goal_exists(origin_signal, world_dir, agent_dir):
                r["dedup_skipped"] = True
            else:
                filed = file_drift_goal(f, marker, r["slug"], project_root)
                r["goal_filed"] = filed["filed"]
                r["goal_id"] = filed["goal_id"]
                if filed["error"]:
                    r["error"] = filed["error"]
            results.append(r)
        except Exception as e:  # absolute fail-open per pointer
            r["status"] = "error"
            r["error"] = f"{type(e).__name__}: {e}"
            results.append(r)

    summary = {
        "checked": len(results),
        "fresh": sum(1 for r in results if r["status"] == "fresh"),
        "bumped": sum(1 for r in results if r["status"] == "bumped"),
        "drift": sum(1 for r in results if r["status"] == "drift"),
        "drift_filed": sum(1 for r in results if r["status"] == "drift" and r["goal_filed"]),
        "drift_deduped": sum(1 for r in results if r["status"] == "drift" and r["dedup_skipped"]),
        "canonical_missing": sum(1 for r in results if r["status"] == "canonical_missing"),
        "errors": sum(1 for r in results if r["status"] == "error"),
    }
    return {"results": results, "summary": summary}


# ---------------------------------------------------------------------------
# Re-anchor (resolve a drift goal: recompute the marker hash + date)
# ---------------------------------------------------------------------------

def reanchor(pointer_file, today: Optional[str] = None) -> dict:
    """Recompute the canonical hash and rewrite the marker's sha256 + verified
    date to today. Single source of truth for the hash computation so an agent
    resolving a drift goal does not have to replicate normalize_and_hash by
    hand. Returns {"ok": bool, "old_hash", "new_hash", "error"}."""
    today = _today_str(today)
    # Single read (no TOCTOU): parse the marker from the SAME text we rewrite.
    try:
        text = Path(pointer_file).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {"ok": False, "error": f"cannot read pointer: {e}"}
    marker = parse_marker(text)
    if not marker:
        return {"ok": False, "error": "no valid freshness-check marker found"}
    new_hash = normalize_and_hash(marker["canonical"])
    if new_hash is None:
        return {"ok": False, "error": f"canonical unreadable: {marker['canonical']}"}
    new_text = text.replace(f'sha256="{marker["sha256"]}"', f'sha256="{new_hash}"')
    new_text = new_text.replace(f'verified="{marker["verified"]}"', f'verified="{today}"')
    new_text = _LAST_VERIFIED_RE.sub(lambda mm: mm.group(1) + today, new_text)
    tmp = str(pointer_file) + f".tmp.{os.getpid()}"
    try:
        Path(tmp).write_bytes(new_text.encode("utf-8"))
        os.replace(tmp, str(pointer_file))
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return {"ok": False, "old_hash": marker["sha256"], "new_hash": new_hash, "error": str(e)}
    return {"ok": True, "old_hash": marker["sha256"], "new_hash": new_hash, "error": None}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_paths():
    """Resolve WORLD_DIR / AGENT_DIR via _paths (agent-var-agnostic: _paths reads
    the repo-appropriate env var internally). Returns (world_dir, agent_dir)."""
    world_dir = None
    agent_dir = None
    try:
        from _paths import WORLD_DIR  # type: ignore
        world_dir = WORLD_DIR
    except Exception:
        pass
    try:
        from _paths import AGENT_DIR  # type: ignore
        agent_dir = AGENT_DIR
    except Exception:
        pass
    return world_dir, agent_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Report only -- no bumps, no goal filing.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ap.add_argument("--reanchor", metavar="POINTER_MD", default=None,
                    help="Recompute marker sha256 + verified date for one pointer file.")
    ap.add_argument("--world", metavar="DIR", default=None,
                    help="Override WORLD_DIR (default: resolved via _paths).")
    args = ap.parse_args()

    if args.reanchor:
        res = reanchor(args.reanchor)
        if args.json:
            print(json.dumps(res, ensure_ascii=True))
        elif res["ok"]:
            print(f"reanchored {args.reanchor}: {res['old_hash'][:12]} -> {res['new_hash'][:12]}")
        else:
            print(f"reanchor failed: {res['error']}", file=sys.stderr)
        return 0 if res["ok"] else 1

    world_dir, agent_dir = _resolve_paths()
    if args.world:
        world_dir = args.world
    if not world_dir:
        print("pointer_freshness: WORLD_DIR unresolved (set --world or MIND_AGENT).",
              file=sys.stderr)
        return 2

    out = scan(world_dir, agent_dir=agent_dir, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(out, ensure_ascii=True, indent=2))
    else:
        s = out["summary"]
        print(f"pointer-freshness: checked={s['checked']} fresh={s['fresh']} "
              f"bumped={s['bumped']} drift={s['drift']} "
              f"(filed={s['drift_filed']} deduped={s['drift_deduped']}) "
              f"canonical_missing={s['canonical_missing']} errors={s['errors']}"
              f"{' [dry-run]' if args.dry_run else ''}")
        for r in out["results"]:
            if r["status"] != "fresh":
                tail = f" goal={r['goal_id']}" if r.get("goal_id") else ""
                err = f" err={r['error']}" if r.get("error") else ""
                print(f"  [{r['status']}] {r['slug']} (age={r['age_days']}d/"
                      f"{r['max_age_days']}){tail}{err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
