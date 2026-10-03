#!/usr/bin/env python3
"""test_spool_flags_reach_daemon.py -- the daemon's `.env.local` channel knows the
spool flags (g-358-231).

TWO CHANNELS carry a flag to the daemon that performs the writes:

  settings overlay  `daemon_overlay_settings_env` (g-358-183) fills every committed
                    .claude/settings.json env key the spawning shell lacks, at both
                    spawn sites. test_daemon_env_settings_overlay.py pins it against
                    the REAL settings file, so a settings flip is delivered with no
                    per-flag entry.
  .env.local        `_N3_ALLOWED_EXACT` names the keys the daemon self-resolves from
                    .env.local at EVERY start. A reader whose key is missing here
                    never receives a value declared there, and nothing errors
                    (guard-3485).

THIS FILE PINS THE SECOND CHANNEL for the spool flags, plus the reader-side name.
It is not what delivers a settings flip: g-115-9888 found the sibling flag missing
from the daemon BEFORE the overlay existed, when settings env reached only Claude
Code's own Bash calls, and the allowlist entry that fixed it is the part that
remained. Do not add a test here that says "flag in settings => flag in the
allowlist": the overlay makes that false as a delivery requirement.

The source is parsed, not imported: importing `__main__` starts the daemon's
module-level machinery, and this must stay a pure static check (same posture as
test_owncloud_sync_controls.py).
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN_SRC = (ROOT / "mind_api" / "src" / "__main__.py").read_text(encoding="utf-8")

KNOWN_SPOOL_FLAGS = ("UTILIZATION_COUNTERS_SPOOLED", "TREE_RETRIEVAL_SPOOLED")


def _allowed_exact() -> set:
    m = re.search(r"_N3_ALLOWED_EXACT\s*=\s*frozenset\(\{(.*?)\}\)", MAIN_SRC, re.S)
    assert m, "could not locate the _N3_ALLOWED_EXACT literal"
    return set(re.findall(r'"([A-Z0-9_]+)"', m.group(1)))


def test_the_allowlist_literal_parsed_and_is_not_empty():
    # a parse that silently found nothing would make every check below vacuous
    assert len(_allowed_exact()) > 10


@pytest.mark.parametrize("flag", KNOWN_SPOOL_FLAGS)
def test_a_known_spool_flag_is_resolvable_from_env_local_by_the_daemon(flag):
    assert flag in _allowed_exact(), (
        "%s is not in _N3_ALLOWED_EXACT: a value declared in .env.local would be "
        "ignored by the daemon that performs the writes, with no error "
        "(guard-3485)" % flag)


def test_the_tree_lane_reads_the_flag_name_settings_and_the_allowlist_use():
    sys.path.insert(0, str(ROOT / "core" / "scripts"))
    import _tree_retrieval_spool as trs
    assert trs.SPOOLED_ENV == "TREE_RETRIEVAL_SPOOLED", (
        "the reader's env name drifted from the one settings.json and the "
        "allowlist carry: a flip would set a name nothing reads (guard-3485)")
