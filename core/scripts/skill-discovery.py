#!/usr/bin/env python3
"""Skill discoverability measurement (behavioral layer).

Sibling of skill-evaluate.py. Per-execution quality scoring (skill-evaluate)
cannot detect the rb-314 failure mode: a skill that never fires produces
zero evaluations, so the five quality dimensions have no data to score.
This script measures absence of invocation against time-since-forge and
prior-rate-of-use, classifies each forged skill, and emits triage hints.

Subcommands:
  report          Full per-skill discovery analysis (JSON to stdout)
  flagged         Only skills whose status is not 'healthy' or 'new' (JSON)
  score <skill>   Single skill detail (JSON)

Inputs (read-only):
  world/forged-skills.yaml          forged_date, type, parent
  meta/skill-quality.yaml           per-skill evaluation history (date stream)
  world/skill-relations.yaml        co_invocation_log corroboration
  meta/skill-discovery-strategy.yaml thresholds and triage templates (REQUIRED;
                                    the script fails loud if absent — this file
                                    is the SINGLE source of truth for thresholds,
                                    DO NOT add fallback defaults in code)

Output: JSON to stdout. Never writes state. Single source of truth for
"is this skill earning its presence?" — consumed by aspirations-evolve
Step 9.5.5 to file Investigate/Idea goals.

Time convention: all timestamps are naive local datetimes per CLAUDE.md
("ALWAYS local system time, never UTC"). The script does NOT defend against
tz-aware values from disk — adding tz handling here would mask convention
violations elsewhere. If a TypeError surfaces, fix the source, not this file.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR, WORLD_DIR, PROJECT_ROOT, agents_root


FORGED_PATH = WORLD_DIR / "forged-skills.yaml"
QUALITY_PATH = META_DIR / "skill-quality.yaml"
RELATIONS_PATH = WORLD_DIR / "skill-relations.yaml"
STRATEGY_PATH = META_DIR / "skill-discovery-strategy.yaml"


def read_yaml(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def load_strategy():
    """Load skill-discovery-strategy.yaml. The file is REQUIRED.

    DO NOT add fallback defaults here. The strategy file is the single source
    of truth for thresholds; a missing file is a setup error worth surfacing
    immediately, not papering over with hardcoded values that drift.
    """
    if not STRATEGY_PATH.exists():
        print(
            "ERROR: skill-discovery-strategy.yaml not found at {}\n"
            "This file is required — it is the single source of truth for "
            "discovery thresholds. Create it from the template in the "
            "session-92 skill-discovery work.".format(STRATEGY_PATH),
            file=sys.stderr,
        )
        sys.exit(3)
    data = read_yaml(STRATEGY_PATH)
    # Fail loud on the keys the rest of the script accesses unconditionally.
    # If a future strategy field is added optionally, gate it at the call site.
    for required_top in ("windows", "decline", "min_data", "goals",
                          "triage_hint_templates"):
        if required_top not in data:
            print("ERROR: skill-discovery-strategy.yaml missing required "
                  "section '{}'".format(required_top), file=sys.stderr)
            sys.exit(3)
    return data


def parse_iso(s):
    """Parse YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS into a naive datetime.

    Returns None when the input is missing or unparseable so a single bad
    timestamp in one record does not abort an entire report. Crashing on a
    well-formed YAML file with one bad date string would be heavy-handed.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def days_between(d1, d2):
    """Fractional days between two naive datetimes (d2 - d1).

    Returns None if either input is None. Result is fractional (days +
    seconds/86400), positive when d2 > d1.
    """
    if d1 is None or d2 is None:
        return None
    delta = d2 - d1
    return delta.days + delta.seconds / 86400.0


