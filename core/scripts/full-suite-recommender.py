#!/usr/bin/env python3
"""
full-suite-recommender.py — Advisory full-suite test recommender for Phase-4 close.

Detects code changes (Mind framework + product workspace under AGENT_WRITE_PATH)
and emits a banner recommending the appropriate full-suite test command(s)
BEFORE the LLM claims "All tests pass" in Phase 5 verify.

Posture: advisory, fail-open, ALWAYS exits 0. The banner is the signal; the
LLM is expected to act on it before Phase 5.

Origin: g-115-858 (Idea, surfaced from g-115-744/746 testSymmetry regression
that targeted-only tests missed but full-suite would have caught).

Wiring: invoked from aspirations-execute Phase 4 close (after primary action,
before phase_4_completed_at). See `.claude/skills/aspirations-execute/SKILL.md`.

Companion rule: `.claude/rules/run-full-suite-after-deep-code.md` defines what
"full-suite" means per code area; this script implements the detector + banner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import fnmatch
from datetime import datetime
from pathlib import Path

# --- Paths ------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_HERE))
from _paths import agent_dir as _agent_dir, agents_root as _agents_root, WORLD_DIR  # noqa: E402
from _fileops import acquire_lock, release_lock  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402

# Mind framework areas (relative to PROJECT_ROOT)
#
# : core/scripts and mind_api/src are DIFFERENT SUITES and must stay
# separate prefixes. They were one tuple feeding one bucket, so a mind_api/src
# change was advised to run `pytest core/scripts/tests` — the one suite that
# does not import it. The governing rule corrected exactly this mapping on
# 2026-07-31 (); the rule was fixed and this renderer was not, so the
# superseded guidance kept reaching every agent via the banner.
MIND_PY_PREFIXES = ("core/scripts/",)
MIND_DAEMON_PY_PREFIX = "mind_api/src/"
MIND_PY_TEST_PREFIX = "core/scripts/tests/"
MIND_WRAPPER_PREFIX = "core/scripts/"  # .sh wrappers (production)
MIND_SKILL_PREFIX = ".claude/skills/"
MIND_RULE_PREFIX = ".claude/rules/"
MIND_CONFIG_PREFIX = "core/config/"

# Product workspace (resolved from local-paths.conf at runtime)
def _agent_write_paths() -> list[Path]:
    """Return AGENT_WRITE_PATH root(s) from the bound agent's local-paths.conf,
    or from the first discovered agent dir's conf if MIND_AGENT is unset.
    Returns [] if no agent dir has a local-paths.conf or the conf lacks
    AGENT_WRITE_PATH — a fresh single-agent deployment without a product
    workspace yields [] and the recommender skips product-workspace detection.

    MULTI-ROOT (g-321-05): AGENT_WRITE_PATH may name several roots separated by
    ';' (optionally quoted for bash-source safety). Each is returned as its own
    Path so every product workspace gets change-detected."""
    agent = os.environ.get("MIND_AGENT", "").strip()
    candidates: list[Path] = []
    if agent:
        candidates.append(_agent_dir(agent) / "local-paths.conf")
    else:
        for child in sorted(_agents_root().iterdir()):
            if child.is_dir() and (child / "local-paths.conf").is_file():
                candidates.append(child / "local-paths.conf")
                break
    for conf in candidates:
        if not conf.exists():
            continue
        for ln in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if ln.startswith("AGENT_WRITE_PATH="):
                raw = ln.split("=", 1)[1].strip().strip('"').strip("'")
                return [Path(p.strip()) for p in raw.split(";") if p.strip()]
    return []


# --- Git change detection ---------------------------------------------------
def _git_changed_paths(repo: Path) -> list[str]:
    """Return uncommitted + staged paths relative to repo root. [] on error."""
    if not (repo / ".git").exists():
        return []
    try:
        diff_out = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status_out = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []

    paths: set[str] = set()
    for ln in (diff_out.stdout or "").splitlines():
        p = ln.strip()
        if p:
            paths.add(p)
    # status --porcelain output: "XY path" or "XY path -> renamed-path"
    for ln in (status_out.stdout or "").splitlines():
        s = ln.rstrip("\n")
        if len(s) < 4:
            continue
        rest = s[3:].strip()
        # Handle renames "old -> new" by taking the new path
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1].strip()
        # Strip surrounding quotes git uses for paths with spaces
        if rest.startswith('"') and rest.endswith('"'):
            rest = rest[1:-1]
        if rest:
            paths.add(rest)
    return sorted(paths)


# --- Mind classification ----------------------------------------------------
def _classify_mind(paths: list[str]) -> dict:
    """Bucket Mind-framework paths into suite-recommendation categories."""
    buckets: dict[str, list[str]] = {
        "py_production": [],    # core/scripts/*.py (non-test)
        "py_daemon": [],        # mind_api/src/*.py — SEPARATE SUITE, see below
        "py_test": [],          # core/scripts/tests/*.py
        "sh_wrapper": [],       # core/scripts/*.sh (production wrapper)
        "skill_md": [],         # .claude/skills/*/SKILL.md
        "rule_md": [],          # .claude/rules/*.md
        "config": [],           # core/config/*.yaml, *.md
        "other_mind": [],       # anything else inside Mind framework
    }
    for p in paths:
        # Order matters: tests/ is a prefix of scripts/
        if p.startswith(MIND_PY_TEST_PREFIX) and p.endswith(".py"):
            buckets["py_test"].append(p)
        elif p.startswith(MIND_DAEMON_PY_PREFIX) and p.endswith(".py"):
            buckets["py_daemon"].append(p)
        elif any(p.startswith(pref) for pref in MIND_PY_PREFIXES) and p.endswith(".py"):
            buckets["py_production"].append(p)
        elif p.startswith(MIND_WRAPPER_PREFIX) and p.endswith(".sh"):
            buckets["sh_wrapper"].append(p)
        elif p.startswith(MIND_SKILL_PREFIX) and p.endswith("SKILL.md"):
            buckets["skill_md"].append(p)
        elif p.startswith(MIND_RULE_PREFIX) and p.endswith(".md"):
            buckets["rule_md"].append(p)
        elif p.startswith(MIND_CONFIG_PREFIX) and (p.endswith(".yaml") or p.endswith(".md")):
            buckets["config"].append(p)
        elif (p.startswith("core/") or p.startswith(".claude/")
              or p.startswith("mind_api/")) and not p.endswith(".pyc"):
            buckets["other_mind"].append(p)
    return buckets


# --- Mind suite commands ----------------------------------------------------
# Three invariants, and the rule table in
# .claude/rules/run-full-suite-after-deep-code.md is their source of truth —
# re-read it before changing these strings, never the quotes in a goal record.
# The shared constraint behind all three: every emitted command is COPIED by an
# agent into a shell, so it must be runnable as printed.
#
# 1. ONE ARM PER TREE. core/scripts and mind_api/src have different suites; a
#    single command for both is how a mind_api/src change came to be advised to
#    run a suite that does not import it ().
# 2. EVERY pytest COMMAND CARRIES STORAGE_BACKEND=local. Mandatory on an
#    own-cloud box (guard-955 / rb-2983): without it a tmp-world write collides
#    on the PRODUCTION S3 key. The 2026-07-09 incident truncated
#    world/aspirations.jsonl from 22 aspirations / 1366 goals to one fixture.
#    The banner is where the command gets copied from, so an unpinned command
#    here is the likeliest way that recurs. run-full-suite.sh self-pins and so
#    emits no prefix — the invariant is on commands containing `pytest`.
# 3. run-full-suite.sh IS THE core/scripts RUNNER, not bare pytest: bare pytest
#    silently omits core/tests/gates plus the invisible and domain halves.
MIND_CORE_SUITE_CMD = "bash core/scripts/run-full-suite.sh"
MIND_DAEMON_SUITE_CMD = (
    'STORAGE_BACKEND=local python -m pytest mind_api/tests -q -m "not daemon_integration"'
)


DAEMON_SURFACE_SCRIPT = "core/scripts/mind-api-code-changed.sh"


def _daemon_surface_pathspec() -> tuple[list[str], str]:
    """The daemon-code boundary, SOURCED from its single source of truth.

    Returns (entries, why_unreadable). Exactly one is truthy. The list is NOT
    duplicated here: mind-api-code-changed.sh's own header declares that
    pathspec "IS the daemon-code boundary and the single source of truth for
    it", and re-listing it is what this function exists to avoid (g-115-3587).

    stdout is captured ALONE -- stderr must not be folded into a payload
    another program parses (guard-1963).
    """
    try:
        proc = subprocess.run(
            bash_cmd(DAEMON_SURFACE_SCRIPT, "--print-pathspec"),
            capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - advisory tool, never raise
        return [], f"daemon surface unreadable: {type(exc).__name__}"
    if proc.returncode != 0:
        return [], f"daemon surface unreadable: {DAEMON_SURFACE_SCRIPT} rc={proc.returncode}"
    entries = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not entries:
        # An empty list would silently disable the advisory forever, which is
        # the failure mode this whole goal is about. Treat it as unreadable.
        return [], f"daemon surface unreadable: {DAEMON_SURFACE_SCRIPT} printed 0 entries"
    return entries, ""


def _daemon_surface_hits(paths: list[str]) -> tuple[list[str], str]:
    """Which changed paths fall inside the daemon's import surface.

    Returns (hits, why_warn_anyway). When the surface cannot be read we return
    a reason instead of silently falling back to a narrower literal -- a silent
    fallback to a stale source is exactly what communication-clarity rule 5
    forbids, and mind-api-code-changed.sh itself fails TOWARD restart.
    """
    entries, why = _daemon_surface_pathspec()
    if why:
        return [], why
    hits = []
    for p in paths:
        for e in entries:
            if "*" in e:
                if fnmatch.fnmatch(p, e):
                    hits.append(p)
                    break
            elif p == e or p.startswith(e + "/"):
                hits.append(p)
                break
    return hits, ""


def _mind_recommendations(buckets: dict) -> list[str]:
    """Build the recommendation list from Mind buckets."""
    recs: list[str] = []
    if buckets["py_production"] or buckets["sh_wrapper"]:
        recs.append(MIND_CORE_SUITE_CMD)
    if buckets["py_daemon"]:
        # mind_api/tests folded into run-full-suite's chunked pool 2026-08-20
        # (); the targeted command stays recommended as the FAST
        # direct-coverage arm for mind_api/src changes.
        recs.append(MIND_DAEMON_SUITE_CMD)
    if buckets["py_test"] and not (buckets["py_production"] or buckets["sh_wrapper"]
                                   or buckets["py_daemon"]):
        # Pure test-file changes still benefit from a full run to catch
        # collection/import regressions in the suite as a whole.
        recs.append(MIND_CORE_SUITE_CMD)
    if buckets["skill_md"]:
        # MIRROR THE RULE'S TABLE, DO NOT RE-INVENT IT (invariant note above).
        # This arm used to emit `bash core/scripts/skill-evaluate.sh <skill>` —
        # the ONE command the rule cited in this banner's own header singles out
        # as wrong for this path class. skill-evaluate.sh requires a subcommand
        # (read | report | underperforming | score), so the bare form only ever
        # prints "Error: unknown subcommand", and `score --skill <s> --goal <g>`
        # rates RUNTIME skill-on-goal performance — it can say nothing about a
        # static SKILL.md edit. The gate was therefore recommending precisely
        # what its governing rule forbids (, guard-4123).
        #
        # The checks below are that rule's per-path row UNIONED with
        # guard-4123's ACTION list: the guard carries one the table omits (the
        # guard-329 removed-literal grep), so both sources are mirrored rather
        # than picking whichever was read first.
        #
        # NOT FIXED HERE, deliberately: skill-evaluate.sh's own exit code. The
        # rc=0 claim in guard-4123 was measured and FALSIFIED ( head
        # correction) — the script exits 1, and the rc=0 only reproduces through
        # a trailing pipe, which is guard-1150, not a defect in the script.
        touched = " ".join(sorted(buckets["skill_md"]))
        recs.append(
            f"[manual] Re-read the edited pseudocode in {touched} and confirm it matches intent"
        )
        recs.append("bash core/scripts/domain-leak-check.sh")
        recs.append(
            "[manual] grep .claude/skills/verify-learning/SKILL.md for any literal"
            " you REMOVED (guard-329)"
        )
        recs.append(
            f"py -3 core/scripts/line-class-diff-check.py {touched}"
            "   # bulk prose edits -- per-class set-diff vs HEAD (g-115-7706)"
        )
        recs.append("Consider /verify-learning if you changed skill behavior (not just narrative)")
    if buckets["rule_md"]:
        recs.append("[manual] Re-read .claude/rules/*.md and confirm wording matches intent (no automated check)")
    if buckets["config"]:
        recs.append("[manual] Re-parse touched config via affected consumer scripts; YAML lint if applicable")
    # : the long-lived daemon serves the modules it imported at ITS
    # STARTUP (guard-4804, guard-3373), so an uncommitted daemon-surface edit is
    # live on disk and stale in the daemon. pytest imports fresh from disk and
    # goes green while an end-to-end check through a daemon-routed wrapper
    # silently exercises the OLD module.
    #
    # The surface is SOURCED, never re-listed. MIND_DAEMON_PY_PREFIX above is
    # `mind_api/src/` and covers ONE of the boundary's 19 entries; the other 18
    # are core/scripts modules the daemon imports (aspirations.py, retrieve.py,
    # storage_backend.py, gates/, _*.py, ...). Keying this advisory on that
    # prefix left an edit to any of them silently un-warned -- the exact drift
    # mind-api-code-changed.sh's own header forbids ("do NOT inline this list").
    #
    # No mtime comparison: these paths are uncommitted-or-staged BY CONSTRUCTION
    # (_git_changed_paths diffs against HEAD), which is content evidence.
    # Ordering the edit against daemon start would add an mtime dependency
    # (guard-1504, rb-190) to warn one case less.
    changed = [p for v in buckets.values() for p in v]
    hits, why = _daemon_surface_hits(changed)
    if hits or why:
        detail = ", ".join(sorted(hits)[:3]) if hits else why
        recs.append(
            "bash core/scripts/mind-api-start.sh --restart"
            "   # daemon serves code from ITS startup -- restart before"
            " trusting any end-to-end result through a daemon-routed wrapper"
            f" [{detail}]"
        )
    return recs


# --- Product classification -------------------------------------------------
def _product_repos_with_changes(write_root: Path) -> list[tuple[Path, str, list[str]]]:
    """Return (repo_path, repo_type, changed_paths) for each sibling with changes."""
    if not write_root.exists():
        return []
    out: list[tuple[Path, str, list[str]]] = []
    for child in sorted(write_root.iterdir()):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        changed = _git_changed_paths(child)
        if not changed:
            continue
        repo_type = _detect_repo_type(child)
        out.append((child, repo_type, changed))
    return out


def _detect_repo_type(repo: Path) -> str:
    """Classify a product repo by build-system signal."""
    if (repo / "gradlew").exists() or (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
        return "gradle"
    if (repo / "package.json").exists():
        return "node"
    if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists() or (repo / "Pipfile").exists():
        return "python"
    # Lune detection — tests/ directory plus selene/lune marker is rare;
    # default to "unknown" if nothing matched
    return "unknown"


def _product_recommendation(repo: Path, repo_type: str) -> str:
    """One full-suite command per product repo."""
    if repo_type == "gradle":
        return f"cd {repo} && ./gradlew test --no-daemon"
    if repo_type == "node":
        return f"cd {repo} && npm test"
    if repo_type == "python":
        return f"cd {repo} && python -m pytest tests/ -v"
    return f"[no recognized build system in {repo.name} — confirm full-suite path manually]"


# --- Banner emission --------------------------------------------------------
BANNER_TOP = "═" * 64
BANNER_TITLE = "▸ FULL-SUITE TEST RECOMMENDER (g-115-858)"


def _emit_banner(goal_id: str, mind_buckets: dict, mind_recs: list[str],
                 product_results: list[tuple[Path, str, list[str]]]) -> None:
    print(BANNER_TOP)
    print(BANNER_TITLE)
    print(BANNER_TOP)
    print(f"Goal: {goal_id}")
    print("Rule: .claude/rules/run-full-suite-after-deep-code.md")
    print()

    # Mind summary
    mind_total = sum(len(v) for v in mind_buckets.values())
    if mind_total:
        print(f"Mind framework changes detected ({mind_total} files):")
        for category, paths in mind_buckets.items():
            if not paths:
                continue
            label = {
                "py_production": "Production Python (core/scripts)",
                "py_daemon": "Daemon Python (mind_api/src)",
                "py_test": "Test files (Python)",
                "sh_wrapper": "Wrapper shells",
                "skill_md": "Skill pseudocode",
                "rule_md": "Behavioral rules",
                "config": "Config",
                "other_mind": "Other Mind files",
            }.get(category, category)
            print(f"  [{label}]")
            # Cap path enumeration at 8 per category to keep banner concise
            for p in paths[:8]:
                print(f"    {p}")
            if len(paths) > 8:
                print(f"    ... and {len(paths) - 8} more")
        if mind_recs:
            print()
            print("Recommended Mind full-suite invocations:")
            for r in mind_recs:
                print(f"  $ {r}")
            if mind_buckets.get("py_daemon"):
                # mind_api/tests folded into the chunked pool 2026-08-20
                # () — run-full-suite.sh now collects it, so a green
                # runner IS evidence about mind_api/src. The targeted command
                # stays the fast direct arm. Only printed when touched.
                print("  NOTE: mind_api/tests runs inside run-full-suite.sh"
                      " since 2026-08-20 (g-115-6942); the command above is"
                      " the fast targeted arm for this tree, not the only"
                      " coverage.")
    else:
        print("Mind framework: no code-affecting changes detected.")

    # Product summary
    print()
    if product_results:
        print(f"Product workspace changes detected ({len(product_results)} repo(s)):")
        for repo, repo_type, changed in product_results:
            print(f"  [{repo.name}] type={repo_type} ({len(changed)} file(s) changed)")
            for p in changed[:5]:
                print(f"    {p}")
            if len(changed) > 5:
                print(f"    ... and {len(changed) - 5} more")
        print()
        print("Recommended product full-suite invocations:")
        for repo, repo_type, _ in product_results:
            print(f"  $ {_product_recommendation(repo, repo_type)}")
        print()
        print("(post-execution.md Step 2.b.1 mandates this pre-push regardless;")
        print(" run BEFORE Phase 5 verify to avoid false 'all tests pass' claim.)")
    else:
        print("Product workspace: no uncommitted product code detected.")

    print()
    print("Targeted new tests are necessary but not sufficient. Run the above")
    print("full-suite invocations and confirm exit code 0 BEFORE claiming")
    print("'all tests pass' in Phase 5 verify.")
    print(BANNER_TOP)


# --- Cross-session pytest-suite mutex ---------------------------------------
# Multiple aspirations-execute Phase-4 closes can fire within sub-second of
# each other (concurrent autonomous loops in different terminals). Without
# this lock, every recommender emits the "run pytest" banner and every
# consumer LLM runs the suite simultaneously — observed: 5 pytest invocations
# at the same second, 4 more within 7 minutes. Thrashes the file system and
# explodes subprocess churn.
#
# Lock semantics (per user spec): O_CREAT|O_EXCL atomic create via _fileops.
# acquire_lock, 5-minute stale TTL (covers crash recovery — a session that
# crashed mid-emit), released on exit (try/finally — graceful exits free the
# lock immediately). Holder prints the recommendation banner; non-holders
# print a skip-message that the consumer LLM reads verbatim and treats as
# "no action needed — another session is handling the suite run".
PYTEST_SUITE_LOCK_STALE_SECONDS = 300


def _pytest_lock_path() -> Path | None:
    """Resolve {WORLD_DIR}/.locks/pytest-suite.lock; None if WORLD_DIR unset."""
    if WORLD_DIR is None:
        return None
    return Path(WORLD_DIR) / ".locks" / "pytest-suite.lock"


def _read_lock_info(lock_path: Path) -> dict:
    """Best-effort read of lock metadata for the skip-message display."""
    try:
        content = lock_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    content = content.strip()
    if not content:
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"raw": content}


def _acquire_pytest_lock(lock_path: Path, goal_id: str) -> bool:
    """Try to acquire the pytest-suite mutex. Returns True if acquired (caller
    MUST call release_lock in finally). False if another live session holds
    the lock — caller should print the skip-message and return.

    `timeout=0` makes acquire_lock try once and raise TimeoutError on contention
    instead of blocking. `stale_seconds=300` (5 min) lets the lock recover
    from a crashed prior emit without manual intervention.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        acquire_lock(
            lock_path,
            timeout=0,
            stale_seconds=PYTEST_SUITE_LOCK_STALE_SECONDS,
        )
    except TimeoutError:
        return False
    # acquire_lock writes the holder PID; overwrite with richer metadata so
    # the skip-message can name the holding session in human terms.
    try:
        info = {
            "pid": os.getpid(),
            "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": os.environ.get("MIND_AGENT", "unknown"),
            "goal_id": goal_id,
        }
        lock_path.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        # Best-effort enrichment; PID-only content from acquire_lock still works.
        pass
    return True


