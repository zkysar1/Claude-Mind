"""Tests for pack_manifest -- the loading manifest over knowledge pack bundles.

Every refusal test starts from a bundle that builds cleanly (``test_clean_bundle_builds`` is the
negative control) and breaks exactly ONE thing, then asserts that thing is the only reason
given, so a test cannot pass on an unrelated refusal. Every edit asserts it landed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest
import yaml

import pack_manifest as pm

FAMILY_A = "kp-" + "a" * 32
FAMILY_B = "kp-" + "b" * 32
DROP = object()  # a manifest override that removes the field

_KINDS = {"nodes": ("kn", "node"), "guardrails": ("kg", "guardrail"), "lessons": ("kl", "lesson")}


def _id(prefix: str, n: int) -> str:
    return f"{prefix}-{n:032x}"


def _doc(front: dict, body: str = "Body text.\n") -> str:
    return "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body


def _item_front(kind: str, n: int, title: str, **extra) -> dict:
    prefix, type_ = _KINDS[kind]
    return {"type": type_, f"{type_}_id": _id(prefix, n), "title": title, **extra}


def make_pack(root: Path, *, family=FAMILY_A, language="en", version="1.0", name="Widget calibration",
              nodes=(), guardrails=(), lessons=(), manifest=None) -> Path:
    """Write one bundle. Each item is ``(n, title)`` or ``(n, title, extra front matter)``."""
    root.mkdir(parents=True, exist_ok=True)
    front = {"type": "pack-manifest", "format_version": 1, "family_id": family,
             "pack_id": f"{family}.{language}", "language": language, "name": name,
             "version": version}
    for key, value in (manifest or {}).items():
        if value is DROP:
            front.pop(key)
        else:
            front[key] = value
    (root / "pack.md").write_text(_doc(front), encoding="utf-8")
    for kind, items in (("nodes", nodes), ("guardrails", guardrails), ("lessons", lessons)):
        for n, title, *extra in items:
            (root / kind).mkdir(exist_ok=True)
            front = _item_front(kind, n, title, **(extra[0] if extra else {}))
            (root / kind / f"item-{n:03d}.md").write_text(_doc(front), encoding="utf-8")
    return root


def _clean(tmp_path: Path, name: str = "pack") -> Path:
    return make_pack(tmp_path / name, nodes=[(1, "First node"), (2, "Second node")],
                     guardrails=[(1, "First rule")], lessons=[(1, "First lesson")])


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"edit did not land: {old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _refusal(*roots: Path) -> dict[str, list[str]]:
    with pytest.raises(pm.PackRefused) as caught:
        pm.build_manifest(list(roots))
    return caught.value.reasons


def _spec_hash(root: Path) -> str:
    """Format section 7, written out from the spec's own Python form."""
    paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    lines = "".join(f"{hashlib.sha256((root / p).read_bytes()).hexdigest()}  {p}\n" for p in paths)
    return hashlib.sha256(lines.encode()).hexdigest()


# --- what the manifest says ------------------------------------------------------------------

def test_clean_bundle_builds(tmp_path):
    out = pm.build_manifest([_clean(tmp_path)])
    (pack,) = out["packs"]
    assert {k: pack[k] for k in ("pack_id", "version", "name", "language")} == {
        "pack_id": f"{FAMILY_A}.en", "version": "1.0", "name": "Widget calibration", "language": "en"}
    assert (pack["nodes"], pack["guardrails"], pack["lessons"]) == (2, 1, 1)
    assert pack["sample_titles"] == ["First node", "Second node"]
    assert out["totals"] == {"packs": 1, "nodes": 2, "guardrails": 1, "lessons": 1}


