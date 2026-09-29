"""Pin : a worker Body pulls the framework at the claim, before its goal runs.

The worker loop's Phase -0.3 pull (iteration-push.sh --no-push) is a step the
model runs, and Bodies skip it: measured 2026-09-28, one Body claimed a goal on a
checkout 316 commits behind and another ran a 9 h unit on the same stale code.
aspirations-claim.sh now runs the pull itself for a worker (BODY_WM_PATH set),
before the claim call. These tests pin the four properties that fix depends on:

  * it fires for a worker and never for the reducer (no BODY_WM_PATH);
  * nothing it prints reaches STDOUT, which carries the goal JSON every caller
    parses (guard-3189: banner on stderr, payload on stdout);
  * a failing pull never fails the claim (fail-soft);
  * it runs at top level between the query build and the claim call, so it
    precedes the claim, rather than inside _post_claim_effects (which runs after).

Behavioural tests EXTRACT the shell block the script actually runs and execute it
against a stub iteration-push.sh (guard-4323: validate the production code, never
a copy written in the probe).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

SCRIPTS = Path(__file__).resolve().parents[1]
CLAIM_SH = SCRIPTS / "aspirations-claim.sh"
SRC = CLAIM_SH.read_text(encoding="utf-8")

# Any gate line is accepted here on purpose: the behavioural tests below, not
# this pattern, decide whether the gate is right (a mutated gate must fail THEM).
_BLOCK_RE = re.compile(
    r'# ── WORKER PULL AT THE CLAIM \(g-375-66\).*?\n'
    r'(if \[[^\n]*\]; then\n.*?\nfi)\n',
    re.S,
)
_CLAIM_CALL = 'RESPONSE="$(rt_call POST /v1/aspirations/claim'
_QUERY_START = 'QUERY="id=${GOAL_ID}&agent=${AGENT}"'


def _shipped_block() -> str:
    m = _BLOCK_RE.search(SRC)
    assert m, (
        "the worker-pull block in aspirations-claim.sh has moved or changed shape; "
        "these tests run the SHIPPED block and cannot fall back to a copy."
    )
    return m.group(1)


def _run(tmp_path: Path, body_wm: str | None, stub_rc: int = 0) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the shipped block with CORE_ROOT pointing at a stub iteration-push.sh."""
    core = tmp_path / "core"
    (core / "scripts").mkdir(parents=True)
    calls = tmp_path / "calls.txt"
    stub = core / "scripts" / "iteration-push.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'echo "STUB-STDOUT line"\n'
        f"exit {stub_rc}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "BODY_WM_PATH"}
    env["CORE_ROOT"] = str(core)
    if body_wm is not None:
        env["BODY_WM_PATH"] = body_wm
    script = 'CORE_ROOT="$CORE_ROOT"\n' + _shipped_block() + '\necho "BLOCK-RC=$?" >&2\n'
    proc = subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    return proc, calls


def test_worker_pulls_with_no_push(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, body_wm="/some/body-wm.yaml")
    assert proc.returncode == 0
    assert calls.exists(), "a worker claim must run the pull"
    assert calls.read_text(encoding="utf-8").split() == ["--no-push"]


def test_pull_output_never_reaches_stdout(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, body_wm="/some/body-wm.yaml")
    assert proc.stdout == "", "stdout carries the goal JSON; the pull must print to stderr"
    assert "STUB-STDOUT line" in proc.stderr  # positive control: the stub did print


def test_reducer_never_pulls_here(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, body_wm=None)
    assert proc.returncode == 0
    assert not calls.exists(), "without BODY_WM_PATH (the reducer) the block must not run"


def test_empty_body_wm_path_is_not_a_worker(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, body_wm="")
    assert proc.returncode == 0
    assert not calls.exists()


def test_failed_pull_is_soft(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, body_wm="/some/body-wm.yaml", stub_rc=7)
    assert calls.exists()
    assert "BLOCK-RC=0" in proc.stderr, "a failing pull must not fail the claim"


def test_block_runs_before_the_claim_at_top_level() -> None:
    block_at = SRC.index(_shipped_block())
    query_at = SRC.index(_QUERY_START)
    claim_at = SRC.index(_CLAIM_CALL)
    # _post_claim_effects is DEFINED above the query build and runs after the claim,
    # so "before the claim call" alone would not rule out a block inside it.
    # SRC.index finds the FIRST claim call; the second one is the retry after a
    # daemon autospawn, which runs after the first and so after the pull too.
    assert query_at < block_at < claim_at
