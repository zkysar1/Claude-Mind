#!/usr/bin/env python3
"""completion_digest.py -- the USER-FACING digest that the completion report emails.

Why this exists (user, 2026-08-17): "I do like receiving what goals are blocked or
assigned to me through the completion report email ... make them easier to read,
and be sure everything is in there I need to quickly understand how it has been
going ... as long as I receive one every day or two that is good."

The on-disk COMPLETION-REPORT.md is written BY an agent FOR agents -- forensic,
trap-numbered, thousands of words before the first fact the user cares about,
and it never LISTS the goals that need him (it says "52 goals carry `user`").
This script builds the email instead: short, deterministic, ordered by what the
reader needs first, with the specific items named. Framework scripts read the
stores here; the LLM never does. Domain-free: no transport, no product names.

Order (the asks first -- the user's 2026-08-03 feedback on the digest lane was
that an email opening with our own archaeology "caused anxiety"):

  1. TL;DR            5 lines max
  2. Needs you        goals with `user` in participants (SSOT predicate shared
                      with audit-user-to-agent.py + the 72h digest), human-gated
                      defers, and open pending questions -- with the NEEDS FROM
                      YOU line and age, oldest first
  3. Blocked          what is stuck, why, and what it holds up
  4. Done             this window, by agent and by aspiration
  5. In progress      active aspirations, progress fractions
  6. Outcome + health fleet pulse, hypotheses, product signals (when configured)
  7. Notes            optional agent-written lines (--notes-file), bounded

Usage:
  completion-digest.sh --agent alpha [--since ISO] [--notes-file F] [--out F]
                       [--world DIR] [--max-items N] [--json]
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _paths import WORLD_DIR, PROJECT_ROOT, agents_root  # noqa: E402

TERMINAL = {"completed", "skipped", "expired", "archived", "retired"}
BATCH_CLOSE_MIN = 30  # >= this many closes by one session inside ~10 min = a batch close


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def _ts(s):
    try:
        return datetime.fromisoformat(str(s)[:19])
    except Exception:
        return None


def _hours(a: datetime | None, b: datetime | None):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 1)


def _age_str(h) -> str:
    if h is None:
        return "age unknown"
    if h < 48:
        return f"{int(h)}h"
    return f"{int(h // 24)}d"


def _load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    try:
        from _fileops import read_jsonl_with_recovery  # noqa: WPS433
        return list(read_jsonl_with_recovery(path) or [])
    except Exception:
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out


def _bash(script: str, *args: str, timeout: int = 60):
    try:
        from _runtime_bash import bash_cmd  # noqa: WPS433
        return subprocess.run(bash_cmd(script, *args), capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def load_population_predicate():
    """Import audit-user-to-agent._find_user_participant_goals -- the SSOT for
    'goals that need the user' (guard-1802: a second copy of this predicate is
    how the narrow-predicate hole appeared). Fail-open to None."""
    target = HERE / "audit-user-to-agent.py"
    try:
        spec = importlib.util.spec_from_file_location("_aut_population", target)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return getattr(mod, "_find_user_participant_goals", None)
    except Exception:
        return None


def gather(world: Path, agent: str, since: datetime | None, now: datetime, max_items: int) -> dict:
    asp_path = world / "aspirations.jsonl"
    asps = _load_jsonl(asp_path)
    agent_files = sorted(Path(p) for p in glob.glob(str(agents_root() / "*" / "aspirations.jsonl")))

    # ---- completed in window (world queue) --------------------------------
    done = []
    active_asps = []
    for asp in asps:
        goals = asp.get("goals") or []
        total = len(goals)
        n_done = sum(1 for g in goals if g.get("status") == "completed")
        if asp.get("status") in ("active", None):
            active_asps.append({"id": asp.get("id"), "title": asp.get("title") or "", "done": n_done, "total": total,
                                "window_done": 0})
        for g in goals:
            if g.get("status") != "completed":
                continue
            ct = _ts(g.get("completed_at"))
            if since and (not ct or ct < since):
                continue
            if not since and not ct:
                continue
            done.append({"id": g.get("id"), "asp": asp.get("id"), "asp_title": asp.get("title") or "",
                         "title": g.get("title") or "", "by": g.get("completed_by") or g.get("executed_by") or "?",
                         "deep": (g.get("outcome_class") == "deep"), "at": ct.isoformat() if ct else "",
                         "sid": str(g.get("completed_by_sid") or "")[:8], "batch": False})
    for a in active_asps:
        a["window_done"] = sum(1 for d in done if d["asp"] == a["id"])
    # Batch closes: the reducer formally closing many worker-executed goals in one
    # sweep stamps them all with the SAME session + a few minutes -- real work, but
    # done EARLIER; counting it as "today's throughput" misleads the reader
    # (measured 2026-08-16: 220 of 499 window closes landed in one 16:22 sweep).
    by_sid = {}
    for d in done:
        if d["sid"] and d["at"]:
            by_sid.setdefault(d["sid"], []).append(d)
    batches = []
    for sid, items in by_sid.items():
        items.sort(key=lambda d: d["at"])
        cluster = [items[0]]
        for d in items[1:] + [None]:
            prev = _ts(cluster[-1]["at"])
            cur = _ts(d["at"]) if d else None
            if d and prev and cur and (cur - prev) <= timedelta(minutes=10):
                cluster.append(d)
                continue
            if len(cluster) >= BATCH_CLOSE_MIN:
                for c in cluster:
                    c["batch"] = True
                batches.append({"by": cluster[0]["by"], "at": cluster[0]["at"][:16],
                                "until": cluster[-1]["at"][11:16], "n": len(cluster)})
            if d:
                cluster = [d]

    # recurring firings (agent queues) in window
    recurring = 0
    for f in agent_files:
        for asp in _load_jsonl(f):
            for g in asp.get("goals") or []:
                if not g.get("recurring"):
                    continue
                la = _ts(g.get("lastAchievedAt") or g.get("last_achieved_at"))
                if la and (not since or la >= since):
                    recurring += 1

    # ---- needs you ----------------------------------------------------------
    pred = load_population_predicate()
    needs = []
    seen = set()
    if pred:
        sources = [("world", asp_path)] + [(f.parent.name, f) for f in agent_files]
        for label, path in sources:
            try:
                cands = pred(label, path)
            except Exception:
                cands = []
            for c in cands:
                g = c.get("goal") or {}
                gid = g.get("id")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                created = _ts(g.get("created_at") or g.get("created"))
                needs.append({"id": gid, "title": g.get("title") or "", "asp": c.get("aspiration_id"),
                              "scope": (g.get("user_leg_scope") or "").strip(),
                              "age_h": _hours(created, now), "kind": "assigned to you",
                              "new": bool(created and since and created >= since),
                              "deliberate": bool(c.get("deliberate")), "priority": g.get("priority") or ""})
    # human-gated defers (defer_reason 'human_blocked:' prefix) not already listed
    for asp in asps:
        if asp.get("status") in TERMINAL:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL:
                continue
            dr = str(g.get("defer_reason") or "")
            if dr.lower().startswith("human_blocked") and g.get("id") not in seen:
                seen.add(g.get("id"))
                created = _ts(g.get("created_at") or g.get("created"))
                needs.append({"id": g.get("id"), "title": g.get("title") or "", "asp": asp.get("id"),
                              "scope": _clip(dr.split(":", 1)[-1], 140), "age_h": _hours(created, now),
                              "kind": "human-gated", "deliberate": False, "priority": g.get("priority") or "",
                              "new": bool(created and since and created >= since)})
    needs.sort(key=lambda x: -(x["age_h"] or 0))

    # pending questions (fleet)
    pqs = []
    r = _bash(str(HERE / "pending-questions-read.sh"), "--all-agents", timeout=90)
    if r and r.returncode == 0 and r.stdout.strip():
        try:
            rows = json.loads(r.stdout)
            for q in rows:
                if str(q.get("status", "")).lower() != "pending":
                    continue
                d = _ts(q.get("date") or q.get("created"))
                pqs.append({"id": q.get("id"), "agent": q.get("agent") or "", "age_h": _hours(d, now),
                            "question": q.get("question") or "", "default_action": q.get("default_action") or ""})
        except Exception:
            pqs = []
    pqs.sort(key=lambda x: (x["agent"] != agent, -(x["age_h"] or 0)))

    # ---- blocked ----------------------------------------------------------------
    blocked = []
    downstream = {}
    for asp in asps:
        if asp.get("status") in TERMINAL:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL:
                continue
            bb = g.get("blocked_by")
            if isinstance(bb, str):
                bb = [bb]
            for dep in bb or []:
                downstream[str(dep)] = downstream.get(str(dep), 0) + 1
    for asp in asps:
        if asp.get("status") in TERMINAL:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL:
                continue
            cause = None
            dr = str(g.get("defer_reason") or "")
            if g.get("status") == "blocked":
                cause = "blocked" + (f": {_clip(dr, 90)}" if dr else "")
            elif g.get("blocker_ref"):
                cause = f"blocker {g.get('blocker_ref')}"
            elif dr:
                cause = "deferred: " + _clip(dr, 90)
            elif g.get("blocked_by"):
                bb = g.get("blocked_by")
                cause = "waits on " + ", ".join(map(str, bb if isinstance(bb, list) else [bb]))[:60]
            if not cause:
                continue
            blocked.append({"id": g.get("id"), "title": g.get("title") or "", "asp": asp.get("id"), "cause": cause,
                            "downstream": downstream.get(str(g.get("id")), 0),
                            "owner": g.get("claimed_by") or g.get("intended_agent") or "-",
                            "human": dr.lower().startswith("human_blocked")})
    blocked_total = len(blocked)
    by_cause = {}
    for b in blocked:
        k = b["cause"].split(":")[0].split(" ")[0]
        by_cause[k] = by_cause.get(k, 0) + 1
    blocked.sort(key=lambda b: (-b["downstream"], b["human"], b["id"] or ""))

    # ---- hypotheses ------------------------------------------------------------
    hyp = {}
    r = _bash(str(HERE / "pipeline-read.sh"), "--accuracy", timeout=60)
    if r and r.returncode == 0:
        try:
            a = json.loads(r.stdout)
            hyp = {"lifetime_pct": a.get("accuracy_pct"), "resolved": a.get("total_resolved")}
        except Exception:
            hyp = {}
    win_conf = win_corr = 0
    corrected = []
    r = _bash(str(HERE / "pipeline-read.sh"), "--stage", "resolved", timeout=60)
    if r and r.returncode == 0:
        try:
            for rec in json.loads(r.stdout):
                od = _ts(rec.get("outcome_date") or rec.get("resolved_at"))
                if since and (not od or od < since - timedelta(days=1)):
                    continue
                o = str(rec.get("outcome") or "").upper()
                if o == "CONFIRMED":
                    win_conf += 1
                elif o == "CORRECTED":
                    win_corr += 1
                    corrected.append({"id": rec.get("id"), "title": rec.get("title") or rec.get("claim") or "",
                                      "at": (od.isoformat() if od else "")[:10]})
        except Exception:
            pass
    corrected.sort(key=lambda c: c["at"], reverse=True)
    hyp.update({"window_confirmed": win_conf, "window_corrected": win_corr, "corrected": corrected[:5]})

    # ---- fleet pulse -----------------------------------------------------------
    pulse = []
    r = _bash(str(HERE / "team-state-read.sh"), "--json", timeout=60)
    if r and r.returncode == 0:
        try:
            st = json.loads(r.stdout).get("agent_status") or {}
            for name, row in sorted(st.items()):
                la = _ts(row.get("last_active"))
                inf = row.get("in_flight") if isinstance(row.get("in_flight"), dict) else {}
                pulse.append({"agent": name, "age_h": _hours(la, now),
                              "in_flight": inf.get("goal_id"), "in_flight_title": inf.get("title") or ""})
        except Exception:
            pulse = []

    # ---- outcome signals -------------------------------------------------------
    outcome = {}
    om = world / "outcome-metrics.yaml"
    if om.exists():
        try:
            import yaml  # noqa: WPS433
            outcome = yaml.safe_load(om.read_text(encoding="utf-8")) or {}
        except Exception:
            outcome = {}

    # ---- spend (domain hook slot: world/scripts/digest-cost.sh) ------------------
    cost = _cost_from_hook(world, agent, since, now)

    return {"done": done, "batches": batches, "recurring": recurring, "needs": needs, "pqs": pqs, "blocked": blocked,
            "blocked_total": blocked_total, "by_cause": by_cause, "active_asps": active_asps, "hyp": hyp,
            "pulse": pulse, "outcome": outcome, "cost": cost}


COST_SLOT = "scripts/digest-cost.sh"


def _cost_from_hook(world: Path, agent: str, since: datetime | None, now: datetime) -> dict:
    """Ask the domain what things cost (see domain-hooks.md `digest-cost`).
    Contract: JSON object on stdout with optional headline/tiles/lines/note/
    as_of/stale. Any failure => {} and the Spend card is omitted; the digest
    never fails on cost."""
    slot = Path(world) / COST_SLOT
    if not slot.exists():
        return {}
    try:
        from _runtime_bash import bash_cmd  # noqa: WPS433
        env = dict(os.environ, DIGEST_SINCE=since.isoformat() if since else "", DIGEST_NOW=now.isoformat(),
                   DIGEST_AGENT=agent)
        r = subprocess.run(bash_cmd(str(slot)), capture_output=True, text=True, timeout=60, env=env)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        d = json.loads(r.stdout)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def render(data: dict, *, agent: str, since: datetime | None, now: datetime, notes: str, max_items: int) -> str:
    L = []
    win_h = _hours(since, now) if since else None
    win_label = f"last {int(win_h)}h ({since:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M} UTC)" if since else "lifetime"
    done, needs, pqs, blocked = data["done"], data["needs"], data["pqs"], data["blocked"]
    deep = sum(1 for d in done if d["deep"])
    quiet = [p["agent"] for p in data["pulse"] if p["age_h"] is not None and p["age_h"] > 6]

    L.append(f"# Fleet digest — {now:%Y-%m-%d} (from {agent})")
    L.append(f"Window: {win_label}")
    L.append("")
    L.append("## TL;DR")
    organic = [d for d in done if not d["batch"]]
    n_batch = len(done) - len(organic)
    rate = f" (~{round(len(organic) / (win_h / 24), 1)}/day)" if win_h and win_h > 0 else ""
    by_agent = {}
    for d in organic:
        by_agent[d["by"]] = by_agent.get(d["by"], 0) + 1
    line = f"- Done: **{len(organic)}** goals{rate}, {sum(1 for d in organic if d['deep'])} deep"
    if by_agent:
        line += " — " + ", ".join(f"{('unattributed' if k == '?' else k)} {v}" for k, v in sorted(by_agent.items(), key=lambda x: -x[1]))
    if data["recurring"]:
        line += f"; +{data['recurring']} recurring sweeps"
    L.append(line)
    if n_batch:
        L.append(f"- Also **{n_batch}** batch-closed: " + "; ".join(f"{b['n']} by {b['by']} on {b['at'][:10]} {b['at'][11:16]}–{b['until']}" for b in data["batches"]) + " (work done earlier, formally closed in one sweep — not today's throughput)")
    L.append(f"- Needs you: **{len(needs)}** goal(s) + **{len(pqs)}** open question(s) — listed below")
    L.append(f"- Blocked: **{data['blocked_total']}** goal(s)" + (" (" + ", ".join(f"{v} {k}" for k, v in sorted(data['by_cause'].items(), key=lambda x: -x[1])) + ")" if data["by_cause"] else ""))
    h = data["hyp"]
    if h:
        L.append(f"- Learning: {h.get('window_confirmed', 0)} hypotheses confirmed / {h.get('window_corrected', 0)} corrected this window; lifetime accuracy {h.get('lifetime_pct', '?')}% over {h.get('resolved', '?')}")
    if data["pulse"]:
        L.append("- Fleet: " + ", ".join(f"{p['agent']} {'active' if (p['age_h'] is not None and p['age_h'] <= 6) else ('quiet ' + _age_str(p['age_h']))}" for p in data["pulse"]))
    n_new = sum(1 for n in needs if n.get("new"))
    if n_new:
        L.append(f"- New asks this window: **{n_new}** (marked NEW below)")
    L.append("")

    # ---- needs you
    L.append(f"## Needs you ({len(needs)} goals, {len(pqs)} questions)")
    if not needs and not pqs:
        L.append("Nothing is waiting on you right now.")
    for i, n in enumerate(needs[:max_items], 1):
        tag = "human-gated" if n["kind"] == "human-gated" else ("deliberately parked with you" if n["deliberate"] else "assigned to you")
        L.append(f"{i}. **{n['id']}**{' NEW' if n.get('new') else ''} {_clip(n['title'], 90)}  _( {tag}, {_age_str(n['age_h'])} old, {n['asp']} )_")
        L.append(f"   NEEDS FROM YOU: {n['scope'] or 'not recorded on the goal (our bug — reply and we will fix it)'}")
    if len(needs) > max_items:
        rest = needs[max_items:]
        L.append(f"   Also waiting ({len(rest)} more, oldest first):")
        for n in rest[:30]:
            L.append(f"   - {n['id']} {_clip(n['title'], 70)} ({_age_str(n['age_h'])}{', ' + _clip(n['scope'], 40) if n['scope'] else ''})")
        if len(rest) > 30:
            L.append(f"   - … +{len(rest) - 30} more")
    if pqs:
        L.append("")
        L.append("Open questions (each was already acted on with the stated default — override if you disagree):")
        for q in pqs[:max_items]:
            L.append(f"- **{q['id']}** ({q['agent']}, {_age_str(q['age_h'])}): {_clip(q['question'], 220)}")
            if q["default_action"]:
                L.append(f"  default taken: {_clip(q['default_action'], 160)}")
        if len(pqs) > max_items:
            L.append(f"- … +{len(pqs) - max_items} more (`/open-questions`)")
    L.append("")

    # ---- blocked
    L.append(f"## Blocked ({data['blocked_total']})")
    if not blocked:
        L.append("Nothing blocked.")
    for b in blocked[:max_items]:
        holds = f" → holds up {b['downstream']} goal(s)" if b["downstream"] else ""
        L.append(f"- **{b['id']}** {_clip(b['title'], 80)}{holds} — {b['cause']} (owner: {b['owner']})")
    if len(blocked) > max_items:
        L.append(f"- … +{len(blocked) - max_items} more")
    L.append("")

    # ---- done
    L.append(f"## Done this window ({len(organic)}" + (f" + {n_batch} batch-closed" if n_batch else "") + ")")
    if not done:
        L.append("No goals completed in this window.")
    by_asp = {}
    for d in done:
        by_asp.setdefault(d["asp"], []).append(d)
    for asp_id, items in sorted(by_asp.items(), key=lambda kv: -len(kv[1]))[:8]:
        items = sorted(items, key=lambda d: (d["batch"], not d["deep"], d["at"]))
        L.append(f"**{asp_id} — {_clip(items[0]['asp_title'], 60)}** ({len(items)})")
        for d in items[:4]:
            L.append(f"  - {d['id']} {_clip(d['title'], 85)} ({d['by']}{', deep' if d['deep'] else ''}{', batch-closed' if d['batch'] else ''})")
        if len(items) > 4:
            L.append(f"  - … +{len(items) - 4} more")
    if len(by_asp) > 8:
        L.append(f"… and {len(by_asp) - 8} more aspirations touched")
    L.append("")

    # ---- in progress
    act = [a for a in data["active_asps"] if a["total"]]
    act.sort(key=lambda a: (-a["window_done"], -(a["done"] / a["total"])))
    L.append("## In progress")
    for a in act[:8]:
        pct = int(100 * a["done"] / a["total"]) if a["total"] else 0
        L.append(f"- {a['id']} {_clip(a['title'], 60)}: {a['done']}/{a['total']} ({pct}%)" + (f", +{a['window_done']} this window" if a["window_done"] else ""))
    L.append("")

    # ---- learning: what we got wrong
    corr = (data.get("hyp") or {}).get("corrected") or []
    if corr:
        L.append("## What we got wrong (hypotheses corrected this window)")
        for c in corr:
            L.append(f"- {c['at']} {_clip(c['title'], 120)}")
        L.append("")

    # ---- right now
    if data["pulse"]:
        L.append("## Each agent right now")
        for p in data["pulse"]:
            state = "active" if (p["age_h"] is not None and p["age_h"] <= 6) else f"quiet {_age_str(p['age_h'])}"
            on = f" — on {p['in_flight']} {_clip(p['in_flight_title'], 70)}" if p.get("in_flight") else " — between goals"
            L.append(f"- {p['agent']}: {state}{on}")
        L.append("")

    # ---- spend
    cost = data.get("cost") or {}
    if cost:
        L.append("## Spend" + (f" — {cost['headline']}" if cost.get("headline") else ""))
        for t in cost.get("tiles") or []:
            L.append(f"- {t.get('label', '')}: **{t.get('value', '')}**" + (f" ({t['sub']})" if t.get("sub") else ""))
        for line in cost.get("lines") or []:
            L.append(f"- {line}")
        if cost.get("note"):
            L.append(f"  _{cost['note']}_")
        if cost.get("as_of"):
            L.append(f"  (as of {cost['as_of']}{'; STALE' if cost.get('stale') else ''})")
        L.append("")

    # ---- outcome + health
    L.append("## Product signals")
    src = (data["outcome"] or {}).get("sources") or {}
    if not src:
        L.append("- no outcome signal configured (outcome-observation hook)")
    else:
        ci = src.get("ci") or {}
        if ci:
            L.append(f"- CI: {ci.get('runs_passed', '?')}/{ci.get('runs_total', '?')} runs passed" + (f" (pass rate {ci.get('pass_rate')})" if ci.get("pass_rate") is not None else ""))
        op = src.get("operator") or {}
        if op:
            L.append(f"- Service: {'reachable' if op.get('reachable') else 'UNREACHABLE'} ({op.get('status', '?')})")
        gt = src.get("git") or {}
        if gt:
            L.append(f"- Git: product estate {'present' if gt.get('prod_repo_present') else 'absent'} on the reporting box ({gt.get('status', '?')})")
        L.append(f"  (as of {(data['outcome'] or {}).get('updated_at', '?')})")
    L.append("")

    if notes.strip():
        L.append("## Notes from the agent")
        for line in notes.strip().splitlines()[:12]:
            L.append(line.rstrip())
        L.append("")

    L.append("---")
    L.append(f"Full agent-side report: agents/{agent}/COMPLETION-REPORT.md (git history is the archive). "
             "Reply to this email to reach the fleet mailbox.")
    return "\n".join(L) + "\n"



# --------------------------------------------------------------------------
# render (HTML email)
# --------------------------------------------------------------------------

import html as _html  # noqa: E402


def _e(x) -> str:
    return _html.escape(str(x if x is not None else ""))


def _pill(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;'
            f'font-weight:600;color:#fff;background:{color};vertical-align:middle">{_e(text)}</span>')


def _card(title: str, inner: str, border: str = "#1e90ff", bg: str = "#fff") -> str:
    return (f'<div style="margin:0 0 18px;border-left:4px solid {border};background:{bg};'
            f'border-radius:6px;padding:14px 16px">'
            f'<h2 style="margin:0 0 10px;font-size:17px;color:#222">{_e(title)}</h2>{inner}</div>')


def _tile(label: str, value: str, sub: str = "", color: str = "#222") -> str:
    return (f'<td style="padding:8px 10px;vertical-align:top;border:1px solid #eee;background:#fafafa;min-width:90px">'
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#888">{_e(label)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{color};line-height:1.2">{_e(value)}</div>'
            f'<div style="font-size:12px;color:#666">{_e(sub)}</div></td>')


TD = 'style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4"'
TH = 'style="padding:6px 8px;border-bottom:2px solid #ddd;text-align:left;font-size:12px;color:#666;text-transform:uppercase"'


def render_html(data: dict, *, agent: str, since: datetime | None, now: datetime, notes: str, max_items: int) -> str:
    """Email-safe HTML twin of render(): inline styles, tables, no scripts, no
    remote assets, single 680px column. Same data, same order, same numbers."""
    done, needs, pqs, blocked = data["done"], data["needs"], data["pqs"], data["blocked"]
    organic = [d for d in done if not d["batch"]]
    n_batch = len(done) - len(organic)
    win_h = _hours(since, now) if since else None
    by_agent = {}
    for d in organic:
        k = "unattributed" if d["by"] == "?" else d["by"]
        by_agent[k] = by_agent.get(k, 0) + 1
    h = data.get("hyp") or {}
    n_new = sum(1 for n in needs if n.get("new"))
    quiet = [p["agent"] for p in data["pulse"] if p["age_h"] is None or p["age_h"] > 6]

    out = []
    out.append(f'<html><head><meta name="viewport" content="width=device-width, initial-scale=1.0">'
               f'<title>{_e("Fleet digest — " + now.strftime("%Y-%m-%d"))}</title></head>')
    out.append('<body style="margin:0;padding:0;background:#f4f4f4;font-family:-apple-system,BlinkMacSystemFont,'
               "'Segoe UI',Roboto,Arial,sans-serif;font-size:14px;line-height:1.5;color:#333\">")
    out.append('<div style="max-width:680px;margin:0 auto;padding:16px">')
    out.append('<div style="background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">')
    # header
    win_txt = (f'{since.strftime("%Y-%m-%d %H:%M")} → {now.strftime("%Y-%m-%d %H:%M")} UTC · last {int(win_h)}h'
               if since and win_h is not None else f'as of {now.strftime("%Y-%m-%d %H:%M")} UTC')
    out.append(f'<div style="padding:20px 22px 14px;border-bottom:2px solid #1e90ff">'
               f'<h1 style="margin:0;font-size:22px;color:#222">Fleet digest — {_e(now.strftime("%Y-%m-%d"))}</h1>'
               f'<p style="margin:6px 0 0;font-size:12px;color:#888">{_e(win_txt)} · from {_e(agent)}</p></div>')
    out.append('<div style="padding:18px 22px">')

    # ---- TL;DR tiles
    tiles = []
    rate = f"~{round(len(organic) / (win_h / 24), 1)}/day" if win_h else ""
    tiles.append(_tile("Done", str(len(organic)), f"{sum(1 for d in organic if d['deep'])} deep · {rate}", "#28a745"))
    tiles.append(_tile("Needs you", str(len(needs)), f"+{len(pqs)} open questions" + (f" · {n_new} new" if n_new else ""),
                       "#fd7e14" if needs else "#28a745"))
    tiles.append(_tile("Blocked", str(data["blocked_total"]),
                       ", ".join(f"{v} {k}" for k, v in sorted(data["by_cause"].items(), key=lambda x: -x[1])[:3]), "#dc3545" if data["blocked_total"] else "#28a745"))
    if h:
        acc = h.get("lifetime_pct")
        tiles.append(_tile("Learning", f"{h.get('window_confirmed', 0)}✓ {h.get('window_corrected', 0)}✗",
                           f"lifetime {acc}% of {h.get('resolved', '?')}" if acc is not None else "hypotheses this window"))
    if data["pulse"]:
        tiles.append(_tile("Fleet", f"{len(data['pulse']) - len(quiet)}/{len(data['pulse'])}",
                           "all active" if not quiet else "quiet: " + ", ".join(quiet), "#28a745" if not quiet else "#fd7e14"))
    tl = '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%"><tr>' + "".join(tiles) + "</tr></table>"
    extra = []
    if by_agent:
        extra.append("Done by agent: " + ", ".join(f"<b>{_e(k)}</b> {v}" for k, v in sorted(by_agent.items(), key=lambda x: -x[1])))
    if n_batch:
        extra.append(f"Also <b>{n_batch}</b> batch-closed (" + "; ".join(f"{b['n']} by {_e(b['by'])} {_e(b['at'][:10])} {_e(b['at'][11:16])}–{_e(b['until'])}" for b in data["batches"])
                     + ") — work done earlier, formally closed in one sweep; not counted above.")
    if data["recurring"]:
        extra.append(f"+{data['recurring']} recurring sweeps ran.")
    if extra:
        tl += '<p style="margin:10px 0 0;font-size:12px;color:#555">' + " &nbsp;·&nbsp; ".join(extra) + "</p>"
    out.append(_card("TL;DR", tl))

    # ---- Needs you
    if not needs and not pqs:
        inner = '<p style="margin:0;color:#28a745">Nothing is waiting on you right now.</p>'
    else:
        rows = []
        for i, n in enumerate(needs[:max_items], 1):
            tag = ("human-gated" if n["kind"] == "human-gated" else ("parked with you" if n["deliberate"] else "assigned to you"))
            pills = _pill(tag, "#6c757d") + (" " + _pill("NEW", "#1e90ff") if n.get("new") else "")
            scope = n["scope"] or '<i style="color:#999">not recorded on the goal — our bug, reply and we will fix it</i>'
            if n["scope"]:
                scope = _e(scope)
            rows.append(f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;color:#999;font-size:12px">{i}</td>'
                        f'<td {TD}><b>{_e(n["id"])}</b> {pills}<br>{_e(_clip(n["title"], 110))}'
                        f'<div style="font-size:12px;color:#c25400;margin-top:2px"><b>Needs from you:</b> {scope}</div></td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap;font-size:12px;color:#666">{_e(_age_str(n["age_h"]))}<br>{_e(n["asp"] or "")}</td></tr>')
        inner = ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">'
                 f'<tr><th {TH}>#</th><th {TH}>Goal · what it needs from you</th><th {TH}>Age</th></tr>' + "".join(rows) + "</table>")
        rest = needs[max_items:]
        if rest:
            items = "".join(f'<li>{_e(n["id"])}{" " + _pill("NEW", "#1e90ff") if n.get("new") else ""} {_e(_clip(n["title"], 80))} '
                            f'<span style="color:#888">({_e(_age_str(n["age_h"]))}{", " + _e(_clip(n["scope"], 40)) if n["scope"] else ""})</span></li>'
                            for n in rest[:30])
            more = f'<li>… +{len(rest) - 30} more</li>' if len(rest) > 30 else ""
            inner += (f'<details style="margin-top:8px"><summary style="cursor:pointer;color:#1e90ff;font-size:13px">Also waiting — {len(rest)} more (oldest first)</summary>'
                      f'<ul style="margin:6px 0 0;padding-left:18px;font-size:12px;line-height:1.5">{items}{more}</ul></details>')
        if pqs:
            qs = []
            for q in pqs[:max_items]:
                qs.append(f'<li style="margin-bottom:8px"><b>{_e(q["id"])}</b> <span style="color:#888">({_e(q["agent"])}, {_e(_age_str(q["age_h"]))})</span><br>'
                          f'{_e(_clip(q["question"], 260))}'
                          + (f'<div style="font-size:12px;color:#2a7d2a;margin-top:2px"><b>Default taken:</b> {_e(_clip(q["default_action"], 200))}</div>' if q["default_action"] else "")
                          + "</li>")
            inner += (f'<h3 style="margin:14px 0 6px;font-size:14px;color:#444">Open questions ({len(pqs)}) — each already acted on with the stated default; override if you disagree</h3>'
                      f'<ul style="margin:0;padding-left:18px;font-size:13px">{"".join(qs)}</ul>')
    out.append(_card(f"Needs you ({len(needs)} goals, {len(pqs)} questions)", inner, "#fd7e14", "#fffaf5"))

    # ---- Blocked
    if not blocked:
        inner = '<p style="margin:0;color:#28a745">Nothing blocked.</p>'
    else:
        rows = []
        for b in blocked[:max_items]:
            holds = f'<br><span style="color:#dc3545;font-size:12px">holds up {b["downstream"]} goal(s)</span>' if b["downstream"] else ""
            rows.append(f'<tr><td {TD}><b>{_e(b["id"])}</b> {_e(_clip(b["title"], 90))}{holds}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;color:#555">{_e(_clip(b["cause"], 120))}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;color:#888;white-space:nowrap">{_e(b["owner"])}</td></tr>')
        inner = ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">'
                 f'<tr><th {TH}>Goal</th><th {TH}>Why</th><th {TH}>Owner</th></tr>' + "".join(rows) + "</table>")
        if len(blocked) > max_items:
            inner += f'<p style="margin:6px 0 0;font-size:12px;color:#888">… +{len(blocked) - max_items} more (sorted by how much each holds up)</p>'
    out.append(_card(f"Blocked ({data['blocked_total']})", inner, "#dc3545", "#fff8f8"))

    # ---- Done
    if not done:
        inner = '<p style="margin:0;color:#888">No goals completed in this window.</p>'
    else:
        by_asp = {}
        for d in done:
            by_asp.setdefault(d["asp"], []).append(d)
        parts = []
        for asp_id, items in sorted(by_asp.items(), key=lambda kv: -len(kv[1]))[:8]:
            items = sorted(items, key=lambda d: (d["batch"], not d["deep"], d["at"]))
            org = sum(1 for d in items if not d["batch"])
            cnt = f"{org}" + (f" + {len(items) - org} batch" if len(items) - org else "")
            lis = "".join(f'<li>{_e(d["id"])} {_e(_clip(d["title"], 90))} <span style="color:#888">({_e(d["by"])}{", deep" if d["deep"] else ""}{", batch-closed" if d["batch"] else ""})</span></li>'
                          for d in items[:4])
            more = f'<li style="color:#888">… +{len(items) - 4} more</li>' if len(items) > 4 else ""
            parts.append(f'<div style="margin-bottom:8px"><b>{_e(asp_id)}</b> — {_e(_clip(items[0]["asp_title"], 70))} <span style="color:#888">({cnt})</span>'
                         f'<ul style="margin:3px 0 0;padding-left:18px;font-size:12px;line-height:1.45">{lis}{more}</ul></div>')
        if len(by_asp) > 8:
            parts.append(f'<p style="margin:0;font-size:12px;color:#888">… and {len(by_asp) - 8} more aspirations touched</p>')
        inner = "".join(parts)
    out.append(_card(f"Done this window ({len(organic)}" + (f" + {n_batch} batch-closed" if n_batch else "") + ")", inner, "#28a745", "#f6fff8"))

    # ---- In progress (bars)
    asps = sorted(data["active_asps"], key=lambda a: (-a["window_done"], -a["done"]))[:12]
    if asps:
        rows = []
        for a in asps:
            pct = int(100 * a["done"] / a["total"]) if a["total"] else 0
            bar = (f'<div style="background:#eee;border-radius:4px;height:8px;width:100%"><div style="background:#1e90ff;height:8px;'
                   f'border-radius:4px;width:{pct}%"></div></div>')
            rows.append(f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;font-size:13px"><b>{_e(a["id"])}</b> {_e(_clip(a["title"], 60))}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;width:34%">{bar}</td>'
                        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;line-height:1.4;padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap;font-size:12px;color:#555">{a["done"]}/{a["total"]} ({pct}%)'
                        + (f'<br><span style="color:#28a745">+{a["window_done"]} this window</span>' if a["window_done"] else "") + "</td></tr>")
        out.append(_card("In progress", '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">' + "".join(rows) + "</table>"))

    # ---- learning + right now
    corr = h.get("corrected") or []
    if corr:
        lis = "".join(f'<li>{_e(c["at"])} — {_e(_clip(c["title"], 130))}</li>' for c in corr)
        out.append(_card("What we got wrong (hypotheses corrected this window)",
                         f'<ul style="margin:0;padding-left:18px;font-size:13px">{lis}</ul>', "#6f42c1", "#faf7ff"))
    if data["pulse"]:
        rows = []
        for p in data["pulse"]:
            active = p["age_h"] is not None and p["age_h"] <= 6
            state = _pill("active", "#28a745") if active else _pill(f"quiet {_age_str(p['age_h'])}", "#fd7e14")
            on = f'{_e(p["in_flight"])} {_e(_clip(p["in_flight_title"], 80))}' if p.get("in_flight") else '<span style="color:#888">between goals</span>'
            rows.append(f'<tr><td {TD}><b>{_e(p["agent"])}</b></td><td {TD}>{state}</td><td {TD}>{on}</td></tr>')
        out.append(_card("Each agent right now", '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">' + "".join(rows) + "</table>"))

    # ---- spend
    cost = data.get("cost") or {}
    if cost:
        inner = ""
        tiles = [_tile(t.get("label", ""), str(t.get("value", "")), str(t.get("sub", "")), "#222") for t in (cost.get("tiles") or [])[:5]]
        if tiles:
            inner += '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%"><tr>' + "".join(tiles) + "</tr></table>"
        if cost.get("lines"):
            inner += '<ul style="margin:8px 0 0;padding-left:18px;font-size:13px">' + "".join(f"<li>{_e(x)}</li>" for x in cost["lines"][:12]) + "</ul>"
        if cost.get("note"):
            inner += f'<p style="margin:8px 0 0;font-size:12px;color:#666">{_e(cost["note"])}</p>'
        if cost.get("as_of"):
            inner += (f'<p style="margin:4px 0 0;font-size:11px;color:{"#dc3545" if cost.get("stale") else "#999"}">as of {_e(cost["as_of"])}'
                      + (" — STALE" if cost.get("stale") else "") + "</p>")
        out.append(_card("Spend" + (f" — {cost['headline']}" if cost.get("headline") else ""), inner or "<p style='margin:0;color:#888'>no figures</p>", "#20c997", "#f3fffb"))

    # ---- product signals
    src = (data["outcome"] or {}).get("sources") or {}
    sig = []
    ci = src.get("ci") or {}
    if ci.get("status") and ci.get("status") != "unavailable":
        sig.append(f'CI: {ci.get("passed", "?")}/{ci.get("runs", "?")} runs passed (pass rate {ci.get("pass_rate", "?")})')
    for name in ("service", "git"):
        row = src.get(name) or {}
        if row.get("status") and row.get("status") != "unavailable":
            sig.append(f'{name.title()}: {row.get("status")}' + (f' ({row.get("note")})' if row.get("note") else ""))
    if sig:
        stamp = (data["outcome"] or {}).get("computed_at") or ""
        out.append(_card("Product signals", '<ul style="margin:0;padding-left:18px;font-size:13px">' + "".join(f"<li>{_e(x)}</li>" for x in sig) + "</ul>"
                         + (f'<p style="margin:6px 0 0;font-size:11px;color:#999">as of {_e(stamp)}</p>' if stamp else "")))

    # ---- notes
    if notes.strip():
        lines = notes.strip().splitlines()[:12]
        out.append(_card(f"Notes from {agent}", "".join(f'<p style="margin:0 0 4px;font-size:13px">{_e(l)}</p>' for l in lines), "#6c757d", "#fafafa"))

    out.append("</div>")  # padding
    out.append('<div style="padding:12px 22px;background:#f8f9fa;border-top:1px solid #eee;font-size:12px;color:#999">'
               f'Full agent-side report: agents/{_e(agent)}/COMPLETION-REPORT.md (git history is the archive). Reply to this email to reach the fleet mailbox.</div>')
    out.append("</div></div></body></html>")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", "") or "agent")
    ap.add_argument("--since", default="", help="ISO timestamp; default: last-report-timestamp or 48h")
    ap.add_argument("--notes-file", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--html-out", default="", help="also write the HTML email twin here")
    ap.add_argument("--world", default="")
    ap.add_argument("--max-items", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="emit the gathered data instead of markdown")
    args = ap.parse_args(argv)
    world = Path(args.world) if args.world else Path(WORLD_DIR)
    now = _now()
    since = _ts(args.since) if args.since else None
    if not since:
        try:
            lr = agents_root() / args.agent / "session" / "last-report-timestamp"
            since = _ts(lr.read_text().strip()) if lr.exists() else None
        except Exception:
            since = None
    if not since:
        since = now - timedelta(hours=48)
    data = gather(world, args.agent, since, now, args.max_items)
    if args.json:
        print(json.dumps(data, indent=1, default=str))
        return 0
    notes = Path(args.notes_file).read_text(encoding="utf-8", errors="replace") if args.notes_file and Path(args.notes_file).exists() else ""
    md = render(data, agent=args.agent, since=since, now=now, notes=notes, max_items=args.max_items)
    if args.html_out:
        hp = render_html(data, agent=args.agent, since=since, now=now, notes=notes, max_items=args.max_items)
        Path(args.html_out).write_text(hp, encoding="utf-8")
        print(f"[completion-digest] wrote {args.html_out} ({len(hp)} bytes)")
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[completion-digest] wrote {args.out} ({len(md)} bytes)")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