def test_two_packs_keep_request_order(tmp_path):
    a = make_pack(tmp_path / "a", family=FAMILY_A, nodes=[(1, "A node")])
    b = make_pack(tmp_path / "b", family=FAMILY_B, language="zh", nodes=[(2, "B node")],
                  guardrails=[(2, "B rule")])
    out = pm.build_manifest([b, a])
    assert [p["pack_id"] for p in out["packs"]] == [f"{FAMILY_B}.zh", f"{FAMILY_A}.en"]
    assert out["totals"] == {"packs": 2, "nodes": 2, "guardrails": 1, "lessons": 0}


def test_shared_item_counts_once_in_totals(tmp_path):
    a = make_pack(tmp_path / "a", family=FAMILY_A, nodes=[(1, "Shared"), (2, "Only A")])
    b = make_pack(tmp_path / "b", family=FAMILY_B, nodes=[(1, "Shared"), (3, "Only B")])
    out = pm.build_manifest([a, b])
    assert [p["nodes"] for p in out["packs"]] == [2, 2]  # per pack: each carries its two
    assert out["totals"]["nodes"] == 3                   # distinct: the shared node lands once


def test_counts_come_from_the_files_not_from_the_pack(tmp_path):
    root = make_pack(tmp_path / "p", nodes=[(1, "Only node")],
                     manifest={"node_count": 99, "nodes": 99})
    assert pm.build_manifest([root])["packs"][0]["nodes"] == 1


def test_zero_is_a_real_count_not_a_missing_directory(tmp_path):
    bare = make_pack(tmp_path / "bare", nodes=[(1, "Only node")])
    full = make_pack(tmp_path / "full", family=FAMILY_B, nodes=[(1, "Only node")],
                     lessons=[(1, "One"), (2, "Two")])
    packs = pm.build_manifest([bare, full])["packs"]
    assert [(p["guardrails"], p["lessons"]) for p in packs] == [(0, 0), (0, 2)]  # the probe sees lessons


def test_sample_titles_put_top_level_nodes_first_then_path_order(tmp_path):
    root = make_pack(tmp_path / "p", nodes=[
        (1, "Child", {"parent": _id("kn", 2)}), (2, "Root two"), (3, "Root three")])
    assert pm.build_manifest([root], samples=2)["packs"][0]["sample_titles"] == ["Root two", "Root three"]
    assert pm.build_manifest([root], samples=5)["packs"][0]["sample_titles"] == [
        "Root two", "Root three", "Child"]


def test_samples_zero_gives_none_and_negative_is_rejected(tmp_path):
    root = _clean(tmp_path)
    assert pm.build_manifest([root], samples=0)["packs"][0]["sample_titles"] == []
    with pytest.raises(ValueError):
        pm.build_manifest([root], samples=-1)


def test_output_is_deterministic(tmp_path):
    root = _clean(tmp_path)
    assert pm.build_manifest([root]) == pm.build_manifest([root])


# --- identity: the pack hash and the files the counts leave out ---------------------------------

def test_unrecognized_files_are_listed_not_counted_but_hashed(tmp_path):
    root = _clean(tmp_path)
    (root / "README.md").write_text("notes\n", encoding="utf-8")
    (root / "nodes" / "diagram.png").write_bytes(b"\x89PNG\xff\xfe not text")
    (root / "notes").mkdir()
    (root / "notes" / "extra.md").write_text("---\ntype: node\n---\nx\n", encoding="utf-8")
    (root / "nodes-extra").mkdir()
    (root / "nodes-extra" / "x.md").write_text("x\n", encoding="utf-8")
    pack = pm.build_manifest([root])["packs"][0]
    assert pack["unrecognized"] == ["README.md", "nodes-extra/x.md", "nodes/diagram.png", "notes/extra.md"]
    assert (pack["nodes"], pack["guardrails"], pack["lessons"]) == (2, 1, 1)
    assert pack["pack_sha256"] == _spec_hash(root)


