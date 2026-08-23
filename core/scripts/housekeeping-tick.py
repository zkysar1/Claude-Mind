#!/usr/bin/env python3
"""housekeeping-tick.py — per-box cadence tick for the temp-store purge +
harness-scratchpad GC (P1 of the 2026-08-21 cleanup plan; user directive:
"we need to add clean up of both temp and scratch pad").

WHAT MOVED, AND WHAT DID NOT (cold-snapshot-tick precedent, g-115-5279): this
moves the mechanical purge's TRIGGER off the goal scorer — which ranks
janitorial work last BY DESIGN, the measured root cause of the whole backlog
(g-115-3319: the open drain goal ranked #120 of 151 for weeks while pressure
grew 18x; 14 drains completed Jun-Jul, then the lane starved). Nothing about
WHAT deletes changed: `temp-drain-purge.sh` and its six guards, four lanes,
citation exemption and third-class watermark are untouched — only the thing
that decides WHEN it runs. The LLM drain (/drain-temp) deliberately stays
goal-driven: encoding needs a mind; deleting enumerated ephemera does not.

PER-BOX + PER-BOUND-AGENT, deliberately. Read the cadence comment on the
iteration-close siblings before copying either pattern: cold-snapshot's stamp
is fleet-shared because N snapshots waste N-1 uploads; a temp store is the
OPPOSITE — it lives on the box that runs its agent, so a world-scoped
recurring goal (claimed once fleet-wide per firing) can never keep every box
clean. Same reasoning as agent-watchdog / monitor-tick.

SHADOW MODE (the arming gate). `housekeeping_tick.shadow: true` in
core/config/aspirations.yaml ships ON: Lane A runs `--dry-run`, Lane B only
reports, and every tick logs a full would-delete record. Flipping shadow to
false is a deliberate operator action taken AFTER reviewing
core/logs/housekeeping-<agent>.jsonl — never a default.

LANES
  A  `temp-drain-purge.sh` for the BOUND agent (--dry-run under shadow).
     `citation_lookup != "ok"` ⇒ verdict DEGRADED — recorded, WARNed, and in
     armed mode ONE deduped Investigate filed. A degraded run NEVER records as
     clean: the purge's own header says a low would_purge under a failed
     lookup is "unmeasured, not clean" (guard-2298 silent-zero class), and a
     tick that logged it as ok would be exactly that class with a cadence.
  B  harness scratchpad GC (<system-temp>/claude — the surface measured
     2026-08-21 at 2,192 project dirs, oldest 2025-12-17, no cleaner at any
     horizon). Three sub-passes, risk-graded:
       (1) recursively-EMPTY project dirs idle past
           scratch_empty_project_age_days → remove (zero content, zero loss);
       (2) THIS project's session dirs whose entire tree is idle past
           scratch_session_age_days → remove, UNLESS a top-level RECEIPT.*
           (temp-drain-purge Lane 3's preservation idiom) or the SID appears
           in a durable store (the temp-citation-ratchet protection extended
           to the one deletion surface it never covered — without this, Lane B
           would be a deletion lane with no citation guard). A LIVE session
           can never match: its tree always carries fresh mtimes.
       (3) other projects' NON-empty dirs: REPORT-ONLY — their layout is not
           this framework's to judge.
     Citation blob unreadable ⇒ sub-pass (2) SKIPS entirely (fail-closed,
     mirroring Lane 2's cited-set-unknown policy in the purge).
  C  report-only recursive census of every agents/*/temp present locally —
     the telemetry the depth-1 pressure metric cannot see (g-115-3773: the
     signal saw 6.6% of alpha's store), gathered BEFORE that metric is
     redesigned (P3) so the redesign starts from measured shapes. Census only:
     Lane A purges ONLY the bound agent; another agent's local tree may be a
     stale mirror of a store whose real home is another box (guard-980).

One JSONL record per EXECUTED tick → core/logs/housekeeping-<agent>.jsonl
(not-due ticks write nothing). Self-gating via
<agent>/session/housekeeping-tick-state.json (monitor-tick's sibling file).
Two callers, one interval: iteration-close productivity-check (autonomous
boxes) and sessionstart-orchestrator (assistant boxes — this box's whole
backlog accrued because it never reaches iteration-close). A lost stamp race
between simultaneous session starts double-runs an idempotent dry-run/find —
harmless, accepted, documented.

FAIL-OPEN at every layer; the CLI always exits 0. Guards honored: guard-580
(bash via _runtime_bash.bash_cmd), guard-420 (tolerant timestamp parse),
guard-487/read_any (dedup fails CLOSED when queues unreadable), guard-1039
(tests inject every path/runner — see test_housekeeping_tick.py).

Usage:
  --tick [--source X]   decide; if due: stamp state, spawn --run DETACHED,
                        exit immediately (hook/loop callers — never waits)
  --run  [--source X]   execute lanes synchronously + append the record
  --force               with --tick: bypass the interval gate (manual)
  --dry-run             with --tick: report the decision only; no stamp, no spawn
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

PROJECT_ROOT = SCRIPT_DIR.parent.parent

try:
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR, agents_root
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ASP_ID, _ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ASP_SOURCE = _asp_source(ASP_ID, WORLD_DIR, AGENT_DIR)
except Exception:                                    # satellite / test boxes
    AGENT_DIR = WORLD_DIR = None
    agents_root = None
    ASP_ID, _ASP_VIA, ASP_SOURCE = "asp-115", "fallback:import-failed", "world"

from _dt import parse_naive_iso  # shared tolerant naive-ISO parse ()

ORIGIN_SIGNAL = "investigate:housekeeping-tick"
DEDUP_HOURS = 48
LANE_A_TIMEOUT = int(os.environ.get("HK_LANE_A_TIMEOUT") or 300)

DEFAULTS = {
    "interval_hours": 6,
    "shadow": True,
    "scratch_session_age_days": 14,
    "scratch_empty_project_age_days": 30,
}


# ── config / state ──────────────────────────────────────────────────────────

def load_config(config_path: Path | None = None) -> dict | None:
    """The housekeeping_tick block from aspirations.yaml, or None ⇒ inert.

    Inert-if-missing is the natural-gate idiom (monitor-probes precedent,
    guard-348): a promoted box whose config lags its scripts must do nothing
    rather than guess at intervals — and must say so once on stderr.
    """
    p = config_path or (SCRIPT_DIR.parent / "config" / "aspirations.yaml")
    try:
        import yaml
        cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get(
            "housekeeping_tick")
    except Exception as exc:
        print(f"[housekeeping-tick] config unreadable ({exc}) — inert", file=sys.stderr)
        return None
    if not isinstance(cfg, dict):
        print("[housekeeping-tick] aspirations.yaml has no housekeeping_tick "
              "block — inert (natural gate)", file=sys.stderr)
        return None
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if v is not None})
    return merged


def _state_path() -> Path | None:
    env = os.environ.get("HK_STATE_PATH")
    if env:
        return Path(env)
    if AGENT_DIR:
        return Path(AGENT_DIR) / "session" / "housekeeping-tick-state.json"
    return None


def load_state(p: Path | None) -> dict:
    try:
        if p and p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(p: Path | None, state: dict) -> None:
    """Atomic tmp+replace, the monitor-tick save_state idiom."""
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:
        print(f"[housekeeping-tick] state save failed: {exc}", file=sys.stderr)


def is_due(state: dict, interval_hours: float, now: _dt.datetime | None = None) -> bool:
    now = now or _dt.datetime.now()
    last = state.get("last_run")
    if not last:
        return True
    try:
        parsed = parse_naive_iso(str(last))
        if parsed is None:
            return True
        return (now - parsed).total_seconds() >= interval_hours * 3600
    except Exception:
        return True     # unreadable stamp — run rather than wedge forever


# ── Lane A: bound-agent temp purge ──────────────────────────────────────────

def run_lane_a(shadow: bool, purge_cmd: list[str] | None = None) -> dict:
    """Invoke temp-drain-purge.sh; classify its JSON. Never raises."""
    try:
        if purge_cmd is None:
            from _runtime_bash import bash_cmd          # guard-580
            script = (SCRIPT_DIR / "temp-drain-purge.sh").as_posix()
            purge_cmd = bash_cmd(script, *(("--dry-run",) if shadow else ()))
        proc = subprocess.run(purge_cmd, capture_output=True, text=True,
                              timeout=LANE_A_TIMEOUT, cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return {"verdict": "timeout", "timeout_s": LANE_A_TIMEOUT}
    except Exception as exc:
        return {"verdict": "spawn-error", "error": str(exc)[:200]}
    if proc.returncode != 0:
        return {"verdict": "purge-error", "rc": proc.returncode,
                "stderr": (proc.stderr or "").strip()[-400:]}
    try:
        data = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {"verdict": "purge-error", "rc": 0,
                "note": "unparseable stdout", "head": (proc.stdout or "")[:200]}
    keep = {k: data.get(k) for k in (
        "purged", "would_purge", "drained_gc_purged", "drained_gc_would_purge",
        "stray_purged", "stray_would_purge", "stray_preserved_git",
        "unmanaged_dotfiles", "watermark", "watermark_source",
        "citation_lookup", "dry_run", "temp_dir")}
    keep["files"] = (data.get("files") or [])[:50]
    keep["stray_preserved_git_dirs"] = data.get("stray_preserved_git_dirs") or []
    # THE LOAD-BEARING CLASSIFICATION: a failed citation lookup means Lane 1
    # ran degraded and Lane 2 was skipped — the numbers above are UNMEASURED,
    # not clean, and this verdict is what stops them being logged as ok.
    keep["verdict"] = "ok" if keep.get("citation_lookup") == "ok" else "degraded"
    return keep


# ── Lane B: harness scratchpad GC ───────────────────────────────────────────

def _tree_stats(d: Path) -> tuple[int, int, float]:
    """(file_count, total_bytes, max_mtime) over d's whole tree, incl. d."""
    files = 0
    total = 0
    try:
        newest = d.stat().st_mtime
    except OSError:
        newest = 0.0
    for root, dirs, names in os.walk(d, onerror=lambda e: None):
        for n in names:
            fp = os.path.join(root, n)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            files += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
        for sub in dirs:
            try:
                newest = max(newest, os.stat(os.path.join(root, sub)).st_mtime)
            except OSError:
                continue
    return files, total, newest


