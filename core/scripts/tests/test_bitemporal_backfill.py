"""Unit tests for the bi-temporal valid_from backfill migration ().

Guards the pure projection + no-corruption verifier in core/scripts/bitemporal-backfill.py.
The module name is hyphenated (not import-safe), so load it via importlib.

These cover the invariants the live migration relied on (2685 real records,
post-apply verify clean) so the logic stays correct when g-306-38 extends the
script with a tree-front-matter mode.
"""
import importlib.util
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent.parent / "bitemporal-backfill.py"
_spec = importlib.util.spec_from_file_location("bitemporal_backfill", _MOD_PATH)
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)


def test_project_sets_valid_from_to_created_when_null():
    recs = [{"id": "rb-1", "created": "2026-03-26", "valid_from": None, "valid_to": None, "x": 1}]
    out, changed, skipped = bb._project(recs)
    assert changed == 1
    assert skipped == []
    assert out[0]["valid_from"] == "2026-03-26"
    assert out[0]["valid_to"] is None  # valid_to never touched
    assert out[0]["x"] == 1  # non-temporal content preserved


def test_project_idempotent_skips_already_set():
    recs = [{"id": "rb-1", "created": "2026-03-26", "valid_from": "2026-03-26", "valid_to": None}]
    out, changed, skipped = bb._project(recs)
    assert changed == 0  # already set -> no change (idempotent)
    assert out[0]["valid_from"] == "2026-03-26"


def test_project_skips_record_without_created():
    recs = [{"id": "rb-1", "valid_from": None, "valid_to": None}]  # no `created`
    out, changed, skipped = bb._project(recs)
    assert changed == 0
    assert skipped == ["rb-1"]
    assert out[0]["valid_from"] is None  # not fabricated


def test_project_does_not_mutate_input():
    recs = [{"id": "rb-1", "created": "2026-03-26", "valid_from": None, "valid_to": None}]
    bb._project(recs)
    assert recs[0]["valid_from"] is None  # original untouched (deep-ish copy)


def test_verify_passes_on_clean_projection():
    pre = [{"id": "rb-1", "created": "2026-03-26", "valid_from": None, "valid_to": None, "body": "z"}]
    post, _c, _s = bb._project(pre)
    ok, problems = bb._verify(pre, post)
    assert ok, problems


def test_verify_catches_content_change():
    pre = [{"id": "rb-1", "created": "2026-03-26", "valid_from": None, "valid_to": None, "body": "z"}]
    post = [{"id": "rb-1", "created": "2026-03-26", "valid_from": "2026-03-26", "valid_to": None, "body": "TAMPERED"}]
    ok, problems = bb._verify(pre, post)
    assert not ok
    assert any("CONTENT-CHANGED" in p for p in problems)


def test_verify_catches_deletion():
    pre = [{"id": "rb-1", "created": "2026-03-26", "valid_from": None, "valid_to": None},
           {"id": "rb-2", "created": "2026-03-27", "valid_from": None, "valid_to": None}]
    post = [{"id": "rb-1", "created": "2026-03-26", "valid_from": "2026-03-26", "valid_to": None}]
    ok, problems = bb._verify(pre, post)
    assert not ok
    assert any("DELETED" in p and "rb-2" in p for p in problems)


def test_verify_allows_concurrent_append():
    # A new record landing post-lock is NOT corruption.
    pre = [{"id": "rb-1", "created": "2026-03-26", "valid_from": None, "valid_to": None}]
    post = [{"id": "rb-1", "created": "2026-03-26", "valid_from": "2026-03-26", "valid_to": None},
            {"id": "rb-2", "created": "2026-03-27", "valid_from": None, "valid_to": None}]  # concurrent append
    ok, problems = bb._verify(pre, post)
    assert ok, problems


def test_verify_catches_valid_to_mutation():
    pre = [{"id": "rb-1", "created": "2026-03-26", "valid_from": None, "valid_to": None}]
    post = [{"id": "rb-1", "created": "2026-03-26", "valid_from": "2026-03-26", "valid_to": "2026-06-01"}]
    ok, problems = bb._verify(pre, post)
    assert not ok
    assert any("VALID_TO-MUTATED" in p for p in problems)


