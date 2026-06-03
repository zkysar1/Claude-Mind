"""B7: _journal_prepare derives the canonical journal_file from date + agent
(single source of truth), eliminating the validation_failed rejections caused
by callers hand-building the wrong path (charlie passed the INDEX path
`agents/charlie/journal.jsonl` 4x instead of the dated ENTRY path).

Pure unit test of the prepare hook — no running daemon needed."""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "mind_api" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import store_registry as sr  # noqa: E402


class _FakeCtx:
    """Minimal ctx exposing ctx.paths.agent.name (the only field used)."""
    def __init__(self, agent_name: str):
        self.paths = type("P", (), {"agent": Path("/root/agents") / agent_name})()


def _prep(agent, rec):
    sr._journal_prepare(_FakeCtx(agent), rec)
    return rec


def test_index_path_is_rewritten_to_dated_entry():
    # The exact B7 incident: charlie passed the index path.
    rec = _prep("charlie", {"session": 9, "date": "2026-05-18",
                            "journal_file": "agents/charlie/journal.jsonl"})
    assert rec["journal_file"] == "charlie/journal/2026/05/2026-05-18.md"


def test_absent_journal_file_is_derived_from_date():
    rec = _prep("alpha", {"session": 3, "date": "2026-05-15"})
    assert rec["journal_file"] == "alpha/journal/2026/05/2026-05-15.md"


def test_canonical_caller_value_is_preserved():
    canonical = "bravo/journal/2026/05/2026-05-18.md"
    rec = _prep("bravo", {"session": 1, "date": "2026-05-18",
                          "journal_file": canonical})
    assert rec["journal_file"] == canonical  # SSOT respected, untouched


def test_wrong_agent_prefix_is_corrected_to_bound_agent():
    # A path naming a DIFFERENT agent must not pass through (the misleading
    # "expected alpha/journal" cross-agent leak class, ). The regex
    # is bound-agent-scoped, so a bravo path under a charlie ctx is rederived.
    rec = _prep("charlie", {"session": 2, "date": "2026-05-18",
                            "journal_file": "bravo/journal/2026/05/2026-05-18.md"})
    assert rec["journal_file"] == "charlie/journal/2026/05/2026-05-18.md"


def test_malformed_date_falls_back_to_today_keeps_canonical_shape():
    # journal_file stays canonical-shaped (so it is not the error surfaced);
    # _journal_validate independently rejects the bad date downstream.
    rec = _prep("delta", {"session": 5, "date": "not-a-date"})
    today = date.today()
    assert rec["journal_file"] == (
        f"delta/journal/{today.year:04d}/{today.month:02d}/{today.isoformat()}.md")


def test_derived_value_passes_validate():
    # End-to-end: prepare then validate must not raise (the bug was a
    # validation_failed 400 at exactly this seam).
    rec = {"session": 7, "date": "2026-05-20",
           "journal_file": "agents/echo/journal.jsonl"}
    ctx = _FakeCtx("echo")
    sr._journal_prepare(ctx, rec)
    sr._journal_validate(ctx, rec, skip_id=False)  # raises on failure


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