def _has_top_receipt(d: Path) -> bool:
    """Top-level RECEIPT / RECEIPT.* (case-insensitive) — Lane 3's idiom."""
    try:
        for child in d.iterdir():
            if child.is_file() and re.match(r"(?i)^receipt(\.[^.]+)?$", child.name):
                return True
    except OSError:
        pass
    return False


def build_cited_blob(world_dir=None, agents_root_fn=None) -> str | None:
    """Concatenated text of the durable stores Lane B checks SIDs against.

    None ⇒ NOTHING was readable ⇒ the caller must SKIP session-dir deletion
    (fail-closed — 'unknown' and 'nothing cited' must not render identically
    when the consumer deletes on the answer; the purge's Lane 2 policy).
    """
    wd = world_dir if world_dir is not None else WORLD_DIR
    chunks: list[str] = []
    read_any = False
    if wd:
        for name in ("aspirations.jsonl", "reasoning-bank.jsonl", "guardrails.jsonl"):
            try:
                chunks.append((Path(wd) / name).read_text(encoding="utf-8", errors="replace"))
                read_any = True
            except OSError:
                continue
    ar = agents_root_fn if agents_root_fn is not None else agents_root
    if ar is not None:
        try:
            for conf in sorted(Path(ar()).glob("*/experience.jsonl")):
                try:
                    chunks.append(conf.read_text(encoding="utf-8", errors="replace"))
                    read_any = True
                except OSError:
                    continue
        except Exception:
            pass
    return "".join(chunks) if read_any else None


