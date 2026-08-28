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
import os
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


# --- integration: the OTHER TWO parsers, exercised END TO END () ---
#
# test_all_three_env_parsers_share_the_case_insensitive_class above asserts the
# `_paths.py` and `storage_backend.py` copies by SOURCE TEXT. That catches a
# narrowing edit to the pattern and nothing else: a parser can carry the right
# regex and still fail to surface the key — a later guard, a different match
# group index, an ordering change, or a caller that re-filters. These two tests
# close that gap by CALLING each parser with a lowercase key and asserting the
# key comes out the far end.
#
# Secrets hygiene is the design constraint here, not an afterthought (guard-1461):
# both parsers produce a container holding every key on the box (`_env_local_vars`
# and, for the second test, the real `os.environ`), and pytest rewrites a failing
# assertion by serializing its operands. So `assert key in mod._env_local_vars`
# would dump the whole dict into the failure output.
#
# SUBSCRIPTING THE ONE KEY IS NECESSARY BUT NOT SUFFICIENT, which is the part
# that is easy to get wrong and was measured here rather than assumed. pytest
# also appends a "where" clause reprs the RECEIVER of any call left inside the
# asserted expression, so `assert os.environ.get(k) == v` still fails with
# `where None = <bound method Mapping.get of environ({...})>` — every variable on
# the box. Both tests below therefore bind the scalar to a local FIRST and assert
# on the local, leaving the container out of the expression entirely. Verified
# both ways through mutation-proof-test.sh --junit-xml: the pre-fix red message
# carried the `bound method ... environ(` repr, the post-fix one ends at
# `assert None == \'synthetic-value\'`.
#
# The fixtures are synthetic and no real `.env.local` is read.


def _load_paths_copy_with_env_local(tmp_path, env_local_text):
    """Import a FRESH copy of `_paths.py` whose PROJECT_ROOT resolves to tmp_path.

    `_paths` derives PROJECT_ROOT from its own ``__file__`` (SCRIPT_DIR.parent.
    parent) and runs ``_env_local_vars = _read_env_local()`` at IMPORT time, so
    neither monkeypatching the constant nor ``importlib.reload`` reaches the
    import-time path: reload re-derives PROJECT_ROOT from the real location.
    Copying the module into a tmp tree and loading it from there is what makes
    the import-time assignment observable, which is the half of path (1) a
    direct ``_read_env_local()`` call cannot cover.

    Deliberately NOT registered in ``sys.modules``: guard-1165 (module-level
    sys.modules stubs poison every test module collected afterward in the shared
    pytest process) and its refinement that ``importlib.reload`` in a finally is
    not a real restore. Nothing here to restore, because nothing is installed.
    This also is not the guard-603 shape — the module loaded is the REAL source,
    not a fake that must stub every symbol the production code imports.
    """
    import importlib.util
    import shutil

    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPT_DIR / "_paths.py", scripts / "_paths.py")
    (tmp_path / ".env.local").write_text(env_local_text, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "_paths_g115_3380_probe", scripts / "_paths.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("key", [
    "view_cage_online",     # all lowercase
    "SMTPserver",           # mixed case — the real shape that was skipped
    "_leading_underscore",  # legal in shell/dotenv
])
def test_paths_env_local_surfaces_non_uppercase_key_at_import(
        tmp_path, monkeypatch, key):
    """Path (1): lowercase key in .env.local -> _read_env_local -> _env_local_vars.

    Asserts the module-level dict that ENVIRONMENT_ID / COMMONS_POLICY are read
    from, so this covers the import-time assignment and not merely the function.
    """
    # The fresh copy resolves AGENT_DIR against tmp_path, which has no agents/;
    # unset the ambient binding so the import does not fall through to scanning
    # real agent dirs. conftest's _restore_env_per_test puts it back regardless.
    monkeypatch.delenv("MIND_AGENT", raising=False)

    mod = _load_paths_copy_with_env_local(
        tmp_path,
        "# a comment line\n"
        f"{key}=synthetic-value\n"
        "MIND_UPPER_KEY=synthetic-upper\n",
    )

    assert mod.PROJECT_ROOT == tmp_path, (
        "the copied module did not re-root; this test would otherwise be "
        "reading the REAL .env.local (guard-1461)"
    )
    # guard-1461, and the intermediate local is LOAD-BEARING, not style. Calling
    # .get() INSIDE the asserted expression is not enough: pytest's assertion
    # rewriting appends a "where" clause that reprs the RECEIVER, so
    # `assert mod._env_local_vars.get(k) == v` fails with
    # `where None = <bound method Mapping.get of {...whole dict...}>`. Binding the
    # scalar first leaves the container out of the expression entirely, so only
    # `None` and the expected value can reach the failure output. Measured on this
    # very test via mutation-proof-test.sh --junit-xml ().
    got = mod._env_local_vars.get(key)
    assert got == "synthetic-value", (
        f"{key!r} was silently SKIPPED by _paths._read_env_local() — an "
        f"uppercase-only key-name class omits the line rather than erroring, so "
        f"ENVIRONMENT_ID/COMMONS_POLICY and every other world-contract var read "
        f"through this dict go missing without a diagnostic. See g-115-3372."
    )
    # No-regression direction (rb-401): widening must not disturb uppercase.
    got_upper = mod._env_local_vars.get("MIND_UPPER_KEY")
    assert got_upper == "synthetic-upper"


