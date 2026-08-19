"""notification_outreach.py -- the fleet-wide "already told the user?" ledger + gate.

User directive 2026-08-16: agents must scan whether anyone (any agent, any
world) already reached out about a topic before sending, and ask whether the
send is needed at all. rb-7986 measured the send site wrote no record; this
module is that record plus the gate. Fixtures use example.com addresses only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import notification_outreach as no  # noqa: E402

NOW = datetime(2026, 8, 16, 12, 0, 0)


@pytest.fixture
def world(tmp_path):
    (tmp_path / "board").mkdir()
    return tmp_path


def _rec(world, **kw):
    kw.setdefault("agent", "alpha")
    kw.setdefault("category", "decision-needed")
    kw.setdefault("body", "")
    kw.setdefault("now", NOW)
    r = no.build_record(**kw)
    no._append(no.ledger_path(world), r)
    return r


# ---------------------------------------------------------------- normalisation

def test_strip_agent_prefix_and_normalize():
    assert no.strip_agent_prefix("[Alpha] [Bravo] Hello") == "Hello"
    a = no.normalize_subject("[Alpha] Decision needed: retire PK? 2026-08-16T11:00")
    b = no.normalize_subject("[Bravo] Decision needed: retire PK? 2026-08-15T09:30")
    assert a == b  # timestamps and sender tag do not make a new topic


def test_entity_ids_extracted_case_insensitively():
    assert no.entity_ids("Re G-115-6222 and guard-4061", "see pq-alpha-x rb-7986") == {
        "g-115-6222", "guard-4061", "pq-alpha-x", "rb-7986"}


def test_shape_email_is_shape_only():
    assert no.shape_email("operator@example.com") == "o***@e***.com"
    assert no.shape_email("x@mail.example.co.uk") == "x***@m***.uk"
    assert no.shape_email("") == ""
    assert no.shape_email("garbage") == "***"


# ---------------------------------------------------------------- matching

def test_no_prior_means_send(world):
    assert no.find_prior("Anything at all", "", "info", world=world, now=NOW) == []


def test_same_topic_from_another_agent_is_duplicate(world):
    _rec(world, subject="[Alpha] Should we retire the legacy PK? (g-115-6222)")
    hits = no.find_prior("[Bravo] Your call: retiring the legacy identity PK", "reads on g-115-6222", "decision-needed", world=world, now=NOW + timedelta(hours=1))
    assert hits and hits[0]["agent"] == "alpha"
    assert "g-115-6222" in hits[0]["why"] or "overlap" in hits[0]["why"]


def test_unrelated_subject_is_not_duplicate(world):
    _rec(world, subject="[Alpha] Should we retire the legacy PK? (g-115-6222)")
    assert no.find_prior("[Echo] ARC-AGI leaderboard submission ready", "totally different", "info", world=world, now=NOW + timedelta(hours=1)) == []


def test_body_fingerprint_catches_reworded_subject(world):
    body = "The Roblox bridge has been unreachable since 03:00 UTC and three restarts failed; the game session cannot start."
    _rec(world, subject="[Alpha] Bridge outage", body=body, category="blocker")
    hits = no.find_prior("[Zeta] Heads-up: game sessions cannot start", body, "blocker", world=world, now=NOW + timedelta(hours=2))
    assert hits and hits[0]["why"].startswith("body overlap")


def test_windows_blocker_24h_default_7d(world):
    _rec(world, subject="[Alpha] Bridge outage g-999-01", category="blocker")
    _rec(world, subject="[Alpha] Question about asp-777 scope", category="decision-needed")
    late = NOW + timedelta(hours=30)
    assert no.find_prior("[Bravo] Bridge outage g-999-01", "", "blocker", world=world, now=late) == []
    assert no.find_prior("[Bravo] Question about asp-777 scope", "", "decision-needed", world=world, now=late)
    assert no.find_prior("[Bravo] Question about asp-777 scope", "", "decision-needed", world=world, now=NOW + timedelta(days=8)) == []


def test_suppressed_rows_do_not_count_as_prior(world):
    _rec(world, subject="[Bravo] Retire the PK? g-115-6222", suppressed_duplicate_of="ntf-x")
    assert no.find_prior("[Echo] Retire the PK? g-115-6222", "", "decision-needed", world=world, now=NOW + timedelta(hours=1)) == []


def test_peer_world_board_mirror_is_seen(world):
    msg = {"id": "msg-peer-1", "author": "omni@zds-mind", "timestamp": (NOW - timedelta(hours=3)).isoformat(),
           "channel": "coordination", "type": "status", "tags": ["user-outreach", "blocker"],
           "text": "USER-OUTREACH env: zds-mind agent: omni category: blocker subject: Roblox bridge down since 03:00\nids: -"}
    (world / "board" / "coordination.jsonl").write_text(json.dumps(msg) + "\n")
    hits = no.find_prior("[Alpha] Blocker: Roblox bridge is down", "since 03:00", "blocker", world=world, now=NOW)
    assert hits and hits[0]["env"] == "zds-mind" and hits[0]["agent"] == "omni"
    assert hits[0]["source"] == "board:coordination.jsonl"


# ---------------------------------------------------------------- record

def test_record_stores_recipient_in_shape_only_and_ids(world):
    r = _rec(world, subject="[Alpha] About g-115-6222", body="x", to="operator@example.com", transport="aws-cli", rc=0)
    raw = no.ledger_path(world).read_text()
    assert "operator@example.com" not in raw
    assert r["to"] == "o***@e***.com"
    assert r["entity_ids"] == ["g-115-6222"]
    assert r["subject"] == "About g-115-6222"  # sender tag stripped


# ---------------------------------------------------------------- CLI

def _cli(*args, world):
    return subprocess.run([sys.executable, str(SCRIPTS / "notification_outreach.py"), *args, "--world", str(world)],
                          capture_output=True, text=True, timeout=60)


def test_cli_check_record_roundtrip(world):
    p = _cli("check", "--category", "info", "--subject", "[Alpha] Hello g-1-1", world=world)
    assert p.returncode == 0, p.stdout + p.stderr
    p = _cli("record", "--agent", "alpha", "--category", "info", "--subject", "[Alpha] Hello g-1-1", "--to", "operator@example.com", world=world)
    assert p.returncode == 0, p.stdout + p.stderr
    p = _cli("check", "--category", "info", "--subject", "[Bravo] Hello again g-1-1", world=world)
    assert p.returncode == 1
    assert "DUPLICATE" in p.stdout and "alpha" in p.stdout
    p = _cli("check", "--category", "info", "--subject", "[Bravo] Hello again g-1-1", "--json", world=world)
    assert json.loads(p.stdout)["duplicate"] is True
    p = _cli("list", world=world)
    assert p.returncode == 0 and "Hello g-1-1" in p.stdout


def test_wrapper_script_exists_and_is_wired_into_notify_user_skill():
    root = SCRIPTS.parent.parent
    assert (SCRIPTS / "notification-outreach-gate.sh").exists()
    skill = (root / ".claude" / "skills" / "notify-user" / "SKILL.md").read_text(encoding="utf-8")
    # the skill delegates to the framework dispatcher, which runs this gate
    assert "core/scripts/notify-user.sh" in skill
    assert "notification-outreach-gate.sh list" in skill
    assert "--allow-duplicate" in skill


# ---------------------------------------------------------------- digests

def test_digest_dedups_fleet_wide_by_category_inside_20h(world):
    _rec(world, subject="[Alpha] Fleet digest — 2026-08-16 (from alpha)", category="user-digest",
         body="Needs you: g-1-1 g-2-2 guard-9; Blocked: g-3-3")
    late = NOW + timedelta(hours=6)
    hits = no.find_prior("[Bravo] Fleet digest — 2026-08-16 (from bravo)", "Needs you: g-1-1", "user-digest", world=world, now=late)
    assert hits and hits[0]["agent"] == "alpha" and "digest" in hits[0]["why"]
    # a wholly different subject is still the same digest lane
    assert no.find_prior("[Zeta] Daily status", "", "user-digest", world=world, now=late)
    # after the window, the next digest goes
    assert no.find_prior("[Bravo] Fleet digest — 2026-08-17 (from bravo)", "", "user-digest", world=world, now=NOW + timedelta(hours=21)) == []


def test_digest_and_specific_asks_never_suppress_each_other(world):
    _rec(world, subject="[Alpha] Fleet digest — 2026-08-16 (from alpha)", category="user-digest",
         body="Needs you: g-1-1 g-2-2 guard-9; Blocked: g-3-3 -- see the goals below")
    # a specific decision about an id the digest listed is NOT a duplicate of the digest
    assert no.find_prior("[Bravo] Your call on g-2-2", "Decision needed on g-2-2: keep or retire?", "decision-needed", world=world, now=NOW + timedelta(hours=1)) == []
    _rec(world, subject="[Bravo] Your call on g-2-2", category="decision-needed", body="Decision needed on g-2-2")
    # and that ask does not block tomorrow's digest
    assert no.find_prior("[Echo] Fleet digest — 2026-08-17 (from echo)", "Needs you: g-2-2", "user-digest", world=world, now=NOW + timedelta(hours=22)) == []
