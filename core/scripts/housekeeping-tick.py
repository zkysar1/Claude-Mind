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
  B  harness scratchpad GC (<system-temp>/claude-<uid>, else the older
     <system-temp>/claude — the surface measured
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
  D  harness transcript archive — copies BOTH harness transcript trees to the
     storage backend (core/scripts/transcript_archive.py). NOT gated by
     `shadow`: shadow arms DELETERS, and this lane only copies (see the
     docstring on run_lane_d). Self-throttled on its own sub-stamp at
     transcript_archive_interval_hours (default 12 — the owner's 1-2x/day);
     set that key to 0 to turn the archiver off.

One JSONL record per EXECUTED tick → core/logs/housekeeping-<agent>.jsonl
(not-due ticks write nothing). Self-gating via
<agent>/session/housekeeping-tick-state.json (monitor-tick's sibling file)
when bound, and via core/logs/housekeeping-tick-state-unbound.json — one
BOX-LOCAL stamp, per machine — when AGENT_DIR is unset. The unbound branch
is not a nicety: until 2026-09-17 it was `return None`, which made every
interval gate in this file disengage on the unbound path (g-358-179).
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
import socket
import subprocess
import sys
# NOT dead, despite no call site since  routed Lane B's base to
# _node_tmpdir(): test_housekeeping_tick.py patches `HK.tempfile` (the
# gettempdir tripwire in _default_root_env) and loads a mutated copy of this
# file whose pre-fix form calls tempfile.gettempdir(). Dropping this import
# fails those tests with AttributeError/NameError, not with a clean signal.
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
from _fresh_read import read_text_for_membership

ORIGIN_SIGNAL = "investigate:housekeeping-tick"
SKIP_STREAK_ORIGIN_SIGNAL = "investigate:housekeeping-tick-skip-streak"
MIND_SEED_STALE_ORIGIN_SIGNAL = "investigate:housekeeping-tick-mind-seed-stale"
# The string-valued keys a lane uses to say it did NOT do its work, and why.
SKIP_REASON_KEYS = ("skipped", "sessions_skipped")
DEDUP_HOURS = 48
# An allow-list, so an unknown status files rather than suppresses (rb-4350).
OPEN_STATUSES = ("pending", "in-progress", "blocked")
LANE_A_TIMEOUT = int(os.environ.get("HK_LANE_A_TIMEOUT") or 300)
LANE_D_TIMEOUT = int(os.environ.get("HK_LANE_D_TIMEOUT") or 1800)
LANE_E_TIMEOUT = int(os.environ.get("HK_LANE_E_TIMEOUT") or 120)

DEFAULTS = {
    "interval_hours": 6,
    "shadow": True,
    "scratch_session_age_days": 14,
    "scratch_empty_project_age_days": 30,
    "transcript_archive_interval_hours": 12,
    # Lane E. 24h against the detector's own 72h staleness threshold, so a
    # freeze is caught inside one window rather than at its edge ().
    # 0 disables the lane, the same off-switch shape lane D uses.
    "mind_seed_freshness_interval_hours": 24,
    # A lane whose skip reason is identical on this many consecutive records
    # is flagged (). 0 disables.
    "skip_streak_ticks": 20,
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


def _state_path() -> Path:
    env = os.environ.get("HK_STATE_PATH")
    if env:
        return Path(env)
    if AGENT_DIR:
        return Path(AGENT_DIR) / "session" / "housekeeping-tick-state.json"
    # UNBOUND TICK — a BOX-LOCAL stamp, never None. Returning None here made
    # load_state() answer {} and save_state() a silent no-op, so EVERY interval
    # gate this file owns disengaged on the unbound path at once — THREE, not
    # the two the filing goal named: do_tick's `interval_hours`, Lane D's
    # `transcript_archive_interval_hours`, and Lane E's
    # `mind_seed_freshness_interval_hours` (measured on cc-04 closing
    #  — the post-fix stamp carries all three keys). Nothing errored:
    # an ungated tick is byte-identical to a due one, which is why a
    # gate-by-gate audit would have missed it and a stamp-file read finds it
    # in one look. Measured on cc-05 over 2026-09-16T11:42..09-17T11:42:
    # 57 executed unbound ticks, lane_d on all 57, re-PUTting the one growing
    # transcript for 8,109,400,693 bytes; the BOUND path on the SAME box ran
    # lane_d twice and returned not-due twice. sessionstart-orchestrator.sh
    # Step 2.7 spawns exactly this shape and its own comment claims the 6h
    # self-gating that this line withheld.
    #
    # PER-MACHINE, and that is the correct scope rather than a convenience:
    # Lane D archives transcripts/<machine>/, so its effect is a property of
    # the BOX, while even the bound stamp above is per-AGENT — an N-agent box
    # still archives up to N times per interval (guard-2585, guard-6523). The
    # unbound lane is the one place the stamp can match the effect exactly.
    # core/logs/ is gitignored and outside every governed root, so this stays
    # box-local and is never synced (guard-599 — persistent state does not
    # belong under an ephemeral agent dir either).
    #
    # Deliberately NOT resolved through the agent fall-through: on a
    # multi-agent box that would bind an unbound tick to whichever agent dir
    # sorts first, silently (guard-4048). Absolute by construction via
    # SCRIPT_DIR (guard-552); the filename names the fallback so it can never
    # be misread as the bound file (guard-2586). Mirrors _log_path()'s
    # `housekeeping-unbound.jsonl` so the stamp sits beside the log it gates.
    return SCRIPT_DIR.parent / "logs" / "housekeeping-tick-state-unbound.json"


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
                # BOTH copies when they diverge (), not the store alone
                # (). A peer's mirror can lag the store with no refresh
                # able to move it, so a SID cited only in the missing tail reads
                # as uncited and the dir is deleted. But experience.jsonl is
                # REWRITTEN by the store, not only appended, so the mirror can
                # equally hold lines the store lacks — store-wins dropped those.
                # This blob is consumed by `x in blob`, where duplicate text is
                # inert and a missing line is destructive, so the union is the
                # safe read here. Counting readers must NOT use it; see
                # _fresh_read.read_text_for_membership.
                try:
                    chunks.append(read_text_for_membership(conf, label="housekeeping-tick"))
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


def _node_tmpdir() -> str:
    """The OS temp dir the way Node's `os.tmpdir()` resolves it ().

    NOT `tempfile.gettempdir()`, and the difference is not cosmetic. Lane B
    sweeps a root the HARNESS created, and the harness is Node -- so the only
    correct base is the one Node would have picked. Python's resolver differs
    from Node's on BOTH axes that matter here (probed on cc-05, bravo, fresh-eyes
    msg-20260917-062455-bravo-5481):

      ORDER    -- with TMPDIR unset and TEMP and TMP both set, Python takes TEMP
                  and Node takes TMP. Python's list is TMPDIR, TEMP, TMP; Node's
                  is TMPDIR, TMP, TEMP. The middle two are SWAPPED.
      WRITABILITY -- `gettempdir()` probes each candidate and silently falls
                  through to /tmp when it cannot write there. Node performs NO
                  such check and keeps an unwritable TMPDIR. So on a box with an
                  unwritable TMPDIR the two resolvers disagree even though the
                  env is unambiguous.

    On such a box Lane B looked under a root the harness never created and
    returned `skipped: no-scratch-root` -- the silent-skip class g-358-143
    closed, reopened one layer down. It fails SAFE (a wrong root is missing, not
    mistakenly deleted), which is exactly why nothing surfaced it.

    Deliberately NO writability check, NO `os.path.isdir` filter: the goal is to
    agree with Node, not to pick a usable directory. An "improvement" here that
    skips an unwritable candidate silently restores the bug.

    Order per Node's documented `os.tmpdir()`:
      POSIX   -- TMPDIR, TMP, TEMP, else "/tmp"
      Windows -- TEMP, TMP, else %SystemRoot%\\temp (falling back to %windir%)
    Node strips trailing separators unless the path IS a root, which is why the
    strip below is guarded rather than a bare rstrip.
    """
    if os.name == "nt":
        names = ("TEMP", "TMP")
        fallback = os.path.join(
            os.environ.get("SystemRoot") or os.environ.get("windir") or "C:\\Windows",
            "temp")
    else:
        names = ("TMPDIR", "TMP", "TEMP")
        fallback = "/tmp"
    for name in names:
        val = os.environ.get(name)
        if val:
            return _strip_trailing_sep(val)
    return fallback


def _strip_trailing_sep(path: str) -> str:
    """Drop trailing separators the way Node does -- but never reduce a ROOT to
    the empty string ("/" must stay "/", "C:\\" must stay "C:\\").

    TWO root shapes, and an emptiness test catches only ONE of them. "/" strips
    to "" and the falsy branch restores it; "C:\\" strips to "C:", which is
    TRUTHY, so that branch never fires for the Windows drive root. "C:" is not
    a root -- it is DRIVE-RELATIVE: PureWindowsPath("C:") / "claude-0" is
    "C:claude-0" with is_absolute() False, resolving against the process's
    per-drive working directory instead of the drive root, so Lane B would
    sweep somewhere other than the root it reports. Measured against the
    pre-fix form 2026-09-17 (alpha, cc-04, uname -r 6.8.0-139-generic).

    DELIBERATELY WIDER than Node's win32 rule (`!path.endsWith(':\\')`), which
    matches the BACKSLASH spelling only. Both drive-root spellings normalise
    back to an absolute drive root here -- measured 2026-09-17 (alpha, cc-04,
    uname -r 6.8.0-139-generic), impl vs. a literal transcription of that rule:

        'C:\\'  -> 'C:\\'  (Node agrees)      'C:/'  -> 'C:\\'  (Node: 'C:')
        'C:'    -> 'C:'    (Node agrees)      'C:\\\\' -> 'C:\\'  (Node: 'C:')

    The widening is the POINT, not an oversight: `TEMP=C:/...` is ordinary under
    Git-Bash/MSYS, so the forward-slash drive root reaches _node_tmpdir in
    practice, and the narrower rule would hand back the drive-RELATIVE "C:" --
    the exact defect the paragraph above describes. Do not "fix" this to match
    Node literally; that re-introduces it.

    What IS Node's rule is the untouched bare "C:": a drive letter that carried
    no trailing separator is returned as-is, because Node strips nothing there
    either. That is what the `stripped != path` conjunct buys.
    """
    stripped = path.rstrip("/\\")
    if not stripped:
        return path[:1] or path
    if stripped != path and len(stripped) == 2 and stripped[1] == ":":
        return stripped + "\\"
    return stripped


def run_lane_b(shadow: bool, cfg: dict, scratch_root: Path | None = None,
               cited_blob: str | None = "UNSET",
               now: float | None = None) -> dict:
    """Sweep the harness scratchpad. Never raises."""
    out: dict = {"root": None, "empty_projects_removed": [], "sessions_removed": [],
                 "sessions_kept_cited": [], "sessions_kept_receipt": [],
                 "other_projects_nonempty": 0, "other_projects_bytes": 0,
                 "cited_blob": "ok", "shadow": shadow}
    try:
        if scratch_root or os.environ.get("HK_SCRATCH_ROOT"):
            root = Path(scratch_root or os.environ["HK_SCRATCH_ROOT"])
        else:
            # The harness root is PER-UID: `claude-${process.getuid?.()??0}`
            # under CLAUDE_CODE_TMPDIR, else the OS temp dir, read from the CLI
            # source (); a platform with no getuid gets 0. A bare
            # `claude` root skipped every run on cc-05 (762 of 762) while the
            # per-uid root existed. The old bare name stays as the fallback.
            # Never a `claude-*` glob: another uid's root is not this
            # process's to delete.
            base = os.environ.get("CLAUDE_CODE_TMPDIR") or _node_tmpdir()
            tmp = Path(base)
            uid = os.getuid() if hasattr(os, "getuid") else 0
            candidates = [tmp / f"claude-{uid}", tmp / "claude"]
            existing = [c for c in candidates if c.is_dir()]
            root = existing[0] if existing else candidates[0]
            out["root_candidates"] = [str(c) for c in candidates]
            if len(existing) > 1:
                out["unswept_roots"] = [str(c) for c in existing[1:]]
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


# ── Lane D: harness transcript archive ──────────────────────────────────────

def run_lane_d(cfg: dict, state_path: Path | None = None,
               archive_cmd: list[str] | None = None,
               now: _dt.datetime | None = None) -> dict:
    """Copy both harnesses' transcript trees to the storage backend. Never raises.

    NOT GATED BY `shadow`, AND THAT ASYMMETRY IS DELIBERATE — do not "restore
    lane parity" here. `shadow` is the DELETION arming gate: it makes Lane A
    dry-run a purge and Lane B report instead of removing. Lane D removes
    nothing; it COPIES, and its worst failure mode is an extra object in the
    archive. It ships `true` on every box, so a Lane D behind it would be
    inert everywhere — an archiver that never runs against a harness that
    deletes on a 30-day clock. The off-switch is its own interval key set to
    0, which says "operator turned the archiver off" instead of overloading
    the word that means "the deleters are not armed yet".

    Self-throttled on its OWN sub-stamp in the tick's state file, because the
    tick's 6h cadence is the purge's, not this one's (owner: 1-2x/day).
    Re-reads state immediately before stamping so the window in which it could
    clobber a concurrent tick stamp is milliseconds wide; the worst outcome of
    losing that race is one extra interval, which is why it is not locked.

    A timeout or a failed spawn deliberately does NOT stamp: an unreachable
    backend should retry at the next tick, not wait out the full interval.
    """
    interval = float(cfg.get("transcript_archive_interval_hours") or 0)
    if interval <= 0:
        return {"verdict": "disabled"}
    sp = state_path if state_path is not None else _state_path()
    st = load_state(sp)
    now = now or _dt.datetime.now()
    last = st.get("last_transcript_archive")
    if last:
        parsed = parse_naive_iso(str(last))
        if parsed is not None and (now - parsed).total_seconds() < interval * 3600:
            return {"verdict": "not-due", "last": last,
                    "interval_hours": interval}
    if archive_cmd is None and os.environ.get("PYTEST_CURRENT_TEST"):
        # Same chokepoint idiom the daemon-spawn paths use (): a
        # test that did not ASK for this lane must never shell out to the
        # real archiver, which writes to the PRODUCTION bucket. A test that
        # wants it passes archive_cmd explicitly.
        return {"verdict": "skipped-under-pytest"}
    if archive_cmd is None:
        archive_cmd = [sys.executable, str(SCRIPT_DIR / "transcript_archive.py"),
                       "archive", "--json"]
    try:
        proc = subprocess.run(archive_cmd, capture_output=True, text=True,
                              timeout=LANE_D_TIMEOUT, cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return {"verdict": "timeout", "timeout_s": LANE_D_TIMEOUT}
    except Exception as exc:
        return {"verdict": "spawn-error", "error": str(exc)[:200]}
    try:
        r = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        # rc is NOT the discriminator: the archiver exits 1 on partial failure
        # and still prints a receipt, so an unparseable stdout is the only
        # shape that leaves us with no measurement at all.
        return {"verdict": "unparseable", "rc": proc.returncode,
                "stderr": (proc.stderr or "").strip()[-400:],
                "head": (proc.stdout or "")[:200]}
    out = {k: r.get(k) for k in (
        "destination", "machine", "live_files", "live_bytes", "archived_count",
        "archived_bytes", "unchanged_skipped", "failed_count",
        "newly_deleted_detected", "index_total_entries", "by_harness")}
    out["failures"] = (r.get("failures") or [])[:10]
    out["newly_deleted_sample"] = (r.get("newly_deleted_sample") or [])[:10]
    out["verdict"] = "partial" if (r.get("failed_count") or 0) else "ok"
    # A receipt exists ⇒ the attempt was MEASURED, so stamp even on partial.
    if sp is None:
        out["stamp"] = "unavailable"        # same exposure the tick itself has
    else:
        fresh = load_state(sp)
        fresh["last_transcript_archive"] = now.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(sp, fresh)
    return out

def run_lane_e(cfg: dict, state_path: Path | None = None,
               check_cmd: list[str] | None = None,
               now: _dt.datetime | None = None) -> dict:
    """Mind-seed publish-key freshness — a SILENCE detector. Never raises.

    WHY IT LIVES HERE (g-357-104). The Claude-Mind publish lane alerts only on
    a FAILED run, so it cannot fire when NO RUN HAPPENS — and that is the
    failure that occurred: the lane was deleted 2026-08-23, main took 100
    commits with zero publishes, and every live customer environment froze on
    the 2026-08-22 artifact for ~12 days with no alert at any point. A human
    reading a code comment found it. The orphan-sweep CAUSE is fixed; the
    DETECTION gap was not, and a disabled workflow, revoked OIDC trust, a
    stale paths: filter or a branch rename all reproduce it identically.

    The detector itself already existed as a verified script with an exit-code
    contract and had no caller — which is exactly what guard-3570 / rb-4335
    mean when they say authoring knowledge does not install it. This is the
    caller.

    NOT A NEW RECURRING GOAL, deliberately: operator-offload-gate (gh-005)
    refuses one for work that is deterministic + clocked + checkable, and this
    is all three. A lane on an existing interval-gated sweep costs no
    per-cycle LLM iteration, so the gate never fires and nothing is overridden
    to get past it. The goal's own wording allows it: "a recurring goal OR AN
    EXISTING SWEEP". An Ayoai-Operator scheduled job stays the better END
    state (token cost scales with EVENTS, not frequency); this is the smallest
    thing that closes the detection gap now.

    PER-BOX AND REDUNDANT ON PURPOSE. The publish key is one GLOBAL artifact,
    so N boxes checking it is N times the work for one fact — but the work is
    a single object-head per box per day, and the redundancy is a FEATURE for
    a detector: if one box's credentials break, the others still see the
    freeze. A fleet-wide-once design would put the detector behind the same
    single point of failure it is watching.

    NOT GATED BY `shadow` — same reasoning as lane D. `shadow` arms DELETERS;
    this lane reads one object's timestamp and removes nothing, so a lane E
    behind it would be inert on every box, which is a detector that never
    runs. Its off-switch is its own interval key set to 0.

    rc=3 (unreachable) IS NOT rc=1 (STALE) and must never be folded into it:
    an unreadable probe is ZERO signals, not one (verify-before-assuming rule
    4), so collapsing them would report a customer-facing freeze on every
    credentials or network blip. Only rc=1 is an alert; rc=3 is recorded and
    warned, and deliberately does NOT stamp so the next tick retries instead
    of waiting out the full interval.
    """
    interval = float(cfg.get("mind_seed_freshness_interval_hours") or 0)
    if interval <= 0:
        return {"verdict": "disabled"}
    sp = state_path if state_path is not None else _state_path()
    st = load_state(sp)
    now = now or _dt.datetime.now()
    last = st.get("last_mind_seed_freshness")
    if last:
        parsed = parse_naive_iso(str(last))
        if parsed is not None and (now - parsed).total_seconds() < interval * 3600:
            return {"verdict": "not-due", "last": last,
                    "interval_hours": interval}
    if check_cmd is None and os.environ.get("PYTEST_CURRENT_TEST"):
        # Same chokepoint idiom as lane D (): a test that did not ASK
        # for this lane must never shell out to the real check, which makes a
        # live AWS call. A test that wants it passes check_cmd explicitly.
        return {"verdict": "skipped-under-pytest"}
    if check_cmd is None:
        # WORLD_DIR from _paths, NEVER os.environ["WORLD_PATH"]: that var is set
        # by _paths.sh for SHELL callers and is absent in a Python child, so
        # reading it returned "no-world-path" on a healthy box (measured before
        # this line existed — the unit tests never caught it because they inject
        # check_cmd and skip this branch entirely). Re-implementing the
        # env/conf/fallback chain inline is also what path-resolution.md
        # forbids; _paths owns it, and this module already imports it.
        if not WORLD_DIR:
            # world/ is an EXTERNAL path; a bare relative "world/scripts/..."
            # dies rc=127 in a way that reads exactly like a dead backend
            # (probe-with-canonical-code-path.md), so refuse to guess.
            return {"verdict": "no-world-path"}
        script = Path(WORLD_DIR) / "scripts" / "mind-seed-freshness-check.sh"
        if not script.exists():
            return {"verdict": "detector-absent", "expected": str(script)}
        from _runtime_bash import bash_cmd                  # guard-580
        check_cmd = bash_cmd(str(script), "--json")
    try:
        proc = subprocess.run(check_cmd, capture_output=True, text=True,
                              timeout=LANE_E_TIMEOUT, cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return {"verdict": "timeout", "timeout_s": LANE_E_TIMEOUT}
    except Exception as exc:
        return {"verdict": "spawn-error", "error": str(exc)[:200]}
    try:
        r = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {"verdict": "unparseable", "rc": proc.returncode,
                "stderr": (proc.stderr or "").strip()[-400:],
                "head": (proc.stdout or "")[:200]}
    # The content_* keys are carried through DELIBERATELY ( F5). The
    # detector gained a content assertion beside its age assertion, and this
    # tuple is a fixed allow-list: a key the script emits and this line does not
    # name is dropped in silence, so the new verdict would never reach the lane
    # record and nothing would be able to see it (the F6 lesson from that same
    # goal — the consumer edit is part of the fix, not a follow-up).
    # content_sha is included because a sha frozen across many ticks is exactly
    # the F5 defect made visible; content_note carries the reason an unverified
    # verdict was reached, which is the difference between "content is fine" and
    # "nothing was compared".
    out = {k: r.get(k) for k in (
        "age_hours", "threshold_hours", "last_modified", "bucket", "key",
        "content_verdict", "content_sha", "content_entries", "content_note",
        # : why an aged key is or is not stale (no = the source has not
        # moved since the served sha; yes = a publish was due and did not land).
        "publish_due", "source_head")}
    out["rc"] = proc.returncode
    # The SCRIPT's exit code is the contract, not its verdict string — the
    # string is for humans and the code is what this lane branches on.
    if proc.returncode == 0:
        out["verdict"] = "ok"
    elif proc.returncode == 1:
        out["verdict"] = "stale"
    elif proc.returncode == 3:
        out["verdict"] = "unreachable"
    else:
        out["verdict"] = "unknown-rc"
    # NO content branch here, deliberately: the script folds a content mismatch
    # into rc=1 rather than inventing a fourth code (its header explains why), so
    # the mapping above already covers it. And an `unverified` content probe on an
    # rc=0 run still STAMPS — the age question was measured and answered; the
    # content probe merely declined to guess.
    if sp is not None and out["verdict"] in ("ok", "stale"):
        # Stamp only on a MEASURED answer. unreachable/timeout/spawn-error left
        # unstamped so the next tick retries rather than waiting the interval.
        fresh = load_state(sp)
        fresh["last_mind_seed_freshness"] = now.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(sp, fresh)
    elif sp is None:
        out["stamp"] = "unavailable"        # same exposure the tick itself has
    return out


# ── record + Investigate ────────────────────────────────────────────────────

def _log_path() -> Path:
    env = os.environ.get("HK_LOG_PATH")
    if env:
        return Path(env)
    agent = os.environ.get("MIND_AGENT") or "unbound"
    return SCRIPT_DIR.parent / "logs" / f"housekeeping-{agent}.jsonl"


def _filer_id() -> str:
    """Who filed this goal, and from WHICH BOX — resolved, never a placeholder.

    An unbound tick (MIND_AGENT unset) is the ordinary case for the cadence
    runner, not an edge case, and it used to render as a self-referential
    placeholder telling the reader to consult the very description that was
    asking them to identify the box. (The retired placeholder token is
    deliberately NOT quoted here: this file is inside the scan surface of the
    test that asserts the token is gone, so quoting it would make this
    docstring a standing violation and force a self-exclusion — guard-1855.)
    On a multi-box fleet the reader then cannot
    tell which machine's ledger to open, and the triage step that says to read
    the ledger "on the filing box" has no referent (g-358-150; first live
    instance g-115-10153, filed by the unbound tick 2026-09-17).
    """
    agent = os.environ.get("MIND_AGENT") or "unbound"
    return f"{agent}@{socket.gethostname()}"


def append_record(record: dict, log_path: Path | None = None) -> None:
    p = log_path or _log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:
        print(f"[housekeeping-tick] record append failed: {exc}", file=sys.stderr)


def _recent_investigate_exists(origin_signal: str = ORIGIN_SIGNAL) -> bool:
    """Deduped-filing gate — cold-snapshot-tick's read_any idiom verbatim:
    fails CLOSED (True = suppress) when the queues cannot actually be READ,
    guard-487; an absent file counts as unread, not as no-duplicate.

    Suppresses while a goal with the origin_signal is still OPEN, or was created
    in the last DEDUP_HOURS (g-358-153). The window alone let a persistent
    condition re-file every 48h beside its open twin; the add-goal duplication
    gate refused that attempt (measured rc=1), so every later tick paid a daemon
    round trip to record a failure. Closing the goal releases the suppression
    (guard-3419), so a condition that outlives its goal can still re-file.
    Lane E rides the same predicate (g-358-151): a frozen seed stays stale on
    every tick, so its open goal must suppress at any age. One predicate, not a
    per-caller flag (reconciled g-358-156)."""
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
            if origin_signal not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in (rec.get("goals") or []):
                if (g.get("origin_signal") or "") != origin_signal:
                    continue
                if g.get("status") in OPEN_STATUSES:
                    return True
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
    filer = _filer_id()
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
            f"record in {_log_path()} on {socket.gethostname()}; (2) run "
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
    return _add_goal(payload)


def file_mind_seed_stale_investigate(reason: str, detail: str) -> dict:
    """ONE deduped Investigate for a MEASURED lane E stale verdict ().

    On its OWN origin signal: the shared ORIGIN_SIGNAL let a lane A Investigate
    suppress this one for 48h (and the reverse). Dedups on an open goal at any
    age too (the shared OPEN_STATUSES predicate), because a frozen seed stays stale on every tick."""
    if _recent_investigate_exists(MIND_SEED_STALE_ORIGIN_SIGNAL):
        return {"filed": False, "suppressed": "recent-or-open-duplicate"}
    filer = _filer_id()
    payload = {
        "title": "Investigate: mind-seed publish key measured STALE with a "
                 "publish due — live environments may be frozen on an old seed",
        "description": (
            f"housekeeping-tick.py lane E ({filer}) measured the published "
            f"mind-seed key STALE ({reason}). Since g-358-151 the detector "
            f"returns stale only when the key is past its age threshold AND the "
            f"source repo's main HEAD is not the served source_sha "
            f"(publish_due=yes) or could not be read (publish_due=unknown), so "
            f"this is not an idle staging branch. Lane E record: {detail}\n\n"
            f"Triage: (1) run the detector by hand and read its PUBLISH-DUE "
            f"line; (2) check that the publish workflow is enabled, its paths: "
            f"filter still matches, its OIDC trust is intact and its branch was "
            f"not renamed (world convention mind-seed-rollout.md); (3) a publish "
            f"that is due and did not run is the silent-freeze class — the "
            f"workflow's own failure alert cannot fire on it."
        ),
        "priority": "HIGH",
        "participants": ["agent"],
        "category": "infrastructure",
        "origin_signal": MIND_SEED_STALE_ORIGIN_SIGNAL,
        "work_class": "framework",
        "intended_agent": "either",
        "tags": ["housekeeping-tick", "mind-seed", "silence-detector"],
    }
    return _add_goal(payload)


def _add_goal(payload: dict) -> dict:
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


def _tail_records(path: Path, n: int) -> list[dict]:
    """The parseable records among the ledger's last `n` lines, oldest first. A
    malformed line is dropped, not back-filled from older lines, so a torn record
    shortens the window and a streak check fails quiet. Reads from the end, so
    the cost stays flat as the unrotated ledger grows."""
    if n <= 0:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos, buf = fh.tell(), b""
            while pos > 0 and buf.count(b"\n") <= n:
                step = min(65536, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
    except OSError:
        return []
    records = []
    for line in buf.decode("utf-8", errors="replace").splitlines()[-(n + 1):]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue                    # also drops a partial first line
        if isinstance(rec, dict):
            records.append(rec)
    return records[-n:]


def _skip_reasons(record: dict) -> dict:
    return {(lane, key): val
            for lane, body in record.items()
            if lane.startswith("lane_") and isinstance(body, dict)
            for key in SKIP_REASON_KEYS
            if isinstance((val := body.get(key)), str) and val}


def skip_streaks(record: dict, history: list[dict], ticks: int) -> list[dict]:
    """Lanes whose skip reason in `record` is identical on the `ticks - 1`
    records before it (g-358-148).

    Every skipped tick writes a correct record, so the ledger alone cannot tell
    a lane that never runs from a quiet one: Lane B skipped on 762 of 762 runs
    over 27 days and nothing noticed (g-358-143). Only lane-body skip keys are
    read, so this detector's own fields in the record can never count as a skip
    (guard-2316)."""
    if ticks <= 0 or len(history) < ticks - 1:
        return []
    window = history[len(history) - (ticks - 1):]
    return [{"lane": lane, "field": key, "reason": reason, "ticks": ticks}
            for (lane, key), reason in sorted(_skip_reasons(record).items())
            if all(_skip_reasons(r).get((lane, key)) == reason for r in window)]


def file_skip_streak_investigate(reason: str, detail: str) -> dict:
    """ONE deduped Investigate for a lane that skips for the same reason every tick."""
    if _recent_investigate_exists(SKIP_STREAK_ORIGIN_SIGNAL):
        return {"filed": False, "suppressed": "recent-duplicate"}
    filer = _filer_id()
    return _add_goal({
        "title": f"Investigate: housekeeping tick lane skipped for the same "
                 f"reason on every recent tick ({reason}) — the lane is not running",
        "description": (
            f"housekeeping-tick.py ({filer}) found a lane whose skip reason was "
            f"identical on consecutive ledger records. Detail: {detail}\n\n"
            f"A skipped tick records correctly, so this reads like a quiet lane "
            f"while the lane does no work at all (g-358-143: Lane B skipped 762 "
            f"of 762 runs over 27 days). Triage: (1) read the last records in "
            f"{_log_path()} on the filing box {socket.gethostname()}; (2) find "
            f"where the named lane sets that reason in housekeeping-tick.py and "
            f"re-check its precondition by hand on that box."
        ),
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "infrastructure",
        "origin_signal": SKIP_STREAK_ORIGIN_SIGNAL,
        "work_class": "framework",
        "intended_agent": "either",
        "tags": ["housekeeping-tick", "skip-streak"],
    })


# ── entry points ────────────────────────────────────────────────────────────

def do_run(cfg: dict, source: str, investigate_fn=None,
           purge_cmd: list[str] | None = None,
           scratch_root: Path | None = None,
           log_path: Path | None = None,
           archive_cmd: list[str] | None = None) -> dict:
    """Execute the lanes synchronously and append one record."""
    shadow = bool(cfg.get("shadow", True))
    started = time.time()
    if purge_cmd is None and not os.environ.get("MIND_AGENT"):
        # temp-drain-purge.sh purges ONE agent's temp/ and refuses with no
        # AGENT_DIR, so an unbound tick can never run it: 819 of 819 unbound
        # records were purge-error (). Not a skip key on purpose — the
        # lane is inapplicable here by design, and the skip-streak detector
        # must not flag it.
        lane_a = {"verdict": "not-applicable-unbound",
                  "note": "no bound agent, so no temp/ to purge"}
    else:
        lane_a = run_lane_a(shadow, purge_cmd=purge_cmd)
    lane_b = run_lane_b(shadow, cfg, scratch_root=scratch_root)
    lane_c = run_lane_c()
    lane_d = run_lane_d(cfg, archive_cmd=archive_cmd)
    lane_e = run_lane_e(cfg)
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
        "lane_d": lane_d,
        "lane_e": lane_e,
        "duration_s": round(time.time() - started, 2),
    }
    if lane_d.get("verdict") in ("spawn-error", "timeout", "unparseable"):
        # Reported on its own channel, NOT folded into the top-level verdict:
        # that field drives lane A's WARN + Investigate path, whose message
        # names lane A specifically.
        #
        # The verdict tuple is EXHAUSTIVE against run_lane_d's failure returns
        # and must be re-derived, never guessed, if that function grows a new
        # one: this hunk arrived from a carrier ref testing ("error", "failed",
        # "partial") — none of which run_lane_d emits — so it merged clean,
        # compiled clean, passed tests, and was INERT (, sig-29).
        print(f"[housekeeping-tick] lane D verdict={lane_d.get('verdict')} — "
              f"transcripts NOT fully archived this tick "
              f"({lane_d.get('error') or lane_d.get('failed_count')})",
              file=sys.stderr)
    if verdict not in ("ok", "not-applicable-unbound"):
        print(f"[housekeeping-tick] WARN — lane A verdict={verdict}; this run "
              f"is UNMEASURED, not clean", file=sys.stderr)
        if not shadow:
            fi = investigate_fn if investigate_fn is not None else file_investigate
            record["investigate"] = fi(verdict, json.dumps(lane_a)[:400])
    if lane_d.get("verdict") not in ("ok", "not-due", "disabled",
                                    "skipped-under-pytest"):
        print(f"[housekeeping-tick] WARN — lane D verdict="
              f"{lane_d.get('verdict')}; transcripts NOT archived this run",
              file=sys.stderr)
    # Lane E, on its OWN channel and NOT folded into the top-level verdict —
    # that field drives lane A's WARN + Investigate path, whose message names
    # lane A specifically. Two branches, because the whole point of the lane is
    # that they are different findings:
    #   stale  = a MEASURED customer-facing freeze -> Investigate, shadow or not
    #   others = the probe could not answer -> WARN, no Investigate. Filing on
    #            an unreadable probe would file on every creds/network blip.
    lane_e_verdict = lane_e.get("verdict")
    if lane_e_verdict == "stale":
        print(f"[housekeeping-tick] WARN — lane E: mind-seed publish key is "
              f"STALE (age_hours={lane_e.get('age_hours')}, threshold="
              f"{lane_e.get('threshold_hours')}, publish_due="
              f"{lane_e.get('publish_due')}). Live customer environments may be "
              f"frozen on an old artifact and the publish lane's own failure "
              f"alert CANNOT fire on silence.", file=sys.stderr)
        # FILES IN SHADOW TOO (), like skip streaks (): shadow
        # arms DELETERS and filing deletes nothing. Under the old `if not shadow`
        # this alarm could file nowhere, because every box runs shadow, and the
        # WARN above is invisible when --tick spawns this run with stderr sent to
        # DEVNULL. It measured stale on 24 of 30 cc-02 ticks and filed nothing.
        # Its own origin signal, so lane A's 48h dedup no longer swallows it.
        fi = (investigate_fn if investigate_fn is not None
              else file_mind_seed_stale_investigate)
        record["investigate_lane_e"] = fi(
            "mind-seed-publish-stale", json.dumps(lane_e)[:400])
    elif lane_e_verdict not in ("ok", "not-due", "disabled",
                                "skipped-under-pytest"):
        print(f"[housekeeping-tick] WARN — lane E verdict={lane_e_verdict}; "
              f"mind-seed freshness UNMEASURED this run (not a clean result)",
              file=sys.stderr)
    # Skip streaks (). Files in SHADOW mode too, unlike lanes A and E:
    # shadow gates DELETERS and filing deletes nothing, and a lane that skips
    # every shadow tick leaves the soak with no evidence to arm from. The WARN
    # is invisible when --tick spawns this run (stdout/stderr to DEVNULL), so the
    # record field and the filed goal are what surface it.
    ticks = int(cfg.get("skip_streak_ticks") or 0)
    streaks = skip_streaks(record, _tail_records(log_path or _log_path(), ticks - 1),
                           ticks)
    if streaks:
        record["skip_streaks"] = streaks
        summary = ", ".join(f"{s['lane']}.{s['field']}={s['reason']}" for s in streaks)
        print(f"[housekeeping-tick] WARN — skipped for the same reason on "
              f"{ticks} consecutive ticks: {summary}", file=sys.stderr)
        fi = investigate_fn if investigate_fn is not None else file_skip_streak_investigate
        record["investigate_skip_streak"] = fi(summary, json.dumps(streaks)[:400])
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
