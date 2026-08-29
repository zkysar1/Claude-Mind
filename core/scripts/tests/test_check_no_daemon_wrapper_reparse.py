"""Coverage for check-no-daemon-wrapper-reparse.sh — 3rd of the 4 pre-commit gates
with no test anywhere (g-115-4399; re-measured 2026-08-29: 4 uncovered of 13).

A gate's job is to REFUSE, so one that silently stops refusing emits exactly what a
clean repo emits. The induced-violation cases are the load-bearing half; the
clean/exempt cases pass against a totally dead gate and cannot substitute for them
(measured on the two sibling gates: under full sabotage the clean cases stayed
green). See guard-5501, rb-6205.

FIXTURE NAMING: the planted wrapper targets a neutral `fixture-store.jsonl`, never a
real store name. The gate only requires the constructed path to END in .jsonl/.json/
.yaml, so this satisfies condition 3 identically -- while keeping the fixture from
tripping the store-write guard, which pattern-matches real store names inside inline
Python and cannot tell a test fixture from an actual write. Cheaper than an override,
and the override is logged.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from _bash_helpers import BASH

GATE = Path(__file__).resolve().parents[1] / "check-no-daemon-wrapper-reparse.sh"

# All 3 conditions the gate's docstring requires, together: rt_call (it IS a daemon
# wrapper) + an inline py -3 -c block + a Path(...WORLD_PATH...) landing on .jsonl.
VIOLATION = '''#!/usr/bin/env bash
rt_call "/v1/fixture/read?counts=1"
py -3 -c "
from pathlib import Path
import os
p = Path(os.environ['WORLD_PATH']) / 'fixture-store.jsonl'
print(sum(1 for _ in p.open()))
"
'''


def _repo(tmp_path: Path, name: str, body: str) -> Path:
    """A self-contained git repo carrying the gate plus one wrapper under test.

    The gate takes NO path argument -- it builds its list from
    `git ls-files 'core/scripts/*.sh'` -- so the fixture must be a real repo with the
    file tracked, and the gate is copied in so PROJECT_ROOT resolves inside it.
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / name).write_text(body, encoding="utf-8")
    (scripts / GATE.name).write_text(GATE.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _run(repo: Path, *args: str):
    return subprocess.run(
        [BASH, str(repo / "core" / "scripts" / GATE.name), *args],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )


def test_induced_violation_is_refused(tmp_path):
    """POSITIVE CONTROL -- the canonical  shape: a wrapper calls rt_call and
    then re-derives the same counts from the daemon's own data file."""
    r = _run(_repo(tmp_path, "fixture-read.sh", VIOLATION), "--audit")
    assert r.returncode != 0, f"gate did NOT refuse a 3-of-3 violation\n{r.stdout}\n{r.stderr}"


def test_audit_mode_does_not_strand_its_exit_code(tmp_path):
    """THE  SHAPE, asserted explicitly. That incident put a real defect INTO a
    gate: the exit code was stranded in the wrong branch, so --audit printed a complete
    findings report and still returned 0. A gate that reports and returns 0 is worse
    than no gate -- it manufactures evidence of cleanliness."""
    r = _run(_repo(tmp_path, "fixture-read.sh", VIOLATION), "--audit")
    assert r.returncode != 0
    assert r.stdout.strip() or r.stderr.strip(), "refused but reported nothing"


def test_rt_call_without_inline_reader_is_allowed(tmp_path):
    """Conditions 1+2 without 3 -- a legitimate response-shape transform, which the
    gate's own docstring says is fine. Flagging these makes it noisy and it gets
    disabled, which is how gates die in practice."""
    body = ('#!/usr/bin/env bash\nrt_call "/v1/fixture/read"\n'
            'py -3 -c "import json,sys; print(json.load(sys.stdin))"\n')
    assert _run(_repo(tmp_path, "ok-transform.sh", body), "--audit").returncode == 0


def test_documented_exemption_marker_suppresses(tmp_path):
    """The escape hatch the gate documents. If this regresses the exemption is a lie
    and every legitimately-exempt wrapper blocks commits."""
    body = VIOLATION + "\n# daemon-wrapper-reparse-exempt: covered by regression test, root cause filed\n"
    assert _run(_repo(tmp_path, "exempted.sh", body), "--audit").returncode == 0