def test_verify_catches_valid_from_overwrite():
    # An existing explicit valid_from must never be overwritten.
    pre = [{"id": "rb-1", "created": "2026-03-26", "valid_from": "2026-01-01", "valid_to": None}]
    post = [{"id": "rb-1", "created": "2026-03-26", "valid_from": "2026-03-26", "valid_to": None}]
    ok, problems = bb._verify(pre, post)
    assert not ok
    assert any("VALID_FROM-OVERWRITE" in p for p in problems)


# ---------------------------------------------------------------------------
# Tree-front-matter mode (). Pure byte-level projector + verifier —
# hermetic (no disk, no daemon). Byte-identity is the hard requirement, so the
# CRLF-preservation + body-hash checks below are load-bearing.
# ---------------------------------------------------------------------------

_FM_LF = (
    b"---\n"
    b"key: demo\n"
    b'created: "2026-05-07"\n'
    b'last_updated: "2026-06-02"\n'
    b"---\n"
    b"\n"
    b"# Demo Node\n"
    b"Body line with unicode: \xe2\x9c\x93\n"  # U+2713 CHECK MARK
)


def test_tree_project_inserts_valid_from_after_created_lf():
    post, changed, reason = bb._project_tree_md(_FM_LF)
    assert changed is True
    assert reason == "inserted"
    text = post.decode("utf-8")
    assert 'valid_from: "2026-05-07"\n' in text
    lines = text.splitlines()
    ci = lines.index('created: "2026-05-07"')
    assert lines[ci + 1] == 'valid_from: "2026-05-07"'  # right after created
    ok, problems = bb._verify_tree_md(_FM_LF, post)
    assert ok, problems


def test_tree_project_preserves_crlf():
    raw = _FM_LF.replace(b"\n", b"\r\n")
    post, changed, _r = bb._project_tree_md(raw)
    assert changed is True
    assert b'valid_from: "2026-05-07"\r\n' in post
    # no lone LF introduced — every newline is part of a CRLF pair
    assert post.count(b"\n") == post.count(b"\r\n")
    ok, problems = bb._verify_tree_md(raw, post)
    assert ok, problems


def test_tree_project_body_byte_identical():
    post, _c, _r = bb._project_tree_md(_FM_LF)
    assert bb._body_after_frontmatter(post) == bb._body_after_frontmatter(_FM_LF)


def test_tree_project_idempotent_when_valid_from_present():
    raw = (b"---\n"
           b'created: "2026-05-07"\n'
           b'valid_from: "2026-05-07"\n'
           b"---\nbody\n")
    post, changed, reason = bb._project_tree_md(raw)
    assert changed is False
    assert reason == "already-has-valid_from"
    assert post == raw


def test_tree_project_skips_when_no_created():
    raw = b'---\nkey: demo\nlast_updated: "2026-06-02"\n---\nbody\n'
    post, changed, reason = bb._project_tree_md(raw)
    assert changed is False
    assert reason == "no-created"
    assert post == raw  # NOT fabricated from last_updated


def test_tree_project_bare_created_value():
    raw = b"---\ncreated: 2026-05-07\n---\nbody\n"
    post, changed, _r = bb._project_tree_md(raw)
    assert changed is True
    assert b"valid_from: 2026-05-07\n" in post  # bare value copied verbatim
    ok, problems = bb._verify_tree_md(raw, post)
    assert ok, problems


def test_tree_project_ignores_created_in_body():
    # `created:` only in the body, never the front matter -> no change.
    raw = b"---\nkey: demo\n---\nThis text mentions\ncreated: something\n"
    _post, changed, reason = bb._project_tree_md(raw)
    assert changed is False
    assert reason == "no-created"


def test_tree_project_no_front_matter():
    raw = b"# Just a body\nno front matter here\n"
    _post, changed, reason = bb._project_tree_md(raw)
    assert changed is False
    assert reason == "no-front-matter"


def test_tree_verify_catches_body_change():
    post, _c, _r = bb._project_tree_md(_FM_LF)
    tampered = post.replace(b"# Demo Node", b"# TAMPERED Node")
    ok, problems = bb._verify_tree_md(_FM_LF, tampered)
    assert not ok
    assert any("BODY-HASH-CHANGED" in p for p in problems)


def test_tree_verify_catches_extra_frontmatter_change():
    # A change beyond the single valid_from insertion must be rejected.
    post, _c, _r = bb._project_tree_md(_FM_LF)
    tampered = post.replace(b"key: demo", b"key: changed")
    ok, problems = bb._verify_tree_md(_FM_LF, tampered)
    assert not ok
    assert any("RECONSTRUCT-MISMATCH" in p for p in problems)
