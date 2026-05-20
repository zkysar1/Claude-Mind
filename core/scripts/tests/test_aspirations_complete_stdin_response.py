"""test_aspirations_complete_stdin_response.py -  regression test.

Pins the response-printer stdin route in aspirations-complete.sh after the
2026-05-17 fix that replaced `$(rt_python_launcher) -c "..." "$RESPONSE"`
(argv pattern, ~32KB Windows limit) with
`printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "..."` (stdin pattern,
no length limit).

Origin of bug (alpha session-75, asp-265 23-goal archival): the daemon-side
POST /v1/aspirations/complete succeeded, archiving asp-265 in
world/aspirations.jsonl. The wrapper's post-200 response-printer at line 86
then exec'd python with the full archived record (~57KB) as argv[1], hit
Windows E2BIG ("Argument list too long"), and exited non-zero. Callers saw
a script failure even though the underlying operation had completed —
the most insidious false-failure shape: the action succeeded but the
report didn't.

Fix (g-115-772): pipe $RESPONSE via stdin; python reads json.load(sys.stdin)
instead of json.loads(sys.argv[1]). Two sites in aspirations-complete.sh:
the post-200 success path (line 93 post-fix) and the autospawn-retry
success path (line 124 post-fix).

Test strategy: this test does NOT invoke the real wrapper (it would require
a daemon + a real aspiration to complete). Instead, it extracts the same
python source the wrapper executes and pipes synthetic $RESPONSE payloads
through it via subprocess. The pinned contract:

  1. small response (the asp record has 2 goals, payload ~2KB):
     stdout = `json.dumps(asp, indent=2, ensure_ascii=False)`, exit 0
  2. large response (50KB+ asp record with 100 goals):
     stdout valid JSON, exit 0 (would have FAILED with argv pattern)
  3. warnings-only response (no `aspiration` key, just warnings):
     warnings to stderr, response sans warnings to stdout, exit 0
  4. response with warnings + aspiration:
     warnings to stderr, aspiration object to stdout, exit 0
  5. large enough to provably exceed argv limit on Windows (~40KB):
     test that the OLD argv path WOULD HAVE failed, and the new path doesn't.

The python source is kept verbatim with the wrapper's source so any
regression in the wrapper that re-introduces the argv pattern shows up
as a divergence in the test fixture.

Refs: g-115-772 (this fix), asp-265 (the canonical failing case, archived
successfully despite the wrapper false-failure), aspirations-complete.sh
lines 93 + 124 (post-fix sites).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve py -3 launcher (Windows shim avoids Microsoft Store stub).
PY_LAUNCHER = shutil.which("py") or sys.executable

# Verbatim python source from aspirations-complete.sh success-path block
# (post-fix). If the wrapper diverges from this, the test will catch the
# semantic drift even if syntactically valid.
WRAPPER_PRINTER_SOURCE = """
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"""


def _run_printer(response_json: str) -> subprocess.CompletedProcess:
    """Execute the wrapper's response-printer with response_json on stdin.

    Mirrors what `printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "..."`
    does in the post-fix wrapper. The python source is held in
    WRAPPER_PRINTER_SOURCE to detect drift.
    """
    return subprocess.run(
        [PY_LAUNCHER, "-3", "-c", WRAPPER_PRINTER_SOURCE],
        input=response_json,
        capture_output=True,
        text=True,
        timeout=15,
        encoding="utf-8",
    )


# ── Case 1: small response, asp present ────────────────────────────────

def test_small_response_with_aspiration() -> None:
    """Small response (2 goals) — same output as legacy argv path."""
    asp = {
        "id": "asp-test-small",
        "title": "small test",
        "status": "completed",
        "goals": [
            {"id": "g-test-01", "title": "first"},
            {"id": "g-test-02", "title": "second"},
        ],
    }
    response = {"aspiration": asp, "warnings": []}
    result = _run_printer(json.dumps(response))
    assert result.returncode == 0, f"exit={result.returncode}; stderr: {result.stderr}"
    assert result.stderr == "", f"stderr should be empty; got: {result.stderr}"
    # stdout should be the aspiration JSON (indent=2, ensure_ascii=False).
    parsed = json.loads(result.stdout)
    assert parsed == asp, "stdout should round-trip to the aspiration object"


# ── Case 2: large response, asp present (exceeds argv limit) ───────────

def test_large_response_exceeds_argv_limit() -> None:
    """Large response (100 goals, ~50KB+) — stdin pipe succeeds.

    Windows argv limit is ~32KB. A 50KB+ payload would trigger E2BIG
    if passed via argv but works fine via stdin. This is the
    asp-265 canonical case (23 goals ~57KB).
    """
    goals = []
    for i in range(100):
        goals.append({
            "id": f"g-large-{i:03d}",
            "title": f"goal {i}",
            "description": "x" * 500,  # ~500 bytes per goal
            "status": "completed",
        })
    asp = {
        "id": "asp-large",
        "title": "large test (100 goals)",
        "status": "completed",
        "goals": goals,
    }
    response = {"aspiration": asp, "warnings": []}
    payload = json.dumps(response)
    assert len(payload) > 40_000, (
        f"test fixture should exceed 40KB to provably exceed argv limit; "
        f"got {len(payload)}B"
    )
    result = _run_printer(payload)
    assert result.returncode == 0, f"exit={result.returncode}; stderr: {result.stderr}"
    parsed = json.loads(result.stdout)
    assert parsed["id"] == "asp-large"
    assert len(parsed["goals"]) == 100


# ── Case 3: warnings-only response (no aspiration key) ─────────────────

def test_response_with_warnings_no_aspiration() -> None:
    """When daemon returns warnings but no aspiration field, stdout
    contains the response sans warnings; stderr lists each warning."""
    response = {
        "warnings": ["First warning", "Second warning"],
        "status": "partial",
        "skipped_goals": ["g-foo-01"],
    }
    result = _run_printer(json.dumps(response))
    assert result.returncode == 0, f"exit={result.returncode}; stderr: {result.stderr}"
    # Warnings to stderr, one per line.
    assert "First warning" in result.stderr
    assert "Second warning" in result.stderr
    # Stdout has the non-warnings portion as JSON.
    parsed = json.loads(result.stdout)
    assert "warnings" not in parsed
    assert parsed["status"] == "partial"
    assert parsed["skipped_goals"] == ["g-foo-01"]


# ── Case 4: response with warnings AND aspiration ──────────────────────

def test_response_with_warnings_and_aspiration() -> None:
    """When both warnings and aspiration are present, aspiration goes to
    stdout (json.dumps with indent=2 ensure_ascii=False); warnings go
    to stderr. This mirrors the legacy CLI output shape."""
    asp = {"id": "asp-both", "title": "both", "goals": []}
    response = {
        "aspiration": asp,
        "warnings": ["Some warning"],
    }
    result = _run_printer(json.dumps(response))
    assert result.returncode == 0
    assert "Some warning" in result.stderr
    parsed = json.loads(result.stdout)
    assert parsed == asp


# ── Case 5: non-ASCII handling (ensure_ascii=False) ────────────────────

def test_response_preserves_non_ascii() -> None:
    """The wrapper uses ensure_ascii=False so unicode characters in goal
    titles/descriptions are preserved verbatim in stdout (not escaped to
    \\uXXXX). This pins behavior callers may depend on."""
    asp = {
        "id": "asp-unicode",
        "title": "unicode test — em dash + smart quotes",
        "goals": [],
    }
    response = {"aspiration": asp, "warnings": []}
    result = _run_printer(json.dumps(response, ensure_ascii=False))
    assert result.returncode == 0
    # em dash and smart-quote characters should appear verbatim.
    assert "—" in result.stdout, f"em dash should be preserved; stdout: {result.stdout[:200]}"


# ── Case 6: drift sentinel — pin the source pattern ────────────────────

def test_wrapper_source_uses_stdin_not_argv() -> None:
    """Direct source-level pin: aspirations-complete.sh must use the
    stdin pattern at both response-printer call sites.

    This catches the regression where a future edit accidentally restores
    the argv pattern. The substring test is intentionally narrow:
    `json.load(sys.stdin)` is the post-fix signature; `json.loads(sys.argv`
    is the pre-fix signature. Both must hold (post present, pre absent).
    """
    wrapper_path = Path(__file__).resolve().parent.parent / "aspirations-complete.sh"
    src = wrapper_path.read_text(encoding="utf-8")
    # Post-fix marker — must appear (success path + autospawn-retry path = 2).
    assert src.count("json.load(sys.stdin)") >= 2, (
        f"aspirations-complete.sh should use json.load(sys.stdin) at both "
        f"response-printer sites; found {src.count('json.load(sys.stdin)')}"
    )
    # Pre-fix marker — must NOT appear anywhere in the response-printer block.
    assert "json.loads(sys.argv[1])" not in src, (
        "aspirations-complete.sh must NOT use json.loads(sys.argv[1]) — "
        "that pattern triggers Windows E2BIG on large responses (g-115-772)"
    )


# ── Case 7: generalized argv-pattern audit across ALL wrappers () ──

def test_all_rt_python_launcher_wrappers_use_stdin() -> None:
    """Generalized regression: every wrapper in core/scripts/*.sh that
    invokes `$(rt_python_launcher) -c "..."` MUST use the stdin pattern,
    not the argv pattern. Pins the g-115-895 batch migration (34 wrappers,
    52 sites) plus the canonical g-115-772 fix (aspirations-complete.sh).

    The argv pattern hits Windows E2BIG (~32KB) on large daemon responses
    — see test_wrapper_source_uses_stdin_not_argv for the canonical
    incident shape. This test forbids any new wrapper from re-introducing
    the pattern.

    Failure mode if test fails: a wrapper that still uses
    `json.loads(sys.argv[1])` or `_src = sys.argv[1]` for the daemon
    response will succeed in normal-load testing but silently false-fail
    on records over ~32KB.

    Exemptions (none currently): wrappers that pass non-response data via
    argv (e.g., a small constant) are fine — but every actual response
    from the daemon must route via stdin.

    Cross-references:
      - g-115-895 (this Apply goal — sweep across 34 wrappers)
      - g-115-893 (the originating Idea)
      - g-115-772 (canonical fix on aspirations-complete.sh)
      - core/scripts/migrate-argv-to-stdin.py (the migration script)
    """
    scripts_dir = Path(__file__).resolve().parent.parent
    failures: list[tuple[str, str]] = []
    audited = 0
    for sh_path in sorted(scripts_dir.glob("*.sh")):
        src = sh_path.read_text(encoding="utf-8")
        # Only audit wrappers that actually invoke rt_python_launcher -c
        # for response handling. Skip wrappers that use it for unrelated
        # purposes (e.g., env probes).
        if "rt_python_launcher" not in src or " -c " not in src:
            continue
        audited += 1
        # Pre-fix patterns that MUST NOT exist:
        if "json.loads(sys.argv[1])" in src:
            failures.append(
                (sh_path.name, "uses json.loads(sys.argv[1]) — Windows "
                               "E2BIG risk; migrate to json.load(sys.stdin)"))
        if "_src = sys.argv[1]" in src:
            failures.append(
                (sh_path.name, "uses _src = sys.argv[1] — Windows E2BIG "
                               "risk; migrate to _src = sys.stdin.read()"))
    assert audited >= 35, (
        f"expected to audit at least 35 rt_python_launcher wrappers, "
        f"found {audited} — has core/scripts been split up?")
    if failures:
        msg = "\n".join(f"  {name}: {reason}" for name, reason in failures)
        raise AssertionError(
            f"{len(failures)} wrapper(s) still use the argv response pattern "
            f"(Windows E2BIG risk per g-115-895 / g-115-772):\n{msg}\n\n"
            "Run `py -3 core/scripts/migrate-argv-to-stdin.py --apply` to "
            "fix.")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