def project_slug(root: Path | None = None) -> str:
    """This project's scratchpad dir name: each [:\\/] char → '-'.

    Verified against the live layout (a Windows drive path C:\\a\\b maps to
    C--a-b, the observed scratchpad dir shape; POSIX /home/x/repo maps to
    -home-x-repo). Existence is still checked before use — a slug-scheme
    drift makes Lane B report project-dir-not-found, never guess.
    """
    return re.sub(r"[:\\/]+", lambda m: "-" * len(m.group()), str(root or PROJECT_ROOT))


def run_lane_b(shadow: bool, cfg: dict, scratch_root: Path | None = None,
               cited_blob: str | None = "UNSET",
               now: float | None = None) -> dict:
    """Sweep the harness scratchpad. Never raises."""
    out: dict = {"root": None, "empty_projects_removed": [], "sessions_removed": [],
                 "sessions_kept_cited": [], "sessions_kept_receipt": [],
                 "other_projects_nonempty": 0, "other_projects_bytes": 0,
                 "cited_blob": "ok", "shadow": shadow}
    try:
        root = scratch_root or Path(os.environ.get("HK_SCRATCH_ROOT")
                                    or Path(tempfile.gettempdir()) / "claude")
        out["root"] = str(root)
        if not root.is_dir():
            out["skipped"] = "no-scratch-root"
            return out
        now = now or time.time()
        empty_cutoff = now - float(cfg["scratch_empty_project_age_days"]) * 86400
        sess_cutoff = now - float(cfg["scratch_session_age_days"]) * 86400
        my_dir = root / project_slug()

        # (1) recursively-empty project dirs — zero content, zero loss.
        # The record caps the NAME list (measured 2,192 project dirs on the
        # authoring box, most empty) but the count is always exact and the
        # armed rmtree covers the full set, not the capped slice.
        _empty_names: list[str] = []
        for proj in sorted(root.iterdir()):
            if not proj.is_dir() or proj == my_dir:
                continue
            files, nbytes, newest = _tree_stats(proj)
            if files == 0 and newest < empty_cutoff:
                _empty_names.append(proj.name)
                if not shadow:
                    shutil.rmtree(proj, ignore_errors=True)
            elif files > 0:
                out["other_projects_nonempty"] += 1     # (3) report-only
                out["other_projects_bytes"] += nbytes
        out["empty_projects_removed_count"] = len(_empty_names)
        out["empty_projects_removed"] = _empty_names[:25]

        # (2) this project's session dirs.
        if cited_blob == "UNSET":
            cited_blob = build_cited_blob()
        if my_dir.is_dir():
            if cited_blob is None:
                out["cited_blob"] = "unreadable"
                out["sessions_skipped"] = "cited-blob-unreadable (fail-closed)"
            else:
                # Cap the recorded list (first live shadow run produced ~460
                # entries in one JSONL line); count + bytes stay exact and the
                # armed rmtree fires per-dir here, never off the capped slice.
                _sess_removed: list[dict] = []
                _sess_bytes = 0
                for sd in sorted(my_dir.iterdir()):
                    if not sd.is_dir():
                        continue          # loose top-level files: report-only
                    files, nbytes, newest = _tree_stats(sd)
                    if newest >= sess_cutoff:
                        continue          # fresh tree — live or recent session
                    if _has_top_receipt(sd):
                        out["sessions_kept_receipt"].append(sd.name)
                        continue
                    if sd.name in cited_blob:
                        out["sessions_kept_cited"].append(sd.name)
                        continue
                    _sess_removed.append(
                        {"sid": sd.name, "files": files, "bytes": nbytes})
                    _sess_bytes += nbytes
                    if not shadow:
                        shutil.rmtree(sd, ignore_errors=True)
                out["sessions_removed_count"] = len(_sess_removed)
                out["sessions_removed_bytes"] = _sess_bytes
                out["sessions_removed"] = _sess_removed[:25]
        else:
            out["sessions_skipped"] = "project-dir-not-found"
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out


