"""test_pending_deploys_gate.py — -b (pending-deploys hard gate, ENFORCE).

Verifies core/scripts/pending-deploys-gate.sh (SG-b): the closure gate that
refuses CLEAN-SUCCESS goal closure while a deploy obligation is unresolved. It
is invoked from iteration-close.sh do_verify (per-goal, --goal <id>) and
do_productivity_check (all-sweep, no --goal).

Isolation strategy mirrors test_pending_deploys.py's hook tests: a temp mind
repo whose core/scripts holds the REAL gate + pending-deploys.py +
deploy-verify.sh + _paths.sh + a .python-shim (execing sys.executable, NOT
`py -3`). Two dependencies are stubbed/faked to make the rc-branching
deterministic and hermetic:

  - a FAKE `gh` on PATH drives deploy-verify.sh's verdict (ok / failed / no_ci /
    API-error->unverified) without a network or a real repo. FAKE_GH_CONCLUSION
    picks the run conclusion; FAKE_GH_ACTIVE picks the active-workflow count
    ("" => workflow-list API error => unverified fast; "0" => no_ci).
  - STUB aspirations-query.sh / aspirations-add-goal.sh capture the Unblock the
    gate files on a FAILED deploy (rc 1) and let the dedup path be exercised,
    without a live daemon.

The gate is fail-open: it ALWAYS exits 0 and emits one summary JSON on stdout
({"checked","cleared","failed","unverified","unblocks_filed","not_clean"}).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE = CORE_SCRIPTS / "pending-deploys-gate.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_pending_deploys_gate_test"

_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_",
)

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _hermetic_env(**overrides) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"}
    env["STORAGE_BACKEND"] = "local"
    env.update(overrides)
    return env


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# ── Temp mind repo: real gate + deps, fake gh, stub daemon wrappers ─────────

def _setup_repo(tmp: Path, agent="zeta") -> Path:
    repo = tmp / "repo"
    (repo / "agents" / agent / "session").mkdir(parents=True)
    (repo / "agents" / agent / "self.md").write_text(f"# {agent}\n")
    (repo / "agents" / agent / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
    core = repo / "core" / "scripts"
    core.mkdir(parents=True)
    (repo / ".claude").mkdir()

    # Real framework artifacts under test + their runtime deps.
    # _runtime_bash.py is a REQUIRED dep of pending-deploys.py ():
    # resolve() shells out to deploy-verify.sh via _runtime_bash.BASH rather
    # than a bare "bash", because on win32 CreateProcess searches System32
    # before PATH and a bare "bash" reaches the WSL launcher, which blocks
    # forever on a dead LxssManager. Omit it and the copied script cannot
    # import, so the hermetic repo silently stops exercising the real path.
    for fname in ("pending-deploys-gate.sh", "pending-deploys.py",
                  "deploy-verify.sh", "_paths.sh", "_runtime_bash.py"):
        dst = core / fname
        dst.write_bytes((CORE_SCRIPTS / fname).read_bytes())
        dst.chmod(0o755)

    # python3/python shim -> sys.executable (deploy-verify's `py -3` uses the
    # real py on PATH, which the hermetic env preserves).
    shim = core / ".python-shim"
    shim.mkdir()
    for name in ("python3", "python"):
        s = shim / name
        s.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
        s.chmod(0o755)

    # Fake `gh`: only `gh api <endpoint> [-q ...]` is used by deploy-verify.sh.
    ghbin = repo / "_fakebin"
    ghbin.mkdir()
    gh = ghbin / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\n'
        'all="$*"\n'
        'case "$all" in\n'
        # landed-detection (pending-deploys.py _landed_on_default): compare
        # ahead_by ("0" => ancestor-landed) and commits/<sha>/pulls merged count
        # (>0 => superseded-by-merge). Defaults (1 / 0) => NOT landed, so tests
        # that do not set these keep the pre-landed-detection behavior.
        '  *"/compare/"*) printf "%s" "${FAKE_AHEAD_BY-1}" ;;\n'
        '  *"/commits/"*"/pulls"*) printf "%s" "${FAKE_MERGED-0}" ;;\n'
        '  *"actions/workflows"*) printf "%s" "${FAKE_GH_ACTIVE-1}" ;;\n'
        '  *"actions/runs"*)\n'
        '    c="${FAKE_GH_CONCLUSION:-success}"\n'
        '    printf \'{"workflow_runs":[{"name":"CI","status":"completed","conclusion":"%s","html_url":"http://x/1"}]}\\n\' "$c" ;;\n'
        '  *"/commits/"*) printf "0000000000000000000000000000000000000000" ;;\n'
        '  *) printf "{}" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    gh.chmod(0o755)

    # Stub aspirations-query.sh: FAKE_QUERY_DUP=1 => a live (pending) Unblock
    # already exists (dedup should suppress filing); else [] (no dup).
    q = core / "aspirations-query.sh"
    q.write_text(
        '#!/usr/bin/env bash\n'
        'if [ "${FAKE_QUERY_DUP:-0}" = "1" ]; then\n'
        '  st="${FAKE_QUERY_STATUS:-pending}"\n'
        '  printf \'[{"goal_id":"g-dup","status":"%s","origin_signal":"unblock:pending-deploy-x"}]\' "$st"\n'
        'else\n'
        '  printf "[]"\n'
        'fi\n'
        'exit 0\n'
    )
    q.chmod(0o755)

    # Stub aspirations-add-goal.sh: capture the filed goal JSON (stdin) to a file.
    a = core / "aspirations-add-goal.sh"
    a.write_text(
        '#!/usr/bin/env bash\n'
        'out="${FILED_GOALS:-/dev/null}"\n'
        'cat >> "$out"\n'
        'printf "\\n" >> "$out"\n'
        'exit 0\n'
    )
    a.chmod(0o755)
    return repo


def _store(repo: Path, agent="zeta") -> Path:
    return repo / "agents" / agent / "session" / "pending-deploys.yaml"


def _seed(repo: Path, entries, agent="zeta"):
    import yaml
    _store(repo, agent).write_text(yaml.safe_dump(entries))


def _entry(repo_name="owner/prod", sha=None, goal="g-115-2688-b", d="/tmp/x"):
    return {"repo": repo_name, "sha": sha or ("a" * 40), "goal_id": goal,
            "dir": d, "ts": "2026-07-19T15:00:00"}


def _run_gate(repo: Path, *args, agent="zeta", **env_overrides):
    ghbin = repo / "_fakebin"
    env = _hermetic_env(**env_overrides)
    env["PATH"] = f"{_to_bash_path(ghbin)}:" + env.get("PATH", "")
    # GH_BIN points at the EXTENSIONLESS stub, deliberately. It is the only form
    # that survives both worlds: bash execs it directly, and it carries the `&`
    # in the runs query intact. Python cannot exec it (WinError 193) and falls
    # back to bash via pending-deploys.run_gh() / deploy-verify's BASH_BIN.
    # A .cmd shim was tried and MEASURED BROKEN -- cmd.exe re-parses the command
    # line and truncates `...head_sha=X&per_page=50` at the ampersand.
    env["GH_BIN"] = (ghbin / "gh").as_posix()
    return subprocess.run(
        [BASH, _to_bash_path(repo / "core" / "scripts" / "pending-deploys-gate.sh"),
         "--agent", agent, "--timeout-mins", "1", *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


def _summary(result) -> dict:
    """The gate prints exactly one summary-JSON line to stdout; parse the last."""
    lines = [l for l in (result.stdout or "").splitlines() if l.strip().startswith("{")]
    assert lines, f"no summary JSON on stdout; stdout={result.stdout!r} stderr={result.stderr!r}"
    return json.loads(lines[-1])


def _load_store(repo: Path):
    import yaml
    p = _store(repo)
    if not p.exists():
        return []
    return yaml.safe_load(p.read_text()) or []


# ── Tests ───────────────────────────────────────────────────────────────────

def test_gate_fast_exit_no_pending():
    """No pending entries -> single cheap summary, checked:0, not_clean:false."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        r = _run_gate(repo, "--goal", "g-115-2688-b")
        assert r.returncode == 0, f"gate crashed: {r.stderr!r}"
        s = _summary(r)
        assert s == {"checked": 0, "cleared": 0, "failed": 0, "unverified": 0,
                     "unblocks_filed": 0, "not_clean": False}, s