def test_pack_hash_is_the_format_section_7_hash(tmp_path):
    root = _clean(tmp_path)
    assert pm.build_manifest([root])["packs"][0]["pack_sha256"] == _spec_hash(root)


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum")
def test_pack_hash_matches_the_spec_shell_command(tmp_path):
    root = _clean(tmp_path)
    (root / "nodes-extra").mkdir()  # '-' sorts before '/', so path order differs from walk order
    (root / "nodes-extra" / "x.md").write_text("x\n", encoding="utf-8")
    shell = subprocess.run(
        "find . -type f | sed 's|^\\./||' | LC_ALL=C sort | xargs sha256sum | sha256sum",
        shell=True, cwd=root, capture_output=True, text=True, check=True).stdout.split()[0]
    assert pm.build_manifest([root])["packs"][0]["pack_sha256"] == shell


# --- refusals: one defect each, and it must be the only reason --------------------------------

def _drop_line(path: Path, line: str) -> None:
    _edit(path, line + "\n", "")


_BREAKS = {
    "no-manifest": (lambda r: (r / "pack.md").unlink(), "pack.md: is missing from the bundle root"),
    "manifest-wrong-type": (lambda r: _edit(r / "pack.md", "type: pack-manifest", "type: pack"),
                            "pack.md: type is 'pack'"),
    "format-version-unknown": (lambda r: _edit(r / "pack.md", "format_version: 1", "format_version: 2"),
                               "pack.md: format_version is 2"),
    "format-version-bool": (lambda r: _edit(r / "pack.md", "format_version: 1", "format_version: true"),
                            "pack.md: format_version is True"),
    "family-id-malformed": (lambda r: (_edit(r / "pack.md", f"family_id: {FAMILY_A}", "family_id: kp-123"),
                                       _edit(r / "pack.md", f"pack_id: {FAMILY_A}.en", "pack_id: kp-123.en")),
                            "pack.md: family_id is missing or not kp-"),
    "pack-id-mismatch": (lambda r: _edit(r / "pack.md", f"pack_id: {FAMILY_A}.en", f"pack_id: {FAMILY_A}.zh"),
                         "pack.md: pack_id is"),
    "language-uppercase": (lambda r: _edit(r / "pack.md", "language: en", "language: EN"),
                           "pack.md: language is 'EN'"),
    "language-yaml-bool": (lambda r: _edit(r / "pack.md", "language: en", "language: no"),
                           "pack.md: language is False"),
    "version-unquoted-number": (lambda r: _edit(r / "pack.md", "version: '1.0'", "version: 1.10"),
                                "loaded as a number (1.1); quote it in YAML"),
    "version-three-parts": (lambda r: _edit(r / "pack.md", "version: '1.0'", "version: '1.0.0'"),
                            "pack.md: version is '1.0.0', not MAJOR.MINOR"),
    "version-leading-zero": (lambda r: _edit(r / "pack.md", "version: '1.0'", "version: '01.2'"),
                             "pack.md: version is '01.2', not MAJOR.MINOR"),
    "version-missing": (lambda r: _drop_line(r / "pack.md", "version: '1.0'"), "pack.md: version is missing"),
    "name-empty": (lambda r: _edit(r / "pack.md", "name: Widget calibration", "name: ''"),
                   "pack.md: name is missing, empty or not text"),
    "name-missing": (lambda r: _drop_line(r / "pack.md", "name: Widget calibration"),
                     "pack.md: name is missing, empty or not text"),
    "item-type-mismatch": (lambda r: _edit(r / "nodes" / "item-001.md", "type: node", "type: lesson"),
                           "nodes/item-001.md: type is 'lesson', but a file under nodes/ must have type 'node'"),
    "item-id-malformed": (lambda r: _edit(r / "nodes" / "item-001.md", _id("kn", 1), "kn-xyz"),
                          "nodes/item-001.md: node_id is missing or malformed"),
    "item-id-wrong-prefix": (lambda r: _edit(r / "nodes" / "item-001.md", _id("kn", 1), _id("kg", 1)),
                             "nodes/item-001.md: node_id is missing or malformed"),
    "item-id-duplicated": (lambda r: _edit(r / "nodes" / "item-002.md", _id("kn", 2), _id("kn", 1)),
                           f"nodes/item-002.md: node_id {_id('kn', 1)} is already used in this bundle"),
    "item-title-missing": (lambda r: _drop_line(r / "nodes" / "item-001.md", "title: First node"),
                           "nodes/item-001.md: title is missing, empty or not text"),
    "item-title-is-a-date": (lambda r: _edit(r / "nodes" / "item-001.md", "title: First node", "title: 2026-09-30"),
                             "nodes/item-001.md: title is missing, empty or not text"),
    "item-no-front-matter": (lambda r: (r / "nodes" / "item-001.md").write_text("just prose\n"),
                             "nodes/item-001.md: does not open with a --- front matter line"),
    "item-front-matter-unclosed": (lambda r: (r / "nodes" / "item-001.md").write_text("---\ntype: node\n"),
                                   "nodes/item-001.md: front matter is never closed"),
    "item-front-matter-bad-yaml": (lambda r: (r / "nodes" / "item-001.md").write_text("---\ntitle: [x\n---\nb\n"),
                                   "nodes/item-001.md: front matter is not valid YAML"),
    "item-front-matter-not-a-mapping": (lambda r: (r / "nodes" / "item-001.md").write_text("---\n- a\n---\nb\n"),
                                        "nodes/item-001.md: front matter is not a mapping"),
    "item-not-utf8": (lambda r: (r / "nodes" / "item-001.md").write_bytes(b"\xff\xfe---\n"),
                      "nodes/item-001.md: is not valid UTF-8"),
    "file-over-the-bound": (lambda r: (r / "nodes" / "big.md").write_text(
        "---\ntype: node\n---\n" + "x" * pm.MAX_FILE_BYTES), "nodes/big.md: is "),
}


