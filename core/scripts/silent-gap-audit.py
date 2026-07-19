#!/usr/bin/env python3
"""Silent-gap / orphaned-asset audit ( — systematizes ).

Turns unread data, stale telemetry, unwired mechanisms, and uninvoked skills
into LOUD Investigate specs — but ONLY for genuinely-NEW gaps. The 2026-06-13
manual run (g-318-08, by bravo) proved the four detection scans work AND that
every gap they surface is usually already tracked by an open sibling goal or
already resolved. So the load-bearing parts of the systematized version are NOT
the detectors — they are the two suppression gates:

  (1) rb-245 ZERO-COUNT VERIFICATION GATE — a "written-but-never-read" /
      "0-invocations" / "near-zero-input" finding is a statistical negation.
      Before concluding "orphaned" the audit MUST verify the field-name /
      grep-pattern against a live record, try MULTIPLE reader patterns, and
      read the timestamp from CONTENT (not mtime). Without this the manual run
      would have FALSE-flagged productivity-snapshots (it IS read) and 13/15
      forged skills (situational infra tools + per-window log, not orphaned).

  (2) DEDUP-AGAINST-OPEN-GOALS — every surfaced gap is checked against the
      title + description + origin_signal text of all OPEN goals (world+agent)
      and SKIPPED when already covered. This both prevents duplicate-goal spam
      AND is the re-file-idempotency mechanism: a gap filed last cadence is now
      an open goal whose text contains the gap's target token, so the next
      cadence's dedup suppresses it.

DETECTIVE BY DEFAULT (like defer-drift-check.py): emits NEW-gap specs as JSON;
the low-frequency caller (aspirations-strategic-scan) surfaces them for
judgment-filing. `--apply` files them directly (origin_signal
"silent-gap-audit:<detector>:<target>") for future automation. The audit's
value is the TAIL — catching the NEXT gap early, before a stale signal festers
for weeks unnoticed — not re-confirming known ones; hence low cadence + strict dedup.

Four detectors:
  (a) written-never-read  — top-level *.jsonl/*.yaml stores with writers but
      zero readers across the source corpus (multi-pattern grep = rb-245 gate)
  (b) telemetry-stale     — telemetry/probe files not refreshed in N days
      (last timestamp parsed from CONTENT, not mtime = rb-245 gate)
  (c) zero-input          — a scorer/gate reading a field that <X% of records
      carry (field-name validated against live schema = rb-245 gate)
  (d) never-invoked       — forged skills with 0 invocations in the log
      (situational-tool + per-window-log suppression = rb-245 gate; this
      detector is mostly-suppressing by design until the invocation log is
      lifetime-complete — faithful to the g-318-08 INCONCLUSIVE finding)

Guards honored: guard-420 (datetime — fromisoformat + Z-strip + tolerant),
guard-645 (field reads with defaults), guard-614 (structured JSON output),
guard-383 (fatal on source read error — a silent empty aggregate would hide
gaps behind a "0 found" lie), guard-467 / rb-245 (zero-count verification),
guard-759 (no /tmp). Reference: g-318-11 (systematizes g-318-08 / exp-g-318-08).
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)
from _paths import WORLD_DIR, META_DIR, AGENT_NAME, agent_dir  # noqa: E402

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

# Detector (a): where a store would be CONSUMED (readers searched here).
READER_SEARCH_DIRS = [
    PROJECT_ROOT / "core" / "scripts",
    PROJECT_ROOT / "mind_api" / "src",
    PROJECT_ROOT / ".claude" / "skills",
    PROJECT_ROOT / ".claude" / "rules",
    Path(WORLD_DIR) / "scripts" if WORLD_DIR else None,
    Path(WORLD_DIR) / "conventions" if WORLD_DIR else None,
]
READER_SOURCE_EXTS = {".py", ".sh", ".md", ".yaml", ".yml", ".js", ".ts"}

# Detector (a): append-only logs / per-design human-or-audit trails whose "no
# programmatic reader" is BY DESIGN — excluded so they are never flagged.
# (Matched as substrings of the store filename, case-insensitive.)
# "-archive" excludes EVERY archive sink (<stem>-archive.jsonl, where -archive is
# jsonl_hygiene's DEFAULT_ARCHIVE_SUFFIX). Archive sinks are write-only cold
# recoverability tiers: store-hygiene rotate/compact MOVES retired/old records
# INTO them and retrieval already excludes retired, so zero programmatic readers
# is correct-by-design, not an orphaned-asset gap. Recovery is manual (git
# history / direct read), mirroring the changelog/journal entries above. Without
# this, the detector false-flags reasoning-bank-archive / guardrails-archive /
# pipeline-archive / etc. every cadence (2, 0). Same
# archive-exclusion principle as jsonl_hygiene._glob_or_single ().
WRITTEN_NEVER_READ_EXCLUDE = (
    "changelog", "journal", "experience", "diary", "skill-invocations",
    "meta-log", "evolution-log", "gate-firings", "gate-eval", "override-bypass",
    "health", "metrics", "audit", "-log", "ledger", "drops", "skill-quality",
    "blocker-gate-overrides", "recommendations", "feedback", "-archive",
)

# Detectors (b) telemetry-stale, (c) zero-input, and (d)'s situational-skill
# patterns are DEPLOYMENT-SPECIFIC (which files are telemetry, which mechanism
# reads which field, which skill names are situational infra tools). Core ships
# DOMAIN-AGNOSTIC: these default empty/generic and are extended from an optional
# domain config (WORLD_DIR/conventions/silent-gap-audit-specs.json) loaded by
# load_domain_specs(). Detectors (a) written-never-read and (d) never-invoked
# work with zero config; (b) and (c) are inert until a domain supplies specs.
# This keeps core/ free of domain terms (.claude/rules/domain-free-examples.md).

# (b) telemetry spec tuple: (path-relative-to-WORLD_DIR, max_age_days, signal_name)
TELEMETRY_SPECS_DEFAULT = ()

# (c) zero-input spec tuple: (mechanism_name, [field_names], source('world'|'agent'|'both'),
#     min_coverage_fraction, note)
ZERO_INPUT_SPECS_DEFAULT = ()

# (d) situational-skill fragments: forged skills whose 0-in-window invocations is
# EXPECTED (situational tools invoked only during specific work; the invocation
# log is per-window not lifetime — rb-245: 0-in-window != orphaned). Only
# FRAMEWORK-GENERIC infra verbs live here; domain-specific tool-name fragments
# (cloud services, product integrations) are added via the domain config's
# "situational_skill_extra" list.
SITUATIONAL_SKILL_BASE = (
    "deploy", "notify", "email", "run-", "session", "probe", "infra",
    "access", "analyze", "monitor", "health",
)

# Time-windowed completed-goal dedup (6). Dedup-against-open-goals alone
# lets a gap that was investigated-and-CLOSED re-fire every ~4h strategic scan —
# the SAME sparse-by-design gap was DEEP-investigated twice before catch. So the
# dedup corpus ALSO includes goals COMPLETED within this many days: a re-detected
# gap matching a recent completion is noise → suppress. A match against only an
# OLDER completion (outside the window) still fires — that is a legitimate
# regression, and catching the tail/next-regression is the audit's whole point,
# so dedup-against-all-completed-FOREVER would break it. 14d suppresses the
# hours-to-days re-fire storm ~84x while keeping regression latency bounded.
# (For a STRUCTURALLY-permanent sub-threshold gap this still re-fires once per
# window; the heavier per-target "investigated-and-expected" suppression ledger
# is the documented alt if that residual proves noisy.)
COMPLETED_DEDUP_WINDOW_DAYS = 14


def load_domain_specs(world_dir=WORLD_DIR):
    """Load deployment-specific detector specs from the optional domain config
    (WORLD_DIR/conventions/silent-gap-audit-specs.json). Returns
    (telemetry_specs, zero_input_specs, situational_skill_extra). Fail-open:
    a missing/malformed config yields the domain-agnostic defaults."""
    telem = list(TELEMETRY_SPECS_DEFAULT)
    zero = list(ZERO_INPUT_SPECS_DEFAULT)
    extra = []
    if world_dir:
        cfg = Path(world_dir) / "conventions" / "silent-gap-audit-specs.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8")) or {}
                telem += [tuple(x) for x in data.get("telemetry", [])]
                zero += [tuple(x) for x in data.get("zero_input", [])]
                extra += [str(x) for x in data.get("situational_skill_extra", [])]
            except Exception:
                pass
    return telem, zero, extra


def build_situational_re(extra=()):
    """Compile the situational-skill regex from the framework-generic base plus
    any domain-supplied fragments."""
    frags = list(SITUATIONAL_SKILL_BASE) + list(extra or [])
    return re.compile("(" + "|".join(re.escape(f) for f in frags) + ")", re.I)


# ----------------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------------

def _now():
    return dt.datetime.now()


def _parse_iso(ts):
    """Tolerant ISO parse (guard-420). Returns datetime or None — never raises."""
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", ""))
    except Exception:
        return None


def _read_goals(source):
    """Read all active goals from world or agent queue via the daemon.

    guard-383: a per-source read error in an N>=2 aggregator MUST be fatal — a
    silent return [] writes a complete-looking lie ("0 gaps") into the merged
    result. The single fail-open boundary is the shell wrapper, never here.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[silent-gap-audit] {source} read failed: {e.body or e}", file=sys.stderr)
        sys.exit(1)
    data = _rt.tolerant_decode_aggregate(f"silent-gap-audit: {source}", out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def open_goal_corpus(all_goals):
    """Build the dedup corpus: one lowercased text blob per OPEN goal
    (pending/in-progress) concatenating title + description + origin_signal.
    Pure (takes goals in) so dedup is unit-testable."""
    corpus = []
    for g in all_goals:
        if g.get("status") not in ("pending", "in-progress"):
            continue
        text = " ".join([
            str(g.get("title") or ""),
            str(g.get("description") or ""),
            str(g.get("origin_signal") or ""),
        ]).lower()
        corpus.append((g.get("id"), text))
    return corpus


def recent_completed_corpus(all_goals, window_days=COMPLETED_DEDUP_WINDOW_DAYS, now=None):
    """Build a dedup corpus of goals COMPLETED within the last `window_days`
    (title + description + origin_signal, lowercased) — the time-windowed
    completed-goal half of the dedup (g-115-2236). Emits the SAME (id, text)
    blob shape as open_goal_corpus so is_covered treats it identically. A
    completed goal with no parseable timestamp is SKIPPED (cannot window it →
    let the gap fire, the regression-safe direction); a completion OLDER than
    the window is likewise skipped, so a gap matching only an old completion
    still fires (legitimate regression — the audit's tail-detection purpose).
    Pure (takes goals + now) so it is unit-testable."""
    if now is None:
        now = _now()
    cutoff = now - dt.timedelta(days=window_days)
    corpus = []
    for g in all_goals:
        if g.get("status") != "completed":
            continue
        ts = _parse_iso(g.get("completed_at") or g.get("completed_date"))
        if ts is None or ts < cutoff:
            continue
        text = " ".join([
            str(g.get("title") or ""),
            str(g.get("description") or ""),
            str(g.get("origin_signal") or ""),
        ]).lower()
        corpus.append((g.get("id"), text))
    return corpus


def is_covered(dedup_tokens, corpus):
    """Dedup-against-open-goals. A gap is COVERED when some single open goal's
    text contains the gap's PRIMARY token (dedup_tokens[0]). Biased toward
    suppression (a false-suppress just delays one cadence; a false-NEW is the
    duplicate-goal spam g-318-08 warned against). Returns (covered, goal_id)."""
    if not dedup_tokens:
        return (False, None)
    primary = str(dedup_tokens[0]).lower().strip()
    if not primary:
        return (False, None)
    for goal_id, text in corpus:
        if primary in text:
            return (True, goal_id)
    return (False, None)


def _iter_source_files():
    """Yield readable source files under the reader-search dirs (detector a)."""
    for root in READER_SEARCH_DIRS:
        if root is None or not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in READER_SOURCE_EXTS:
                yield p


def build_reader_blob():
    """Concatenate all source-file text into one lowercased blob, so a store's
    reader count is blob.count(token). One filesystem pass; cheap enough at the
    low audit cadence. Returns (blob, file_count)."""
    parts = []
    n = 0
    for p in _iter_source_files():
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
            n += 1
        except Exception:
            continue
    return ("\n".join(parts).lower(), n)


def _store_reader_patterns(filename):
    """rb-245 multi-pattern gate for detector (a): the patterns a real reader
    might use to reference a store. ALL must be absent before "never read"."""
    base = filename
    stem = filename.rsplit(".", 1)[0]
    pats = {base.lower(), stem.lower()}
    # kebab <-> snake variants (a store named foo-bar.jsonl may be read as foo_bar)
    pats.add(stem.replace("-", "_").lower())
    pats.add(stem.replace("_", "-").lower())
    return {p for p in pats if len(p) >= 4}  # drop too-short tokens (noise)


# ----------------------------------------------------------------------------
# Detector (a): stores written-but-never-read
# ----------------------------------------------------------------------------

def detect_written_never_read(reader_blob=None):
    """Top-level *.jsonl/*.yaml stores under WORLD_DIR/META_DIR with >0 content
    but zero readers across the source corpus. rb-245 gate: try MULTIPLE reader
    patterns; only flag when ALL are absent AND the store is non-empty."""
    gaps = []
    if reader_blob is None:
        reader_blob, _ = build_reader_blob()
    roots = [d for d in (WORLD_DIR, META_DIR) if d]
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for store in sorted(root.glob("*.jsonl")) + sorted(root.glob("*.yaml")):
            fn = store.name
            low = fn.lower()
            if any(ex in low for ex in WRITTEN_NEVER_READ_EXCLUDE):
                continue
            try:
                size = store.stat().st_size
            except Exception:
                continue
            if size == 0:
                continue  # empty store is a different (non-orphan) condition
            patterns = _store_reader_patterns(fn)
            hits = {p: reader_blob.count(p) for p in patterns}
            total_hits = sum(hits.values())
            if total_hits > 0:
                continue  # has a reader under at least one pattern — not orphaned
            gaps.append({
                "detector": "written-never-read",
                "target": fn,
                "summary": f"store '{fn}' ({size} bytes) has writers but ZERO readers across the source corpus",
                "evidence": {"size_bytes": size, "patterns_tried": sorted(patterns), "reader_hits": 0},
                "severity": "medium",
                "dedup_tokens": [store.stem],
                "rb245_passed": True,
                "rb245_note": f"{len(patterns)} reader patterns tried, all 0; store non-empty",
            })
    return gaps


# ----------------------------------------------------------------------------
# Detector (b): telemetry/probes not refreshed in N days
# ----------------------------------------------------------------------------

_TS_FIELDS = ("timestamp", "ts", "date", "recorded_at", "created", "appended_at", "at", "time")


def _last_jsonl_timestamp(path):
    """Read the LAST non-empty JSONL row and extract its timestamp from CONTENT
    (rb-245: NOT mtime — mtime can be touched without a real refresh). Returns
    a datetime or None."""
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    except Exception:
        return None
    for ln in reversed(lines):
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        for f in _TS_FIELDS:
            v = row.get(f)
            d = _parse_iso(v)
            if d is not None:
                return d
    return None


def detect_telemetry_stale(now=None, specs=()):
    """Telemetry files not refreshed in N days. rb-245 gate: the staleness is
    measured from the last ROW's content timestamp; if no timestamp is parseable
    the finding is suppressed (uncheckable, not asserted)."""
    now = now or _now()
    gaps = []
    if not WORLD_DIR:
        return gaps
    for rel, max_age_days, signal_name in specs:
        path = Path(WORLD_DIR) / rel
        if not path.exists():
            continue  # absent file is a different gap class, not "stale"
        last = _last_jsonl_timestamp(path)
        if last is None:
            gaps.append({
                "detector": "telemetry-stale",
                "target": rel,
                "summary": f"telemetry '{rel}' ({signal_name}) has no parseable content timestamp",
                "evidence": {"max_age_days": max_age_days, "last_timestamp": None},
                "severity": "low",
                "dedup_tokens": [rel.rsplit(".", 1)[0], signal_name.split()[0]],
                "rb245_passed": False,  # rb-245: cannot assert staleness without a content timestamp
                "rb245_note": "no parseable content timestamp in last row — uncheckable, suppressed",
            })
            continue
        age_days = (now - last).total_seconds() / 86400
        if age_days <= max_age_days:
            continue
        gaps.append({
            "detector": "telemetry-stale",
            "target": rel,
            "summary": f"telemetry '{rel}' ({signal_name}) is {age_days:.0f}d stale (last row {last.isoformat(timespec='seconds')}, threshold {max_age_days}d)",
            "evidence": {"age_days": round(age_days, 1), "max_age_days": max_age_days,
                         "last_timestamp": last.isoformat(timespec="seconds")},
            "severity": "medium",
            "dedup_tokens": [rel.rsplit(".", 1)[0], signal_name.split()[0]],
            "rb245_passed": True,
            "rb245_note": f"staleness from last-ROW content timestamp ({last.isoformat(timespec='seconds')}), not mtime",
        })
    return gaps


# ----------------------------------------------------------------------------
# Detector (c): zero-input mechanisms
# ----------------------------------------------------------------------------

def detect_zero_input(world_goals, agent_goals, specs=()):
    """A scorer/gate reads a field that <min_coverage of records carry. rb-245
    gate: the field-name is validated against the live schema (it must appear as
    a key in >=1 record); a field present in ZERO records is ambiguous
    (misspelled spec vs genuinely-zero) and suppressed pending schema review."""
    gaps = []
    for name, fields, source, min_cov, note in specs:
        if source == "world":
            pool = world_goals
        elif source == "agent":
            pool = agent_goals
        else:
            pool = list(world_goals) + list(agent_goals)
        total = len(pool)
        if total == 0:
            continue
        # rb-245: does the field name appear as a key in ANY record? (validates spelling)
        field_known = any(f in g for g in pool for f in fields)
        carriers = sum(1 for g in pool if any(g.get(f) not in (None, "", [], {}) for f in fields))
        coverage = carriers / total
        if coverage >= min_cov:
            continue
        if not field_known:
            gaps.append({
                "detector": "zero-input",
                "target": name,
                "summary": f"mechanism '{name}' reads {fields} but the field is absent from EVERY record",
                "evidence": {"fields": fields, "carriers": carriers, "total": total, "coverage": round(coverage, 4)},
                "severity": "low",
                "dedup_tokens": [name.split()[-1], fields[0]],
                "rb245_passed": False,  # rb-245: zero presence == misspelled-field-vs-real ambiguity
                "rb245_note": f"field(s) {fields} appear in 0 records — verify spelling/schema before asserting zero-input",
            })
            continue
        gaps.append({
            "detector": "zero-input",
            "target": name,
            "summary": f"mechanism '{name}' reads {fields} but only {carriers}/{total} ({coverage:.2%}) of records carry it — {note}",
            "evidence": {"fields": fields, "carriers": carriers, "total": total,
                         "coverage": round(coverage, 4), "min_coverage": min_cov},
            "severity": "medium",
            "dedup_tokens": [name.split()[-1], fields[0]],
            "rb245_passed": True,
            "rb245_note": f"field {fields} validated present in >=1 record; coverage {coverage:.2%} < {min_cov:.2%}",
        })
    return gaps


# ----------------------------------------------------------------------------
# Detector (d): skills/capabilities defined but never invoked
# ----------------------------------------------------------------------------

def _load_forged_skill_names():
    """Forged-skill names from world/forged-skills.yaml (best-effort, no PyYAML
    dependency — a light line scan for top-level skill keys/names)."""
    if not WORLD_DIR:
        return []
    path = Path(WORLD_DIR) / "forged-skills.yaml"
    if not path.exists():
        return []
    names = []
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*(?:- )?name:\s*[\"']?([A-Za-z0-9_./-]+)", ln)
            if m:
                names.append(m.group(1))
    except Exception:
        return []
    return names


def _invocation_counts():
    """Per-skill invocation counts from the agent's skill-invocations.jsonl.
    NOTE (rb-245): this log is per-window, NOT lifetime — a 0 here means
    'not invoked in the recorded window', which is NOT 'orphaned'."""
    counts = {}
    inv = agent_dir(AGENT_NAME) / "skill-invocations.jsonl"
    if not inv.exists():
        return counts
    try:
        for ln in inv.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue
            sk = row.get("skill") or row.get("name") or row.get("skill_name")
            if sk:
                counts[sk] = counts.get(sk, 0) + 1
    except Exception:
        pass
    return counts


def detect_never_invoked(skill_names=None, inv_counts=None, situational_re=None):
    """Forged skills with 0 invocations. rb-245 gate (per ): the
    invocation log is per-WINDOW not lifetime, and most forged skills are
    SITUATIONAL infra tools — so a 0-count is INCONCLUSIVE, not orphaned. This
    detector is therefore mostly-suppressing BY DESIGN: it surfaces candidates
    but the rb-245 gate suppresses situational tools and notes the per-window
    limitation for the rest. (It will start flagging genuine orphans only once a
    lifetime-complete invocation log exists.)"""
    if skill_names is None:
        skill_names = _load_forged_skill_names()
    if inv_counts is None:
        inv_counts = _invocation_counts()
    if situational_re is None:
        situational_re = build_situational_re()  # framework-generic base only
    gaps = []
    for sk in skill_names:
        if inv_counts.get(sk, 0) > 0:
            continue
        situational = bool(situational_re.search(sk))
        gaps.append({
            "detector": "never-invoked",
            "target": sk,
            "summary": f"forged skill '{sk}' has 0 invocations in the recorded window",
            "evidence": {"invocations_in_window": 0, "situational": situational},
            "severity": "low",
            "dedup_tokens": [sk],
            "rb245_passed": False,  # always suppressed: per-window log != lifetime (rb-245)
            "rb245_note": (
                "situational infra tool — 0-in-window expected, not orphaned"
                if situational else
                "skill-invocations.jsonl is per-window not lifetime — 0-count inconclusive; "
                "needs lifetime-invocation-log + trigger-wiring verification before asserting orphaned"
            ),
        })
    return gaps


# ----------------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------------

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48]