def collect_invocation_dates(skill_name, quality_data, relations_data,
                              journal_dates, companion_dates, ledger_dates):
    """Collect all invocation timestamps for a skill from five data sources.

    Five independent sources provide multi-signal corroboration (rule 1 of
    .claude/rules/verify-before-assuming.md). A skill flagged silent across
    all five is high-confidence; a skill present in any one is downgraded
    out of silently_undertriggering.

    1. skill-quality.yaml evaluations — scored executions (subset of all
       invocations; scoring is opt-in per evaluator).
    2. skill-relations.yaml co_invocation_log — sparse log of multi-skill
       goal executions (currently very few entries).
    3. agent journal.jsonl — append-only activity log; skill name appears
       when invocation is journaled (e.g., aspirations-state-update Step 4).
    4. companion-script invocations — for infra-wrapper forged skills whose
       value-add is "skill-level abstraction over a companion script"
       (rb-314 instrumentation gap, g-115-798 investigation). Agents
       routinely call the companion script directly, bypassing the skill
       entry-point. Without this source, heavily-used wrappers like
       access-roblox-studio (26 companion invocations / 30d) appear
       silently_undertriggering. See zeta/reports/g-115-798-investigate-
       skill-discovery-silent-flags.md and g-115-879.
    5. agents/<name>/skill-invocations.jsonl ledger — the PRIMARY per-model-
       invocation record (one line per Skill-tool fire, `skill` + `ts`
       fields). Was MISSING until g-115-2280 (2026-07-16): the audit flagged
       notify-user cold_after_use(110d) while the ledger held 80 fires, the
       latest the previous day — the reported ages were age-since-forge
       artifacts of seeing zero invocations, not real silence.

    Returns (dates, sources) where dates is the deduplicated sorted list
    used for invocation arithmetic, and sources is a per-source raw count
    used for the confidence dimension. Two events at the exact same second
    collapse in `set(dates)` — INTENTIONAL: same-second hits are almost
    always the same execution recorded by two sources, not two distinct
    invocations.
    """
    dates = []
    sources = {"quality": 0, "co_invocation": 0, "journal": 0,
               "companion_script": 0, "ledger": 0}

    skills = quality_data.get("skills", {}) or {}
    skill_info = skills.get(skill_name, {})
    if isinstance(skill_info, dict):
        for ev in skill_info.get("evaluations", []) or []:
            if isinstance(ev, dict):
                dt = parse_iso(ev.get("date"))
                if dt:
                    dates.append(dt)
                    sources["quality"] += 1

    log = relations_data.get("co_invocation_log", []) or []
    for entry in log:
        if not isinstance(entry, dict):
            continue
        skills_used = entry.get("skills", []) or []
        if skill_name in skills_used:
            dt = parse_iso(entry.get("date"))
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


def collect_ledger_skill_dates(skill_names):
    """Scan all agents/<name>/skill-invocations.jsonl ledgers once.

    The ledger is the PRIMARY invocation record: the Skill-tool tracking
    path appends {"ts", "skill", "agent", "sid", "invocation_source"} on
    every model invocation. Exact match on the `skill` field (no regex —
    the field is already the bare skill name). Same tolerance contract as
    the journal scanner: half-written tails skipped via JSONDecodeError.

    AGENTS-ROOT GLOB (g-115-1405 class): use the _paths SSOT agents_root()
    — NEVER PROJECT_ROOT.glob("*/...") (depth-1 matches nothing post-
    relocation; see the CLAUDE.md cross-agent glob-consumer table).
    """
    out = {name: [] for name in skill_names}
    if not skill_names:
        return out
    wanted = set(skill_names)
    for ledger_path in agents_root().glob("*/skill-invocations.jsonl"):
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
                dt = parse_iso(rec.get("ts") or rec.get("timestamp"))
                if dt:
                    out[name].append(dt)
    return out


