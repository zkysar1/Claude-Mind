"""Tests for notify-build-payload.py — silent-empty-email guard.

Origin (2026-05-20): completion-report emails arrived with only Title +
UTC timestamp + reply-footer; the SendInfoAlert payload's Body field was
empty because /notify-user's SKILL.md instructed the LLM to hand-write
the JSON template at every call site. The strategic fix moved payload
construction into this deterministic helper. These tests pin the
contract.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = PROJECT_ROOT / "core" / "scripts" / "notify-build-payload.py"


def run_helper(args, env_extra=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture
def fake_root(tmp_path):
    """Build a fake project root containing several agents' self.md files."""
    agents = tmp_path / "agents"

    alpha = agents / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "self.md").write_text(
        '---\ncreated: "2026-01-01"\nsource: "test"\n---\n\n'
        "# Self\n\n"
        "I am Alpha, the developer for the framework — the hands that build.\n\n"
        "Additional paragraphs go here.\n",
        encoding="utf-8",
    )

    # Bravo-shape: double front-matter blocks (revision_id in second block)
    bravo = agents / "bravo"
    bravo.mkdir()
    (bravo / "self.md").write_text(
        '---\ncreated: "2026-03-29"\nsource: "agent"\n---\n'
        'revision_id: "bravo-001"\nprevious_revision_id: null\n---\n\n'
        "# Self\n\n"
        "I am the product manager and business analyst for the framework — the mind that drives the product forward.\n\n"
        "More body content follows.\n",
        encoding="utf-8",
    )

    # First sentence wraps across two lines (echo-shape)
    echo = agents / "echo"
    echo.mkdir()
    (echo / "self.md").write_text(
        '---\ncreated: "2026-05-16"\nsource: "agent"\n---\n\n'
        "# Self\n\n"
        "I am Echo — the ARC-AGI-3 Vertical Owner for the framework. I own one\n"
        "question and will not let go of it.\n",
        encoding="utf-8",
    )

    # Malformed: no '# Self' heading
    broken = agents / "broken"
    broken.mkdir()
    (broken / "self.md").write_text(
        '---\ncreated: "test"\n---\n\nNo Self heading here.\n',
        encoding="utf-8",
    )

    return tmp_path


# ============================================================================
# Happy paths — every category emits the right shape
# ============================================================================

def test_completion_payload_shape(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "completion",
        "--subject", "Completion Report (31h)",
        "--message", "Window covered 31h. 6 goals closed across 2 aspirations. System HEALTHY.",
        "--project-root", str(fake_root),
    ])
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)
    assert payload["InfoMessage"] == "completion: Completion Report (31h)"
    assert payload["InfoType"] == "Completion Report"
    assert payload["Title"] == "Completion Report (31h)"
    # : Body is the message verbatim — NO identity prepend (plain-
    # language contract). The old assertion checked Body.startswith the '# Self'
    # identity sentence; that prepend is gone.
    assert payload["Body"] == "Window covered 31h. 6 goals closed across 2 aspirations. System HEALTHY."
    assert not payload["Body"].startswith("I am Alpha")
    assert payload["Sections"] == []
    assert payload["NextSteps"] == []


def test_blocker_payload_shape(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "blocker",
        "--subject", "Bridge down",
        "--message", "Bridge servers unreachable across 4 environments for 3 consecutive probes.",
        "--project-root", str(fake_root),
    ])
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)
    assert "ErrorMessage" in payload
    assert "ErrorFrom" in payload
    assert payload["ErrorFrom"] == "Bridge down"
    # : ErrorMessage is the message verbatim — NO identity prepend.
    assert payload["ErrorMessage"] == "Bridge servers unreachable across 4 environments for 3 consecutive probes."
    assert not payload["ErrorMessage"].startswith("I am Alpha")
    # SendErrorAlert and SendInfoAlert are different  functions; do
    # NOT mix fields between them.
    assert "InfoMessage" not in payload
    assert "Title" not in payload
    assert "Body" not in payload


