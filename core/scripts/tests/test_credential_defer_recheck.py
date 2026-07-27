"""test_credential_defer_recheck.py — unit tests for .

Tests the _extract_env_key function from credential-defer-recheck.py in isolation
(no system calls, no daemon dependency). Covers:

  1. Explicit "env-read.sh has KEY" pattern → extracts key
  2. Explicit "env-read has KEY" pattern (no .sh) → extracts key
  3. "credential KEY" pattern → extracts key
  4. "env var KEY" / "env key KEY" patterns → extracts key
  5. Fallback: env/credential indicator word + bare KEY → extracts key
  6. Human-only defer (no env/credential indicator) → returns None
  7. Human-only text with uppercase words but no indicator → returns None
  8. Short key (no underscore) → returns None (not a valid env key shape)
  9. human_blocked: prefix is stripped before extraction
  10. Case-insensitive indicator matching (Credential, ENV-READ, etc.)

Pattern mirrors test_defer_recheck_patterns.py: importlib + sys.path shape
for hyphenated filenames.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_credential_defer_recheck():
    """Load credential-defer-recheck.py via importlib (hyphen-free attr name)."""
    spec = importlib.util.spec_from_file_location(
        "credential_defer_recheck_mod",
        CORE_SCRIPTS / "credential-defer-recheck.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for credential-defer-recheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Explicit extraction patterns
# ---------------------------------------------------------------------------

def test_env_read_sh_has_pattern():
    """'env-read.sh has MIND_API_KEY' → extracts MIND_API_KEY."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: env-read.sh has MIND_API_KEY missing"
    ) == "MIND_API_KEY"


def test_env_read_no_sh_pattern():
    """'env-read has MIND_API_KEY' (no .sh) → extracts MIND_API_KEY."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: credential MIND_API_KEY missing; env-read has MIND_API_KEY exits 1"
    ) == "MIND_API_KEY"


def test_credential_keyword_pattern():
    """'credential MIND_API_KEY' → extracts MIND_API_KEY."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: credential MIND_API_KEY not set"
    ) == "MIND_API_KEY"


def test_env_var_keyword_pattern():
    """'env var AWS_ACCESS_KEY_ID' → extracts AWS_ACCESS_KEY_ID."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: env var AWS_ACCESS_KEY_ID not configured"
    ) == "AWS_ACCESS_KEY_ID"


def test_env_key_keyword_pattern():
    """'env key MY_SERVICE_TOKEN' → extracts MY_SERVICE_TOKEN."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: env key MY_SERVICE_TOKEN absent"
    ) == "MY_SERVICE_TOKEN"


# ---------------------------------------------------------------------------
# Fallback: indicator word + bare key
# ---------------------------------------------------------------------------

def test_fallback_credential_indicator_with_bare_key():
    """Indicator word 'credential' present + bare KEY in text → extracts key."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: credential for OPENAI_API_KEY has not been provisioned yet"
    ) == "OPENAI_API_KEY"


def test_fallback_envread_indicator_with_bare_key():
    """Indicator word 'env-read' (hyphenated) → indicator fires, extracts key."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: env-read probe for DATABASE_URL failed"
    ) == "DATABASE_URL"


def test_fallback_envvar_indicator_with_bare_key():
    """Indicator word 'envvar' → indicator fires via regex, extracts key."""
    mod = _import_credential_defer_recheck()
    # "env var" (with space) triggers the indicator
    assert mod._extract_env_key(
        "human_blocked: env var STRIPE_SECRET_KEY must be set"
    ) == "STRIPE_SECRET_KEY"


# ---------------------------------------------------------------------------
# Human-only defers → must return None
# ---------------------------------------------------------------------------

def test_human_only_approve_click():
    """'user approve-click' — no env/credential indicator → None."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: user approve-click the deployment gate"
    ) is None


def test_human_only_legal_counsel():
    """'legal counsel sign-off' — no indicator → None."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: legal counsel sign-off required before proceeding"
    ) is None


def test_human_only_gui_action():
    """Pure GUI/human action with no env-key indicator → None."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: open the dashboard and click enable in the UI"
    ) is None


def test_human_only_with_uppercase_words_but_no_indicator():
    """Uppercase words present but no env/credential indicator → None.
    Defends against false positives on e.g. project names, acronyms.
    """
    mod = _import_credential_defer_recheck()
    # NOT_THE_KEY looks like an env var but there's no indicator
    assert mod._extract_env_key(
        "human_blocked: user must contact AWS_SUPPORT to open a ticket"
    ) is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_prefix_stripped_before_extraction():
    """human_blocked: prefix is stripped; extraction sees clean text."""
    mod = _import_credential_defer_recheck()
    # If prefix were NOT stripped, 'human_blocked:' itself could confuse
    # the indicator scan. Ensure the first token 'human_blocked:' doesn't
    # match as a key name.
    result = mod._extract_env_key(
        "human_blocked: credential SOME_API_KEY is absent"
    )
    assert result == "SOME_API_KEY"


def test_no_underscore_key_not_extracted():
    """A word without underscore is not a valid env-key even if indicator present."""
    mod = _import_credential_defer_recheck()
    # "APIKEY" has no underscore — _KEY_NAME_RE requires at least one
    assert mod._extract_env_key(
        "human_blocked: credential APIKEY is absent"
    ) is None


def test_case_insensitive_indicator():
    """Indicator matching is case-insensitive (Credential, ENV-READ, etc.)."""
    mod = _import_credential_defer_recheck()
    assert mod._extract_env_key(
        "human_blocked: Credential UPPER_CASE_KEY not found"
    ) == "UPPER_CASE_KEY"
    assert mod._extract_env_key(
        "human_blocked: ENV-READ has ANOTHER_KEY failed"
    ) == "ANOTHER_KEY"


def test_env_read_pattern_takes_priority_over_fallback():
    """Explicit env-read pattern fires before fallback bare-key scan."""
    mod = _import_credential_defer_recheck()
    # If there are two possible keys, the explicit pattern should win
    result = mod._extract_env_key(
        "human_blocked: env-read.sh has FIRST_KEY; also credential SECOND_KEY"
    )
    assert result == "FIRST_KEY"
