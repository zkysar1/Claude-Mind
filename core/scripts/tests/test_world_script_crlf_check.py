"""Tests for core/scripts/world-script-crlf-check.py ().

THE CENTREPIECE IS THE RED ARM, NOT THE GREEN ONE. On the live tree this
detector reports 0 offenders across ~800 files, and a detector that can only
ever say zero reports exactly the same thing as a detector that is broken. So
`test_bash_actually_refuses_the_crlf_fixture` pins the GROUND TRUTH the
predicate is supposed to track (bash exits rc=2 on a CRLF script), and the
RED-arm tests pin that the scanner flags precisely those files. Evaluating one
arm alone cannot distinguish a gate that refuses from a gate that cannot fire
(guard-2590), and "the script exists" is not evidence it runs -- which is why
the goal's own acceptance criterion demanded a deliberately CRLF-ed fixture.

THE LONE-CR CASE IS DELIBERATE AND IS NOT AN EDGE CASE. The predicate is CR
PRESENCE, not CRLF presence: a bare b"\\r" mid-file is just as fatal to bash as
a whole-file CRLF, and a CRLF-only predicate silently passes it.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_world_script_crlf_check.py -q
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from _bash_helpers import BASH

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_MOD_PATH = os.path.join(_SCRIPTS, "world-script-crlf-check.py")

_spec = importlib.util.spec_from_file_location("world_script_crlf_check", _MOD_PATH)
crlf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crlf)

CLEAN = b"#!/usr/bin/env bash\nset -euo pipefail\necho ok\n"
WHOLE_CRLF = b"#!/usr/bin/env bash\r\nset -euo pipefail\r\necho ok\r\n"
LONE_CR = b"#!/usr/bin/env bash\nset -euo pipefail\recho ok\n"


def _fixture_dir(tmp_path, **files):
    d = tmp_path / "scripts"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_bytes(body)
    return d


def _scan(d):
    return crlf.scan_root(d, "fixture")


def _offenders(res):
    return {os.path.basename(o["path"]) for o in res["offenders"]}


# --------------------------------------------------------------------------
# ground truth -- what the predicate is supposed to track
# --------------------------------------------------------------------------

def test_bash_actually_refuses_the_crlf_fixture(tmp_path):
    """The RED arm is only meaningful if bash really does reject these bytes.

    This reproduces the 2026-08-22 incident signature exactly: bash reads
    `set -euo pipefail\\r` and dies with `set: pipefail: invalid option name`,
    rc=2 -- the failure that killed the fleet's outbound email transport
    silently, because callers that swallow stderr see only a non-zero rc.
    """
    d = _fixture_dir(tmp_path, clean=CLEAN, crlf=WHOLE_CRLF)
    ok = subprocess.run([BASH, (d / "clean").as_posix()], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == "ok"

    bad = subprocess.run([BASH, (d / "crlf").as_posix()], capture_output=True, text=True)
    assert bad.returncode != 0, "a CRLF script must not run clean -- fixture is wrong"
    assert "pipefail" in bad.stderr


# --------------------------------------------------------------------------
# RED arm
# --------------------------------------------------------------------------

def test_whole_file_crlf_is_flagged(tmp_path):
    res = _scan(_fixture_dir(tmp_path, **{"a.sh": WHOLE_CRLF}))
    assert _offenders(res) == {"a.sh"}
    o = res["offenders"][0]
    assert o["cr"] == 3 and o["crlf"] == 3 and o["whole_file"] is True


def test_lone_cr_is_flagged_even_though_it_is_not_crlf(tmp_path):
    """A CRLF-only predicate passes this file. CR presence is the predicate."""
    res = _scan(_fixture_dir(tmp_path, **{"b.sh": LONE_CR}))
    assert _offenders(res) == {"b.sh"}
    o = res["offenders"][0]
    assert o["cr"] == 1 and o["crlf"] == 0 and o["whole_file"] is False


def test_mixed_tree_flags_only_the_offenders(tmp_path):
    res = _scan(_fixture_dir(
        tmp_path, **{"ok.sh": CLEAN, "bad.sh": WHOLE_CRLF, "sneaky.sh": LONE_CR}))
    assert _offenders(res) == {"bad.sh", "sneaky.sh"}
    assert res["scanned"] == 3 and res["failed"] == []


def test_nested_directories_are_scanned(tmp_path):
    d = _fixture_dir(tmp_path, **{"top.sh": CLEAN})
    (d / "sub").mkdir()
    (d / "sub" / "deep.sh").write_bytes(WHOLE_CRLF)
    res = _scan(d)
    assert _offenders(res) == {"deep.sh"} and res["scanned"] == 2


# --------------------------------------------------------------------------
# GREEN arm + the non-.sh boundary
# --------------------------------------------------------------------------

def test_clean_tree_is_clean(tmp_path):
    res = _scan(_fixture_dir(tmp_path, **{"a.sh": CLEAN, "b.sh": CLEAN}))
    assert res["offenders"] == [] and res["scanned"] == 2 and res["failed"] == []


def test_non_sh_files_are_out_of_scope(tmp_path):
    """Python tolerates CRLF, bash does not -- the scope is *.sh deliberately.

    world/scripts/check-grant-constant-agreement.py is the live example: it
    carried CRLF until g-363-95 rewrote it (LF since 2026-08-24) and was
    correctly NOT an offender in EITHER state -- the scope is the extension,
    not the bytes, so this test does not depend on that file's current
    contents.
    """
    res = _scan(_fixture_dir(tmp_path, **{"a.py": WHOLE_CRLF, "b.json": WHOLE_CRLF}))
    assert res["offenders"] == [] and res["scanned"] == 0


# --------------------------------------------------------------------------
# blindness must never render as clean (guard-4093 / guard-1675 / rb-245)
# --------------------------------------------------------------------------

def test_missing_root_is_a_failure_not_a_clean_zero(tmp_path):
    res = crlf.scan_root(tmp_path / "does-not-exist", "fixture")
    assert res["scanned"] == 0
    assert res["failed"], "an unreadable root must report failure, never a clean zero"


def test_unresolvable_root_is_a_failure(tmp_path):
    res = crlf.scan_root(None, "fixture")
    assert res["failed"] and res["offenders"] == []


# --------------------------------------------------------------------------
# registration -- an always-run lane that is not registered is droppable
# --------------------------------------------------------------------------

def test_lane_is_registered_in_the_always_run_battery():
    spec = importlib.util.spec_from_file_location(
        "precheck_always_run_battery",
        os.path.join(_SCRIPTS, "precheck-always-run-battery.py"))
    bat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bat)
    lane = next((l for l in bat.LANES if l["name"] == "world-script-crlf-check"), None)
    assert lane is not None, "lane absent from LANES -- it would never be dispatched"
    assert lane["script"] == "world-script-crlf-check.sh"
    assert lane["apply_flag"] is False, "report-only by contract -- never hand it --apply"
    assert "offenders" in lane["finds"]["lists"], (
        "offenders must be a `lists` find, or a RED scan reports as a clean lane")


def test_meter_classifies_the_lane_as_always_run():
    """A lane absent from sweep_tier() hits the WARN-default `medium` and becomes
    droppable in a tight zone -- the g-115-3124 drift class."""
    src = open(os.path.join(_SCRIPTS, "aspirations-precheck-budget-meter.sh"),
               encoding="utf-8").read()
    arm = next(l for l in src.splitlines() if "tree-debt-gate|" in l and "|" in l)
    assert "world-script-crlf-check" in arm


def test_cli_emits_parseable_json_and_never_blocks(tmp_path):
    """Exit code is always 0: the loop must never block on a detector."""
    d = _fixture_dir(tmp_path, **{"bad.sh": WHOLE_CRLF})
    r = subprocess.run([sys.executable, _MOD_PATH, "--root", str(d)],
                       capture_output=True, text=True)
    assert r.returncode == 0, "a detector must not block the loop, even when RED"
    payload = json.loads(r.stdout)
    assert payload["offender_count"] == 1
    assert payload["offenders"][0]["path"].endswith("bad.sh")
