"""Tests for knowledge-export — the store-reader that bridges the pure
:mod:`knowledge_projection` core to the real Mind stores.

Hermetic: every test builds a tiny temp world (tree yaml + 3 JSONL) and a fake
project root, so nothing depends on the box's actual stores. The security
properties (framework suppression, agent-name / path / secret redaction) are
re-asserted end-to-end through the reader, because that is where store-shape
mistakes would re-leak what the pure core already knows to strip.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
EXPORT_PY = SCRIPT_DIR.parent / "knowledge-export.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("knowledge_export_under_test", EXPORT_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_mod()

CONFORMANCE_PY = SCRIPT_DIR.parent / "okf-bundle-conformance.py"


def _load_conformance_mod():
    """The shared OKF shape checker — same hyphenated-filename dance as _load_mod."""
    spec = importlib.util.spec_from_file_location("okf_conformance_under_test", CONFORMANCE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── fixture builder ──────────────────────────────────────────────────────────

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


# Basename of the knowledge-tree index. Named once so the fixtures below and the
# assertions that check a degraded entry's `path` cannot drift apart.
TREE_INDEX_BASENAME = "_tree.yaml"


def _build_world(root: Path, *, write_bodies: bool = False) -> Path:
    """Create a minimal but realistic world/ tree + three JSONL stores under root.

    ``write_bodies=True`` also writes the two node ``.md`` files on disk so
    ``read_tree_nodes`` can carry their body. Default off keeps the legacy tests
    (which only exercise the ``_tree.yaml`` summary) byte-for-byte unchanged.
    """
    world = root / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (root / "agents" / "alpha").mkdir(parents=True)  # → agent name "alpha"

    tree = {
        "nodes": {
            "coral-reefs": {
                "summary": "Reefs studied by alpha at /home/ec2/world data.",
                "parent": "marine-biology",
                "children": ["bleaching"],
                "file": "world/knowledge/tree/marine-biology/coral-reefs.md",
                # Date-only, as the live index stores it (1379/1379 nodes, measured
                # 2026-08-12). 9-hook-internals below deliberately OMITS it, so the
                # two-node fixture carries its own negative control.
                "last_updated": "2026-04-28",
            },
            "9-hook-internals": {  # leading digit exercises _humanize_key
                "summary": "framework plumbing per guard-321",
                "parent": "system",
                "children": [],
                "file": "world/knowledge/tree/system/hook-internals.md",
            },
        }
    }
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump(tree), encoding="utf-8")

    if write_bodies:
        # Write the actual node .md files so read_tree_nodes can carry the body.
        # The reef body names alpha + an absolute path so redaction has something to strip;
        # the system body carries a framework identifier that must NEVER be read/exposed.
        reef_md = tree_dir / "marine-biology" / "coral-reefs.md"
        reef_md.parent.mkdir(parents=True, exist_ok=True)
        reef_md.write_text(
            "---\n"
            "topic: Coral reefs\n"
            "last_updated: '2026-07-20'\n"
            "---\n\n"
            "Coral reefs studied by alpha at /home/ec2/world host a quarter of marine\n"
            "species. Bleaching is a warming signal that alpha tracks.\n",
            encoding="utf-8",
        )
        hook_md = tree_dir / "system" / "hook-internals.md"
        hook_md.parent.mkdir(parents=True, exist_ok=True)
        hook_md.write_text(
            "---\ntopic: Hook internals\n---\n\n"
            "Framework plumbing internals per guard-321 in _paths.py — never expose.\n",
            encoding="utf-8",
        )

    _write_jsonl(
        world / "reasoning-bank.jsonl",
        [
            {"applies_to": "domain", "category": "marine-biology/method",
             "title": "Cross-check sources",
             "failure_lesson": "Trusted one source once; it was wrong."},
            {"applies_to": "framework", "category": "framework-architecture",
             "title": "Never inline $VAR",
             "failure_lesson": "guard-165 plumbing detail in _paths.py"},
        ],
    )
    _write_jsonl(
        world / "guardrails.jsonl",
        [
            {"category": "marine-biology/method", "rule": "Verify every claim against two sources."},
            {"category": "framework-architecture", "rule": "Never critical() in a handler."},
            {"rule": "Untagged guardrail — must fail closed."},  # no category
        ],
    )
    _write_jsonl(
        world / "pipeline.jsonl",
        [
            {"category": "marine-biology/reefs", "claim": "Warmer water bleaches reefs faster.",
             "horizon": "short", "stage": "resolved", "outcome": "Confirmed by alpha."},
            {"category": "system/loop", "claim": "The veto budget bounds continuations.",
             "horizon": "session", "stage": "active", "outcome": ""},
        ],
    )
    return world


# ── store-reader unit helpers ────────────────────────────────────────────────

def test_read_jsonl_skips_blank_and_bad_lines(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text('{"a": 1}\n\n  \nnot json\n{"b": 2}\n[1,2]\n', encoding="utf-8")
    assert M._read_jsonl(p) == [{"a": 1}, {"b": 2}]  # bad line + non-dict [1,2] dropped


def test_read_jsonl_missing_file_is_empty(tmp_path: Path) -> None:
    assert M._read_jsonl(tmp_path / "nope.jsonl") == []


def test_humanize_key_strips_leading_number_and_dashes() -> None:
    assert M._humanize_key("9-action-matrix-design") == "Action matrix design"
    assert M._humanize_key("coral-reefs") == "Coral reefs"
    assert M._humanize_key("deep_sea") == "Deep sea"
    assert M._humanize_key("") == ""


def test_read_tree_nodes_shapes_records(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    nodes = {n["key"]: n for n in M.read_tree_nodes(world)}
    assert set(nodes) == {"coral-reefs", "9-hook-internals"}
    reef = nodes["coral-reefs"]
    assert reef["title"] == "Coral reefs"
    assert reef["parent"] == "marine-biology"
    assert reef["children"] == ["bleaching"]
    # category carries the file path so the projection's system/-suppression works.
    assert reef["category"] == "world/knowledge/tree/marine-biology/coral-reefs.md"
    # Carried off the INDEX entry, not the node's front matter — a wiki client's
    # "changed since your last visit" is keyed on it, and this reader dropped it
    # entirely until . Negative control on the next line: an index entry
    # without the key must yield "", never a fabricated or plausible-looking date,
    # because the consumer treats "" as unknown and stays silent rather than
    # claiming nothing changed (guard-3221).
    assert reef["last_updated"] == "2026-04-28"
    assert nodes["9-hook-internals"]["last_updated"] == ""


def test_read_tree_nodes_missing_yaml_is_empty(tmp_path: Path) -> None:
    assert M.read_tree_nodes(tmp_path / "world") == []


def test_read_tree_nodes_reads_body_for_domain_only(tmp_path: Path) -> None:
    world = _build_world(tmp_path, write_bodies=True)
    nodes = {n["key"]: n for n in M.read_tree_nodes(world)}
    reef = nodes["coral-reefs"]
    # Domain node: full body read, front matter stripped, and distinct from the summary.
    assert "host a quarter of marine" in reef["body"]
    assert reef["body"] != reef["summary"]
    assert "topic:" not in reef["body"]        # YAML front matter stripped
    assert "last_updated" not in reef["body"]
    # System node: body NOT read (projection suppresses it downstream; defense in depth).
    assert nodes["9-hook-internals"]["body"] == ""


def test_read_tree_nodes_body_empty_when_md_absent(tmp_path: Path) -> None:
    # No .md files on disk (write_bodies=False) → body defaults to "" (export never fails).
    world = _build_world(tmp_path)
    nodes = {n["key"]: n for n in M.read_tree_nodes(world)}
    assert nodes["coral-reefs"]["body"] == ""


# ── index-shape robustness () ────────────────────────────────────────
# Three index SHAPES reach this reader in production, measured across 18 sidecar
# worlds: the canonical dict-valued mapping (covered above), a flat string-valued
# mapping, and an index that will not parse at all. The first two must yield nodes;
# the third must yield a MARKED empty result rather than an exception.
#
# Every malformed member gets its own case rather than one test named for the class:
# a single "malformed index" test reads as full coverage to anyone scanning the
# suite while covering one member of four (guard-2441). They fail through three
# DIFFERENT code paths — a YAML parse error, valid YAML whose root is not a mapping,
# and a decode error that is a ValueError and not an OSError.


def _string_valued_world(root: Path, mapping: dict[str, str]) -> Path:
    """A world whose tree index maps each key straight to a filename STRING."""
    world = root / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / '_tree.yaml').write_text(
        yaml.safe_dump({"nodes": dict(mapping)}), encoding="utf-8"
    )
    return world


def _raw_index_world(root: Path, raw: bytes) -> Path:
    """A world whose tree index is written as RAW BYTES — valid or not."""
    world = root / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / '_tree.yaml').write_bytes(raw)
    return world


def test_read_tree_nodes_coerces_string_valued_index(tmp_path: Path) -> None:
    """DEFECT B: a flat ``<key>: "<file>.md"`` index yielded ZERO nodes.

    Two of 18 live sidecar indexes carry this shape (33 and 12 nodes), and every node
    was dropped by the ``isinstance(node, dict)`` guard — so the export read tree=0
    against a perfectly healthy index and the bundle was byte-indistinguishable from
    an empty world's.
    """
    world = _string_valued_world(
        tmp_path, {"coral-reefs": "coral-reefs.md", "kelp": "kelp.md"}
    )
    nodes = {n["key"]: n for n in M.read_tree_nodes(world)}
    assert set(nodes) == {"coral-reefs", "kelp"}, "string-valued nodes were dropped"
    # The coerced node carries `file` as its category, which is what every downstream
    # classifier reads; summary/parent/children fall back to their empty defaults.
    assert nodes["kelp"]["category"] == "kelp.md"
    assert nodes["kelp"]["summary"] == ""
    assert nodes["kelp"]["children"] == []


def test_read_tree_nodes_string_valued_index_still_suppresses_framework(
    tmp_path: Path,
) -> None:
    """The coercion must not become a framework-body leak.

    ``top_level_category`` strips the extension, so a bare ``system.md`` still reads
    as the framework root and its body is never loaded — the same defense-in-depth
    the dict-valued path relies on. This is the security-relevant half of DEFECT B:
    a coercion that classified every flat node as domain would expose framework
    bodies to the kid-facing wiki.
    """
    world = _string_valued_world(tmp_path, {"system": "system.md", "reefs": "reefs.md"})
    tree_dir = world / "knowledge" / "tree"
    (tree_dir / "system.md").write_text("Framework internals — never expose.\n", "utf-8")
    (tree_dir / "reefs.md").write_text("Reefs are lovely.\n", "utf-8")
    nodes = {n["key"]: n for n in M.read_tree_nodes(world)}
    assert nodes["system"]["body"] == "", "framework body was read through the coercion"
    assert "Reefs are lovely" in nodes["reefs"]["body"], "domain body was not read"


def test_read_tree_nodes_healthy_index_leaves_status_untouched(tmp_path: Path) -> None:
    """POSITIVE CONTROL for the degrade cases below.

    Without it, every one of them would also pass against a reader that degraded
    unconditionally — a zero from a broken function and a zero from a working one are
    textually identical.
    """
    status: dict[str, object] = {}
    nodes = M.read_tree_nodes(_build_world(tmp_path), status=status)
    assert len(nodes) == 2
    assert status == {}, "healthy index must not be marked degraded"


@pytest.mark.parametrize(
    ("member", "raw", "expect_in_error"),
    [
        # yaml.YAMLError — the shape measured live (a stray backtick at line 4 col 3).
        ("scanner_error", b"nodes:\n  a:\n    summary: x\n  `bad: [\n", "Error"),
        # Valid YAML whose root is a LIST. `safe_load(x) or {}` does not substitute for
        # a truthy non-mapping, so this reached `.get` and raised AttributeError.
        ("non_mapping_root", b"- one\n- two\n", "expected a mapping"),
        # Valid YAML whose root is a bare SCALAR — the same hole, other member.
        ("scalar_root", b"just-a-string\n", "expected a mapping"),
        # Corrupt bytes. UnicodeDecodeError is a ValueError, NOT an OSError, so an
        # `(OSError, YAMLError)` tuple would let this escape silently (guard-2441).
        ("corrupt_bytes", b"nodes:\n  a: \xff\xfe\x00bad\n", "UnicodeDecodeError"),
    ],
)
def test_read_tree_nodes_unreadable_index_degrades_not_raises(
    tmp_path: Path, member: str, raw: bytes, expect_in_error: str
) -> None:
    """DEFECT C: an unreadable index killed the export instead of marking the bundle.

    ``knowledge-export.sh`` then left the previous bundle in place, so a malformed
    index was indistinguishable from a healthy no-op at the bundle — 5 of 18 live
    indexes were in exactly that state.
    """
    world = _raw_index_world(tmp_path, raw)
    status: dict[str, object] = {}
    nodes = M.read_tree_nodes(world, status=status)   # must not raise
    assert nodes == []
    assert status.get("degraded") is True, f"{member} did not mark the status degraded"
    assert status.get("store") == "tree"
    assert str(status.get("path")).endswith('_tree.yaml')
    # The reason names the MEASURED condition, not a generic "could not read"
    # (guard-1946) — a reader has to be able to tell these four members apart.
    assert expect_in_error in str(status.get("error")), status.get("error")


def test_read_tree_nodes_degrades_without_a_status_dict(tmp_path: Path) -> None:
    """``status`` is opt-in, so the no-arg call must still swallow the failure.

    Every pre-existing caller passes no ``status``; if the guarded parse only held
    when a dict was supplied, the fix would be inert on the production path.
    """
    world = _raw_index_world(tmp_path, b"- not\n- a mapping\n")
    assert M.read_tree_nodes(world) == []


def test_build_bundle_reports_tree_degradation(tmp_path: Path) -> None:
    """The marker has to survive the trip to the caller, not just exist in the reader."""
    world = _raw_index_world(tmp_path, b"nodes:\n  `bad: [\n")
    status: dict[str, object] = {}
    bundle = M.build_bundle(world, tmp_path, env={}, tree_status=status)
    assert bundle.counts()["tree"] == 0
    assert status.get("degraded") is True


def test_build_bundle_healthy_world_reports_no_degradation(tmp_path: Path) -> None:
    """Positive control for the assertion above, at the build_bundle layer."""
    status: dict[str, object] = {}
    bundle = M.build_bundle(_build_world(tmp_path), tmp_path, env={}, tree_status=status)
    assert bundle.counts()["tree"] > 0
    assert status == {}


def test_agent_names_lists_agent_dirs(tmp_path: Path) -> None:
    _build_world(tmp_path)
    assert M._agent_names(tmp_path) == ["alpha"]


def test_secret_values_selects_by_suffix_and_length() -> None:
    env = {
        "MY_API_KEY": "longsecretvalue123",   # selected
        "SESSION_TOKEN": "anothersecret456",  # selected
        "SHORT_KEY": "abc",                   # too short (<8) — excluded
        "PLAIN_NAME": "notasecretatall",      # wrong suffix — excluded
    }
    got = set(M._secret_values(env))
    assert got == {"longsecretvalue123", "anothersecret456"}


# ── build_bundle integration (security-critical) ─────────────────────────────

def test_build_bundle_filters_and_redacts(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})

    # Exactly the domain entries survive; framework rows are gone.
    assert bundle.counts() == {
        "tree": 1, "hypotheses": 1, "guardrails": 1, "lessons": 1, "self": 0,
    }  # the fixture world has no agents/<a>/self.md -> `self` projects empty

    node = bundle.tree[0]
    assert node["key"] == "coral-reefs"
    # Redaction ran through the reader: agent name + absolute path stripped.
    assert "alpha" not in node["summary"].lower()
    assert "/home/ec2" not in node["summary"]
    assert "the agent" in node["summary"]

    assert bundle.hypotheses[0]["horizon"] == "short"
    assert "alpha" not in bundle.hypotheses[0]["outcome"].lower()
    assert bundle.guardrails[0]["rule"].startswith("Verify every claim")
    assert "Cross-check" in bundle.lessons[0]["title"]


def test_build_bundle_never_leaks_framework_subtree(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    # The system tree node, framework guardrail, untagged guardrail, and system
    # hypothesis are all absent.
    assert all("system" not in str(t.get("parent")) for t in bundle.tree)
    rules = [g["rule"] for g in bundle.guardrails]
    assert not any("fail closed" in r for r in rules)
    assert not any("critical()" in r for r in rules)
    assert all(h["status"] != "active" for h in bundle.hypotheses)


def test_build_bundle_redacts_node_body(tmp_path: Path) -> None:
    world = _build_world(tmp_path, write_bodies=True)
    bundle = M.build_bundle(world, tmp_path, env={})
    body = bundle.tree[0]["body"]
    # The reef body named alpha + an absolute path — both redacted, content survives.
    assert body  # non-empty
    assert "alpha" not in body.lower()
    assert "/home/ec2" not in body
    assert "the agent" in body
    assert "marine" in body


def test_build_bundle_never_exposes_framework_node_body(tmp_path: Path) -> None:
    world = _build_world(tmp_path, write_bodies=True)
    bundle = M.build_bundle(world, tmp_path, env={})
    # Only the domain node survives; its framework sibling's body never appears.
    assert len(bundle.tree) == 1
    blob = " ".join(str(t.get("body")) for t in bundle.tree)
    assert "Framework plumbing" not in blob
    assert "_paths.py" not in blob


def test_build_bundle_redacts_env_secret_values(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    secret = "topsecrettoken0xdeadbeef"
    # Inject the secret into an exposed lesson, then confirm the reader strips it
    # when that same value is present as an env secret.
    _write_jsonl(
        world / "reasoning-bank.jsonl",
        [{"applies_to": "domain", "category": "marine-biology/note", "title": "Note",
          "failure_lesson": f"the key was {secret} — never show it"}],
    )
    bundle = M.build_bundle(world, tmp_path, env={"SOME_API_TOKEN": secret})
    lesson = bundle.lessons[0]["lesson"]
    assert secret not in lesson
    assert "[redacted]" in lesson


def test_build_bundle_empty_world_is_empty_bundle(tmp_path: Path) -> None:
    (tmp_path / "world").mkdir()
    bundle = M.build_bundle(tmp_path / "world", tmp_path, env={})
    assert bundle.counts() == {
        "tree": 0, "hypotheses": 0, "guardrails": 0, "lessons": 0, "self": 0,
    }


# ── OKF markdown bundle (PEARL §10.5) ────────────────────────────────────────

def _fm(text: str) -> dict:
    """Parse the ---fenced YAML frontmatter of an OKF concept file."""
    assert text.startswith("---\n")
    _, fm, _body = text.split("---\n", 2)
    return yaml.safe_load(fm)


def test_okf_bundle_layout_and_required_type(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    counts = M.write_okf_bundle(bundle, out)

    assert (out / "index.md").is_file()
    # One wiki article per exposed node; each carries the required `type` discriminator.
    node_files = list((out / "nodes").glob("*.md"))
    assert len(node_files) == counts["tree"] == 1
    fm = _fm(node_files[0].read_text(encoding="utf-8"))
    assert fm["type"] == "node"  # the one REQUIRED OKF frontmatter key
    assert fm["key"] == "coral-reefs"
    # Hypotheses, guardrails, lessons each become their own typed concept files.
    assert _fm(next((out / "hypotheses").glob("*.md")).read_text(encoding="utf-8"))["type"] == "hypothesis"
    assert _fm(next((out / "guardrails").glob("*.md")).read_text(encoding="utf-8"))["type"] == "guardrail"
    assert _fm(next((out / "lessons").glob("*.md")).read_text(encoding="utf-8"))["type"] == "lesson"


def test_okf_bundle_carries_redaction_into_markdown(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)
    body = next((out / "nodes").glob("*.md")).read_text(encoding="utf-8")
    # The reef node's summary named alpha + a path — the markdown must not leak them.
    assert "alpha" not in body.lower()
    assert "/home/ec2" not in body
    assert "the agent" in body


def test_okf_bundle_prefers_full_body_over_summary(tmp_path: Path) -> None:
    world = _build_world(tmp_path, write_bodies=True)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)
    body = next((out / "nodes").glob("*.md")).read_text(encoding="utf-8")
    # The OKF wiki article carries the full body (not just the one-line summary),
    # with redaction preserved.
    assert "host a quarter of marine" in body
    assert "the agent" in body
    assert "/home/ec2" not in body


def test_okf_index_links_resolve_to_written_files(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)
    index = (out / "index.md").read_text(encoding="utf-8")
    # Every nodes/<stem>.md link in the index points at a file that exists (invariant 6
    # allows dangling, but our own article links must resolve).
    import re as _re
    for rel in _re.findall(r"\]\((nodes/[^)]+\.md)\)", index):
        assert (out / rel).is_file(), f"index links missing file {rel}"


def test_okf_empty_bundle_writes_index_and_empty_dirs(tmp_path: Path) -> None:
    (tmp_path / "world").mkdir()
    bundle = M.build_bundle(tmp_path / "world", tmp_path, env={})
    out = tmp_path / "okf"
    counts = M.write_okf_bundle(bundle, out)
    assert counts == {
        "tree": 0, "hypotheses": 0, "guardrails": 0, "lessons": 0, "self": 0,
    }
    assert (out / "index.md").is_file()
    assert list((out / "nodes").glob("*.md")) == []


def test_generated_at_is_utc_not_box_local(monkeypatch) -> None:
    """The stamp must be UTC by construction, not by the box's ambient TZ ().

    The production caller is a systemd unit on the sidecar box, which does NOT inherit
    the `TZ=UTC` env pinned on agent boxes. A bare `datetime.now()` would stamp
    box-local time and silently offset every age comparison, so this pins the one
    property that cannot be checked by eye. Asserting against a fixed non-UTC TZ makes
    the regression fail loudly instead of drifting by an hour.
    """
    import time

    stamp = M._generated_at()
    assert len(stamp) == 19 and stamp[10] == "T", stamp  # naive ISO, no zone suffix
    # Same instant, computed independently in UTC. Allow a small window for clock read
    # skew; a TZ-offset bug is >= 1h, far outside it.
    expected = datetime.datetime.now(datetime.timezone.utc)
    got = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    assert abs((expected.replace(tzinfo=None) - got).total_seconds()) < 120, (stamp, expected)
    # And under a deliberately non-UTC TZ the stamp must NOT move.
    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(time, "tzset"):
        time.tzset()
        try:
            shifted = M._generated_at()
            got2 = datetime.datetime.strptime(shifted, "%Y-%m-%dT%H:%M:%S")
            assert abs((expected.replace(tzinfo=None) - got2).total_seconds()) < 120, shifted
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()


def test_json_payload_carries_generated_at(tmp_path: Path, monkeypatch, capsys) -> None:
    """The served JSON bundle carries its own age ().

    Without it the daemon's /knowledge/* routes return whatever the last export wrote,
    however old, and a stopped timer is indistinguishable from a current bundle.
    """
    world = _build_world(tmp_path)
    monkeypatch.setenv("WORLD_PATH", str(world))
    monkeypatch.delenv("META_PATH", raising=False)
    out = tmp_path / "bundle.json"
    assert M.main(["-o", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "generated_at" in payload, sorted(payload)
    # First key — a consumer reading the head of the file sees the age immediately.
    assert next(iter(payload)) == "generated_at"
    datetime.datetime.strptime(payload["generated_at"], "%Y-%m-%dT%H:%M:%S")
    # Additive: every pre-existing key keeps its name.
    assert {"counts", "tree", "hypotheses", "guardrails", "lessons"} <= set(payload)


def test_okf_index_carries_generated_at(tmp_path: Path) -> None:
    """The downloadable wiki outlives the box it came from, so its index states its age."""
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)
    index = (out / "index.md").read_text(encoding="utf-8")
    fm = _fm(index)
    assert fm["type"] == "index"  # the required discriminator is unchanged
    stamp = str(fm["generated_at"])
    datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    # Also human-visible in the body, not only in machine frontmatter.
    assert f"- Generated: {stamp} UTC" in index


# ── Writer field-fidelity () ───────────────────────────────────────


def test_okf_node_frontmatter_carries_summary(tmp_path: Path) -> None:
    """`summary` must reach frontmatter even when the node HAS a body.

    This was the one projected field write_okf_bundle actually lost: `body` and
    `summary` were both consumed by a single `body or summary` fallback, so a node
    with a body rendered the body and dropped the summary entirely — leaving a
    consumer no short description to preview by without parsing the article.
    """
    world = _build_world(tmp_path, write_bodies=True)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)
    text = next((out / "nodes").glob("*.md")).read_text(encoding="utf-8")
    fm = _fm(text)
    assert fm["summary"], "summary must survive into frontmatter, not only as a body fallback"
    # It stays REDACTED — frontmatter is not a bypass around the projection.
    assert "alpha" not in str(fm["summary"]).lower()
    assert "/home/ec2" not in str(fm["summary"])
    # And the body is still the full article, not the summary (prior behaviour intact).
    assert "host a quarter of marine" in text


def test_okf_writer_loses_no_projected_field(tmp_path: Path) -> None:
    """Every field project() emits must land in frontmatter or in the body.

    The guard against re-introducing a silent drop at the export boundary. It
    asserts the WRITER is lossless with respect to what it is GIVEN — which is a
    different claim from "the bundle carries every store field". project() is an
    allowlist by design (it is the redaction/suppression boundary for a
    user-facing wiki); this test must never be read as licence to widen it.
    """
    world = _build_world(tmp_path, write_bodies=True)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)

    for records, folder in (
        (bundle.tree, "nodes"), (bundle.hypotheses, "hypotheses"),
        (bundle.guardrails, "guardrails"), (bundle.lessons, "lessons"),
    ):
        if not records:
            continue
        text = next((out / folder).glob("*.md")).read_text(encoding="utf-8")
        fm = _fm(text)
        for key, value in records[0].items():
            rendered = " ".join(map(str, value)) if isinstance(value, list) else str(value)
            if not rendered:
                continue  # an empty optional field has nothing to lose
            assert key in fm or rendered in text, (
                f"{folder}: projected field `{key}` reaches neither frontmatter nor body"
            )


def test_okf_bundle_conforms_to_shape_contract(tmp_path: Path) -> None:
    """This producer's own output must pass the shared conformance checker.

    Half of the two-producer drift guard (g-115-3266): the checker is
    producer-agnostic so the sibling wiki daemon's bundles run through the same
    invariants. Deliberately checks the SHAPE contract only — the convention is
    explicit that field names are the producer's choice, so field-set parity
    between producers is NOT asserted here or anywhere.
    """
    obc = _load_conformance_mod()
    world = _build_world(tmp_path, write_bodies=True)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)

    report = obc.check_bundle(out)
    assert report["documents"] > 0, "empty bundle proves nothing (rb-245)"
    assert report["conforms"], f"own bundle violates the shape contract: {report['violations']}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── SELF projection () ───────────────────────────────────────────────
# self.md is AGENT IDENTITY, not domain knowledge, and the bulk of a real one
# (44-55 KB, measured across five agents 2026-08-29) is precisely the "cognitive
# plumbing" PEARL 10.3 suppresses: absolute workspace paths, box hostnames,
# sub-repo tiering, agent-provisionable action lists, revision chains. These tests
# pin the cut, and pin it from the CUSTOMER side -- what must NOT come out.

_SELF_MD = """---
created: "2026-05-09"
last_updated: "2026-08-27"
last_update_trigger: "fresh-eyes N=107 retired the premise; see g-115-4913"
source: "agent"
revision_id: "self-20260827T022711-alpha-f57d"
previous_revision_id: "self-20260822T105350-alpha-145c"
---

