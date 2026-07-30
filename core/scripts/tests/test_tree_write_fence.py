"""Two-writer regression tests for tree_write_fence ().

The defect: knowledge-tree .md bodies are written with the raw Edit/Write tool
with no lock, no version check and no conflict signal, so two agents that both
Read a node and then both write it produce a silent last-write-wins loss.

These tests do two things, in order, because proving the fix without first
proving the bug is how a fence that fences nothing ships green:

  1. reproduce the loss with no fence  -> writer A's section is GONE, silently
  2. run the same sequence with the fence -> the loss is DETECTED (DIVERGED)
     and written to a durable ledger

Note what is NOT claimed: the fence does not PREVENT the overwrite. It converts
an undetectable loss into a loud one. See the module docstring for why
prevention was rejected here (loop-wedge risk; rb-3080 tree-index OCC stalls).
"""
import os
import subprocess
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bash_helpers import BASH
import tree_write_fence as F  # noqa: E402


NODE_REL = "knowledge/tree/system/demo-node.md"

BASE_BODY = """---
topic: demo
last_updated: "2026-07-28"
---

# Demo Node

## Pre-existing Section
original content
"""

A_SECTION = "\n## Section By Writer A\nA's hard-won encoding\n"
B_SECTION = "\n## Section By Writer B\nB's encoding\n"


def _mknode(tmp_path):
    p = tmp_path / NODE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(BASE_BODY, encoding="utf-8")
    return p


# ---------------------------------------------------------------- scope


def test_in_scope_matches_only_tree_node_bodies():
    assert F.in_scope("world/knowledge/tree/system/x.md")
    assert F.in_scope(r"C:\w\knowledge\tree\system\x.md")  # windows separators
    # the INDEX is deliberately out of scope (already locked via _fileops)
    assert not F.in_scope("world/knowledge/tree/_tree.yaml")
    assert not F.in_scope("core/scripts/retrieve.py")
    assert not F.in_scope("world/conventions/board.md")
    assert not F.in_scope("")
    assert not F.in_scope(None)


# ------------------------------------------------- 1. reproduce the bug


def test_unfenced_concurrent_write_silently_destroys_first_writer(tmp_path):
    """The defect itself. No fence: A's section vanishes with zero signal."""
    node = _mknode(tmp_path)

    # Both writers Read the same starting state (the real precondition).
    a_view = node.read_text(encoding="utf-8")
    b_view = node.read_text(encoding="utf-8")
    assert a_view == b_view

    # A writes first and verifies it landed -- exactly what the live incident did.
    node.write_text(a_view + A_SECTION, encoding="utf-8")
    assert "Section By Writer A" in node.read_text(encoding="utf-8")

    # B writes from its STALE view. Nothing raises; nothing reports.
    node.write_text(b_view + B_SECTION, encoding="utf-8")

    final = node.read_text(encoding="utf-8")
    assert "Section By Writer B" in final
    assert "Section By Writer A" not in final       # <-- silently destroyed
    assert "original content" in final              # looks perfectly healthy


# ------------------------------------------------- 2. the fence detects it


def test_fence_detects_the_overwrite_and_is_not_silent(tmp_path):
    node = _mknode(tmp_path)
    store = tmp_path / "baselines.json"
    ledger = tmp_path / "conflicts.jsonl"

    # Writer B observes the node (Read -> record baseline).
    b_view = node.read_text(encoding="utf-8")
    rec = F.record(node, store)
    assert rec["scoped"] is True and rec["recorded"] is True

    # Nothing has changed yet -> B may safely write.
    assert F.check(node, store)["verdict"] == "clean"

    # Writer A lands in between.
    node.write_text(b_view + A_SECTION, encoding="utf-8")

    # Now B's pre-write check catches it.
    verdict = F.check(node, store)
    assert verdict["verdict"] == "DIVERGED"
    assert verdict["baseline_sha256"] != verdict["live_sha256"]

    # ...and the report is DURABLE, not stderr-only (guard-772).
    assert F.append_ledger(ledger, verdict) is True
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["verdict"] == "DIVERGED"
    assert rows[0]["path"].endswith("demo-node.md")


