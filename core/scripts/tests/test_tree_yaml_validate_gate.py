"""Regression for  — the Layer-B PreToolUse tree-YAML parse gate.

The gate (core/scripts/tree-yaml-validate-gate.py) must REFUSE an
Edit/Write/MultiEdit that would make a knowledge-tree file (_tree.yaml or a node
*.md) unparseable as YAML, and APPROVE everything else (parse-clean edits,
non-tree files, and edits to an already-broken file — which may be the fix).

The canonical break it must catch (the g-115-1067 incident shape) is an unquoted
scalar with an internal colon-space ("Option D: client_type ...") that PyYAML
rejects with "mapping values are not allowed here" — exactly the multi-line
unquoted-scalar-with-internal-colon failure mode named in the goal spec.

Hook contract under test: empty stdout + exit 0 = approve; structured JSON on
stdout carrying permissionDecision=deny = refuse.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
HOOK = CORE_SCRIPTS / "tree-yaml-validate-gate.py"
PY = sys.executable

# A clean, parseable _tree.yaml fragment used as the pre-edit baseline.
VALID = (
    "nodes:\n"
    "  child:\n"
    "    file: world/knowledge/tree/c.md\n"
    "    summary: clean summary\n"
    "    depth: 1\n"
)


def _tree_file(tmp, content):
    """Write `content` to a _tree.yaml under a knowledge/tree/ dir (so the gate's
    path detector fires) and return its path."""
    d = Path(tmp) / "world" / "knowledge" / "tree"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "_tree.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _run(payload):
    return subprocess.run(
        [PY, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
    )


def _is_deny(result):
    return "permissionDecision" in result.stdout and "deny" in result.stdout


def test_edit_introducing_colon_break_is_denied():
    """The core case: an Edit that injects an unquoted scalar with an internal
    colon-space must be refused."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, VALID)
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(p),
                "old_string": "summary: clean summary",
                "new_string": "summary: Option D: client_type dispatch reverted problem",
            },
        }
        r = _run(payload)
        assert r.returncode == 0, f"hook must exit 0, got {r.returncode}"
        assert _is_deny(r), f"expected deny, got stdout={r.stdout!r} stderr={r.stderr!r}"


def test_valid_edit_is_approved():
    """A parse-clean Edit must pass (validate, do not restrict the path)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, VALID)
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(p),
                "old_string": "summary: clean summary",
                "new_string": "summary: updated but still clean summary",
            },
        }
        r = _run(payload)
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"expected approve (empty stdout), got {r.stdout!r}"


def test_write_breaking_content_is_denied():
    """A full Write whose new content does not parse must be refused."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, VALID)
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(p),
                "content": "nodes:\n  child:\n    summary: Bad: two: colons here\n",
            },
        }
        r = _run(payload)
        assert r.returncode == 0
        assert _is_deny(r), f"breaking Write must deny, got {r.stdout!r}"


