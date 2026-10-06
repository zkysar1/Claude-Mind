"""Tests for knowledge_projection — the durable-store analogue of SafeEventProjection.

Heavy on the two security-critical properties: (1) redaction never emits a secret,
host path, agent name, or framework id; (2) the applies_to-less stores (guardrails,
hypotheses) fail CLOSED — an untagged or framework entry is suppressed, never leaked.
"""

from __future__ import annotations

import pytest

import knowledge_projection as kp
from knowledge_projection import (
    _PROGRAM_PURPOSE_CAP,
    _SELF_PURPOSE_CAP,
    goal_handle,
    project_goals,
    resolve_goal_handle,
    project_program,
    project_self,
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


def test_redact_strips_secrets_never_injected() -> None:
    """The tier-(b)/(c) case: a credential whose value the box never held.

    Exact-value redaction only sees ``secret_values``; a key the agent wrote into a
    research note or pasted from a log is invisible to it and would otherwise reach a
    PUBLIC endpoint verbatim. Each case passes NO secret_values on purpose.

    Per guard-1270, every assertion is a derived boolean — the candidate secret is never
    printed, only tested for absence.
    """
    cases = [
        # (text, the substring that must NOT survive)
        ("the key is gsk_live_abcdef0123456789SECRETvalue ok", "gsk_live_abcdef0123456789SECRETvalue"),
        ("bearer sk-proj-abcdef0123456789ABCDEF fine", "sk-proj-abcdef0123456789ABCDEF"),
        # AWS's canonical DOCUMENTATION placeholder, used here as a redaction
        # fixture — not a credential. (A test for secret-stripping necessarily
        # contains secret-shaped strings; that is the point of the fixture.)
        ("aws AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),  # secret-scanner: skip
        ("opaque aZ9x2Qm7Lp4Wd8Rt6Yv3Nb1Kc5Hj0Fg tail", "aZ9x2Qm7Lp4Wd8Rt6Yv3Nb1Kc5Hj0Fg"),
        ("API_KEY=hunter2plaintext done", "hunter2plaintext"),
        ("-----BEGIN PRIVATE KEY-----\nMIIBVQIBADAN\n-----END PRIVATE KEY-----", "MIIBVQIBADAN"),
    ]
    for text, must_not_survive in cases:
        out = redact(text)
        assert must_not_survive not in out
        assert "[redacted]" in out


def test_redact_strips_url_embedded_credentials_but_keeps_the_host() -> None:
    # Shape preservation matters even here: the reader should still see WHICH service was
    # referenced, just not the credentials used to reach it.
    out = redact("see https://svcuser:hunter2pass@example.com/docs")
    assert "hunter2pass" not in out
    assert "svcuser" not in out
    assert "example.com" in out


def test_redact_entropy_backstop_spares_ordinary_prose() -> None:
    # The catch-all is bounded (>=25 chars AND entropy >4.5) so the domain vocabulary this
    # wiki exists to publish is never eaten. A false positive here silently deletes real
    # knowledge, so this guard is as load-bearing as the leak tests above.
    prose = "Photosynthesis converts carbon dioxide into glucose inside chloroplasts"
    assert redact(prose) == prose


def test_redact_entropy_pass_preserves_line_structure() -> None:
    """fresh-eyes F-2: the entropy pass must not eat prose or newlines.

    Node bodies are multi-line markdown. Tokenizing on " " alone made a newline part
    of the token, so "intro\\n<blob>" scored as ONE high-entropy token and the marker
    replaced the prose AND the line break. The secret must still go; everything around
    it must survive byte-for-byte.
    """
    blob = "aZ9x2Qm7Lp4Wd8Rt6Yv3Nb1Kc5Hj0Fg"
    out = redact(f"Reefs bleach above 30C.\n\nLogged {blob} during the run.\n- bullet")
    assert blob not in out
    assert "Reefs bleach above 30C." in out
    assert "during the run." in out
    assert "\n\n" in out and "\n- bullet" in out


def test_redact_strips_unterminated_pem_block() -> None:
    """fresh-eyes F-4: a key captured from a TRUNCATED log has BEGIN and no END.

    Requiring the closing fence let the key body through verbatim. The body is short
    enough to slip under the entropy floor too, so this was a real leak path with no
    backstop. Consuming to end-of-string is the correct (fail-closed) direction.
    """
    assert "MIIBVQIBADAN" not in redact("-----BEGIN PRIVATE KEY-----\nMIIBVQIBADAN\n")


def test_redact_paths_posix_and_windows() -> None:
    assert "[path]" in redact("see /home/ec2-user/mind-workspace/world/x.md")
    assert "/home/ec2-user" not in redact("see /home/ec2-user/mind-workspace/world/x.md")
    assert "[path]" in redact(r"open <PROJECT_ROOT>\core")
    # An explicit workspace path is also collapsed.
    out = redact("under /srv/tricks-ws/research", workspace_paths=["/srv/tricks-ws"])
    assert "/srv/tricks-ws" not in out


#: Slashes that are prose, not paths (). The loose path pattern turned each of these
#: into "word[path]", and a row so altered could not be corrected by its member.
_PROSE_WITH_SLASHES = (
    "Use and/or here.",
    "Due 9/30/2026 on dev/main.",
    "TCP/IP, 24/7 and 3/4 of the reef.",
    "Either (and/or) yes/no, his/her, 2026/10/05.",
)

#: Slash runs that say where something lives, each with a fragment that must NOT survive.
#: They must stay redacted whatever the prose rule keeps whole: redaction is a privacy
#: control, and narrowing a matcher that forbids shrinks what it forbids (guard-1901).
_PATHS_THAT_STAY_REDACTED = {
    "rooted posix": ("see /etc/hosts now", "etc/hosts"),
    "rooted and prose-shaped": ("see /and/or now", "and/or"),
    "windows drive, backslash": (r"open C:\Users\me\x now", "Users"),
    "windows drive, slash": ("open C:/Users/me/x now", "Users"),
    "home tilde": ("see ~/projects/x now", "projects"),
    "home of a named user, two words": ("cd ~deploy/projects now", "deploy/projects"),
    "home of a named user, hidden dir": ("InaccessiblePaths=-~deploy/.cache", ".cache"),
    "user at host, two words": ("scp admin@box/data now", "box/data"),
    "variable-rooted, two words": ("cd $HOME/projects now", "HOME/projects"),
    "variable-rooted, three segments": ("run $HOME/.local/bin/uv now", ".local/bin/uv"),
    "relative, three segments": ("see world/knowledge/tree now", "knowledge/tree"),
    "relative, with an extension": ("see notes/summary.txt now", "summary.txt"),
    "relative, hidden segment": ("see dir/.hidden now", ".hidden"),
    "three words": ("read/write/execute", "write/execute"),
    "hyphenated name": ("arn user/fleet-agent now", "fleet-agent"),
    "underscored name": ("role assumed-role/Test_role now", "Test_role"),
    "name with a digit": ("moved node/web-02 now", "web-02"),
    "dotted host and one segment": ("see www.example.com/page now", "/page"),
    "url path": ("see https://example.com/a/b now", "/a/b"),
    "url port path": ("curl http://localhost:8080/api/v1/logs", "api/v1/logs"),
    "port and one segment": ("curl localhost:3000/health now", "/health"),
}


def test_redact_keeps_prose_slashes_whole() -> None:
    for text in _PROSE_WITH_SLASHES:
        assert redact(text) == text, text
    # The sentence the loose pattern mangled into "Use and[path] here. Due 9[path] on
    # dev[path] Real path [path]": now only the path goes.
    assert redact("Use and/or here. Due 9/30/2026 on dev/main. Real path /etc/hosts.") == (
        "Use and/or here. Due 9/30/2026 on dev/main. Real path [path]"
    )


def test_redact_still_redacts_every_slash_run_that_is_a_path() -> None:
    for why, (text, gone) in _PATHS_THAT_STAY_REDACTED.items():
        out = redact(text)
        assert "[path]" in out, why
        assert gone not in out, why


def test_redact_never_raises_on_slash_heavy_text() -> None:
    """The prose rule reads back from the match, so it must hold for every edge of a string."""
    from itertools import product

    for n in range(1, 5):
        for chars in product("a1 /.\\$~@", repeat=n):
            text = "".join(chars)
            assert isinstance(redact(text), str), repr(text)


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
            {"status": "active", "category": "marine-biology/method", "rule": "Verify every claim against two sources."},
            {"status": "active", "category": "framework-architecture", "rule": "Never critical() in a handler."},
            {"status": "active", "rule": "Untagged framework guardrail — must fail closed."},
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
    assert bundle.counts() == {
        "tree": 1, "hypotheses": 1, "guardrails": 1, "lessons": 1, "self": 0, "goals": 0, "program": 0,
    }  # no self.md passed to project() -> `self` projects empty
    assert bundle.tree[0]["key"] == "reefs"
    assert bundle.hypotheses[0]["horizon"] == "short"
    assert bundle.guardrails[0]["rule"].startswith("Verify every claim")
    assert "Cross-check" in bundle.lessons[0]["title"]


def test_project_preserves_parent_child_links() -> None:
    """Shape preservation (PEARL §10.3): an exposed node keeps its graph edges.

    The sibling assertions all check what must be REMOVED; this checks what must SURVIVE.
    Without it a refactor that treated parent/children as framework internals would flatten
    the wiki into an unnavigable list while every other test in this file still passed.
    """
    f = _fixtures()
    bundle = project(
        tree_nodes=f["tree_nodes"], reasoning=f["reasoning"],
        guardrails=f["guardrails"], hypotheses=f["hypotheses"],
        redactor=Redactor(agent_names=("alpha",)),
    )
    node = bundle.tree[0]
    assert node["parent"] == "marine-biology"
    assert node["children"] == ["bleaching"]


def test_project_carries_last_updated_with_absent_control() -> None:
    """A wiki client's "what changed since your last visit" is keyed on this field.

    Sibling of the parent/child test above — it checks what must SURVIVE, and this
    field survived NOTHING until g-335-1146: two independent field-by-field dict
    rebuilds (here and read_tree_nodes) each named six keys and dropped it, which is
    invisible because a comprehension listing its keys looks identical whether or not
    it lists them all.

    The NEGATIVE CONTROL is the load-bearing half (guard-3221). Asserting only that a
    dated node keeps its date passes against any implementation that emits a non-empty
    string, including one that fabricates a default. The consumer distinguishes "no
    date" from "old" and goes silent rather than claiming nothing changed — which only
    works if an undated node arrives as "" instead of a plausible-looking stamp.
    """
    nodes = [
        {"key": "dated", "category": "marine-biology/reefs", "title": "Dated",
         "summary": "", "parent": "marine-biology", "children": [],
         "last_updated": "2026-04-28"},
        {"key": "undated", "category": "marine-biology/reefs", "title": "Undated",
         "summary": "", "parent": "marine-biology", "children": []},
    ]
    bundle = project(
        tree_nodes=nodes, reasoning=[], guardrails=[], hypotheses=[],
        redactor=Redactor(agent_names=("alpha",)),
    )
    by_key = {n["key"]: n for n in bundle.tree}
    assert by_key["dated"]["last_updated"] == "2026-04-28"
    # Control: present as a key, empty as a value. A MISSING key would also fail the
    # consumer safely, but "" is the contract the exporter and the client agree on.
    assert by_key["undated"]["last_updated"] == ""


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


def test_project_exposes_redacted_body() -> None:
    # A domain node's full .md body is exposed as its own field, redacted, and distinct
    # from the summary (the click-through content the PEARL UI renders).
    node = {"key": "reefs", "category": "marine-biology/reefs", "title": "Coral reefs",
            "summary": "Short reef summary.",
            "body": "Full article: reefs studied by alpha at /home/x/world in depth.",
            "parent": "marine-biology", "children": []}
    bundle = project(tree_nodes=[node], reasoning=[], guardrails=[], hypotheses=[],
                     redactor=Redactor(agent_names=("alpha",)))
    body = bundle.tree[0]["body"]
    assert "Full article" in body
    assert "alpha" not in body.lower()
    assert "/home/x" not in body
    assert "the agent" in body
    assert body != bundle.tree[0]["summary"]


def test_project_never_exposes_framework_node_body() -> None:
    # The system/ subtree is suppressed wholesale — its body must never reach the bundle,
    # even though summary-suppression is the property test_project_never_leaks... covers.
    nodes = [
        {"key": "reefs", "category": "marine-biology/reefs", "title": "Reefs",
         "summary": "s", "body": "domain reef body", "parent": "marine-biology",
         "children": []},
        {"key": "hooks", "category": "system/hooks", "title": "Hooks",
         "summary": "s", "body": "SECRET framework plumbing internals", "parent": "system",
         "children": []},
    ]
    bundle = project(tree_nodes=nodes, reasoning=[], guardrails=[], hypotheses=[],
                     redactor=Redactor())
    assert len(bundle.tree) == 1  # framework node suppressed entirely
    blob = " ".join(str(t.get("body")) for t in bundle.tree)
    assert "framework plumbing" not in blob


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


def test_self_purpose_redacts_before_capping_so_a_straddling_token_cannot_leak():
    """A redactable token straddling the cap must not leave its prefix in the output.

    THE ORDERING IS THE WHOLE TEST. Capping first bisects the SOURCE token, so the
    redactor's pattern no longer matches the surviving fragment and the prefix is
    published; redacting first can only ever bisect a "[redacted]" placeholder, which
    is harmless. A token placed WHOLLY INSIDE the cap passes under BOTH orderings and
    is therefore not coverage -- it is asserted below only as the control.

    Not hypothetical: measured 2026-08-29, pre-## prose is 1824 B (alpha) and 2053 B
    (bravo) against a 1200-char cap, so this boundary is crossed in production today.
    Pre-fix, this test's first assertion failed with purpose ending 'SUPERSECRETVAL'.
    """
    secret = "SUPERSECRETVALUE"
    redactor = Redactor(agent_names=("zeta",), workspace_paths=(), secret_values=(secret,))

    # Token spans the cap boundary: starts 8 chars before _SELF_PURPOSE_CAP.
    straddling = "x" * (_SELF_PURPOSE_CAP - 8) + secret + "y" * 400
    out = project_self({"created": "2026-01-01"}, straddling, redactor)

    assert secret not in out["purpose"]
    # The specific failure mode: the pre-cap PREFIX of the token surviving.
    for n in range(4, len(secret)):
        assert not out["purpose"].endswith(secret[:n]), (
            f"leaked a {n}-char prefix of a straddling secret: {out['purpose'][-24:]!r}"
        )
    assert len(out["purpose"]) <= _SELF_PURPOSE_CAP

    # Control: wholly inside the cap. Passes under either ordering by construction,
    # so it guards the ordinary path without standing in for the assertion above.
    inside = "x" * 1100 + " " + secret + " " + "y" * 400
    assert secret not in project_self({"created": "2026-01-01"}, inside, redactor)["purpose"]


# ── project_goals — the two fail-closed gates + the three-field allowlist ─────

def _g(**kw):
    """A goal record with the two gate fields defaulted OPEN, so each test varies one."""
    base = {"title": "Ship the reef explorer", "work_class": "product", "status": "pending"}
    base.update(kw)
    return base


def test_project_goals_work_class_gate_exposes_only_product() -> None:
    """Gate 1. Everything that is not literally ``product`` is dropped, missing included.

    The queue is overwhelmingly framework plumbing; publishing it would show a member
    the agent's maintenance backlog instead of the roadmap they came for.
    """
    goals = [
        _g(work_class="product", title="Product one"),
        _g(work_class="framework", title="Framework one"),
        _g(work_class="hygiene", title="Hygiene one"),
        _g(work_class="unclassified", title="Unclassified one"),
        _g(work_class=None, title="Null one"),
        {"title": "Absent one", "status": "pending"},  # key missing entirely
    ]
    out = project_goals(goals, Redactor())
    assert [g["title"] for g in out] == ["Product one"]


def test_project_goals_work_class_match_is_case_and_space_insensitive() -> None:
    """A store value is hand-written often enough that ``" Product "`` must not leak-by-drop.

    This gate fails CLOSED, so a casing miss silently HIDES real product work rather
    than exposing private work — the safe direction, and therefore the one nothing
    would ever alert on.
    """
    out = project_goals([_g(work_class="  Product  ")], Redactor())
    assert len(out) == 1


def test_project_goals_status_gate_maps_three_and_suppresses_the_rest() -> None:
    """Gate 2. An UNMAPPED status is suppressed, not passed through.

    That is the load-bearing half: a new internal status added to the store later stays
    private by default instead of appearing verbatim on a member-facing page.
    """
    mapped = [
        (_g(status="pending", title="A"), "planned"),
        (_g(status="in-progress", title="B"), "in progress"),
        (_g(status="completed", title="C"), "done"),
    ]
    for goal, public in mapped:
        out = project_goals([goal], Redactor())
        assert len(out) == 1 and out[0]["status"] == public, goal

    for internal in ("blocked", "skipped", "expired", "decomposed", "superseded", "", None):
        assert project_goals([_g(status=internal)], Redactor()) == [], internal


def test_project_goals_emits_exactly_three_keys_and_no_internal_field() -> None:
    """The ALLOWLIST is the whole security property — an internal field must not ride along.

    ``outcome_note`` and ``defer_reason`` carry verbatim measurements (account ids, table
    names, box hostnames, partner-agent names); ``participants``/``claimed_by`` carry
    fleet internals. None of them are read, so none can leak.
    """
    goal = _g(
        goal_id="g-369-70",
        asp_id="asp-369",
        outcome_note="dynamo table prod-accounts on cc-02 holds 110 vin_ keys",
        defer_reason="human_blocked: waiting on zachary",
        participants=["agent", "user"],
        claimed_by="zeta",
        priority="HIGH",
        category="ayoai-platform-services",
        created_at="2026-08-01",
    )
    out = project_goals([goal], Redactor())
    assert len(out) == 1
    assert set(out[0]) == {"title", "status", "updated"}, sorted(out[0])
    blob = " ".join(str(v) for v in out[0].values())
    for internal in ("dynamo", "prod-accounts", "cc-02", "zeta", "human_blocked", "HIGH", "asp-369"):
        assert internal not in blob, internal


def test_project_goals_redacts_and_caps_the_title() -> None:
    """The title is the one free-text field that survives, so it gets the full redactor.

    A product title routinely cites a script or an agent name; belt and braces.
    """
    from knowledge_projection import _GOAL_TITLE_CAP

    out = project_goals(
        [_g(title="alpha fixed knowledge-export.py for g-369-70 " + "x" * _GOAL_TITLE_CAP)],
        Redactor(agent_names=("alpha",)),
    )
    assert len(out) == 1
    title = out[0]["title"]
    assert "alpha" not in title and "knowledge-export.py" not in title and "g-369-70" not in title
    assert len(title) <= _GOAL_TITLE_CAP


def test_project_goals_drops_a_goal_whose_title_redacts_to_nothing() -> None:
    """A title that is ENTIRELY internal must not publish as an empty row.

    An empty-titled entry on a member-facing page is worse than absence: it advertises
    that something was hidden.
    """
    assert project_goals([_g(title="g-369-70")], Redactor()) == []
    assert project_goals([_g(title="   ")], Redactor()) == []


def test_project_goals_updated_prefers_completed_then_started_then_created() -> None:
    """The fallback chain, and the "" floor.

    "" must read as UNKNOWN, never as old — which is why it is empty rather than an
    epoch date a consumer would render as 1970.
    """
    assert project_goals(
        [_g(completed_date="2026-08-30", started="2026-08-01", created_at="2026-07-01")],
        Redactor(),
    )[0]["updated"] == "2026-08-30"
    assert project_goals(
        [_g(started="2026-08-01", created_at="2026-07-01")], Redactor()
    )[0]["updated"] == "2026-08-01"
    assert project_goals([_g(created_at="2026-07-01")], Redactor())[0]["updated"] == "2026-07-01"
    assert project_goals([_g()], Redactor())[0]["updated"] == ""


def test_project_goals_empty_input_is_empty_output() -> None:
    assert project_goals([], Redactor()) == []


def test_goals_is_in_counts_but_not_in_the_broken_export_refusal_set() -> None:
    """The twin of ``self``'s gap, and it is load-bearing for the OPPOSITE reason.

    ``goals`` is in ``counts()`` because guard-5144 says a projection absent from counts
    is a projection no verifier can check. It is NOT in ``KNOWLEDGE_COUNT_KEYS`` because
    goals are work items, not knowledge: a world can legitimately publish zero product
    goals while its four knowledge stores are perfectly healthy, so folding it in would
    let a genuinely broken export walk past the all-zero refusal gate.
    """
    from knowledge_projection import KNOWLEDGE_COUNT_KEYS

    assert "goals" in ProjectedBundle().counts()
    assert "goals" not in KNOWLEDGE_COUNT_KEYS

    bundle = ProjectedBundle(goals=[{"title": "t", "status": "planned", "updated": ""}])
    assert bundle.counts()["goals"] == 1
    assert any(bundle.counts().values()), "the naive check would pass this broken bundle"
    assert not any(bundle.counts()[k] for k in KNOWLEDGE_COUNT_KEYS), (
        "the refusal must still see this export as all-zero knowledge"
    )


# ── project_program () ─────────────────────────────────────────────
#
# The consumer-side twin of the self projection, with ONE deliberate divergence:
# the prose cut is an opt-in marker, not the structural "##" cut self.md uses.
# These tests pin the divergence, because the tempting change is to "make it
# consistent" with project_self — which is what would reintroduce the leak.

_PROGRAM_MD = """the framework is a platform for autonomous agents.

<!-- public:begin -->
Your agent is here to learn what makes a world feel alive, and to get better
at it every day.
<!-- public:end -->

We are NOT entering the Kaggle competition. Do NOT publish the Kaggle repo.
Resolve the root from AGENT_WRITE_PATH in agents/alpha/local-paths.conf.
"""


def _program(body, fm=None, redactor=None):
    return project_program(fm if fm is not None else {"created": "2026-05-13",
                                                      "last_updated": "2026-08-29"},
                           body,
                           redactor or Redactor(agent_names=(), workspace_paths=(), secret_values=()))


def test_program_publishes_only_the_marked_block():
    """The marked region ships; everything around it is suppressed."""
    got = _program(_PROGRAM_MD)
    assert set(got) == {"purpose", "created", "last_updated"}
    assert "makes a world feel alive" in str(got["purpose"])
    # The measured leak case: real strategy prose sits OUTSIDE the markers and must
    # not travel, however close to the block it is.
    for outside in ("Kaggle", "AGENT_WRITE_PATH", "local-paths.conf",
                    "the framework is a platform"):
        assert outside not in str(got["purpose"]), f"content outside the markers leaked: {outside}"


def test_program_without_markers_publishes_nothing():
    """THE load-bearing test. Fail-closed is the whole design.

    MEASURED 2026-09-02 on the only real program.md that exists: a self-style
    "cut at the first ##" slice of it ships a verbatim competitive directive
    naming a competition track twice, plus internal repo names and source paths,
    none of which the Redactor covers. So an unmarked file must publish NOTHING
    rather than a heuristic guess at what is safe.
    """
    unmarked = ("the framework is a platform.\n\nWe are NOT entering the Kaggle competition.\n"
                "\n## Architecture\n\nprimitives/ drives every EnvironmentAdapter.\n")
    assert _program(unmarked) == {}


@pytest.mark.parametrize("body,label", [
    ("<!-- public:begin -->\nhalf written\n", "opener with no closer"),
    ("trailing\n<!-- public:end -->\n", "closer with no opener"),
    ("<!-- public:end -->\nbackwards\n<!-- public:begin -->\n", "markers out of order"),
    ("<!-- public:begin -->\n   \n<!-- public:end -->\n", "marked block is blank"),
    ("", "empty file — the init-world.sh zero-byte placeholder"),
])
def test_program_requires_both_markers_in_order(body, label):
    """A half-written edit must cost the feature, never leak the remainder of the file."""
    assert _program(body) == {}, label


def test_program_refuses_a_dates_only_husk():
    """Front matter with no marked prose is not a program — publish nothing."""
    assert _program("no markers here", fm={"created": "2026-05-13"}) == {}


def test_program_redacts_before_capping_so_a_straddling_token_cannot_leak():
    """Same ordering property project_self pins, for the same reason.

    Redacting AFTER a cap would let a forbidden token survive by being truncated
    into a shape the redactor no longer matches.
    """
    secret = "sk-live-programsecret"
    redactor = Redactor(agent_names=(), workspace_paths=(), secret_values=(secret,))
    straddling = "x" * (_PROGRAM_PURPOSE_CAP - 8) + secret + "y" * 400
    out = _program(f"<!-- public:begin -->\n{straddling}\n<!-- public:end -->", redactor=redactor)
    assert secret not in str(out["purpose"])
    assert len(str(out["purpose"])) <= _PROGRAM_PURPOSE_CAP


def test_project_wires_program_through_and_defaults_it_empty():
    """An existing caller that passes no program keeps its exact shape, not a changed one."""
    redactor = Redactor(agent_names=(), workspace_paths=(), secret_values=())
    assert project(tree_nodes=[], reasoning=[], guardrails=[], hypotheses=[],
                   redactor=redactor).program == {}
    wired = project(tree_nodes=[], reasoning=[], guardrails=[], hypotheses=[],
                    redactor=redactor,
                    program_front_matter={"created": "2026-05-13"},
                    program_body=_PROGRAM_MD)
    assert "makes a world feel alive" in str(wired.program["purpose"])
    assert wired.counts()["program"] == 1


# ── goal handles () — the opaque per-goal address ───────────────────
#
# The Planned board strips ids BY DESIGN, so the four write verbs (/31) had no
# way to say WHICH goal a member meant. These pin the handle that closes that gap and
# the three properties the write path leans on: the id is not recoverable from the
# published board, the publishable set and the addressable set are the SAME set, and
# every case that is not exactly-one-match resolves to nothing rather than to a guess.

_HANDLE_SECRET = "unit-test-handle-secret-not-a-real-key"
_OTHER_SECRET = "unit-test-handle-secret-a-different-one"
#: Every handle below is computed under a REAL environment id, because that is the only
#: shape production uses: both live call sites (project_goals, resolve_goal_handle) pass
#: one explicitly. The default "" is a call-site convenience that now fails closed
#: (guard-6312), so a test omitting it would assert against "" and pass vacuously.
_ENV = "unit-test-env"


def test_project_goals_emits_no_handle_without_a_secret() -> None:
    """FAIL CLOSED — and this pin is what keeps the default row shape unchanged.

    An unprovisioned box publishes a board with NO handles: never a guessable token and
    never the goal id. Every addressed write it then receives resolves to nothing, which
    is the safe degradation. The three-field pin above stays literally true by default.
    """
    out = project_goals([_g(id="g-369-119")], Redactor())
    assert len(out) == 1
    assert set(out[0]) == {"title", "status", "updated"}, sorted(out[0])


def test_project_goals_emits_a_fourth_handle_field_under_a_secret() -> None:
    """The one shape change, and it is opt-in: a fourth key appears ONLY with a secret."""
    out = project_goals(
        [_g(id="g-369-119")], Redactor(), handle_secret=_HANDLE_SECRET, environment_id=_ENV
    )
    assert len(out) == 1
    assert set(out[0]) == {"title", "status", "updated", "handle"}, sorted(out[0])
    handle = str(out[0]["handle"])
    assert len(handle) == kp._GOAL_HANDLE_HEX
    assert all(c in "0123456789abcdef" for c in handle), handle
    # The three original fields are untouched — an added sibling field, never a
    # redefinition of the locked ones (rb-2148).
    assert out[0]["title"] == "Ship the reef explorer"
    assert out[0]["status"] == "planned"


def test_published_handle_does_not_leak_the_goal_id_or_the_secret() -> None:
    """OUTCOME 1: the id is not recoverable from what the board publishes.

    Two assertions, and the SECOND is the load-bearing one. That the id does not appear
    verbatim is necessary but weak — the goal-id space is small enough to enumerate, so
    a handle computed under a PUBLICLY derivable key could be inverted by brute force.
    What actually makes the id unrecoverable is that the handle is a function of a
    secret: change only the secret and the handle changes completely, so an attacker
    without it cannot compute the mapping to invert.
    """
    gid = "g-369-119"
    row = project_goals(
        [_g(id=gid)], Redactor(), handle_secret=_HANDLE_SECRET, environment_id=_ENV
    )[0]
    blob = " ".join(str(v) for v in row.values())
    for fragment in (gid, "369-119", "g-369", _HANDLE_SECRET):
        assert fragment not in blob, fragment
    assert goal_handle(gid, _HANDLE_SECRET, _ENV) != goal_handle(gid, _OTHER_SECRET, _ENV)


def test_goal_handle_is_stable_per_goal_and_distinct_between_goals() -> None:
    """Stable, because resolve RECOMPUTES rather than reading a stored mapping."""
    assert goal_handle("g-369-119", _HANDLE_SECRET, _ENV) == goal_handle(
        "g-369-119", _HANDLE_SECRET, _ENV
    )
    assert goal_handle("g-369-119", _HANDLE_SECRET, _ENV) != goal_handle(
        "g-369-30", _HANDLE_SECRET, _ENV
    )


def test_goal_handle_is_per_environment_even_under_one_shared_secret() -> None:
    """No cross-environment correlation, and it must not depend on key hygiene.

    environment_id is mixed into the MESSAGE, not merely assumed to differ through the
    key — so one secret provisioned fleet-wide across two environments still yields two
    unrelated handles for the same goal. The NUL separator is what stops
    ("env", "g-1-2") and ("envg", "-1-2") from colliding.
    """
    a = goal_handle("g-369-119", _HANDLE_SECRET, "env-a")
    b = goal_handle("g-369-119", _HANDLE_SECRET, "env-b")
    assert a != b
    assert goal_handle("g-1-2", _HANDLE_SECRET, "env") != goal_handle("-1-2", _HANDLE_SECRET, "envg")


def test_goal_handle_is_empty_without_an_id_or_a_secret() -> None:
    """Both inputs are required; an empty return is what suppresses the field."""
    assert goal_handle("", _HANDLE_SECRET, _ENV) == ""
    assert goal_handle("g-369-119", "", _ENV) == ""
    assert goal_handle("   ", _HANDLE_SECRET, _ENV) == ""


def test_goal_handle_is_empty_without_an_environment_id() -> None:
    """The THIRD arm of the fence, and the one guard-6312 exists for.

    ``environment_id`` is mixed into the HMAC message, so an empty value does not merely
    weaken the handle — it retires the property the component carries. Two environments
    sharing one fleet-wide secret would publish IDENTICAL handles for the same goal, and
    nothing logs it: the docstring's no-cross-environment-correlation promise fails open.

    The positive control is load-bearing, not ceremony. Every assertion here is
    ``== ""``, so without it the test would still pass if ``goal_handle`` returned ""
    unconditionally — the observational-equivalence hole of guard-2219.

    Both the OMITTED and the explicitly-empty shape are pinned. In Python they reach the
    same branch (the default IS ``""``), so this is not two behaviours — it is one
    behaviour asserted through the two call shapes a caller can actually write, which is
    what stops a later signature change from silently reopening the omitted path.

    No child process is needed here, and that is a deliberate departure from the goal's
    stated verification method: guard-2337 requires child-process env planting only for an
    env-derived IMPORT-TIME invariant, and exempts an injectable parameter. Measured —
    ``knowledge_projection.py`` contains zero ``os.environ``/``getenv`` reads, and
    ``environment_id`` arrives purely as an argument.
    """
    # Positive control FIRST: this exact call is non-empty with a real environment.
    assert goal_handle("g-369-119", _HANDLE_SECRET, _ENV) != ""

    assert goal_handle("g-369-119", _HANDLE_SECRET) == ""          # omitted
    assert goal_handle("g-369-119", _HANDLE_SECRET, "") == ""      # explicitly empty
    assert goal_handle("g-369-119", _HANDLE_SECRET, "   ") == ""   # whitespace only


def test_project_goals_emits_no_handle_without_an_environment_id() -> None:
    """The fence reaches the BOARD: a secret alone is not enough to publish a handle.

    This is the assertion that actually protects members. A box provisioned with a secret
    but no environment id previously published a full board of well-formed handles keyed
    on "" — correlatable across environments and matching no resolver holding a real id.
    """
    plain = project_goals([_g(id="g-369-119")], Redactor(), handle_secret=_HANDLE_SECRET)
    assert len(plain) == 1
    assert set(plain[0]) == {"title", "status", "updated"}, sorted(plain[0])

    # Positive control: the same call WITH an environment does publish the fourth key.
    keyed = project_goals(
        [_g(id="g-369-119")], Redactor(), handle_secret=_HANDLE_SECRET, environment_id=_ENV
    )
    assert "handle" in keyed[0], sorted(keyed[0])


def test_project_goals_publishes_an_id_less_goal_without_a_handle() -> None:
    """A malformed record loses its ADDRESSABILITY, not its visibility.

    Dropping the row instead would let one bad store line silently shrink the board.
    """
    out = project_goals(
        [_g()], Redactor(), handle_secret=_HANDLE_SECRET, environment_id=_ENV
    )
    assert len(out) == 1 and "handle" not in out[0], sorted(out[0])


def test_resolve_round_trips_the_handle_the_board_actually_published() -> None:
    """OUTCOME 2, end to end: resolve the PUBLISHED value, not a re-derived one.

    Taking the handle off the projected row is the whole point — a test that recomputed
    it would pass even if the projection published a different value.
    """
    goals = [_g(id="g-369-119", title="A"), _g(id="g-369-30", title="B")]
    rows = project_goals(
        goals, Redactor(), handle_secret=_HANDLE_SECRET, environment_id=_ENV
    )
    for row, goal in zip(rows, goals):
        assert resolve_goal_handle(
            str(row["handle"]), goals, _HANDLE_SECRET, Redactor(), _ENV
        ) == goal["id"]


def test_resolve_returns_none_for_unknown_wrong_secret_and_wrong_environment() -> None:
    """Every miss is None. An unknown handle must never fall back to a nearest match."""
    goals = [_g(id="g-369-119")]
    handle = str(
        project_goals(
            goals, Redactor(), handle_secret=_HANDLE_SECRET, environment_id=_ENV
        )[0]["handle"]
    )

    assert resolve_goal_handle("deadbeefdeadbeef", goals, _HANDLE_SECRET, Redactor(), _ENV) is None
    assert resolve_goal_handle(handle, goals, _OTHER_SECRET, Redactor(), _ENV) is None
    assert resolve_goal_handle(handle, goals, _HANDLE_SECRET, Redactor(), "env-b") is None
    assert resolve_goal_handle(handle, goals, "", Redactor(), _ENV) is None
    assert resolve_goal_handle("", goals, _HANDLE_SECRET, Redactor(), _ENV) is None
    assert resolve_goal_handle(handle, [], _HANDLE_SECRET, Redactor(), _ENV) is None
    # The environment_id arm of the same fence: an unprovisioned resolver matches nothing
    # rather than matching a handle keyed on "" (guard-6312).
    assert resolve_goal_handle(handle, goals, _HANDLE_SECRET, Redactor(), "") is None


def test_resolve_refuses_a_goal_the_board_never_published() -> None:
    """The addressable set IS the publishable set — one predicate, so it cannot drift.

    Each goal here is suppressed by a DIFFERENT gate, and a handle computed for it
    resolves to nothing. Without the shared predicate a write could reach the agent's own
    framework backlog, or a `blocked` goal no member ever saw.
    """
    for hidden in (
        _g(id="g-369-201", work_class="framework"),   # gate 1 — not product work
        _g(id="g-369-202", status="blocked"),         # gate 2 — status not published
        _g(id="g-369-203", title="   "),              # title survives nothing
    ):
        assert project_goals(
            [hidden], Redactor(), handle_secret=_HANDLE_SECRET, environment_id=_ENV
        ) == []
        handle = goal_handle(str(hidden["id"]), _HANDLE_SECRET, _ENV)
        # Non-empty by construction: without _ENV this handle would be "" and the assertion
        # below would hold trivially, testing nothing about the exposure predicate.
        assert handle, hidden["id"]
        assert resolve_goal_handle(handle, [hidden], _HANDLE_SECRET, Redactor(), _ENV) is None


def test_resolve_returns_none_when_two_exposed_goals_share_a_handle(monkeypatch) -> None:
    """A collision resolves to NOTHING, never to an arbitrary pick.

    Forced by shrinking the handle to one hex character (16 buckets) rather than by
    stubbing goal_handle, so the real ambiguity branch runs against real digests. At the
    production width a collision sits near 1e-15; the branch exists because the caller is
    a write path against live member data, where a coin-flip mutates the wrong member's
    goal.
    """
    monkeypatch.setattr(kp, "_GOAL_HANDLE_HEX", 1)
    ids = [f"g-999-{n}" for n in range(200)]
    seen: dict[str, str] = {}
    pair = None
    for gid in ids:
        h = goal_handle(gid, _HANDLE_SECRET, _ENV)
        if h in seen:
            pair = (seen[h], gid, h)
            break
        seen[h] = gid
    assert pair is not None, "no collision at 1 hex char over 200 ids — widen the search"
    first, second, handle = pair
    goals = [_g(id=first), _g(id=second)]
    assert resolve_goal_handle(handle, goals, _HANDLE_SECRET, Redactor(), _ENV) is None


def test_resolve_treats_a_repeated_record_for_one_id_as_unambiguous() -> None:
    """Two records, ONE id, is duplication — not ambiguity. It must still resolve."""
    goals = [_g(id="g-369-119", title="A"), _g(id="g-369-119", title="A again")]
    handle = goal_handle("g-369-119", _HANDLE_SECRET, _ENV)
    assert resolve_goal_handle(handle, goals, _HANDLE_SECRET, Redactor(), _ENV) == "g-369-119"


def test_resolve_accepts_the_handle_with_surrounding_whitespace_and_uppercase() -> None:
    """An inbound handle crosses a URL/JSON boundary before it gets here."""
    handle = goal_handle("g-369-119", _HANDLE_SECRET, _ENV)
    goals = [_g(id="g-369-119")]
    assert resolve_goal_handle(
        f"  {handle.upper()}  ", goals, _HANDLE_SECRET, Redactor(), _ENV
    ) == "g-369-119"


def test_project_wires_the_handle_secret_through_and_defaults_it_off() -> None:
    """The project() seam: an existing caller passing no secret keeps its exact shape."""
    redactor = Redactor(agent_names=(), workspace_paths=(), secret_values=())
    plain = project(tree_nodes=[], reasoning=[], guardrails=[], hypotheses=[],
                    redactor=redactor, goals=[_g(id="g-369-119")])
    assert set(plain.goals[0]) == {"title", "status", "updated"}, sorted(plain.goals[0])
    wired = project(tree_nodes=[], reasoning=[], guardrails=[], hypotheses=[],
                    redactor=redactor, goals=[_g(id="g-369-119")],
                    goal_handle_secret=_HANDLE_SECRET, environment_id="env-a")
    assert wired.goals[0]["handle"] == goal_handle("g-369-119", _HANDLE_SECRET, "env-a")


# ── per-item knowledge handles () ─────────────────────────────────
# The address a member-facing edit or forget carries for ONE wiki node, hypothesis or
# guardrail. Same rules as the goal handle above: fail closed, visible == addressable.


def _item_stores() -> dict[str, list[dict[str, object]]]:
    """One exposed and one hidden record per store, each carrying the id the store has."""
    return {
        "tree_nodes": [
            {"key": "reefs", "category": "marine-biology/reefs", "title": "Coral reefs"},
            {"key": "hooks", "category": "system/hooks", "title": "Hook internals"},
        ],
        "hypotheses": [
            {"id": "2026-09-30_reef-heat", "category": "marine-biology/reefs",
             "claim": "Warmer water bleaches reefs faster.", "stage": "active"},
            {"id": "2026-09-30_loop-veto", "category": "system/loop",
             "claim": "The veto budget bounds continuations.", "stage": "active"},
        ],
        "guardrails": [
            {"id": "guard-9001", "status": "active", "category": "marine-biology/method",
             "rule": "Verify every claim against two sources."},
            {"id": "guard-9002", "status": "active", "category": "framework-architecture",
             "rule": "Never critical() in a handler."},
        ],
    }


def _project_items(secret: str = "", env: str = "") -> ProjectedBundle:
    s = _item_stores()
    return project(tree_nodes=s["tree_nodes"], reasoning=[], guardrails=s["guardrails"],
                   hypotheses=s["hypotheses"], redactor=Redactor(),
                   item_handle_secret=secret, environment_id=env)


def _resolve(handle: str, secret: str = _HANDLE_SECRET, env: str = _ENV):
    s = _item_stores()
    return kp.resolve_item_handle(handle, tree_nodes=s["tree_nodes"],
                                  hypotheses=s["hypotheses"], guardrails=s["guardrails"],
                                  secret=secret, environment_id=env)


def test_item_handle_fails_closed_on_every_message_component() -> None:
    """guard-6312: an empty or forged component yields "", never a guessable token."""
    assert kp.item_handle("node", "reefs", _HANDLE_SECRET, _ENV)
    assert kp.item_handle("lesson", "reefs", _HANDLE_SECRET, _ENV) == ""
    assert kp.item_handle("", "reefs", _HANDLE_SECRET, _ENV) == ""
    assert kp.item_handle("node", "  ", _HANDLE_SECRET, _ENV) == ""
    assert kp.item_handle("node", "reefs", "", _ENV) == ""
    assert kp.item_handle("node", "reefs", _HANDLE_SECRET, "") == ""
    assert kp.item_handle("node", "re\x00efs", _HANDLE_SECRET, _ENV) == ""
    assert kp.item_handle("node", "reefs", _HANDLE_SECRET, "env\x00a") == ""


def test_item_handle_separates_kinds_environments_and_goal_handles() -> None:
    """One id under three kinds, two environments and the goal scheme: all distinct."""
    handles = {kp.item_handle(k, "x-1", _HANDLE_SECRET, _ENV) for k in kp.KNOWLEDGE_ITEM_KINDS}
    handles.add(kp.item_handle("node", "x-1", _HANDLE_SECRET, "env-b"))
    handles.add(goal_handle("x-1", _HANDLE_SECRET, _ENV))
    assert len(handles) == 5 and "" not in handles
    assert all(len(h) == 16 for h in handles)


def test_project_adds_item_handles_only_under_a_secret_and_an_environment() -> None:
    """The seam defaults OFF: no secret, or no environment, keeps every row's shape."""
    for bundle in (_project_items(), _project_items(_HANDLE_SECRET, ""),
                   _project_items("", _ENV)):
        rows = bundle.tree + bundle.hypotheses + bundle.guardrails
        assert len(rows) == 3 and all("handle" not in r for r in rows), rows
    wired = _project_items(_HANDLE_SECRET, _ENV)
    assert wired.tree[0]["handle"] == kp.item_handle("node", "reefs", _HANDLE_SECRET, _ENV)
    assert wired.hypotheses[0]["handle"] == kp.item_handle(
        "hypothesis", "2026-09-30_reef-heat", _HANDLE_SECRET, _ENV)
    assert wired.guardrails[0]["handle"] == kp.item_handle(
        "guardrail", "guard-9001", _HANDLE_SECRET, _ENV)
    # The handle replaces nothing: the hidden ids stay hidden.
    assert "guard-9001" not in str(wired.guardrails)
    assert "reef-heat" not in str(wired.hypotheses)


def test_resolve_item_round_trips_the_handles_the_projection_published() -> None:
    """Resolve the PUBLISHED values, so a projection/resolver drift cannot pass."""
    wired = _project_items(_HANDLE_SECRET, _ENV)
    assert _resolve(str(wired.tree[0]["handle"])) == ("node", "reefs")
    assert _resolve(str(wired.hypotheses[0]["handle"])) == ("hypothesis", "2026-09-30_reef-heat")
    assert _resolve(str(wired.guardrails[0]["handle"])) == ("guardrail", "guard-9001")
    # An inbound handle crosses a URL/JSON boundary before it gets here.
    assert _resolve(f"  {str(wired.tree[0]['handle']).upper()} ") == ("node", "reefs")


def test_resolve_item_refuses_every_item_the_projection_hides() -> None:
    """Visible == addressable: a well-formed handle for a hidden item resolves to nothing."""
    for kind, iid in (("node", "hooks"), ("hypothesis", "2026-09-30_loop-veto"),
                      ("guardrail", "guard-9002")):
        handle = kp.item_handle(kind, iid, _HANDLE_SECRET, _ENV)
        # Non-empty by construction, so the None below tests the exposure predicate and
        # not an empty handle.
        assert handle, (kind, iid)
        assert _resolve(handle) is None, (kind, iid)


def test_a_guardrail_the_resident_no_longer_follows_is_neither_published_nor_addressable() -> None:
    """Shown == used: only ``status == "active"``, the resident's own test, is exposed. A
    member's correction supersedes a rule by retiring it, so the retired rule must leave
    the view and stop resolving, or the member would see the old text beside the new."""
    s = _item_stores()
    cat = "marine-biology/method"  # in the allowlist: the category filter alone exposes all
    s["guardrails"] = [
        {"id": "guard-9101", "status": "active", "category": cat, "rule": "Active rule."},
        {"id": "guard-9102", "status": "retired", "category": cat, "rule": "Retired rule."},
        {"id": "guard-9103", "category": cat, "rule": "No status at all."},
        {"id": "guard-9104", "status": "Active", "category": cat, "rule": "Wrong case."},
    ]
    allow = kp.domain_categories(s["tree_nodes"])
    assert all(kp.is_exposed_by_category(g, allow) for g in s["guardrails"]), "positive control"
    bundle = project(tree_nodes=s["tree_nodes"], reasoning=[], guardrails=s["guardrails"],
                     hypotheses=[], redactor=Redactor(), item_handle_secret=_HANDLE_SECRET,
                     environment_id=_ENV)
    assert [g["rule"] for g in bundle.guardrails] == ["Active rule."]
    for g in s["guardrails"]:
        handle = kp.item_handle("guardrail", g["id"], _HANDLE_SECRET, _ENV)
        got = kp.resolve_item_handle(handle, tree_nodes=s["tree_nodes"], hypotheses=[],
                                     guardrails=s["guardrails"], secret=_HANDLE_SECRET,
                                     environment_id=_ENV)
        assert got == (("guardrail", g["id"]) if g["id"] == "guard-9101" else None), g["id"]


def test_a_hypothesis_the_member_forgot_is_neither_published_nor_addressable() -> None:
    """Shown == forgotten: ``forgotten_at`` is the one field the cut reads, and a record stays in
    its store because its lifecycle only moves forward. An undo clears the marker to null (the
    store's field writer has no delete), so a null, an empty string and an absent marker all
    show the record, and any stamp hides it, at any stage."""
    s = _item_stores()
    cat = "marine-biology/reefs"
    stamp = "2026-10-02T00:00:00+00:00"
    s["hypotheses"] = [
        {"id": "2026-09-30_shown-absent", "category": cat, "claim": "No marker at all.", "stage": "active"},
        {"id": "2026-09-30_shown-null", "category": cat, "claim": "Marker cleared by an undo.",
         "stage": "active", kp.FORGOTTEN_FIELD: None},
        {"id": "2026-09-30_shown-empty", "category": cat, "claim": "Empty marker.",
         "stage": "active", kp.FORGOTTEN_FIELD: ""},
        {"id": "2026-09-30_gone-active", "category": cat, "claim": "Hidden.",
         "stage": "active", kp.FORGOTTEN_FIELD: stamp},
        {"id": "2026-09-30_gone-resolved", "category": cat, "claim": "Resolved and hidden.",
         "stage": "resolved", kp.FORGOTTEN_FIELD: stamp},
    ]
    allow = kp.domain_categories(s["tree_nodes"])
    assert all(kp.is_exposed_by_category(h, allow) for h in s["hypotheses"]), \
        "positive control: the category filter alone exposes all five"
    bundle = project(tree_nodes=s["tree_nodes"], reasoning=[], guardrails=[],
                     hypotheses=s["hypotheses"], redactor=Redactor(),
                     item_handle_secret=_HANDLE_SECRET, environment_id=_ENV)
    assert [h["statement"] for h in bundle.hypotheses] == [
        "No marker at all.", "Marker cleared by an undo.", "Empty marker."]
    for h in s["hypotheses"]:
        handle = kp.item_handle("hypothesis", h["id"], _HANDLE_SECRET, _ENV)
        got = kp.resolve_item_handle(handle, tree_nodes=s["tree_nodes"], hypotheses=s["hypotheses"],
                                     guardrails=[], secret=_HANDLE_SECRET, environment_id=_ENV)
        assert got == (None if "gone" in h["id"] else ("hypothesis", h["id"])), h["id"]


def test_is_forgotten_reads_the_marker_by_truthiness() -> None:
    assert kp.is_forgotten({kp.FORGOTTEN_FIELD: "2026-10-02T00:00:00+00:00"}) is True
    for unmarked in ({}, {kp.FORGOTTEN_FIELD: None}, {kp.FORGOTTEN_FIELD: ""}):
        assert kp.is_forgotten(unmarked) is False, unmarked


def test_item_text_fields_is_the_table_item_text_reads() -> None:
    """One table: what a forget blanks is what the member was shown and an edit replaces."""
    record = {"claim": "The claim.", "title": "The title.", "body": "The body.", "rule": "The rule."}
    for kind in ("node", "hypothesis", "guardrail"):
        fields = kp.item_text_fields(kind)
        assert fields and kp.item_text(kind, record) == record[fields[0]], kind
    assert kp.item_text_fields("hypothesis") == ("claim", "title")
    assert kp.item_text("hypothesis", {"title": "Only a title."}) == "Only a title.", "no claim shows the title"
    assert kp.item_text_fields("not-a-kind") == ()


def test_resolve_item_returns_none_for_every_miss() -> None:
    handle = kp.item_handle("node", "reefs", _HANDLE_SECRET, _ENV)
    assert _resolve(handle) == ("node", "reefs")  # positive control for the misses below
    assert _resolve("deadbeefdeadbeef") is None
    assert _resolve("") is None
    assert _resolve(handle, secret=_OTHER_SECRET) is None
    assert _resolve(handle, secret="") is None
    assert _resolve(handle, env="env-b") is None
    assert _resolve(handle, env="") is None
    # A goal handle over the same id is a different namespace, never an item address.
    assert _resolve(goal_handle("reefs", _HANDLE_SECRET, _ENV)) is None


def test_resolve_item_returns_none_when_two_exposed_items_share_a_handle(monkeypatch) -> None:
    """A collision resolves to NOTHING. Forced with real digests at one hex character."""
    monkeypatch.setattr(kp, "_GOAL_HANDLE_HEX", 1)
    seen: dict[str, str] = {}
    pair = None
    for n in range(200):
        key = f"reef-{n}"
        h = kp.item_handle("node", key, _HANDLE_SECRET, _ENV)
        if h in seen:
            pair = (seen[h], key, h)
            break
        seen[h] = key
    assert pair is not None, "no collision at 1 hex char over 200 keys — widen the search"
    first, second, handle = pair
    nodes = [{"key": first, "category": "marine-biology/reefs"},
             {"key": second, "category": "marine-biology/reefs"}]
    assert kp.resolve_item_handle(handle, tree_nodes=nodes, hypotheses=[], guardrails=[],
                                  secret=_HANDLE_SECRET, environment_id=_ENV) is None
    # The same node listed twice is duplication, not ambiguity: it still resolves.
    assert kp.resolve_item_handle(handle, tree_nodes=nodes[:1] * 2, hypotheses=[],
                                  guardrails=[], secret=_HANDLE_SECRET,
                                  environment_id=_ENV) == ("node", first)


def test_project_publishes_an_id_less_record_without_a_handle() -> None:
    """A store line with no id still shows; it just cannot be addressed."""
    bundle = project(tree_nodes=[{"key": "reefs", "category": "marine-biology/reefs"}],
                     reasoning=[], hypotheses=[{"category": "marine-biology/reefs",
                                                "claim": "No id on this line."}],
                     guardrails=[], redactor=Redactor(),
                     item_handle_secret=_HANDLE_SECRET, environment_id=_ENV)
    assert len(bundle.hypotheses) == 1 and "handle" not in bundle.hypotheses[0]
    assert "handle" in bundle.tree[0]


# ── unredacted: a correction may replace only text the member saw whole () ──

#: One text per way the view alters what is stored. Each is a positive control: the test
#: first asserts the view really differs, so a False below tests the predicate and not a
#: text the redactor never touches.
_ALTERED_BY_THE_VIEW = {
    "absolute path": "Survey notes live in /srv/survey/reefs.md for now.",
    "relative path": "Count the fish in survey/reefs/north.",
    "agent name": "Nova counted the fish twice.",
    "framework id": "The count follows guard-12 closely.",
    "secret value": "Log in with s3cr3t-value-for-tests, then count.",
    "secret shape": "API_KEY=hunter2plaintext done",
    "high-entropy token": "opaque aZ9x2Qm7Lp4Wd8Rt6Yv3Nb1Kc5Hj0Fg tail",
    "collapsed spaces": "Reef A  and reef B.",
    "indented first line": "    count = 3\nThe reef has three fish.",
}
_VIEW_REDACTOR = Redactor(agent_names=("nova",), secret_values=("s3cr3t-value-for-tests",))


def test_is_unredacted_is_false_for_every_text_the_view_alters() -> None:
    for why, text in _ALTERED_BY_THE_VIEW.items():
        assert _VIEW_REDACTOR(text) != text, f"not a positive control: {why}"
        assert not kp.is_unredacted(text, _VIEW_REDACTOR), why


def test_is_unredacted_is_true_when_only_the_ends_are_trimmed() -> None:
    for text in ("Coral reefs bleach in warm water.", "", "Line one.\n\nLine two.\n",
                 "\n\nA body read after its front matter.\n"):
        assert kp.is_unredacted(text, _VIEW_REDACTOR), repr(text)


def test_is_unredacted_is_true_for_prose_slashes() -> None:
    """A slash inside prose is not an alteration, so a row carrying one can be corrected ()."""
    for text in _PROSE_WITH_SLASHES:
        assert kp.is_unredacted(text, _VIEW_REDACTOR), text


def test_project_publishes_prose_slashes_whole_and_flags_the_row() -> None:
    body = "Count and/or weigh the fish. Due 9/30/2026 on dev/main."
    nodes = [{"key": "reefs", "category": "marine-biology/reefs", "body": body},
             {"key": "vents", "category": "marine-biology/vents", "body": body + " Logs: /srv/vents/log.md"}]
    bundle = project(tree_nodes=nodes, reasoning=[], hypotheses=[], guardrails=[],
                     redactor=_VIEW_REDACTOR, item_handle_secret=_HANDLE_SECRET, environment_id=_ENV)
    assert bundle.tree[0]["body"] == body and bundle.tree[0].get("unredacted") is True
    # The same prose beside a real path: the prose survives, the path goes, and the row is not
    # offered for correction because the member would be correcting text that hides a path.
    assert bundle.tree[1]["body"] == body + " Logs: [path]"
    assert "unredacted" not in bundle.tree[1]


def test_project_marks_unredacted_only_on_addressable_rows_whose_text_shows_whole() -> None:
    """The flag answers the member's "is what I see all there is?", for every kind a member
    can correct: a node, a hypothesis and (by supersede) a guardrail."""
    nodes = [
        {"key": "reefs", "category": "marine-biology/reefs", "body": "Reefs bleach when warm.\n"},
        {"key": "vents", "category": "marine-biology/vents",
         "body": "Vent logs live in /srv/vents/log.md."},
    ]
    hyps = [
        {"id": "h-clean", "category": "marine-biology/reefs", "claim": "Warm water bleaches reefs."},
        {"id": "h-named", "category": "marine-biology/reefs", "claim": "Nova saw it first."},
        {"id": "h-titled", "category": "marine-biology/reefs", "title": "Reefs recover slowly."},
    ]
    guards = [{"id": "guard-9001", "status": "active", "category": "marine-biology/method",
               "rule": "Verify every claim against two sources."},
              {"id": "guard-9003", "status": "active", "category": "marine-biology/method",
               "rule": "Ask Nova before sampling."}]

    def run(secret: str, env: str) -> ProjectedBundle:
        return project(tree_nodes=nodes, reasoning=[], hypotheses=hyps, guardrails=guards,
                       redactor=_VIEW_REDACTOR, item_handle_secret=secret, environment_id=env)

    wired = run(_HANDLE_SECRET, _ENV)
    assert [r["key"] for r in wired.tree if r.get("unredacted")] == ["reefs"]
    # A hypothesis with no claim shows its title, so the flag reads the title.
    assert [bool(r.get("unredacted")) for r in wired.hypotheses] == [True, False, True]
    # A guardrail's correctable text is its rule; the agent name is redacted in the second.
    assert [bool(r.get("unredacted")) for r in wired.guardrails] == [True, False]
    assert all("handle" in r for r in wired.guardrails)
    # "No" is an absent key, never False, so a present key always means yes.
    assert all(r.get("unredacted", True) is True
               for r in wired.tree + wired.hypotheses + wired.guardrails)
    # What the flag promises, read off the published values: shown == stored.
    assert wired.tree[0]["body"] == nodes[0]["body"].strip()
    assert wired.hypotheses[2]["statement"] == hyps[2]["title"]
    # No secret or no environment: no handle, so no flag, and every row keeps its shape.
    for bundle in (run("", ""), run(_HANDLE_SECRET, ""), run("", _ENV)):
        rows = bundle.tree + bundle.hypotheses + bundle.guardrails
        assert rows and all("unredacted" not in r for r in rows), rows


def test_project_never_marks_a_body_its_reader_cut() -> None:
    """A cut body is not the stored body, however whole its text looks ( finding 7)."""
    def row(**mark: object) -> dict[str, object]:
        node = {"key": "reefs", "category": "marine-biology/reefs",
                "body": "Reefs bleach when warm.", **mark}
        return project(tree_nodes=[node], reasoning=[], hypotheses=[], guardrails=[],
                       redactor=_VIEW_REDACTOR, item_handle_secret=_HANDLE_SECRET,
                       environment_id=_ENV).tree[0]

    # Positive controls: the same row with no mark, or marked whole, is flagged.
    assert row().get("unredacted") is True
    assert row(body_truncated=False).get("unredacted") is True
    cut = row(body_truncated=True)
    assert "handle" in cut and "unredacted" not in cut
    # The mark is read, never published: nothing else about the row changes.
    assert cut == {k: v for k, v in row().items() if k != "unredacted"}


# ── item_text / view_digest: the base a correction carries () ──────────────

def test_item_text_is_the_text_each_published_row_shows() -> None:
    """project() publishes redactor(item_text(...)) for every kind, so the member-edit
    applier hashes the very field the member was shown."""
    nodes = [{"key": "reefs", "category": "marine-biology/reefs", "body": "Reefs bleach.\n"}]
    hyps = [
        {"id": "h-claim", "category": "marine-biology/reefs",
         "claim": "Warm water bleaches reefs.", "title": "A title the row does not show."},
        {"id": "h-titled", "category": "marine-biology/reefs", "title": "Reefs recover slowly."},
        {"id": "h-empty", "category": "marine-biology/reefs"},
    ]
    guards = [{"id": "guard-9001", "status": "active", "category": "marine-biology/method",
               "rule": "Nova verifies every claim."}]
    bundle = project(tree_nodes=nodes, reasoning=[], hypotheses=hyps, guardrails=guards,
                     redactor=_VIEW_REDACTOR)
    assert bundle.guardrails[0]["rule"] != guards[0]["rule"], "not a positive control"
    assert [r["body"] for r in bundle.tree] == [
        _VIEW_REDACTOR(kp.item_text("node", n)) for n in nodes]
    assert [r["statement"] for r in bundle.hypotheses] == [
        _VIEW_REDACTOR(kp.item_text("hypothesis", h)) for h in hyps]
    assert [r["rule"] for r in bundle.guardrails] == [
        _VIEW_REDACTOR(kp.item_text("guardrail", g)) for g in guards]
    assert [kp.item_text("hypothesis", h) for h in hyps] == [
        "Warm water bleaches reefs.", "Reefs recover slowly.", ""]
    assert kp.item_text("lesson", {"body": "not an item kind"}) == ""


#: Digest prefixes a browser produced for these strings (a JS TextEncoder, then SHA-256),
#: measured 2026-10-01. A JS string holds UTF-16 code units, so each key is the same
#: code-unit sequence on both sides: a split pair, lone surrogates, and plain text.
_BROWSER_DIGEST_PREFIXES = {
    "abc": "ba7816bf8f01cfea",
    "": "e3b0c44298fc1c14",
    "\U0001F600": "f0443a342c5ef547",
    chr(0xD83D) + chr(0xDE00): "f0443a342c5ef547",
    chr(0xD800): "83d544ccc223c057",
    chr(0xDC00): "83d544ccc223c057",
    chr(0xD800) + "a": "94b964456d33b6a0",
    "a" + chr(0xD83D): "51d277510ba4bf97",
    "x" + chr(0xD800) + chr(0xD800) + "y": "1f85bdc75f53cb4f",
}


def test_view_digest_matches_what_a_browser_hashes() -> None:
    assert kp.view_digest("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    for text, prefix in _BROWSER_DIGEST_PREFIXES.items():
        digest = kp.view_digest(text)
        assert len(digest) == 64 and digest.startswith(prefix), ascii(text)


def test_handle_inputs_present_is_the_rule_both_handle_functions_apply():
    """: one predicate, read by the drain before it applies a member's write. It
    must say False exactly where goal_handle and item_handle return "" for a valid id."""
    for secret in ("", " ", "s3cret"):
        for env in ("", "  ", "env-1"):
            present = kp.handle_inputs_present(secret, env)
            assert present == bool(kp.goal_handle("g-1-1", secret, env)), (secret, env)
            assert present == bool(kp.item_handle("node", "acme", secret, env)), (secret, env)
    assert kp.handle_inputs_present(" ", "env-1"), "the secret is taken as given, unstripped"
    assert not kp.handle_inputs_present("s3cret", " \t"), "a blank environment_id is absent"