def test_storage_backend_bootstrap_exports_non_uppercase_key(tmp_path, monkeypatch):
    """Path (2): lowercase key in .env.local -> storage_backend -> os.environ.

    `_bootstrap_env_defaults` is a no-op under pytest unless
    ENV_BOOTSTRAP_ALLOW_PYTEST is set, and takes `root` as a test seam — both
    are load-bearing here: without the seam this would read the real
    `.env.local` and side-load production config into the suite process.
    """
    import storage_backend

    key = "view_cage_online"

    # setenv-then-delenv so a teardown restore is ALWAYS registered. A bare
    # monkeypatch.delenv on an ABSENT var registers nothing, and the function
    # under test sets the key via os.environ.setdefault — which would then LEAK
    # into every test collected afterward in the shared pytest process
    # (guard-1165's in-process pollution; the same reasoning as
    # test_telemetry_append_log_integrity.py::_delenv_with_restore). Popping
    # rather than snapshotting is also guard-2334's remedy: a value merely
    # captured by a restore fixture is inherited from the launching shell.
    monkeypatch.setenv(key, "_g115_3380_placeholder_")
    monkeypatch.delenv(key)
    monkeypatch.setenv("ENV_BOOTSTRAP_ALLOW_PYTEST", "1")
    # guard-955: pin the backend so nothing here can resolve a real one.
    monkeypatch.setenv("STORAGE_BACKEND", "local")

    (tmp_path / ".env.local").write_text(
        "# a comment line\n"
        f"{key}=synthetic-value\n",
        encoding="utf-8",
    )

    storage_backend._bootstrap_env_defaults(root=tmp_path)

    # guard-1461 names "os.environ as a whole" alongside _env_local_vars, and
    # THIS is the assertion where it bites hardest: the receiver here is the real
    # process environment. Bind the scalar BEFORE the assert — with the call
    # inside the expression, pytest's rewriting emits
    # `where None = <bound method Mapping.get of environ({...})>` and dumps every
    # variable on the box into the failure output (and thence the CI log and the
    # session transcript). Observed verbatim in this test's own mutation-proof
    # run before the local was introduced.
    got = os.environ.get(key)
    assert got == "synthetic-value", (
        f"{key!r} never reached os.environ — storage_backend's .env.local "
        f"parser silently SKIPPED the line, so every consumer sees an unset "
        f"var that is plainly present in the file. See g-115-3372."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