def collect_journal_skill_dates(skill_names):
    """Scan all <agent>/journal.jsonl files once for skill-name mentions.

    Returns dict: skill_name -> [datetime, ...]. Whole-word match on the
    JSON-encoded record string. Cheap (single pass per journal file, all
    skills checked at once); correct enough for kebab-case identifiers
    (false positives vanishingly rare for skill names like notify-user).

    Partial-line reads of an actively-appending journal are tolerated by
    the JSONDecodeError continue — append-only log readers MUST tolerate
    a half-written tail. Other I/O failures (permission denied, missing
    file mid-glob) are intentionally NOT caught — those are setup bugs
    worth surfacing.
    """
    out = {name: [] for name in skill_names}
    if not skill_names:
        return out

    name_pattern = re.compile(
        r'(?<![\w-])(' + '|'.join(re.escape(n) for n in skill_names) + r')(?![\w-])'
    )

    # AGENTS-ROOT GLOB (5): agent dirs live at PROJECT_ROOT/agents/<name>
    # (AGENTS_PARENT_DIR), so journals are at agents/<name>/journal.jsonl. Use the
    # _paths SSOT agents_root() — NEVER PROJECT_ROOT.glob("*/...") (depth-1 matches
    # nothing post-relocation; the agents/ glob-drift bug class, CLAUDE.md sync-list).
    for journal_path in agents_root().glob("*/journal.jsonl"):
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
                dt = parse_iso(rec.get("timestamp") or rec.get("date"))
                if not dt:
                    continue
                for m in matches:
                    out[m].append(dt)
    return out


def collect_companion_script_dates(skill_names, forged_skills):
    """Scan execution-diary + board/coordination for companion-script invocations.

    Closes the rb-314 / g-115-798 instrumentation gap: infra-wrapper forged
    skills (access-roblox-studio, access-operator-api, access-aws-services,
    run-game-session, etc.) are routinely exercised via their companion
    scripts directly, bypassing the skill entry-point that skill-quality /
    co-invocation / journal sources measure. Without this source, heavily-
    used wrappers appear silently_undertriggering even though their
    underlying capability is production-active.

    Reads each skill's `companion_scripts` list from forged-skills.yaml,
    extracts the basename of each path (e.g. "world/scripts/roblox-studio.sh"
    -> "roblox-studio.sh"), and scans for those basenames in:
      1. <agent>/session/execution-diary.jsonl across all 6 agents (single
         pass per file; all scripts matched at once via combined regex)

    Diary is the authoritative invocation surface — entries are written by
    iteration-close.sh phase markers and explicit findings, which corresponds
    to actual script executions. Board (world/board/coordination.jsonl) is
    deliberately NOT scanned here: board posts can mention a script in
    discussion ("we should retire X") without invoking it, which would
    falsely inflate counts and break the brief's acceptance test
    (manage-roblox-scripts MUST stay at 0 even with 1 historical board
    mention). The brief explicitly labels board as "cross-agent
    corroboration" rather than primary count, and the diary-only table in
    the brief's §2 is what the acceptance numbers are calibrated against.

    Returns dict: skill_name -> [datetime, ...]. A single script invocation
    that matches multiple skills (e.g. roblox-studio.sh belongs to both
    access-roblox-studio and run-game-session) is recorded under every
    matching skill — the brief explicitly considers this "27 companion
    invocations" for run-game-session because both its scripts were hit.
    Same-second dedup happens upstream in collect_invocation_dates via
    set(dates), so a single record matching two scripts of one skill does
    not double-count.

    Read failures (missing diary file for an agent that has never
    iterated, missing board file in a fresh world) are tolerated by the
    glob-then-open pattern. Per the journal scan above, JSONDecodeError
    on a half-written tail line is also tolerated. Other I/O failures
    surface as setup bugs.
    """
    out = {name: [] for name in skill_names}
    if not skill_names or not forged_skills:
        return out

    # Build script-basename -> [skill_name, ...] reverse map. A companion
    # script can belong to multiple skills; tracking all matches is correct
    # per the  brief (it counts run-game-session's two scripts
    # toward its invocation total, not just the one shared with
    # access-roblox-studio).
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

    # Single regex over all script basenames — same shape as the journal
    # scanner above (whole-word boundary so "aws-exec.sh" doesn't match
    # "aws-exec.sh.bak" and vice-versa). The dot in ".sh" is escaped by
    # re.escape; (?<![\w.-]) / (?![\w.-]) prevents prefix/suffix bleed-
    # through across the kebab-case + dot-separator basename format.
    name_pattern = re.compile(
        r'(?<![\w.-])(' +
        '|'.join(re.escape(b) for b in script_to_skills) +
        r')(?![\w.-])'
    )

    # Build the list of files to scan: per-agent execution diaries only.
    # See docstring "Diary is the authoritative invocation surface" — board
    # is intentionally excluded to keep the genuine-cold signal (e.g.
    # manage-roblox-scripts) at 0 even with historical mentions.
    # Glob is forgiving when a fresh agent has not yet created its session/ dir.
    # AGENTS-ROOT GLOB (5): diaries live at agents/<name>/session/
    # execution-diary.jsonl — use _paths SSOT agents_root(), NEVER
    # PROJECT_ROOT.glob("*/...") (agents/ glob-drift bug class, CLAUDE.md sync-list).
    scan_paths = list(agents_root().glob("*/session/execution-diary.jsonl"))

    for path in scan_paths:
        with open(path, "r", encoding="utf-8") as f:
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
                dt = parse_iso(rec.get("timestamp") or rec.get("date"))
                if not dt:
                    continue
                for basename in matches:
                    for skill_name in script_to_skills.get(basename, []):
                        out[skill_name].append(dt)
    return out