def run_pipeline(detectors_out, corpus):
    """Shared suppression pipeline: rb-245 gate, then dedup-against-open-goals.
    Pure (takes detector output + corpus in). Returns
    (new_gaps, suppressed_rb245, suppressed_dedup)."""
    new_gaps, supp_rb245, supp_dedup = [], [], []
    for gap in detectors_out:
        if not gap.get("rb245_passed", False):
            supp_rb245.append({"detector": gap["detector"], "target": gap["target"],
                               "rb245_note": gap.get("rb245_note", "")})
            continue
        covered, covering = is_covered(gap.get("dedup_tokens", []), corpus)
        if covered:
            supp_dedup.append({"detector": gap["detector"], "target": gap["target"],
                               "covering_goal_id": covering})
            continue
        gap["suggested"] = {
            "title": f"Investigate: silent gap — {gap['summary'][:90]}",
            "description": (
                f"Silent-gap audit ({gap['detector']}) surfaced a genuinely-NEW gap "
                f"(passed rb-245 verification AND not covered by any open goal).\n\n"
                f"Target: {gap['target']}\n"
                f"Summary: {gap['summary']}\n"
                f"Evidence: {json.dumps(gap.get('evidence', {}))}\n"
                f"rb-245: {gap.get('rb245_note','')}\n\n"
                f"Verify the gap is real (re-run the detector's check against a live record), "
                f"then either wire the missing reader/refresh/input or retire the orphaned asset. "
                f"Source: g-318-11 silent-gap-audit (systematizes g-318-08)."
            ),
            "category": "framework-architecture",
            "priority": "MEDIUM" if gap.get("severity") == "medium" else "LOW",
            # Use the already-allowed "investigate:" prefix (these ARE Investigate
            # goals) with silent-gap in the value — avoids touching the
            # origin_signal allowlist/infer parity. Dedup matches the target
            # token, not origin_signal, so traceability needs no new prefix.
            "origin_signal": f"investigate:silent-gap-{gap['detector']}-{_slug(gap['target'])}",
        }
        new_gaps.append(gap)
    return new_gaps, supp_rb245, supp_dedup


