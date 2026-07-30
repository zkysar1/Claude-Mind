#!/usr/bin/env python3
"""Audit pass -- lane P of the reclaim duty: re-derive every goal carrying 'user'.

Two lanes over every non-terminal goal whose participants INCLUDE "user", in
world/aspirations.jsonl and in each agent's <agent>/aspirations.jsonl:

  PROMOTE  participants == ["user"]      "should the AGENT be involved?"
           Runs capability-gate.py on title+description. A match means the
           goal should have been [agent, user] at creation. With --apply,
           promotes via aspirations.py update-goal and logs to
           world/audit-user-to-agent.jsonl. Adding the agent is safe, so
           this lane may mutate.

  DROP     "user" alongside others       "is the USER still needed?"
           Joins the goal's declared `user_leg_scope` against the scopes
           granted in world/conventions/capability-routing.md
           "## Standing User Grants". A covered scope means the user's leg
           is already standing-approved and "user" can come off. REPORTS
           ONLY -- removing the human is a one-way door inside the loop, and
           the field it depends on is populated on a minority of goals.

Why the second lane exists (measured 2026-07-29 on the live world queue):
the promote lane's exact `participants == ["user"]` predicate had a live
candidate set of ZERO -- one goal in the fleet matched it, and that goal was
a deliberate park the audit correctly refuses to touch. The other 28
user-carrying goals were all ["agent", "user"] and structurally invisible.
Correct routing caused the blindness: capability-before-user.md tells the
fleet to file [agent, user] whenever both legs are real, so the creation-time
gate's success produced exactly the population the audit-time tool could not
see. The creation-time advisory (gates/user_leg_scope.py) has always tested
`"user" in participants`; only this half was narrower.

`user_leg_scope` is the join key, and that was always its purpose -- the
creation-time advisory says so verbatim: "Standing-grant matching will fall
back to prose recognition." This is that matching, done as an exact join
between two vocabularies that already existed and had never been connected.

Dry-run by default -- prints the plan without mutating.

Part (1) of g-243-02 (plan: curious-sparking-simon.md). Pairs with the
--evidence flag in capability-gate.py (same goal, Part 2, already shipped).
Drop lane serves .claude/rules/reclaim-routed-work.md lane P.

Usage:
  py -3 core/scripts/audit-user-to-agent.py            # dry-run, both lanes
  py -3 core/scripts/audit-user-to-agent.py --apply    # apply PROMOTE only
  py -3 core/scripts/audit-user-to-agent.py --output json
  py -3 core/scripts/audit-user-to-agent.py --limit 5  # first 5 promotables
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
GATE_PY = SCRIPT_DIR / "capability-gate.py"
ASP_PY = SCRIPT_DIR / "aspirations.py"

sys.path.insert(0, str(SCRIPT_DIR))
from _fileops import locked_append_jsonl  # noqa: E402
from _paths import agent_dir as _agent_dir, enumerate_agent_confs  # noqa: E402
# SSOT for the scope vocabulary — never copy the set. The same module backs the
# creation-time advisory in aspirations.py (cmd_add_goal / cmd_add /
# cmd_update_goal), so audit time and creation time cannot drift apart.
from gates.user_leg_scope import VALID_USER_LEG_SCOPES  # noqa: E402


def _world_dir_for(agent: str) -> Path:
    """Resolve WORLD_PATH from <agent>/local-paths.conf.

    Routes the value through `_path_helpers.absolutize` so a drive-letter
    path is absolutized correctly on POSIX-flavored Python (g-115-733).
    Plain Path(value) returns a relative PosixPath under that flavor.
    """
    conf = _agent_dir(agent) / "local-paths.conf"
    if not conf.is_file():
        return None
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "WORLD_PATH":
            from _path_helpers import absolutize
            return absolutize(v.strip().strip('"').strip("'"), PROJECT_ROOT)
    return None


def _discover_agents() -> list:
    """Every directory that owns a local-paths.conf is an agent."""
    out = []
    for conf in enumerate_agent_confs():
        out.append(conf.parent.name)
    return out


def _load_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


def _find_user_participant_goals(source_label: str, asp_path: Path) -> list:
    """Return every non-terminal goal whose participants INCLUDE 'user'.

    Shape-tagged, nothing silently dropped. Each candidate:
    {source, aspiration_id, goal, file_path, shape, deliberate}.

    `shape` is "user-only" (participants == ['user']) or "agent-user"
    ('user' alongside others). The distinction decides which lane runs:
    user-only asks "should the agent be involved?" (promote); agent-user
    asks "is the user still needed?" (drop) — a different question with a
    much higher evidence bar, because dropping the human is a one-way door
    inside the loop while adding the agent is not.

    WHY "include" and not "== ['user']" (measured 2026-07-29, live world
    queue): the exact-match predicate this replaces had a live candidate set
    of ZERO. One goal in the entire fleet matched it -- g-314-01, a
    deliberate park -- which the `deliberate` guard below then excluded, so
    the auditor was structurally incapable of returning a candidate. The
    other 28 user-carrying goals were all ['agent', 'user'] and invisible to
    it. The creation-time advisory (gates/user_leg_scope.py) has always
    tested `"user" in participants`; only the audit-time half was narrower.
    The gate's own success caused the blindness: capability-before-user.md
    tells the fleet to file [agent, user] whenever both legs are real, so
    correct routing produces exactly the population the auditor could not
    see.
    """
    out = []
    for asp in _load_jsonl(asp_path):
        if asp.get("status") in ("archived", "retired", "completed"):
            continue
        for g in (asp.get("goals") or []):
            parts = g.get("participants")
            if not isinstance(parts, list):
                continue
            norm = [str(p).strip().lower() for p in parts]
            if "user" not in norm:
                continue
            if g.get("status") not in ("pending", "blocked"):
                continue
            # Deliberate user routing is REPORTED, not skipped. The audit
            # targets accidental agent-side drift (user wrongly attached AT
            # CREATION), not goals the user explicitly directed:
            # origin_signal == "user_directive" marks a deliberate choice
            # (e.g. the participants:[user] park signal --  :
            # "DO-NOT-TOUCH: park is participants:[user] ONLY ... Reversal =
            # the user edits participants"). Acting on it would violate the
            # directive. ( audit run, 2026-06-09: caught the
            # felt-sense-checkin "lane" keyword false-positive on .)
            #
            # It is tagged rather than `continue`d because a silent skip is
            # indistinguishable from a clean sweep -- the exact failure this
            # whole audit lane exists to correct. A tagged record shows up in
            # the count and states why it was left alone.
            deliberate = (g.get("origin_signal") or "").strip().lower().startswith(
                ("user_directive", "user-directed"))
            out.append({
                "source": source_label,
                "aspiration_id": asp.get("id"),
                "goal": g,
                "file_path": str(asp_path),
                "shape": "user-only" if norm == ["user"] else "agent-user",
                "deliberate": deliberate,
            })
    return out


_GRANT_ROW = re.compile(r"^\|\s*(grant-[0-9]+)\s*\|(.+)$")


def _scope_head(cell: str) -> str:
    """The DECLARATIVE head of a grant's scope cell.

    Grant scope cells open with the granted scope and then qualify it at
    length -- conditions, carve-outs, history, and clauses that name actions
    the grant explicitly does NOT cover. Matching enum tokens against the
    whole cell therefore inverts meaning on real rows: grant-008's body says
    "PROVIDED the commit is verified" (a precondition, not a grant of
    `commit`), and grant-009's says "anything needing NEW credentials ...
    still routes to user" (an explicit REFUSAL). A whole-cell scan reads
    both as grants.

    Cutting at the first period keeps only the declarative lead. The cut is
    deliberately crude and under-matches -- an abbreviation ("incl.") ends
    the head early. That bias is the safe one: a missed match leaves a goal
    routed to the user exactly as it is today, while a false match would
    recommend removing the human. This is the same false-positive class the
    capability gate's tuning history warns about, met with the one predicate
    that cannot silently over-fire.
    """
    return cell.split(".", 1)[0]


def _parse_standing_grants(world_dir: Path) -> dict:
    """Parse `## Standing User Grants` into {scope_token: [grant_id, ...]}.

    Returns {"by_scope": {...}, "unkeyed": [(grant_id, head)], "error": str|None}.

    `unkeyed` is a first-class result, not a leftover: a grant whose head
    uses none of the VALID_USER_LEG_SCOPES vocabulary can never be matched
    by a goal's `user_leg_scope`, so the permission it carries is invisible
    to this audit no matter how many goals it ought to free. Surfacing it
    tells the reader which row to reword -- the machine-findable-terms
    requirement of `.claude/rules/reclaim-routed-work.md` rule 4, applied to
    the grants table itself.
    """
    out = {"by_scope": {}, "unkeyed": [], "error": None}
    if world_dir is None:
        out["error"] = "no WORLD_PATH"
        return out
    path = world_dir / "conventions" / "capability-routing.md"
    if not path.is_file():
        out["error"] = f"not found: {path}"
        return out
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        out["error"] = f"read failed: {e}"
        return out

    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped.lower().startswith("## standing user grants")
            continue
        if not in_section:
            continue
        m = _GRANT_ROW.match(stripped)
        if not m:
            continue
        grant_id, rest = m.group(1), m.group(2)
        cells = [c.strip() for c in rest.split("|")]
        if not cells:
            continue
        head = _scope_head(cells[0])
        hits = [s for s in sorted(VALID_USER_LEG_SCOPES)
                if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", head, re.I)]
        if hits:
            for s in hits:
                out["by_scope"].setdefault(s, []).append(grant_id)
        else:
            out["unkeyed"].append((grant_id, head[:90]))
    return out


def _run_gate(failure_reason: str, agent_env: str) -> dict:
    """Invoke capability-gate.py with JSON output. Returns parsed dict or {}.

    Uses --intended-participants user so the gate evaluates as if the goal
    were being routed to [user] right now — matches the original decision
    point we're auditing after the fact.
    """
    env = dict(os.environ)
    env["MIND_AGENT"] = agent_env
    try:
        r = subprocess.run(
            [sys.executable, str(GATE_PY),
             "--failure-reason", failure_reason,
             "--intended-participants", "user",
             "--output", "json"],
            capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return {"error": f"gate invocation failed: {e}"}
    if not r.stdout.strip():
        return {"error": "gate produced no output",
                "stderr": (r.stderr or "")[:200]}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"gate json parse failed: {e}"}


def _update_goal_participants(source: str, goal_id: str,
                              new_participants: list) -> tuple:
    """Call aspirations.py update-goal to set participants. Returns (ok, msg)."""
    env = dict(os.environ)
    # update-goal resolves the queue by --source argument
    try:
        r = subprocess.run(
            [sys.executable, str(ASP_PY),
             "--source", source,
             "update-goal", goal_id, "participants",
             json.dumps(new_participants)],
            capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return False, f"update-goal invocation failed: {e}"
    if r.returncode != 0:
        return False, f"rc={r.returncode} stderr={(r.stderr or '').strip()[:200]}"
    return True, (r.stdout or "").strip() or "ok"


def _log_reclassification(world_dir: Path, record: dict) -> None:
    """Append to world/audit-user-to-agent.jsonl. Fail-silent on write errors."""
    if world_dir is None:
        print("[audit-user-to-agent] WARN: no WORLD_PATH -> cannot log",
              file=sys.stderr)
        return
    path = world_dir / "audit-user-to-agent.jsonl"
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[audit-user-to-agent] WARN: log append failed: {e}",
              file=sys.stderr)


def _assess_user_leg(cand: dict, grants: dict) -> dict:
    """Lane P drop-check for an ['agent', 'user'] goal.

    The user-only lane asks "should the AGENT be involved?" and answers it
    with the capability gate. This lane asks the opposite and harder
    question -- "is the USER still needed?" -- which the gate cannot answer:
    the gate scores agent capability, and the agent already being capable
    says nothing about whether the human leg is discharged.

    The answer is decidable exactly when the user's leg was DECLARED. That
    is what `user_leg_scope` is for, and the creation-time advisory already
    says so in as many words: "Standing-grant matching will fall back to
    prose recognition." Matching a declared scope token against the granted
    scopes is that matching, done for real -- an exact join between two
    vocabularies that already existed and had never been connected.

    Verdicts:
      grant-covered -> a standing grant covers this scope; the user's leg is
                       already approved, so `user` can come off. RECOMMEND.
      keep          -> scope declared and NOT granted. Correct as routed.
      undeclared    -> no `user_leg_scope`; the leg was never written down,
                       so nothing can re-derive it mechanically. The fix is
                       to backfill the field, not to guess.
      deliberate    -> the user directed this routing. Never touch.

    Returns a verdict dict; NEVER mutates. Dropping the human is a one-way
    door inside the loop, and the field it depends on is populated on a
    small minority of goals -- so this half of the audit reports and the
    re-derivation stays with the reader.
    """
    g = cand["goal"]
    scope = (g.get("user_leg_scope") or "").strip()
    base = {
        "goal_id": g.get("id"),
        "aspiration_id": cand["aspiration_id"],
        "source": cand["source"],
        "title": g.get("title", ""),
        "status": g.get("status"),
        "user_leg_scope": scope or None,
        "participants": g.get("participants"),
    }
    if cand.get("deliberate"):
        return {**base, "verdict": "deliberate",
                "reason": f"origin_signal={g.get('origin_signal')!r} -- user directed "
                          "this routing; the reversal is the user editing participants"}
    if not scope:
        return {**base, "verdict": "undeclared",
                "reason": "no user_leg_scope -- the user's leg was never declared, so "
                          "no standing grant can match it and no sweep can re-derive "
                          "it. Backfill with: aspirations-update-goal.sh "
                          f"{g.get('id')} user_leg_scope <scope>",
                "valid_scopes": sorted(VALID_USER_LEG_SCOPES)}
    if scope not in VALID_USER_LEG_SCOPES:
        return {**base, "verdict": "undeclared",
                "reason": f"user_leg_scope={scope!r} is outside the canonical set, so it "
                          "cannot join the grants table",
                "valid_scopes": sorted(VALID_USER_LEG_SCOPES)}
    covering = grants.get("by_scope", {}).get(scope, [])
    if covering:
        return {**base, "verdict": "grant-covered", "grants": covering,
                "reason": f"user_leg_scope={scope!r} is covered by "
                          f"{', '.join(covering)} -- the user has standing-approved this "
                          "scope, so the user leg is already discharged. Drop 'user' from "
                          "participants; if the agent leg is also done, close the goal."}
    return {**base, "verdict": "keep",
            "reason": f"user_leg_scope={scope!r} matches no standing grant -- the user "
                      "leg is real and this routing is correct"}


def _summarize(cands: list) -> str:
    """Short goal summary: title truncated + aspiration + source."""
    lines = []
    for c in cands:
        g = c["goal"]
        title = (g.get("title") or "")[:70]
        lines.append(f"  [{c['source']}/{c['aspiration_id']}] {g['id']}: {title}")
    return "\n".join(lines) if lines else "  (none)"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reclaim lane P: promote [user] goals to [agent, user], and "
                    "re-derive whether 'user' is still needed on [agent, user] goals.")
    ap.add_argument("--apply", action="store_true",
                    help="Mutate goals in the PROMOTE lane. The drop lane never "
                         "mutates. Default is dry-run.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the first N PROMOTE candidates (for testing). "
                         "Does not bound the drop lane.")
    ap.add_argument("--agent-for-gate", default=None,
                    help="MIND_AGENT env for gate invocations. "
                         "Defaults to current session's agent or the first "
                         "discovered agent dir.")
    ap.add_argument("--output", choices=("text", "json"), default="text",
                    help="text (default) or json for programmatic consumers.")
    args = ap.parse_args(argv)

    _discovered = _discover_agents()
    agent_for_gate = (args.agent_for_gate
                      or os.environ.get("MIND_AGENT", "").strip()
                      or (_discovered[0] if _discovered else ""))
    if not agent_for_gate:
        print("[audit-user-to-agent] No agent found — set MIND_AGENT or create "
              "an agent dir with local-paths.conf.", file=sys.stderr)
        return 2

    # Resolve world dir from the gate-agent's local-paths.conf.
    # ASSUMPTION: all agents in this repo share the same WORLD_PATH. The audit
    # ledger lives in that shared world dir. If agents ever use separate worlds,
    # this script must grow per-agent world resolution — today it does not.
    world_dir = _world_dir_for(agent_for_gate)

    # Build the candidate list from world/aspirations.jsonl + every agent's file.
    all_user_goals = []
    if world_dir is not None:
        all_user_goals += _find_user_participant_goals(
            "world", world_dir / "aspirations.jsonl")
    for a in _discover_agents():
        all_user_goals += _find_user_participant_goals(
            a, _agent_dir(a) / "aspirations.jsonl")

    # Lane split. user-only runs the promote path below (unchanged); agent-user
    # runs the report-only drop check. Deliberate user routings are excluded
    # from promotion but still assessed and counted, so they appear in the
    # report as "left alone, and here is why" rather than vanishing.
    grants = _parse_standing_grants(world_dir)
    leg_verdicts = [_assess_user_leg(c, grants)
                    for c in all_user_goals if c["shape"] == "agent-user"]
    candidates = [c for c in all_user_goals
                  if c["shape"] == "user-only" and not c["deliberate"]]
    deliberate_user_only = [c for c in all_user_goals
                            if c["shape"] == "user-only" and c["deliberate"]]

    if args.limit and args.limit > 0:
        candidates = candidates[:args.limit]

    # Header goes to STDERR under --output json so stdout stays a single parseable
    # document. A human-readable banner ahead of the JSON makes `| json.load`
    # fail on char 1, which is exactly how a machine-readable mode ends up
    # documented but unusable.
    print(f"[audit-user-to-agent] {len(all_user_goals)} non-terminal goal(s) carry "
          f"'user': {len(candidates)} promotable user-only, "
          f"{len(deliberate_user_only)} deliberate user-only, "
          f"{len(leg_verdicts)} [agent, user]",
          file=(sys.stderr if args.output == "json" else sys.stdout))

    # NO early return on an empty promote lane. That return used to end the run
    # here, and on the live queue `candidates` is legitimately 0 -- so an early
    # exit would print a clean line and silently skip the entire drop lane,
    # reproducing the invisibility this change exists to fix.

    reclassified = []
    unchanged = []
    errors = []

    for c in candidates:
        g = c["goal"]
        failure_reason = (g.get("title") or "").strip()
        desc = (g.get("description") or "").strip()
        if desc:
            failure_reason += "\n" + desc[:500]
        if not failure_reason:
            errors.append({"goal_id": g["id"], "reason": "no title/description"})
            continue

        gate = _run_gate(failure_reason, agent_for_gate)
        if gate.get("error"):
            errors.append({"goal_id": g["id"], "reason": gate["error"]})
            continue

        matches = gate.get("matches") or []
        narrative_patterns = gate.get("narrative_patterns") or []

        # : include narrative-pattern matches as a reclassification
        # signal. Today the gate's would_block only flips on capability-keyword
        # matches; narrative-only matches are pure telemetry at CREATE_BLOCKER
        # time (insufficient signal alone for a hard refusal). At AUDIT time,
        # however, a [user]-only goal whose title/description carries narrative
        # phrasings like "user approves X" or "pending user sign-off" is a
        # routing-drift candidate even without a capability-keyword hit — the
        # narrative phrasing IS the drift signature. Flag for reclassification
        # so a follow-up can verify whether the user-routing was intentional.
        if not matches and not narrative_patterns:
            unchanged.append(c)
            continue

        # Match found (capability OR narrative) — propose promotion to [agent, user].
        if matches:
            top = matches[0]
            matched = top.get("skill") or (top.get("row") or "")[:80] or "(unknown)"
            matched_kw = top.get("matched_keyword") or ""
        else:
            matched = f"narrative-only: {narrative_patterns[0]}"
            matched_kw = narrative_patterns[0]

        action = {
            "goal_id": g["id"],
            "aspiration_id": c["aspiration_id"],
            "source": c["source"],
            "title": g.get("title", ""),
            "old_participants": ["user"],
            "new_participants": ["agent", "user"],
            "matched_capability": matched,
            "matched_keyword": matched_kw,
            "match_count": len(matches),
            "narrative_patterns": narrative_patterns,
        }

        if args.apply:
            # Determine source for aspirations.py: 'world' or 'agent'. For agent
            # sources, MIND_AGENT must point at the owning agent so the script
            # reads the right <agent>/aspirations.jsonl.
            if c["source"] == "world":
                update_source = "world"
                env_agent = agent_for_gate
            else:
                update_source = "agent"
                env_agent = c["source"]
            os.environ["MIND_AGENT"] = env_agent
            ok, msg = _update_goal_participants(
                update_source, g["id"], ["agent", "user"])
            os.environ["MIND_AGENT"] = agent_for_gate  # restore
            action["applied"] = bool(ok)
            if not ok:
                action["apply_error"] = msg[:200]
                errors.append({"goal_id": g["id"], "reason": f"update failed: {msg[:200]}"})
                continue
            _log_reclassification(world_dir, {
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                **action,
            })

        reclassified.append(action)

    by_verdict = {}
    for v in leg_verdicts:
        by_verdict.setdefault(v["verdict"], []).append(v)

    if args.output == "json":
        json.dump({
            "promote_lane": {
                "candidates": len(candidates),
                "reclassified": reclassified,
                "unchanged": [c["goal"]["id"] for c in unchanged],
                "deliberate_skipped": [c["goal"]["id"] for c in deliberate_user_only],
                "errors": errors,
            },
            "drop_lane": {
                "assessed": len(leg_verdicts),
                "counts": {k: len(v) for k, v in sorted(by_verdict.items())},
                "verdicts": leg_verdicts,
            },
            "grants": {
                "by_scope": grants.get("by_scope", {}),
                "unkeyed": [{"grant": gid, "head": head}
                            for gid, head in grants.get("unkeyed", [])],
                "error": grants.get("error"),
            },
            "applied": bool(args.apply),
        }, sys.stdout, indent=1)
        print("")
        return 0

    # --- Report: lane 1, promote [user] -> [agent, user] ---
    print("")
    print(f"Would reclassify: {len(reclassified)}")
    for a in reclassified:
        flag = " [APPLIED]" if a.get("applied") else " [DRY-RUN]"
        print(f"  {a['goal_id']}: '{a['title'][:50]}'{flag}")
        print(f"    [{a['source']}/{a['aspiration_id']}] match: "
              f"{a['matched_capability']} (kw: {a['matched_keyword']})")
    print(f"\nUnchanged (no match): {len(unchanged)}")
    if unchanged:
        print(_summarize(unchanged))
    if deliberate_user_only:
        print(f"\nDeliberate user routing, left alone: {len(deliberate_user_only)}")
        print(_summarize(deliberate_user_only))
    if errors:
        print(f"\nErrors: {len(errors)}")
        for e in errors:
            print(f"  {e['goal_id']}: {e['reason']}")

    # --- Report: lane 2, is the user still needed on [agent, user]? ---
    print("")
    print("=" * 68)
    print(f"[agent, user] user-leg re-derivation: {len(leg_verdicts)} goal(s)")
    if grants.get("error"):
        print(f"  standing grants UNREADABLE ({grants['error']}) -- every scope "
              "below is reported as ungranted, which is the fail-safe direction "
              "but is NOT evidence the user leg is real")
    else:
        print(f"  granted scopes: "
              f"{ {k: v for k, v in sorted(grants['by_scope'].items())} or '(none)'}")
    print("=" * 68)

    for verdict, label in (
        ("grant-covered", "DROP 'user' -- a standing grant already covers this leg"),
        ("undeclared", "UNDECLARED user leg -- cannot be re-derived until backfilled"),
        ("keep", "keep -- user leg is real and ungranted"),
        ("deliberate", "deliberate -- user directed this routing"),
    ):
        rows = by_verdict.get(verdict, [])
        if not rows:
            continue
        print(f"\n{label}: {len(rows)}")
        for v in rows:
            print(f"  {v['goal_id']:<16} {v['status']:<9} "
                  f"scope={v['user_leg_scope'] or '-'}  {v['title'][:58]}")
            if verdict in ("grant-covered", "undeclared"):
                print(f"      -> {v['reason']}")

    if grants.get("unkeyed"):
        print(f"\nGrants no goal can key to: {len(grants['unkeyed'])}")
        print("  These rows grant real permission, but their scope head uses none of")
        print("  the user_leg_scope vocabulary, so no goal can ever match them here.")
        print(f"  Vocabulary: {sorted(VALID_USER_LEG_SCOPES)}")
        for gid, head in grants["unkeyed"]:
            print(f"    {gid}: {head}")

    print("\nThe drop lane NEVER mutates: --apply governs the promote lane only.")
    print("Removing the human is a one-way door inside the loop, and user_leg_scope")
    print("is populated on a minority of goals -- so this half reports and you decide.")
    if not args.apply:
        print("\n(dry-run -- pass --apply to mutate goals in the promote lane)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