# Self

I am the analyst-investigator, the lens that converts ambiguity into evidence.

alpha builds from a verified foundation.

## Primary Workspace

Resolve from AGENT_WRITE_PATH in agents/alpha/local-paths.conf; /home/ec2/world
on the Linux boxes. 40 sub-repos, 8 ACTIVE Tier-1.

## Agent-Provisionable Actions

- restart the daemon
"""


def _self_bundle(tmp_path: Path, text: str | None = _SELF_MD):
    """A world plus an agents/alpha/self.md, projected. ``None`` writes no self.md."""
    world = _build_world(tmp_path, write_bodies=True)
    if text is not None:
        (tmp_path / "agents" / "alpha" / "self.md").write_text(text, encoding="utf-8")
    return M.build_bundle(world, tmp_path, env={"MIND_AGENT": "alpha"})


def test_self_projection_allowlists_front_matter_and_cuts_at_first_section(tmp_path: Path) -> None:
    """Two dated keys plus the pre-``##`` prose. Everything else is suppressed."""
    got = _self_bundle(tmp_path).agent_self

    assert set(got) == {"purpose", "created", "last_updated"}, (
        "front matter is an ALLOWLIST (SELF_EXPOSED_FM_FIELDS) -- a new key must be "
        "added there deliberately, never arrive by default"
    )
    assert got["created"] == "2026-05-09" and got["last_updated"] == "2026-08-27"

    purpose = str(got["purpose"])
    assert "converts ambiguity into evidence" in purpose, "the identity statement is the point"
    assert not purpose.startswith("#"), "the file's own '# Self' title is not prose"
    # The structural cut: nothing at or below the first '##' survives.
    for plumbing in ("Primary Workspace", "AGENT_WRITE_PATH", "local-paths.conf",
                     "Tier-1", "Agent-Provisionable", "restart the daemon"):
        assert plumbing not in purpose, f"section content leaked past the '##' cut: {plumbing}"