@pytest.mark.parametrize("name", sorted(_BREAKS))
def test_refusal_names_the_one_defect(tmp_path, name):
    root = _clean(tmp_path)
    pm.build_manifest([root])  # negative control: it builds before the break
    breaker, expected = _BREAKS[name]
    breaker(root)
    reasons = _refusal(root)[str(root)]
    assert len(reasons) == 1 and expected in reasons[0], reasons


def test_a_pack_with_no_items_is_refused(tmp_path):
    root = make_pack(tmp_path / "p")
    assert _refusal(root)[str(root)] == ["holds no items: a pack carries at least one node, lesson or guardrail"]


def test_every_reason_is_listed_not_the_first_only(tmp_path):
    root = _clean(tmp_path)
    _edit(root / "pack.md", "format_version: 1", "format_version: 2")
    _edit(root / "nodes" / "item-001.md", "type: node", "type: lesson")
    _drop_line(root / "nodes" / "item-002.md", "title: Second node")
    assert len(_refusal(root)[str(root)]) == 3


def test_one_bad_bundle_refuses_the_whole_request_and_only_names_the_bad_one(tmp_path):
    good = make_pack(tmp_path / "good", family=FAMILY_A, nodes=[(1, "Fine")])
    bad = make_pack(tmp_path / "bad", family=FAMILY_B)
    reasons = _refusal(good, bad)
    assert list(reasons) == [str(bad)]


def test_a_path_that_is_not_a_directory_is_refused(tmp_path):
    assert _refusal(tmp_path / "missing") == {str(tmp_path / "missing"): ["is not a directory"]}


def test_an_empty_request_is_refused(tmp_path):
    assert _refusal() == {"": ["no packs given"]}


def test_the_same_pack_twice_is_refused_at_the_second(tmp_path):
    first = _clean(tmp_path, "one")
    second = _clean(tmp_path, "two")
    assert _refusal(first, second) == {
        str(second): [f"pack_id {FAMILY_A}.en was already given as {first}"]}


def test_an_oversize_manifest_is_reported_once_not_also_as_missing(tmp_path):
    root = _clean(tmp_path)
    (root / "pack.md").write_text("---\ntype: pack-manifest\n---\n" + "x" * pm.MAX_FILE_BYTES)
    (reason,) = _refusal(root)[str(root)]
    assert reason.startswith("pack.md: is ") and "over this reader's" in reason


