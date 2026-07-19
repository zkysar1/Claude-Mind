"""Tests for knowledge_projection — the durable-store analogue of SafeEventProjection.

Heavy on the two security-critical properties: (1) redaction never emits a secret,
host path, agent name, or framework id; (2) the applies_to-less stores (guardrails,
hypotheses) fail CLOSED — an untagged or framework entry is suppressed, never leaked.
"""

from __future__ import annotations

from knowledge_projection import (
    FRAMEWORK_TREE_ROOT,
    ProjectedBundle,
    Redactor,
    domain_categories,
    is_domain_tree_node,
    is_exposed_by_category,
    is_exposed_reasoning,
    project,
    redact,
    top_level_category,
)


# ── top_level_category ───────────────────────────────────────────────────────

def test_top_level_category_variants() -> None:
    assert top_level_category("system/hooks/foo") == "system"
    assert top_level_category("intelligence/npc") == "intelligence"
    assert top_level_category("intelligence") == "intelligence"
    assert top_level_category("/system/foo/") == "system"
    assert top_level_category("") == ""
    # A full node file path drops the world/knowledge/tree/ prefix.
    assert top_level_category("world/knowledge/tree/system/foo.md") == "system"
    assert top_level_category("world/knowledge/tree/marine-biology/reefs.md") == "marine-biology"
    # A top-level category ROOT node file is tree/<cat>.md — the extension must be
    # stripped so it classifies under its category, not "<cat>.md" (else the framework
    # root system.md would read as a domain category and leak).
    assert top_level_category("world/knowledge/tree/system.md") == "system"
    assert top_level_category("world/knowledge/tree/intelligence.md") == "intelligence"


# ── tree filter ──────────────────────────────────────────────────────────────

def test_is_domain_tree_node() -> None:
    assert is_domain_tree_node("marine-biology/reefs") is True
    assert is_domain_tree_node("system/hooks") is False
    assert is_domain_tree_node("world/knowledge/tree/system/x.md") is False
    # The framework subtree ROOT node file must be suppressed too (the .md-strip fix).
    assert is_domain_tree_node("world/knowledge/tree/system.md") is False
    assert is_domain_tree_node("") is False


def test_domain_categories_excludes_system() -> None:
    nodes = [
        {"category": "marine-biology/reefs"},
        {"file": "world/knowledge/tree/astronomy/stars.md"},
        {"category": "system/hooks"},  # framework — must not enter the allowlist
    ]
    assert domain_categories(nodes) == frozenset({"marine-biology", "astronomy"})


# ── reasoning-bank filter (reliable applies_to) ──────────────────────────────

def test_is_exposed_reasoning() -> None:
    assert is_exposed_reasoning({"applies_to": "domain"}) is True
    assert is_exposed_reasoning({"applies_to": "any"}) is True
    assert is_exposed_reasoning({"applies_to": "framework"}) is False
    assert is_exposed_reasoning({"applies_to": "specific"}) is False
    assert is_exposed_reasoning({}) is False  # missing → suppressed


# ── guardrail / hypothesis filter — the fail-closed allowlist ────────────────

def test_is_exposed_by_category_fails_closed() -> None:
    allow = frozenset({"marine-biology", "astronomy"})
    assert is_exposed_by_category({"category": "marine-biology/method"}, allow) is True
    # A framework category NOT in the allowlist is suppressed.
    assert is_exposed_by_category({"category": "framework-architecture"}, allow) is False
    assert is_exposed_by_category({"category": "system/hooks"}, allow) is False
    # Missing category fails closed — this is the security guarantee for the
    # applies_to-less stores (an untagged framework guardrail must NOT leak).
    assert is_exposed_by_category({}, allow) is False
    assert is_exposed_by_category({"category": ""}, allow) is False


# ── redaction (security-critical) ────────────────────────────────────────────

def test_redact_never_emits_a_secret() -> None:
    secret = "gsk_live_abcdef0123456789SECRETvalue"
    out = redact(f"my key is {secret} do not show", secret_values=[secret])
    assert secret not in out
    assert "gsk_live" not in out
    assert "[redacted]" in out


