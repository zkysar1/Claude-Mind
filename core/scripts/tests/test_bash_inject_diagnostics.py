"""test_bash_inject_diagnostics.py — per-(SID, reason) binding-miss diagnostics.

Paired with g-115-1187 (Apply: bash-agent-inject diagnostic enrichment), which
closed the g-115-1146 gap: the prior per-SID one-shot sentinel suppressed every
binding-miss after the first for a SID, so mid-session injection drops produced
ZERO additional log lines and were invisible.

Two surfaces under test:

  1. `_session_binding.resolve_binding_with_diagnostics` — returns
     (binding, reason). Verifies each distinct failure mode reports its own
     reason string instead of a single opaque miss.

  2. `bash-agent-inject.py` helpers — `_log_binding_miss_once` keys the sentinel
     on (sid, reason) so distinct modes log separately; `_mark_binding_resolved`
     + `_binding_was_resolved` enable mid-session-disappeared classification.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _session_binding import resolve_binding_with_diagnostics  # noqa: E402


# bash-agent-inject.py is hyphenated — load it via importlib for its helpers.
def _load_inject_module():
    spec = importlib.util.spec_from_file_location(
        "bash_agent_inject", CORE_SCRIPTS / "bash-agent-inject.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INJECT = _load_inject_module()


def _make_binding(root: Path, agent: str, sid: str, *,
                  yaml_text: str | None = None,
                  with_conf: bool = True) -> None:
    """Materialize agents/<agent>/sessions/<sid>/binding.yaml + local-paths.conf."""
    sess = root / "agents" / agent / "sessions" / sid
    sess.mkdir(parents=True, exist_ok=True)
    if yaml_text is None:
        yaml_text = (
            f"session_id: {sid}\n"
            f"agent: {agent}\n"
            f"mode: autonomous\n"
            f"started_at: '2026-05-23T00:00:00'\n"
        )
    (sess / "binding.yaml").write_text(yaml_text, encoding="utf-8")
    if with_conf:
        (root / "agents" / agent / "local-paths.conf").write_text(
            "WORLD_DIR=/tmp/w\nMETA_DIR=/tmp/m\n", encoding="utf-8"
        )


# ── Surface 1: resolve_binding_with_diagnostics failure modes ──────────────

def test_resolve_success(tmp_path):
    sid = "11111111-1111-1111-1111-111111111111"
    _make_binding(tmp_path, "alpha", sid)
    binding, reason = resolve_binding_with_diagnostics(sid, tmp_path)
    assert binding is not None
    assert binding.agent == "alpha"
    assert reason == ""


def test_reason_invalid_sid(tmp_path):
    binding, reason = resolve_binding_with_diagnostics("../etc/passwd", tmp_path)
    assert binding is None
    assert reason == "invalid-sid"


def test_reason_binding_yaml_missing(tmp_path):
    # agents/ exists with an agent dir, but no binding.yaml for this SID.
    (tmp_path / "agents" / "alpha").mkdir(parents=True)
    binding, reason = resolve_binding_with_diagnostics(
        "22222222-2222-2222-2222-222222222222", tmp_path
    )
    assert binding is None
    assert reason == "binding-yaml-missing"


def test_reason_agents_root_missing(tmp_path):
    # No agents/ dir at all.
    binding, reason = resolve_binding_with_diagnostics(
        "33333333-3333-3333-3333-333333333333", tmp_path
    )
    assert binding is None
    assert reason == "agents-root-missing"


def test_reason_session_id_mismatch(tmp_path):
    sid = "44444444-4444-4444-4444-444444444444"
    # binding.yaml carries a DIFFERENT session_id than the dir name.
    _make_binding(
        tmp_path, "alpha", sid,
        yaml_text=("session_id: WRONG-SID\nagent: alpha\nmode: autonomous\n"),
    )
    binding, reason = resolve_binding_with_diagnostics(sid, tmp_path)
    assert binding is None
    assert reason == "session-id-mismatch"


def test_reason_agent_name_mismatch(tmp_path):
    sid = "55555555-5555-5555-5555-555555555555"
    # binding.yaml agent != parent dir name (dir is alpha, file says bravo).
    _make_binding(
        tmp_path, "alpha", sid,
        yaml_text=(f"session_id: {sid}\nagent: bravo\nmode: autonomous\n"),
    )
    binding, reason = resolve_binding_with_diagnostics(sid, tmp_path)
    assert binding is None
    assert reason == "agent-name-mismatch"


def test_reason_local_paths_conf_missing(tmp_path):
    sid = "66666666-6666-6666-6666-666666666666"
    # Valid binding.yaml but no local-paths.conf in the agent dir.
    _make_binding(tmp_path, "alpha", sid, with_conf=False)
    binding, reason = resolve_binding_with_diagnostics(sid, tmp_path)
    assert binding is None
    assert reason == "local-paths-conf-missing"


def test_reason_binding_yaml_parse_failed(tmp_path):
    sid = "77777777-7777-7777-7777-777777777777"
    # Malformed YAML that parses to a non-dict (a bare scalar).
    _make_binding(tmp_path, "alpha", sid, yaml_text="just a string, not a mapping\n")
    binding, reason = resolve_binding_with_diagnostics(sid, tmp_path)
    assert binding is None
    assert reason == "binding-yaml-parse-failed"


# ── Surface 2: bash-agent-inject sentinel + memo helpers ───────────────────

def test_sanitize_reason():
    assert INJECT._sanitize_reason("binding-yaml-missing") == "binding-yaml-missing"
    assert INJECT._sanitize_reason("session-id-mismatch") == "session-id-mismatch"
    # Path-traversal / unexpected chars collapse to '-'.
    assert "/" not in INJECT._sanitize_reason("../../etc")
    assert "\\" not in INJECT._sanitize_reason("a\\b")
    assert INJECT._sanitize_reason("") == "unknown"
    assert INJECT._sanitize_reason(None) == "unknown"


def test_per_sid_reason_sentinel_distinct(tmp_path):
    """Two DISTINCT reasons for the same SID each log once (the  fix).

    Before the fix, the second reason would be suppressed by the per-SID
    sentinel set on the first miss.
    """
    sid = "88888888-8888-8888-8888-888888888888"
    log = tmp_path / "core" / "logs" / "bash-inject-misses.jsonl"

    INJECT._log_binding_miss_once(sid, tmp_path, "binding-yaml-missing")
    INJECT._log_binding_miss_once(sid, tmp_path, "binding-yaml-mid-session-disappeared")
    # Re-fire the FIRST reason — must be suppressed (already logged once).
    INJECT._log_binding_miss_once(sid, tmp_path, "binding-yaml-missing")

    lines = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    reasons = sorted(r["reason"] for r in lines)
    assert reasons == ["binding-yaml-mid-session-disappeared", "binding-yaml-missing"]
    # Two sentinels exist — one per (sid, reason).
    sentinels = sorted(p.name for p in (tmp_path / "core" / "logs" / "bash-inject-sentinels").iterdir())
    assert sentinels == [
        f"{sid}__binding-yaml-mid-session-disappeared",
        f"{sid}__binding-yaml-missing",
    ]


def test_resolved_memo_roundtrip(tmp_path):
    sid = "99999999-9999-9999-9999-999999999999"
    assert INJECT._binding_was_resolved(sid, tmp_path) is False
    INJECT._mark_binding_resolved(sid, tmp_path)
    assert INJECT._binding_was_resolved(sid, tmp_path) is True


def test_memo_rejects_traversal_sid(tmp_path):
    # A path-traversal SID must not create a memo outside the resolved dir.
    INJECT._mark_binding_resolved("../escape", tmp_path)
    assert INJECT._binding_was_resolved("../escape", tmp_path) is False
    resolved_dir = tmp_path / "core" / "logs" / "bash-inject-resolved"
    # Either the dir was never created, or it holds nothing for the bad SID.
    if resolved_dir.exists():
        assert list(resolved_dir.iterdir()) == []


# ── Surface 3: fail-safe agent reuse on transient resolve failure () ──

def test_resolved_memo_stores_agent_name(tmp_path):
    """The memo now persists the agent NAME so a TRANSIENT resolve failure can
    fail SAFE by reusing it instead of fail-open to the first agent."""
    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert INJECT._last_resolved_agent(sid, tmp_path) == ""
    INJECT._mark_binding_resolved(sid, tmp_path, "bravo")
    assert INJECT._last_resolved_agent(sid, tmp_path) == "bravo"
    # Presence memo still works for mid-session-disappeared classification.
    assert INJECT._binding_was_resolved(sid, tmp_path) is True


def test_last_resolved_agent_empty_without_name(tmp_path):
    """A legacy zero-byte / nameless memo yields '' — no fail-safe, fail open."""
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    INJECT._mark_binding_resolved(sid, tmp_path)  # 2-arg call: empty name
    assert INJECT._binding_was_resolved(sid, tmp_path) is True
    assert INJECT._last_resolved_agent(sid, tmp_path) == ""


def test_last_resolved_agent_rejects_injection(tmp_path):
    """A tampered memo must never inject shell metacharacters into the export
    clause — only simple agent-name tokens are returned."""
    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    d = tmp_path / "core" / "logs" / "bash-inject-resolved"
    d.mkdir(parents=True, exist_ok=True)
    (d / sid).write_text("alpha; rm -rf /", encoding="utf-8")
    assert INJECT._last_resolved_agent(sid, tmp_path) == ""
    # Traversal SID is rejected outright (never reads outside the resolved dir).
    assert INJECT._last_resolved_agent("../escape", tmp_path) == ""