def test_non_tree_file_is_approved():
    """A file outside knowledge/tree/ is never gated, even with breaking YAML."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "notes.yaml"
        p.write_text(VALID, encoding="utf-8")
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(p),
                "old_string": "summary: clean summary",
                "new_string": "summary: Option D: client_type dispatch",
            },
        }
        r = _run(payload)
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"non-tree file must approve, got {r.stdout!r}"


def test_edit_to_already_broken_tree_is_approved():
    """An edit to a tree file that is ALREADY unparseable may be the fix — so it
    must be allowed (the gate blocks only edits that INTRODUCE a break)."""
    broken = "nodes:\n  child:\n    summary: Option D: already broken value\n"
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, broken)
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(p),
                "old_string": "Option D: already broken value",
                "new_string": "fixed clean value",
            },
        }
        r = _run(payload)
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"fix-path edit must approve, got {r.stdout!r}"


def test_node_md_front_matter_break_is_denied():
    """A node *.md whose proposed front matter would not parse must be refused;
    only the front matter is validated, not the markdown body."""
    valid_md = (
        "---\n"
        "key: system/example\n"
        "summary: a clean node summary\n"
        "depth: 2\n"
        "---\n"
        "# Body\n\nProse here with a colon: this line is body, never validated.\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "world" / "knowledge" / "tree" / "system"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "example.md"
        p.write_text(valid_md, encoding="utf-8")
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(p),
                "old_string": "summary: a clean node summary",
                "new_string": "summary: Option D: client_type dispatch problem",
            },
        }
        r = _run(payload)
        assert r.returncode == 0
        assert _is_deny(r), f"front-matter break must deny, got {r.stdout!r}"


def test_node_md_body_only_edit_is_approved():
    """Editing only the markdown body (front matter untouched) must pass even when
    the body contains colons — the body is not YAML."""
    valid_md = (
        "---\n"
        "key: system/example\n"
        "summary: a clean node summary\n"
        "---\n"
        "# Body\n\nOriginal body line.\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "world" / "knowledge" / "tree" / "system"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "example.md"
        p.write_text(valid_md, encoding="utf-8")
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(p),
                "old_string": "Original body line.",
                "new_string": "Rewritten body: now with an internal colon, still fine.",
            },
        }
        r = _run(payload)
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"body-only edit must approve, got {r.stdout!r}"


# ── : body-presence gate (Pattern-B index-body desync producer) ──
# A direct _tree.yaml edit that ADDS a node key must have that node's .md body
# already on disk. Prevents the orphaned-index-entry class the recurring
# body-presence audit () otherwise catches after the fact.

def _add_key_edit(tree_path, extra_node_yaml):
    """Edit payload that appends `extra_node_yaml` (a 'nodes' child block) to the
    VALID baseline."""
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(tree_path),
            "old_string": "    depth: 1\n",
            "new_string": "    depth: 1\n" + extra_node_yaml,
        },
    }


def test_add_node_key_missing_body_is_denied():
    """Adding a _tree.yaml node key whose .md body does not exist must be refused."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, VALID)
        payload = _add_key_edit(
            p,
            "  orphan:\n"
            "    file: world/knowledge/tree/orphan.md\n"
            "    summary: has no body on disk\n",
        )
        r = _run(payload)
        assert r.returncode == 0
        assert _is_deny(r), f"missing-body add must deny, got {r.stdout!r}"
        assert "body-presence" in r.stdout


def test_add_node_key_with_existing_body_is_approved():
    """Adding a node key whose .md body already exists must pass (body-first)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, VALID)
        body = Path(tmp) / "world" / "knowledge" / "tree" / "hasbody.md"
        body.write_text("---\ntopic: has body\n---\n", encoding="utf-8")
        payload = _add_key_edit(
            p,
            "  hasbody:\n"
            "    file: world/knowledge/tree/hasbody.md\n"
            "    summary: body exists first\n",
        )
        r = _run(payload)
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"existing-body add must approve, got {r.stdout!r}"


def test_add_interior_node_without_file_is_approved():
    """A new interior node with no 'file:' field has no body requirement."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, VALID)
        payload = _add_key_edit(
            p,
            "  interior:\n"
            "    summary: interior-only node, no body\n",
        )
        r = _run(payload)
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"interior add must approve, got {r.stdout!r}"


def test_summary_only_edit_adds_no_key_is_approved():
    """An edit that changes a summary but adds no new node key must pass even
    though the baseline node's own body does not exist on disk (only NEW keys
    are gated)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _tree_file(tmp, VALID)
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(p),
                "old_string": "summary: clean summary",
                "new_string": "summary: reworded clean summary",
            },
        }
        r = _run(payload)
        assert r.returncode == 0
        assert r.stdout.strip() == "", f"summary-only edit must approve, got {r.stdout!r}"


# ── : duplicate-key gate ────────────────────────────────────────────
# A duplicate key is NOT a parse error, so every test above it here passes while
# the value silently vanishes: yaml.safe_load accepts the document, raises
# nothing, and keeps only the LAST occurrence (guard-2388). Measured 2026-08-29
# across 2,940 live tree nodes: 19 had accumulated repeated front-matter keys,
# worst `.../arc-agi-3/arc-environment-models/ls20-class.md` with `prior_source`
# SEVEN times. No script produces that shape -- a dict cannot hold two identical
# keys -- so the producer is hand-editing, which is exactly the concurrent-
# authorship hazard guard-2388 describes.

def _node_md(tmp, content, name="example.md"):
    d = Path(tmp) / "world" / "knowledge" / "tree" / "system"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


_CLEAN_NODE = (
    "---\n"
    "key: system/example\n"
    "summary: a clean node summary\n"
    "last_update_trigger:\n"
    "  type: fresh-eyes\n"
    "  source: 'pass A (2026-08-27)'\n"
    "---\n"
    "# Body\n\nline.\n"
)


def test_edit_introducing_a_duplicate_front_matter_key_is_denied():
    """THE defining property. RED before ."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _node_md(tmp, _CLEAN_NODE)
        r = _run({"tool_name": "Edit", "tool_input": {
            "file_path": str(p),
            "old_string": "  source: 'pass A (2026-08-27)'",
            "new_string": ("  source: 'pass A (2026-08-27)'\n"
                           "  source: 'pass B (2026-08-29)'"),
        }})
        assert r.returncode == 0
        assert _is_deny(r), (
            "adding a second `source:` to the same mapping must be denied — it "
            f"parses clean and destroys pass A silently. got: {r.stdout!r}")
        assert "source" in r.stdout, "the deny must name the offending key"