def test_redact_paths_posix_and_windows() -> None:
    assert "[path]" in redact("see /home/ec2-user/mind-workspace/world/x.md")
    assert "/home/ec2-user" not in redact("see /home/ec2-user/mind-workspace/world/x.md")
    assert "[path]" in redact(r"open <PROJECT_ROOT>\core")
    # An explicit workspace path is also collapsed.
    out = redact("under /srv/tricks-ws/research", workspace_paths=["/srv/tricks-ws"])
    assert "/srv/tricks-ws" not in out


def test_redact_agent_names_case_insensitive_word_boundary() -> None:
    out = redact("Alpha handed off to bravo", agent_names=["alpha", "bravo"])
    assert "the agent" in out
    assert "alpha" not in out.lower()
    assert "bravo" not in out.lower()
    # Word-boundary: a substring inside another word is NOT rewritten.
    assert "alphabet" in redact("the alphabet", agent_names=["alpha"])


def test_redact_strips_framework_ids() -> None:
    out = redact("per rb-2859 and guard-321 and g-115-2119 see cleanup.sh / _paths.py")
    assert "rb-2859" not in out
    assert "guard-321" not in out
    assert "g-115-2119" not in out
    assert "cleanup.sh" not in out
    assert "_paths.py" not in out


def test_redact_strips_source_file_refs_with_line_suffix() -> None:
    # Code-file references (incl. a :NNN line suffix) are stripped as defense-in-depth —
    # a domain node's prose citing code must not leak class/file internals.
    out = redact("build SHA via Driver.java:1718 and app.ts and mod.go and main.lua")
    assert "Driver.java" not in out
    assert "1718" not in out
    assert "app.ts" not in out
    assert "mod.go" not in out
    assert "main.lua" not in out
    # But ordinary numbered prose with a dot is NOT a filename and survives.
    assert "3.c" in redact("see subsection 3.c for details")


def test_redact_strips_code_identifiers_but_keeps_acronyms() -> None:
    out = redact("the loop calls filter_actions() and reads _BFS_MAX_NODES each tick")
    assert "filter_actions()" not in out
    assert "_BFS_MAX_NODES" not in out
    # Legitimate all-caps domain acronyms (no leading underscore) survive.
    keep = redact("NASA studies DNA and H2O on Mars")
    assert "NASA" in keep and "DNA" in keep and "H2O" in keep


def test_redact_preserves_ordinary_prose() -> None:
    text = "Coral reefs host a quarter of marine species; bleaching is a warming signal."
    assert redact(text) == text


def test_redactor_dataclass_applies_all_classes() -> None:
    r = Redactor(agent_names=("tricks",), workspace_paths=("/srv/ws",), secret_values=("s3cr3ttoken",))
    out = r("tricks wrote s3cr3ttoken to /srv/ws/notes about reefs")
    assert "tricks" not in out.lower()
    assert "s3cr3ttoken" not in out
    assert "/srv/ws" not in out
    assert "reefs" in out  # the actual content survives


# ── project() integration ────────────────────────────────────────────────────