def file_investigate(gap, target_asp="asp-115"):
    """--apply: file ONE new gap as an Investigate via the daemon add-goal
    endpoint (_rt — the canonical Python->daemon path). NOT a bash subprocess: handing a Windows
    absolute path to bash strips its backslashes (rc=127) and a `C:/`-prefixed
    path is not reliably resolved by the subprocess shell either — the daemon
    client sidesteps both. Best-effort, fail-open — a filing error never aborts
    the audit. The Duplication override is justified by THIS audit's own
    dedup-against-open-goals (a stricter title+description+origin_signal scan
    than the daemon's gate); we only reach here for verified-NEW gaps."""
    s = gap["suggested"]
    record = {
        "title": s["title"][:140],
        "description": s["description"],
        "priority": s["priority"],
        "participants": ["agent"],
        "category": s["category"],
        "intended_agent": "either",
        "origin_signal": s["origin_signal"],
        "tags": ["silent-gap-audit", gap["detector"]],
    }
    override = {"Duplication": (
        f"silent-gap-audit dedup confirmed '{gap['target']}' not covered by any "
        f"open goal (title+description+origin_signal scan)")}
    try:
        resp = _rt.aspirations_add_goal(target_asp, record, source="world", overrides=override)
    except Exception as e:
        print(f"[silent-gap-audit] file failed for {gap['target']}: {e}", file=sys.stderr)
        return None
    gid = None
    if isinstance(resp, dict):
        g = resp.get("goal")
        if isinstance(g, dict):
            gid = g.get("id")
        gid = gid or resp.get("id")
    return gid