def classify(skill_name, forged_info, quality_data, relations_data,
             journal_dates, companion_dates, ledger_dates, strategy, now):
    """Compute discovery metrics + classification for one forged skill."""
    forged_date = parse_iso(forged_info.get("forged_date"))
    days_since_forge = days_between(forged_date, now)

    invocations, sources = collect_invocation_dates(
        skill_name, quality_data, relations_data, journal_dates,
        companion_dates, ledger_dates,
    )
    total_invocations = len(invocations)
    sources_with_data = sum(1 for v in sources.values() if v > 0)
    last_invocation = invocations[-1] if invocations else None
    first_invocation = invocations[0] if invocations else None
    days_since_last = days_between(last_invocation, now)
    forge_to_first_days = days_between(forged_date, first_invocation)

    # Use `is not None` (not falsy) so days_since_forge=0.0 (forged today) does
    # not collapse to the "forge date unknown" branch. We need explicit None
    # handling for invocations_per_week so an unknown forge date does not
    # silently produce a misleading non-None rate.
    if days_since_forge is not None:
        weeks_active = max(days_since_forge / 7.0, 1.0)
        invocations_per_week = round(total_invocations / weeks_active, 3)
    else:
        invocations_per_week = None

    w = strategy["windows"]
    decline_cfg = strategy["decline"]
    min_data = strategy["min_data"]

    # Decline detection — compare last_window_days vs prior_window_days.
    # Requires enough history that both windows can be populated without
    # straddling the forge date.
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

    # Status precedence: silently_undertriggering > cold_after_use > declining > new > healthy.
    # rb-314 case wins because it represents a complete capability loss; the
    # other statuses presume some prior invocation history.
    if days_since_forge is None:
        status = "unknown_forge_date"
    elif days_since_forge < w["grace_period_days"]:
        status = "new"
    elif total_invocations == 0 and days_since_forge >= w["silent_window_days"]:
        status = "silently_undertriggering"
    elif days_since_last is not None and days_since_last >= w["staleness_window_days"]:
        # `days_since_last is not None` already implies invocations is non-empty
        # (last_invocation = invocations[-1]) — no need for `total_invocations > 0`.
        status = "cold_after_use"
    elif decline_signal and decline_signal["is_declining"]:
        status = "declining"
    else:
        status = "healthy"

    # Action recommendation — separate from status because detection alone is
    # not enough to file a goal. Goals are filed only after the action window
    # has also elapsed, keeping signal-to-noise reasonable.
    action_required = False
    if status == "silently_undertriggering" and days_since_forge >= w["action_silent_days"]:
        action_required = True
    elif status == "cold_after_use" and days_since_last >= w["action_cold_days"]:
        action_required = True
    elif status == "declining":
        action_required = True

    triage_hints = strategy["triage_hint_templates"].get(status, "")

    # Confidence: how many independent sources corroborate the invocation count.
    # 0 sources with data → silent verdict is high-confidence (rb-314 case).
    # 1 source → invocation count is at least a lower bound but under-logging
    # cannot be ruled out; downgrade so the agent triages logging gaps first.
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
        "triage_hints": triage_hints,
    }


