"""The observability half of : iteration-push's diagnostics must be
READABLE, and iteration-close must not silence them. (g-115-4502.)

WHY THIS FILE EXISTS. iteration-push.sh's log() is the only voice the push path
has. It carries the stranded-depth alarm, the integrate-defer line, and the
repeated-refusal escalation — all written to be read IN-TURN by the loop LLM.
The mechanism is two sinks and one call site, and NOTHING tested any of them:
a future edit that adds a `2>/dev/null` at the call site, or drops the file tee,
restores the exact pre-fix condition (an alarm written for a reader who cannot
reach it) while every existing test still passes. That silence is what g-115-4484
existed to remove, which is what makes this gap worth more than a routine one.

THE GOAL'S SPEC WAS STALE AND THIS FILE DELIBERATELY DIVERGES FROM IT.
g-115-4502 was filed 2026-08-01 describing a capture-to-tempfile mechanism:
iteration-close catching stderr, re-emitting it, and appending it under a dated
header to core/logs/iteration-push-stderr.log, plus an mktemp-unavailable
fallback. That mechanism was **added and then deliberately removed** — the call
site's own comment records why: it "duplicated that persistence into a second
file, giving a reader two places to look for one stream." Measured 2026-08-28:
`grep -rn iteration-push-stderr core/` returns nothing and the named log file
does not exist, so cases 2 and 3 of the filed spec are untestable by
construction. Writing them against a mechanism that no longer exists would have
produced a green suite proving nothing. The INTENT — this path can regress
silently — is unchanged and is what is tested here, against the design actually
shipping:

    log() -> stderr                                  (the in-turn channel)
          -> $ITERATION_PUSH_LOG_FILE or $GITDIR/iteration-push.log  (persistence)
    iteration-close.sh:3454 -> unredirected, so it INHERITS that stderr

THE CALL-SITE TEST ANCHORS ON THE SCRIPT NAME, NOT ON THE REDIRECT (guard-2354).
The mutation this test must catch ADDS a redirect to the call line. If the
extraction anchored on the line's full text, that mutation would break the
ANCHOR, the helper would raise "call site not found" first, and the behavioural
assertion would never execute — a red for the wrong reason and a vacuous proof.
Anchoring on the invariant `iteration-push.sh` token means the mutant is still
found and is judged on the thing under test.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _bash_helpers import BASH  # noqa: E402

PUSH_SH = CORE_SCRIPTS / "iteration-push.sh"
CLOSE_SH = CORE_SCRIPTS / "iteration-close.sh"
MARKER = "[iteration-push]"


# --------------------------------------------------------------------------- #
# helpers — a hermetic repo, never the real one, never the network
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


def _must(repo: Path, *args: str) -> str:
    r = _git(repo, *args)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


def _repo_with_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, text=True, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   capture_output=True, text=True, check=True)
    _must(clone, "config", "user.name", "test-clone")
    _must(clone, "config", "user.email", "clone@test.local")
    (clone / "base.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _must(clone, "add", "base.txt")
    _must(clone, "commit", "-q", "-m", "base", "--", "base.txt")
    _must(clone, "push", "-q", "-u", "origin", "main")
    return clone


def _run_push(repo: Path, *flags: str, env_extra=None) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [BASH, str(PUSH_SH), "--repo", str(repo),
         "--min-commits", "1", "--fetch-interval-min", "0", *flags],
        capture_output=True, text=True, timeout=120, env=env,
    )


# --------------------------------------------------------------------------- #
# sink 1 — the in-turn channel the loop LLM reads
# --------------------------------------------------------------------------- #
def test_diagnostics_go_to_stderr_and_not_stdout(tmp_path):
    """The channel matters, not just the text.

    A caller that captures only stdout, or a `2>&1` that folds the two together,
    both LOOK like the diagnostics arrived. Asserting presence on stderr AND
    absence on stdout is what distinguishes "reached the reader" from "reached
    a buffer" (rb-968: stderr log lines contaminating stdout is a real shape in
    this repo, and this is the same seam from the other side).
    """
    repo = _repo_with_origin(tmp_path)
    r = _run_push(repo)
    assert MARKER in r.stderr, f"no diagnostics on stderr; stderr={r.stderr!r}"
    assert MARKER not in r.stdout, (
        f"diagnostics leaked onto stdout — a caller parsing stdout would ingest "
        f"log lines as data; stdout={r.stdout!r}"
    )


# --------------------------------------------------------------------------- #
# sink 2 — persistence, at the SOURCE so every call site gets it
# --------------------------------------------------------------------------- #
def test_every_stderr_line_is_also_persisted_to_the_log_file(tmp_path):
    """Both sinks carry the SAME lines, and the file adds a timestamp.

    Checking only that the file is non-empty would pass on a tee that dropped
    most lines, which is the regression that matters: the alarms are individual
    lines, so a partial tee loses exactly the one nobody was watching for.
    """
    repo = _repo_with_origin(tmp_path)
    logf = tmp_path / "push.log"
    r = _run_push(repo, env_extra={"ITERATION_PUSH_LOG_FILE": str(logf)})

    assert logf.exists(), "ITERATION_PUSH_LOG_FILE was set but no file was written"
    file_lines = [ln for ln in logf.read_text(encoding="utf-8").splitlines() if ln.strip()]
    err_lines = [ln for ln in r.stderr.splitlines() if ln.startswith(MARKER)]
    assert err_lines, f"no diagnostics emitted at all; stderr={r.stderr!r}"

    # Every stderr line has a file twin. The file strips the "[iteration-push] "
    # prefix and prepends an ISO timestamp, so compare on the message body.
    stamped = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} ")
    bodies = set()
    for ln in file_lines:
        assert stamped.match(ln), f"log line is missing its ISO timestamp: {ln!r}"
        bodies.add(ln.split(" ", 1)[1])
    for ln in err_lines:
        msg = ln[len(MARKER):].strip()
        assert msg in bodies, (
            f"line reached stderr but NOT the log file: {msg!r}\n"
            f"file bodies={sorted(bodies)!r}"
        )
    assert len(file_lines) == len(err_lines), (
        f"sink line counts diverge: stderr={len(err_lines)} file={len(file_lines)}"
    )


def test_log_file_defaults_into_the_repo_gitdir_when_unset(tmp_path):
    """With no override the persistence still happens, under the repo's .git.

    This is the sink every real call site actually uses — the override above
    exists for tests. If only the override were covered, the shipped path would
    be the uncovered one.
    """
    repo = _repo_with_origin(tmp_path)
    default_log = repo / ".git" / "iteration-push.log"
    before = default_log.read_text(encoding="utf-8") if default_log.exists() else ""
    r = _run_push(repo)
    assert MARKER in r.stderr
    assert default_log.exists(), (
        f"no default log at {default_log}; persistence is not happening for "
        f"callers that set no override"
    )
    assert len(default_log.read_text(encoding="utf-8")) > len(before), (
        "default log exists but this run appended nothing to it"
    )


# --------------------------------------------------------------------------- #
# the wiring — the half that can regress in silence
# --------------------------------------------------------------------------- #
def _iteration_close_push_invocations() -> list:
    """Every real (non-comment) invocation of iteration-push.sh in iteration-close.

    Anchored on the SCRIPT NAME, which is invariant under the mutation this
    guards against (adding a redirect). guard-2354: the anchor must not contain
    the thing under test, or a mutation breaks the extraction and the real
    assertion never runs.
    """
    out = []
    for raw in CLOSE_SH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if "iteration-push.sh" in line and re.search(r"\bbash\b", line):
            out.append(line)
    return out


def test_iteration_close_invokes_the_push_unredirected(tmp_path):
    """iteration-close must NOT swallow iteration-push's stderr.

    This is the actual g-115-4484 regression: the call site previously discarded
    stderr, so the alarms could never reach anyone. The call site's own comment
    now says "DELIBERATELY UNREDIRECTED" and warns against re-adding either a
    `2>/dev/null` or a plain `2>>log`. Nothing enforced that until this test.
    """
    calls = _iteration_close_push_invocations()
    assert calls, (
        "no bash invocation of iteration-push.sh found in iteration-close.sh — "
        "the extraction anchor has drifted; fix the anchor, do not delete the test"
    )
    # Anti-vacuity: a second call site added later must fail loudly rather than
    # ride along unchecked, since this test would otherwise only cover the first.
    assert len(calls) == 1, (
        f"expected exactly 1 invocation, found {len(calls)}: {calls!r}. "
        f"A new call site needs its own redirect assertion."
    )
    for line in calls:
        assert not re.search(r"2\s*>", line), (
            f"iteration-close redirects iteration-push's stderr, silencing the "
            f"stranded-depth and integrate-defer alarms that exist to be read "
            f"in-turn: {line!r}"
        )
        assert not re.search(r"&\s*>", line), f"stderr folded via &>: {line!r}"
