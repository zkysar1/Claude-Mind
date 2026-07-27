"""test_pending_questions_add.py — regression for .

pending-questions-add.sh must tolerate the same on-disk container shapes its
reader sibling pending-questions-sweep.py reads (rb-1786 — a paired
reader/writer over one store MUST tolerate the same shapes):

    shape A:   {"questions": [ {...}, ... ]}            dict wrapper
    shape B:   [ {"questions": [ {...}, ... ]}, ... ]   list with wrapper element
    shape C:   [ {"id": ...}, {"id": ...}, ... ]        bare list of entry dicts
    mixed B+C: a list carrying BOTH a wrapper element and bare entry dicts
               (alpha's real on-disk file, 2026-06-14 audit)

Before the fix, add.sh's two-shape `get_list()` rejected a pure bare list with
"schema not recognized" (exit 2) and silently ignored the bare entries in a
mixed list (so the duplicate-id check had a hole there).

The script's YAML logic lives in an inline `python3 - <<'PYEOF' ... PYEOF`
heredoc (not an importable module), so this test EXTRACTS that heredoc and runs
it as a subprocess against an isolated tmp PQ file (guard-692: never touch the
real agent file; guard-759: tmp_path/tempfile, not a hard-coded /tmp), passing
inputs via env exactly as the wrapper does.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ADD_SCRIPT = CORE_SCRIPTS / "pending-questions-add.sh"


def _extract_heredoc() -> str:
    """Return the inline Python body between `<<'PYEOF'` and the closing PYEOF."""
    text = ADD_SCRIPT.read_text(encoding="utf-8")
    marker = "<<'PYEOF'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nPYEOF", start)
    return text[start:end]


_HEREDOC = _extract_heredoc()


def _run(pq_path: Path, entry_id: str,
         question: str = "q?", default_action: str = "da",
         priority: str = "", qtype: str = "", context: str = ""):
    """Run the extracted inline Python against pq_path with the wrapper's env."""
    env = {
        "PQ_FILE": str(pq_path),
        "ID": entry_id,
        "QUESTION": question,
        "DEFAULT_ACTION": default_action,
        "PRIORITY": priority,
        "TYPE": qtype,
        "CONTEXT": context,
    }
    return subprocess.run(
        [sys.executable, "-c", _HEREDOC],
        env=env, capture_output=True, text=True,
    )


def _write(pq_path: Path, data) -> None:
    with open(pq_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def _ids_in(pq_path: Path) -> list[str]:
    """Flatten all entry ids the way the reader does (shape-agnostic)."""
    data = yaml.safe_load(pq_path.read_text(encoding="utf-8"))
    out: list[str] = []
    if isinstance(data, dict):
        items = data.get("questions", []) or []
        out += [e.get("id") for e in items if isinstance(e, dict)]
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("questions"), list):
                out += [e.get("id") for e in item["questions"]
                        if isinstance(e, dict)]
            elif "id" in item or "question" in item:
                out.append(item.get("id"))
    return out


# --- Case 1: shape C (bare list) is accepted and shape preserved (the fix) ---

def test_shape_c_bare_list_appends_and_preserves_shape():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pending-questions.yaml"
        _write(pq, [{"id": "pq-a", "question": "x"},
                    {"id": "pq-b", "question": "y"}])
        r = _run(pq, "pq-c")
        assert r.returncode == 0, f"shape C rejected: rc={r.returncode} {r.stderr!r}"
        data = yaml.safe_load(pq.read_text(encoding="utf-8"))
        assert isinstance(data, list), "shape C must stay a bare list (no normalization)"
        assert "pq-c" in _ids_in(pq)
        # new entry appended as a top-level list element, not wrapped
        assert any(isinstance(e, dict) and e.get("id") == "pq-c" for e in data)


# --- Case 2: shape A (dict wrapper) still works (regression guard) -----------

def test_shape_a_dict_wrapper_appends():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pending-questions.yaml"
        _write(pq, {"questions": [{"id": "pq-a", "question": "x"}]})
        r = _run(pq, "pq-b")
        assert r.returncode == 0, f"shape A failed: rc={r.returncode} {r.stderr!r}"
        data = yaml.safe_load(pq.read_text(encoding="utf-8"))
        assert isinstance(data, dict), "shape A must stay a dict"
        assert set(_ids_in(pq)) == {"pq-a", "pq-b"}


# --- Case 3: shape B (list with wrapper element) still works -----------------

