""": product-repo reads are RECORDED, so Q4 can adjudicate them.

Before this change the context-reads recorder dropped every read outside
is_in_scope_advisory, and a product repo (an AGENT_WRITE_PATH root outside this
repo) lies outside it. So a product file opened WITH THE READ TOOL was never
recorded, and Q4 scored an accurate citation of it `decorative-citation` -- the
same verdict as a file never opened. The only move that cleared it was deleting
the citation, which is the opposite of what Q4 exists to encourage.

The disposition is to widen the RECORDER only (is_in_scope_record). These tests
pin both halves: the new recording (fails on the pre-change code), and the
scopes that must NOT move -- the blocking dedup gate and the pre-edit advisory.

Every test drives the REAL hook scripts with the production hook-JSON shape
(guard-920), against a throwaway agent whose local-paths.conf names a tmp
product root.

Run: py -3 -m pytest core/scripts/tests/test_context_reads_product_scope.py -v
"""
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # repo root

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402
from _context_reads_helper import norm_path as _norm  # noqa: E402

THROWAWAY_AGENT = "_product_scope_test_throwaway_agent_"
SID = "product-scope-sid-001"

# In-scope control: core/scripts is advisory scope, so today's recorder keeps it.
IN_SCOPE_FILE = SCRIPT_DIR / "context-reads-record.sh"
SKILLS_FILE = PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md"
RULES_FILE = PROJECT_ROOT / ".claude" / "rules" / "read-before-edit.md"


@contextmanager
def _throwaway_agent(write_path):
    """agents/<throwaway>/ with a local-paths.conf naming `write_path` as
    AGENT_WRITE_PATH. Yields the env for the hook subprocesses. Always cleans up."""
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    try:
        (agent_dir / "session").mkdir(parents=True, exist_ok=True)
        # newline="" disables CRLF translation on Windows (guard-1688). The value
        # is QUOTED, as live confs write it: _paths.sh `source`s this file, so
        # an unquoted multi-root value splits at ';' into a second command.
        (agent_dir / "local-paths.conf").write_text(
            f'WORLD_PATH=\nMETA_PATH=\nAGENT_WRITE_PATH="{write_path}"\n',
            encoding="utf-8", newline="")
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        env["STORAGE_BACKEND"] = "local"   # guard-955
        yield env
    finally:
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _product_file(root, rel, text="x = 1\n"):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _hook(script, env, file_path, **tool_input):
    payload = json.dumps({
        "tool_name": "Read",
        "tool_input": dict(file_path=str(file_path), **tool_input),
        "session_id": SID,
    })
    r = subprocess.run([BASH, f"core/scripts/{script}"], input=payload,
                       capture_output=True, text=True, env=env, timeout=30,
                       cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout, r.stderr


def _manifest():
    mf = PROJECT_ROOT / "agents" / THROWAWAY_AGENT / "session" / "context-reads.txt"
    if not mf.exists():
        return []
    return [l.strip() for l in mf.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── The new recording (each of these FAILS on the pre-change recorder) ───────

def test_a_product_repo_read_is_recorded(tmp_path):
    root = tmp_path / "product"
    f = _product_file(root, "widget-lib/src/widget-core.py")
    with _throwaway_agent(root) as env:
        rc, _o, err = _hook("context-reads-record.sh", env, f)
        lines = _manifest()
    assert rc == 0, err
    assert _norm(f) in lines, f"product read must be recorded. manifest={lines!r}"


def test_a_RANGED_product_read_is_recorded_as_partial_only(tmp_path):
    """The full/partial split carries over unchanged: Q4 still refuses a ranged
    peek, so the new tier must not turn one into a full record."""
    root = tmp_path / "product"
    f = _product_file(root, "widget-lib/src/widget-core.py")
    with _throwaway_agent(root) as env:
        _hook("context-reads-record.sh", env, f, offset=1, limit=5)
        lines = _manifest()
    assert "#partial:" + _norm(f) in lines, lines
    assert _norm(f) not in lines, lines


def test_every_root_of_a_multi_root_write_path_is_recorded(tmp_path):
    """AGENT_WRITE_PATH may name several roots separated by ';' ()."""
    a, b = tmp_path / "repo-a", tmp_path / "repo-b"
    f = _product_file(b, "pkg/mod.py")
    with _throwaway_agent(f"{a};{b}") as env:
        _hook("context-reads-record.sh", env, f)
        lines = _manifest()
    assert _norm(f) in lines, lines


# ── The containment must be exact (pass on both codes; they guard the new one) ─

def test_a_SIBLING_dir_sharing_the_root_prefix_is_not_recorded(tmp_path):
    """'<tmp>/Ayoai-Mind' starts with the string '<tmp>/Ayoai' but is not under it.
    The in-scope branch in the same call is the positive control."""
    root = tmp_path / "Ayoai"
    inside = _product_file(root, "pkg/mod.py")
    sibling = _product_file(tmp_path / "Ayoai-Mind", "pkg/mod.py")
    with _throwaway_agent(root) as env:
        _hook("context-reads-record.sh", env, inside)
        _hook("context-reads-record.sh", env, sibling)
        lines = _manifest()
    assert _norm(inside) in lines, lines
    assert _norm(sibling) not in lines, lines


def test_in_repo_classes_keep_their_scope_when_a_write_root_CONTAINS_the_repo():
    """A box whose write root spans this repo must not start recording
    .claude/rules/** through the product tier: q4 expressible_predicate reads
    those paths as unrecordable (unadjudicable), and the two must agree. The
    skills read is the control proving the hook ran."""
    with _throwaway_agent(PROJECT_ROOT.parent) as env:
        _hook("context-reads-record.sh", env, SKILLS_FILE)
        _hook("context-reads-record.sh", env, RULES_FILE)
        lines = _manifest()
    assert _norm(SKILLS_FILE) in lines, lines
    assert _norm(RULES_FILE) not in lines, lines


# ── Scopes that must NOT move (recorder-only disposition) ────────────────────

def test_a_recorded_product_file_is_never_dedup_blocked(tmp_path):
    root = tmp_path / "product"
    f = _product_file(root, "widget-lib/src/widget-core.py")
    with _throwaway_agent(root) as env:
        _hook("context-reads-record.sh", env, f)
        rc, _o, err = _hook("context-reads-gate.sh", env, f)
    assert rc == 0, f"a product re-read must never be blocked, got {rc}: {err!r}"


def test_the_pre_edit_advisory_stays_silent_on_product_code(tmp_path):
    """check-file prints a path only when it is in ADVISORY scope and unread.
    Product code stays out of that scope, as pre-edit-context-gate.sh documents."""
    root = tmp_path / "product"
    f = _product_file(root, "widget-lib/src/widget-core.py")
    with _throwaway_agent(root) as env:
        r = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "context-reads.py"), "check-file",
             "--partial-aware", "--session-id", SID, str(f)],
            capture_output=True, text=True, env=env, timeout=30,
            cwd=str(PROJECT_ROOT))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", r.stdout


