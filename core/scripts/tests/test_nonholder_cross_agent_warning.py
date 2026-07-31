""": cross-agent non-holder complete-by must WARN, not stay silent.

Incident (2026-07-31): foxtrot's claim on g-115-4204 was falsely released
mid-execution; bravo then claimed the goal legitimately; foxtrot's unaware
session completed it at 07:55:54 while claimed_by=bravo — and NOTHING fired.
`_nonholder_claim_warning` (g-115-3176 outcome 5) explicitly returned None on
`holder != caller_agent`, deferring to "a separate concern with its own
handling" — handling that did not exist. The claim side refuses cross-agent
claims; the complete side had zero coverage.

Fix under test: when the holder is a DIFFERENT agent whose claim-holding
session is the LIVE runner of that agent, complete-by/release return a
warning (surfaced to stderr by the wrappers). Dormant/dead holders stay
quiet — completing a dead session's abandoned goal is ordinary supersession,
and the sweeps must never be nagged (warn-only posture preserved; nothing
refuses).

End-to-end via DaemonFixture: seeds a world goal claimed by `bravo`
(claimed_by_sid=SID-B), a live bravo runner (running-session-id=SID-B +
fresh runner-heartbeat), and completes as `alpha`.

Run: STORAGE_BACKEND=local py -3 core/scripts/tests/test_nonholder_cross_agent_warning.py
"""
import json
import os
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path) -> Path:
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": "g-100-01",
        "title": "Cross-agent completion target",
        "description": "Claimed by bravo; completed by alpha in the test.",
        "status": "in-progress",
        "priority": "MEDIUM",
        "claimed_by": "bravo",
        "claimed_at": "2026-07-31T07:00:00",
        "claimed_by_sid": "SID-BRAVO-LIVE",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-100",
        "title": "nonholder cross-agent warning",
        "motivation": "regression pin for g-115-4232",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-07-01T00:00:00",
        "goals": [goal],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_bravo_runner(project_root: Path, sid: str, heartbeat_age_s: float):
    """Create agents/bravo/session with running-session-id + heartbeat."""
    sess = project_root / "agents" / "bravo" / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "running-session-id").write_text(sid, encoding="utf-8")
    hb = sess / "runner-heartbeat"
    hb.touch()
    if heartbeat_age_s:
        old = time.time() - heartbeat_age_s
        os.utime(hb, (old, old))
    return sess


def _seed_heartbeat_config(project_root: Path):
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        "runner_heartbeat:\n  stale_minutes: 15\n", encoding="utf-8")


def _complete_by(port: int, goal_id: str, agent: str, sid: str):
    url = (f"http://127.0.0.1:{port}/v1/aspirations/complete-by"
           f"?goal_id={goal_id}&source=world&agent_name={agent}&sid={sid}")
    req = urllib.request.Request(url, method="POST",
                                 headers={"X-Mind-Agent": agent})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_case(name, heartbeat_age_s, rsid, expect_warning):
    with tempfile.TemporaryDirectory(prefix="nonholder_xagent_") as tmpd:
        tmp = Path(tmpd)
        world = _make_world(tmp)
        with DaemonFixture(world, agent="alpha") as df:
            _seed_heartbeat_config(df.project_root)
            _seed_bravo_runner(df.project_root, rsid, heartbeat_age_s)
            resp = _complete_by(df.port, "g-100-01", "alpha", "SID-ALPHA")
            warnings = resp.get("warnings") or []
            xagent = [w for w in warnings
                      if "DIFFERENT AGENT" in w and "bravo" in w]
            if expect_warning:
                assert xagent, (
                    f"expected cross-agent non-holder warning, got "
                    f"warnings={warnings!r}")
            else:
                assert not xagent, (
                    f"unexpected cross-agent warning for a non-live holder: "
                    f"{xagent!r}")
            goal = resp.get("goal") or {}
            assert goal.get("completed_by") == "alpha", (
                "warn-only contract broken — completion must still apply "
                f"(completed_by={goal.get('completed_by')!r})")
    print(f"  [PASS] {name}")
    return True


def main():
    cases = [
        # Live bravo runner holding the claim → WARN.
        ("test_live_cross_agent_holder_warns",
         lambda: _run_case("test_live_cross_agent_holder_warns",
                           heartbeat_age_s=0, rsid="SID-BRAVO-LIVE",
                           expect_warning=True)),
        # Stale heartbeat (dormant holder) → quiet (ordinary supersession).
        ("test_dormant_holder_stays_quiet",
         lambda: _run_case("test_dormant_holder_stays_quiet",
                           heartbeat_age_s=3600, rsid="SID-BRAVO-LIVE",
                           expect_warning=False)),
        # Holder sid is NOT bravo's current runner → quiet (previous session).
        ("test_non_runner_holder_sid_stays_quiet",
         lambda: _run_case("test_non_runner_holder_sid_stays_quiet",
                           heartbeat_age_s=0, rsid="SID-BRAVO-NEWER",
                           expect_warning=False)),
    ]
    passed = 0
    for name, fn in cases:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:
            print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(cases)} passed")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