def test_gate_clears_on_ok():
    """rc 0 (CI success) -> entry cleared, not_clean:false."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        _seed(repo, [_entry()])
        r = _run_gate(repo, "--goal", "g-115-2688-b", FAKE_GH_CONCLUSION="success")
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["checked"] == 1 and s["cleared"] == 1 and s["not_clean"] is False, s
        assert _load_store(repo) == [], f"verified entry not cleared: {_load_store(repo)}"


def test_gate_clears_on_no_ci():
    """rc 0 via no_ci (repo has no active workflows) is a real pass, not a block."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        _seed(repo, [_entry()])
        r = _run_gate(repo, "--goal", "g-115-2688-b", FAKE_GH_ACTIVE="0")
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["cleared"] == 1 and s["not_clean"] is False, s
        assert _load_store(repo) == [], "no_ci entry should clear"


def test_gate_files_unblock_on_failed():
    """rc 1 (CI failure) -> HIGH Unblock filed, closure not-clean, entry KEPT."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        filed = Path(td) / "filed-goals.jsonl"
        _seed(repo, [_entry(sha="b" * 40)])
        r = _run_gate(repo, "--goal", "g-115-2688-b",
                      FAKE_GH_CONCLUSION="failure", FILED_GOALS=str(filed))
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["failed"] == 1 and s["not_clean"] is True, s
        assert s["unblocks_filed"] == 1, f"Unblock not filed: {s} stderr={r.stderr!r}"
        # entry KEPT (SG-c backstop; dedup makes re-probe idempotent)
        assert len(_load_store(repo)) == 1, "failed entry must be kept for SG-c"
        # filed goal is a HIGH Unblock with a deploy origin_signal
        body = json.loads([l for l in filed.read_text().splitlines() if l.strip()][0])
        assert body["priority"] == "HIGH", body
        assert body["title"].startswith("Unblock:"), body
        assert body["origin_signal"] == "unblock:pending-deploy-" + ("b" * 40)[:7], body
        assert body["participants"] == ["agent"], body


def test_gate_dedup_unblock_when_live_exists():
    """rc 1 but a live Unblock already names this sha -> no re-file (dedup)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        filed = Path(td) / "filed-goals.jsonl"
        _seed(repo, [_entry(sha="c" * 40)])
        r = _run_gate(repo, "--goal", "g-115-2688-b",
                      FAKE_GH_CONCLUSION="failure", FAKE_QUERY_DUP="1",
                      FILED_GOALS=str(filed))
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["failed"] == 1 and s["not_clean"] is True, s
        assert s["unblocks_filed"] == 0, "dedup should suppress the re-file"
        assert not filed.exists() or filed.read_text().strip() == "", "no goal should be filed"