def main():
    ap = argparse.ArgumentParser(
        description=("Silent-gap / orphaned-asset audit — 4 detectors + rb-245 "
                     "gate + dedup-against-open-goals. Detective by default; "
                     "--apply files NEW gaps as Investigate goals. (g-318-11)"),
    )
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--apply", action="store_true",
                    help="File genuinely-NEW gaps as Investigate goals into --target-asp.")
    ap.add_argument("--target-asp", default="asp-115",
                    help="Aspiration to file Investigate goals into (default asp-115).")
    ap.add_argument("--detectors", default="all",
                    help="Comma list of detectors to run (written-never-read,telemetry-stale,"
                         "zero-input,never-invoked) or 'all'.")
    args = ap.parse_args()

    want = (set(d.strip() for d in args.detectors.split(",")) if args.detectors != "all"
            else {"written-never-read", "telemetry-stale", "zero-input", "never-invoked"})

    world_goals = _read_goals("world")
    agent_goals = _read_goals("agent")
    all_goals = world_goals + agent_goals
    corpus = open_goal_corpus(all_goals)
    open_corpus_count = len(corpus)

    # Time-windowed completed-goal dedup (6): extend the dedup corpus
    # with goals COMPLETED within COMPLETED_DEDUP_WINDOW_DAYS so a gap that was
    # investigated-and-closed does not re-fire every ~4h scan. all_goals already
    # spans mixed status — the active-aspiration read returns completed goals too
    # (open_goal_corpus filters them out; this ADDS the recent ones back for
    # dedup). is_covered treats both corpora identically (same (id, text) blob).
    completed_corpus = recent_completed_corpus(all_goals, COMPLETED_DEDUP_WINDOW_DAYS)
    corpus = corpus + completed_corpus

    # Deployment-specific detector specs come from the domain config (core stays
    # domain-agnostic — see load_domain_specs / domain-free-examples.md).
    telem_specs, zero_specs, situational_extra = load_domain_specs()
    situational_re = build_situational_re(situational_extra)

    detectors_out = []
    scanned = {
        "open_goals": open_corpus_count,
        "completed_goals_in_dedup_window": len(completed_corpus),
        "completed_dedup_window_days": COMPLETED_DEDUP_WINDOW_DAYS,
    }
    if "written-never-read" in want:
        blob, fcount = build_reader_blob()
        d = detect_written_never_read(reader_blob=blob)
        detectors_out += d
        scanned["source_files"] = fcount
    if "telemetry-stale" in want:
        detectors_out += detect_telemetry_stale(specs=telem_specs)
        scanned["telemetry_specs"] = len(telem_specs)
    if "zero-input" in want:
        detectors_out += detect_zero_input(world_goals, agent_goals, specs=zero_specs)
        scanned["zero_input_specs"] = len(zero_specs)
    if "never-invoked" in want:
        detectors_out += detect_never_invoked(situational_re=situational_re)

    new_gaps, supp_rb245, supp_dedup = run_pipeline(detectors_out, corpus)

    filed = []
    if args.apply:
        for gap in new_gaps:
            gid = file_investigate(gap, args.target_asp)
            if gid:
                filed.append({"goal_id": gid, "detector": gap["detector"], "target": gap["target"]})

    result = {
        "ran_at": _now().isoformat(timespec="seconds"),
        "detectors_run": sorted(want),
        "scanned": scanned,
        "new_gap_count": len(new_gaps),
        "new_gaps": new_gaps,
        "suppressed_rb245": supp_rb245,
        "suppressed_dedup": supp_dedup,
        "filed": filed,
        "applied": bool(args.apply),
    }

    if args.output == "human":
        print(f"ran_at={result['ran_at']} detectors={','.join(result['detectors_run'])}")
        print(f"scanned={scanned}")
        print(f"NEW gaps: {len(new_gaps)} | suppressed rb-245: {len(supp_rb245)} | suppressed dedup: {len(supp_dedup)}")
        for g in new_gaps:
            print(f"  NEW [{g['detector']}] {g['target']}: {g['summary']}")
        for s in supp_dedup:
            print(f"  dedup [{s['detector']}] {s['target']} <- covered by {s['covering_goal_id']}")
        for s in supp_rb245:
            print(f"  rb245 [{s['detector']}] {s['target']}: {s['rb245_note']}")
        if filed:
            print(f"filed: {[f['goal_id'] for f in filed]}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
