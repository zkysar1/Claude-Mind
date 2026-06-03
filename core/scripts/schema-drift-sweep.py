#!/usr/bin/env python3
"""Schema-Drift Sweep — weekly audit of JSONL stores against SKILL.md pseudocode.

Reads core/config/schema-registry.yaml. For each registered store:
  1. Probe the live JSONL file: verify every registry live_field is present in
     the last record. If a registry-claimed field is missing, exit 2
     (registry-itself-drifted — the registry must be fixed first).
  2. Grep SKILL.md files + core/config/**/*.md for each stale_field name.
     Filter hits through the registry's allowlist.
  3. Report drift_hits (real drift) + allowlisted_hits (documented exceptions).

Exit codes:
  0 — clean (no drift, no registry issues)
  1 — drift detected (one or more non-allowlisted stale_field mentions)
  2 — registry-itself-drifted (a live_field is absent in live record)
  3 — usage / config error

Usage:
  schema-drift-sweep.py --all                       # all stores
  schema-drift-sweep.py --store pattern-signatures  # single store
  schema-drift-sweep.py --all --create-goals        # auto-file cross-agent handoff goals
  schema-drift-sweep.py --all --output json         # machine-readable

Design notes:
  - Written as a diagnostic, not a gate. Fail-loud on registry drift (exit 2)
    because that means downstream drift reports are untrustworthy.
  - Skips stores whose jsonl_path includes `<agent>/` when MIND_AGENT is unset.
  - World stores are resolved via _paths.WORLD_DIR; meta stores via META_DIR.
  - Grep uses Python regex, not subprocess, for portability and glob allowlist matching.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Path resolution shared with the rest of the framework.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402
from _long_path import open_long_path  # noqa: E402  —  / rb-450
from _runtime_bash import bash_cmd  # noqa: E402  # : Windows-safe bash resolution

try:
    import yaml
except ImportError:
    print("FATAL: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(3)


REGISTRY_PATH = _paths.CONFIG_DIR / "schema-registry.yaml"
PROJECT_ROOT = _paths.PROJECT_ROOT

# Grep targets — every .md file that could host schema pseudocode.
GREP_ROOTS = [
    PROJECT_ROOT / ".claude" / "skills",
    PROJECT_ROOT / ".claude" / "rules",
    _paths.CONFIG_DIR,
]


# ────────────────────────────── path resolution ──────────────────────────────

def resolve_store_path(virtual_path: str) -> Path | None:
    """Map a registry jsonl_path to a real filesystem path.

    Supports the three known prefixes:
      world/...     → WORLD_DIR
      meta/...      → META_DIR
      <agent>/...   → PROJECT_ROOT/<AGENT_NAME>/... (None if agent unbound)
    Unrecognized prefixes fall through to PROJECT_ROOT and will likely miss.
    """
    if virtual_path.startswith("world/"):
        return _paths.WORLD_DIR / virtual_path[6:]
    if virtual_path.startswith("meta/"):
        return _paths.META_DIR / virtual_path[5:]
    if virtual_path.startswith("<agent>/"):
        if not _paths.AGENT_DIR:
            return None  # signal "skip — agent unbound"
        return _paths.AGENT_DIR / virtual_path[len("<agent>/"):]
    return PROJECT_ROOT / virtual_path


# ────────────────────────────── live-field probe ──────────────────────────────

def _get_dotted(record, dotted: str):
    """Identical semantic to audit-schema-gate.py and jsonl-field-probe.py.

    A field is "present" only if every intermediate key exists AND the terminal
    value is non-null. A key written as null counts as absent — we want the
    registry to catch "schema declares this but nobody writes it."
    """
    cur = record
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return False
        if part not in cur:
            return False
        cur = cur[part]
    return cur is not None


def probe_live_fields(path: Path, live_fields: list, look_back: int = 10) -> tuple[list, str | None]:
    """Probe the last `look_back` JSONL records and check each live_field for presence.

    A field is considered "present" if it appears (non-null) in ANY of the
    last `look_back` records. This handles stage-conditional fields (e.g.
    pipeline.claim required only on non-discovered records) and lazily-
    populated fields (e.g. pattern-signatures.utilization.* set on first
    retrieval) without false REGISTRY-DRIFTED firings.

    Origin: g-001-226. Replaces single-last-record probe that fired
    REGISTRY-DRIFTED whenever the most-recent record happened to be in a
    state that legitimately lacks the field. Empirics from g-001-224 /
    g-001-225 showed both pipeline.claim/source_goal and pattern-signatures
    .utilization.* are present in 32–94% of records but absent in the
    latest — a single-record probe cannot model that.

    Returns (missing_fields, error). `missing_fields` lists live_fields
    absent across ALL last `look_back` records (true registry drift). `error`
    is None on success; set when the file can't be read. When error is set,
    missing_fields is [] — the caller must distinguish "can't probe" from
    "clean".
    """
    if not path or not path.is_file():
        return ([], f"file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return ([], f"read failed: {type(e).__name__}: {e}")

    # Collect up to `look_back` parseable records, walking backwards from EOF.
    records = []
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(records) >= look_back:
            break

    if not records:
        return ([], "no parseable records")

    # A live_field is present if ANY of the recent records has it non-null.
    missing = [
        f for f in live_fields
        if not any(_get_dotted(r, f) for r in records)
    ]
    return (missing, None)


# ──────────────────────────── grep for stale fields ────────────────────────────

def iter_md_files():
    """Yield every .md file under the grep roots."""
    for root in GREP_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            yield p


# Identifier-boundary regex: prefix must NOT be a word char (so `my_hit_rate`
# and `chit_rate` don't match), but a leading `.` IS allowed (so `sig.hit_rate`
# matches since pseudocode commonly writes `sig.times_triggered > 3`). Suffix
# must NOT be a word char (so `hit_rates` doesn't match `hit_rate`).
def _make_pattern(field_name: str) -> re.Pattern:
    esc = re.escape(field_name)
    return re.compile(rf"(?<![A-Za-z0-9_]){esc}(?![A-Za-z0-9_])")


# Inline marker for content-based exemption (). Survives line shifts
# because the scan reads the source line, not a (file, line) coordinate.
# The marker is the literal substring `DRIFT-EXEMPT` anywhere on the same
# line as the stale field reference. Comment syntax adapts to the host file:
# `# DRIFT-EXEMPT: pedagogy` (Python/shell), `<!-- DRIFT-EXEMPT: ... -->`
# (Markdown prose), or any other comment form that contains the substring.
# Tag a reason after the colon so future readers see the intent (pedagogy,
# test-input, anti-pattern illustration, rename-documentation) rather than
# rediscovering the rationale from the registry.
_MARKER_TOKEN = "DRIFT-EXEMPT"


def grep_stale_fields(stale_fields: dict) -> list:
    """Return a list of raw hits: {file, line, field, live_equivalent, line_text,
    marker_exempted}.

    `file` is expressed relative to PROJECT_ROOT using forward slashes — that's
    the form allowlist entries use, so downstream filtering is string-equal.
    `marker_exempted` is True when the source line carries an inline
    DRIFT-EXEMPT marker (g-115-234); process_store routes those to the
    allowlisted bucket regardless of registry coordinates.
    """
    patterns = {name: _make_pattern(name) for name in stale_fields}
    hits = []
    for md in iter_md_files():
        try:
            #  / rb-450: long-path retry for >260-char paths.
            # Path.read_text() doesn't expose the retry path; route through
            # open_long_path so MAX_PATH-affected files don't silently
            # disappear from drift sweeps. POSIX no-op.
            with open_long_path(md) as fh:
                text = fh.read()
        except Exception:
            continue
        rel = md.relative_to(PROJECT_ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, rx in patterns.items():
                if rx.search(line):
                    hits.append({
                        "file": rel,
                        "line": lineno,
                        "field": name,
                        "live_equivalent": stale_fields[name],
                        "line_text": line.strip(),
                        "marker_exempted": _MARKER_TOKEN in line,
                    })
    return hits


# ────────────────────────────── allowlist filtering ──────────────────────────────

def allowlist_matches(hit: dict, allowlist: list) -> bool:
    """Return True if `hit` is covered by any allowlist entry.

    Entry format: "<file_glob>:<line_or_*>:<field_or_*>". Each of the three
    positions supports fnmatch-style globs. `*` in the line position matches
    any line; `*` in the field position matches any field.
    """
    file_str = hit["file"]
    line_str = str(hit["line"])
    field_str = hit["field"]
    for entry in allowlist:
        if not isinstance(entry, str) or entry.count(":") < 2:
            continue
        # Split from the RIGHT twice so the file part can contain colons
        # (Windows drive letters, etc.) without breaking the parse.
        file_glob, line_glob, field_glob = entry.rsplit(":", 2)
        if not fnmatch.fnmatch(file_str, file_glob):
            continue
        if line_glob != "*" and line_glob != line_str:
            continue
        if field_glob != "*" and field_glob != field_str:
            continue
        return True
    return False


# ────────────────────────────── store processing ──────────────────────────────

def process_store(store_name: str, store_cfg: dict, allowlist: list) -> dict:
    """Run probe + grep for one store. Returns a result dict.

    Result shape:
      { store, jsonl_path, probe: {status, missing, error},
        drift_hits: [...], allowlisted_hits: [...] }
    status: "clean" | "drift" | "registry-drifted" | "skipped" | "unprobed"
    """
    virtual = store_cfg.get("jsonl_path", "")
    resolved = resolve_store_path(virtual)
    live_fields = store_cfg.get("live_fields", []) or []
    stale_fields = store_cfg.get("stale_fields", {}) or {}

    result = {
        "store": store_name,
        "jsonl_path": virtual,
        "resolved_path": str(resolved) if resolved else None,
        "probe": {"status": "unprobed", "missing": [], "error": None},
        "drift_hits": [],
        "allowlisted_hits": [],
    }

    if resolved is None:
        result["probe"]["status"] = "skipped"
        result["probe"]["error"] = "per-agent store and MIND_AGENT unset"
    else:
        missing, err = probe_live_fields(resolved, live_fields)
        result["probe"]["missing"] = missing
        result["probe"]["error"] = err
        if err:
            result["probe"]["status"] = "unprobed"
        elif missing:
            result["probe"]["status"] = "registry-drifted"
        else:
            result["probe"]["status"] = "clean"

    # Grep for stale field mentions regardless of probe status — a broken probe
    # shouldn't hide pseudocode drift.
    if stale_fields:
        raw_hits = grep_stale_fields(stale_fields)
        for hit in raw_hits:
            # : inline DRIFT-EXEMPT marker on the same line is
            # equivalent to an allowlist entry — survives line shifts. Marker
            # check happens before registry-coordinate match because a marker
            # is the local source of truth for "this site is intentional."
            if hit.get("marker_exempted") or allowlist_matches(hit, allowlist):
                result["allowlisted_hits"].append(hit)
            else:
                result["drift_hits"].append(hit)

    return result


# ────────────────────────────── goal creation ──────────────────────────────

def create_fix_goals(all_results: list, aspiration_id: str = "asp-115") -> int:
    """For each store with drift, create one cross-agent handoff goal.

    Uses aspirations-add-goal.sh. Groups by store so the target agent gets a
    single focused goal per drift cluster rather than one per file:line.
    Returns the count of goals successfully created.
    """
    created = 0
    script = _paths.CORE_ROOT / "scripts" / "aspirations-add-goal.sh"
    if not script.is_file():
        print(f"WARN: {script} not found; skipping goal creation", file=sys.stderr)
        return 0

    # Invoke with a relative path and cwd=PROJECT_ROOT so Git Bash on Windows
    # resolves the script through the shell's current directory rather than
    # parsing a Windows-style absolute path (which fails on MSYS).
    rel_script = script.relative_to(_paths.PROJECT_ROOT).as_posix()

    # Derive the handoff target from team-state.yaml agent_status keys minus
    # MIND_AGENT (lexical sort for determinism). Was hardcoded "alpha" — broke
    # when bravo (or any future N-th agent) ran --create-goals because every
    # drift fix routed to alpha regardless of who detected it. Falls back to
    # "alpha" only when team-state is unreadable or this is a solo-agent world.
    self_agent = os.environ.get("MIND_AGENT", "")
    handoff_target = ""
    try:
        ts_path = _paths.WORLD_DIR / "team-state.yaml"
        if ts_path.is_file():
            with open(ts_path, "r", encoding="utf-8") as f:
                ts = yaml.safe_load(f) or {}
            peers = sorted(a for a in (ts.get("agent_status") or {}).keys()
                           if a != self_agent)
            if peers:
                handoff_target = peers[0]
    except Exception:
        pass  # fail-open to "alpha" — preserves prior behavior on errors

    for r in all_results:
        if not r["drift_hits"]:
            continue
        sites = sorted({h["file"] for h in r["drift_hits"]})
        sites_str = ", ".join(sites[:5]) + (f" (+{len(sites)-5} more)" if len(sites) > 5 else "")
        goal = {
            "title": f"Apply: Fix schema drift in {r['store']} pseudocode",
            "description": (
                f"schema-drift-sweep.py found {len(r['drift_hits'])} stale field "
                f"reference(s) across {len(sites)} file(s) for store {r['store']}. "
                f"Sites: {sites_str}. Live schema: {r['jsonl_path']}. "
                f"Use core/config/schema-registry.yaml stale→live mapping to rewrite."
            ),
            "priority": "MEDIUM",
            "participants": ["agent"],
            "handoff_to": handoff_target,
            "handoff_from": os.environ.get("MIND_AGENT", "unknown"),
            "handoff_created_at": _iso_now(),
            "source": "world",
            # origin-signal-gate: schema-drift-sweep IS a signal source.
            # Cite the drifted store so audits can reconstruct the scan.
            "origin_signal": f"investigate:schema-drift-{r['store']}",
        }
        try:
            proc = subprocess.run(
                bash_cmd(rel_script, "--source", "world", aspiration_id),
                input=json.dumps(goal),
                text=True, capture_output=True, timeout=30,
                cwd=str(_paths.PROJECT_ROOT),
            )
            if proc.returncode == 0:
                created += 1
            else:
                print(f"WARN: goal creation failed for {r['store']}: {proc.stderr.strip()}",
                      file=sys.stderr)
        except Exception as e:
            print(f"WARN: goal creation exception for {r['store']}: {e}", file=sys.stderr)
    return created


def _iso_now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ────────────────────────────── output ──────────────────────────────

def emit_human(all_results: list, created_goals: int):
    print("=" * 70)
    print("SCHEMA-DRIFT SWEEP")
    print("=" * 70)
    any_drift = False
    any_registry_issue = False
    for r in all_results:
        status = r["probe"]["status"]
        drift = len(r["drift_hits"])
        allow = len(r["allowlisted_hits"])
        tag = {
            "clean": "OK",
            "drift": "!!",
            "registry-drifted": "XX",
            "skipped": " .",
            "unprobed": " ?",
        }.get(status, " .")
        print(f"\n[{tag}] {r['store']:24s}  probe={status:18s} drift={drift:2d}  allowlisted={allow:2d}")
        if r["probe"]["missing"]:
            any_registry_issue = True
            print(f"    REGISTRY-DRIFTED: live_fields absent in last record:")
            for f in r["probe"]["missing"]:
                print(f"      - {f}")
        if r["probe"]["error"] and status == "unprobed":
            print(f"    probe error: {r['probe']['error']}")
        if r["drift_hits"]:
            any_drift = True
            print(f"    DRIFT HITS:")
            for h in r["drift_hits"][:10]:
                print(f"      {h['file']}:{h['line']}  {h['field']} -> {h['live_equivalent']}")
            if len(r["drift_hits"]) > 10:
                print(f"      ... and {len(r['drift_hits']) - 10} more")
    print()
    if created_goals:
        print(f"-> Created {created_goals} cross-agent handoff goal(s) for drift fixes.")

    print("=" * 70)
    if any_registry_issue:
        print("RESULT: REGISTRY-DRIFTED -- fix schema-registry.yaml before trusting drift reports.")
    elif any_drift:
        print("RESULT: DRIFT DETECTED -- see sites above.")
    else:
        print("RESULT: CLEAN.")


def emit_json(all_results: list, created_goals: int):
    out = {
        "results": all_results,
        "created_goals": created_goals,
        "summary": {
            "stores": len(all_results),
            "drift_stores": sum(1 for r in all_results if r["drift_hits"]),
            "registry_drifted_stores": sum(
                1 for r in all_results if r["probe"]["status"] == "registry-drifted"
            ),
            "total_drift_hits": sum(len(r["drift_hits"]) for r in all_results),
            "total_allowlisted_hits": sum(len(r["allowlisted_hits"]) for r in all_results),
        },
    }
    print(json.dumps(out, indent=2))


# ────────────────────────────── main ──────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="sweep all registered stores")
    ap.add_argument("--store", help="sweep one named store")
    ap.add_argument("--output", choices=["human", "json"], default="human")
    ap.add_argument("--create-goals", action="store_true",
                    help="create cross-agent handoff goals for each drift cluster")
    ap.add_argument("--aspiration-id", default="asp-115",
                    help="aspiration to file handoff goals under (default: asp-115)")
    args = ap.parse_args(argv)

    if not (args.all or args.store):
        ap.error("specify --all or --store <name>")

    if not REGISTRY_PATH.is_file():
        print(f"FATAL: registry missing: {REGISTRY_PATH}", file=sys.stderr)
        return 3
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        registry = yaml.safe_load(f) or {}

    stores = registry.get("stores", {}) or {}
    allowlist = registry.get("allowlist", []) or []

    if args.store:
        if args.store not in stores:
            print(f"FATAL: unknown store: {args.store}. "
                  f"Known: {', '.join(sorted(stores))}", file=sys.stderr)
            return 3
        selected = {args.store: stores[args.store]}
    else:
        selected = stores

    all_results = [process_store(n, cfg, allowlist) for n, cfg in selected.items()]

    created_goals = 0
    if args.create_goals:
        created_goals = create_fix_goals(all_results, args.aspiration_id)

    if args.output == "json":
        emit_json(all_results, created_goals)
    else:
        emit_human(all_results, created_goals)

    any_registry = any(r["probe"]["status"] == "registry-drifted" for r in all_results)
    any_drift = any(r["drift_hits"] for r in all_results)
    if any_registry:
        return 2
    if any_drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