def test_info_update_decision_categories(fake_root):
    for cat, expected_info_type in [
        ("info", "Notification"),
        ("update", "Aspiration Update"),
        ("decision-needed", "Decision Needed"),
    ]:
        rc, out, err = run_helper([
            "--agent", "alpha",
            "--category", cat,
            "--subject", f"Test {cat}",
            "--message", "Long enough to clear the 20-char minimum body threshold easily.",
            "--project-root", str(fake_root),
        ])
        assert rc == 0, f"category={cat}: stderr: {err}"
        payload = json.loads(out)
        assert payload["InfoType"] == expected_info_type
        assert payload["InfoMessage"] == f"{cat}: Test {cat}"


# ============================================================================
# Self-identity extraction
# ============================================================================

def test_double_front_matter_self_md(fake_root):
    """Bravo-style self.md with two YAML blocks before '# Self' still VALIDATES.

    g-115-2822: the identity is no longer prepended to the body, but
    extract_self_identity() is still called for the rc=3 validation. rc==0 here
    proves the double-front-matter shape still parses without a false rc=3 —
    that is the function's remaining contract. The body is the message verbatim.
    """
    rc, out, err = run_helper([
        "--agent", "bravo",
        "--category", "completion",
        "--subject", "Test",
        "--message", "Long enough to clear the 20-char minimum body threshold easily.",
        "--project-root", str(fake_root),
    ])
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)
    # Body is verbatim — no identity prepend, no '# Self' sentence leak.
    assert payload["Body"] == "Long enough to clear the 20-char minimum body threshold easily."
    assert "product manager and business analyst" not in payload["Body"]


def test_first_sentence_wraps_lines(fake_root):
    """Echo's first sentence spans 2 source lines — the self.md still VALIDATES.

    g-115-2822: identity is no longer emitted in the body, but
    extract_self_identity() still runs for validation. rc==0 proves the
    wrapped-first-sentence shape parses without a false rc=3.
    """
    rc, out, err = run_helper([
        "--agent", "echo",
        "--category", "info",
        "--subject", "Test",
        "--message", "Long enough to clear the 20-char minimum body threshold easily.",
        "--project-root", str(fake_root),
    ])
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)
    # Body is verbatim — no identity prepend.
    assert payload["Body"] == "Long enough to clear the 20-char minimum body threshold easily."
    assert "ARC-AGI-3 Vertical Owner" not in payload["Body"]


# ============================================================================
# Fail-loud paths — the silent-empty-email guard and friends
# ============================================================================

def test_short_message_refused(fake_root):
    """The headline test for the 2026-05-20 incident."""
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "completion",
        "--subject", "Empty",
        "--message", "too short",
        "--project-root", str(fake_root),
    ])
    assert rc == 2, f"expected refusal (rc=2); got rc={rc}, out={out!r}, err={err!r}"
    assert "Silent-empty-email guard" in err


def test_empty_message_refused(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "completion",
        "--subject", "Empty",
        "--message", "",
        "--project-root", str(fake_root),
    ])
    assert rc == 2
    assert "0 chars" in err


def test_missing_self_md_fails_loud(fake_root):
    rc, out, err = run_helper([
        "--agent", "ghost",
        "--category", "info",
        "--subject", "x",
        "--message", "y" * 100,
        "--project-root", str(fake_root),
    ])
    assert rc == 3
    assert "self.md does not exist" in err


def test_malformed_self_md_no_heading_fails_loud(fake_root):
    rc, out, err = run_helper([
        "--agent", "broken",
        "--category", "info",
        "--subject", "x",
        "--message", "y" * 100,
        "--project-root", str(fake_root),
    ])
    assert rc == 3
    assert "no '# Self' heading" in err


def test_bad_category_refused(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "nonsense",
        "--subject", "x",
        "--message", "y" * 100,
        "--project-root", str(fake_root),
    ])
    # argparse exits 2 on choice mismatch — that's the same code as our
    # silent-empty-email guard, but the stderr text is different.
    assert rc == 2
    assert "invalid choice" in err.lower() or "nonsense" in err


