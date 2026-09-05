"""Peer-liveness classification for the reducer watchdog (owner directive, 2026-09-05).

WHY THIS EXISTS. On 2026-09-04 foxtrot (LAPTOP-3IOFCNEO -- a Windows/WSL2 tmux
session, NOT an LXC container) died on an API error at 22:25 UTC and stayed
dark until the owner restarted it by hand at ~07:26 the next morning: about
nine hours. Nothing paged him. The fleet liveness sweep
(world/scripts/fleet-liveness-sweep.py, a systemd timer on zakbox1) draws its
population from `lxc list`, so foxtrot is structurally outside the only
detector that emails a human; every in-loop detector (agent-watchdog --tick,
StalledProbe, heartbeat-tick) dies WITH the loop it watches; and
runner-liveness-evidence classifies retry activity as ALIVE by design (right
for the kill decision, useless as a page). Owner directive, verbatim intent:
"we need to upgrade the fleet watcher so I get alerted for things like this."

WHAT IT DOES. Every live REDUCER's watchdog tick (iteration-close.sh) reads
every peer's team-state shard from the AUTHORITATIVE store (guard-980: the
local mirror of a PEER's shard is stale by construction) and classifies each
peer. A peer whose mind heartbeat (`last_active`) has not moved for
`stale_hours` while its session is marked running is NOT paged on that signal
alone -- guard-4180 forbids paging on a fixed-point-in-cycle stamp, guard-1063
requires a board cross-check, and guard-1042 / rb-3000 name goal-record
signals (claims, completions) as the authoritative liveness hierarchy. So the
verdict is corroborated with three INDEPENDENT-WRITER signals, each read from
the store of record and only when the heartbeat is already stale:

  - execution diary head   (execution-diary.sh writes it; _fleet_diary reads it)
  - last board post        (the model posts it; message ids self-timestamp)
  - last claim/completion  (aspirations-*.sh write them; the synced world store)

If ANY corroborating signal moved inside the window the peer is `slow`
(reported, never paged). If every READABLE corroborating signal is also frozen
the peer is `stalled`, and the reducer pages the owner once per episode. If NO
corroborating signal could be read at all the verdict is `unknown`: a blind
probe must not manufacture a page (guard-1753, guard-1977).

PURE WHERE IT MATTERS. `classify_peer` takes values, not handles, and `scan`
takes its readers as injectable callables, so the decision table is unit-
testable without a backend (core/scripts/tests/test_peer_liveness.py). IO lives
in the default readers at the bottom of this file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

DEFAULT_STALE_HOURS = 3.0
STALE_HOURS_ENV = "AGENT_WATCHDOG_PEER_STALE_HOURS"

# Channels a live agent posts to. Read only for peers whose heartbeat is
# already stale, so the cost is paid on the rare tick that needs it.
BOARD_CHANNELS = ("coordination", "findings", "general", "decisions")

V_ALIVE = "alive"
V_SLOW = "slow"
V_STALLED = "stalled"
V_STOPPED = "stopped"
V_RETIRED = "retired"
V_UNKNOWN = "unknown"
ALERTING = frozenset({V_STALLED})

PROV_AUTHORITATIVE = "authoritative"
PROV_LOCAL_MIRROR = "local-mirror"
PROV_NONE = "none"

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_DIARY_TS_RE = re.compile(r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')

# (who-key, when-key) pairs on a goal record that prove the named agent acted.
# guard-1042: claimed_at / completed_at / fresh filed goals are the authoritative
# liveness hierarchy; rb-3000: completions are the cross-box ground truth.
GOAL_SIGNAL_KEYS = (
    ("completed_by", "completed_date"),
    ("completed_by", "completed_at"),
    ("claimed_by", "claimed_at"),
    ("lastAchievedBy", "lastAchievedAt"),
    ("filed_by_agent", "created_at"),
)


# ─────────────────────────────────────────────────────────────────────────────
# Small pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def stale_hours() -> float:
    """Threshold in hours. Env override must be a positive float; else default."""
    raw = (os.environ.get(STALE_HOURS_ENV) or "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_STALE_HOURS


def utc_now() -> dt.datetime:
    """Naive UTC wall time -- the fleet's one clock (CLAUDE.md, TZ=UTC by fiat)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def parse_iso(s: Any) -> Optional[dt.datetime]:
    """Naive datetime from an ISO stamp with or without an offset; None if unparsable.

    Delegates to worker_stall._parse_iso (the fleet's carrier-stamp parser) so
    the two probes cannot disagree about a `Z` suffix or a `-05:00` offset
    (guard-2676). The fallback exists only for a hermetic import failure.
    """
    if s is None or s == "":
        return None
    try:
        from worker_stall import _parse_iso  # type: ignore
        return _parse_iso(str(s))
    except Exception:  # noqa: BLE001 -- fallback keeps this module importable alone
        txt = str(s).strip()
        if txt.endswith(("Z", "z")):
            txt = txt[:-1]
        try:
            parsed = dt.datetime.fromisoformat(txt)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed


def signal(ts: Optional[dt.datetime] = None, readable: bool = False, source: str = "") -> dict:
    """One corroborating signal.

    `readable` means the signal was read from the store of record, so its
    STALENESS is evidence. A fresh timestamp is positive evidence whatever its
    provenance (a mirror cannot invent activity that did not happen); a stale
    timestamp from a mirror or an unreadable store proves nothing (guard-980).
    """
    return {"ts": ts, "readable": bool(readable), "source": source}


def is_alerting(verdict: str) -> bool:
    return verdict in ALERTING


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _is_retired(row: dict) -> bool:
    try:
        import _team_state  # type: ignore
        return bool(_team_state._is_retired(row))
    except Exception:  # noqa: BLE001 -- keep the revival rule's shape locally
        if not row.get("retired"):
            return False
        retired_at = str(row.get("retired_at") or "")
        last_active = str(row.get("last_active") or "")
        return not (last_active and retired_at and last_active > retired_at)


def _mins(delta: Optional[dt.timedelta]) -> Optional[float]:
    return round(delta.total_seconds() / 60.0, 1) if delta is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# The decision table
# ─────────────────────────────────────────────────────────────────────────────

def classify_peer(agent: str, row: Optional[dict], provenance: str, *, now: dt.datetime,
                  stale_hours: float, diary: Optional[dict] = None,
                  board: Optional[dict] = None, goals: Optional[dict] = None) -> dict:
    """Classify ONE peer. Pure: values in, dict out.

    Verdicts: alive | slow | stalled | stopped | retired | unknown. Only
    `stalled` alerts. `corroboration_needed` is True when the heartbeat is stale
    and no corroborating signal was supplied -- `scan` uses it to decide which
    peers are worth the expensive independent reads.
    """
    thr = dt.timedelta(hours=float(stale_hours))
    out: Dict[str, Any] = {
        "agent": agent, "provenance": provenance, "verdict": V_UNKNOWN, "reason": "",
        "last_active": None, "last_active_age_min": None, "corroboration_needed": False,
        "session_ended": None, "in_flight": None, "live_phase": None, "signals": {},
    }
    if not isinstance(row, dict) or not row or provenance == PROV_NONE:
        out["reason"] = "no shard row could be read anywhere (known blindness, not absence)"
        return out
    out["session_ended"] = _truthy(row.get("session_ended"))
    inflight = row.get("in_flight")
    out["in_flight"] = inflight.get("goal_id") if isinstance(inflight, dict) else (inflight or None)
    out["live_phase"] = row.get("live_phase")
    out["last_active"] = row.get("last_active")

    if _is_retired(row):
        out["verdict"] = V_RETIRED
        out["reason"] = "retirement tombstone -- decommissioned, not quiet"
        return out
    if provenance == PROV_LOCAL_MIRROR:
        out["reason"] = "row came from the local mirror, which is not evidence about a peer (guard-980)"
        return out
    if out["session_ended"]:
        out["verdict"] = V_STOPPED
        out["reason"] = "session_ended is set -- the agent stopped on purpose"
        return out

    la = parse_iso(row.get("last_active"))
    if la is None:
        out["reason"] = "row carries no parsable last_active"
        return out
    la_age = now - la
    out["last_active_age_min"] = _mins(la_age)
    if la_age <= thr:
        out["verdict"] = V_ALIVE
        out["reason"] = f"last_active {out['last_active_age_min']}m ago, inside the {stale_hours}h window"
        return out

    supplied = {k: v for k, v in (("diary", diary), ("board", board), ("goals", goals)) if v is not None}
    if not supplied:
        out["corroboration_needed"] = True
        out["reason"] = (f"last_active stale ({out['last_active_age_min']}m) -- not a verdict on its own "
                         f"(guard-4180); corroboration not yet gathered")
        return out

    fresh: List[str] = []
    readable_stale: List[str] = []
    for name, sig in supplied.items():
        ts = sig.get("ts")
        age = (now - ts) if isinstance(ts, dt.datetime) else None
        is_fresh = age is not None and age <= thr
        out["signals"][name] = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(ts, dt.datetime) else None,
            "age_min": _mins(age), "readable": bool(sig.get("readable")), "fresh": is_fresh,
            "source": sig.get("source"),
        }
        if is_fresh:
            fresh.append(name)
        elif sig.get("readable"):
            readable_stale.append(name)

    if fresh:
        out["verdict"] = V_SLOW
        out["reason"] = (f"last_active stale ({out['last_active_age_min']}m) but an independent writer moved "
                         f"inside the window: {', '.join(fresh)} -- alive-but-slow or a broken heartbeat "
                         f"writer; report, do not page (guard-4180)")
        return out
    if not readable_stale:
        out["reason"] = (f"last_active stale ({out['last_active_age_min']}m) and no corroborating signal "
                         f"could be READ from the store of record -- blind, not dead (guard-1753)")
        return out
    out["verdict"] = V_STALLED
    out["reason"] = (f"no sign of life for over {stale_hours}h while the session is marked running: "
                     f"last_active frozen {out['last_active_age_min']}m and every readable independent "
                     f"signal ({', '.join(readable_stale)}) is frozen too")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pure parsers behind the IO readers (unit-tested directly)
