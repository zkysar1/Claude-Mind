#!/usr/bin/env python3
"""Domain-phase + demand-first gate for the supply-side generation lanes.

Two questions, ONE component. Both generation lanes (`/generate-domain-goals`
and `create-aspiration`'s from-self composition) CALL this module; neither
transcribes its logic (guard-2676 — a second copy drifts silently and nothing
fails when it does).

  1. PHASE  -- is this candidate's work category valid for the domain's
               CURRENT declared phase? Work built for a phase that has already
               passed is waste, not supply.
  2. DEMAND -- is unconsumed actionable demand sitting on the board? Consuming
               demand outranks inventing supply, so generation defers while
               real requests go unanswered.

Both are FAIL-OPEN. A world that declares no calendar gets no gate at all: the
`domain-calendar` hook slot is opt-in (Pattern B, `domain-hooks.md`) and its
absence means "this domain has no phase model", never "refuse".

WHY A SCRIPT AND NOT A SKILL.md STEP (guard-399): an "LLM must consult the hook
at step N" instruction has no baseline anyone can run. This module is the bash
baseline; the SKILL.md steps are enrichment on top of it.

Calendar format -- one fenced ```yaml block anywhere in the slot file:

    phases:
      - id: planning
        starts: 2026-01-01          # optional; open-ended when omitted
        ends:   2026-03-31          # optional
        valid_categories:   [design, research]     # optional allow-list
        invalid_categories: [rollout]              # optional deny-list
    demand:
      actionable_types: [directive, escalation, question, request]
      max_unconsumed: 0

CLI
    py -3 core/scripts/generation_phase_gate.py phase-check --category <c> [--now ISO] [--json]
    py -3 core/scripts/generation_phase_gate.py demand-check --posts-file <jsonl> --author <a> [--json]
    py -3 core/scripts/generation_phase_gate.py resolve-phase [--now ISO] [--json]

Exit codes: 0 = allow / fail-open, 1 = refuse (phase) or defer (demand),
2 = usage error. Callers branch on the exit code; --json carries the detail.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SLOT_NAME = "domain-calendar"

# Default actionable set. MEASURED against the live board 2026-09-02 (12,147
# posts / 30 days, all four channels): `severity` appears on ZERO posts, so a
# floor keyed on it would be a phantom read (guard-159). `type` is present on
# 100% and separates demand from traffic -- these seven types total ~82 posts
# where `finding`/`claim`/`complete`/`status` total 11,377. A domain may
# override the set in its calendar's `demand.actionable_types`.
DEFAULT_ACTIONABLE_TYPES = (
    "directive",
    "escalation",
    "question",
    "request",
    "review-request",
    "decision-needed",
    "blocker",
)
DEFAULT_MAX_UNCONSUMED = 0

_FENCE_RE = re.compile(r"^[ \t]*```[ \t]*ya?ml[ \t]*$", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"^[ \t]*```[ \t]*$")


# ---------------------------------------------------------------- calendar --
def calendar_path() -> "Path | None":
    """Absolute path to this world's calendar slot, or None when unresolvable."""
    override = os.environ.get("GENERATION_CALENDAR_PATH")
    if override:
        return Path(override)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import WORLD_DIR  # type: ignore
    except Exception:
        return None
    if not WORLD_DIR:
        return None
    return Path(WORLD_DIR) / "conventions" / f"{SLOT_NAME}.md"


def parse_calendar(text: str) -> "dict | None":
    """Return the calendar mapping from the first ```yaml block that has phases.

    Returns None when no such block parses -- an unparseable calendar is
    treated exactly like an absent one (fail-open), never like a refusal.
    """
    try:
        import yaml  # type: ignore
    except Exception:
        return None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if _FENCE_RE.match(lines[i]):
            body = []
            i += 1
            while i < len(lines) and not _FENCE_END_RE.match(lines[i]):
                body.append(lines[i])
                i += 1
            try:
                got = yaml.safe_load("\n".join(body))
            except Exception:
                got = None
            if isinstance(got, dict) and isinstance(got.get("phases"), list):
                return got
        i += 1
    return None


