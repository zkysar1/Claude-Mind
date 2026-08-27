"""Pin the defining contract of core/scripts/replay-stamp-verify.sh (gap-145, ).

The script's whole reason to exist is that its verification read is PER-ID. A future
edit that "simplifies" it to one batched `--replay-candidates` call would still pass a
casual smoke test on an EMPTY input and would fail INVERTED on real input: the batched
surface excludes records whose next_review_date is in the future
(mind_api/src/world/pipeline.py, `if review_date > today: continue`), and this operation
always sets next_review_date to today+INTERVAL — so after a fully successful stamp every
record it just wrote is excluded, and the read-back reports ZERO VERIFIED on total
success. Measured live 2026-08-25: candidate count 659 -> 658, the freshly-stamped record
absent from the batched surface while an independent per-id read confirmed the write.

That inversion is what these tests exist to keep out.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "replay-stamp-verify.sh"


def _body():
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_verification_never_invokes_the_batched_surface():
    """The killer regression: verifying via --replay-candidates instead of --id.

    Matches an INVOCATION (`pipeline-read.sh ... --replay-candidates`), not the mere
    token — the flag name appears legitimately in the header comment and in the emitted
    `verification` field, and a bare substring test would forbid documenting the hazard.
    """
    offenders = [
        line.strip()
        for line in _body().splitlines()
        if re.search(r"pipeline-read\.sh[^\n#]*--replay-candidates", line)
    ]
    assert not offenders, (
        "verification must be per-id; found a batched --replay-candidates invocation, "
        "which reports zero verified after a SUCCESSFUL stamp: " + repr(offenders)
    )


def test_verification_reads_back_per_id():
    assert re.search(r"pipeline-read\.sh\"?\s+--id", _body()), (
        "expected a per-id read-back (`pipeline-read.sh --id`) in the verify path"
    )


def test_not_set_e_because_it_reports_on_failure_paths():
    """guard-614: a wrapper emitting structured output on every exit path must not use
    `set -e`, or it dies mid-report on the first non-zero daemon call and a per-record
    failure list becomes silence."""
    body = _body()
    assert re.search(r"^set -uo pipefail", body, re.M), "expected `set -uo pipefail`"
    assert not re.search(r"^set -e(?![a-z])", body, re.M), "must NOT use `set -e` (guard-614)"


def _run(*args, stdin=None):
    return subprocess.run(
        # guard-580/guard-581: resolved BASH, and .as_posix() — bash silently
        # strips the backslashes of a str(WindowsPath), so str(SCRIPT) would make
        # every one of these tests fail on a Windows box for a reason unrelated to
        # what they assert.
        [BASH, SCRIPT.as_posix(), *args],
        capture_output=True, text=True, input=stdin, timeout=120,
    )


def test_no_ids_is_a_refusal_not_a_silent_success():
    r = _run()
    assert r.returncode == 2, f"expected rc=2 on no ids, got {r.returncode}"
    assert "no_ids" in (r.stderr + r.stdout)


def test_unknown_flag_is_refused():
    r = _run("--not-a-flag")
    assert r.returncode == 2, f"expected rc=2 on unknown flag, got {r.returncode}"


def test_missing_record_fails_loudly_with_structured_output():
    """A bogus id must produce rc!=0 AND a parseable per-record failure entry — the
    failure path is exactly where a `set -e` script would have gone silent."""
    r = _run("no-such-record-for-this-test-xyz")
    assert r.returncode != 0, "a missing record must not exit 0"
    assert '"ok": false' in r.stdout or '"ok":false' in r.stdout, r.stdout[:400]
    assert "pre-read" in r.stdout, r.stdout[:400]


def test_interval_days_as_last_arg_does_not_hang():
    """F-001 (fresh-eyes, ): `shift 2` with $#=1 cannot shift, so $# never
    decremented and the arg loop spun forever emitting NOTHING. Measured rc=124 under
    `timeout` with zero bytes on both streams — the worst shape for a wrapper whose
    contract is to report on every exit path. The 7 tests already in this file all
    passed while that hang was live; none of them touched the arg parser."""
    r = _run("--interval-days")          # subprocess timeout=120 turns a regression
    assert r.returncode == 2, f"expected rc=2, got {r.returncode}"   # into an error
    assert "missing_value" in (r.stderr + r.stdout)


def test_non_numeric_interval_is_refused_not_silently_written():
    """F-002 (fresh-eyes, ): an unvalidated value made the date computation
    raise, left NEXT_REVIEW empty, and the script wrote next_review_date:"" onto real
    records AT rc=0. An empty next_review_date is FALSY in the replay_candidates filter
    (`if next_review:`), so such a record is never excluded and resurfaces as a candidate
    every cycle — the exact corruption this wrapper exists to prevent, self-inflicted."""
    r = _run("--interval-days", "abc", "--dry-run", "any-id")
    assert r.returncode == 2, f"expected rc=2, got {r.returncode}"
    assert "bad_interval" in (r.stderr + r.stdout)
    assert '"next_review_date": ""' not in r.stdout


def test_summary_is_ndjson_not_a_regex_reparse():
    r"""F-003: the summary re-derived object boundaries from concatenated JSON with
    `re.findall(r"\{.*?\}(?=\s*\{|\s*$)")`. It happened to work on every observed
    shape, but a record whose string value contains "} {" would split wrong — and the
    producer already knows the boundaries, so re-deriving them buys nothing."""
    body = _body()
    assert "re.findall" not in body, "summary must not re-derive JSON object boundaries"
    assert 'printf \'%s\\n\' "${RESULTS[@]:-}"' in body, "records must be emitted one per line"


if __name__ == "__main__":  # allow direct execution alongside pytest
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
