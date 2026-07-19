"""test_board_source_tag_attribution.py — regression test for board.py cmd_post
source-tag attribution (g-115-519; transport repaired g-115-2351).

History: the original version drove board.py as a subprocess against a tmp
world and asserted on-disk counter changes. That shape tested a DEAD path
without knowing it — board.py's attribution spawn of
`reasoning-bank.py <family> increment ...` became a silent no-op when H2
Wave 2 (2026-05-15) removed the rb CLI subcommands (the child imported the
library and exited 0 without writing), so every case failed with tih=0 and
the file was quarantined (g-115-2351 class A). The repair routes board.py
through _rt.store_increment (the canonical Python->daemon client, same as
utilization-feedback.py).

This rewrite tests the attribution LOOP in-process with a stubbed _rt
module (records calls; no daemon, no filesystem stores, no live-world
hazard — the whole guard-955 / g-115-960 env-inheritance class evaporates).
The end-to-end daemon write path (real store increments + smoothed score
recompute + the 4-digit-ID regex regression) is covered by
mind_api/tests/test_runtime_board_write.py::test_post_findings_citation_increments.

Self-contained: channel appends land in a tmp MIND_WORLD; store writes are
stubbed out entirely.

COLLECTION-SAFETY (g-115-2487 / guard-1165): ALL side effects — tmp dir, env
pins, sys.path insert, the sys.modules["_rt"] stub, and the `import board` —
live INSIDE main() with save/restore in finally. pytest imports every
collected test_*.py into ONE shared process; the previous module-level stub
(2 symbols only) poisoned every later module resolving _rt → 69F/9E across
12 unrelated files. pytest collects 0 tests here (no test_ functions, no
import side effects); the file runs via run-invisible-suites.sh / direct
python3 (the zeta msg-20260716-114123 runner contract).
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent

# Pure containers — mutated only while main() runs. No module-level env,
# sys.path, or sys.modules mutation (guard-1165).
CALLS: list[tuple[str, str, str]] = []
RAISE: dict = {"exc": None}


class _StubRtError(RuntimeError):
    pass


def _stub_store_increment(store, rec_id, field):
    if RAISE["exc"] is not None:
        raise RAISE["exc"]
    CALLS.append((store, rec_id, field))


def _build_rt_stub() -> types.ModuleType:
    stub = types.ModuleType("_rt")
    stub.RtError = _StubRtError
    stub.store_increment = _stub_store_increment
    return stub


def _post(board, channel: str, tags: str, text: str) -> None:
    """Drive board.cmd_post in-process with stdin/stdout redirected."""
    ns = types.SimpleNamespace(channel=channel, author="test-agent",
                               type=None, reply_to=None, tags=tags)
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(text)
    sys.stdout = io.StringIO()
    try:
        board.cmd_post(ns)
    finally:
        sys.stdin, sys.stdout = old_in, old_out


_ENV_KEYS = ("MIND_WORLD", "MIND_META", "STORAGE_BACKEND", "MIND_AGENT")


def main() -> int:
    failed: list[str] = []

    # Save prior process state so the finally-restore leaves the interpreter
    # exactly as found (relevant when embedded in a shared runner process).
    saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    saved_rt = sys.modules.get("_rt")
    saved_board = sys.modules.get("board")
    path_inserted = False

    # Pin the environment BEFORE the board import: board.py resolves
    # WORLD_DIR from _paths at module import, and _fileops/storage_backend
    # read STORAGE_BACKEND at first use. main()-style file runs OUTSIDE
    # pytest — no conftest autouse pin (5 / guard-955).
    tmp = Path(tempfile.mkdtemp(prefix="board-attr-unit-"))
    try:
        os.environ["MIND_WORLD"] = (tmp / "world").as_posix()
        os.environ["MIND_META"] = (tmp / "meta").as_posix()
        os.environ["STORAGE_BACKEND"] = "local"
        os.environ.setdefault("MIND_AGENT", "test-board-attr-agent")
        (tmp / "world").mkdir(parents=True, exist_ok=True)
        (tmp / "meta").mkdir(parents=True, exist_ok=True)

        if str(CORE_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(CORE_SCRIPTS))
            path_inserted = True

        # Stub _rt BEFORE importing board: cmd_post lazy-imports _rt inside
        # the findings branch, which resolves from sys.modules first. Drop
        # any previously-imported board so its module state binds against
        # THIS tmp env, not a stale one.
        sys.modules["_rt"] = _build_rt_stub()
        sys.modules.pop("board", None)
        import board  # noqa: E402  (local import by design — see docstring)

        findings_path = tmp / "world" / "board" / "findings.jsonl"

        # ─── Case 1: findings + valid guard-NNN tag → one guardrails call ───
        CALLS.clear()
        _post(board, "findings", "fresh-eyes-code,guard-901,severity:constrains",
              "Test finding 1 — should attribute to guard-901.")
        if CALLS != [("guardrails", "guard-901",
                      "utilization.times_inferred_helpful")]:
            failed.append(f"Case 1: expected one guardrails call, got {CALLS}")

        # ─── Case 2: findings + valid rb-NNN tag → one reasoning-bank call ───
        CALLS.clear()
        _post(board, "findings", "fresh-eyes-code,rb-901,severity:informs",
              "Test finding 2 — should attribute to rb-901.")
        if CALLS != [("reasoning-bank", "rb-901",
                      "utilization.times_inferred_helpful")]:
            failed.append(f"Case 2: expected one reasoning-bank call, got {CALLS}")

        # ─── Case 3: multiple unique source tags → one call each, sorted ───
        CALLS.clear()
        _post(board, "findings",
              "fresh-eyes-code,guard-901,rb-901,severity:invalidates",
              "Test finding 3 — multi-source attribution.")
        if CALLS != [("guardrails", "guard-901",
                      "utilization.times_inferred_helpful"),
                     ("reasoning-bank", "rb-901",
                      "utilization.times_inferred_helpful")]:
            failed.append(f"Case 3: expected sorted guard+rb calls, got {CALLS}")

        # ─── Case 4: findings + non-source tags only → no calls ───
        CALLS.clear()
        _post(board, "findings",
              "fresh-eyes-code,severity:constrains,affects:core/scripts/foo.sh",
              "Test finding 4 — no source-tags, no attribution.")
        if CALLS:
            failed.append(f"Case 4: expected no calls, got {CALLS}")

        # ─── Case 5: NON-findings channel + source tag → no calls ───
        CALLS.clear()
        _post(board, "general", "guard-901,note",
              "Test 5 — guard-tagged general post should NOT attribute.")
        if CALLS:
            failed.append(f"Case 5: expected no calls on general channel, got {CALLS}")

        # ─── Case 6: 4-digit IDs attribute (1 regex regression) ───
        # The pre-fix \d{3} regex silently excluded every ID past 999 —
        # rb-3742 / guard-1151 are live current IDs.
        CALLS.clear()
        _post(board, "findings",
              "fresh-eyes-code,guard-1151,rb-3742,severity:constrains",
              "Test finding 6 — modern 4-digit IDs must attribute.")
        if CALLS != [("guardrails", "guard-1151",
                      "utilization.times_inferred_helpful"),
                     ("reasoning-bank", "rb-3742",
                      "utilization.times_inferred_helpful")]:
            failed.append(f"Case 6: expected 4-digit guard+rb calls, got {CALLS}")

        # ─── Case 7: duplicate tags dedup to one call ───
        CALLS.clear()
        _post(board, "findings", "guard-901,guard-901,fresh-eyes-code",
              "Test finding 7 — duplicate tags increment once.")
        if CALLS != [("guardrails", "guard-901",
                      "utilization.times_inferred_helpful")]:
            failed.append(f"Case 7: expected single deduped call, got {CALLS}")

        # ─── Case 8: fail-soft — increment error must not break the post ───
        CALLS.clear()
        RAISE["exc"] = _StubRtError("daemon unreachable (simulated)")
        old_err = sys.stderr
        sys.stderr = io.StringIO()
        try:
            _post(board, "findings", "guard-901", "Test finding 8 — fail-soft.")
            err_text = sys.stderr.getvalue()
        finally:
            sys.stderr = old_err
            RAISE["exc"] = None
        if "guard-901" not in err_text:
            failed.append(f"Case 8: expected visible stderr naming the cite, "
                          f"got {err_text!r}")

        # ─── Case 9: channel writes landed in the tmp world (sanity) ───
        if not findings_path.exists():
            failed.append("Case 9: findings.jsonl missing from tmp world — "
                          "cmd_post wrote elsewhere")
        else:
            n = len([ln for ln in findings_path.read_text(encoding="utf-8")
                     .splitlines() if ln.strip()])
            # Cases 1,2,3,4,6,7,8 posted to findings = 7 lines.
            if n != 7:
                failed.append(f"Case 9: expected 7 findings posts, got {n}")

        if failed:
            print("\n".join(f"FAIL: {f}" for f in failed), file=sys.stderr)
            return 1
        print("PASS: all 9 cases — attribution loop gates on findings channel, "
              "matches 3- and 4-digit IDs, dedups, fails soft, and routes via "
              "_rt.store_increment.")
        return 0

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # Restore sys.modules exactly as found (guard-1165: no residue).
        if saved_rt is not None:
            sys.modules["_rt"] = saved_rt
        else:
            sys.modules.pop("_rt", None)
        if saved_board is not None:
            sys.modules["board"] = saved_board
        else:
            sys.modules.pop("board", None)
        # Restore env pins.
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if path_inserted:
            try:
                sys.path.remove(str(CORE_SCRIPTS))
            except ValueError:
                pass


if __name__ == "__main__":
    sys.exit(main())