def load_calendar(path: "Path | None" = None) -> "dict | None":
    p = path if path is not None else calendar_path()
    if p is None:
        return None
    try:
        if not p.is_file():
            return None
        return parse_calendar(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _as_dt(value) -> "datetime | None":
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:  # date objects from yaml.safe_load
        return datetime(value.year, value.month, value.day)
    except Exception:
        return None


def _norm(items) -> "set[str]":
    if not items:
        return set()
    if isinstance(items, str):
        items = [items]
    return {str(x).strip().lower() for x in items if str(x).strip()}


def resolve_phase(calendar: "dict | None", now: "datetime | None" = None) -> "dict | None":
    """First phase whose window contains `now`. None when none does.

    An open start or open end means unbounded on that side, so a domain can
    declare a single trailing phase without inventing an end date.
    """
    if not calendar:
        return None
    now = now or datetime.now()
    for phase in calendar.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        start = _as_dt(phase.get("starts") or phase.get("start"))
        end = _as_dt(phase.get("ends") or phase.get("end"))
        if start is not None and now < start:
            continue
        if end is not None and now > _end_of_day(end):
            continue
        return phase
    return None


def _end_of_day(dt: datetime) -> datetime:
    """A bare date as an END bound means the whole of that day, not 00:00."""
    if (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0):
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


# ------------------------------------------------------------- phase check --
def phase_check(category: str, calendar: "dict | None", now: "datetime | None" = None) -> dict:
    """Decide whether `category` is valid work for the domain's current phase."""
    if not calendar:
        return {
            "gate": "generation-phase-gate",
            "check": "phase",
            "decision": "fail-open",
            "reason": "no domain calendar declared (slot absent or unparseable)",
            "phase": None,
            "category": category,
        }
    phase = resolve_phase(calendar, now)
    if phase is None:
        return {
            "gate": "generation-phase-gate",
            "check": "phase",
            "decision": "fail-open",
            "reason": "calendar declares no phase covering the current time",
            "phase": None,
            "category": category,
        }
    pid = str(phase.get("id") or "<unnamed>")
    cat = (category or "").strip().lower()
    invalid = _norm(phase.get("invalid_categories"))
    valid = _norm(phase.get("valid_categories"))
    if cat and cat in invalid:
        return {
            "gate": "generation-phase-gate",
            "check": "phase",
            "decision": "refuse",
            "reason": f"category '{category}' is declared invalid during phase '{pid}'",
            "phase": pid,
            "category": category,
        }
    if valid and cat not in valid:
        return {
            "gate": "generation-phase-gate",
            "check": "phase",
            "decision": "refuse",
            "reason": (
                f"phase '{pid}' declares an allow-list "
                f"({', '.join(sorted(valid))}) and '{category}' is not on it"
            ),
            "phase": pid,
            "category": category,
        }
    return {
        "gate": "generation-phase-gate",
        "check": "phase",
        "decision": "allow",
        "reason": f"category '{category}' is valid during phase '{pid}'",
        "phase": pid,
        "category": category,
    }


# ------------------------------------------------------------ demand check --
def _author_agent(author: str) -> str:
    """`<agent>@<env-id>` -> `<agent>`; a bare name is returned unchanged."""
    return (author or "").split("@", 1)[0].strip().lower()


def demand_check(posts, author: str, calendar: "dict | None" = None) -> dict:
    """Defer generation while unconsumed actionable demand sits on the board.

    A post counts as unconsumed demand when its `type` is in the actionable
    set, it was not written by this agent, and this agent has posted no reply
    to it. Every predicate keys on a field that EXISTS on live posts
    (`type`, `author`, `reply_to`, `id` -- all 12,147/12,147) rather than on
    the absent `severity` (guard-159).

    LIMITATION, stated rather than hidden: consumption is judged only from the
    posts the CALLER passed in. A reply that falls outside the caller's window
    is invisible here, so a long-answered post can read unconsumed. If a defer
    looks wrong, widen the `--since` window before treating it as a finding.
    """
    cfg = (calendar or {}).get("demand") or {}
    actionable = _norm(cfg.get("actionable_types")) or set(DEFAULT_ACTIONABLE_TYPES)
    try:
        max_unconsumed = int(cfg.get("max_unconsumed", DEFAULT_MAX_UNCONSUMED))
    except (TypeError, ValueError):
        max_unconsumed = DEFAULT_MAX_UNCONSUMED
    me = _author_agent(author)

    replied_to = {
        str(p.get("reply_to"))
        for p in posts
        if isinstance(p, dict)
        and p.get("reply_to")
        and (not me or _author_agent(str(p.get("author", ""))) == me)
    }
    outstanding = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        if str(p.get("type", "")).strip().lower() not in actionable:
            continue
        if me and _author_agent(str(p.get("author", ""))) == me:
            continue
        if str(p.get("id")) in replied_to:
            continue
        outstanding.append(
            {
                "id": p.get("id"),
                "type": p.get("type"),
                "author": p.get("author"),
                "timestamp": p.get("timestamp"),
                "text": (str(p.get("text", "")) or "")[:160],
            }
        )
    decision = "defer" if len(outstanding) > max_unconsumed else "allow"
    return {
        "gate": "generation-phase-gate",
        "check": "demand",
        "decision": decision,
        "reason": (
            f"{len(outstanding)} unconsumed actionable post(s) > max_unconsumed"
            f"={max_unconsumed}; consume demand before inventing supply"
            if decision == "defer"
            else f"{len(outstanding)} unconsumed actionable post(s) <= max_unconsumed={max_unconsumed}"
        ),
        "unconsumed_count": len(outstanding),
        "max_unconsumed": max_unconsumed,
        "actionable_types": sorted(actionable),
        "outstanding": outstanding[:20],
    }


def _read_posts(path: "str | None"):
    """Read board-read.sh --json output (JSONL, one post per line)."""
    if not path or path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    posts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            posts.append(obj)
        elif isinstance(obj, list):
            posts.extend(x for x in obj if isinstance(x, dict))
    return posts


# --------------------------------------------------------------------- CLI --
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("phase-check", "demand-check", "resolve-phase"):
        p = sub.add_parser(name)
        p.add_argument("--calendar", default=None, help="calendar slot path override")
        p.add_argument("--json", action="store_true")
        if name == "phase-check":
            p.add_argument("--category", required=True)
            p.add_argument("--now", default=None)
        elif name == "resolve-phase":
            p.add_argument("--now", default=None)
        else:
            p.add_argument("--posts-file", default="-")
            p.add_argument("--author", default=os.environ.get("MIND_AGENT", ""))

    args = ap.parse_args(argv)
    cal_path = Path(args.calendar) if args.calendar else None
    calendar = load_calendar(cal_path)
    now = _as_dt(getattr(args, "now", None))

    if args.cmd == "resolve-phase":
        phase = resolve_phase(calendar, now)
        out = {
            "gate": "generation-phase-gate",
            "check": "resolve",
            "decision": "allow" if phase else "fail-open",
            "phase": (str(phase.get("id")) if phase else None),
            "calendar_present": calendar is not None,
        }
        rc = 0
    elif args.cmd == "phase-check":
        out = phase_check(args.category, calendar, now)
        rc = 1 if out["decision"] == "refuse" else 0
    else:
        out = demand_check(_read_posts(args.posts_file), args.author, calendar)
        rc = 1 if out["decision"] == "defer" else 0

    if args.json:
        print(json.dumps(out))
    else:
        print(f"{out['decision']}: {out.get('reason', '')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
