"""test_rb_entry_type_taxonomy_sync.py --  (follow-up to ).

g-306-11 added the optional reasoning-bank `entry_type` taxonomy across the
dual-mirror RB validators: the CLI (core/scripts/reasoning-bank.py) and the
daemon (mind_api/src/store_registry.py). The two copies of
RB_VALID_ENTRY_TYPES are hand-kept "verbatim-in-sync" by a comment on BOTH
sides (reasoning-bank.py:91 / store_registry.py:214) -- i.e. the sync is
honor-system. A ONE-SIDED edit to the valid-set (add a type to the daemon but
forget the CLI, or vice-versa) would diverge SILENTLY: a record the CLI accepts
the daemon would 400 (or vice-versa), with no test catching the drift.

This pins the invariant: the two RB_VALID_ENTRY_TYPES sets MUST be identical.
A one-sided edit now fails this test. A legitimate taxonomy expansion (adding a
new entry_type) stays green only if BOTH sides are touched -- exactly the
discipline the verbatim-in-sync comments ask for, now enforced rather than
trusted.

Cross-references:
  - g-306-11 -- added entry_type + the dual-mirror validators
  - test_rb_validate_list_field_rejection.py -- pins the DAEMON validator behavior
  - test_applies_to_required.py -- pins the CLI validator + the importlib
    env-stash pattern reused here
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Daemon side -- a plain package import, no module-load side effects.
from mind_api.src.store_registry import (  # noqa: E402
    RB_VALID_ENTRY_TYPES as DAEMON_ENTRY_TYPES,
)

# CLI side -- core/scripts/reasoning-bank.py is hyphen-named, so it cannot be a
# normal import; load it via importlib (the pattern proven in
# test_applies_to_required.py / test_tag_normalization.py). Importing the module
# bootstraps WORLD/path resolution, so stash MIND_WORLD/MIND_AGENT FIRST and
# restore IMMEDIATELY after the load (guard-588: a module-level os.environ
# mutation must not leak into other tests in the same pytest session -- pytest
# imports every test module during collection before running any test).
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="rb-entry-type-sync-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RB_PATH = CORE_SCRIPTS / "reasoning-bank.py"
_spec = importlib.util.spec_from_file_location("reasoning_bank", _RB_PATH)
_rb_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rb_mod)
CLI_ENTRY_TYPES = _rb_mod.RB_VALID_ENTRY_TYPES

if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def test_rb_valid_entry_types_cli_daemon_set_equality():
    """The CLI and daemon RB_VALID_ENTRY_TYPES sets MUST be identical.

    This is the regression the verbatim-in-sync comments ask for but could not
    enforce. A one-sided edit (add/remove a type on one side only) makes this
    fail; a coordinated two-sided taxonomy change keeps it green.
    """
    assert CLI_ENTRY_TYPES == DAEMON_ENTRY_TYPES, (
        "RB_VALID_ENTRY_TYPES diverged between the CLI "
        f"(core/scripts/reasoning-bank.py: {sorted(CLI_ENTRY_TYPES)}) and the "
        f"daemon (mind_api/src/store_registry.py: {sorted(DAEMON_ENTRY_TYPES)}). "
        "These two copies are hand-kept verbatim-in-sync; edit BOTH sides when "
        "changing the entry_type taxonomy."
    )


def test_rb_valid_entry_types_wellformed():
    """Both sets are non-empty sets of strings -- a sanity guard so the equality
    assertion above cannot pass vacuously on two empty sets."""
    for name, s in (("CLI", CLI_ENTRY_TYPES), ("daemon", DAEMON_ENTRY_TYPES)):
        assert isinstance(s, (set, frozenset)) and s, f"{name} set empty or not a set"
        assert all(isinstance(x, str) for x in s), f"{name} has non-str members: {s!r}"


if __name__ == "__main__":
    test_rb_valid_entry_types_cli_daemon_set_equality()
    test_rb_valid_entry_types_wellformed()
    print(
        "PASS: RB_VALID_ENTRY_TYPES CLI/daemon set-equality + wellformed "
        f"(both = {sorted(CLI_ENTRY_TYPES)})"
    )
