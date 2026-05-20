#!/usr/bin/env python3
"""Backfill `closes_knowledge_debt` on historical goals.

One-time utility that fills in the goal-schema field introduced alongside
the debt-closure classifier override (see goal-schemas.md "Knowledge-Debt
Closure Field"). Goals created before that change have no `closes_knowledge_debt`
field; this script pattern-matches them against currently-outstanding
knowledge_debt[] node_keys and back-populates the field so reporting,
reflection, and the Step 8 auto-detect fallback can see the link.

Dry-run by default. --apply actually invokes aspirations-update-goal.sh.

Match rule: a goal matches a debt entry if the debt's node_key appears as
a word-boundary substring in the goal's title, description, category, or
skill string (case-insensitive, with -/_/space treated as equivalent).

Scope caveat: this is approximate. It cannot reconstruct debts that were
already cleared (no reverse-pointer history exists). It only makes the
*currently-outstanding* debts visible on the goals that referenced them.
That is sufficient for the reporting surface and for the classifier's
semantic override on any re-execution of recurring goals.
"""

import argparse
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
from _paths import agent_dir as _agent_dir  # noqa: E402


def _posix(p):
    """Return a POSIX-style path string (forward slashes).

    On Windows, passing a backslash path to a bash subprocess strips the
    backslashes because bash treats them as escape characters. POSIX form
    works on both Windows (git-bash understands it) and real POSIX shells.
    """
    return str(p).replace("\\", "/")


def _run_py(script_name, py_args, env_overrides=None, check=True):
    """Run a core python script directly via the current Python interpreter.

    Bypasses the bash wrapper scripts. On Windows, a subprocess.run that picks
    up WSL bash (because it's first on PATH) cannot find `py`, so the .bash
    wrappers that call the `python3` shim fail. Calling python directly sides
    steps the whole bash binary lottery.
    """
    import os
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    script = CORE_ROOT / "scripts" / script_name
    cmd = [sys.executable, str(script)] + list(py_args)
    res = subprocess.run(
        cmd, env=env, cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8",
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\nstderr: {res.stderr}"
        )
    return res.stdout


def _read_handoff_debts(agent):
    """Read knowledge_debts_pending from <agent>/session/handoff.yaml."""
    path = _agent_dir(agent) / "session" / "handoff.yaml"
    if not path.is_file():
        return []
    try:
        import yaml
    except ImportError:
        print("PyYAML not available — skipping handoff scan", file=sys.stderr)
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"handoff read failed for {agent}: {e}", file=sys.stderr)
        return []
    entries = data.get("knowledge_debts_pending") or data.get("knowledge_debt") or []
    if not isinstance(entries, list):
        return []
    return entries


def _read_wm_debts(agent):
    """Read knowledge_debt slot from <agent>/session/working-memory.yaml.

    Uses _rt.wm_read (daemon client) — the wm.py read CLI was deleted in
    the 2026-05-14 cutover.
    """
    _prev_agent = os.environ.get("MIND_AGENT")
    os.environ["MIND_AGENT"] = agent
    try:
        raw = _rt.wm_read(slot="knowledge_debt", as_json=True)
    except _rt.RtError as e:
        print(f"wm read failed for {agent}: {e.body or e}", file=sys.stderr)
        return []
    finally:
        if _prev_agent is None:
            os.environ.pop("MIND_AGENT", None)
        else:
            os.environ["MIND_AGENT"] = _prev_agent
    try:
        return json.loads(raw) if (raw or "").strip() else []
    except json.JSONDecodeError:
        return []


def _normalize(s):
    """Lowercase + collapse - _ whitespace to single spaces for matching."""
    return re.sub(r"[-_\s]+", " ", str(s or "")).lower().strip()


def _match_goal_to_debts(goal, debt_keys_norm):
    """Return the subset of debt_keys that appear in goal's text fields."""
    haystack = " ".join(
        _normalize(goal.get(f, ""))
        for f in ("title", "description", "category", "skill")
    )
    matches = []
    for raw_key, norm_key in debt_keys_norm:
        if not norm_key:
            continue
        # Word-boundary match on the normalized form.
        pat = r"\b" + re.escape(norm_key) + r"\b"
        if re.search(pat, haystack):
            matches.append(raw_key)
    return matches