# ─────────────────────────────────────────────────────────────────────────────

def _last_timestamp_in_text(text: str) -> Optional[dt.datetime]:
    """Timestamp of the LAST diary line (append-only store, so that is the head).

    Prefers the record's own `timestamp` key; falls back to any ISO stamp on the
    line, then walks backwards over trailing blank/partial lines.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _DIARY_TS_RE.search(line) or _ISO_RE.search(line)
        if m:
            return parse_iso(m.group(1) if m.re is _DIARY_TS_RE else m.group(0))
        # A non-empty line with no stamp (banner, torn write): keep walking.
    return None


def _board_signals_from_texts(texts: Dict[str, Tuple[Optional[str], str]],
                              agents: Iterable[str]) -> Dict[str, dict]:
    """Latest self-timestamped message id per agent across channel texts.

    `texts` is {channel: (text|None, provenance)}. Message ids are
    `msg-YYYYMMDD-HHMMSS-<author>-N` (guard-1063: self-timestamping), so no JSON
    parse is needed and a torn line cannot poison the scan.
    """
    names = sorted({a for a in agents if a}, key=len, reverse=True)
    out = {a: signal(None, False, "board") for a in names}
    if not names:
        return out
    pat = re.compile(r"msg-(\d{8})-(\d{6})-(" + "|".join(re.escape(a) for a in names) + r")-\d+\b")
    for _ch, (text, prov) in texts.items():
        if not text:
            continue
        readable = prov == PROV_AUTHORITATIVE
        for m in pat.finditer(text):
            try:
                ts = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                continue
            cur = out[m.group(3)]
            if cur["ts"] is None or ts > cur["ts"]:
                cur["ts"] = ts
            cur["readable"] = cur["readable"] or readable
    return out


def _goal_signals_from_lines(lines: Optional[Iterable[str]], provenance: str,
                             agents: Iterable[str]) -> Dict[str, dict]:
    """Latest claim / completion / filing stamp per agent from aspiration records."""
    wanted = {a for a in agents if a}
    out = {a: signal(None, False, "goals") for a in wanted}
    if lines is None or not wanted:
        return out
    readable = provenance == PROV_AUTHORITATIVE
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for g in rec.get("goals") or []:
            if not isinstance(g, dict):
                continue
            for who_key, when_key in GOAL_SIGNAL_KEYS:
                who = g.get(who_key)
                if who not in wanted:
                    continue
                ts = parse_iso(g.get(when_key))
                if ts is None:
                    continue
                cur = out[who]
                if cur["ts"] is None or ts > cur["ts"]:
                    cur["ts"] = ts
    for a in wanted:
        out[a]["readable"] = readable
    return out


# ─────────────────────────────────────────────────────────────────────────────
# IO readers (default wiring; every one fails open and says so in `source`)
# ─────────────────────────────────────────────────────────────────────────────

def read_rows(world_dir: Path) -> Tuple[Dict[str, dict], Dict[str, str], str]:
    """(rows, provenance_by_agent, roster_provenance) from the authoritative store."""
    import _team_state  # type: ignore
    rows, prov = _team_state.load_rows_authoritative_with_provenance(world_dir)
    by_agent = dict((prov or {}).get("by_agent") or {})
    roster = (prov or {}).get("roster") or PROV_NONE
    return dict(rows or {}), by_agent, roster


def _read_store_text(path: Path) -> Tuple[Optional[str], str]:
    """Authoritative-first text read of one world store; local mirror as fallback."""
    try:
        from storage_backend import get_backend  # type: ignore
        b = get_backend()
        return b.read_authoritative_bytes(Path(path)).decode("utf-8", errors="replace"), PROV_AUTHORITATIVE
    except FileNotFoundError:
        return None, "absent"
    except Exception:  # noqa: BLE001 -- backend absent/erroring: fall back to the mirror
        pass
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace"), PROV_LOCAL_MIRROR
    except OSError:
        return None, "error"


def read_diary_signal(agent: str, agents_root: Optional[Path] = None) -> dict:
    """Head timestamp of the peer's execution diary, read via _fleet_diary."""
    try:
        from _fleet_diary import read_agent_diary  # type: ignore
        text, prov = read_agent_diary(agent, base=agents_root)
    except Exception as e:  # noqa: BLE001
        return signal(None, False, f"diary:error:{type(e).__name__}")
    if text is None:
        return signal(None, False, f"diary:{prov}")
    return signal(_last_timestamp_in_text(text), prov == PROV_AUTHORITATIVE, f"diary:{prov}")


