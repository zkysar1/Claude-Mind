"""Skill-discovery read endpoints — parity with core/scripts/skill-discovery.py.

Daemonises all 3 skill-discovery subcommands (Batch 6). ALL are pure reads
(zero writes, no history/changelog/lock):

  GET /v1/skill-discovery/report                       mixed
  GET /v1/skill-discovery/flagged?action_required_only=1 mixed
  GET /v1/skill-discovery/score?skill=<name>           mixed (404 if not forged)

SCOPE: mixed. Reads world/forged-skills.yaml + world/skill-relations.yaml,
meta/skill-quality.yaml + meta/skill-discovery-strategy.yaml (the REQUIRED hard
dependency — 500 if missing), and (via the replicated glob bug below) cross-agent
journal.jsonl + session/execution-diary.jsonl. No X-Mind-Agent gate: the
world/meta reads are agent-agnostic and the daemon's path resolver falls back to
the first configured agent for world/meta resolution.

BYTE-COMPATIBILITY: all 3 commands print json.dumps(obj, indent=2,
ensure_ascii=False) + "\n" (CLI lines 530/541/556). Determinism: build_report
uses datetime.now(), and days_between() uses only delta.days +
delta.seconds/86400 (microseconds ignored), while generated_at is
isoformat(timespec="seconds"). So injecting now = fromisoformat(<CLI
generated_at>) reproduces every now-dependent field byte-for-byte — the test
harness does exactly this. score not-found prints {"skill":X,"found":false}
then sys.exit(2); the daemon returns that body with HTTP 404.

AGENTS-ROOT GLOB (g-115-1405, FIXED — was a replicated CLI bug): the CLI and
this handler scan agents/<name>/journal.jsonl and agents/<name>/session/
execution-diary.jsonl via the agents/ parent (CLI: agents_root() from _paths;
daemon: ctx.paths.agents_root) — NEVER project_root.glob("*/...") at DEPTH 1.
Under AGENTS_PARENT_DIR="agents" the real journals live at agents/<name>/...
(depth 2 from the repo root); the prior depth-1 glob silently matched NOTHING,
zeroing the journal + companion invocation sources for every skill (the agents/
glob-drift bug class). During the inert daemonization the bug was deliberately
replicated for drop-in byte-compat and deferred to its own post-cutover goal
(g-115-1405) because correcting it raises invocation counts (behavioural). Both
tiers now use the agents/ parent and STAY byte-compatible: the byte-compat tests
use synthetic skill names absent from every journal, so both sources resolve
empty byte-identically.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml


class _StrategyError(Exception):
    """Raised when skill-discovery-strategy.yaml is missing/invalid (CLI exit 3)."""


def _read_yaml(path: Path) -> Dict[str, Any]:
    from storage_backend import get_backend
    get_backend().ensure_local(path)  # own-cloud read-path fix: materialize S3-only file before local read; no-op on LocalBackend and out-of-root paths
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    # /: robust tzinfo-strip via the shared _dt.parse_naive_iso
    # (core/scripts is on the daemon sys.path — cf. the `from storage_backend import
    # get_backend` import above). The bare strip-the-Z-then-fromisoformat idiom
    # returned an AWARE datetime for offset-bearing input, so a later naive
    # subtraction (_days_between) raised TypeError. Date-only fallback preserved.
    from _dt import parse_naive_iso
    r = parse_naive_iso(s)
    if r is not None:
        return r
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _days_between(d1, d2):
    if d1 is None or d2 is None:
        return None
    delta = d2 - d1
    return delta.days + delta.seconds / 86400.0


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return round(s[mid], 2)
    return round((s[mid - 1] + s[mid]) / 2.0, 2)


def _load_strategy(ctx):
    path = ctx.paths.meta / "skill-discovery-strategy.yaml"
    if not path.exists():
        raise _StrategyError(
            "skill-discovery-strategy.yaml not found at {} — required (single "
            "source of truth for discovery thresholds).".format(path))
    data = _read_yaml(path)
    for required_top in ("windows", "decline", "min_data", "goals",
                          "triage_hint_templates"):
        if required_top not in data:
            raise _StrategyError(
                "skill-discovery-strategy.yaml missing required section "
                "'{}'".format(required_top))
    return data


def _collect_invocation_dates(skill_name, quality_data, relations_data,
                              journal_dates, companion_dates, ledger_dates):
    dates = []
    sources = {"quality": 0, "co_invocation": 0, "journal": 0,
               "companion_script": 0, "ledger": 0}
    skills = quality_data.get("skills", {}) or {}
    skill_info = skills.get(skill_name, {})
    if isinstance(skill_info, dict):
        for ev in skill_info.get("evaluations", []) or []:
            if isinstance(ev, dict):
                dt = _parse_iso(ev.get("date"))
                if dt:
                    dates.append(dt)
                    sources["quality"] += 1
    log = relations_data.get("co_invocation_log", []) or []
    for entry in log:
        if not isinstance(entry, dict):
            continue
        skills_used = entry.get("skills", []) or []
        if skill_name in skills_used:
            dt = _parse_iso(entry.get("date"))
            if dt:
                dates.append(dt)
                sources["co_invocation"] += 1
    for dt in journal_dates.get(skill_name, []):
        dates.append(dt)
        sources["journal"] += 1
    for dt in companion_dates.get(skill_name, []):
        dates.append(dt)
        sources["companion_script"] += 1
    for dt in ledger_dates.get(skill_name, []):
        dates.append(dt)
        sources["ledger"] += 1
    return sorted(set(dates)), sources


def _collect_journal_skill_dates(ctx, skill_names):
    """Scan agents/<name>/journal.jsonl across all agents for skill-name mentions.
    Uses ctx.paths.agents_root (the agents/ parent), matching the CLI's _paths
    agents_root() SSOT — NEVER ctx.paths.project_root.glob("*/...") (the agents/
    glob-drift bug class: the depth-1 form silently matched nothing post-
    AGENTS_PARENT_DIR relocation, g-115-1405)."""
    out = {name: [] for name in skill_names}
    if not skill_names:
        return out
    name_pattern = re.compile(
        r'(?<![\w-])(' + '|'.join(re.escape(n) for n in skill_names) + r')(?![\w-])'
    )
    for journal_path in ctx.paths.agents_root.glob("*/journal.jsonl"):
        from storage_backend import get_backend
        get_backend().ensure_local(journal_path)  # own-cloud read-path fix: materialize per-agent journal before local read
        with open(journal_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                blob = json.dumps(rec, ensure_ascii=False)
                matches = set(name_pattern.findall(blob))
                if not matches:
                    continue
                dt = _parse_iso(rec.get("timestamp") or rec.get("date"))
                if not dt:
                    continue
                for m in matches:
                    out[m].append(dt)
    return out


def _collect_ledger_skill_dates(ctx, skill_names):
    """Scan agents/<name>/skill-invocations.jsonl ledgers across all agents.

    The ledger is the PRIMARY invocation record: the Skill-tool tracking hook
    appends {"ts", "skill", "agent", "sid", "invocation_source"} on every
    model-side Skill invocation. Before this source existed the audit
    false-flagged actively-used skills (g-115-2280: notify-user flagged
    cold_after_use(110d) while its ledger held 80 fires, latest <1d old).
    Uses ctx.paths.agents_root (matching the CLI's _paths agents_root() SSOT)
    — NEVER ctx.paths.project_root.glob("*/...") (the agents/ glob-drift bug
    class: depth-1 silently matched nothing post-relocation, g-115-1405)."""
    out = {name: [] for name in skill_names}
    if not skill_names:
        return out
    wanted = set(skill_names)
    for ledger_path in ctx.paths.agents_root.glob("*/skill-invocations.jsonl"):
        from storage_backend import get_backend
        get_backend().ensure_local(ledger_path)  # own-cloud read-path fix: materialize per-agent ledger before local read
        with open(ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                name = rec.get("skill")
                if name not in wanted:
                    continue
                dt = _parse_iso(rec.get("ts") or rec.get("timestamp"))
                if dt:
                    out[name].append(dt)
    return out


def _collect_companion_script_dates(ctx, skill_names, forged_skills):
    """Scan agents/<name>/session/execution-diary.jsonl across all agents for
    companion-script-basename invocations. Uses ctx.paths.agents_root (matching
    the CLI's _paths agents_root() SSOT) — NEVER ctx.paths.project_root.glob(
    "*/...") (the agents/ glob-drift bug class: depth-1 silently matched nothing
    post-relocation, g-115-1405)."""
    out = {name: [] for name in skill_names}
    if not skill_names or not forged_skills:
        return out
    script_to_skills = {}
    for skill_name in skill_names:
        info = forged_skills.get(skill_name, {})
        if not isinstance(info, dict):
            continue
        scripts = info.get("companion_scripts", []) or []
        for path in scripts:
            if not isinstance(path, str):
                continue
            basename = path.rsplit("/", 1)[-1].strip()
            if not basename:
                continue
            script_to_skills.setdefault(basename, []).append(skill_name)
    if not script_to_skills:
        return out
    name_pattern = re.compile(
        r'(?<![\w.-])(' + '|'.join(re.escape(b) for b in script_to_skills) + r')(?![\w.-])'
    )
    # ROSTER + AUTHORITATIVE READ (), replacing
    # ctx.paths.agents_root.glob("*/session/execution-diary.jsonl") + per-path
    # ensure_local(). The ensure_local was a real fix for the READ half, but it
    # could only ever run on paths the GLOB had already found — and the glob
    # enumerates by what the local read-through cache holds, so an ABSENT peer's
    # diary was never enumerated and therefore never materialized. Measured on
    # cc-02 at filing: 1 of 5 agents present cold. Enumerating from a roster is
    # the half ensure_local structurally cannot supply.
    #
    # No base is passed: in production ctx.paths.agents_root IS _paths
    # agents_root() (both mirror AGENTS_PARENT_DIR per CLAUDE.md "Agent-dir
    # Resolution"), and the no-base path additionally unions the team-state shard
    # roster — which is world state, so it survives a box where no peer AGENT dir
    # has been pulled yet. Passing the ctx root would silently drop that half.
    # Function-local import, matching the storage_backend import this replaced —
    # core/scripts is on the daemon's sys.path at call time, not module load.
    from _fleet_diary import read_fleet_diaries

    for _agent_name, _diary_text in read_fleet_diaries():
        # splitlines(), not io.StringIO — identical to the CLI twin, which keeps
        # the byte-compat this pair is guarded on (CLAUDE.md cross-agent-glob).
        for line in _diary_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            blob = json.dumps(rec, ensure_ascii=False)
            matches = set(name_pattern.findall(blob))
            if not matches:
                continue
            dt = _parse_iso(rec.get("timestamp") or rec.get("date"))
            if not dt:
                continue
            for basename in matches:
                for skill_name in script_to_skills.get(basename, []):
                    out[skill_name].append(dt)
    return out


def _collect_triage_verdicts(ctx, skill_names):
    """Map skill -> most recent SETTLED triage verdict from completed goals.

    Mirror of core/scripts/skill-discovery.py::collect_triage_verdicts
    (g-115-3084). Ported here 2026-08-01 by g-115-4326: the CLI shipped this
    2026-07-27 (239e5cc7e) and it was never ported, so under the daemon-only
    architecture — where the daemon is the ONLY live path — the suppression has
    been inert in production for its entire existence. Measured direction: CLI
    8 occurrences of verdict_suppressed*, daemon 0.

    RAW LOCAL READ, DELIBERATELY — no get_backend().ensure_local() unlike
    _read_yaml above. This mirrors the CLI, whose docstring establishes the
    staleness as fail-safe (guard-980/1139): an unsynced verdict is simply not
    found, suppression does not fire, and behavior degrades to today's
    re-flagging. The failure direction is toward the status quo, never toward
    wrongly silencing a live signal. Adding ensure_local on THIS side only would
    make the daemon read more than the reference implementation and break the
    very byte-compat parity this port exists to restore — if it is wanted, it is
    a change to BOTH tiers and its own decision.
    """
    verdicts = {}
    path = ctx.paths.world / "aspirations.jsonl"
    if not path.exists():
        return verdicts
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        # CLI warns on stderr here; the daemon has no stderr channel per
        # request, and the degradation is identical (suppression disabled).
        return verdicts
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
        except ValueError:
            continue
        for goal in asp.get("goals", []) or []:
            if goal.get("status") != "completed":
                continue
            note = goal.get("outcome_note") or goal.get("description") or ""
            # Require an explicit settled KEEP. A RETIRE verdict must NOT
            # suppress — it prescribes a different action that still needs doing.
            if not re.search(r"\bKEEP\b", note):
                continue
            key_text = "{} {}".format(goal.get("title") or "",
                                      goal.get("origin_signal") or "")
            when = (_parse_iso(goal.get("completed_at"))
                    or _parse_iso(goal.get("last_modified"))
                    or _parse_iso(goal.get("created_at")))
            if when is None:
                continue
            for skill_name in skill_names:
                if skill_name and skill_name in key_text:
                    prev = verdicts.get(skill_name)
                    if prev is None or when > prev["verdict_date"]:
                        verdicts[skill_name] = {
                            "goal_id": goal.get("id"),
                            "verdict": "KEEP",
                            "verdict_date": when,
                        }
    return verdicts


def _classify(skill_name, forged_info, quality_data, relations_data,
              journal_dates, companion_dates, ledger_dates, strategy, now,
              verdicts=None):
    forged_date = _parse_iso(forged_info.get("forged_date"))
    days_since_forge = _days_between(forged_date, now)

    invocations, sources = _collect_invocation_dates(
        skill_name, quality_data, relations_data, journal_dates,
        companion_dates, ledger_dates)
    total_invocations = len(invocations)
    sources_with_data = sum(1 for v in sources.values() if v > 0)
    last_invocation = invocations[-1] if invocations else None
    first_invocation = invocations[0] if invocations else None
    days_since_last = _days_between(last_invocation, now)
    forge_to_first_days = _days_between(forged_date, first_invocation)

    if days_since_forge is not None:
        weeks_active = max(days_since_forge / 7.0, 1.0)
        invocations_per_week = round(total_invocations / weeks_active, 3)
    else:
        invocations_per_week = None

    w = strategy["windows"]
    decline_cfg = strategy["decline"]
    min_data = strategy["min_data"]

    decline_signal = None
    if (
        decline_cfg.get("enabled")
        and days_since_forge is not None
        and days_since_forge >= (decline_cfg["last_window_days"] + decline_cfg["prior_window_days"])
    ):
        last_cutoff = now - timedelta(days=decline_cfg["last_window_days"])
        prior_cutoff = last_cutoff - timedelta(days=decline_cfg["prior_window_days"])
        last_count = sum(1 for d in invocations if d >= last_cutoff)
        prior_count = sum(1 for d in invocations if prior_cutoff <= d < last_cutoff)
        if prior_count >= min_data["min_prior_invocations"]:
            last_rate = last_count / decline_cfg["last_window_days"]
            prior_rate = prior_count / decline_cfg["prior_window_days"]
            ratio = last_rate / prior_rate
            decline_signal = {
                "last_window_count": last_count,
                "prior_window_count": prior_count,
                "ratio": round(ratio, 3),
                "is_declining": ratio < decline_cfg["decline_threshold"],
            }

    if days_since_forge is None:
        status = "unknown_forge_date"
    elif days_since_forge < w["grace_period_days"]:
        status = "new"
    elif total_invocations == 0 and days_since_forge >= w["silent_window_days"]:
        status = "silently_undertriggering"
    elif days_since_last is not None and days_since_last >= w["staleness_window_days"]:
        status = "cold_after_use"
    elif decline_signal and decline_signal["is_declining"]:
        status = "declining"
    else:
        status = "healthy"

    action_required = False
    if status == "silently_undertriggering" and days_since_forge >= w["action_silent_days"]:
        action_required = True
    elif status == "cold_after_use" and days_since_last >= w["action_cold_days"]:
        action_required = True
    elif status == "declining":
        action_required = True

    # Re-flagging a skill on the same unchanged metric AFTER it was already
    # triaged to KEEP is measurement noise, not signal ().
    #
    # rb-3132 IS THE LOAD-BEARING CONSTRAINT: a same-class recurrence AFTER a
    # remediated verdict FALSIFIES that verdict. So suppression is not a blanket
    # mute — any invocation dated after the verdict means the skill was used and
    # went cold AGAIN, which is new signal and must re-flag. Suppression applies
    # only while the metric is genuinely unchanged since the verdict.
    #
    # Optional section, gated at the call site per _load_strategy()'s contract —
    # an older strategy file simply keeps today's behavior.
    suppression = strategy.get("verdict_suppression") or {}
    verdict_suppressed = None
    if action_required and suppression.get("enabled") and verdicts:
        v = verdicts.get(skill_name)
        if v is not None:
            verdict_age = _days_between(v["verdict_date"], now)
            new_signal_since_verdict = (
                last_invocation is not None and last_invocation > v["verdict_date"]
            )
            if (
                verdict_age is not None
                and verdict_age <= suppression["window_days"]
                and not new_signal_since_verdict
            ):
                action_required = False
                verdict_suppressed = {
                    "verdict": v["verdict"],
                    "verdict_goal": v["goal_id"],
                    "verdict_date": v["verdict_date"].isoformat(),
                    "verdict_age_days": round(verdict_age, 2),
                    "window_days": suppression["window_days"],
                    "reason": (
                        "already triaged to KEEP by {} {:.0f}d ago and no invocation "
                        "since — same unchanged metric, no new signal (g-115-3084; "
                        "rb-3132 recurrence check passed)".format(
                            v["goal_id"], verdict_age)
                    ),
                }

    triage_hints = strategy["triage_hint_templates"].get(status, "")

    if total_invocations == 0:
        confidence = "high"
    elif sources_with_data >= 2:
        confidence = "high"
    else:
        confidence = "low"

    return {
        "skill": skill_name,
        "forged_date": forged_info.get("forged_date"),
        "type": forged_info.get("type"),
        "parent": forged_info.get("parent"),
        "days_since_forge": round(days_since_forge, 2) if days_since_forge is not None else None,
        "total_invocations": total_invocations,
        "invocation_sources": sources,
        "confidence": confidence,
        "first_invocation_date": first_invocation.isoformat() if first_invocation else None,
        "last_invocation_date": last_invocation.isoformat() if last_invocation else None,
        "days_since_last_invocation": round(days_since_last, 2) if days_since_last is not None else None,
        "forge_to_first_invocation_latency_days": (
            round(forge_to_first_days, 2) if forge_to_first_days is not None else None
        ),
        "invocations_per_week": invocations_per_week,
        "decline_signal": decline_signal,
        "status": status,
        "action_required": action_required,
        # Position is load-bearing: json.dumps preserves insertion order and the
        # byte-compat tests compare rendered bytes, so this key must sit between
        # action_required and triage_hints exactly as the CLI emits it.
        "verdict_suppressed": verdict_suppressed,
        "triage_hints": triage_hints,
    }


def build_report(ctx, now: datetime) -> Dict[str, Any]:
    """Full report. `now` is REQUIRED (callers inject for determinism)."""
    forged = _read_yaml(ctx.paths.world / "forged-skills.yaml").get("skills", {}) or {}
    quality = _read_yaml(ctx.paths.meta / "skill-quality.yaml")
    relations = _read_yaml(ctx.paths.world / "skill-relations.yaml")
    strategy = _load_strategy(ctx)

    skill_names = sorted(forged.keys())
    journal_dates = _collect_journal_skill_dates(ctx, skill_names)
    companion_dates = _collect_companion_script_dates(ctx, skill_names, forged)
    ledger_dates = _collect_ledger_skill_dates(ctx, skill_names)
    verdicts = (_collect_triage_verdicts(ctx, skill_names)
                if (strategy.get("verdict_suppression") or {}).get("enabled")
                else None)

    skills_out = []
    counts = {}
    for name in skill_names:
        info = forged[name]
        if not isinstance(info, dict):
            continue
        record = _classify(name, info, quality, relations, journal_dates,
                            companion_dates, ledger_dates, strategy, now,
                            verdicts=verdicts)
        skills_out.append(record)
        counts[record["status"]] = counts.get(record["status"], 0) + 1

    overall_invocations = sum(s["total_invocations"] for s in skills_out)
    median_latency = _median(
        [s["forge_to_first_invocation_latency_days"] for s in skills_out
         if s["forge_to_first_invocation_latency_days"] is not None])

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "strategy": {
            "version": strategy["version"],
            "windows": strategy["windows"],
            "decline": strategy["decline"],
        },
        "summary": {
            "total_forged_skills": len(skills_out),
            "status_counts": counts,
            "total_invocations_logged": overall_invocations,
            "median_forge_to_first_latency_days": median_latency,
            # Report suppressions explicitly. A suppressed flag is a decision
            # the consumer should be able to see and audit, not a silent drop.
            "verdict_suppressed_count": sum(
                1 for s in skills_out if s.get("verdict_suppressed")),
            "verdict_suppressed_skills": [
                {"skill": s["skill"], **s["verdict_suppressed"]}
                for s in skills_out if s.get("verdict_suppressed")
            ],
        },
        "skills": skills_out,
    }


def _out(obj: Any) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    return Response.text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json")


def _truthy(v) -> bool:
    return v is not None and str(v).lower() not in ("", "0", "false", "no")


def _flagged_payload(out: Dict[str, Any], action_required_only: bool) -> Dict[str, Any]:
    skills = [
        s for s in out["skills"]
        if s["status"] not in ("healthy", "new", "unknown_forge_date")
    ]
    if action_required_only:
        skills = [s for s in skills if s["action_required"]]
    return {
        "generated_at": out["generated_at"],
        "count": len(skills),
        "skills": skills,
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def report(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        out = build_report(ctx, datetime.now())
    except _StrategyError as e:
        return Response.error(500, "strategy_unavailable", str(e))
    return _out(out)


def flagged(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        out = build_report(ctx, datetime.now())
    except _StrategyError as e:
        return Response.error(500, "strategy_unavailable", str(e))
    return _out(_flagged_payload(out, _truthy(ctx.query.get("action_required_only"))))


def score(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    skill = (ctx.query.get("skill") or "").strip()
    if not skill:
        return Response.error(400, "missing_param", "skill query param required")
    try:
        out = build_report(ctx, datetime.now())
    except _StrategyError as e:
        return Response.error(500, "strategy_unavailable", str(e))
    match = [s for s in out["skills"] if s["skill"] == skill]
    if not match:
        # CLI prints the not-found JSON then sys.exit(2) -> HTTP 404, same body.
        return Response.text(
            json.dumps({"skill": skill, "found": False}, indent=2, ensure_ascii=False) + "\n",
            status=404, content_type="application/json")
    return _out(match[0])


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/skill-discovery/report")] = report
    routes[("GET", "/v1/skill-discovery/flagged")] = flagged
    routes[("GET", "/v1/skill-discovery/score")] = score