# ── Lane C: recursive temp census (report-only) ─────────────────────────────

def run_lane_c(agents_root_fn=None) -> list[dict]:
    rows: list[dict] = []
    ar = agents_root_fn if agents_root_fn is not None else agents_root
    if ar is None:
        return rows
    try:
        for adir in sorted(Path(ar()).iterdir()):
            tdir = adir / "temp"
            if not tdir.is_dir():
                continue
            files, nbytes, _ = _tree_stats(tdir)
            try:
                depth1 = sum(1 for c in tdir.iterdir() if c.is_file())
                subdirs = sum(1 for c in tdir.iterdir() if c.is_dir())
            except OSError:
                depth1 = subdirs = -1
            rows.append({"agent": adir.name, "files": files, "bytes": nbytes,
                         "depth1_files": depth1, "subdirs": subdirs})
    except Exception as exc:
        rows.append({"error": str(exc)[:200]})
    return rows


# ── record + Investigate ────────────────────────────────────────────────────

def _log_path() -> Path:
    env = os.environ.get("HK_LOG_PATH")
    if env:
        return Path(env)
    agent = os.environ.get("MIND_AGENT") or "unbound"
    return SCRIPT_DIR.parent / "logs" / f"housekeeping-{agent}.jsonl"


def append_record(record: dict, log_path: Path | None = None) -> None:
    p = log_path or _log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:
        print(f"[housekeeping-tick] record append failed: {exc}", file=sys.stderr)