def test_gate_keeps_on_unverified():
    """rc 2 (CI unverified) -> entry KEPT, not_clean:true, NO Unblock."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        _seed(repo, [_entry(sha="d" * 40)])
        # FAKE_GH_ACTIVE="" => workflow-list API error => deploy-verify exit 2 (fast).
        r = _run_gate(repo, "--goal", "g-115-2688-b", FAKE_GH_ACTIVE="")
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["unverified"] == 1 and s["not_clean"] is True, s
        assert s["unblocks_filed"] == 0, "unverified must NOT file an Unblock (rb-611)"
        assert len(_load_store(repo)) == 1, "unverified entry kept for re-probe"


def test_gate_all_sweep_checks_every_entry():
    """No --goal (productivity-check sweep) -> re-probes ALL entries."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        _seed(repo, [_entry(sha="a" * 40, goal="g-1"),
                     _entry(repo_name="owner/other", sha="b" * 40, goal="g-2")])
        r = _run_gate(repo, FAKE_GH_CONCLUSION="success")  # no --goal
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["checked"] == 2 and s["cleared"] == 2, s
        assert _load_store(repo) == [], "all verified entries cleared"


def test_gate_fail_open_no_agent():
    """No agent resolvable -> exit 0, error summary, never raises."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        env = _hermetic_env()
        env.pop("MIND_AGENT", None)
        r = subprocess.run(
            [BASH, _to_bash_path(repo / "core" / "scripts" / "pending-deploys-gate.sh")],
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert r.returncode == 0, r.stderr
        assert '"error":"no-agent"' in r.stdout, r.stdout


# ── : landed-detection (defect 1) + completed-dedup (defect 2) ─────

def test_gate_clears_superseded_via_landed_detection():
    """DEFECT 1: a FAILED deploy whose sha landed via a MERGED PR (superseded)
    is auto-cleared by resolve's landed-detection -> the gate counts it cleared,
    files NO Unblock, and the closure stays clean. This is the end-to-end fix
    for the bddb90c poison class (deploy-verify(dead-sha)=failed forever)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        filed = Path(td) / "filed-goals.jsonl"
        _seed(repo, [_entry(sha="e" * 40)])
        # deploy-verify -> failed; landed-detection -> not-ancestor but merged PR.
        r = _run_gate(repo, "--goal", "g-115-2688-b",
                      FAKE_GH_CONCLUSION="failure", FAKE_AHEAD_BY="9", FAKE_MERGED="1",
                      FILED_GOALS=str(filed))
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["cleared"] == 1 and s["failed"] == 0, s
        assert s["not_clean"] is False, f"superseded-landed entry must clear cleanly: {s}"
        assert s["unblocks_filed"] == 0, "no Unblock for a landed sha"
        assert _load_store(repo) == [], "landed entry not cleared from ledger"


