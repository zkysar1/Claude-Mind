#!/usr/bin/env python3
"""recovery_yank.py — the consumer recovery-gate.sh never had ( part 3).

`recovery-gate.sh` demotes a reducer RUNNING->IDLE, wipes its runner manifest
and writes `session/recovery-log.jsonl` + `session/recovery-notice`. Until
2026-09-01 NOTHING read those records: a worker Body that parked on a demoted
reducer parked exactly as it would on a user `/stop`, and a FALSE demotion (a
live loop in a multi-hour provider rate-limit backoff) went unannounced until a
human noticed the whole fleet idle.

Two consumers, one module, so the "what counts as a yank / what counts as a
user stop" vocabulary cannot drift between them:

  check          worker-side classifier, run from the worker-loop park sequence.
                 Verdict + rc:  recovery-yank=0  user-stop=1  none=2  error=3
                 --mark-escalated writes session/recovery-yank-escalated with the
                 yank timestamp so the escalation fires ONCE per yank.
  preconditions  reducer-side gate for recovery-yank-reverse.sh: may THIS sid
                 restore RUNNING?  rc 0 = every precondition holds (JSON on
                 stdout carries the yank), 1 = at least one fails (reasons in
                 JSON), 3 = error.
  record-reversal  append the `yank_reversed` audit entry, rewrite the notice
                 so /prime announces the reversal instead of the yank, and
                 return the team-state marker to mirror cross-box.
  mark-escalated write the once-per-yank sentinel (what `check
                 --mark-escalated` does, exposed for tests and hand use).

Signal vocabulary
  yank         the latest recovery-log entry whose action is `recover` (or an
               older entry with no `action` key), OR — cross-box, where the log
               and the notice are machine-local — the synced team-state row
               `agent_status.<agent>.last_recovery` written by the gate.
  user-stop    any of: `stop-requested`, `stop-loop`, `stop-target-mode` present;
               a `last-stop-reason` whose path is a user path (or user_initiated)
               stamped at/after the yank; `handoff.yaml` written after the yank
               (a consolidation ran, i.e. a graceful stop completed).
  resolved     a `yank_reversed` entry newer than the yank, or agent-state
               RUNNING with a running-session-id (a /start happened since).

The asymmetry is deliberate: every ambiguous read resolves AWAY from
`recovery-yank` for the worker (a spurious escalation costs one email, but the
default is silence and silence is the measured defect, so ambiguity about the
yank itself does NOT suppress it) and AWAY from reversal for the reducer (a
wrong RUNNING restore is the unrecoverable direction — dual reducers).

Timestamps are naive `YYYY-MM-DDTHH:MM:SS` in UTC wall time (CLAUDE.md).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:  # pragma: no cover - import shape only
    from _paths import agent_dir as _agent_dir, PROJECT_ROOT as _PROJECT_ROOT
except Exception:  # noqa: BLE001
    _agent_dir = None
    _PROJECT_ROOT = _HERE.parent.parent

VERDICT_RC = {"recovery-yank": 0, "user-stop": 1, "none": 2, "error": 3}
USER_STOP_PATHS = {"user-stop", "user_stop", "graceful-stop", "productivity-gate"}
# Paths the recovery gate itself (or its sibling automated demoters) record —
# their presence after a yank is the yank's own bookkeeping, not a user stop.
AUTOMATED_STOP_PATHS = {"recovery-gate-zombie", "recovery-failed-permanent",
                        "worker-body-parked", "reducer-self-fence"}
DEFAULT_WINDOW_MINUTES = 360
ESCALATED_SENTINEL = "recovery-yank-escalated"


# --------------------------------------------------------------------------- utils
def parse_ts(value: Any) -> Optional[_dt.datetime]:
    """Tolerant naive-ISO parser. Returns None on anything unparseable."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "+" in s[10:]:
        s = s[:10] + s[10:].split("+", 1)[0]
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return d


