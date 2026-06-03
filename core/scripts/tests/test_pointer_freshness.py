"""test_pointer_freshness.py — unit tests for the pointer-freshness checker.

Covers every branch of pointer_freshness.scan() and its helpers without ever
touching the live world directory or the daemon:

  - parse_marker: valid marker + missing-required-key rejection
  - normalize_and_hash: CRLF/LF line-ending equivalence (the whole point of
    normalization — editor churn must not register as drift)
  - slug_for: stem -> kebab slug
  - scan fresh: verified within max_age_days -> no-op, canonical not even hashed
  - scan auto-bump: stale + canonical UNCHANGED -> verified date rewritten to
    today; second scan sees it fresh (deterministic, no goal)
  - scan drift (dry-run): stale + canonical CHANGED -> status drift, NO bump,
    NO goal filed
  - scan canonical_missing: stale + canonical path gone -> logged, no goal
  - open_goal_exists: dedup read of world/agent aspirations.jsonl
  - scan drift dedup: file_drift_goal called once, second scan deduped (the
    real subprocess is monkeypatched to a daemon-free fake that appends the
    goal so the next scan's open-goal scan sees it)
  - reanchor: recompute marker sha256 + verified date so a drifted pointer
    becomes fresh again

Self-contained: every test runs against a tempfile world dir passed into scan().
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import pointer_freshness as pf  # noqa: E402

TODAY = "2026-05-30"


# ── helpers ──────────────────────────────────────────────────────────────

def _marker(canonical: str, sha: str, verified: str, max_age: int = 30,
            target_asp: str = "asp-999", source: str = "world") -> str:
    return (f'<!-- freshness-check: canonical="{canonical}" sha256="{sha}" '
            f'verified="{verified}" max_age_days="{max_age}" '
            f'target_aspiration="{target_asp}" target_source="{source}" -->')


def _write_pointer(world: Path, name: str, marker: str, body: str = "") -> Path:
    p = world / "conventions" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {name}\n\n{marker}\n\n{body}\n", encoding="utf-8")
    return p


def _write_canonical(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


# ── marker parsing ───────────────────────────────────────────────────────

def test_parse_marker_valid():
    line = _marker("C:/x/canon.md", "abc123", "2026-05-01")
    m = pf.parse_marker(line)
    assert m is not None
    assert m["canonical"] == "C:/x/canon.md"
    assert m["sha256"] == "abc123"
    assert m["verified"] == "2026-05-01"
    assert m["max_age_days"] == "30"
    assert m["target_aspiration"] == "asp-999"


def test_parse_marker_missing_required_key_returns_none():
    # No sha256 -> rejected.
    line = '<!-- freshness-check: canonical="x.md" verified="2026-05-01" max_age_days="30" -->'
    assert pf.parse_marker(line) is None
    # No freshness-check token at all -> None.
    assert pf.parse_marker("just a normal comment") is None


# ── hashing ──────────────────────────────────────────────────────────────

def test_normalize_and_hash_crlf_lf_equivalent():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lf = tmp / "lf.md"
        crlf = tmp / "crlf.md"
        lf.write_bytes(b"line one\nline two\nline three\n")
        crlf.write_bytes(b"line one\r\nline two\r\nline three\r\n")
        assert pf.normalize_and_hash(lf) == pf.normalize_and_hash(crlf)


def test_normalize_and_hash_missing_returns_none():
    assert pf.normalize_and_hash("C:/definitely/not/here-zzz.md") is None


def test_slug_for():
    assert pf.slug_for("C:/a/b/mycelium-api-impl.md") == "mycelium-api-impl"
    assert pf.slug_for("Cross_Repo Lodestar.md") == "cross-repo-lodestar"


# ── scan: fresh ──────────────────────────────────────────────────────────

def test_scan_fresh_skips_hash():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        world = tmp / "world"
        canon = _write_canonical(tmp, "canon.md", "stable content\n")
        h = pf.normalize_and_hash(canon)
        _write_pointer(world, "ptr.md", _marker(str(canon), h, TODAY))
        out = pf.scan(world, today=TODAY, dry_run=True)
        assert out["summary"]["fresh"] == 1
        r = out["results"][0]
        assert r["status"] == "fresh"
        # Fresh path is cheap: canonical never hashed.
        assert r["current_hash"] is None


# ── scan: auto-bump (stale + unchanged) ──────────────────────────────────

def test_scan_stale_match_bumps_and_becomes_fresh():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        world = tmp / "world"
        canon = _write_canonical(tmp, "canon.md", "stable content\n")
        h = pf.normalize_and_hash(canon)
        ptr = _write_pointer(world, "ptr.md",
                             _marker(str(canon), h, "2000-01-01"),
                             body="Last verified: 2000-01-01")
        # Dry-run: would bump but does not write.
        out = pf.scan(world, today=TODAY, dry_run=True)
        assert out["summary"]["bumped"] == 1
        assert 'verified="2000-01-01"' in ptr.read_text(encoding="utf-8")

        # Real run: rewrites the marker date AND the human "Last verified" line.
        out = pf.scan(world, today=TODAY, dry_run=False)
        assert out["summary"]["bumped"] == 1
        text = ptr.read_text(encoding="utf-8")
        assert f'verified="{TODAY}"' in text
        assert f"Last verified: {TODAY}" in text
        assert "2000-01-01" not in text

        # Second run: now fresh, no further action.
        out2 = pf.scan(world, today=TODAY, dry_run=False)
        assert out2["summary"]["fresh"] == 1
        assert out2["summary"]["bumped"] == 0


# ── scan: drift (dry-run, no goal) ───────────────────────────────────────

def test_scan_drift_dryrun_no_bump_no_goal():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        world = tmp / "world"
        canon = _write_canonical(tmp, "canon.md", "original content\n")
        h = pf.normalize_and_hash(canon)
        ptr = _write_pointer(world, "ptr.md", _marker(str(canon), h, "2000-01-01"))
        # Canonical changes AFTER the marker was anchored -> drift.
        canon.write_text("CHANGED content — much longer now\n", encoding="utf-8")
        out = pf.scan(world, today=TODAY, dry_run=True)
        r = out["results"][0]
        assert r["status"] == "drift"
        assert r["goal_filed"] is False
        # Drift never bumps the date — the stale signal must survive.
        assert 'verified="2000-01-01"' in ptr.read_text(encoding="utf-8")


# ── scan: canonical missing ──────────────────────────────────────────────

def test_scan_canonical_missing():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        world = tmp / "world"
        _write_pointer(world, "ptr.md",
                       _marker("C:/gone/missing-zzz.md", "deadbeef", "2000-01-01"))
        out = pf.scan(world, today=TODAY, dry_run=False)
        r = out["results"][0]
        assert r["status"] == "canonical_missing"
        assert r["goal_filed"] is False


# ── dedup read ───────────────────────────────────────────────────────────

def test_open_goal_exists():
    with tempfile.TemporaryDirectory() as d:
        world = Path(d) / "world"
        world.mkdir(parents=True)
        asp = world / "aspirations.jsonl"
        sig = "investigate:freshness-drift-ptr"
        rows = [
            {"id": "asp-a", "status": "active", "goals": [
                {"id": "g-1", "origin_signal": sig, "status": "pending"}]},
            {"id": "asp-b", "status": "active", "goals": [
                {"id": "g-2", "origin_signal": "other:thing", "status": "pending"}]},
        ]
        asp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        assert pf.open_goal_exists(sig, world, None) is True
        assert pf.open_goal_exists("investigate:freshness-drift-nope", world, None) is False

        # A completed goal with the signal does NOT count as open.
        asp.write_text(json.dumps(
            {"id": "asp-a", "status": "active", "goals": [
                {"id": "g-1", "origin_signal": sig, "status": "completed"}]}) + "\n",
            encoding="utf-8")
        assert pf.open_goal_exists(sig, world, None) is False


# ── drift: files once, then dedupes ──────────────────────────────────────

def test_scan_drift_files_once_then_dedupes(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        world = tmp / "world"
        world.mkdir(parents=True)
        asp = world / "aspirations.jsonl"
        asp.write_text("", encoding="utf-8")
        canon = _write_canonical(tmp, "canon.md", "original\n")
        h = pf.normalize_and_hash(canon)
        _write_pointer(world, "ptr.md", _marker(str(canon), h, "2000-01-01"))
        canon.write_text("DRIFTED\n", encoding="utf-8")

        calls = {"n": 0}

        # Daemon-free fake: record the call and append an OPEN goal carrying the
        # drift origin_signal, so the next scan's open_goal_exists sees it.
        def fake_file_drift_goal(pointer_file, marker, slug, project_root):
            calls["n"] += 1
            sig = f"investigate:freshness-drift-{slug}"
            with open(asp, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"id": f"asp-fake-{calls['n']}", "status": "active", "goals": [
                        {"id": f"g-{calls['n']}", "origin_signal": sig,
                         "status": "pending"}]}) + "\n")
            return {"filed": True, "goal_id": f"g-{calls['n']}", "error": None}

        monkeypatch.setattr(pf, "file_drift_goal", fake_file_drift_goal)

        out1 = pf.scan(world, today=TODAY, dry_run=False)
        assert out1["results"][0]["status"] == "drift"
        assert out1["results"][0]["goal_filed"] is True
        assert calls["n"] == 1

        out2 = pf.scan(world, today=TODAY, dry_run=False)
        assert out2["results"][0]["status"] == "drift"
        assert out2["results"][0]["dedup_skipped"] is True
        assert calls["n"] == 1  # not called again


# ── reanchor ─────────────────────────────────────────────────────────────

def test_reanchor_makes_drifted_pointer_fresh():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        world = tmp / "world"
        canon = _write_canonical(tmp, "canon.md", "original\n")
        h = pf.normalize_and_hash(canon)
        ptr = _write_pointer(world, "ptr.md", _marker(str(canon), h, "2000-01-01"))
        canon.write_text("DRIFTED content\n", encoding="utf-8")

        # Confirm drift first.
        assert pf.scan(world, today=TODAY, dry_run=True)["results"][0]["status"] == "drift"

        res = pf.reanchor(ptr, today=TODAY)
        assert res["ok"] is True
        assert res["new_hash"] == pf.normalize_and_hash(canon)

        # Reanchored marker now matches; pointer is fresh.
        out = pf.scan(world, today=TODAY, dry_run=True)
        assert out["results"][0]["status"] == "fresh"
        text = ptr.read_text(encoding="utf-8")
        assert f'verified="{TODAY}"' in text
        assert res["new_hash"] in text


def test_discover_multiline_marker():
    # The module docstring documents a 2-line marker; discovery must find it.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        world = tmp / "world"
        canon = _write_canonical(tmp, "canon.md", "c\n")
        h = pf.normalize_and_hash(canon)
        multiline = (f'<!-- freshness-check: canonical="{canon}" sha256="{h}"\n'
                     f'     verified="{TODAY}" max_age_days="30" target_aspiration="asp-9" -->')
        _write_pointer(world, "ml.md", multiline)
        found = pf.discover_pointers(world)
        assert len(found) == 1
        assert found[0]["marker"]["max_age_days"] == "30"
        assert found[0]["marker"]["canonical"] == str(canon)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