def test_missing_agent_refused(fake_root):
    rc, out, err = run_helper(
        [
            "--category", "info",
            "--subject", "x",
            "--message", "y" * 100,
            "--project-root", str(fake_root),
        ],
        # Explicitly unset MIND_AGENT to force the failure
        env_extra={"MIND_AGENT": ""},
    )
    assert rc == 1
    assert "MIND_AGENT" in err or "--agent" in err


def test_missing_message_refused(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "info",
        "--subject", "x",
        "--project-root", str(fake_root),
    ])
    assert rc == 1
    assert "--message" in err


# ============================================================================
# Optional inputs — env fallback, sections, next-steps, message-file
# ============================================================================

def test_agent_from_env_var(fake_root):
    rc, out, err = run_helper(
        [
            "--category", "info",
            "--subject", "EnvAgentTest",
            "--message", "Long enough to clear the 20-char minimum body threshold easily.",
            "--project-root", str(fake_root),
        ],
        env_extra={"MIND_AGENT": "alpha"},
    )
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)
    # : Body is verbatim — no identity prepend (the env-var fallback
    # still resolves the agent + validates its self.md; only the prepend is gone).
    assert payload["Body"] == "Long enough to clear the 20-char minimum body threshold easily."
    assert not payload["Body"].startswith("I am Alpha")


def test_message_file_input(fake_root):
    msg_file = fake_root / "msg.md"
    msg_file.write_text(
        "Detailed completion report content.\n"
        "Multiple paragraphs explaining what happened.\n"
        "Plenty of substance for the user to read.\n",
        encoding="utf-8",
    )
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "completion",
        "--subject", "From File",
        "--message-file", str(msg_file),
        "--project-root", str(fake_root),
    ])
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)
    assert "Detailed completion report content" in payload["Body"]


def test_message_file_missing_refused(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "info",
        "--subject", "x",
        "--message-file", str(fake_root / "nope.md"),
        "--project-root", str(fake_root),
    ])
    assert rc == 1
    assert "does not exist" in err


def test_sections_and_next_steps_passthrough(fake_root):
    sections = json.dumps([
        {"title": "Goals Closed", "style": "success", "items": ["g-001-04", "g-115-985"]}
    ])
    next_steps = json.dumps(["Review the deep close in g-115-985"])
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "completion",
        "--subject", "Test",
        "--message", "Long enough to clear the 20-char minimum body threshold easily.",
        "--sections-json", sections,
        "--next-steps-json", next_steps,
        "--project-root", str(fake_root),
    ])
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)
    assert payload["Sections"][0]["title"] == "Goals Closed"
    assert payload["NextSteps"][0] == "Review the deep close in g-115-985"


def test_bad_sections_json_refused(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "info",
        "--subject", "x",
        "--message", "y" * 100,
        "--sections-json", "{not json",
        "--project-root", str(fake_root),
    ])
    assert rc == 1
    assert "sections-json" in err


def test_sections_must_be_array(fake_root):
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "info",
        "--subject", "x",
        "--message", "y" * 100,
        "--sections-json", '{"title": "wrong shape"}',
        "--project-root", str(fake_root),
    ])
    assert rc == 1
    assert "array" in err


# ============================================================================
# Output shape — JSON correctness
# ============================================================================

def test_output_is_valid_json(fake_root):
    """The whole point: stdout is parseable JSON suitable for piping to email-send.sh."""
    rc, out, err = run_helper([
        "--agent", "alpha",
        "--category", "info",
        "--subject", "Subject with \"quotes\" and \\ backslash",
        "--message", "Body with em-dash — and unicode ✅ and newlines\nshould all serialize cleanly enough to pipe.",
        "--project-root", str(fake_root),
    ])
    assert rc == 0, f"stderr: {err}"
    payload = json.loads(out)  # would raise if malformed
    assert payload["Title"] == 'Subject with "quotes" and \\ backslash'
    assert "em-dash —" in payload["Body"]
    assert "✅" in payload["Body"]
