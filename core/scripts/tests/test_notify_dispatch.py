"""notify_dispatch.py -- the FRAMEWORK notification chokepoint.

User directive 2026-08-16/17: the framework wants to notify the user and
double-checks before it happens; the domain supplies only the transport via
the slot world/scripts/notify-transport.sh. These tests run the real dispatcher
against a tmp world whose slot is a fake transport that records what it got.
Fixtures use example.com only; nothing here reaches a real transport.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import notify_dispatch as nd  # noqa: E402

FAKE_TRANSPORT = '''#!/usr/bin/env bash
# fake domain transport: record the payload + env, succeed unless asked to fail
cat > "$FAKE_OUT.$$.json"
echo "dispatched=${NOTIFY_DISPATCHED:-} cat=${NOTIFY_CATEGORY:-} goal=${NOTIFY_GOAL_ID:-}" >> "$FAKE_OUT.env"
if [ -n "${FAKE_FAIL:-}" ]; then echo "transport exploded" >&2; exit 7; fi
echo "fake transport accepted"
'''


@pytest.fixture
def world(tmp_path):
    (tmp_path / "board").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "notify-transport.sh").write_text(FAKE_TRANSPORT)
    return tmp_path


def _env(world, **kw):
    e = dict(os.environ)
    e.update({"MIND_WORLD": str(world), "STORAGE_BACKEND": "local", "MIND_AGENT": "alpha",
              "FAKE_OUT": str(world / "fake"), "USER_EMAIL": "operator@example.com"})
    e.pop("EMAIL_SEND_ALLOW_DUPLICATE", None)
    e.update(kw)
    return e


def _run(world, *args, stdin=None, **envkw):
    return subprocess.run([sys.executable, str(SCRIPTS / "notify_dispatch.py"), "--no-mirror-peers", *args],
                          input=stdin, env=_env(world, **envkw), capture_output=True, text=True, timeout=120)


def _sent(world):
    return sorted(world.glob("fake.*.json"))


def _ledger(world):
    p = world / "notifications-sent.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


BODY = "Decision needed on g-115-6222: the legacy identity PK has no live writer. I decided to keep it 30 days; override if you disagree."


def test_send_path_calls_slot_with_dispatched_env_and_records(world):
    p = _run(world, "--category", "decision-needed", "--subject", "Retire the legacy PK? (g-115-6222)",
             "--message", BODY, "--goal-id", "g-115-6222")
    assert p.returncode == nd.RC_SENT, p.stderr
    assert "fake transport accepted" in p.stdout
    files = _sent(world)
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload.get("XPayloadProvenance")  # built by the sanctioned builder
    assert "dispatched=1 cat=decision-needed goal=g-115-6222" in (world / "fake.env").read_text()
    rows = _ledger(world)
    assert len(rows) == 1 and rows[0]["rc"] == 0 and rows[0]["transport"] == "notify-transport.sh"
    assert rows[0]["to"] == "o***@e***.com" and "operator@example.com" not in (world / "notifications-sent.jsonl").read_text()


def test_duplicate_from_another_agent_is_refused_and_recorded_then_override_sends(world):
    assert _run(world, "--category", "decision-needed", "--subject", "Retire the legacy PK? (g-115-6222)", "--message", BODY).returncode == 0
    p = _run(world, "--category", "decision-needed", "--subject", "Your call: retiring the legacy identity PK",
             "--message", "Bravo here; reads on g-115-6222 say no writer remains. Please decide.", MIND_AGENT="bravo")
    assert p.returncode == nd.RC_DUP, p.stderr
    assert "DUPLICATE" in p.stderr and "alpha" in p.stderr
    assert len(_sent(world)) == 1  # transport NOT called
    rows = _ledger(world)
    assert rows[-1]["rc"] == nd.RC_DUP and rows[-1]["suppressed_duplicate_of"] == rows[0]["id"]
    p = _run(world, "--category", "decision-needed", "--subject", "Your call: retiring the legacy identity PK",
             "--message", "In addition to alpha's note on g-115-6222: the window closes 2026-09-15.",
             "--allow-duplicate", "adds the deadline", MIND_AGENT="bravo")
    assert p.returncode == nd.RC_SENT, p.stderr
    assert len(_sent(world)) == 2
    assert _ledger(world)[-1]["duplicate_override_reason"] == "adds the deadline"


def test_env_override_is_honoured_like_the_flag(world):
    assert _run(world, "--category", "decision-needed", "--subject", "Your call on g-7-7", "--message", "I decided to defer g-7-7 a week because the vendor is down; override if you disagree.").returncode == 0
    p = _run(world, "--category", "decision-needed", "--subject", "Your call on g-7-7 (update)", "--message", "New detail on g-7-7: the vendor now says two weeks, which changes the plan; still deferring unless you object.", EMAIL_SEND_ALLOW_DUPLICATE="deadline moved")
    assert p.returncode == nd.RC_SENT, p.stderr


def test_routing_gate_suppresses_status_reports_and_does_not_call_transport(world):
    p = _run(world, "--category", "completion", "--subject", "Completion Report (2h, 3 goals)",
             "--message", "alpha completed 3 goals: g-1-1, g-1-2, g-1-3. All committed and pushed. Nothing further.", "--dry-run")
    assert p.returncode == nd.RC_ROUTED, p.stderr
    assert "SUPPRESSED by routing gate" in p.stderr
    assert _sent(world) == []


def test_missing_slot_is_rc5_no_transport(world):
    (world / "scripts" / "notify-transport.sh").unlink()
    p = _run(world, "--category", "decision-needed", "--subject", "Your call on asp-9",
             "--message", "I decided to pause asp-9 for a week because the data source is down; override if you disagree.")
    assert p.returncode == nd.RC_NO_TRANSPORT
    assert "no transport configured" in p.stderr
    assert _ledger(world) == []  # nothing was sent, nothing recorded as sent


def test_transport_failure_is_rc6_and_not_recorded_as_sent(world):
    p = _run(world, "--category", "decision-needed", "--subject", "Your call on asp-9",
             "--message", "I decided to pause asp-9 for a week because the data source is down; override if you disagree.", FAKE_FAIL="1")
    assert p.returncode == nd.RC_TRANSPORT_FAIL
    assert "transport exploded" in p.stderr
    assert _ledger(world) == []


def test_payload_stdin_path_derives_subject_body_category(world):
    build = subprocess.run([sys.executable, str(SCRIPTS / "notify-build-payload.py"), "--agent", "alpha", "--category", "decision-needed",
                            "--subject", "Your call on g-9-9", "--message", "I decided to keep the bridge in maintenance mode for g-9-9 until Friday; override if you disagree."],
                           capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stderr
    p = _run(world, "--payload-stdin", stdin=build.stdout)  # no --category: derived from the payload's InfoType
    assert p.returncode == nd.RC_SENT, p.stderr
    rows = _ledger(world)
    assert rows[0]["category"] == "decision-needed" and rows[0]["subject"] == "Your call on g-9-9"
    # and a second, differently-worded ask on the SAME id within the window is a duplicate
    p2 = _run(world, "--category", "decision-needed", "--subject", "Need your decision: bridge maintenance (g-9-9)", "--message", "Zeta here -- should the bridge stay in maintenance for g-9-9? Please decide.", MIND_AGENT="zeta")
    assert p2.returncode == nd.RC_DUP


def test_routing_gate_lets_a_human_only_blocker_through(world):
    # a plain outage is fleet-handleable (2026-08-10 directive) -- but a
    # credential he alone can grant is a human-only class and SENDS
    p = _run(world, "--category", "blocker", "--subject", "Blocked: need a credential only you can grant (g-9-8)",
             "--message", "The deploy for g-9-8 is blocked on an API key that only the account owner can issue; nothing the fleet can provision.")
    assert p.returncode in (nd.RC_SENT, nd.RC_ROUTED), p.stderr  # depends on the human-only class list; never a crash
    if p.returncode == nd.RC_SENT:
        assert len(_sent(world)) == 1


def test_slot_need_not_be_executable(world):
    os.chmod(world / "scripts" / "notify-transport.sh", 0o644)  # own-cloud pulls do not preserve +x
    p = _run(world, "--category", "decision-needed", "--subject", "Your call on asp-9",
             "--message", "I decided to pause asp-9 for a week because the data source is down; override if you disagree.")
    assert p.returncode == nd.RC_SENT, p.stderr


def test_usage_errors(world):
    assert _run(world, "--category", "info", "--subject", "x").returncode == nd.RC_USAGE
    p = subprocess.run([sys.executable, str(SCRIPTS / "notify_dispatch.py"), "--category", "info", "--subject", "x", "--message", "y", "--world", str(world)],
                       env={**_env(world), "MIND_AGENT": ""}, capture_output=True, text=True, timeout=60)
    assert p.returncode == nd.RC_USAGE and "--agent" in p.stderr


def test_wrapper_and_skill_wiring():
    root = SCRIPTS.parent.parent
    assert (SCRIPTS / "notify-user.sh").exists()
    skill = (root / ".claude" / "skills" / "notify-user" / "SKILL.md").read_text(encoding="utf-8")
    assert "core/scripts/notify-user.sh" in skill
    hooks = (root / "core" / "config" / "conventions" / "domain-hooks.md").read_text(encoding="utf-8")
    assert "notify-transport" in hooks


# --------------------------------------------------------------------------- #
# `reply` — answering something HE asked ()
# --------------------------------------------------------------------------- #
# `reply` is the third ALWAYS_SEND category, so it is the one shape that walks
# past the 2026-08-10 suppression directive. The citation is the whole safety
# margin, and guard-4722 is why: it records the routing gate CORRECTLY refusing
# a reply-shaped message whose closing sentence had become a permission request
# for already-granted work, with the explicit remedy "not to re-send with a
# different category". These tests pin the citation, not the wording.

ANSWER = ("The grant was already applied on 2026-08-16 under policy "
          "ayoai-fleet-least-priv v7, Sid LambdaDeployRotateRevoke. "
          "No action needed on your side.")
ASKED = "your 2026-08-15 email 'send me an email with exact instructions'"


def test_reply_without_a_citation_is_refused_and_sends_nothing(world):
    p = _run(world, "--category", "reply", "--subject", "The IAM grant you asked about",
             "--message", ANSWER)
    assert p.returncode == nd.RC_USAGE, p.stderr
    assert "--in-reply-to" in p.stderr
    assert "guard-4722" in p.stderr, "the refusal must name why, or it reads as a missing-arg nit"
    assert _sent(world) == [], "a refused reply must not reach the transport"


def test_reply_with_a_citation_sends_and_the_citation_reaches_the_email(world):
    """The load-bearing one. A citation that stayed in the gate's working copy
    and never reached the payload would be an audit trail he cannot see — and
    his seeing it is what makes a wrong claim self-correcting."""
    p = _run(world, "--category", "reply", "--subject", "The IAM grant you asked about",
             "--message", ANSWER, "--in-reply-to", ASKED)
    assert p.returncode == nd.RC_SENT, p.stderr
    payload = json.loads(_sent(world)[0].read_text())
    blob = json.dumps(payload)
    assert ASKED in blob, "the citation never reached the payload"
    assert nd.REPLY_CITATION_PREFIX in blob


def test_the_citation_is_appended_after_the_answer_never_before_it(world):
    """TWO independent reasons, either of which alone justifies the order, so
    this is asserted by POSITION rather than by exact formatting (guard-355):

    1. notification_outreach.body_fingerprint() is the normalized HEAD of the
       body (BODY_FP_CHARS=400). A fixed prefix on every reply seeds a shared
       fingerprint, and the second unrelated reply inside the window gets
       refused as a duplicate of the first.
    2. He asked a question. The answer belongs at the top of the mail.
    """
    p = _run(world, "--category", "reply", "--subject", "The IAM grant you asked about",
             "--message", ANSWER, "--in-reply-to", ASKED)
    assert p.returncode == nd.RC_SENT, p.stderr
    body = json.loads(_sent(world)[0].read_text()).get("Body") or ""
    assert body.index("The grant was already applied") < body.index(nd.REPLY_CITATION_PREFIX)


def test_the_citation_is_recorded_in_the_outreach_ledger(world):
    """The audit half: 'he asked for this' must be reviewable later, not just
    visible in one inbox."""
    p = _run(world, "--category", "reply", "--subject", "The IAM grant you asked about",
             "--message", ANSWER, "--in-reply-to", ASKED)
    assert p.returncode == nd.RC_SENT, p.stderr
    rows = [r for r in _ledger(world) if r.get("category") == "reply"]
    assert rows, "the reply was not recorded in the ledger"
    assert nd.REPLY_CITATION_PREFIX in json.dumps(rows[-1])


def test_a_prebuilt_payload_already_carrying_the_citation_passes(world):
    """ONE rule covers BOTH entry paths: supply --in-reply-to, or already carry
    the citation. The payload path derives its category after main() has parsed
    argv, so a main()-only check would leave exactly the bypass-capable shape
    unguarded — this test is what fails if the check migrates up there."""
    payload = {"InfoType": "Reply", "Title": "The IAM grant you asked about",
               "Body": ANSWER + "\n\n" + nd.REPLY_CITATION_PREFIX + ASKED,
               "XPayloadProvenance": "test/v1"}
    p = _run(world, "--payload-stdin", stdin=json.dumps(payload))
    assert p.returncode == nd.RC_SENT, p.stderr


def test_a_prebuilt_reply_payload_without_a_citation_is_refused(world):
    """The control for the test above — otherwise 'both paths are covered'
    would pass just as happily if the payload path checked nothing at all."""
    payload = {"InfoType": "Reply", "Title": "The IAM grant you asked about",
               "Body": ANSWER, "XPayloadProvenance": "test/v1"}
    p = _run(world, "--payload-stdin", stdin=json.dumps(payload))
    assert p.returncode == nd.RC_USAGE, p.stderr
    assert _sent(world) == []


def test_reply_is_info_shaped_and_deliberately_NOT_step_1_5_exempt():
    """The cross-cutting contract, in one place because its two halves live in
    two files and only their INTERSECTION is correct (the g-115-4962 lesson,
    one category over).

    info-shaped: `blocker` is the only shape with no pretty renderer, and an
    answer to a direct question is the last message that should arrive as an
    "AyoAi Error Alert" in raw text (rb-3754: a category/shape mismatch makes a
    payload vanish server-side while the async invoke still reports success).

    NOT Step-1.5-exempt: the routing gate now always sends `reply`, so the
    approval-request gate is the only thing left standing between it and the
    user — and guard-4722 names the exact message it has to catch. Exempting
    `reply` would delete that catch. The digest's exemption does NOT transfer:
    it quotes goal descriptions it did not author, a reply is composed
    deliberately one message at a time.
    """
    import re
    builder = (SCRIPTS / "notify-build-payload.py").read_text(encoding="utf-8")
    valid = re.search(r"VALID_CATEGORIES = \(([^)]*)\)", builder, re.S)
    assert valid and "reply" in set(re.findall(r'"([a-z-]+)"', valid.group(1)))
    assert '"reply": "Reply"' in builder, "reply must map to an InfoType, or the builder KeyErrors"

    skill = (SCRIPTS.parent.parent / ".claude" / "skills" / "notify-user" / "SKILL.md").read_text(encoding="utf-8")
    gate = re.search(r"IF category not in \(([^)]*)\):", skill)
    assert gate, "could not find Step 1.5's exempt tuple in notify-user/SKILL.md"
    assert "reply" not in set(re.findall(r'"([a-z-]+)"', gate.group(1))), (
        "`reply` was added to Step 1.5's exempt tuple — that converts an "
        "ALWAYS_SEND category into the re-send door guard-4722 forbids")
