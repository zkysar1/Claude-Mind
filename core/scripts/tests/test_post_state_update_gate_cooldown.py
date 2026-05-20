"""test_post_state_update_gate_cooldown.py — regression test for .

Exercises the cooldown decision tree in core/scripts/post-state-update-gate.sh
(the Python heredoc that decides whether to suppress dispatch). The cooldown
block was extended in g-115-291 to read cross-agent
`world/team-state.yaml agent_status.*.last_fresh_eyes_run` in addition to
own-agent `fresh_eyes_last_fire` (WM). This test mirrors that logic in
isolation so we can drive all 5 verdict paths without standing up a full
git-state + WM + team-state environment.

Cases covered (current_set vs own/peer cooldown sources within window):
  1. own only, current ⊆ own              → yes:self
  2. peer only, current ⊆ peer             → yes:peer
  3. both, current ⊆ union but not subset of either alone → yes:union
  4. neither in cooldown (both expired)    → no
  5. own + peer in cooldown but current ⊄ union → no
  6. own expired, peer fresh, current ⊆ peer → yes:peer  (expiry filtering)
  7. peer record from SELF agent (alpha) is ignored → only own counts (yes:self)

The function under test is a faithful in-Python copy of the heredoc body in
post-state-update-gate.sh lines ~169-228. Keep the two in sync — if the
heredoc changes shape, this test must be updated. Source-of-truth is the
shell script; this is the regression net.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta


def evaluate_cooldown(current_set: str,
                      own_json: str | None,
                      peer_team_state: dict | None,
                      hours: float = 4.0,
                      self_agent: str = "alpha",
                      now: datetime | None = None) -> str:
    """Mirror of the Python heredoc in post-state-update-gate.sh cooldown block.

    Returns one of: "yes:self", "yes:peer", "yes:union", "no".
    """
    if now is None:
        now = datetime.now()

    own_files: set[str] = set()
    own_in_cooldown = False
    if own_json and own_json != "null":
        try:
            data = json.loads(own_json)
            # : list-of-records schema. Legacy dict treated as [dict].
            records = data if isinstance(data, list) else [data]
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                try:
                    last_time = datetime.fromisoformat(rec.get("time", ""))
                except (ValueError, TypeError):
                    continue
                if (now - last_time) <= timedelta(hours=hours):
                    own_files.update(rec.get("files", []))
                    own_in_cooldown = True
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    peer_files: set[str] = set()
    peer_in_cooldown = False
    if peer_team_state:
        for agent_name, agent_data in (peer_team_state.get("agent_status") or {}).items():
            if agent_name == self_agent:
                continue  # peer-coverage requires non-self
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
            peer_files.update(last_run.get("files", []))
            peer_in_cooldown = True

    if not (own_in_cooldown or peer_in_cooldown):
        return "no"

    union_files = own_files | peer_files
    current = set(l.strip() for l in current_set.splitlines() if l.strip())
    if current and current.issubset(union_files):
        if peer_files and current.issubset(peer_files) and not current.issubset(own_files):
            return "yes:peer"
        elif current.issubset(own_files):
            return "yes:self"
        else:
            return "yes:union"
    return "no"


def main() -> int:
    failures: list[str] = []
    now = datetime(2026, 4, 27, 12, 0, 0)
    fresh_iso = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")     # 1h ago — within 4h cooldown
    expired_iso = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")    # 5h ago — outside 4h cooldown

    def run_case(name: str, expected: str, **kwargs) -> None:
        kwargs.setdefault("now", now)
        actual = evaluate_cooldown(**kwargs)
        if actual != expected:
            failures.append(f"{name}: expected={expected!r} actual={actual!r}")

    # Case 1: own only, current ⊆ own → yes:self
    run_case(
        "CASE 1 own-only-subset",
        expected="yes:self",
        current_set="core/scripts/foo.sh\ncore/scripts/bar.sh",
        own_json=json.dumps({"time": fresh_iso, "files": [
            "core/scripts/foo.sh", "core/scripts/bar.sh", "core/scripts/baz.sh"
        ]}),
        peer_team_state=None,
    )

    # Case 2: peer only, current ⊆ peer → yes:peer
    run_case(
        "CASE 2 peer-only-subset",
        expected="yes:peer",
        current_set="core/scripts/foo.sh\ncore/scripts/bar.sh",
        own_json="null",
        peer_team_state={
            "agent_status": {
                "bravo": {
                    "last_fresh_eyes_run": {
                        "time": fresh_iso,
                        "files": ["core/scripts/foo.sh", "core/scripts/bar.sh"],
                    }
                }
            }
        },
    )

    # Case 3: both, current ⊆ union but not subset of either alone → yes:union
    run_case(
        "CASE 3 union-subset",
        expected="yes:union",
        current_set="core/scripts/foo.sh\ncore/scripts/baz.sh",
        own_json=json.dumps({"time": fresh_iso, "files": ["core/scripts/foo.sh"]}),
        peer_team_state={
            "agent_status": {
                "bravo": {
                    "last_fresh_eyes_run": {
                        "time": fresh_iso,
                        "files": ["core/scripts/baz.sh"],
                    }
                }
            }
        },
    )

    # Case 4: neither in cooldown → no
    run_case(
        "CASE 4 both-expired",
        expected="no",
        current_set="core/scripts/foo.sh",
        own_json=json.dumps({"time": expired_iso, "files": ["core/scripts/foo.sh"]}),
        peer_team_state={
            "agent_status": {
                "bravo": {
                    "last_fresh_eyes_run": {
                        "time": expired_iso,
                        "files": ["core/scripts/foo.sh"],
                    }
                }
            }
        },
    )

    # Case 5: in cooldown but current ⊄ union → no
    run_case(
        "CASE 5 not-subset",
        expected="no",
        current_set="core/scripts/never-reviewed.sh",
        own_json=json.dumps({"time": fresh_iso, "files": ["core/scripts/foo.sh"]}),
        peer_team_state={
            "agent_status": {
                "bravo": {
                    "last_fresh_eyes_run": {
                        "time": fresh_iso,
                        "files": ["core/scripts/bar.sh"],
                    }
                }
            }
        },
    )

    # Case 6: own expired, peer fresh, current ⊆ peer → yes:peer (expiry filtering)
    run_case(
        "CASE 6 own-expired-peer-fresh",
        expected="yes:peer",
        current_set="core/scripts/foo.sh",
        own_json=json.dumps({"time": expired_iso, "files": ["core/scripts/foo.sh"]}),
        peer_team_state={
            "agent_status": {
                "bravo": {
                    "last_fresh_eyes_run": {
                        "time": fresh_iso,
                        "files": ["core/scripts/foo.sh"],
                    }
                }
            }
        },
    )

    # Case 7: peer record from SELF agent (alpha) is ignored → only own counts (yes:self)
    run_case(
        "CASE 7 self-record-ignored",
        expected="yes:self",
        current_set="core/scripts/foo.sh",
        own_json=json.dumps({"time": fresh_iso, "files": ["core/scripts/foo.sh"]}),
        peer_team_state={
            "agent_status": {
                "alpha": {  # self — must be ignored
                    "last_fresh_eyes_run": {
                        "time": fresh_iso,
                        "files": ["core/scripts/foo.sh", "core/scripts/extra.sh"],
                    }
                }
            }
        },
        self_agent="alpha",
    )

    # Case 8: defensive — no own, no peer at all → no
    run_case(
        "CASE 8 nothing-set",
        expected="no",
        current_set="core/scripts/foo.sh",
        own_json="null",
        peer_team_state=None,
    )

    # Case 9: peer entry with malformed time → ignored, fall back to no
    run_case(
        "CASE 9 malformed-peer-time",
        expected="no",
        current_set="core/scripts/foo.sh",
        own_json="null",
        peer_team_state={
            "agent_status": {
                "bravo": {
                    "last_fresh_eyes_run": {
                        "time": "garbage-not-iso",
                        "files": ["core/scripts/foo.sh"],
                    }
                }
            }
        },
    )

    # Case 10 (): list-of-records schema, single fresh record → yes:self
    run_case(
        "CASE 10 list-single-record",
        expected="yes:self",
        current_set="core/scripts/foo.sh",
        own_json=json.dumps([
            {"time": fresh_iso, "files": ["core/scripts/foo.sh"]},
        ]),
        peer_team_state=None,
    )

    # Case 11 (): list-of-records, two fresh records, current ⊆ union of both
    # This is the bug-fix path — legacy single-record overwrite would lose the
    # earlier record's coverage. List-of-records preserves both.
    run_case(
        "CASE 11 list-two-records-union",
        expected="yes:self",
        current_set="core/scripts/foo.sh\ncore/scripts/baz.sh",
        own_json=json.dumps([
            {"time": fresh_iso, "files": ["core/scripts/baz.sh"]},
            {"time": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
             "files": ["core/scripts/foo.sh"]},
        ]),
        peer_team_state=None,
    )

    # Case 12 (): list-of-records, mixed expired+fresh, current ⊆ fresh only.
    # Expired record's files MUST NOT contribute to coverage.
    run_case(
        "CASE 12 list-expiry-filtering",
        expected="yes:self",
        current_set="core/scripts/foo.sh",
        own_json=json.dumps([
            {"time": fresh_iso, "files": ["core/scripts/foo.sh"]},
            {"time": expired_iso, "files": ["core/scripts/expired.sh"]},
        ]),
        peer_team_state=None,
    )

    # Case 13 (): list-of-records, current set requires expired record's
    # coverage — must return "no" because expired record cannot count.
    run_case(
        "CASE 13 list-expired-coverage-fails",
        expected="no",
        current_set="core/scripts/expired.sh",
        own_json=json.dumps([
            {"time": fresh_iso, "files": ["core/scripts/other.sh"]},
            {"time": expired_iso, "files": ["core/scripts/expired.sh"]},
        ]),
        peer_team_state=None,
    )

    # Case 14 (): list-of-records, all expired → no in_cooldown
    run_case(
        "CASE 14 list-all-expired",
        expected="no",
        current_set="core/scripts/foo.sh",
        own_json=json.dumps([
            {"time": expired_iso, "files": ["core/scripts/foo.sh"]},
            {"time": (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S"),
             "files": ["core/scripts/foo.sh"]},
        ]),
        peer_team_state=None,
    )

    # Case 15 (): list-of-records, malformed entries (non-dict, missing
    # time, bad time) coexist with valid entry — valid wins, others ignored.
    run_case(
        "CASE 15 list-malformed-entries-ignored",
        expected="yes:self",
        current_set="core/scripts/foo.sh",
        own_json=json.dumps([
            "not-a-dict",
            {"time": "garbage", "files": ["core/scripts/junk.sh"]},
            {"files": ["core/scripts/no-time.sh"]},
            {"time": fresh_iso, "files": ["core/scripts/foo.sh"]},
        ]),
        peer_team_state=None,
    )

    # Case 16 (): list-of-records empty list → no in_cooldown
    run_case(
        "CASE 16 list-empty",
        expected="no",
        current_set="core/scripts/foo.sh",
        own_json="[]",
        peer_team_state=None,
    )

    if failures:
        print(f"TEST FAIL ({len(failures)} case(s)):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1

    print("TEST PASS: 16 cases — own-only, peer-only, union, expired, not-subset,")
    print("           own-expired-peer-fresh, self-ignored, nothing-set, malformed-time,")
    print("           list-single, list-two-records-union, list-expiry-filtering,")
    print("           list-expired-coverage-fails, list-all-expired,")
    print("           list-malformed-entries-ignored, list-empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
