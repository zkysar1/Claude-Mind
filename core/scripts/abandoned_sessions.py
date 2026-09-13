#!/usr/bin/env python3
"""Close abandoned Body manifests late, on the box that ran them ().

WHY THIS EXISTS. A Body's manifest leaves `active` only through its own stop
hook (body-manifest.close_body_on_genuine), which runs as the turn-ending
session exits. A power-down, an lxc stop or a killed pane never runs that hook,
so the manifest reads `active` with no writer left alive to change it, and the
peer-side stall probe (worker_stall.classify_body) grades every such Body as a
live stall, forever. Measured 2026-09-10: eight such manifests across seven
containers, two of them 8.2 days old. Measured 2026-09-11 from cc-10: 37 of 77
carriers in the store graded `stalled_no_close`.

WHAT IT DOES. For each manifest on this box reading `active` whose session is
provably not running here, it runs the ORDINARY close late, through the
manifest's sole writer (body-manifest.close_body_late): a forked worker is
staged exactly as a genuine close stages it and marked closed-pending-merge; a
Body with no forked WM (reducer, observer) has nothing to stage and is marked
closed-stale. NOTHING IS DELETED — no session dir, no WM, no baseline. A
`parked` Body is never touched (resumable by contract, g-306-291).

THE DEFINITION OF ABANDONED, and why it is not a heartbeat age. Every stale
stamp this framework owns (runner-heartbeat, body-heartbeat, the carrier ts,
the diary) is written at a fixed point in a work cycle, so its age measures
cycle length, not liveness (guard-4180), and absence of a stamp is the designed
steady state of whole populations (g-115-6939). The harness keeps a better
record: Claude Code writes `<config>/sessions/<pid>.json` for every live
process — {pid, sessionId, procStart, pidDomain, startedAt}. That record is
SID-keyed, written by an INDEPENDENT writer (the harness, not the Mind), and
carries the process start time that defeats pid reuse. Session X is abandoned
on this box only when ALL of these hold:

  1. X is not the session running this pass.
  2. The registry is AUTHORITATIVE here: the current session's own entry reads
     live. That is the positive control — a registry this harness does not
     maintain (another harness, an old build, a different config dir) makes
     every absence meaningless, so no close happens at all.
  3. X ran under that same harness and config dir: its transcript exists.
  4. No registry entry for X reads live or unverifiable (entry_liveness).
  5. The copy is this box's: the session dir belongs to this uid, and the
     manifest's machine_id and any local carrier's host name this box. Only
     the box that abandoned a Body may close it (a clone or a restored copy
     carries another box's sessions/ tree, and absence proves nothing there).
  6. X has been quiet for QUIET_MINUTES — its session dir and its transcript.
  7. All of the above still hold against a FRESH registry read taken
     immediately before the write.

Every unreadable input resolves to NOT abandoned. A wrong close is the
unrecoverable direction (worker-loop Phase -0 refuses every later unit and
only a fresh /start reopens); a missed one costs one more phantom until the
next SessionStart. This is also the definition the telemetry reaper's
SID-granular open question (g-115-1364 / g-115-5114) can share.

WIRING. sessionstart-orchestrator.sh Step 2.8 calls `--hook` with the
SessionStart payload on stdin. The hook path does only a cheap local scan; when
there is a candidate it spawns `--run` DETACHED (the housekeeping-tick pattern),
which waits for this session's own registry entry to appear before judging, so
session start never pays for the wait.

CLI:
  python3 core/scripts/abandoned_sessions.py --hook            # SessionStart
  python3 core/scripts/abandoned_sessions.py --run [--current-sid SID] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _session_binding import _SESSIONS_DIRNAME, _agents_root  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parent.parent
_STATE_DIRNAME = "session"  # agent-wide state dir; mirrors body-manifest.py
_MANIFEST_FILENAME = "body-manifest.yaml"

QUIET_MINUTES = 15.0
# A registry entry whose process started more than this long BEFORE the box's
# last start is dead whatever its pid now names. The margin absorbs clock
# granularity between the harness's startedAt and the boot estimate.
BOOT_MARGIN_SECONDS = 60.0
# How long --run waits for this session's own registry entry (the positive
# control) before giving up and closing nothing.
AUTHORITY_WAIT_SECONDS = 20.0

LIVE, DEAD, UNKNOWN = "live", "dead", "unknown"

_BODY_STATE_RE = re.compile(r"^body_state:\s*['\"]?([\w-]+)['\"]?\s*$", re.M)
_MACHINE_ID_RE = re.compile(r"^machine_id:\s*['\"]?([^'\"\n]*)['\"]?\s*$", re.M)


class Proc:
    """Process-table reads, behind a seam the tests replace."""

    def exists(self, pid: int) -> Optional[bool]:
        """True/False when knowable, None when this platform cannot tell."""
        if os.path.isdir("/proc/self"):
            return os.path.isdir(f"/proc/{pid}")
        try:
            import psutil  # type: ignore
            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001 — no psutil: fall through
            pass
        if os.name == "posix":
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return None
        # Windows without psutil: never guess, and never os.kill — on Windows
        # that call TERMINATES the target rather than probing it.
        return None

    def start_ticks(self, pid: int) -> Optional[str]:
        """Linux /proc/<pid>/stat starttime — the value the registry records as
        procStart (verified equal on cc-10, 2026-09-11). None when unreadable."""
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            return raw[raw.rfind(")") + 2:].split()[19]
        except (OSError, IndexError, ValueError):
            return None


def claude_config_dir() -> Path:
    env = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(env).expanduser() if env else Path.home() / ".claude"


def box_boot_epoch() -> Optional[float]:
    """Earliest available estimate of when this box (container) last started.

    The MINIMUM is deliberate: an estimate that is too EARLY only classifies
    fewer registry entries dead (the fail-closed direction), one that is too
    LATE could condemn a process that started after the true boot. Under lxcfs
    both sources report the container start (measured cc-10: 16s apart).
    """
    estimates: List[float] = []
    try:
        import psutil  # type: ignore
        estimates.append(float(psutil.boot_time()))
    except Exception:  # noqa: BLE001
        pass
    try:
        up = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        estimates.append(time.time() - up)
    except (OSError, ValueError, IndexError):
        pass
    return min(estimates) if estimates else None


def this_pid_domain() -> Optional[str]:
    """This process's pid namespace in the registry's pidDomain format
    (`linux:<machine-id>:pid:[<inode>]`), or None where it cannot be derived."""
    try:
        mid = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        ns = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    return f"linux:{mid}:{ns}" if mid and ns else None


def read_registry(config_dir: Path) -> "tuple[List[dict], int]":
    """(entries, unreadable_count). An unreadable file is COUNTED, never
    skipped silently: it could be the one entry that proves a session alive."""
    entries: List[dict] = []
    bad = 0
    d = config_dir / "sessions"
    if not d.is_dir():
        return entries, 0
    for p in sorted(d.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bad += 1
            continue
        if isinstance(doc, dict) and doc.get("sessionId"):
            entries.append(doc)
        else:
            bad += 1
    return entries, bad


def entry_liveness(entry: dict, *, boot_epoch: Optional[float],
                   pid_domain: Optional[str], proc: Proc) -> str:
    """LIVE / DEAD / UNKNOWN for one registry entry. UNKNOWN is treated as live
    by every caller — it exists as its own value only so a held session reports
    WHY it was held."""
    pid = entry.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return UNKNOWN
    started = entry.get("startedAt")
    if (boot_epoch is not None and isinstance(started, (int, float))
            and not isinstance(started, bool)
            and started / 1000.0 < boot_epoch - BOOT_MARGIN_SECONDS):
        return DEAD  # the process began before this box last started
    domain = entry.get("pidDomain")
    if domain and domain != pid_domain:
        # Another pid namespace, or one this platform cannot name: the pid
        # means nothing here, and only the boot rule above could condemn it.
        return UNKNOWN
    exists = proc.exists(pid)
    if exists is None:
        return UNKNOWN
    if not exists:
        return DEAD
    want, have = entry.get("procStart"), proc.start_ticks(pid)
    if want is None or have is None:
        return LIVE  # alive and not disprovable
    return LIVE if str(want) == str(have) else DEAD  # mismatch = pid reused


def sid_liveness(sid: str, entries: List[dict], **kw) -> str:
    """LIVE if any entry for `sid` is live, UNKNOWN if any is unverifiable,
    DEAD otherwise — including when no entry names it at all."""
    verdicts = [entry_liveness(e, **kw) for e in entries
                if str(e.get("sessionId")) == sid]
    if LIVE in verdicts:
        return LIVE
    if UNKNOWN in verdicts:
        return UNKNOWN
    return DEAD


def transcript_mtime(config_dir: Path, sid: str) -> Optional[float]:
    try:
        for p in (config_dir / "projects").glob(f"*/{sid}.jsonl"):
            return p.stat().st_mtime
    except OSError:
        pass
    return None


def newest_mtime(root: Path) -> Optional[float]:
    """Newest file mtime anywhere under `root`; None when any entry cannot be
    read, because an unreadable file could be the newest one."""
    newest: Optional[float] = None
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        m = e.stat(follow_symlinks=False).st_mtime
                        newest = m if newest is None or m > newest else newest
        except OSError:
            return None
    return newest


def active_manifests(project_root: Path) -> List["tuple[str, str, Path, str]"]:
    """(agent, sid, session_dir, manifest_text) for every manifest on this box
    reading `active`. Local-only by construction: sessions/ never syncs."""
    out = []
    root = _agents_root(project_root)
    try:
        agent_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for adir in agent_dirs:
        sroot = adir / _SESSIONS_DIRNAME
        if not sroot.is_dir():
            continue
        for mpath in sorted(sroot.glob(f"*/{_MANIFEST_FILENAME}")):
            try:
                text = mpath.read_text(encoding="utf-8")
            except OSError:
                continue
            m = _BODY_STATE_RE.search(text)
            if m and m.group(1) == "active":
                out.append((adir.name, mpath.parent.name, mpath.parent, text))
    return out


def _load_body_manifest():
    cached = sys.modules.get("body_manifest")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "body_manifest", SCRIPT_DIR / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["body_manifest"] = mod
    spec.loader.exec_module(mod)
    return mod


def _box_identities() -> "tuple[str, set]":
    host = socket.gethostname()
    ids = {host}
    mid = (os.environ.get("MACHINE_ID") or "").strip()
    if mid and mid.lower() != "unknown":
        ids.add(mid)
    return host, ids


def late_close_pass(project_root: Optional[Path] = None, current_sid: str = "", *,
                    dry_run: bool = False, now: Optional[float] = None,
                    config_dir: Optional[Path] = None, proc: Optional[Proc] = None,
                    boot_epoch: Optional[float] = None,
                    pid_domain: Optional[str] = None,
                    registry_reader: Optional[Callable[[], "tuple[List[dict], int]"]] = None,
                    host: Optional[str] = None, machine_ids: Optional[set] = None,
                    uid: Optional[int] = None,
                    authority_wait_seconds: float = 0.0) -> Dict:
    """One pass over this box's active manifests. Never raises; returns a
    summary naming every close and the reason every other candidate was held.

    The keyword seams exist for the hermetic tests; production passes none and
    gets the real registry, process table, boot estimate and identities."""
    pr = project_root or PROJECT_ROOT
    cfg = config_dir or claude_config_dir()
    proc = proc or Proc()
    now = time.time() if now is None else now
    if host is None or machine_ids is None:
        _h, _ids = _box_identities()
        host = _h if host is None else host
        machine_ids = _ids if machine_ids is None else machine_ids
    if uid is None and hasattr(os, "getuid"):
        uid = os.getuid()
    reader = registry_reader or (lambda: read_registry(cfg))
    kw = {"boot_epoch": boot_epoch, "pid_domain": pid_domain, "proc": proc}
    summary: Dict = {"current_sid": current_sid, "dry_run": dry_run,
                     "scanned": 0, "authoritative": False, "authority": None,
                     "closed": [], "held": []}
    try:
        cands = [c for c in active_manifests(pr) if c[1] != current_sid]
        summary["scanned"] = len(cands)
        if not cands:
            # : an empty enumeration is AMBIGUOUS. active_manifests()
            # swallows OSError both on the agents root and on each manifest
            # read, so [] means either "this box is clean" or "the scan was
            # blind". Outcome 1 of this goal is verified by re-running this
            # pass and reading its zero, so a clean box must not report the
            # same authoritative=False a denied box reports (guard-2236: a
            # bare false that conflates "nothing happened" with "could not
            # tell"). authority() is NOT consulted here by design -- it proves
            # the current session is live so we may WRITE, and with zero
            # candidates there is nothing to write.
            # BOUND, deliberately not overclaimed: this re-reads the agents
            # root only. A manifest that is itself unreadable is still skipped
            # silently by active_manifests(), so this attests "the root was
            # readable and held no other active manifest", not "every manifest
            # on this box was read".
            try:
                list(_agents_root(pr).iterdir())
            except OSError as exc:
                summary["authority"] = "agents-root-unreadable:%s" % type(exc).__name__
                return summary
            summary["authoritative"] = True
            summary["authority"] = "ok-nothing-to-close"
            return summary

        def authority() -> "tuple[Optional[List[dict]], str]":
            if not current_sid:
                return None, "no-current-sid"
            entries, bad = reader()
            if bad:
                return None, f"registry-unreadable:{bad}"
            if sid_liveness(current_sid, entries, **kw) != LIVE:
                return None, "current-session-not-live-in-registry"
            return entries, "ok"

        entries, why = authority()
        deadline = time.time() + max(0.0, authority_wait_seconds)
        while entries is None and time.time() < deadline:
            time.sleep(1.0)
            entries, why = authority()
        summary["authority"] = why
        if entries is None:
            summary["held"] = [{"agent": a, "sid": s, "reason": why}
                               for a, s, _d, _t in cands]
            return summary
        summary["authoritative"] = True
        bm = None
        for agent, sid, sdir, text in cands:
            try:
                reason = _hold_reason(agent, sid, sdir, text, pr, cfg, entries,
                                      now, host, machine_ids, uid, kw)
                if reason is None:
                    fresh, why2 = authority()
                    if fresh is None:
                        reason = f"registry-changed:{why2}"
                    elif sid_liveness(sid, fresh, **kw) != DEAD:
                        reason = "registry-changed:session-appeared"
                if reason is not None:
                    summary["held"].append(
                        {"agent": agent, "sid": sid, "reason": reason})
                    continue
                if dry_run:
                    summary["closed"].append(
                        {"agent": agent, "sid": sid, "result": "would-close"})
                    continue
                bm = bm or _load_body_manifest()
                result = bm.close_body_late(sid, agent, pr)
                summary["closed"].append(
                    {"agent": agent, "sid": sid, "result": result})
            except Exception as exc:  # noqa: BLE001 — one bad dir never stops the pass
                summary["held"].append({"agent": agent, "sid": sid,
                                        "reason": f"error:{type(exc).__name__}: {exc}"})
    except Exception as exc:  # noqa: BLE001 — SessionStart must never break
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


def _hold_reason(agent, sid, sdir, text, pr, cfg, entries, now, host,
                 machine_ids, uid, kw) -> Optional[str]:
    """None when every condition for abandoned holds, else the first failure."""
    m = _MACHINE_ID_RE.search(text)
    if m and m.group(1).strip() and m.group(1).strip() not in machine_ids:
        return "foreign-machine"
    carrier = _agents_root(pr) / agent / _STATE_DIRNAME / f"body-heartbeat-{sid}.json"
    if carrier.is_file():
        try:
            doc = json.loads(carrier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "carrier-unreadable"
        c_host = doc.get("host") if isinstance(doc, dict) else None
        if c_host and c_host != host:
            return "foreign-host"
    if uid is not None:
        try:
            if sdir.stat().st_uid != uid:
                return "foreign-owner"
        except OSError:
            return "session-dir-unreadable"
    t_mtime = transcript_mtime(cfg, sid)
    if t_mtime is None:
        return "no-transcript"
    verdict = sid_liveness(sid, entries, **kw)
    if verdict == LIVE:
        return "running"
    if verdict == UNKNOWN:
        return "liveness-unknown"
    newest = newest_mtime(sdir)
    if newest is None:
        return "activity-unreadable"
    if now - max(newest, t_mtime) < QUIET_MINUTES * 60.0:
        return "recent-activity"
    return None


def _hook(stdin_text: str) -> int:
    """SessionStart entry: cheap local scan, detached --run only when needed."""
    try:
        sid = str((json.loads(stdin_text or "{}") or {}).get("session_id") or "")
    except ValueError:
        sid = ""
    sid = sid.strip() or (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        return 0
    if not [c for c in active_manifests(PROJECT_ROOT) if c[1] != sid]:
        return 0
    log_dir = PROJECT_ROOT / "core" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"stdin": subprocess.DEVNULL, "cwd": str(PROJECT_ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_dir / "abandoned-sessions.log", "ab") as log:
        subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "abandoned_sessions.py"),
             "--run", "--current-sid", sid,
             "--authority-wait", str(AUTHORITY_WAIT_SECONDS)],
            stdout=log, stderr=log, **kwargs)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--hook", action="store_true",
                      help="SessionStart: read the payload on stdin, spawn --run if needed")
    mode.add_argument("--run", action="store_true", help="run the pass now")
    ap.add_argument("--current-sid", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--authority-wait", type=float, default=0.0)
    args = ap.parse_args(argv)
    if args.hook:
        try:
            return _hook(sys.stdin.read())
        except Exception as exc:  # noqa: BLE001 — never break session start
            print(f"abandoned_sessions: hook failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 0
    sid = (args.current_sid or os.environ.get("CLAUDE_CODE_SESSION_ID")
           or os.environ.get("MIND_SID") or "").strip()
    summary = late_close_pass(
        current_sid=sid, dry_run=args.dry_run,
        boot_epoch=box_boot_epoch(), pid_domain=this_pid_domain(),
        authority_wait_seconds=args.authority_wait)
    summary["ts"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