def test_shape_b_wrapper_element_appends_into_wrapper():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pending-questions.yaml"
        _write(pq, [{"questions": [{"id": "pq-a", "question": "x"}]}])
        r = _run(pq, "pq-b")
        assert r.returncode == 0, f"shape B failed: rc={r.returncode} {r.stderr!r}"
        data = yaml.safe_load(pq.read_text(encoding="utf-8"))
        assert isinstance(data, list) and "questions" in data[0], \
            "shape B must stay a list carrying a wrapper element"
        assert "pq-b" in data[0]["questions"][-1]["id"]
        assert set(_ids_in(pq)) == {"pq-a", "pq-b"}


# --- Case 4: mixed B+C — dup check sees bare entries; non-dup goes to wrapper -

def test_mixed_shape_dup_check_sees_bare_entries():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pending-questions.yaml"
        _write(pq, [{"questions": [{"id": "pq-wrapped", "question": "x"}]},
                    {"id": "pq-bare", "question": "y"}])
        # Adding a duplicate of the BARE entry must be refused (the flattened
        # dup check — pre-fix this slipped through, only the wrapper was scanned).
        dup = _run(pq, "pq-bare")
        assert dup.returncode == 2, \
            f"duplicate bare id must be refused; rc={dup.returncode} {dup.stderr!r}"
        assert "duplicate" in dup.stderr.lower()
        # A genuinely new id is accepted (appended into the first wrapper).
        ok = _run(pq, "pq-new")
        assert ok.returncode == 0, f"new id rejected: rc={ok.returncode} {ok.stderr!r}"
        assert "pq-new" in _ids_in(pq)


# --- Case 5: a genuinely unrecognized container is still rejected ------------

def test_unrecognized_shape_rejected():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pending-questions.yaml"
        # dict WITHOUT a `questions:` key — neither A, B, nor C.
        _write(pq, {"not_questions": [{"id": "pq-a"}]})
        r = _run(pq, "pq-b")
        assert r.returncode == 2, \
            f"unrecognized shape must be rejected; rc={r.returncode} {r.stderr!r}"
        assert "schema not recognized" in r.stderr.lower()


# --- Case 6: EMPTY file initializes as shape C, visible to naive consumers ----
# Regression guard for . yaml.safe_load("") is None, which hits the
# `data is None` initializer branch. That branch MUST seed a bare list (shape C),
# NOT a dict wrapper (shape A) — a fresh dict-wrapper file is invisible to the
# naive top-level-status scans in the user-facing consumers
# (agent-completion-report / open-questions / respond), which was alpha's
# 19-of-21 hidden-pending incident. If someone reverts the initializer to
# `data = {"questions": []}`, this test fails.

def test_empty_file_initializes_as_bare_list_seen_by_naive_filter():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pending-questions.yaml"
        pq.write_text("", encoding="utf-8")           # empty → yaml.safe_load == None
        r = _run(pq, "pq-fresh")
        assert r.returncode == 0, f"empty-file add failed: rc={r.returncode} {r.stderr!r}"
        data = yaml.safe_load(pq.read_text(encoding="utf-8"))
        assert isinstance(data, list), \
            "empty file must initialize as a bare list (shape C), not a dict wrapper"
        # The naive top-level-status filter the user-facing consumers use: iterate
        # the top-level list, keep dict entries whose status == pending. A dict
        # wrapper would make this scan iterate KEYS and see nothing (the bug).
        naive_pending = [q for q in data
                         if isinstance(q, dict) and q.get("status") == "pending"]
        assert [q["id"] for q in naive_pending] == ["pq-fresh"], \
            "naive top-level-status filter must see the freshly-added pending question"
        # A second add preserves shape C and stays visible.
        r2 = _run(pq, "pq-fresh-2")
        assert r2.returncode == 0, f"second add failed: rc={r2.returncode} {r2.stderr!r}"
        data2 = yaml.safe_load(pq.read_text(encoding="utf-8"))
        assert isinstance(data2, list) and set(_ids_in(pq)) == {"pq-fresh", "pq-fresh-2"}


if __name__ == "__main__":
    test_shape_c_bare_list_appends_and_preserves_shape()
    test_shape_a_dict_wrapper_appends()
    test_shape_b_wrapper_element_appends_into_wrapper()
    test_mixed_shape_dup_check_sees_bare_entries()
    test_unrecognized_shape_rejected()
    test_empty_file_initializes_as_bare_list_seen_by_naive_filter()
    print("ok")