def test_duplicate_nested_deeper_than_top_level_is_caught():
    """The production shape. Every real instance measured was NESTED (under
    `last_update_trigger:`), not a top-level key, so a checker that only walked
    the outermost mapping would have reported all 2,940 nodes clean."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _node_md(tmp, _CLEAN_NODE)
        r = _run({"tool_name": "Edit", "tool_input": {
            "file_path": str(p),
            "old_string": "  type: fresh-eyes",
            "new_string": "  type: fresh-eyes\n  prior_source: x\n  prior_source: y",
        }})
        assert _is_deny(r), f"nested duplicate must be denied, got {r.stdout!r}"
        assert "prior_source" in r.stdout


def test_going_from_two_to_three_occurrences_is_denied():
    """Counter semantics, not set semantics. A set-based check would see
    'prior_source is already duplicated' and wave the edit through, which is how
    ls20-class.md reached SEVEN copies one edit at a time."""
    already_dup = _CLEAN_NODE.replace(
        "  type: fresh-eyes",
        "  type: fresh-eyes\n  prior_source: one\n  prior_source: two")
    with tempfile.TemporaryDirectory() as tmp:
        p = _node_md(tmp, already_dup)
        r = _run({"tool_name": "Edit", "tool_input": {
            "file_path": str(p),
            "old_string": "  prior_source: two",
            "new_string": "  prior_source: two\n  prior_source: three",
        }})
        assert _is_deny(r), f"2 -> 3 occurrences must be denied, got {r.stdout!r}"


def test_an_edit_that_REPAIRS_an_existing_duplicate_is_approved():
    """The gate denies only what an edit INTRODUCES — the same 'current must be
    clean' rule the parse gate uses. Without this the 19 already-affected nodes
    would be unfixable: the repair itself would be blocked."""
    already_dup = _CLEAN_NODE.replace(
        "  type: fresh-eyes",
        "  type: fresh-eyes\n  prior_source: one\n  prior_source: two")
    with tempfile.TemporaryDirectory() as tmp:
        p = _node_md(tmp, already_dup)
        r = _run({"tool_name": "Edit", "tool_input": {
            "file_path": str(p),
            "old_string": "  prior_source: one\n  prior_source: two",
            "new_string": "  prior_source: two",
        }})
        assert r.returncode == 0
        assert r.stdout.strip() == "", (
            f"repairing a duplicate must be allowed through, got {r.stdout!r}")


def test_adding_a_DISTINCT_key_is_still_approved():
    """Anti-vacuity twin (guard-1220). A gate that denied every front-matter
    addition would pass all four tests above while making the tree uneditable."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _node_md(tmp, _CLEAN_NODE)
        r = _run({"tool_name": "Edit", "tool_input": {
            "file_path": str(p),
            "old_string": "  type: fresh-eyes",
            "new_string": "  type: fresh-eyes\n  prior_source: 'pass A (2026-08-27)'",
        }})
        assert r.returncode == 0
        assert r.stdout.strip() == "", (
            f"a distinct new key must still be approved, got {r.stdout!r}")
