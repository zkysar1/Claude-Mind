#!/usr/bin/env python3
# domain-leak-exempt: defer-narrative examples in docstring/comments cite
# concrete domain actions (Roblox Studio etc.) to illustrate the
# external-user-action defer pattern. The terms are pedagogical, not framework
# coupling.
"""Defer Recheck — re-examine aged goal defers against live state.

For each goal with defer_reason != null older than --max-age-hours, re-probe
the defer condition. Currently handles:

  (a) "blocked_on_dependency: g-XXX must ..." — check if g-XXX is completed.
      If yes, clear defer_reason (dependency resolved).
  (b) "Gated on g-X + g-Y" multi-goal clause — extract ALL g-ids inside the
      gating clause and require all completed before clearing. Excludes
      parenthetical g-ids outside the clause (rb-667 anti-overcapture).
  (c) "precondition_unmet: <jsonpath><op><value>" — JSON-path numeric
      comparator. Currently resolves `summary.*` paths against
      processor-run.sh summary; clears when condition met. Mechanism is
      structurally correct: re-runs each sweep against current state, so
      a defer that doesn't clear today auto-clears the moment external
      state advances to satisfy the condition. (g-115-342, iter-114)

Eligibility (added g-001-193, 2026-04-27): goals with `deferred_until` set
are SKIPPED regardless of defer_reason narrative. Rationale: deferred_until
is the AUTHORITATIVE structured time gate consumed by goal-selector.py.
When both fields are present, defer_reason is parallel narrative, not an
independent constraint that defer-recheck must clear. Coverage analysis
of 16 over-aged unrecognized defers (g-001-193 investigation, 2026-04-27)
showed ALL had deferred_until set; the right fix is structural-field
respect, not regex extension to cover narrative time-gate phrasings
("Wait N weeks", "T+Nd", "elapsed since DATE", etc.).

Future extensions (not yet implemented):

  (b) abstention expiry (both-agents-abstained, 72h TTL) — today handled by
      goal-selector.py at selection time, but a sweep here would surface
      cleared abstentions for observability.
  (c) infra-failure probes — re-run the canonical companion script for the
      skill named in defer_reason; clear on success.

Called by aspirations-precheck Phase 0.5b.3 (Stale-Defer Sweep). Dry-run by
default; --apply actually clears defer_reason via aspirations-update-goal.sh.

Exit: always 0 (reporting tool). Returns JSON:
  {
    "scanned": N,
    "eligible": N,         # defer_reason set AND age >= max_age_hours
    "cleared": N,          # dependency-type defers cleared (apply only)
    "would_clear": [...],  # dry-run list of goal_ids whose defer would clear
    "details": [...],      # per-goal reasoning
  }

Reference: g-115-154 (2026-04-21 session 55). Incident: g-115-71 defer cited
g-115-87 dependency which had been completed 3 days earlier; manual clear
unblocked the goal. Mirrors bravo's g-115-27 blocker-recheck (Layer C) for
participants:[user] blockers.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Canonical path + fileop primitives. _paths.WORLD_DIR is the single source
# of truth for WORLD_PATH resolution (env var > local-paths.conf > default);
# do NOT reintroduce an inline conf parser here — it will silently diverge
# when _paths.py gains new resolution rules.
from _paths import WORLD_DIR
from _fileops import locked_append_jsonl


# --- Metrics logging (rb-468 / guard-389 — defer-false-positive rate) -------
# Appends one record per actual clear plus one per-run summary. Consumers
# derive "fraction of defers that clear on re-probe" from these events
# (goal g-241-05 does the decision analysis on or after 2026-05-07).

def _resolve_metrics_log(cli_path):
    """Resolve the metrics log path.

    cli_path == ""     → disabled (explicit user request)
    cli_path is None   → <WORLD_DIR>/defer-recheck-metrics.jsonl
    cli_path otherwise → honor the explicit path
    """
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "defer-recheck-metrics.jsonl"


def _append_metric(path, record):
    """Append one record to the metrics JSONL. Fail-open by design.

    Metric-write failure must not abort the clearance run — the clear is
    the load-bearing work, the metric is instrumentation. Failures print
    to stderr so they stay visible instead of vanishing silently.
    """
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[defer-recheck] WARN: metrics append failed: {e}",
              file=sys.stderr)

# Two patterns for dependency detection:
#   STRUCTURED: "blocked_on_dependency: g-NNN-NN" or "dependency: g-NNN-NN"
#   PROXIMITY:  "g-NNN-NN <blocking-verb>" or "<awaiting-verb> g-NNN-NN"
# The proximity pattern catches free-form LLM phrasings like "g-238-05 blocked"
# or "until g-226-19 executes" that predate the structured convention.
# Both patterns collect ALL goal-ids mentioned — the caller checks whether
# ALL of them are completed before clearing (safer than first-match).
DEP_STRUCTURED = re.compile(r"(?:blocked_on_dependency|dependency|depends on):\s*(g-\d+-\d+)", re.IGNORECASE)
DEP_PROXIMITY = re.compile(
    r"(?:(?:blocked on|awaiting|prerequisite|until)\s+(?:asp-\d+\s+)?(g-\d+-\d+))"
    r"|(?:(g-\d+-\d+)\s+(?:blocked|executes|completes|deploys|lands|resolves|fires))",
    re.IGNORECASE,
)

# Multi-goal gated-on clause (g-115-342, iter-114). Catches "Gated on g-X
# <descriptor> + g-Y <descriptor>" form where the "+" or "and" connector
# joins two or more dependency goal-ids inside a single gating clause.
# Captures the clause text up to the first period or semicolon; the caller
# then extracts ALL g-ids within that clause. This handles g-268-10's
# "Gated on g-268-09 measurement results + g-271-12 BitNet capacity unlock"
# narrative — DEP_PROXIMITY would only catch one g-id with a "gated on"
# alternation, missing the second after the "+" connector.
DEP_GATED_CLAUSE = re.compile(
    r"gated\s+on\s+([^.;]+?)(?:[.;]|$)",
    re.IGNORECASE,
)
_GID_RE = re.compile(r"g-\d+-\d+", re.IGNORECASE)

# Three additional precondition_unmet pattern handlers (g-115-340, iter-110).
# Catch chronic narrative defers that the dep regexes above can't match:
#   ELAPSED:  "precondition_unmet: NNh_elapsed_since_g-XXX-YY_<event>" — clears
#             when cited goal completed AND elapsed >= NN hours.
#   TIMEGATE: "precondition_unmet: time-gate not_before=YYYY-MM-DD[Thh:mm:ss]" —
#             RECOGNIZED-ONLY (not auto-cleared). Per g-001-193, narrative
#             time-gates should migrate to the structured `deferred_until`
#             field consumed by goal-selector. Reporting a recognized
#             pattern surfaces the migration recommendation; auto-clearing
#             would short-circuit the structural fix.
#   STUDIO:   "precondition_unmet: roblox_studio_session_required" —
#             RECOGNIZED-ONLY. External user-action gate; cannot auto-clear.
#             Aggregates count for batched-notification routing.
PRECON_ELAPSED_RE = re.compile(
    r"precondition_unmet:\s*(\d+(?:\.\d+)?)h_elapsed_since_(g-\d+-\d+)(?:_\w+)?",
    re.IGNORECASE,
)
PRECON_TIMEGATE_RE = re.compile(
    r"precondition_unmet:\s*time[-_]gate\s+not_before=(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)",
    re.IGNORECASE,
)
PRECON_STUDIO_RE = re.compile(
    r"precondition_unmet:\s*(roblox_studio_session_required|domain_session_required)",
    re.IGNORECASE,
)

# g-001-271: Studio source freshness pattern. Recognizes defers whose
# precondition is "Studio has sync'd Lua post-<dep-commit>" — typically with
# an _AND_ clause naming a Studio-side test-scenario event. The dep portion
# is agent-checkable via `roblox-studio.sh deploy-state` (compares plugin's
# POSTed PLUGIN_VERSION against ROBLOX_REPO HEAD short-sha). The _AND_
# portion is genuinely Studio-action gated and never auto-clears here.
#
# Auto-clear semantics:
#   - dep present AND completed AND probe shows deploy_is_current=true AND
#     no _AND_ clause → CLEAR (whole defer satisfied)
#   - dep present AND completed AND probe.deploy_is_current=true AND
#     _AND_ clause present → SKIP with "source verified; remaining: <clause>"
#     (surfaces the actual remaining blocker per .claude/rules/verify-before-
#     assuming.md and outcome 4 of g-001-271)
#   - dep missing / incomplete / probe stale / probe inconclusive → SKIP
#     with explanatory reason naming the failing leg
PRECON_STUDIO_SOURCE_RE = re.compile(
    r"precondition_unmet:\s*studio_source_at_post_(g[-_]\d+[-_]\d+)_commit"
    r"(?:_AND_(\w+))?",
    re.IGNORECASE,
)

# Pattern (d) — precondition_unmet: <jsonpath><op><value> [in <source-hint>]
# JSON-path numeric comparator (g-115-342, iter-114). Resolves the dotted
# jsonpath against a configured data source and clears when the actual value
# satisfies the operator+value condition. First wired source: processor-run
# summary (`processor-run.sh summary` returns run_summary.json on stdout).
# Trigger: dotted path beginning with `summary.` (e.g., `summary.treesRefined`).
# Future sources to consider: pipeline-meta, aspirations-query auto-filer
# count, etc. — extend `_resolve_jsonpath_source` rather than the regex.
# OUT OF SCOPE for this handler:
#   - Free-form prose narratives (g-181-04, g-226-65) — pure narrative is
#     not regex-extensible; converting to a structured precondition_unmet
#     phrasing is the right fix, not regex stretching here.
#   - Phase-completion gates ("Phase N <name> gate") — separate semantic;
#     would need its own handler that maps phase names to processor-run
#     phase status. Left for future Idea filing if 100h+ defer accumulates.
#   - Count-based "Nth_<event>" — separate semantic; would need a counter
#     source (auto-filer count via aspirations-query). Left for future Idea.
PRECON_JSONPATH_RE = re.compile(
    r"precondition_unmet:\s*([a-z_][\w]*(?:\.[a-z_][\w]*)+)\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _extract_dep_ids(text: str) -> list[str]:
    """Return all goal-ids cited as dependencies in defer_reason text."""
    ids: list[str] = []
    for m in DEP_STRUCTURED.finditer(text):
        ids.append(m.group(1))
    for m in DEP_PROXIMITY.finditer(text):
        gid = m.group(1) or m.group(2)
        if gid:
            ids.append(gid)
    # Multi-goal "gated on X + Y" clause: capture clause text and extract all
    # g-ids inside (handles connectors like "+", "and", "&"). The clause
    # boundary is period/semicolon/end-of-text — parenthetical g-ids outside
    # the clause (e.g., "(g-115-330 sibling pattern)") are correctly excluded.
    for cm in DEP_GATED_CLAUSE.finditer(text):
        for gm in _GID_RE.finditer(cm.group(1)):
            ids.append(gm.group(0))
    # De-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for g in ids:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _classify_time_gated_dep(g: dict, by_id: dict) -> dict | None:
    """Recognize-only (g-115-2098): deferred_until goal whose defer_reason
    names dependency goal-ids that have ALL completed.

    deferred_until stays authoritative (g-001-193) — this NEVER clears.
    But when the cited deps complete EARLY (before the gate date), the goal
    is an early-completion candidate the LLM should review: estimate-only
    gates ("resolve after g-X completes", date = outer-bound guess) can be
    acted on now; observation windows (resolves_no_earlier_than semantics,
    where elapsed TIME is the point) must stand. Auto-clearing cannot
    distinguish the two — a surfaced classification can (canonical case:
    g-115-2052 sat gated to 07-16 while its census dep g-115-2051 completed
    07-13 and fully resolved the underlying hypothesis; only manual
    grooming caught it). Mirrors the precon_timegate recognize-only
    precedent.
    """
    reason = g.get("defer_reason") or ""
    if not reason:
        return None
    dep_ids = _extract_dep_ids(reason)
    if not dep_ids:
        return None
    statuses = {did: (by_id.get(did) or {}).get("status") for did in dep_ids}
    if not all(s == "completed" for s in statuses.values()):
        return None
    return {"goal_id": g.get("id"), "action": "skipped",
            "pattern": "deps_complete_time_gated",
            "dep_ids": dep_ids,
            "reason": (f"all cited deps completed ({', '.join(dep_ids)}) but "
                       f"deferred_until={g.get('deferred_until')} holds the goal — "
                       f"early-completion candidate: review whether the time gate is "
                       f"still load-bearing (estimate-only gates can resolve now; "
                       f"observation windows must stand). Never auto-cleared.")}


def _try_precon_elapsed(reason: str, by_id: dict) -> dict | None:
    """Pattern (a) — precondition_unmet: NNh_elapsed_since_g-XXX-YY_<event>.

    Clears when cited dep goal is completed AND `now - completed_date` >=
    target hours. Catches deploy/ship soak-window defers (e.g., g-115-192
    waiting 24h after g-115-191's favicon deploy).
    """
    m = PRECON_ELAPSED_RE.search(reason)
    if not m:
        return None
    target_hours = float(m.group(1))
    dep_id = m.group(2)
    dep = by_id.get(dep_id)
    if not dep:
        return {"action": "skipped",
                "reason": f"precon_elapsed: dep {dep_id} not found in queues"}
    if dep.get("status") != "completed":
        return {"action": "skipped",
                "reason": f"precon_elapsed: dep {dep_id} status={dep.get('status')}"}
    completed_ts = dep.get("completed_at") or dep.get("completed_date")
    if not completed_ts:
        return {"action": "skipped",
                "reason": f"precon_elapsed: dep {dep_id} completed but no completed_at/completed_date"}
    elapsed = _age_hours(completed_ts)
    if elapsed is None or elapsed < target_hours:
        elapsed_str = f"{elapsed:.1f}h" if elapsed is not None else "unparseable"
        return {"action": "skipped",
                "reason": f"precon_elapsed: dep {dep_id} elapsed={elapsed_str} < target={target_hours}h"}
    return {"action": "clear",
            "reason": f"precon_elapsed: dep {dep_id} elapsed={elapsed:.1f}h >= target={target_hours}h",
            "dep_ids": [dep_id]}


def _try_precon_timegate(reason: str) -> dict | None:
    """Pattern (b) — precondition_unmet: time-gate not_before=DATE.

    Per g-001-193 finding: narrative time-gates should migrate to the
    structured `deferred_until` field consumed by goal-selector. This
    handler RECOGNIZES the pattern but does NOT auto-clear — auto-clearing
    would short-circuit the structural fix and re-encode the same regex-
    extension drift the prior investigation explicitly rejected.
    Reports a migration recommendation as the skip reason.
    """
    m = PRECON_TIMEGATE_RE.search(reason)
    if not m:
        return None
    target = m.group(1)
    return {"action": "skipped",
            "reason": (f"precon_timegate: not_before={target} — narrative time-gate "
                       f"recognized; recommend migration to deferred_until structured "
                       f"field (g-001-193 finding)")}


def _try_precon_studio(reason: str) -> dict | None:
    """Pattern (c) — precondition_unmet: roblox_studio_session_required.

    External user-action requirement (user must open Roblox Studio and
    invoke the smoke test). Cannot auto-clear. Recognized-only so the
    sweep classifies these distinctly from "no recognized dependency
    pattern" and the caller can aggregate count for batched-notification
    routing when 3+ accumulate (per studio-verify-queue protocol).
    """
    m = PRECON_STUDIO_RE.search(reason)
    if not m:
        return None
    return {"action": "skipped",
            "reason": ("precon_studio: roblox_studio_session_required — user-action "
                       "gate, queue for batched smoke test (no auto-clear)")}


def _try_precon_studio_source(reason: str, by_id: dict) -> dict | None:
    """Pattern (e) — precondition_unmet: studio_source_at_post_<dep>_commit[_AND_<event>].

    Recognizes Studio source freshness defers (g-274-18, g-274-23 family).
    Dep portion is agent-checkable via roblox-studio.sh deploy-state which
    compares plugin's POSTed PLUGIN_VERSION against ROBLOX_REPO HEAD
    short-sha (g-275-08 / rb-711 infrastructure). _AND_ clause names a
    Studio-action gate that this handler cannot resolve — surface it as
    the remaining blocker per outcome 4 of g-001-271.

    Auto-clear ONLY when both (a) dep is completed AND (b) probe shows
    deploy_is_current=true AND (c) no _AND_ clause is present.
    """
    m = PRECON_STUDIO_SOURCE_RE.search(reason)
    if not m:
        return None
    # Goal-id captured from regex may have underscores (defer text uses
    # `g_274_22` form because hyphens would terminate the regex word class).
    # Normalize to the canonical hyphen form for queue lookup.
    dep_id_raw = m.group(1)
    dep_id = dep_id_raw.replace("_", "-")
    and_clause = m.group(2)  # None when no _AND_ part

    dep = by_id.get(dep_id)
    if not dep:
        return {"action": "skipped",
                "reason": f"precon_studio_source: dep {dep_id} not found in queues"}
    if dep.get("status") != "completed":
        return {"action": "skipped",
                "reason": f"precon_studio_source: dep {dep_id} status={dep.get('status')!r}"}

    # Probe Studio plugin freshness inline via urllib + git log. roblox-studio.sh
    # deploy-state is the canonical wrapper (g-275-08), but subprocess.run from
    # Python on Windows cannot reliably invoke bash scripts under OneDrive paths
    # — Git Bash subprocess fails to materialize OneDrive Files-On-Demand
    # entries that succeed for direct Python file I/O (verified rc=127 reproduce
    # 2026-05-07). We replicate the script's logic in-process: GET /api/plugin-
    # version + `git log -1 --format=%h` against ROBLOX_REPO. Same probe shape,
    # same match policy (g-001-273: layered label / exact / ancestry).
    # If `roblox-studio.sh deploy-state` evolves the policy, mirror the change
    # here. (Reference: rb-373 deploy-vs-HEAD pattern; g-275-08 plugin-version
    # POST infrastructure; g-001-273 ancestry-policy refinement.)
    import urllib.request
    import urllib.error
    bridge_base = os.environ.get("AYOBRIDGE_BASE", "http://127.0.0.1:28080")
    try:
        req = urllib.request.Request(bridge_base + "/api/plugin-version")
        with urllib.request.urlopen(req, timeout=5) as resp:
            probe_body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"action": "skipped",
                "reason": f"precon_studio_source: bridge unreachable at {bridge_base} ({e.reason}) — start-bridge first"}
    except Exception as e:
        return {"action": "skipped",
                "reason": f"precon_studio_source: bridge probe failed: {e}"}

    roblox_repo_env = os.environ.get("ROBLOX_REPO", "").strip()
    if not roblox_repo_env:
        return {"action": "skipped",
                "reason": "precon_studio_source: ROBLOX_REPO env var not set — required for studio-source check"}
    roblox_repo = Path(roblox_repo_env)
    if not (roblox_repo / ".git").exists():
        return {"action": "skipped",
                "reason": f"precon_studio_source: ROBLOX_REPO not found at {roblox_repo} (set ROBLOX_REPO env)"}
    rc, head_out, head_err = _run(["git", "-C", str(roblox_repo), "log", "-1", "--format=%h"])
    if rc != 0 or not head_out.strip():
        return {"action": "skipped",
                "reason": f"precon_studio_source: git log failed in {roblox_repo}: rc={rc} err={(head_err or '')[:80]!r}"}
    head_sha = head_out.strip()

    reported_version = probe_body.get("version")
    if not reported_version:
        # Bridge reachable but plugin has not POSTed yet (Studio offline / disabled).
        details_reason = probe_body.get("reason", "plugin has not reported")
        return {"action": "skipped",
                "reason": (f"precon_studio_source: dep {dep_id} complete BUT probe inconclusive "
                           f"(no_plugin_version: {details_reason}, head_sha={head_sha!r})")}

    # Match policy mirrors roblox-studio.sh deploy-state (g-001-273): layered
    # check — goal-id label fast-path, then exact short/long sha startswith,
    # then `git merge-base --is-ancestor` for the bump-then-commit steady
    # state where reported is HEAD's parent. Without ancestry, the bump cycle
    # would always read "stale" because bumping PLUGIN_VERSION advances HEAD
    # by 1 and the plugin reports its own bump-time HEAD (now HEAD's parent).
    ancestry_rc = None
    if reported_version.startswith("g-"):
        # Plugin version is a goal-id label, not a sha. Per g-275-08 design
        # this is an info state, not a stale signal — but it ALSO means we
        # can't auto-clear the studio_source defer because we have no SHA
        # to compare. Treat as inconclusive.
        deploy_current = None
        probe_status = "label_not_sha"
    elif (reported_version.startswith(head_sha)
          or head_sha.startswith(reported_version)
          or reported_version == head_sha):
        deploy_current = True
        probe_status = "current"
    else:
        # Ancestry probe. exit 0 = ancestor (current modulo bump-cycle lag),
        # exit 1 = not an ancestor (truly stale or unrelated branch),
        # exit 128 = unresolvable ref (sha doesn't exist in repo).
        rc_anc, _stdout_anc, _stderr_anc = _run(
            ["git", "-C", str(roblox_repo), "merge-base", "--is-ancestor",
             reported_version, "HEAD"]
        )
        ancestry_rc = rc_anc
        if rc_anc == 0:
            deploy_current = True
            probe_status = "ancestor_current"
        elif rc_anc == 1:
            deploy_current = False
            probe_status = "stale"
        else:
            deploy_current = None
            probe_status = "unknown_ref"

    if deploy_current is True:
        # Source verified. Check AND clause.
        if and_clause:
            return {"action": "skipped",
                    "reason": (f"precon_studio_source: source verified "
                               f"(probe.status={probe_status}, plugin={reported_version!r}, "
                               f"dep {dep_id} complete); remaining blocker: "
                               f"{and_clause} (Studio-side action gate)"),
                    "dep_ids": [dep_id]}
        return {"action": "clear",
                "reason": (f"precon_studio_source: dep {dep_id} complete AND plugin "
                           f"current (reported={reported_version!r} matches head={head_sha!r})"),
                "dep_ids": [dep_id]}
    if deploy_current is None:
        # Inconclusive — label-not-sha or unknown_ref. Bridge-unreachable and
        # no-plugin-version paths return earlier (lines ~354/376). probe_body is
        # the bridge response; details may carry server-side reason text. (Bug
        # fix: previously referenced an undefined `probe` here — would NameError
        # if the label_not_sha branch fired.)
        details_reason = (probe_body.get("details") or {}).get("reason", "")
        return {"action": "skipped",
                "reason": (f"precon_studio_source: dep {dep_id} complete BUT probe inconclusive "
                           f"(status={probe_status!r}, ancestry_rc={ancestry_rc!r}, "
                           f"reason={details_reason!r})")}
    return {"action": "skipped",
            "reason": (f"precon_studio_source: dep {dep_id} complete BUT plugin stale "
                       f"(reported={reported_version!r} != head={head_sha!r})")}


# ----- JSON-path source resolution + comparator helpers (g-115-342) ---------
# Sentinel used by _walk_jsonpath to distinguish "missing key" from a real
# falsy value (None, 0, ""). Module-level so identity comparisons stay cheap.
_MISSING = object()


def _resolve_jsonpath_source(jsonpath: str) -> tuple[dict | None, str | None]:
    """Resolve a precondition jsonpath to a (data, source_name_or_err) tuple.

    Currently wires one source: `summary.*` paths resolve against
    `world/scripts/processor-run.sh summary` (returns run_summary.json on
    stdout; default JSON mode). Returns (None, error_string) when the
    source is unavailable or the prefix has no registered source — caller
    surfaces error_string in the skip diagnostic so future readers can
    distinguish real not-yet-met conditions from infrastructure failures.

    Adding a new source: register the prefix here (`pipeline_meta.*`,
    `auto_filer_count.*`, etc.) and shell out to the canonical companion
    script. Per probe-with-canonical-code-path: do NOT inline equivalent
    discovery commands — call the script the live system uses.
    """
    if jsonpath.lower().startswith("summary."):
        # SSOT: PROCESSOR_DIR is defined in world/scripts/processor-run.sh:28.
        # We read run_summary.json directly because shelling to bash from
        # Python on Windows is unreliable (PATH-resolved `bash` may be WSL
        # bash, which exits 127 on Win-style paths). The script's `summary`
        # subcommand (line 599) is `cat run_summary.json` — same semantics
        # without the cross-shell hazard. If PROCESSOR_DIR ever moves,
        # set PROCESSOR_DIR in the environment AND update processor-run.sh:28.
        processor_dir_env = os.environ.get("PROCESSOR_DIR", "").strip()
        if not processor_dir_env:
            return (None,
                    "PROCESSOR_DIR env var not set — required for summary.* deferral source")
        runs_dir = Path(processor_dir_env) / "output" / "runs"
        if not runs_dir.exists():
            return (None, f"processor runs dir not found at {runs_dir}")
        run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
        if not run_dirs:
            return (None, "no processor run directories found")
        latest = max(run_dirs, key=lambda p: p.stat().st_mtime)
        summary_file = latest / "run_summary.json"
        if not summary_file.exists():
            return (None, f"latest run {latest.name} has no run_summary.json (run may have crashed)")
        try:
            return (json.loads(summary_file.read_text()),
                    f"processor-run.summary({latest.name})")
        except Exception as e:
            return (None, f"failed to parse {summary_file.name}: {e}")
    return (None, f"no source registered for jsonpath prefix '{jsonpath.split('.')[0]}'")


def _walk_jsonpath(data, path: str):
    """Walk a dotted path through a dict; return value or sentinel _MISSING."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _compare_numeric(actual, op: str, expected: float) -> bool:
    """Numeric compare per operator. Returns False on type mismatch."""
    try:
        a = float(actual)
    except (TypeError, ValueError):
        return False
    if op == ">":  return a > expected
    if op == "<":  return a < expected
    if op == ">=": return a >= expected
    if op == "<=": return a <= expected
    if op == "==": return a == expected
    if op == "!=": return a != expected
    return False


def _try_precon_jsonpath(reason: str) -> dict | None:
    """Pattern (d) — precondition_unmet: <jsonpath><op><value>.

    Resolves jsonpath against a configured data source (currently processor-
    run summary). Clears when actual value satisfies operator+value
    condition. Skips with explanatory diagnostic when source is unavailable,
    path is missing, or condition not met. Mechanism is structurally correct:
    a defer that does not clear today (state hasn't advanced) clears on the
    next sweep where state does advance, without further code changes.
    """
    m = PRECON_JSONPATH_RE.search(reason)
    if not m:
        return None
    path, op, val_str = m.group(1), m.group(2), m.group(3)
    try:
        expected = float(val_str)
    except ValueError:
        return {"action": "skipped",
                "reason": f"precon_jsonpath: invalid numeric value {val_str!r}"}
    data, src_or_err = _resolve_jsonpath_source(path)
    if data is None:
        return {"action": "skipped",
                "reason": f"precon_jsonpath: source unavailable ({src_or_err})"}
    actual = _walk_jsonpath(data, path)
    if actual is _MISSING:
        return {"action": "skipped",
                "reason": f"precon_jsonpath: path '{path}' missing in {src_or_err}"}
    if _compare_numeric(actual, op, expected):
        return {"action": "clear",
                "reason": f"precon_jsonpath: {path}={actual} {op} {expected} (source={src_or_err})"}
    return {"action": "skipped",
            "reason": f"precon_jsonpath: {path}={actual} does not satisfy {op}{expected} (source={src_or_err})"}


def _try_new_patterns(reason: str, by_id: dict) -> dict | None:
    """Try all five narrative-pattern handlers in priority order.

    g-115-340 added precon_elapsed/precon_timegate/precon_studio.
    g-115-342 added precon_jsonpath as the last fallback.
    g-001-271 added precon_studio_source — Studio plugin freshness via
    roblox-studio.sh deploy-state (g-275-08 / rb-711 infrastructure).
    Order matters: precon_studio_source is checked BEFORE precon_studio
    because the more-specific pattern (`studio_source_at_post_*`) shares
    the `studio` keyword with the broader user-action gate; running the
    specific check first prevents a false skip via the broader handler.
    precon_elapsed is the cheapest auto-clear path (no external probe);
    the others are state-dependent (timegate is recognize-only;
    studio is user-action; studio_source calls a probe; jsonpath reads
    a file). Returns a dict with keys
    {action, reason, [dep_ids,] pattern} when any handler matches;
    None when none match (caller falls back to the "no recognized
    dependency pattern" skip).
    """
    r = _try_precon_elapsed(reason, by_id)
    if r is not None:
        r["pattern"] = "precon_elapsed"
        return r
    r = _try_precon_studio_source(reason, by_id)
    if r is not None:
        r["pattern"] = "precon_studio_source"
        return r
    r = _try_precon_timegate(reason)
    if r is not None:
        r["pattern"] = "precon_timegate"
        return r
    r = _try_precon_studio(reason)
    if r is not None:
        r["pattern"] = "precon_studio"
        return r
    r = _try_precon_jsonpath(reason)
    if r is not None:
        r["pattern"] = "precon_jsonpath"
        return r
    return None


def _run(argv, input_text=None):
    result = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode, result.stdout, result.stderr


def _py(args, input_text=None):
    return _run([sys.executable] + args, input_text=input_text)


def _read_goals(source):
    """Return list of goals from world or agent queue.

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    Decode path is g-115-766-tolerant via `_rt.tolerant_decode_aggregate`
    (extracted from this site + 3 siblings via g-115-949). Replaces the
    prior silent-collapse `except json.JSONDecodeError: return []` that
    would hide corruption behind a "no goals to recheck" no-op.

    RtError is FATAL (sys.exit(1)) per guard-383: the caller at line 702
    merges this function's world+agent reads into a single aggregate
    (`_read_goals("world") + _read_goals("agent")`), and a silent [] from
    one source would poison the merged set with a complete-looking lie —
    cleared defers based on a half-view. Mirrors the canonical pattern in
    parent-supersession-sweep.py:158-164 / blocker-recheck.py / the rest
    of the g-115-797 catalogue. The defer-recheck.sh wrapper swallows
    non-zero exit so a transient daemon outage just leaves the defers
    untouched for the next sweep — no regression on daemon-up.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        # guard-383: source error is FATAL for the N>=2-source aggregator
        # pattern (line 702 merges "world" + "agent"). A silent [] would
        # poison the merged aggregate with a complete-looking lie.
        print(f"[defer-recheck] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _rt.tolerant_decode_aggregate(f"[defer-recheck] {source}", out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", ""))
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _clear_defer(source, goal_id):
    """Clear defer_reason via aspirations.py update-goal.

    INVARIANT: uses sys.executable directly (not a bash subprocess). Shelling
    through bash for core/scripts/*.sh wrappers has been unreliable from
    Python on Windows — bash can resolve to WSL bash.exe with different PATH
    resolution. Same rationale as blocker-recheck.py _py(). The shell wrapper
    aspirations-update-goal.sh just sources _paths.sh + execs aspirations.py,
    so calling aspirations.py directly loses nothing functional."""
    rc, _, _ = _py([str(SCRIPT_DIR / "aspirations.py"),
                    "--source", source, "update-goal",
                    goal_id, "defer_reason", "null"])
    return rc == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=2.0,
                    help="Minimum defer age before recheck (default 2h)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually clear defer_reason (default: dry-run)")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log for defer-false-positive "
                          "rate measurement (rb-468). Default: "
                          "<WORLD_PATH>/defer-recheck-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()
    metrics_path = _resolve_metrics_log(args.metrics_log)

    # Build goal index for dependency lookup (both sources).
    all_goals = _read_goals("world") + _read_goals("agent")
    by_id = {g.get("id"): g for g in all_goals}

    scanned = 0
    eligible = 0
    cleared = 0
    would_clear = []
    details = []
    time_gated_early = []  # g-115-2098: deps-complete-but-time-gated goal ids

    for g in all_goals:
        scanned += 1
        if not g.get("defer_reason"):
            continue
        # Skip goals that are not pending/in-progress — a completed or blocked
        # goal has defer_reason as historical residue, not a live constraint.
        # Clearing cosmetic-only text has no selection impact.
        if g.get("status") not in ("pending", "in-progress"):
            continue
        # Skip goals with structured deferred_until set — that field is the
        # authoritative time gate consumed by goal-selector.py. defer-recheck's
        # narrative-clearing role is the regex-fallback path for goals without
        # a structured gate; when one IS present, defer_reason is parallel
        # narrative and clearing it would not change selection behavior.
        # See g-001-193 investigation (2026-04-27) and docstring above.
        # g-115-2098: before skipping, surface (recognize-only, never clear)
        # the early-completion case — all cited deps completed but the time
        # gate still holds. See _classify_time_gated_dep.
        if g.get("deferred_until"):
            tg = _classify_time_gated_dep(g, by_id)
            if tg is not None:
                time_gated_early.append(tg["goal_id"])
                details.append(tg)
            continue
        age_h = _age_hours(g.get("defer_reason_set_at") or g.get("started") or g.get("created_at"))
        if age_h is None or age_h < args.max_age_hours:
            continue
        eligible += 1

        reason = g.get("defer_reason") or ""
        dep_ids = _extract_dep_ids(reason)
        if not dep_ids:
            # Try the three g-115-340 precondition_unmet pattern handlers
            # before falling back to "no recognized dependency pattern".
            new_match = _try_new_patterns(reason, by_id)
            if new_match is not None:
                if new_match["action"] == "clear":
                    entry = {"goal_id": g["id"], "source": g["_source"],
                             "age_hours": round(age_h, 1),
                             "dep_ids": new_match.get("dep_ids", []),
                             "action": "would_clear",
                             "pattern": new_match["pattern"]}
                    would_clear.append(g["id"])
                    if args.apply:
                        ok = _clear_defer(g["_source"], g["id"])
                        entry["action"] = "cleared" if ok else "clear_failed"
                        if ok:
                            cleared += 1
                            _append_metric(metrics_path, {
                                "type": "defer_cleared",
                                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                                "goal_id": g["id"],
                                "source": g["_source"],
                                "aspiration_id": g.get("_aspiration_id"),
                                "age_hours_at_clear": round(age_h, 2),
                                "dep_ids": new_match.get("dep_ids", []),
                                "clear_reason": new_match["pattern"],
                                "agent": os.environ.get("MIND_AGENT", "") or None,
                            })
                    details.append(entry)
                else:
                    details.append({"goal_id": g["id"], "age_hours": round(age_h, 1),
                                    "action": new_match["action"],
                                    "reason": new_match["reason"],
                                    "pattern": new_match["pattern"]})
                continue
            details.append({"goal_id": g["id"], "age_hours": round(age_h, 1),
                            "action": "skipped", "reason": "no recognized dependency pattern"})
            continue

        # All cited deps must be completed before we clear — partial
        # completion leaves real reason to wait.
        dep_statuses = []
        missing = []
        for did in dep_ids:
            dep = by_id.get(did)
            if not dep:
                missing.append(did)
            else:
                dep_statuses.append({"id": did, "status": dep.get("status")})

        if missing:
            details.append({"goal_id": g["id"], "age_hours": round(age_h, 1),
                            "dep_ids": dep_ids, "action": "skipped",
                            "reason": f"dep(s) not found: {missing}"})
            continue

        incomplete = [d for d in dep_statuses if d["status"] != "completed"]
        if incomplete:
            details.append({"goal_id": g["id"], "age_hours": round(age_h, 1),
                            "dep_ids": dep_ids, "dep_statuses": dep_statuses,
                            "action": "skipped",
                            "reason": f"{len(incomplete)}/{len(dep_statuses)} deps not completed"})
            continue

        # All cited deps completed — clearable.
        entry = {"goal_id": g["id"], "source": g["_source"], "age_hours": round(age_h, 1),
                 "dep_ids": dep_ids, "action": "would_clear"}
        would_clear.append(g["id"])

        if args.apply:
            ok = _clear_defer(g["_source"], g["id"])
            entry["action"] = "cleared" if ok else "clear_failed"
            if ok:
                cleared += 1
                # LOAD-BEARING: log ONLY after the clear succeeds. Reordering
                # this to log first would produce phantom metrics on failed
                # clears and corrupt the defer-false-positive rate (rb-468).
                _append_metric(metrics_path, {
                    "type": "defer_cleared",
                    "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                    "goal_id": g["id"],
                    "source": g["_source"],
                    "aspiration_id": g.get("_aspiration_id"),
                    "age_hours_at_clear": round(age_h, 2),
                    "dep_ids": dep_ids,
                    "clear_reason": "deps_completed",
                    "agent": os.environ.get("MIND_AGENT", "") or None,
                })
        details.append(entry)

    # Per-run summary event for the metrics log. Emitted regardless of
    # whether anything cleared, so consumers can compute a true rate with
    # a real denominator: (cleared events) / (eligible_per_run events).
    # Dry-runs log too, marked apply=false — useful to see how often the
    # sweep surfaces clearable defers that would be cleared by --apply.
    _append_metric(metrics_path, {
        "type": "run_summary",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "scanned": scanned,
        "eligible": eligible,
        "would_clear_count": len(would_clear),
        "cleared": cleared,
        "apply": args.apply,
        "max_age_hours": args.max_age_hours,
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "cleared": cleared,
        "would_clear": would_clear,
        "time_gated_early_candidates": time_gated_early,
        "details": details,
        "apply": args.apply,
        "metrics_log": str(metrics_path) if metrics_path else None,
    }

    if args.output == "human":
        print(f"scanned={scanned} eligible={eligible} would_clear={len(would_clear)} cleared={cleared}")
        for d in details:
            print(f"  {d['goal_id']}: {d['action']} ({d.get('reason','')})")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