def read_board_signals(world_dir: Path, agents: Iterable[str]) -> Dict[str, dict]:
    texts = {ch: _read_store_text(Path(world_dir) / "board" / f"{ch}.jsonl") for ch in BOARD_CHANNELS}
    return _board_signals_from_texts(texts, agents)


def read_goal_signals(world_dir: Path, agents: Iterable[str]) -> Dict[str, dict]:
    try:
        from worker_stall import _read_queue_lines  # type: ignore
        lines, prov = _read_queue_lines(Path(world_dir) / "aspirations.jsonl")
    except Exception:  # noqa: BLE001
        lines, prov = None, PROV_NONE
    return _goal_signals_from_lines(lines, prov, agents)


# ─────────────────────────────────────────────────────────────────────────────
# The scan
# ─────────────────────────────────────────────────────────────────────────────

def scan(world_dir: Path, self_agent: str, *, now: Optional[dt.datetime] = None,
         stale_hours_override: Optional[float] = None,
         rows_reader: Callable[[Path], Tuple[dict, dict, str]] = read_rows,
         diary_reader: Callable[[str], dict] = read_diary_signal,
         board_reader: Callable[[Path, Iterable[str]], Dict[str, dict]] = read_board_signals,
         goal_reader: Callable[[Path, Iterable[str]], Dict[str, dict]] = read_goal_signals) -> dict:
    """Classify every peer of `self_agent`. Never raises.

    Cheap pass first (one authoritative rows read, no corroboration); the three
    independent reads run once, and only for peers whose heartbeat is stale.
    """
    now = now or utc_now()
    # `is not None`, NOT truthiness ( fresh-eyes). A caller asking for
    # `stale_hours_override=0` wants EVERYTHING treated as stale -- the maximally
    # forced positive control -- and a falsy test silently hands back the 3.0
    # default instead, i.e. the exact OPPOSITE, reported as a clean "nothing is
    # stalled". Measured through the CLI: `--stale-hours 0` produced thr=3.0.
    thr = float(stale_hours_override) if stale_hours_override is not None else stale_hours()
    report: Dict[str, Any] = {
        "self": self_agent, "checked_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "stale_hours": thr, "peers": [], "blind": False, "blind_cause": None,
        "roster_provenance": None,
    }
    try:
        rows, by_agent, roster = rows_reader(Path(world_dir))
    except Exception as e:  # noqa: BLE001 -- a blind scan must SAY so, not return healthy
        report["blind"] = True
        report["blind_cause"] = f"rows read failed: {type(e).__name__}: {e}"[:300]
        return report
    report["roster_provenance"] = roster
    if roster == PROV_LOCAL_MIRROR:
        report["blind"] = True
        report["blind_cause"] = "roster came from the local mirror; peers may be missing entirely"

    peers = sorted(a for a in rows if a and a != self_agent)
    first = {a: classify_peer(a, rows.get(a), by_agent.get(a, PROV_NONE), now=now, stale_hours=thr)
             for a in peers}
    needs = [a for a, r in first.items() if r.get("corroboration_needed")]
    if needs:
        try:
            board = board_reader(Path(world_dir), needs)
        except Exception as e:  # noqa: BLE001
            board = {a: signal(None, False, f"board:error:{type(e).__name__}") for a in needs}
        try:
            goals = goal_reader(Path(world_dir), needs)
        except Exception as e:  # noqa: BLE001
            goals = {a: signal(None, False, f"goals:error:{type(e).__name__}") for a in needs}
        for a in needs:
            try:
                diary = diary_reader(a)
            except Exception as e:  # noqa: BLE001
                diary = signal(None, False, f"diary:error:{type(e).__name__}")
            first[a] = classify_peer(a, rows.get(a), by_agent.get(a, PROV_NONE), now=now, stale_hours=thr,
                                     diary=diary, board=board.get(a, signal(None, False, "board")),
                                     goals=goals.get(a, signal(None, False, "goals")))
    report["peers"] = [first[a] for a in peers]
    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI — a machine-readable entry point for OUT-OF-LOOP callers ()
