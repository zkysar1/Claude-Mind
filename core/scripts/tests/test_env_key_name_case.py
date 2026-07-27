#!/usr/bin/env python3
""" — env.py key-name class must follow the real dotenv rule.

The bug this pins was a SILENT SKIP, which is why it survived: an uppercase-only
key-name class (`[A-Z][A-Z0-9_]*`) does not REJECT a lowercase key, it simply
never matches the line — so `parse_local()` omits the entry, returns no error,
and every caller sees an absent credential that is plainly present in the file.
Nine live credentials were invisible that way in a downstream deployment's
`.env.local` (SMTP settings and several API ids). `verify-before-assuming.md`
rule 4 names the class: a command that fails quietly and returns empty has told
you nothing.

It survived because it had ZERO test coverage — the only `parse_local` reference
anywhere in this suite was `session_telemetry._parse_local_iso`, an unrelated
timestamp helper whose name merely collides. So this file exists as much to
occupy the namespace as to assert the behavior.

Both DIRECTIONS are pinned deliberately (rb-401 — tightening a static pattern
demands re-checking the other direction):
  * lowercase / leading-underscore names parse (the fix), and
  * uppercase names and prose comments behave exactly as before (no regression).
The prose cases matter because widening a comment-matching regex is the obvious
place to introduce a false positive, and the measured census showed none.

Secrets hygiene: every fixture here is synthetic. Nothing reads the real
`.env.local`, and no value is ever printed (guard-1270 / guard-1461).
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import env  # noqa: E402


# --- the fix: lowercase and leading-underscore names must parse --------------

@pytest.mark.parametrize("key", [
    "SMTPserver",        # mixed case — the real shape that was skipped
    "view_cage_online",  # all lowercase
    "ebay_app_id",
    "pin",
    "_leading_underscore",  # legal in shell/dotenv, also rejected by [A-Z]...
])
def test_parse_local_reads_non_uppercase_key(tmp_path, monkeypatch, key):
    """A non-uppercase key must yield a NON-EMPTY value, not a silent omission.

    Asserting on the returned value (not just membership) is the point: the bug
    presented as "the credential is empty", so a membership-only assertion could
    pass while the caller still saw nothing.
    """
    local = tmp_path / ".env.local"
    local.write_text(f"{key}=synthetic-value\n", encoding="utf-8")
    monkeypatch.setattr(env, "LOCAL_PATH", local)

    values = env.parse_local()

    assert key in values, (
        f"{key!r} was silently SKIPPED by parse_local() — the uppercase-only "
        f"key-name class is back. See g-115-3372."
    )
    assert values[key] == "synthetic-value"


def test_parse_example_reads_non_uppercase_commented_key(tmp_path, monkeypatch):
    """The commented form (.env.example) shares the same defect and same fix."""
    example = tmp_path / ".env.example"
    example.write_text(
        "# --- Synthetic Category ---\n"
        "# ayoai_lower_key=  # a lowercase entry\n"
        "# MIND_UPPER_KEY=  # an uppercase entry\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env, "EXAMPLE_PATH", example)

    keys = {e["key"] for e in env.parse_example()}

    assert "ayoai_lower_key" in keys, (
        "commented lowercase key silently skipped — COMMENTED_KEY_RE regressed "
        "to an uppercase-only name class (g-115-3372)"
    )
    assert "MIND_UPPER_KEY" in keys, "uppercase commented key regressed"


# --- no-regression direction (rb-401) ---------------------------------------

def test_uppercase_keys_still_parse(tmp_path, monkeypatch):
    """Widening the name class must not disturb the pre-existing case."""
    local = tmp_path / ".env.local"
    local.write_text(
        "MIND_API_KEY=synthetic-a\n"
        "AWS_ACCESS_KEY_ID=synthetic-b\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env, "LOCAL_PATH", local)

    values = env.parse_local()

    assert values == {"MIND_API_KEY": "synthetic-a",
                      "AWS_ACCESS_KEY_ID": "synthetic-b"}


@pytest.mark.parametrize("line", [
    "# see the docs=here",   # space before `=` — not an identifier
    "# note: x=1",           # `:` breaks the identifier
    "# TODO=fix",            # trailing text after `=` is not a description
    "# todo=fix",            # same, lowercase — the widened class must not help
    "# --- Category ---",    # category header, handled by CATEGORY_RE
])
def test_prose_comments_are_not_read_as_keys(line):
    """Widening a comment regex is where a false positive would come from.

    None of these match under EITHER the old or the new class, because the
    commented form still requires `=` followed only by optional whitespace or a
    `#` description. Pinned so a future "simplification" of that tail cannot
    quietly turn ordinary prose into parsed credential entries.
    """
    assert env.COMMENTED_KEY_RE.match(line) is None


# --- direction guard: pin the fix at the source ------------------------------

def test_key_name_class_is_case_insensitive_at_the_source():
    """Assert the PATTERN, not only the behavior.

    A behavior test can be satisfied by an accidental rewrite; this fails the
    instant either pattern is narrowed back to an uppercase-only class, and
    names the reason in the failure message so the next reader does not have to
    rediscover that the failure mode is silence.
    """
    for name, rx in (("COMMENTED_KEY_RE", env.COMMENTED_KEY_RE),
                     ("ACTIVE_KEY_RE", env.ACTIVE_KEY_RE)):
        assert "A-Za-z_" in rx.pattern, (
            f"{name} no longer accepts lowercase/underscore-leading key names "
            f"({rx.pattern!r}). An uppercase-only class SILENTLY SKIPS such "
            f"keys rather than erroring — see g-115-3372."
        )


def test_all_three_env_parsers_share_the_case_insensitive_class():
    """The defect had THREE copies; the goal named one.

    `env.py` is the copy the originating goal (g-115-3372) described, but a
    participant census found the identical uppercase-only class in
    `_paths.py::_read_env_local` (the import-time world-contract loader) and in
    `storage_backend.py` (which exports .env.local into os.environ). Fixing only
    the named copy would have left the same silent skip live in two parsers —
    so this asserts the SET, by source text, not just the one module.

    Source-text assertion rather than import: `_paths` is the import-cycle-proof
    base and `storage_backend`'s copy lives inside a function-local `import re`
    within a try block, so neither exposes a module-level pattern object to
    compare against.
    """
    bad = r"[A-Z][A-Z0-9_]*)="
    good = r"[A-Za-z_][A-Za-z0-9_]*)="
    for rel in ("core/scripts/env.py",
                "core/scripts/_paths.py",
                "core/scripts/storage_backend.py"):
        src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert good in src, (
            f"{rel} has no case-insensitive .env key-name pattern — the "
            f"g-115-3372 fix is missing or was narrowed back."
        )
        assert bad not in src, (
            f"{rel} still contains an uppercase-only .env key-name class "
            f"({bad!r}). That form SILENTLY SKIPS lowercase keys instead of "
            f"erroring — the g-115-3372 defect. All three copies must stay in "
            f"sync; see the comment at each site."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