def fmt_ts(d: _dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def _now(arg: Optional[str]) -> _dt.datetime:
    d = parse_ts(arg) if arg else None
    return d or _dt.datetime.utcnow().replace(microsecond=0)


def _read_text(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _mtime(p: Path) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.utcfromtimestamp(p.stat().st_mtime).replace(microsecond=0)
    except OSError:
        return None


def resolve_agent_dir(agent: Optional[str], explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    if not agent:
        raise SystemExit("recovery_yank: --agent or --agent-dir is required")
    if _agent_dir is None:
        return Path(_PROJECT_ROOT) / "agents" / agent
    return Path(_agent_dir(agent))


# ---------------------------------------------------------------- recovery-log reads
def read_log_entries(agent_dir: Path) -> list[dict]:
    """All parseable entries of session/recovery-log.jsonl, file order.

    A corrupt line is skipped, never fatal: the log is append-only and a torn
    write at the tail must not blind the classifier to the entries before it.
    """
    p = agent_dir / "session" / "recovery-log.jsonl"
    out: list[dict] = []
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def latest_yank(entries: list[dict]) -> Optional[dict]:
    """The newest entry whose action is `recover` (or absent — pre- shape)."""
    for e in reversed(entries):
        action = e.get("action", "recover")
        if action == "recover" and parse_ts(e.get("ts")) is not None:
            return e
    return None


def reversal_after(entries: list[dict], yank_ts: _dt.datetime) -> Optional[dict]:
    for e in reversed(entries):
        if e.get("action") == "yank_reversed":
            t = parse_ts(e.get("ts"))
            if t is not None and t >= yank_ts:
                return e
    return None


def marker_to_yank(marker: Any) -> Optional[dict]:
    """team-state `agent_status.<agent>.last_recovery` -> yank dict, or None.

    The field may arrive as a dict or as a JSON string (whichever the team-state
    writer stored); both shapes decode to the same record.
    """
    if isinstance(marker, str):
        try:
            marker = json.loads(marker)
        except json.JSONDecodeError:
            return None
    if not isinstance(marker, dict) or parse_ts(marker.get("ts")) is None:
        return None
    y = dict(marker)
    y.setdefault("action", "recover")
    y["source_channel"] = "team-state"
    return y


# ------------------------------------------------------------ user-stop evidence
def parse_last_stop_reason(agent_dir: Path) -> Optional[dict]:
    """key=value lines written by stop-reason-record.py (path, stopped_at, ...)."""
    p = agent_dir / "session" / "last-stop-reason"
    raw = _read_text(p)
    if raw is None:
        return None
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    if not fields:
        return None
    fields.setdefault("stopped_at", "")
    if parse_ts(fields["stopped_at"]) is None:
        m = _mtime(p)
        fields["stopped_at"] = fmt_ts(m) if m else ""
    return fields


def user_stop_evidence(agent_dir: Path, since: Optional[_dt.datetime]) -> list[dict]:
    """Every user-stop artifact that post-dates `since` (all of them when since is None).

    Each item: {signal, ts}. The signal files carry no timestamp of their own;
    a present `stop-requested`/`stop-loop`/`stop-target-mode` is treated as
    CURRENT (its mtime is the stamp) — /stop writes them first and the next
    /start clears them, so their mere presence means a user stop is in flight
    or completed since the last start.
    """
    sess = agent_dir / "session"
    found: list[dict] = []

    def _after(t: Optional[_dt.datetime]) -> bool:
        return t is not None and (since is None or t >= since)

    for name in ("stop-requested", "stop-loop", "stop-target-mode"):
        p = sess / name
        if p.exists():
            m = _mtime(p)
            if _after(m):
                found.append({"signal": name, "ts": fmt_ts(m) if m else None})

    reason = parse_last_stop_reason(agent_dir)
    if reason:
        path = reason.get("path", "")
        user_flag = reason.get("user_initiated", "").lower() in ("1", "true", "yes")
        if (path in USER_STOP_PATHS or user_flag) and path not in AUTOMATED_STOP_PATHS:
            t = parse_ts(reason.get("stopped_at"))
            if _after(t):
                found.append({"signal": "last-stop-reason", "path": path,
                              "ts": fmt_ts(t) if t else None})

    handoff = sess / "handoff.yaml"
    if handoff.exists():
        m = _mtime(handoff)
        if since is not None and _after(m):
            found.append({"signal": "handoff.yaml", "ts": fmt_ts(m) if m else None})
    return found


# ------------------------------------------------------------------ classify
def classify(*, local_yank: Optional[dict], marker_yank: Optional[dict],
             reversed_entry: Optional[dict], user_stops: list[dict],
             agent_state: Optional[str], running_sid: Optional[str],
             escalated_ts: Optional[str]) -> dict:
    """Pure verdict. Inputs are already-read facts; no I/O here (tested directly)."""
    yank = local_yank or marker_yank
    if local_yank and marker_yank:
        lt, mt = parse_ts(local_yank.get("ts")), parse_ts(marker_yank.get("ts"))
        if lt and mt and mt > lt:
            yank = marker_yank
    if yank is None:
        return {"verdict": "none", "reason": "no recovery yank on record", "yank": None,
                "escalated_before": False, "user_stops": user_stops}
    yts = parse_ts(yank.get("ts"))
    if reversed_entry is not None:
        return {"verdict": "none", "reason": "yank already reversed at %s" % reversed_entry.get("ts"),
                "yank": yank, "escalated_before": False, "user_stops": user_stops}
    if (agent_state or "").upper() == "RUNNING" and running_sid:
        return {"verdict": "none",
                "reason": "agent-state RUNNING with running-session-id=%s — a /start post-dates the yank" % running_sid,
                "yank": yank, "escalated_before": False, "user_stops": user_stops}
    later_stops = [s for s in user_stops
                   if yts is None or parse_ts(s.get("ts")) is None or parse_ts(s["ts"]) >= yts]
    if later_stops:
        return {"verdict": "user-stop",
                "reason": "user-stop artifact(s) at/after the yank: %s" % ", ".join(s["signal"] for s in later_stops),
                "yank": yank, "escalated_before": False, "user_stops": later_stops}
    return {"verdict": "recovery-yank",
            "reason": "recovery-gate demotion at %s (%s) with no user-stop artifact after it"
                      % (yank.get("ts"), yank.get("path", "?")),
            "yank": yank,
            "escalated_before": bool(escalated_ts) and escalated_ts == yank.get("ts"),
            "user_stops": user_stops}


def read_team_state_marker(agent: str, timeout: float = 20.0) -> Optional[Any]:
    """The synced cross-box mirror of the yank. Fail-open: None on any failure."""
    wrapper = _HERE / "team-state-read.sh"
    if not wrapper.exists():
        return None
    env = dict(os.environ)
    env["MIND_AGENT"] = agent
    try:
        # guard-580/581: resolved bash (never the System32 WSL stub) + a POSIX
        # script path; a bare "bash" argv[0] can hang forever on win32.
        from _runtime_bash import bash_cmd
        r = subprocess.run(
            bash_cmd(wrapper, "--field", "agent_status.%s.last_recovery" % agent, "--json"),
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError, ImportError, ValueError):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out or out == "null":
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def write_escalated_sentinel(agent_dir: Path, yank_ts: str) -> Path:
    sess = agent_dir / "session"
    sess.mkdir(parents=True, exist_ok=True)
    p = sess / ESCALATED_SENTINEL
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yank_ts + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def cmd_check(args: argparse.Namespace) -> int:
    agent_dir = resolve_agent_dir(args.agent, args.agent_dir)
    entries = read_log_entries(agent_dir)
    local = latest_yank(entries)
    marker = None
    if args.team_state_marker is not None:
        marker = marker_to_yank(args.team_state_marker)
    elif not args.no_team_state and args.agent:
        marker = marker_to_yank(read_team_state_marker(args.agent))
    yank_ts = None
    for cand in (local, marker):
        t = parse_ts(cand.get("ts")) if cand else None
        if t and (yank_ts is None or t > yank_ts):
            yank_ts = t
    reversed_entry = reversal_after(entries, yank_ts) if yank_ts else None
    stops = user_stop_evidence(agent_dir, yank_ts)
    state = _read_text(agent_dir / "session" / "agent-state")
    running_sid = _read_text(agent_dir / "session" / "running-session-id")
    escalated_ts = _read_text(agent_dir / "session" / ESCALATED_SENTINEL)
    verdict = classify(local_yank=local, marker_yank=marker, reversed_entry=reversed_entry,
                       user_stops=stops, agent_state=state, running_sid=running_sid,
                       escalated_ts=escalated_ts)
    verdict["agent"] = args.agent
    verdict["agent_dir"] = str(agent_dir)
    if args.mark_escalated and verdict["verdict"] == "recovery-yank" and verdict["yank"]:
        write_escalated_sentinel(agent_dir, str(verdict["yank"].get("ts")))
        verdict["marked_escalated"] = True
    print(json.dumps(verdict, ensure_ascii=False, indent=None if args.compact else 2))
    return VERDICT_RC[verdict["verdict"]]


# -------------------------------------------------------------- preconditions
def _read_binding(agent_dir: Path, sid: str) -> Optional[dict]:
    p = agent_dir / "sessions" / sid / "binding.yaml"
    raw = _read_text(p)
    if raw is None:
        return None
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(raw)
        if isinstance(data, dict):
            return {k: (v if isinstance(v, str) else str(v)) for k, v in data.items()}
    except Exception:  # noqa: BLE001 - fall back to the flat key: value shape
        pass
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out or None


def evaluate_preconditions(agent_dir: Path, agent: Optional[str], sid: str,
                           now: _dt.datetime, window_minutes: int) -> dict:
    """Every condition under which a demoted SID may restore RUNNING. ALL must hold."""
    reasons: list[str] = []
    sess = agent_dir / "session"
    entries = read_log_entries(agent_dir)
    yank = latest_yank(entries)
    if yank is None:
        return {"ok": False, "reasons": ["no `recover` entry in recovery-log.jsonl"], "yank": None, "sid": sid}
    yts = parse_ts(yank.get("ts"))
    if yank.get("sid_recorded") != sid:
        reasons.append("demoted sid %r is not this sid %r" % (yank.get("sid_recorded"), sid))
    rev = reversal_after(entries, yts) if yts else None
    if rev is not None:
        reasons.append("yank already reversed at %s" % rev.get("ts"))
    if yts is None:
        reasons.append("yank ts unparseable")
    else:
        age_min = (now - yts).total_seconds() / 60.0
        if age_min < 0:
            reasons.append("yank ts %s is in the future" % yank.get("ts"))
        elif age_min > window_minutes:
            reasons.append("yank is %.0f min old, past the %d min reversal window" % (age_min, window_minutes))
    state = (_read_text(sess / "agent-state") or "").upper()
    if state != "IDLE":
        reasons.append("agent-state is %r, not IDLE" % (state or "absent"))
    mode = (_read_text(sess / "agent-mode") or "").lower()
    if mode != "autonomous":
        reasons.append("agent-mode is %r, not autonomous" % (mode or "absent"))
    binding = _read_binding(agent_dir, sid)
    if binding is None:
        reasons.append("no sessions/%s/binding.yaml" % sid)
    else:
        if (binding.get("mode") or "").lower() != "autonomous":
            reasons.append("binding mode is %r, not autonomous" % binding.get("mode"))
        if agent and binding.get("agent") and binding.get("agent") != agent:
            reasons.append("binding agent %r != %r" % (binding.get("agent"), agent))
        bstart = parse_ts(binding.get("started_at"))
        if bstart is None:
            reasons.append("binding started_at unparseable")
        elif yts is not None and bstart > yts:
            reasons.append("binding started_at %s post-dates the yank — this is a NEW session, not the demoted runner"
                           % binding.get("started_at"))
    for name in ("stop-requested", "stop-loop", "stop-target-mode"):
        if (sess / name).exists():
            reasons.append("%s is present (a stop is in flight)" % name)
    for s in user_stop_evidence(agent_dir, yts):
        if s["signal"] in ("stop-requested", "stop-loop", "stop-target-mode"):
            continue  # already reported above
        reasons.append("user-stop evidence after the yank: %s at %s" % (s["signal"], s.get("ts")))
    running_sid = _read_text(sess / "running-session-id")
    if running_sid and running_sid != sid:
        reasons.append("running-session-id names another runner: %s" % running_sid)
    return {"ok": not reasons, "reasons": reasons, "yank": yank, "sid": sid,
            "window_minutes": window_minutes, "now": fmt_ts(now)}


def cmd_preconditions(args: argparse.Namespace) -> int:
    agent_dir = resolve_agent_dir(args.agent, args.agent_dir)
    window = args.window_minutes
    if window is None:
        env = os.environ.get("RECOVERY_YANK_REVERSE_WINDOW_MINUTES", "")
        window = int(env) if env.isdigit() else DEFAULT_WINDOW_MINUTES
    result = evaluate_preconditions(agent_dir, args.agent, args.sid, _now(args.now), window)
    result["agent"] = args.agent
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


# ------------------------------------------------------------ record-reversal
def _append_jsonl(p: Path, record: dict) -> None:
    try:
        from _fileops import locked_append_jsonl  # type: ignore
        locked_append_jsonl(str(p), record)
        return
    except Exception:  # noqa: BLE001 - lock helper unavailable in a sandbox
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def cmd_record_reversal(args: argparse.Namespace) -> int:
    agent_dir = resolve_agent_dir(args.agent, args.agent_dir)
    sess = agent_dir / "session"
    entries = read_log_entries(agent_dir)
    yank = latest_yank(entries)
    if yank is None:
        print("record-reversal: no yank to reverse", file=sys.stderr)
        return 1
    now = _now(args.now)
    record = {
        "ts": fmt_ts(now),
        "agent": args.agent,
        "action": "yank_reversed",
        "path": yank.get("path", "?"),
        "cause": "live session %s reversed the %s demotion of %s: %s"
                 % (args.sid, yank.get("path", "?"), yank.get("ts"), (yank.get("cause") or "")[:200]),
        "sid_recorded": args.sid,
        "acting_sid": args.sid,
        "source": "recovery-yank-reverse",
        "evidence": {"yank": yank, "runner_token_rotated": True},
    }
    _append_jsonl(sess / "recovery-log.jsonl", record)
    notice = sess / "recovery-notice"
    try:
        notice.write_text(
            "RECOVERY YANK REVERSED at %s: the %s demotion recorded at %s (%s) was applied to a "
            "live session (%s reached its stop hook). Runner state restored; see recovery-log.jsonl.\n"
            % (record["ts"], yank.get("path", "?"), yank.get("ts"), (yank.get("cause") or "")[:160], args.sid),
            encoding="utf-8")
    except OSError:
        pass
    marker = {"ts": yank.get("ts"), "path": yank.get("path", "?"),
              "cause": (yank.get("cause") or "")[:240], "sid_recorded": yank.get("sid_recorded"),
              "acting_sid": yank.get("acting_sid"), "reversed_at": record["ts"], "reversed_by": args.sid}
    print(json.dumps({"record": record, "team_state_marker": marker}, ensure_ascii=False))
    return 0


def cmd_mark_escalated(args: argparse.Namespace) -> int:
    agent_dir = resolve_agent_dir(args.agent, args.agent_dir)
    yank = latest_yank(read_log_entries(agent_dir))
    ts = args.yank_ts or (yank.get("ts") if yank else None)
    if not ts:
        print("mark-escalated: no yank timestamp (pass --yank-ts)", file=sys.stderr)
        return 1
    p = write_escalated_sentinel(agent_dir, str(ts))
    print(str(p))
    return 0


def new_runner_token() -> str:
    return str(uuid.uuid4())


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--agent", default=os.environ.get("MIND_AGENT") or None)
        p.add_argument("--agent-dir", default=None, help="explicit agent dir (tests / sandboxes)")
        p.add_argument("--now", default=None, help="override wall clock (naive ISO)")

    c = sub.add_parser("check", help="worker-side yank/user-stop/none classifier")
    _common(c)
    c.add_argument("--mark-escalated", action="store_true")
    c.add_argument("--no-team-state", action="store_true", help="skip the cross-box marker read")
    c.add_argument("--team-state-marker", default=None,
                   help="inject the agent_status.<agent>.last_recovery value (JSON) instead of reading it")
    c.add_argument("--json", dest="compact", action="store_true", help="single-line JSON")
    c.set_defaults(fn=cmd_check)

    p = sub.add_parser("preconditions", help="reducer-side: may --sid restore RUNNING?")
    _common(p)
    p.add_argument("--sid", required=True)
    p.add_argument("--window-minutes", type=int, default=None)
    p.set_defaults(fn=cmd_preconditions)

    r = sub.add_parser("record-reversal", help="append the yank_reversed audit entry + rewrite the notice")
    _common(r)
    r.add_argument("--sid", required=True)
    r.set_defaults(fn=cmd_record_reversal)

    m = sub.add_parser("mark-escalated", help="write the once-per-yank escalation sentinel")
    _common(m)
    m.add_argument("--yank-ts", default=None)
    m.set_defaults(fn=cmd_mark_escalated)

    t = sub.add_parser("new-token", help="print a fresh runner-token UUID4")
    t.set_defaults(fn=lambda a: (print(new_runner_token()) or 0))

    args = ap.parse_args(argv)
    try:
        return int(args.fn(args))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never manufacture a verdict from a crash (guard-4220)
        print(json.dumps({"verdict": "error", "error": "%s: %s" % (type(exc).__name__, exc)}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
