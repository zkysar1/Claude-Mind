"""In-flight Bash command windows, one per running command, per session ().

bash-agent-inject.py (PreToolUse[Bash]) opens a window just before a command
runs. bash-edit-record.sh (PostToolUse[Bash]) closes it when the command returns,
and reads every window still open to decide WHICH SESSION a file change belongs
to: the one session whose command was running when the file changed. Before this
the recorder credited a change to whichever session's command happened to END
first after it, so a session running a long command was stamped with a sibling's
edits, and iteration-commit.sh --session-sid staged them.

KEY: a hash of the command text as it will run. bash-agent-inject rewrites every
command (it prepends its exports), and PostToolUse hooks receive that rewritten
text (iteration-close-reminder.py relies on it), so both hooks derive the same
key with no shared id. Two identical commands running at once in one session
share a key and the later start wins. That can only narrow the session's window,
never widen it.

WHERE: agents/<agent>/sessions/<SID>/.bash-inflight/<key>, the same-box
per-session dir. Only sessions on this checkout can change its files, and a
session dir that does not exist is never created here.

EXPIRY: every PreToolUse hook of one event starts in parallel (guard-7426), so a
command another gate refuses still gets its window, and no PostToolUse ever
closes it. A foreground command cannot outlive its own timeout, so a window
expires at start + timeout + slack. Until then a stale window only makes
attribution more conservative (more changes recorded ambiguous), never less.

Every function is fail-open: a hook on the path of every Bash call must never
block the command.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

DIRNAME = ".bash-inflight"
SLACK_S = 60.0


def command_key(command: str) -> str:
    """The window key for a command, from its text exactly as it will run."""
    return hashlib.sha1(command.encode("utf-8", "surrogatepass")).hexdigest()[:20]


def _env_ms(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _timeout_s(timeout_ms) -> float:
    """The longest this call can run in the foreground, in seconds."""
    cap = _env_ms("BASH_MAX_TIMEOUT_MS", 600000)
    try:
        ms = float(timeout_ms)
    except (TypeError, ValueError):
        ms = _env_ms("BASH_DEFAULT_TIMEOUT_MS", 120000)
    return min(ms, cap) / 1000.0


def open_window(session_dir, key: str, timeout_ms=None, now: float | None = None) -> None:
    """Record that the command with this key starts now. Also drops this
    session's expired windows. Never raises."""
    try:
        session_dir = Path(session_dir)
        if not session_dir.is_dir():
            return
        d = session_dir / DIRNAME
        d.mkdir(exist_ok=True)
        now = time.time() if now is None else now
        for p in d.iterdir():
            w = _read(p)
            if w is not None and w[1] < now:
                p.unlink(missing_ok=True)
        (d / key).write_text(
            json.dumps({"start": now, "expires": now + _timeout_s(timeout_ms) + SLACK_S}),
            encoding="utf-8")
    except Exception:
        pass


def close_window(session_dir, key: str) -> float | None:
    """Remove the window with this key and return its start time, or None when
    there is none to remove. Never raises."""
    try:
        p = Path(session_dir) / DIRNAME / key
        w = _read(p)
        p.unlink()
        return None if w is None else w[0]
    except Exception:
        return None


def open_windows(sessions_root, now: float | None = None) -> list[tuple[str, float]]:
    """(sid, start) for every unexpired window of every session under
    sessions_root. Never raises."""
    now = time.time() if now is None else now
    out: list[tuple[str, float]] = []
    try:
        session_dirs = list(Path(sessions_root).iterdir())
    except Exception:
        return out
    for sd in session_dirs:
        try:
            windows = list((sd / DIRNAME).iterdir())
        except Exception:
            continue
        for p in windows:
            w = _read(p)
            if w is not None and w[1] >= now:
                out.append((sd.name, w[0]))
    return out


def attribute(mtime: int, windows) -> tuple[str, list[str]]:
    """Which session changed a file whose mtime is `mtime` (whole seconds).

    Returns (sid, candidates): one session running a command at that second
    gets the change, (sid, []). Several make it ambiguous, ("", [their sids]).
    None makes it unattributed, ("", []): no session of this agent was running a
    command, so the change came from something else (a background job, a Write
    edit, another agent)."""
    cands = sorted({sid for sid, start in windows if int(start) <= mtime})
    if len(cands) == 1:
        return cands[0], []
    return "", cands


def _read(p: Path) -> tuple[float, float] | None:
    """(start, expires) of one window, or None when it is gone. A window caught
    half-written reads as just opened, with the longest possible life."""
    try:
        w = json.loads(p.read_text(encoding="utf-8"))
        return float(w["start"]), float(w["expires"])
    except FileNotFoundError:
        return None
    except Exception:
        pass
    try:
        m = p.stat().st_mtime
    except Exception:
        return None
    return m, m + _env_ms("BASH_MAX_TIMEOUT_MS", 600000) / 1000.0 + SLACK_S