# ─────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    """Emit scan() as JSON on stdout. Always prints a report; always returns 0.

    EXISTS FOR THE HOST SWEEPER. world/scripts/fleet-liveness-sweep.py runs on
    zakbox1, which has no Mind checkout, no world mirror and no store
    credentials, so it cannot import this module -- it invokes this CLI inside a
    Mind container over `lxc exec` and parses the JSON. Re-deriving the decision
    table on the host instead would put two classifiers on one question, which
    is the drift guard-2676 forbids.

    `--self ''` (the default) classifies EVERY agent in the roster: the sweeper
    is not a peer of anyone, so it needs the whole population and then subtracts
    the agents its own container lane already covers.

    THE VERDICT IS IN THE PAYLOAD, NOT IN THE EXIT CODE (guard-1150). A blind
    scan prints `"blind": true` with a cause and still exits 0; an EMPTY stdout
    is the only "this told me nothing" signal a caller may act on.
    """
    import argparse

    ap = argparse.ArgumentParser(
        description="Classify every agent from the authoritative team-state shards")
    ap.add_argument("--world", default=None,
                    help="world dir (default: resolved from _paths)")
    ap.add_argument("--self", dest="self_agent", default="",
                    help="agent to exclude as self (default: none -- classify all)")
    ap.add_argument("--stale-hours", type=float, default=None,
                    help=f"heartbeat staleness threshold (default {DEFAULT_STALE_HOURS})")
    a = ap.parse_args(argv)

    world = a.world
    if not world:
        try:
            from _paths import WORLD_DIR  # type: ignore
            world = str(WORLD_DIR)
        except Exception as e:  # noqa: BLE001 -- a blind scan must SAY so
            print(json.dumps({
                "self": a.self_agent, "peers": [], "blind": True,
                "blind_cause": f"world dir unresolved: {type(e).__name__}: {e}"[:300],
                "roster_provenance": None,
            }))
            return 0
    report = scan(Path(world), a.self_agent, stale_hours_override=a.stale_hours)
    # Stamp the ALERTING decision on each peer HERE rather than letting an
    # out-of-loop caller re-derive it. `ALERTING` (only `stalled` pages) is this
    # module's contract; a host-side copy of that rule would be the second
    # decision table guard-2676 forbids, and it would drift the first time a
    # verdict is added.
    for peer in report.get("peers") or []:
        peer["alerting"] = is_alerting(peer.get("verdict"))
    print(json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