def build_report(now=None):
    """Build the full report. Returns dict with skills + summary."""
    now = now or datetime.now()
    forged = read_yaml(FORGED_PATH).get("skills", {}) or {}
    quality = read_yaml(QUALITY_PATH)
    relations = read_yaml(RELATIONS_PATH)
    strategy = load_strategy()  # fails loud if missing — see load_strategy docstring

    skill_names = sorted(forged.keys())
    journal_dates = collect_journal_skill_dates(skill_names)
    companion_dates = collect_companion_script_dates(skill_names, forged)
    ledger_dates = collect_ledger_skill_dates(skill_names)

    skills_out = []
    counts = {}
    for name in skill_names:
        info = forged[name]
        if not isinstance(info, dict):
            continue
        record = classify(name, info, quality, relations, journal_dates,
                          companion_dates, ledger_dates, strategy, now)
        skills_out.append(record)
        counts[record["status"]] = counts.get(record["status"], 0) + 1

    overall_invocations = sum(s["total_invocations"] for s in skills_out)
    median_latency = _median(
        [s["forge_to_first_invocation_latency_days"] for s in skills_out
         if s["forge_to_first_invocation_latency_days"] is not None]
    )

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
        },
        "skills": skills_out,
    }


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return round(s[mid], 2)
    return round((s[mid - 1] + s[mid]) / 2.0, 2)


def cmd_report(args):
    out = build_report()
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_flagged(args):
    out = build_report()
    flagged = [
        s for s in out["skills"]
        if s["status"] not in ("healthy", "new", "unknown_forge_date")
    ]
    if args.action_required_only:
        flagged = [s for s in flagged if s["action_required"]]
    print(json.dumps({
        "generated_at": out["generated_at"],
        "count": len(flagged),
        "skills": flagged,
    }, indent=2, ensure_ascii=False))


def cmd_score(args):
    out = build_report()
    match = [s for s in out["skills"] if s["skill"] == args.skill]
    if not match:
        # Skill not in forged registry — return clear empty record so callers
        # don't have to special-case "did the script find it?"
        print(json.dumps({"skill": args.skill, "found": False}, indent=2, ensure_ascii=False))
        sys.exit(2)
    print(json.dumps(match[0], indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Skill discoverability measurement")
    sub = parser.add_subparsers(dest="cmd")

    p_report = sub.add_parser("report", help="Full per-skill discovery analysis")
    p_report.set_defaults(func=cmd_report)

    p_flag = sub.add_parser("flagged", help="Only non-healthy / non-new skills")
    p_flag.add_argument("--action-required-only", action="store_true",
                        help="Only skills past their action window")
    p_flag.set_defaults(func=cmd_flagged)

    p_score = sub.add_parser("score", help="Single skill discovery record")
    p_score.add_argument("skill", help="Skill name (matches forged-skills.yaml key)")
    p_score.set_defaults(func=cmd_score)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(2)
    args.func(args)


if __name__ == "__main__":
    main()