def test_self_projection_suppresses_identity_plumbing_and_redacts(tmp_path: Path) -> None:
    """The customer-side leak check: none of this may appear anywhere in the view."""
    blob = repr(_self_bundle(tmp_path).agent_self)
    for forbidden in (
        "revision_id", "self-20260827", "self-20260822",   # the revision chain
        "last_update_trigger", "fresh-eyes", "g-115-4913",  # the internal narrative
        "/home/ec2/world",                                  # absolute paths
        "alpha",                                            # agent name -> "the agent"
    ):
        assert forbidden not in blob, f"`{forbidden}` reached the customer-facing self view"
    assert "the agent" in blob, "Redactor must rewrite the agent name, not merely drop it"


def test_self_projection_refuses_a_dates_only_husk(tmp_path: Path) -> None:
    """Front matter with no prose behind it is not an identity -- publish nothing."""
    bundle = _self_bundle(tmp_path, '---\ncreated: "2026-05-09"\n---\n\n## Primary Workspace\n\nx\n')
    assert bundle.agent_self == {}, "dates alone must not publish as an identity"
    assert bundle.counts()["self"] == 0


def test_self_absent_publishes_no_key_rather_than_a_hollow_one(tmp_path: Path) -> None:
    """No self.md -> `{}`, so a consumer can tell 'not published' from 'published blank'."""
    bundle = _self_bundle(tmp_path, None)
    assert bundle.agent_self == {} and bundle.counts()["self"] == 0