def _recent_investigate_exists() -> bool:
    """Deduped-filing gate — cold-snapshot-tick's read_any idiom verbatim:
    fails CLOSED (True = suppress) when the queues cannot actually be READ,
    guard-487; an absent file counts as unread, not as no-duplicate."""
    cutoff = _dt.datetime.now() - _dt.timedelta(hours=DEDUP_HOURS)
    if WORLD_DIR is None and AGENT_DIR is None:
        return True
    paths = [Path(p) / "aspirations.jsonl" for p in (WORLD_DIR, AGENT_DIR) if p]
    read_any = False
    for qp in paths:
        try:
            text = qp.read_text(encoding="utf-8", errors="replace")
            read_any = True
        except OSError:
            continue
        for line in text.splitlines():
            if ORIGIN_SIGNAL not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in (rec.get("goals") or []):
                if (g.get("origin_signal") or "") != ORIGIN_SIGNAL:
                    continue
                created = g.get("created_at") or ""
                try:
                    if _dt.datetime.fromisoformat(str(created)) > cutoff:
                        return True
                except (ValueError, TypeError):
                    continue
    return False if read_any else True


def file_investigate(reason: str, detail: str) -> dict:
    """ONE deduped Investigate for a degraded/failed armed run."""
    if _recent_investigate_exists():
        return {"filed": False, "suppressed": "recent-duplicate"}
    filer = os.environ.get("MIND_AGENT") or "<see description>"
    payload = {
        "title": f"Investigate: housekeeping tick reported {reason} — the "
                 f"cadence purge ran unmeasured or not at all",
        "description": (
            f"housekeeping-tick.py ({filer}) executed an ARMED run whose Lane A "
            f"did not report verdict=ok. Detail: {detail}\n\n"
            f"A degraded run means temp-drain-purge.sh could not determine the "
            f"cited set (citation_lookup!=ok): Lane 1 degraded to the legacy "
            f"allow-list and Lane 2 was skipped — its numbers are UNMEASURED, "
            f"not clean, per the purge's own header. Triage: (1) read the last "
            f"record in core/logs/housekeeping-<agent>.jsonl; (2) run "
            f"`python3 core/scripts/temp-citation-ratchet.py --cited-paths` by "
            f"hand and read its stderr; (3) re-run "
            f"`bash core/scripts/temp-drain-purge.sh --dry-run` and check "
            f"citation_lookup in the JSON."
        ),
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "infrastructure",
        "origin_signal": ORIGIN_SIGNAL,
        "work_class": "framework",
        "intended_agent": "either",
        "tags": ["housekeeping-tick", "temp-store", "citation-integrity"],
    }
    try:
        from _runtime_bash import bash_cmd               # guard-580
        script_path = (SCRIPT_DIR / "aspirations-add-goal.sh").as_posix()
        result = subprocess.run(
            bash_cmd(script_path, "--source", ASP_SOURCE, ASP_ID),
            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return {"filed": False, "error": str(exc)[:200]}
    if result.returncode != 0:
        return {"filed": False, "rc": result.returncode,
                "stderr": (result.stderr or "").strip()[:300]}
    return {"filed": True}


# ── entry points ────────────────────────────────────────────────────────────

def do_run(cfg: dict, source: str, investigate_fn=None,
           purge_cmd: list[str] | None = None,
           scratch_root: Path | None = None,
           log_path: Path | None = None) -> dict:
    """Execute the lanes synchronously and append one record."""
    shadow = bool(cfg.get("shadow", True))
    started = time.time()
    lane_a = run_lane_a(shadow, purge_cmd=purge_cmd)
    lane_b = run_lane_b(shadow, cfg, scratch_root=scratch_root)
    lane_c = run_lane_c()
    verdict = lane_a.get("verdict") or "ok"
    record = {
        "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": os.environ.get("MIND_AGENT") or "unbound",
        "source": source,
        "mode": "shadow" if shadow else "armed",
        "verdict": verdict,
        "lane_a": lane_a,
        "lane_b": lane_b,
        "lane_c": lane_c,
        "duration_s": round(time.time() - started, 2),
    }
    if verdict != "ok":
        print(f"[housekeeping-tick] WARN — lane A verdict={verdict}; this run "
              f"is UNMEASURED, not clean", file=sys.stderr)
        if not shadow:
            fi = investigate_fn if investigate_fn is not None else file_investigate
            record["investigate"] = fi(verdict, json.dumps(lane_a)[:400])
    append_record(record, log_path=log_path)
    return record


def do_tick(args, cfg: dict) -> int:
    sp = _state_path()
    state = load_state(sp)
    if not args.force and not is_due(state, float(cfg["interval_hours"])):
        return 0                                        # quiet not-due exit
    if args.dry_run:
        print(json.dumps({"op": "housekeeping-tick", "due": True,
                          "would_spawn": True, "mode":
                          "shadow" if cfg.get("shadow", True) else "armed"}))
        return 0
    # Claim FIRST (stamp), then spawn: a lost race between two simultaneous
    # session starts double-runs an idempotent sweep — harmless; a crash after
    # the stamp just waits one interval. Same trade cold-snapshot made.
    state["last_run"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    state["last_source"] = args.source
    save_state(sp, state)
    kwargs: dict = {
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL, "cwd": str(PROJECT_ROOT),
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "housekeeping-tick.py"),
         "--run", "--source", args.source], **kwargs)
    print(json.dumps({"op": "housekeeping-tick", "spawned": True,
                      "mode": "shadow" if cfg.get("shadow", True) else "armed"}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tick", action="store_true",
                    help="decide + stamp + spawn the run detached")
    ap.add_argument("--run", action="store_true",
                    help="execute the lanes synchronously (spawned by --tick)")
    ap.add_argument("--force", action="store_true",
                    help="with --tick: bypass the interval gate")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --tick: report the decision; stamp/spawn nothing")
    ap.add_argument("--source", default="manual",
                    help="caller tag recorded in state + record")
    args = ap.parse_args()
    try:
        cfg = load_config()
        if cfg is None:
            return 0                                    # inert (natural gate)
        if args.run:
            do_run(cfg, args.source)
            return 0
        return do_tick(args, cfg)
    except Exception as exc:      # fail-open — never abort a hook or the loop
        print(f"[housekeeping-tick] {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
