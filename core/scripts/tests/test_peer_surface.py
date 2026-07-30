"""Tests for peer_surface.py -- the /prime cross-deployment surface ().

Each test pins one of the three measured traps documented in the module
docstring. All three produce a WRONG-BUT-PLAUSIBLE peer count rather than an
error, which is why they need explicit pins.
"""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import peer_surface  # noqa: E402


REGISTRY = {
    "ayoai-mind": "own-cloud",
    "claude-mind": "local",
    "zds-mind": "local",
    "local": "local",
}
SELF = "ayoai-mind"
ROSTER = ["alpha", "bravo", "echo", "foxtrot", "zeta"]


def row(author, channel="coordination"):
    return {"author": author, "channel": channel, "text": "x"}


def classify(rows):
    return peer_surface.classify(rows, SELF, REGISTRY, ROSTER)


# ── Trap 1: board-read.sh --json emits JSONL, not a JSON array ───────────

def test_parse_jsonl_reads_concatenated_objects():
    """json.load() raises 'Extra data' on line 2 of this input."""
    text = "\n".join(json.dumps({"author": "omni", "n": i}) for i in range(3))
    assert len(peer_surface.parse_jsonl(io.StringIO(text))) == 3


def test_parse_jsonl_rejects_array_assumption():
    """A real JSON array is NOT the wire format; one line -> one row, not 3."""
    assert len(peer_surface.parse_jsonl(io.StringIO(json.dumps([1, 2, 3])))) == 1


def test_parse_jsonl_skips_blank_and_malformed_lines():
    text = '{"author": "omni"}\n\nnot json\n{"author": "omni@zds-mind"}\n'
    assert len(peer_surface.parse_jsonl(io.StringIO(text))) == 2


# ── Trap 3: "author not in roster" over-counts ───────────────────────────

def test_bare_local_artifact_authors_are_not_counted_as_peers():
    """Measured 2026-07-30: `investigate` / `meta-tiebreaker` are LOCAL posts
    whose author field captured a goal-title fragment. They are non-roster, so
    a naive not-in-roster predicate counts them as peer traffic. They must be
    excluded from the count AND surfaced, not silently dropped."""
    res = classify([row("investigate"), row("meta-tiebreaker")])
    assert res["inbound_total"] == 0
    assert res["unattributed"] == {"investigate": 1, "meta-tiebreaker": 1}


def test_bare_author_attributes_only_with_at_form_evidence():
    """Bare `omni` counts ONLY because `omni@zds-mind` is independently seen."""
    res = classify([row("omni"), row("omni"), row("omni@zds-mind")])
    assert res["inbound_total"] == 3
    assert res["confirmed"] == 1
    assert res["attributed"] == 2
    assert res["by_env"] == {"zds-mind": 3}
    assert res["unattributed"] == {}


def test_bare_author_without_evidence_stays_unattributed():
    """Same author, no @-form anywhere -> not counted. This is the ONLY thing
    separating `omni` from `investigate`; drop the evidence and omni must fall
    back to unattributed too."""
    res = classify([row("omni"), row("omni")])
    assert res["inbound_total"] == 0
    assert res["unattributed"] == {"omni": 2}


def test_local_roster_authors_never_counted_or_reported():
    res = classify([row("bravo"), row("alpha"), row("zeta")])
    assert res["inbound_total"] == 0
    assert res["unattributed"] == {}


def test_self_env_marker_is_not_inbound():
    """A local agent stamping its own env-id is outbound-shaped, not inbound."""
    res = classify([row("bravo@ayoai-mind")])
    assert res["inbound_total"] == 0


def test_unregistered_env_marker_is_not_counted_but_IS_reported():
    """An env-id absent from the registry must not be counted as peer traffic
    (we cannot vouch for it) but must never be dropped silently either.

    The first draft did exactly that -- not counted, not reported, zero trace,
    indistinguishable from no traffic. Caught by fresh-eyes review of this very
    file. A deployment nobody registered is the highest-signal thing this
    surface can see, so silence is the worst possible handling."""
    res = classify([row("someone@not-a-registered-world")] * 2)
    assert res["inbound_total"] == 0
    assert res["unregistered_envs"] == {"not-a-registered-world": 2}
    assert res["unattributed"] == {}  # distinct bucket: has a marker, just unknown


def test_self_env_marker_is_not_reported_as_unregistered():
    """Guards the fix's blast radius: `bravo@ayoai-mind` is outbound-shaped and
    must stay out of BOTH the peer count and the unregistered bucket."""
    res = classify([row("bravo@ayoai-mind")])
    assert res["inbound_total"] == 0
    assert res["unregistered_envs"] == {}


def test_unregistered_line_is_rendered(monkeypatch, capsys):
    out = _render([row("ghost@who-dis")], monkeypatch, capsys)
    assert "UNREGISTERED" in out
    assert "who-dis" in out


def test_channel_and_env_breakdown():
    res = classify([
        row("omni@zds-mind", "coordination"),
        row("omni", "findings"),
        row("omni", "findings"),
    ])
    assert res["by_channel"] == {"coordination": 1, "findings": 2}
    assert res["by_env"] == {"zds-mind": 3}


# ── split_author: '@' not '-', because every env-id contains a hyphen ────

@pytest.mark.parametrize("author,expected", [
    ("omni@zds-mind", ("omni", "zds-mind")),
    ("omni", ("omni", None)),
    ("alpha-ayoai-mind", ("alpha-ayoai-mind", None)),  # hyphen form: unsplittable
    ("", ("", None)),
    (None, ("", None)),
])
def test_split_author(author, expected):
    assert peer_surface.split_author(author) == expected


# ── Trap 2 / display: absence of traffic is not absence of a channel ─────

def _render(rows, monkeypatch, capsys, **env):
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        "\n".join(json.dumps(r) for r in rows)))
    base = {
        "PEER_SELF_ENV": SELF,
        "PEER_REGISTRY": json.dumps(REGISTRY),
        "PEER_ROSTER": ",".join(ROSTER),
        "PEER_WINDOW": "7d",
        "PEER_JSON": "",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    peer_surface.main()
    return capsys.readouterr().out


def test_quiet_window_does_not_read_as_no_channel(monkeypatch, capsys):
    out = _render([], monkeypatch, capsys)
    assert "live but quiet" in out
    assert "Peers: 3 registered" in out


def test_display_names_peers_backends_and_pointer(monkeypatch, capsys):
    out = _render([row("omni@zds-mind")], monkeypatch, capsys)
    assert "zds-mind:local" in out
    assert "self=ayoai-mind:own-cloud" in out
    assert "peer-board-post.sh" in out
    assert "cross-deployment-channel.md" in out


def test_single_env_inbound_line_is_not_redundant(monkeypatch, capsys):
    out = _render([row("omni@zds-mind")], monkeypatch, capsys)
    assert "from zds-mind" in out
    assert "from zds-mind 1" not in out


def test_unreadable_registry_says_so_rather_than_reporting_zero_peers(
        monkeypatch, capsys):
    out = _render([], monkeypatch, capsys, PEER_REGISTRY="{}")
    assert "registry unreadable" in out
    assert "0 peers" not in out


def test_json_mode_is_machine_readable(monkeypatch, capsys):
    out = _render([row("omni@zds-mind")], monkeypatch, capsys, PEER_JSON="1")
    data = json.loads(out)
    assert data["inbound_total"] == 1
    assert data["self_backend"] == "own-cloud"
    assert sorted(data["peers"]) == ["claude-mind", "local", "zds-mind"]
