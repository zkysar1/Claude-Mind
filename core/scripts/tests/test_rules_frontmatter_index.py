"""Every rule carries the one line a lean-index runtime shows (rules-loading.md, 2026-08-29).

A Zak-Code Body sees a rule as ``- name: description [path]`` and nothing else unless the
rule is pinned with ``alwaysApply: true`` — then its FULL body rides in the prompt, capped
at 8 KB per file. So: every rule needs a ``description:`` that is an imperative of at most
140 chars (the index truncates past that), and every pinned body must fit its cap or it
is cut mid-sentence in the one place it was meant to be complete.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RULES = sorted((REPO / ".claude" / "rules").glob("*.md"))
INDEX_SUMMARY_CHARS = 140  # zakcode.rules._INDEX_SUMMARY_CHARS
MAX_RULE_FILE_CHARS = 8_192  # zakcode.rules.MAX_RULE_FILE_CHARS


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    """The ``key: value`` lines of a leading ``---`` block (Zak-Code's parser shape) + body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta, "\n".join(lines[end + 1 :]).strip()


@pytest.mark.parametrize("path", RULES, ids=[p.stem for p in RULES])
def test_every_rule_has_a_short_imperative_description(path: Path) -> None:
    meta, _ = _front_matter(path.read_text(encoding="utf-8"))
    description = meta.get("description", "")
    assert description, f"{path.name}: no description: — a lean index shows only the title"
    assert len(description) <= INDEX_SUMMARY_CHARS, (
        f"{path.name}: description is {len(description)} chars; the index truncates past "
        f"{INDEX_SUMMARY_CHARS}"
    )
    assert not re.fullmatch(r"[A-Z][A-Za-z\- ]+", description), (
        f"{path.name}: description reads like a title, not an imperative"
    )


@pytest.mark.parametrize("path", RULES, ids=[p.stem for p in RULES])
def test_a_pinned_rule_body_fits_the_per_file_cap(path: Path) -> None:
    meta, body = _front_matter(path.read_text(encoding="utf-8"))
    if meta.get("alwaysapply", "").lower() not in ("true", "yes", "1"):
        pytest.skip("not pinned")
    assert len(body) <= MAX_RULE_FILE_CHARS, (
        f"{path.name} is pinned (alwaysApply) but its body is {len(body)} chars — over the "
        f"{MAX_RULE_FILE_CHARS} per-file cap it would be cut mid-sentence in the prompt"
    )


def test_the_pinned_set_is_small_enough_to_leave_room_for_the_index() -> None:
    pinned = 0
    for path in RULES:
        meta, body = _front_matter(path.read_text(encoding="utf-8"))
        if meta.get("alwaysapply", "").lower() in ("true", "yes", "1"):
            pinned += min(len(body), MAX_RULE_FILE_CHARS) + len(path.stem) + 5
    # 32 KB total; the 34-line index needs ~8 KB; keep pins under ~25 KB.
    assert pinned <= 25_000, f"pinned bodies total {pinned} chars — the index no longer fits"
