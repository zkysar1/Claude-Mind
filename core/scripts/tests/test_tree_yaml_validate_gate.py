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