def test_gate_clears_ancestor_landed_via_landed_detection():
    """DEFECT 1: an UNVERIFIED deploy whose sha is an ancestor of the default
    branch (ahead_by==0 -- CI ran on the branch HEAD, not this intermediate
    commit) is cleared, not stranded unverified forever (aa9a788/4adcc37)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        _seed(repo, [_entry(sha="f" * 40)])
        # FAKE_GH_ACTIVE="" -> deploy-verify unverified fast; ahead_by=0 -> ancestor.
        r = _run_gate(repo, "--goal", "g-115-2688-b",
                      FAKE_GH_ACTIVE="", FAKE_AHEAD_BY="0")
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["cleared"] == 1 and s["unverified"] == 0 and s["not_clean"] is False, s
        assert _load_store(repo) == [], "ancestor-landed entry not cleared"


def test_gate_dedup_unblock_when_completed_exists():
    """DEFECT 2: rc 1, NOT landed, but a COMPLETED Unblock already names this sha
    -> no re-file. The re-file storm (8 duplicate bddb90c Unblocks) came from the
    dedup checking only live goals; a completed Unblock must suppress too."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        filed = Path(td) / "filed-goals.jsonl"
        _seed(repo, [_entry(sha="c" * 40)])
        r = _run_gate(repo, "--goal", "g-115-2688-b",
                      FAKE_GH_CONCLUSION="failure", FAKE_QUERY_DUP="1",
                      FAKE_QUERY_STATUS="completed", FILED_GOALS=str(filed))
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["failed"] == 1 and s["not_clean"] is True, s
        assert s["unblocks_filed"] == 0, "a completed Unblock must suppress the re-file"
        assert not filed.exists() or filed.read_text().strip() == "", "no goal should be filed"


def test_gate_refiles_when_only_skipped_exists():
    """DEFECT 2 boundary: a SKIPPED Unblock does NOT suppress -- a re-failed
    re-push legitimately needs a fresh Unblock, so skipped/expired never dedup."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        filed = Path(td) / "filed-goals.jsonl"
        _seed(repo, [_entry(sha="c" * 40)])
        r = _run_gate(repo, "--goal", "g-115-2688-b",
                      FAKE_GH_CONCLUSION="failure", FAKE_QUERY_DUP="1",
                      FAKE_QUERY_STATUS="skipped", FILED_GOALS=str(filed))
        assert r.returncode == 0, r.stderr
        s = _summary(r)
        assert s["failed"] == 1, s
        assert s["unblocks_filed"] == 1, "skipped must NOT dedup -- fresh Unblock filed"
