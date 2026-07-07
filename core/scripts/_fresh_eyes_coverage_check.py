#!/usr/bin/env python3
""": post-state-update-gate.sh cooldown coverage check.

Extracted from post-state-update-gate.sh inline PYEOF for testability and
maintainability (the heredoc had grown to ~110 lines and was hard to unit
test).

Inputs (env vars):
  CURRENT                 — newline-separated repo-relative paths in current change set
  COOLDOWN_JSON           — own-agent fresh_eyes_last_fire WM slot (JSON list of records, or "null")
  COOLDOWN_HOURS          — float; cooldown window in hours (default 4)
  TEAM_STATE_PATH         — path to world/team-state.yaml (peer-coverage source)
  SELF_AGENT              — current agent name (excluded from peer scan)
  PROJECT_ROOT            — repo root for resolving file paths to compute current sigs
  SUPPRESSION_AUDIT_PATH  — path to peer-vs-self audit JSONL (fail-open)

Output (stdout, two-line protocol):
  Line 1: verdict in {"no", "yes:peer", "yes:self", "yes:union"}
  Line 2: JSON array of covered paths (for partial-overlap dedup)

Coverage semantics (g-115-573):
  For each candidate path P in CURRENT:
    sig_match    := record has P in files AND P in sigs AND sigs[P] == hash(current P)
    sig_conflict := record has P in files AND P in sigs AND sigs[P] != hash(current P)
    path_only    := record has P in files (regardless of sigs)
  Coverage(P):
    sig_match    → covered (signature confirms unchanged content).
    sig_conflict → NOT covered (amendment detected; ignore path-only fallback).
    path_only    → covered (legacy / no-sig record provides path-only coverage,
                   backward-compat with pre-573 records).
    else         → not covered.
  Suppress dispatch only when every current path is covered.

Fail-open: any exception → print "no" + "[]" so the gate continues.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta


def file_sig(rel_path, root):
    """sha1[:12] of current file content. None if unreadable."""
    full = os.path.join(root, rel_path) if root else rel_path
    try:
        with open(full, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except (OSError, IOError):
        return None


def parse_own_records(raw, hours, now):
    """Parse own-agent fresh_eyes_last_fire WM slot. Returns
    (records: list[{files_set, sigs, source}], own_files: set, in_cooldown: bool).
    Tolerates legacy single-dict schema and pre-573 records without content_signatures.
    """
    records = []
    own_files = set()
    in_cooldown = False
    raw = (raw or "").strip()
    if raw and raw != "null":
        try:
            data = json.loads(raw)
            recs = data if isinstance(data, list) else [data]
            for rec in recs:
                if not isinstance(rec, dict):
                    continue
                try:
                    last_time = datetime.fromisoformat(rec.get("time", ""))
                except (ValueError, TypeError):
                    continue
                if (now - last_time) <= timedelta(hours=hours):
                    files_set = set(rec.get("files", []))
                    sigs = rec.get("content_signatures")
                    sigs = sigs if isinstance(sigs, dict) else None
                    records.append({"files_set": files_set, "sigs": sigs, "source": "own"})
                    own_files.update(files_set)
                    in_cooldown = True
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return records, own_files, in_cooldown


def _composed_agent_status(ts_path):
    """agent_status composed from the core team-state file + per-agent row
    files (g-328-27 sharding — rows win newest-wins; rows dir is a sibling
    of ts_path). Fail-open to whatever the core file yields."""
    core = {}
    try:
        import yaml
        if ts_path and os.path.isfile(ts_path):
            with open(ts_path, "r", encoding="utf-8") as f:
                core = (yaml.safe_load(f) or {}).get("agent_status") or {}
    except Exception:
        core = {}
    if not isinstance(core, dict):
        core = {}
    try:
        from _team_state import compose_agent_status, load_rows
        return compose_agent_status(core, load_rows(os.path.dirname(str(ts_path))))
    except Exception:
        return core


def parse_self_ts_record(ts_path, self_agent, hours, now):
    """: parse self-agent's team-state.<self>.last_fresh_eyes_run as
    own-side coverage. /fresh-eyes-code Phase 5b writes here but NOT to
    per-agent WM fresh_eyes_last_fire. Without this, voluntary
    /fresh-eyes-code invocations (e.g. from precheck Phase 0-pre3
    consumption) don't shield subsequent gate dispatches within
    COOLDOWN_HOURS. Sibling of parse_peer_records — same body shape, inverted
    agent_name filter. Returns (records, files, in_cooldown) tagged source='own'
    so verdict logic merges with WM-source own_records cleanly.
    """
    records = []
    files = set()
    in_cooldown = False
    if not (ts_path and self_agent):
        return records, files, in_cooldown
    try:
        agent_data = _composed_agent_status(ts_path).get(self_agent)
        if not isinstance(agent_data, dict):
            return records, files, in_cooldown
        last_run = agent_data.get("last_fresh_eyes_run")
        if not isinstance(last_run, dict):
            return records, files, in_cooldown
        last_time_str = last_run.get("time", "")
        if not last_time_str:
            return records, files, in_cooldown
        try:
            last_time = datetime.fromisoformat(last_time_str)
        except (ValueError, TypeError):
            return records, files, in_cooldown
        if (now - last_time) > timedelta(hours=hours):
            return records, files, in_cooldown
        files_set = set(last_run.get("files", []))
        sigs = last_run.get("content_signatures")
        sigs = sigs if isinstance(sigs, dict) else None
        records.append({"files_set": files_set, "sigs": sigs, "source": "own"})
        files.update(files_set)
        in_cooldown = True
    except Exception:
        pass
    return records, files, in_cooldown


def parse_peer_records(ts_path, self_agent, hours, now):
    """Parse cross-agent team-state.agent_status.*.last_fresh_eyes_run.
    Returns (records: list[{files_set, sigs, source}], peer_files: set, in_cooldown: bool).
    Excludes self_agent (peer-coverage is non-self by definition; self_agent
    handled by parse_self_ts_record).
    """
    records = []
    peer_files = set()
    in_cooldown = False
    if not ts_path:
        return records, peer_files, in_cooldown
    try:
        for agent_name, agent_data in _composed_agent_status(ts_path).items():
            if agent_name == self_agent:
                continue
            if not isinstance(agent_data, dict):
                continue
            last_run = agent_data.get("last_fresh_eyes_run")
            if not isinstance(last_run, dict):
                continue
            last_time_str = last_run.get("time", "")
            if not last_time_str:
                continue
            try:
                last_time = datetime.fromisoformat(last_time_str)
            except (ValueError, TypeError):
                continue
            if (now - last_time) > timedelta(hours=hours):
                continue
            files_set = set(last_run.get("files", []))
            sigs = last_run.get("content_signatures")
            sigs = sigs if isinstance(sigs, dict) else None
            records.append({"files_set": files_set, "sigs": sigs, "source": f"peer:{agent_name}"})
            peer_files.update(files_set)
            in_cooldown = True
    except Exception:
        pass
    return records, peer_files, in_cooldown


def evaluate_coverage(current, current_sigs, all_records):
    """Apply  sig-aware coverage check. Returns
    (covered: set, covered_by_peer: set, covered_by_own: set, verdict: str).
    """
    covered = set()
    covered_by_peer = set()
    covered_by_own = set()
    for p in current:
        cur_sig = current_sigs.get(p)
        sig_match_recs = [
            r for r in all_records
            if p in r["files_set"] and r["sigs"] and p in r["sigs"]
            and r["sigs"][p] == cur_sig
        ]
        sig_conflict_recs = [
            r for r in all_records
            if p in r["files_set"] and r["sigs"] and p in r["sigs"]
            and r["sigs"][p] != cur_sig
        ]
        if sig_match_recs:
            covered.add(p)
            sources = {r["source"] for r in sig_match_recs}
        elif sig_conflict_recs:
            continue  # amend detected — skip path-only fallback
        else:
            path_only_recs = [r for r in all_records if p in r["files_set"]]
            if not path_only_recs:
                continue
            covered.add(p)
            sources = {r["source"] for r in path_only_recs}
        if all(s.startswith("peer:") for s in sources):
            covered_by_peer.add(p)
        elif all(s == "own" for s in sources):
            covered_by_own.add(p)

    current_set = set(current)
    verdict = "no"
    if current_set and current_set.issubset(covered):
        if covered_by_peer == current_set:
            verdict = "yes:peer"
        elif covered_by_own == current_set:
            verdict = "yes:self"
        else:
            verdict = "yes:union"
    return covered, covered_by_peer, covered_by_own, verdict


def write_audit(audit_path, now, verdict, self_agent, current_size,
                own_files_count, peer_files_count, own_in_cooldown, peer_in_cooldown):
    """ peer-vs-self suppression audit log (append-only JSONL).
    Fail-open: any error swallowed."""
    if not (verdict.startswith("yes") and audit_path):
        return
    try:
        entry = {
            "timestamp": now.isoformat(timespec="seconds"),
            "verdict": verdict,
            "agent": self_agent or "",
            "current_set_size": current_size,
            "own_files_count": own_files_count,
            "peer_files_count": peer_files_count,
            "own_in_cooldown": own_in_cooldown,
            "peer_in_cooldown": peer_in_cooldown,
        }
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        pass


def main():
    try:
        try:
            hours = float(os.environ.get("COOLDOWN_HOURS", "4"))
        except (ValueError, TypeError):
            hours = 4.0
        now = datetime.now()
        project_root = os.environ.get("PROJECT_ROOT", "")

        own_records, own_files, own_in_cooldown = parse_own_records(
            os.environ.get("COOLDOWN_JSON", ""), hours, now,
        )
        peer_records, peer_files, peer_in_cooldown = parse_peer_records(
            os.environ.get("TEAM_STATE_PATH", ""),
            os.environ.get("SELF_AGENT", ""),
            hours, now,
        )
        # : merge self-agent's team-state record into own-side coverage.
        # /fresh-eyes-code Phase 5b writes only to team-state (not to WM), so
        # without this read, voluntary /fresh-eyes-code calls don't shield
        # subsequent gate dispatches even when same agent + same files within
        # cooldown. Tag source='own' so verdict logic merges cleanly with WM.
        self_ts_records, self_ts_files, self_ts_in_cooldown = parse_self_ts_record(
            os.environ.get("TEAM_STATE_PATH", ""),
            os.environ.get("SELF_AGENT", ""),
            hours, now,
        )
        own_records.extend(self_ts_records)
        own_files.update(self_ts_files)
        own_in_cooldown = own_in_cooldown or self_ts_in_cooldown

        if not (own_in_cooldown or peer_in_cooldown):
            print("no")
            print("[]")
            return 0

        current = sorted(
            p.strip() for p in os.environ.get("CURRENT", "").splitlines() if p.strip()
        )
        current_sigs = {p: file_sig(p, project_root) for p in current}
        covered, covered_by_peer, covered_by_own, verdict = evaluate_coverage(
            current, current_sigs, own_records + peer_records,
        )

        print(verdict)
        print(json.dumps(sorted(covered)))

        write_audit(
            os.environ.get("SUPPRESSION_AUDIT_PATH", "").strip(),
            now, verdict,
            os.environ.get("SELF_AGENT", ""),
            len(current),
            len(own_files),
            len(peer_files),
            own_in_cooldown,
            peer_in_cooldown,
        )
        return 0
    except Exception:
        print("no")
        print("[]")
        return 0


if __name__ == "__main__":
    sys.exit(main())