def _fixtures() -> dict[str, list[dict[str, object]]]:
    return {
        "tree_nodes": [
            {"key": "reefs", "category": "marine-biology/reefs", "title": "Coral reefs",
             "summary": "Reefs studied by alpha at /home/x/world.", "parent": "marine-biology",
             "children": ["bleaching"]},
            {"key": "hooks", "category": "system/hooks", "title": "Hook internals",
             "summary": "framework plumbing", "parent": "system", "children": []},
        ],
        "reasoning": [
            {"applies_to": "domain", "category": "marine-biology/method",
             "title": "Cross-check sources",
             "failure_lesson": "Trusted one source once; it was wrong."},
            {"applies_to": "framework", "category": "framework-architecture",
             "title": "Never inline $VAR", "failure_lesson": "guard-165 plumbing detail"},
            # applies_to=any but a FRAMEWORK category — the leaky-engineering-lesson case
            # the intersection filter must suppress (loose applies_to alone would expose it).
            {"applies_to": "any", "category": "infrastructure",
             "title": "BFS cap is a backstop",
             "failure_lesson": "_BFS_MAX_NODES=1024 in ReachabilityNav.plan_route"},
        ],
        "guardrails": [
            {"category": "marine-biology/method", "rule": "Verify every claim against two sources."},
            {"category": "framework-architecture", "rule": "Never critical() in a handler."},
            {"rule": "Untagged framework guardrail — must fail closed."},
        ],
        "hypotheses": [
            {"category": "marine-biology/reefs", "claim": "Warmer water bleaches reefs faster.",
             "horizon": "short", "stage": "resolved", "outcome": "Confirmed by alpha."},
            {"category": "system/loop", "claim": "The veto budget bounds continuations.",
             "horizon": "session", "stage": "active", "outcome": ""},
        ],
    }


def test_project_suppresses_framework_and_exposes_domain() -> None:
    f = _fixtures()
    bundle = project(
        tree_nodes=f["tree_nodes"], reasoning=f["reasoning"],
        guardrails=f["guardrails"], hypotheses=f["hypotheses"],
        redactor=Redactor(agent_names=("alpha",)),
    )
    assert isinstance(bundle, ProjectedBundle)
    # Exactly the domain entries survive each store.
    assert bundle.counts() == {"tree": 1, "hypotheses": 1, "guardrails": 1, "lessons": 1}
    assert bundle.tree[0]["key"] == "reefs"
    assert bundle.hypotheses[0]["horizon"] == "short"
    assert bundle.guardrails[0]["rule"].startswith("Verify every claim")
    assert "Cross-check" in bundle.lessons[0]["title"]


def test_project_redacts_exposed_strings() -> None:
    f = _fixtures()
    bundle = project(
        tree_nodes=f["tree_nodes"], reasoning=f["reasoning"],
        guardrails=f["guardrails"], hypotheses=f["hypotheses"],
        redactor=Redactor(agent_names=("alpha",)),
    )
    # The reef node's summary named alpha + a path — both redacted, content kept.
    summary = bundle.tree[0]["summary"]
    assert "alpha" not in summary.lower()
    assert "/home/x" not in summary
    assert "Reefs studied by the agent" in summary
    # The exposed hypothesis outcome named alpha.
    assert "alpha" not in bundle.hypotheses[0]["outcome"].lower()


def test_project_suppresses_any_tagged_framework_lesson() -> None:
    # The intersection filter (applies_to AND domain-category) must drop an
    # applies_to=any lesson whose category is framework/infrastructure — its prose
    # carries internal identifiers the redaction cannot strip.
    f = _fixtures()
    bundle = project(
        tree_nodes=f["tree_nodes"], reasoning=f["reasoning"],
        guardrails=f["guardrails"], hypotheses=f["hypotheses"],
        redactor=Redactor(),
    )
    assert len(bundle.lessons) == 1  # only the marine-biology/method lesson survives
    blob = " ".join(lesson["lesson"] for lesson in bundle.lessons)
    assert "_BFS_MAX_NODES" not in blob  # the leaky engineering lesson is gone
    assert "ReachabilityNav" not in blob


def test_project_never_leaks_the_framework_subtree_category() -> None:
    # No exposed entry may carry a system/ or framework category signal.
    f = _fixtures()
    bundle = project(
        tree_nodes=f["tree_nodes"], reasoning=f["reasoning"],
        guardrails=f["guardrails"], hypotheses=f["hypotheses"],
        redactor=Redactor(),
    )
    assert all(FRAMEWORK_TREE_ROOT not in str(t.get("parent")) for t in bundle.tree)
    # The untagged guardrail and the system hypothesis are gone.
    rules = [g["rule"] for g in bundle.guardrails]
    assert not any("fail closed" in r for r in rules)
    assert not any("critical()" in r for r in rules)
    assert all(h["status"] != "active" for h in bundle.hypotheses)
