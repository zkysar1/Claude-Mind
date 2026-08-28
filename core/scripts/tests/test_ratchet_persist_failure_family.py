"""Every advisory ratchet must report `error` when the baseline write FAILS.

The defect (g-115-4275, found by adversarial fresh-eyes during g-115-3946):
each ratchet computes its verdict inside `_modify`, which runs INSIDE
`locked_modify_yaml` and fills a `captured` dict BEFORE the write lands. The
error path then called `captured.setdefault("verdict", "error")` -- a NO-OP
once `_modify` has run. So a write that failed after the modifier still
reported seeded/ratcheted/stable, and /verify-learning recorded a baseline
write that never happened.

WHY THIS TEST USES A MODIFIER-THEN-RAISE SHIM, and not the "make the baseline
path a directory" trick the reference implementation's own test uses: an
unwritable path fails at READ time, BEFORE `_modify` is ever called, so
`captured` is empty and `setdefault` behaves correctly. That scenario passes
identically on the broken code -- measured 2026-08-27, pre-patch
experience-orphan-ratchet returned verdict='error' under it. A test for this
defect MUST fail the write AFTER the modifier has run, or it is vacuous.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent

# (script stem, argv for JSON output)
FAMILY = [
    ("temp-citation-ratchet", ["--json"]),          # the reference implementation
    ("unchecked-write-ratchet", ["--json"]),
    ("session-manifest-orphan-ratchet", ["--json"]),
    ("learning-routing-ratchet", ["--json"]),
    ("goal-field-census-ratchet", ["--json"]),
    ("experience-orphan-ratchet", ["--json"]),
    ("eviction-conservation-ratchet", ["--json"]),
    ("skillmd-flag-audit", ["--output", "json", "--ratchet"]),
]

SITECUSTOMIZE = '''
import os, sys
sys.path.insert(0, os.environ["RATCHET_SCRIPTS_DIR"])
import _fileops

def _write_fails_after_modifier(path, modifier_fn, initial=None):
    # Run the modifier exactly as a real cycle would, so `captured` is
    # populated with the COMPUTED verdict, then fail the write.
    try:
        modifier_fn(dict(initial or {}))
    except Exception:
        pass
    raise OSError("simulated write failure AFTER the modifier ran")

_fileops.locked_modify_yaml = _write_fails_after_modifier
'''


def _world(tmp: Path) -> Path:
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "n.md").write_text(
        "# Node\n\nEvidence: agents/alpha/temp/a.md\n", encoding="utf-8")
    for store in ("reasoning-bank", "guardrails", "pattern-signatures"):
        (world / f"{store}.jsonl").write_text(
            json.dumps({"id": "x-0", "category": "c",
                        "content": "see agents/alpha/temp/a.md"}) + "\n",
            encoding="utf-8")
    (world / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-1", "goals": [
            {"id": "g-1-1", "status": "pending", "title": "t"}]}) + "\n",
        encoding="utf-8")
    return world


@pytest.mark.parametrize("stem,argv", FAMILY, ids=[s for s, _ in FAMILY])
def test_failed_persist_never_reports_the_computed_verdict(stem, argv):
    script = SCRIPTS / f"{stem}.py"
    assert script.is_file(), f"{script} missing -- family list is stale"
    with tempfile.TemporaryDirectory(prefix="ratchetfam_") as d:
        tmp = Path(d)
        meta = tmp / "meta"
        meta.mkdir()
        shim = tmp / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")

        env = os.environ.copy()
        env["MIND_WORLD"] = str(_world(tmp))
        env["MIND_META"] = str(meta)
        env["STORAGE_BACKEND"] = "local"     # guard-955: never touch the real store
        env["RATCHET_SCRIPTS_DIR"] = str(SCRIPTS)
        env["PYTHONPATH"] = str(shim) + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("VERIFY_LEARNING_DRIFT_HARD_GATE", None)

        r = subprocess.run([sys.executable, str(script), *argv],
                           capture_output=True, text=True, encoding="utf-8",
                           env=env, cwd=str(SCRIPTS.parent.parent))
        assert r.stdout.strip(), (
            f"{stem} emitted no stdout (rc={r.returncode}); stderr:\n{r.stderr[-800:]}")
        out = json.loads(r.stdout)

        assert out["verdict"] == "error", (
            f"{stem}: a FAILED baseline write reported verdict="
            f"{out['verdict']!r} -- the caller records a write that never "
            "happened, and stderr is the only contradicting signal")
        assert out.get("baseline") is None, (
            f"{stem}: reported baseline={out.get('baseline')!r} for a write "
            "that failed")
        assert "FAILED" in (out.get("message") or ""), (
            f"{stem}: message does not disclose the failure: "
            f"{out.get('message')!r}")