# ── End to end: the Q4 verdict (FAILS on the pre-change recorder) ────────────

def test_Q4_passes_a_READ_product_citation_and_still_fails_an_UNREAD_one(tmp_path):
    """The positive control the goal asked for, at the verdict level. Line 1
    cites an in-scope file (read); line 3 a product file (read); line 5 a
    product file that exists but was never read. Pre-change: lines 3 AND 5 are
    decorative. Post-change: only line 5, so the check still bites."""
    root = tmp_path / "product"
    read_f = _product_file(root, "widget-lib/src/widget-core.py")
    _product_file(root, "widget-lib/src/widget-keys.py")
    art = tmp_path / "art.md"
    art.write_text(
        "In 2026 the Read hook is the tracker's only file writer "
        "(core/scripts/context-reads-record.sh).\n\n"
        "In 2026 the widget core is a frozen dataclass "
        "(widget-lib/src/widget-core.py).\n\n"
        "In 2026 the widget key map is defined in one module "
        "(widget-lib/src/widget-keys.py).\n",
        encoding="utf-8")
    with _throwaway_agent(root) as env:
        _hook("context-reads-record.sh", env, IN_SCOPE_FILE)
        _hook("context-reads-record.sh", env, read_f)
        r = subprocess.run(
            [BASH, "core/scripts/q4-provenance-sample.sh", "--goal", "g-115-9674",
             "--artifact", str(art), "--session-id", SID, "--json"],
            capture_output=True, text=True, env=env, timeout=60,
            cwd=str(PROJECT_ROOT))
    result = json.loads(r.stdout)
    # The decorative test must have RUN, or an empty finding list proves nothing.
    assert result["provenance_manifest"] == "readable", result
    assert result["sampled_count"] == 3, result
    decorative = sorted(f["start_line"] for f in result["findings"]
                        if f["kind"] == "decorative-citation")
    assert decorative == [5], result["findings"]
    assert result["verdict"] == "fail" and r.returncode == 1, result