def test_recording_after_own_write_prevents_false_positive(tmp_path):
    """Re-observing after our own write must reset the baseline.

    Without this the agent's SECOND edit to a node it legitimately owns would
    report DIVERGED against its own change -- the false-positive class that
    makes a fence get ignored.
    """
    node = _mknode(tmp_path)
    store = tmp_path / "baselines.json"

    F.record(node, store)
    node.write_text(node.read_text(encoding="utf-8") + A_SECTION, encoding="utf-8")
    assert F.check(node, store)["verdict"] == "DIVERGED"   # our own write

    F.record(node, store)                                   # re-observe
    assert F.check(node, store)["verdict"] == "clean"


# ------------------------------------------------------- fail-open posture


def test_concurrent_records_do_not_lose_baselines(tmp_path):
    """Regression: the shipped first cut lost 19 of 20 concurrent baselines.

    It kept every baseline in ONE json map and did load -> mutate ->
    os.replace. The replace is atomic; the read-modify-write is not. Hooks fire
    concurrently, so writers clobbered each other and the survivors degraded to
    `no_baseline` -- the fence going silently blind, which is the exact class it
    exists to catch. Storage is now one file per node, so there is no shared
    structure to race on. This test FAILS on the pre-fix implementation.
    """
    import threading

    store = tmp_path / "baselines"
    nodes = []
    d = tmp_path / "knowledge" / "tree" / "system"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        p = d / ("n%02d.md" % i)
        p.write_text("# node %d\n" % i, encoding="utf-8")
        nodes.append(p)

    barrier = threading.Barrier(len(nodes))

    def worker(p):
        barrier.wait()          # maximize overlap
        F.record(p, store)

    threads = [threading.Thread(target=worker, args=(p,)) for p in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(F.load_baselines(store)) == len(nodes)
    # and every one is individually retrievable, not merely counted
    for p in nodes:
        assert F.check(p, store)["verdict"] == "clean"


def test_never_raises_on_hostile_inputs(tmp_path):
    store = tmp_path / "nested" / "deep" / "baselines"
    assert F.check(tmp_path / "knowledge/tree/absent.md", store)["verdict"] == "no_baseline"
    assert F.file_hash(tmp_path / "does-not-exist.md") is None
    assert F.load_baselines(tmp_path / "nope") == {}      # absent store dir
    # one corrupt entry must not blind the whole store
    good = tmp_path / "knowledge" / "tree" / "ok.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# ok\n", encoding="utf-8")
    F.record(good, store)
    (Path(store) / "corrupt.json").write_text("{not json", encoding="utf-8")
    assert len(F.load_baselines(store)) == 1
    assert F.check(good, store)["verdict"] == "clean"
    # out-of-scope paths short-circuit both ops
    assert F.record("core/scripts/x.py", store)["scoped"] is False
    assert F.check("core/scripts/x.py", store)["verdict"] == "not_scoped"


def test_hash_is_byte_exact_not_text_normalized(tmp_path):
    """CRLF vs LF must be a real difference, not folded away by text mode.

    A text-mode read on Windows folds CRLF->LF, so a writer that rewrote line
    endings would hash identical and the fence would miss a real rewrite.
    """
    p = tmp_path / "knowledge" / "tree" / "n.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"# x\nline\n")
    lf = F.file_hash(p)
    p.write_bytes(b"# x\r\nline\r\n")
    assert F.file_hash(p) != lf


# ------------------------------------------------- shell-wrapper wiring ()
# Everything above exercises tree_write_fence.py DIRECTLY, and all of it passed on
# the day the fence shipped. The fence was inert anyway: its shell wrapper bailed
# before ever calling this module, so no baseline was recorded at any of its four
# wiring points. Measured 2026-07-28, 18h after landing -- zero baselines, zero
# ledger entries, and one real lost update that went undetected while the fence
# was "live". The engine was never the problem, and the engine is all this file
# tested. That is the same shape the docstring above warns about, one level up:
# a fence that fences nothing ships green when the tests stop at the engine.
#
# Root cause: MIND_AGENT is injected only into PreToolUse[Bash], NOT into
# Read/Write/Edit hooks. The wrapper gated on AGENT_DIR (empty without it) and
# exited 0. It hand-tested GREEN from a Bash call, because that path DOES get the
# inject -- which is exactly what kept it hidden. The sibling
# context-reads-record.sh documents the hazard and carries the fallback already.