def test_self_agent_resolution_is_fail_closed_when_ambiguous(tmp_path: Path) -> None:
    """Two agent dirs and no MIND_AGENT binding: guessing would publish the wrong identity."""
    world = _build_world(tmp_path, write_bodies=True)
    (tmp_path / "agents" / "alpha" / "self.md").write_text(_SELF_MD, encoding="utf-8")
    (tmp_path / "agents" / "bravo").mkdir(parents=True)
    assert M.build_bundle(world, tmp_path, env={}).agent_self == {}
    # ...and the single-agent sidecar case, which IS unambiguous, still resolves.
    (tmp_path / "agents" / "bravo").rmdir()
    assert M.build_bundle(world, tmp_path, env={}).agent_self != {}


def test_self_reaches_json_payload_and_okf_bundle(tmp_path: Path) -> None:
    """Both emitted forms carry it -- an enumerated writer drops what it is not told about."""
    bundle = _self_bundle(tmp_path)
    out = tmp_path / "okf"
    counts = M.write_okf_bundle(bundle, out)

    assert counts["self"] == 1
    self_md = (out / "self.md").read_text(encoding="utf-8")
    fm = _fm(self_md)
    assert fm["type"] == "self", "one concept = one md + a required `type` discriminator"
    assert fm["created"] == "2026-05-09" and fm["last_updated"] == "2026-08-27"
    assert "converts ambiguity into evidence" in self_md
    assert "- [About this agent](self.md)" in (out / "index.md").read_text(encoding="utf-8")