def test_an_oversize_only_item_is_reported_once_not_also_as_no_items(tmp_path):
    root = make_pack(tmp_path / "p")
    (root / "nodes").mkdir()
    (root / "nodes" / "big.md").write_text("---\ntype: node\n---\n" + "x" * pm.MAX_FILE_BYTES)
    (reason,) = _refusal(root)[str(root)]
    assert reason.startswith("nodes/big.md: is ") and "over this reader's" in reason


def test_one_bundle_given_as_a_bare_path_is_one_bundle(tmp_path):
    root = _clean(tmp_path)
    assert pm.build_manifest(root) == pm.build_manifest([root]) == pm.build_manifest([str(root)])
    assert pm.build_manifest(str(root))["totals"]["packs"] == 1


def test_a_relative_bundle_path_and_the_current_directory_work(tmp_path, monkeypatch):
    root = _clean(tmp_path)
    expected = pm.build_manifest([root])
    monkeypatch.chdir(tmp_path)
    assert pm.build_manifest([Path("pack")]) == expected
    monkeypatch.chdir(root)
    assert pm.build_manifest([Path(".")]) == expected


# --- untrusted bundles: links and pipes are refused, never followed or opened -------------------

def test_a_directory_that_cannot_be_listed_is_refused_not_skipped(tmp_path, monkeypatch):
    root = _clean(tmp_path)
    assert pm.build_manifest([root])["packs"][0]["nodes"] == 2  # control: it lists fine
    real = os.scandir

    def deny(path):
        if Path(path).name == "nodes":
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", deny)
    assert _refusal(root)[str(root)] == ["nodes: cannot be listed (Permission denied)"]

def test_symlinks_are_refused_and_their_targets_are_not_read(tmp_path):
    root = _clean(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(_doc(_item_front("nodes", 9, "Secret title")), encoding="utf-8")
    try:
        os.symlink(outside, root / "nodes" / "link.md")
        os.symlink(tmp_path, root / "up", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    reasons = _refusal(root)
    assert sorted(reasons[str(root)]) == [
        "nodes/link.md: is a symlink; a bundle is one self-contained directory",
        "up: is a symlink; a bundle is one self-contained directory"]
    assert "Secret title" not in json.dumps(reasons)


@pytest.mark.skipif(not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"), reason="POSIX only")
def test_a_pipe_is_refused_without_being_opened(tmp_path):
    root = _clean(tmp_path)
    os.mkfifo(root / "nodes" / "pipe.md")

    def _hung(*_):
        raise TimeoutError("the reader opened a FIFO and blocked")

    signal.signal(signal.SIGALRM, _hung)
    signal.alarm(10)
    try:
        reasons = _refusal(root)
    finally:
        signal.alarm(0)
    assert reasons[str(root)] == ["nodes/pipe.md: is not a regular file"]


# --- the command line ----------------------------------------------------------------------

def test_cli_prints_the_manifest(tmp_path, capsys):
    root = _clean(tmp_path)
    assert pm.main([str(root), "--samples", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == pm.build_manifest([root], samples=1)


def test_cli_prints_the_reasons_and_exits_1_on_refusal(tmp_path, capsys):
    root = make_pack(tmp_path / "p")
    assert pm.main([str(root)]) == 1
    assert json.loads(capsys.readouterr().out) == {"refused": {str(root): [
        "holds no items: a pack carries at least one node, lesson or guardrail"]}}


def test_cli_rejects_bad_usage_with_exit_2(tmp_path, capsys):
    assert pm.main([str(_clean(tmp_path)), "--samples", "-1"]) == 2
    assert "samples must be 0 or more" in capsys.readouterr().err
    with pytest.raises(SystemExit) as caught:
        pm.main([])
    assert caught.value.code == 2