def _iter_goals(aspirations):
    """Yield (aspiration_id, goal) pairs for all goals in all aspirations."""
    for asp in aspirations:
        asp_id = asp.get("id", "?")
        for g in asp.get("goals", []):
            yield asp_id, g


def _load_aspirations_active(agent):
    """Uses _rt.aspirations_read (daemon client) — the aspirations.py read
    CLI was deleted in the 2026-05-14 cutover."""
    _prev_agent = os.environ.get("MIND_AGENT")
    os.environ["MIND_AGENT"] = agent
    try:
        raw = _rt.aspirations_read(source="world", active=True)
    except _rt.RtError:
        return []
    finally:
        if _prev_agent is None:
            os.environ.pop("MIND_AGENT", None)
        else:
            os.environ["MIND_AGENT"] = _prev_agent
    try:
        return json.loads(raw) if (raw or "").strip() else []
    except json.JSONDecodeError:
        return []


def _load_aspirations_archived(agent):
    """Uses _rt.rt_call (daemon client) — the aspirations.py read --archive
    CLI was deleted in the 2026-05-14 cutover."""
    _prev_agent = os.environ.get("MIND_AGENT")
    os.environ["MIND_AGENT"] = agent
    try:
        raw = _rt.rt_call("GET", "/v1/aspirations/read",
                          query="source=world&archive=1")
    except _rt.RtError:
        return []
    finally:
        if _prev_agent is None:
            os.environ.pop("MIND_AGENT", None)
        else:
            os.environ["MIND_AGENT"] = _prev_agent
    try:
        return json.loads(raw) if (raw or "").strip() else []
    except json.JSONDecodeError:
        return []


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Backfill closes_knowledge_debt on historical goals from current "
            "knowledge_debt[] entries (dry-run by default)."
        )
    )
    ap.add_argument("--agent", default="",
                    help="Agent to read debts from (defaults to $MIND_AGENT). "
                         "Can be repeated to union multiple agents.")
    ap.add_argument("--also-agent", action="append", default=[],
                    help="Additional agent(s) to include in the debt source.")
    ap.add_argument("--include-archived", action="store_true",
                    help="Also scan archived aspirations (default: active only).")
    ap.add_argument("--only-completed", action="store_true",
                    help="Only backfill goals whose status is 'completed'. "
                         "Default: all statuses (pending goals benefit too — "
                         "the field hints the classifier on their first run).")
    ap.add_argument("--require-tree-node", action="store_true",
                    help="Only backfill for node_keys that actually exist in "
                         "the knowledge tree (via tree-find-node.sh lookup). "
                         "Recommended — filters out placeholder/bogus debt "
                         "entries like 'multiple' or 'various' that don't "
                         "point at real nodes.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually invoke aspirations-update-goal. "
                         "Default is dry-run (prints proposed changes only).")
    ap.add_argument("--output", default="human", choices=["human", "json"],
                    help="Report format.")
    args = ap.parse_args(argv)

    import os
    agent_primary = args.agent or os.environ.get("MIND_AGENT", "").strip()
    if not agent_primary:
        print("ERROR: no agent specified (pass --agent or set MIND_AGENT)",
              file=sys.stderr)
        return 2

    agents = [agent_primary] + list(args.also_agent)

    # Gather debts from wm + handoff across all listed agents. De-dup by node_key.
    raw_debts = []
    for a in agents:
        raw_debts.extend(
            ("wm", a, d) for d in _read_wm_debts(a) if isinstance(d, dict)
        )
        raw_debts.extend(
            ("handoff", a, d) for d in _read_handoff_debts(a) if isinstance(d, dict)
        )

    # Unique node_keys (preserve first-seen metadata).
    seen = {}
    for source, a, d in raw_debts:
        nk = d.get("node_key")
        if not nk or nk in seen:
            continue
        seen[nk] = {"source": source, "agent": a, **d}
    debt_list = list(seen.values())

    if not debt_list:
        report = {
            "agents": agents,
            "debts_found": 0,
            "matches": [],
            "applied": False,
        }
        return _emit(report, args.output)

    # Optional: drop debt entries whose node_key doesn't exist in the tree.
    # Debts pointing at real nodes are where the classifier override produces
    # a useful signal; placeholder labels like "multiple" generate noise.
    filtered_keys = []
    if args.require_tree_node:
        for d in debt_list:
            nk = d["node_key"]
            try:
                out = _run_py(
                    "tree.py", ["read", "--node", nk],
                    env_overrides={"MIND_AGENT": agent_primary},
                    check=False,
                )
                stripped = out.strip()
                exists = bool(stripped) and stripped not in ("null", "{}", "[]")
            except Exception:
                exists = False
            if exists:
                filtered_keys.append(d["node_key"])
            else:
                print(
                    f"[backfill] skipping '{nk}' — not a real tree node "
                    f"(probably a placeholder debt label)",
                    file=sys.stderr,
                )
        if not filtered_keys:
            report = {
                "agents": agents,
                "debts_found": len(debt_list),
                "debt_node_keys": [d["node_key"] for d in debt_list],
                "proposed_count": 0,
                "applied": False,
                "applied_count": 0,
                "apply_errors": [],
                "matches": [],
                "note": ("No debt entries point at real tree nodes. The debts "
                         "in working memory use placeholder labels; backfill "
                         "would produce false matches. Fix the handoff/wm "
                         "entries to reference actual node_keys."),
            }
            return _emit(report, args.output)
        debt_list = [d for d in debt_list if d["node_key"] in filtered_keys]

    debt_keys_norm = [(d["node_key"], _normalize(d["node_key"])) for d in debt_list]

    # Load aspiration corpus (world + primary agent queue are both searched
    # by aspirations.py — --source defaults to "project" which reads world).
    aspirations = _load_aspirations_active(agent_primary)
    if args.include_archived:
        aspirations = list(aspirations) + _load_aspirations_archived(agent_primary)

    proposed = []
    for asp_id, goal in _iter_goals(aspirations):
        if args.only_completed and goal.get("status") != "completed":
            continue
        existing = goal.get("closes_knowledge_debt") or []
        if not isinstance(existing, list):
            existing = []
        matches = _match_goal_to_debts(goal, debt_keys_norm)
        new_keys = [k for k in matches if k not in existing]
        if not new_keys:
            continue
        updated = list(existing) + new_keys
        proposed.append({
            "aspiration": asp_id,
            "goal_id": goal.get("id"),
            "goal_title": goal.get("title", ""),
            "goal_status": goal.get("status"),
            "existing_field": existing,
            "new_matches": new_keys,
            "would_be": updated,
        })

    applied_count = 0
    apply_errors = []
    if args.apply:
        for entry in proposed:
            try:
                payload = json.dumps(entry["would_be"])
                _run_py(
                    "aspirations.py",
                    ["update-goal", entry["goal_id"], "closes_knowledge_debt", payload],
                    env_overrides={"MIND_AGENT": agent_primary},
                    check=True,
                )
                applied_count += 1
            except Exception as e:
                apply_errors.append({
                    "goal_id": entry["goal_id"],
                    "error": str(e),
                })

    report = {
        "agents": agents,
        "debts_found": len(debt_list),
        "debt_node_keys": [d["node_key"] for d in debt_list],
        "goals_scanned_from": "active + archived" if args.include_archived else "active",
        "only_completed": args.only_completed,
        "proposed_count": len(proposed),
        "applied": args.apply,
        "applied_count": applied_count,
        "apply_errors": apply_errors,
        "matches": proposed,
    }
    return _emit(report, args.output)


def _emit(report, output_format):
    if output_format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(f"Agents: {', '.join(report['agents'])}")
    print(f"Debts found: {report['debts_found']}")
    if report["debts_found"]:
        print(f"Debt node_keys: {report['debt_node_keys']}")
    print(f"Goals scanned: {report.get('goals_scanned_from', 'active')}")
    print(f"Only completed: {report.get('only_completed', False)}")
    print(f"Proposed updates: {report['proposed_count']}")
    if report["applied"]:
        print(f"Applied: {report['applied_count']}")
        if report["apply_errors"]:
            print(f"Errors: {len(report['apply_errors'])}")
            for err in report["apply_errors"]:
                print(f"  - {err['goal_id']}: {err['error']}")
    else:
        print("Mode: DRY-RUN (no changes written). Pass --apply to commit.")

    if report["matches"]:
        print()
        print("Proposed matches:")
        for m in report["matches"]:
            print(f"  {m['goal_id']} [{m['goal_status']}] ({m['aspiration']})")
            print(f"    title: {m['goal_title'][:110]}")
            print(f"    existing closes_knowledge_debt: {m['existing_field']}")
            print(f"    new matches: {m['new_matches']}")
            print(f"    would become: {m['would_be']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