def _emit_skip_banner(goal_id: str, info: dict) -> None:
    """Print a skip-message readable by the consumer LLM as 'no action needed'."""
    holder_pid = info.get("pid", "?")
    holder_started = info.get("started_at", "?")
    holder_agent = info.get("agent", "?")
    holder_goal = info.get("goal_id", "?")
    print(
        f"[full-suite-recommender] SKIP: another session holds the pytest-suite "
        f"lock (pid={holder_pid}, agent={holder_agent}, goal={holder_goal}, "
        f"started={holder_started}). No action needed — that session will run "
        f"the full suite for this batch of changes. Goal: {goal_id}"
    )


# --- Entrypoint -------------------------------------------------------------
USAGE = (
    "usage: full-suite-recommender.py [GOAL_ID] [--outcome-class deep|routine]\n"
    "  GOAL_ID is POSITIONAL, not an env var -- `GOAL_ID=x ...` leaves it unset\n"
    "  and every line below prints goal=? (g-115-3587)."
)


def main() -> int:
    # Args: [goal_id] [--outcome-class deep|routine|...]
    # goal_id is POSITIONAL. A GOAL_ID env var is NOT read ().
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(USAGE)
        return 0
    goal_id = "?"
    outcome_class = "deep"  # default — gate is most useful for deep closures
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        goal_id = sys.argv[1]
    # Parse --outcome-class
    for i, a in enumerate(sys.argv[1:], start=1):
        if a == "--outcome-class" and i + 1 < len(sys.argv):
            outcome_class = sys.argv[i + 1]

    # Routine outcomes skip the recommender (per-rule scope).
    if outcome_class == "routine":
        # Quiet skip — emit a single-line breadcrumb so it's visible in diary.
        print(f"[full-suite-recommender] skip outcome_class=routine (goal={goal_id})")
        return 0

    # Detect Mind changes
    mind_changed = _git_changed_paths(PROJECT_ROOT)
    mind_buckets = _classify_mind(mind_changed)
    mind_recs = _mind_recommendations(mind_buckets)

    # Detect product changes across every configured write root ()
    product_results: list[tuple[Path, str, list[str]]] = []
    for write_root in _agent_write_paths():
        product_results.extend(_product_repos_with_changes(write_root))

    # Emit banner if anything detected; otherwise quiet skip
    any_changes = sum(len(v) for v in mind_buckets.values()) > 0 or len(product_results) > 0
    if not any_changes:
        #  (foxtrot addendum): this scan sees only what git sees, and
        # .gitignore excludes /.mind-data/ -- so world/ and meta/ edits are
        # STRUCTURALLY INVISIBLE here. Say so, or the line reads as an all-clear
        # and an agent closes a domain-script goal having run nothing (guard-3097).
        print(
            f"[full-suite-recommender] no GIT-VISIBLE code changes (goal={goal_id}); "
            "world/ and meta/ are gitignored and were NOT scanned, so this is not an "
            "all-clear -- if this goal edited domain scripts under world/, run the "
            "domain suite yourself (STORAGE_BACKEND=local per guard-955); skipping banner"
        )
        return 0

    # Cross-session pytest-suite mutex. Acquire only when we'd actually emit
    # a banner — the no-changes / routine-skip early returns don't need the
    # lock (they don't recommend pytest). If the lock is held by another live
    # session, print a skip-message instead of the banner; the consumer LLM
    # reads it as "no action needed".
    lock_path = _pytest_lock_path()
    if lock_path is None:
        # WORLD_DIR unconfigured (fresh single-agent deployment). Skip the
        # mutex and emit the banner directly — better to over-run pytest
        # than to mask the recommendation in a non-multi-agent setup.
        _emit_banner(goal_id, mind_buckets, mind_recs, product_results)
        return 0

    if not _acquire_pytest_lock(lock_path, goal_id):
        _emit_skip_banner(goal_id, _read_lock_info(lock_path))
        return 0

    try:
        _emit_banner(goal_id, mind_buckets, mind_recs, product_results)
    finally:
        release_lock(lock_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Fail-open: any error => exit 0 with diagnostic to stderr
        sys.stderr.write(f"[full-suite-recommender] error (fail-open): {exc}\n")
        sys.exit(0)
