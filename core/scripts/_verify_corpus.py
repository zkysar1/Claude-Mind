"""Single source for the verify-learning check corpus.

Since 2026-08-18 (g-115-6689) the 2,235 evidence checks live in
`core/config/verify-learning-checks.jsonl`, not inline in
`.claude/skills/verify-learning/SKILL.md`. Every audit that used to do

    VERIFY_LEARNING.read_text(encoding="utf-8").splitlines()

now sees a 175-line skill and silently reports ~zero. That is worse than an
error: `check-verify-learning-citation-drift.py` went from correctly FAILING on
5 missing records to vacuously PASSING on 0 checked.

`corpus_text()` returns the corpus **byte-identical to the pre-cutover
SKILL.md** — the registry stores every line of the original file verbatim, not
just the check lines, so regeneration reproduces the whole document. Two
consequences worth knowing before you use it:

  * LINE NUMBERS ARE PRESERVED. A consumer reporting "line 6380" still means
    line 6380 of the corpus, exactly as it did before the cutover. This is why
    the fix at each call site is a one-line swap and nothing downstream moves.
  * It is NOT the current SKILL.md. Anything asking "what does the skill file
    say today" must keep reading the file; this is for the CHECK CORPUS only.

Fallback is deliberate and LOUD. A deployment mid-promotion may have the thin
SKILL.md without the registry yet, and hard-failing every audit there would be
worse than degrading. But a silent fallback to a source that yields ~0 is the
exact failure this module exists to remove, so the fallback warns on stderr and
`corpus_source()` reports which source answered — let a caller's positive
control see it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY = PROJECT_ROOT / "core" / "config" / "verify-learning-checks.jsonl"
SKILL_MD = PROJECT_ROOT / ".claude" / "skills" / "verify-learning" / "SKILL.md"
_ENGINE = PROJECT_ROOT / "core" / "scripts" / "verify-check-registry.py"

_cache: tuple[str, str] | None = None


def _load_engine():
    # The engine's filename is hyphenated, so it is not importable by name.
    # Routing through it (rather than re-decoding the JSONL here) keeps ONE
    # definition of the record schema — the slim-key mapping lives in exactly
    # one place and this module cannot drift from it.
    spec = importlib.util.spec_from_file_location("_vcr_engine", str(_ENGINE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _resolve() -> tuple[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    if REGISTRY.is_file():
        try:
            eng = _load_engine()
            _cache = (eng.regenerate(eng.load_registry(REGISTRY)), "registry")
            return _cache
        except Exception as exc:  # noqa: BLE001 — degrade loudly, never silently
            print(f"[_verify_corpus] registry unreadable ({exc.__class__.__name__}: {exc}); "
                  f"falling back to SKILL.md — counts below will be ~0 and mean NOTHING",
                  file=sys.stderr)
    else:
        print(f"[_verify_corpus] {REGISTRY} absent; falling back to SKILL.md — if this "
              f"deployment is post-cutover, counts below will be ~0 and mean NOTHING",
              file=sys.stderr)
    _cache = (SKILL_MD.read_text(encoding="utf-8", errors="replace"), "skill_md")
    return _cache


def corpus_text() -> str:
    """The check corpus, byte-identical to the pre-cutover SKILL.md."""
    return _resolve()[0]


def corpus_lines() -> list[str]:
    return corpus_text().splitlines()


def corpus_source() -> str:
    """'registry' or 'skill_md' — report it when a count looks suspicious."""
    return _resolve()[1]