def test_self_key_is_always_present_in_the_json_payload(tmp_path: Path, monkeypatch) -> None:
    """The JSON writer is the OTHER enumerated writer, and it was the untested half.

    ``test_self_reaches_json_payload_and_okf_bundle`` names the JSON payload but only
    exercises ``write_okf_bundle``; nothing called ``main()`` and read the key back. The
    g-368-53 merge moved the data keys out of the payload literal into a ``.update()``
    so ``degraded`` could sit second, and dropping ``self`` on the way would have left
    the whole suite green. Caught by a hand-run positive control, pinned here.

    The assertion is PRESENCE, not content: ``project_root`` is bound to the real repo
    root inside ``main()`` and is not injectable, so a tmp world exposes no agent
    identity. That is the documented contract -- ``{}`` distinguishes "no identity
    published" from "published and blank", so the key must survive even when empty.
    """
    world = _build_world(tmp_path)
    monkeypatch.setenv("WORLD_PATH", str(world))
    monkeypatch.delenv("META_PATH", raising=False)
    out = tmp_path / "bundle.json"
    assert M.main(["-o", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "self" in payload, sorted(payload)
    assert "self" in payload["counts"], sorted(payload["counts"])


def test_healthy_export_omits_the_degraded_key_from_the_written_payload(
    tmp_path: Path, monkeypatch
) -> None:
    """The half that runs on every healthy sidecar, on every hourly timer fire.

    The three existing `degraded` assertions (read_tree_nodes / build_bundle) test the
    ``status`` dict — the FUNCTION's out-parameter. Nothing asserted the key as it
    appears in the BUNDLE ON DISK, so a refactor of main()'s payload assembly could
    start emitting ``degraded: null`` on healthy exports with every test still green.
    That is a false alarm shipped to every environment, on the path taken when nothing
    is wrong — strictly worse than missing the degraded case, which only fires on an
    already-broken index.

    ABSENCE is the contract, not falsiness: the emit site's own comment says a consumer
    tests ``"degraded" in bundle``, so a present-but-empty key breaks that reader.
    """
    world = _build_world(tmp_path)
    monkeypatch.setenv("WORLD_PATH", str(world))
    monkeypatch.delenv("META_PATH", raising=False)
    out = tmp_path / "bundle.json"
    assert M.main(["-o", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "degraded" not in payload, sorted(payload)
    # POSITIVE CONTROL: a bundle that failed to export would also lack the key.
    assert payload["counts"]["tree"] > 0, "fixture did not produce a real export"


def test_malformed_index_refuses_to_write_and_names_the_cause(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A malformed index REFUSES the write (rc=2). It does not emit a degraded bundle.

    This is not the assertion g-368-54 asked for, and the difference is the finding.
    That goal asked for a test pinning ``degraded`` in main()'s WRITTEN payload. Writing
    it revealed that the branch at the emit site is UNREACHABLE, measured three ways:

      1. A broken index does not degrade the tree alone — it zeroes EVERY knowledge
         count. Measured on this fixture: healthy {tree:1, hypotheses:1, guardrails:1,
         lessons:1} -> broken {0, 0, 0, 0}. The non-tree stores are gated on the tree.
      2. ``world_store_evidence`` counts ``tree_index_bytes``, and ``_degrade`` can only
         fire when the index EXISTS and is unreadable — which means non-zero bytes. So
         evidence is ALWAYS truthy exactly when the tree degraded.
      3. All-zero counts + truthy evidence is precisely the g-368-34 refusal condition,
         so main() returns 2 before the payload dict is built.

    The two features are individually correct and were landed by the same goal; together
    they make the degraded PAYLOAD dead code. It went unnoticed because g-368-53's
    two-arm probe verified the marker at the ``status``/``build_bundle`` level, never
    end-to-end through main() to a file — which is the gap g-368-54 exists to close.

    So this pins what actually happens, and the operator-visible contract that survives:
    the refusal NAMES the cause on stderr, which is g-368-53's real delivery.
    """
    world = _build_world(tmp_path)
    # a stray backtick at line 4 — the yaml.YAMLError shape measured on a live index
    index = world / "knowledge" / "tree" / TREE_INDEX_BASENAME
    index.write_bytes(b"nodes:\n  a:\n    summary: x\n  `bad: [\n")
    monkeypatch.setenv("WORLD_PATH", str(world))
    monkeypatch.delenv("META_PATH", raising=False)
    out = tmp_path / "bundle.json"

    assert M.main(["-o", str(out)]) == 2
    assert not out.exists(), "the refusal must leave the previous bundle in place"

    err = capsys.readouterr().err
    assert "REFUSING to write an all-zero bundle" in err
    #  threaded tree_status into main so the refusal names WHY, not just THAT.
    # Without this the operator sees a hollow-bundle refusal with no cause and cannot
    # tell a malformed index from a genuinely empty world.
    assert "CAUSE:" in err and "ScannerError" in err, err
    assert TREE_INDEX_BASENAME in err, err


def test_degraded_payload_branch_is_currently_unreachable(tmp_path: Path) -> None:
    """Pins the reachability gap itself, so making it reachable is a DELIBERATE act.

    If a future change exempts a degraded export from the all-zero refusal (or lets the
    non-tree stores project independently of the tree), this test fails and points the
    author at ``test_malformed_index_refuses_to_write_and_names_the_cause`` — which will
    then need to become the payload assertion g-368-54 originally asked for.

    Asserted on the PRECONDITION, not by grepping the source: the branch is unreachable
    because degradation implies all-zero counts AND non-empty evidence, and that is a
    property of behaviour, not of text.
    """
    import knowledge_projection as K

    world = _build_world(tmp_path)
    (world / "knowledge" / "tree" / TREE_INDEX_BASENAME).write_bytes(b"- not a mapping\n")
    status: dict[str, object] = {}
    bundle = M.build_bundle(world, tmp_path, env={}, tree_status=status)

    assert status.get("degraded") is True, "fixture must actually degrade"
    assert not any(bundle.counts()[k] for k in K.KNOWLEDGE_COUNT_KEYS), (
        "a degraded tree no longer zeroes every knowledge count — the refusal may no "
        "longer pre-empt the payload, so the degraded key may now be REACHABLE"
    )
    assert M.world_store_evidence(world), (
        "a degraded index no longer yields store evidence — the refusal may no longer "
        "fire, so the degraded key may now be REACHABLE"
    )


def test_self_count_cannot_satisfy_the_broken_export_refusal(tmp_path: Path) -> None:
    """`self` is in counts() but NOT in KNOWLEDGE_COUNT_KEYS -- and that gap is load-bearing.

    The refusal (g-368-34) fires when a world that demonstrably holds stores projects
    to all-zero knowledge. `self` is read from the AGENT directory, not the world, so it
    is non-zero exactly when the four knowledge stores have all failed. Folding it into
    that check would let a genuinely broken export walk past the gate built to catch it
    -- the state guard-5144 records 13 live sidecars sitting in.
    """
    import knowledge_projection as K

    assert "self" in K.ProjectedBundle().counts()
    assert "self" not in K.KNOWLEDGE_COUNT_KEYS

    bundle = M.ProjectedBundle(agent_self={"purpose": "p"})
    assert bundle.counts()["self"] == 1
    assert any(bundle.counts().values()), "the naive check would pass this broken bundle"
    assert not any(bundle.counts()[k] for k in K.KNOWLEDGE_COUNT_KEYS), (
        "the refusal must still see this export as all-zero knowledge"
    )