HOOK_SH = Path(__file__).resolve().parent.parent / "tree-write-fence.sh"


def _run_hook(op, file_path, agent_dir, session_id="", agent="alpha"):
    """Invoke the wrapper the way a hook does: hook JSON on stdin."""
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)   # _paths.py test override -> hermetic
    env["MIND_AGENT"] = agent
    payload = json.dumps({"session_id": session_id,
                          "tool_input": {"file_path": str(file_path)}})
    return subprocess.run([BASH, str(HOOK_SH), op], input=payload,
                          capture_output=True, text=True, env=env)


def test_shell_wrapper_reaches_the_engine_and_a_baseline_lands(tmp_path):
    """The test whose absence let an inert fence ship green.

    Asserts the far side of the wrapper, not its exit code: exit 0 is what the
    BROKEN wrapper returned too (it is fail-open by design), so a returncode
    assertion alone would have passed against the defect.
    """
    node = _mknode(tmp_path)
    agent_dir = tmp_path / "agent"
    (agent_dir / "session").mkdir(parents=True)

    r = _run_hook("record", node, agent_dir)
    assert r.returncode == 0, r.stderr

    baselines = agent_dir / "session" / "tree-write-baselines"
    assert baselines.is_dir() and list(baselines.iterdir()), (
        "wrapper exited 0 but no baseline landed -- it is not reaching the engine, "
        "which is the g-115-3720 defect exactly")


def test_shell_wrapper_detects_divergence_end_to_end(tmp_path):
    """record -> another writer lands -> check must report the loss loudly."""
    node = _mknode(tmp_path)
    agent_dir = tmp_path / "agent"
    (agent_dir / "session").mkdir(parents=True)

    assert _run_hook("record", node, agent_dir).returncode == 0
    node.write_text(BASE_BODY + B_SECTION, encoding="utf-8")   # writer B lands
    r = _run_hook("check", node, agent_dir)

    assert r.returncode == 0                      # advisory: never blocks
    assert "CONFLICT" in r.stderr, r.stderr       # loud on stderr
    assert str(node) in r.stderr                  # and it NAMES the file
    ledger = agent_dir / "session" / "tree-write-conflicts.jsonl"
    assert ledger.exists(), "stderr alone is not durable (guard-772)"
    assert json.loads(ledger.read_text().splitlines()[-1])["verdict"] == "DIVERGED"


def test_wrapper_does_not_depend_on_ayoai_agent_being_injected():
    """Pin the specific regression.

    Behavioural coverage of the session-binding fallback needs a real binding
    under PROJECT_ROOT and is not hermetic, so this asserts the structure: the
    fallback must be present, and the bare-AGENT_DIR bail that caused the outage
    must stay gone.
    """
    src = HOOK_SH.read_text(encoding="utf-8")
    # Strip comments before asserting. The fix's own comment QUOTES the removed
    # guard in order to explain it, so a whole-file substring check matches the
    # explanation and fails against the corrected script -- which is what this
    # assertion did on first run. A literal in a comment is not the same claim as
    # the same literal in code.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "session-binding-read.sh" in code, (
        "the agent fallback is gone -- the wrapper is inert in every hook context "
        "again (MIND_AGENT is not injected outside PreToolUse[Bash])")
    assert '[ -z "${AGENT_DIR:-}" ] && exit 0' not in code, (
        "the bare-AGENT_DIR bail is back; that is the g-115-3720 silent no-op")


def test_wrapper_fails_open_when_no_agent_can_be_resolved(tmp_path):
    """A fence that breaks must never be the outage."""
    node = _mknode(tmp_path)
    agent_dir = tmp_path / "agent"
    (agent_dir / "session").mkdir(parents=True)
    r = _run_hook("record", node, agent_dir, session_id="", agent="")
    assert r.returncode == 0
