# domain-leak-exempt: projection redaction patterns enumerate framework id shapes
# (rb-/guard-/g- ids, *.sh/*.py) as REGEXES to strip them from kid-facing output —
# they are functional, not illustrative. All domain/agent specifics are INJECTED.
"""KnowledgeProjection — the durable-store analogue of zak-code's SafeEventProjection.

Transforms the Mind's accumulated knowledge stores (tree nodes, reasoning-bank
lessons, guardrails, hypotheses) into a SAFE, kid-facing view before any byte can
leave the box (PEARL §10.3). Two filters, then redaction:

1. Domain/framework filter — expose what the agent learned about its RESEARCH,
   suppress the cognitive plumbing:
   - Tree nodes: suppress the ``system/`` subtree (framework internals); expose every
     other top-level category (the domain subtrees). The set of exposed top-level
     categories is the DOMAIN CATEGORY ALLOWLIST.
   - Reasoning bank: expose ``applies_to`` in {domain, any} AND a domain-allowlisted
     category. applies_to alone is too loose — "any"-tagged cross-cutting ENGINEERING
     lessons (category ``framework-architecture`` / ``infrastructure`` / etc.) carry
     internal class/constant/method identifiers in their prose that the id/path
     redaction does not catch. The intersection with the category allowlist is the
     fail-closed cut (verified 2026-07-15 against a self-referential world: 1345
     applies_to-only → 22 intersection, the framework-engineering lessons dropped).
   - Guardrails + hypotheses: these stores do NOT carry a reliable ``applies_to``
     (guardrails were 817/820 unset as of 2026-07-15), so they are filtered by
     CATEGORY against the domain allowlist — an ALLOWLIST, never a denylist, so an
     untagged framework entry fails CLOSED (suppressed) rather than leaking.

2. Redaction — every exposed string passes through :func:`redact`: filesystem paths →
   ``[path]``, known agent names → "the agent", framework ids (rb-N / guard-N / g-N-N /
   *.sh / *.py) removed, and secrets dropped in THREE tiers, because any one alone
   fails open on a public endpoint:
     a. exact injected ``secret_values`` — what the box's environment holds;
     b. secret SHAPES (:data:`_SECRET_PATTERNS`) — PEM blocks, URL-embedded credentials,
        provider-prefixed tokens, ``KEY=value`` pairs. Catches the credential the agent
        wrote into a research note or pasted from a log, which tier (a) never sees
        because its value was never in the environment;
     c. a high-entropy catch-all (:func:`_redact_high_entropy`) for opaque leftovers no
        pattern can enumerate.
   Injected specifics (agent names, workspace paths, secret values) keep ``core/``
   domain-free.

The output preserves wiki SHAPE (titles, summaries, parent/child links, hypothesis
statement+outcome, guardrail rule in plain language) — a traversable structure, not a
redacted husk.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: The one framework subtree in the knowledge tree. Its top-level category name is the
#: single reliable "this is cognitive plumbing" signal (path-segment based).
FRAMEWORK_TREE_ROOT = "system"

#: reasoning-bank ``applies_to`` values that represent domain learning (exposed). Every
#: other value ("framework", "specific", or missing) is suppressed — fail closed.
_EXPOSED_APPLIES_TO = frozenset({"domain", "any"})

#: Front-matter keys of an agent ``self.md`` that may reach a customer. An ALLOWLIST,
#: fail-closed like every other cut in this module: a key absent from this set is
#: suppressed whether or not anyone thought about it. Deliberately tiny — the two
#: dates answer "how current is this identity?", which is the only front-matter
#: question a customer has. Everything else in that block is framework-internal
#: (``revision_id``/``previous_revision_id`` are the revision chain, ``source`` is
#: provenance, and ``last_update_trigger`` is a narrative that cites goal ids, agent
#: names and internal findings verbatim).
SELF_EXPOSED_FM_FIELDS = frozenset({"created", "last_updated"})

#: The count keys that represent actual KNOWLEDGE. The broken-export refusal in
#: knowledge-export's ``main()`` fires when every one of these is zero over a world that
#: demonstrably holds stores. ``self`` is excluded on purpose: it is projected from the
#: agent directory, not from the world stores, so it is non-zero on a world whose four
#: knowledge stores all failed to project. Folding it into that check would let a
#: genuinely broken export pass the very gate that exists to catch it — the failure
#: g-368-34 built the refusal for, and the one guard-5144 records 13 sidecars living in.
KNOWLEDGE_COUNT_KEYS: tuple[str, ...] = ("tree", "hypotheses", "guardrails", "lessons")

#: Cap on the projected purpose text. A self.md runs 44-55 KB; the identity statement
#: is its opening prose and is far under this. The cap bounds a malformed file, it is
#: not the filter — :func:`project_self` is.
_SELF_PURPOSE_CAP = 1200

#: Framework-id shapes stripped from any exposed string. These are internal handles
#: (reasoning-bank / guardrail / goal ids, script filenames) — noise and mild leakage
#: to a kid. Order matters: strip compound ids before bare tokens.
_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bg-\d+-\d+\b"),             # goal ids g-NNN-NN
    re.compile(r"\b(?:rb|guard|sig|sa|bel|sq|asp|pt|trans)-\d+\b", re.IGNORECASE),
    # Source/config filenames, incl. an optional :NNN line suffix (e.g. Driver.java:1718).
    # A code-file reference is never legitimate kid-facing domain content, so stripping it
    # is defense-in-depth for a node/lesson whose prose cites code. Stem must start with a
    # letter/underscore so section numbering ("3.c") is not matched; single-letter code
    # extensions (.c/.h) are omitted as too collision-prone with prose.
    re.compile(
        r"\b[A-Za-z_][\w-]*\.(?:sh|py|ya?ml|jsonl?|java|tsx?|jsx?|go|rs|cpp|lua|kt)(?::\d+)?\b"
    ),
    # Function-call syntax (filter_actions(), next_frame()) — code, never kid prose.
    re.compile(r"\b[A-Za-z_]\w*\(\)"),
    # Leading-underscore SCREAMING_SNAKE constants (_BFS_MAX_NODES, _API_KEY). The leading
    # underscore is what disambiguates from legitimate acronyms (NASA, DNA, H2O carry none).
    re.compile(r"\b_[A-Z][A-Z0-9_]{2,}\b"),
)

#: Secret SHAPES, stripped even when the value was never injected via ``secret_values``.
#: Exact-value redaction alone only catches secrets the box's environment happens to hold —
#: a credential the agent wrote into a research note, pasted from a log, or belonging to a
#: different service passes straight through to a PUBLIC endpoint. These patterns close that
#: class structurally, mirroring zak-code's sibling ``redact_secrets_extended``.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private-key blocks. The END fence is OPTIONAL on purpose: a note that
    # captured a key from a TRUNCATED log has a BEGIN and no END, and requiring the
    # fence let the key body through verbatim (fresh-eyes F-4). With no END, consume
    # to end-of-string — everything after an unterminated BEGIN is presumed key
    # material. Over-redaction is the correct failure direction on a public surface.
    re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----(?:.*?-----END[A-Z ]*PRIVATE KEY-----|.*\Z)",
        re.DOTALL,
    ),
    # URL-embedded credentials (scheme://user:pass@host): drop the creds, keep the URL shape.
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    # Provider-prefixed API tokens. Prefix + a long opaque tail is never domain prose.
    re.compile(r"\b(?:gsk_|sk-|vin_|ghp_|gho_|github_pat_|xox[baprs]-|AKIA|ASIA)[A-Za-z0-9_\-]{8,}\b"),
    # key=value / key: value where the KEY itself names a credential.
    re.compile(
        r"\b\w*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)\w*\s*[=:]\s*\S+",
        re.IGNORECASE,
    ),
)

#: High-entropy catch-all thresholds — deliberately identical to zak-code's sibling
#: projection so both surfaces redact the same strings. A 40-char git SHA scores ~4.0 and
#: passes; a random API token scores >4.5 and is dropped.
_ENTROPY_MIN_LEN = 25
_ENTROPY_THRESHOLD = 4.5


def _shannon_entropy(s: str) -> float:
    """Bits-per-character Shannon entropy of ``s`` (0.0 for the empty string)."""
    if not s:
        return 0.0
    from collections import Counter
    from math import log2

    n = len(s)
    return -sum((c / n) * log2(c / n) for c in Counter(s).values())


def _redact_high_entropy(text: str) -> str:
    """Final backstop: drop whitespace-delimited tokens that LOOK like opaque secrets.

    Catches the unprefixed, un-injected case the pattern list cannot enumerate. Bounded to
    long tokens above the entropy threshold so ordinary prose — and the domain vocabulary
    this wiki exists to publish — is untouched.
    """
    if not text:
        return text
    # Split on whitespace RUNS while KEEPING them (capture group), so newlines and
    # indentation survive verbatim. Splitting on " " alone made a newline part of the
    # token: "intro\n<blob>" scored as ONE high-entropy token and the whole thing —
    # the prose AND the line break — was replaced by the marker (fresh-eyes F-2).
    # Node bodies are multi-line markdown, so that silently ate real content.
    out = []
    for part in re.split(r"(\s+)", text):
        if not part or part.isspace():
            out.append(part)
            continue
        core = part.strip(".,;:!?()[]{}\"'")
        if len(core) >= _ENTROPY_MIN_LEN and _shannon_entropy(core) > _ENTROPY_THRESHOLD:
            out.append(part.replace(core, "[redacted]"))
        else:
            out.append(part)
    return "".join(out)


def top_level_category(category_or_path: str) -> str:
    """First path segment of a tree category or node file path.

    ``"system/hooks/foo"`` → ``"system"``; ``"intelligence/npc"`` → ``"intelligence"``;
    a bare ``"intelligence"`` → ``"intelligence"``. Also tolerates a full node ``file``
    like ``"world/knowledge/tree/system/foo.md"`` by dropping the tree-root prefix.
    """
    s = (category_or_path or "").strip().strip("/")
    if not s:
        return ""
    parts = s.split("/")
    # Drop a leading world/knowledge/tree/ prefix if a full node path was passed.
    marker = ["world", "knowledge", "tree"]
    if len(parts) > len(marker) and parts[: len(marker)] == marker:
        parts = parts[len(marker) :]
    seg = parts[0]
    # A top-level category ROOT node's file is ``tree/<cat>.md`` (e.g. ``system.md``),
    # so the first segment carries the extension. Strip it so the root node classifies
    # under its category — otherwise the framework root ``system.md`` reads as a domain
    # category ("system.md" != "system") and leaks into the exposed wiki. Category names
    # are kebab-case and never end in these extensions, so stripping is safe.
    for ext in (".md", ".yaml", ".yml", ".jsonl", ".json"):
        if seg.endswith(ext):
            return seg[: -len(ext)]
    return seg


@dataclass(frozen=True)
class Redactor:
    """Injected specifics for :func:`redact` — keeps ``core/`` domain-free.

    ``agent_names`` are replaced by "the agent"; ``workspace_paths`` (and any absolute
    path) become ``[path]``; ``secret_values`` (exact strings, e.g. loaded from the
    environment) become ``[redacted]``. All are optional — an empty Redactor still
    strips absolute paths and framework ids.
    """

    agent_names: tuple[str, ...] = ()
    workspace_paths: tuple[str, ...] = ()
    secret_values: tuple[str, ...] = ()

    def __call__(self, text: str) -> str:
        return redact(
            text,
            agent_names=self.agent_names,
            workspace_paths=self.workspace_paths,
            secret_values=self.secret_values,
        )


def redact(
    text: str,
    *,
    agent_names: Iterable[str] = (),
    workspace_paths: Iterable[str] = (),
    secret_values: Iterable[str] = (),
) -> str:
    """Strip framework/host internals from a kid-facing string.

    Order is deliberate: secret VALUES first (before any structural rewrite can split
    them), then explicit workspace paths, then generic absolute paths, then agent
    names, then framework ids. Longest-first within each class so a prefix match never
    leaves a tail behind.
    """
    if not text:
        return text
    out = text
    # 1. Exact secret values — longest first so an overlapping shorter secret can't
    #    leave a fragment. Never emit any part of the value.
    for secret in sorted((s for s in secret_values if s), key=len, reverse=True):
        out = out.replace(secret, "[redacted]")
    # 1b. Secret SHAPES — the un-injected case. Runs BEFORE the path pass so URL-embedded
    #     credentials are dropped while the URL keeps its shape (the path regex would
    #     otherwise chew the scheme and leave the password standing).
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[redacted]", out)
    # 2. Explicit workspace paths (longest first), then generic absolute paths.
    for wp in sorted((p for p in workspace_paths if p), key=len, reverse=True):
        out = out.replace(wp, "[path]")
    out = _ABS_PATH_RE.sub("[path]", out)
    # 3. Agent names → "the agent" (word-boundary, case-insensitive, longest first).
    for name in sorted({n for n in agent_names if n}, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(name)}\b", "the agent", out, flags=re.IGNORECASE)
    # 4. Framework ids and script/store filenames.
    for pat in _ID_PATTERNS:
        out = pat.sub("", out)
    # 5. Entropy backstop LAST — after the structural passes have had their chance, so a
    #    path/id/token is redacted by the rule that describes it, and only genuinely opaque
    #    leftovers fall to the catch-all.
    out = _redact_high_entropy(out)
    # Tidy the double spaces / orphaned punctuation id-stripping can leave behind.
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


#: Absolute paths: POSIX (``/home/…``) and Windows (``C:\…`` / ``C:/…``). Conservative —
#: only matches rooted paths so ordinary prose ("and/or") is untouched.
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[\w.\-\\/]{2,}")


# ── filter predicates ────────────────────────────────────────────────────────

def is_domain_tree_node(category_or_path: str) -> bool:
    """A tree node is domain (exposed) iff its top-level category is not ``system``."""
    tl = top_level_category(category_or_path)
    return bool(tl) and tl != FRAMEWORK_TREE_ROOT


def domain_categories(tree_nodes: Iterable[Mapping[str, object]]) -> frozenset[str]:
    """Derive the domain-category allowlist from the exposed tree nodes.

    A node is represented by a mapping carrying ``category`` (preferred) or ``file``.
    The allowlist is every top-level category of an exposed (non-``system``) node —
    the reliable signal guardrails/hypotheses (which lack ``applies_to``) filter on.
    """
    allow: set[str] = set()
    for node in tree_nodes:
        key = str(node.get("category") or node.get("file") or "")
        tl = top_level_category(key)
        if tl and tl != FRAMEWORK_TREE_ROOT:
            allow.add(tl)
    return frozenset(allow)


def is_exposed_reasoning(entry: Mapping[str, object]) -> bool:
    """Reasoning-bank entries filter on the reliable ``applies_to`` field."""
    return str(entry.get("applies_to", "")).strip().lower() in _EXPOSED_APPLIES_TO


def is_exposed_by_category(entry: Mapping[str, object], allow: frozenset[str]) -> bool:
    """Guardrails + hypotheses lack a reliable ``applies_to`` → allowlist by category.

    Fails CLOSED: an entry whose category is missing or whose top-level is not in the
    domain allowlist is suppressed, so an untagged framework entry never leaks.
    """
    tl = top_level_category(str(entry.get("category") or ""))
    return bool(tl) and tl in allow


# ── projections (shape-preserving) ───────────────────────────────────────────

def project_self(
    front_matter: Mapping[str, object] | None,
    body: str,
    redactor: "Redactor",
) -> dict[str, object]:
    """Project an agent ``self.md`` down to the customer-facing identity view.

    PEARL §10.3 is filter-at-the-source, so the cut is made HERE and the consumer holds
    no projection logic. ``self.md`` is not domain knowledge — it is the agent's
    operating identity, and the bulk of it is exactly the "cognitive plumbing" §10.3
    suppresses: absolute workspace paths, box hostnames, sub-repo tiering,
    agent-provisionable action lists, revision chains. So this is an allowlist of two
    dated fields plus ONE bounded slice of prose, and everything else is dropped.

    The prose cut is STRUCTURAL, not a character budget: a ``self.md`` opens with an
    identity statement and then turns into ``##`` sections, and every one of those
    sections is plumbing. Taking the text before the first ``##`` therefore yields the
    identity paragraphs and nothing else, and it stays correct as sections are added
    or reordered — which a line count or a heading denylist would not.

    The agent's NAME is deliberately absent. :class:`Redactor` already rewrites agent
    names to "the agent" in every other projected string; emitting the name here would
    contradict the redaction the rest of the bundle performs.

    Returns ``{}`` when there is nothing exposable — an absent, empty or unreadable
    ``self.md`` yields no key rather than a hollow one, so a consumer can distinguish
    "no identity published" from "identity published and blank".
    """
    fm = dict(front_matter or {})
    out: dict[str, object] = {}

    purpose_src = body or ""
    # Drop a leading "# Heading" line — it is the file's own title ("# Self"), not prose.
    lines = purpose_src.lstrip("\n").split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    # Cut at the first "##" section: everything from there down is plumbing.
    cut = next((i for i, ln in enumerate(lines) if ln.startswith("##")), len(lines))
    purpose = "\n".join(lines[:cut]).strip()
    if purpose:
        out["purpose"] = redactor(purpose)[:_SELF_PURPOSE_CAP]

    for key in sorted(SELF_EXPOSED_FM_FIELDS):
        value = fm.get(key)
        if value in (None, ""):
            continue
        # Dates, like the tree's `last_updated`, are NOT redacted: an ISO date carries
        # no secret and no agent name, and passing it through the redactor could only
        # mangle it.
        out[key] = str(value)

    # A dates-only projection is not an identity — refuse to publish a husk.
    return out if "purpose" in out else {}


@dataclass
class ProjectedBundle:
    """The kid-facing knowledge view. Shape-preserving; every string already redacted."""

    tree: list[dict[str, object]] = field(default_factory=list)
    hypotheses: list[dict[str, object]] = field(default_factory=list)
    guardrails: list[dict[str, object]] = field(default_factory=list)
    lessons: list[dict[str, object]] = field(default_factory=list)
    #: The customer-facing identity view, or ``{}`` when nothing is exposable. Named
    #: ``agent_self`` because a dataclass field literally named ``self`` would shadow
    #: the receiver in every method below; it is emitted under the JSON key ``self``.
    agent_self: dict[str, object] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        # `self` is 0/1, not a length: it is one projected view, not a store. It is in
        # counts() because guard-5144 makes counts the verification signal for an export
        # ("read the COUNTS, never just file presence"), so a projection absent from
        # counts is a projection no verifier can check. It is NOT in
        # KNOWLEDGE_COUNT_KEYS: see that constant for why the distinction is load-bearing.
        return {
            "tree": len(self.tree),
            "hypotheses": len(self.hypotheses),
            "guardrails": len(self.guardrails),
            "lessons": len(self.lessons),
            "self": 1 if self.agent_self else 0,
        }


def project(
    *,
    tree_nodes: Iterable[Mapping[str, object]],
    reasoning: Iterable[Mapping[str, object]],
    guardrails: Iterable[Mapping[str, object]],
    hypotheses: Iterable[Mapping[str, object]],
    redactor: Redactor,
    self_front_matter: Mapping[str, object] | None = None,
    self_body: str = "",
) -> ProjectedBundle:
    """Filter + redact all four stores into a :class:`ProjectedBundle`.

    Every input is an already-parsed iterable of records (store I/O lives in the CLI
    wrapper, so this core is pure and unit-testable). The domain allowlist is derived
    from the exposed tree, then applied to the applies_to-less stores.
    """
    nodes = [n for n in tree_nodes if is_domain_tree_node(str(n.get("category") or n.get("file") or ""))]
    allow = domain_categories(nodes)

    bundle = ProjectedBundle()
    # Keyword-only with defaults, so every existing caller keeps its exact behaviour and
    # gets an empty `agent_self` rather than a changed shape.
    bundle.agent_self = project_self(self_front_matter, self_body, redactor)
    for n in nodes:
        bundle.tree.append(
            {
                "key": str(n.get("key") or n.get("id") or ""),
                "title": redactor(str(n.get("title") or "")),
                "summary": redactor(str(n.get("summary") or "")),
                "body": redactor(str(n.get("body") or "")),
                "parent": str(n.get("parent") or ""),
                "children": [str(c) for c in (n.get("children") or []) if c],
                # NOT redacted, like key/parent/children above and unlike the four text
                # fields — an ISO date carries no secret and no agent name, so passing it
                # through the redactor would only risk mangling it.
                # Consumer: a client rendering "what changed since your last visit"
                # (g-335-1146). No client can compute that without a per-node date,
                # and the bundle-level generated_at/age_seconds describe the EXPORT, not
                # the node. Emitted as "" when absent so the field's presence never
                # implies a real timestamp — a consumer must treat "" as unknown, not old.
                "last_updated": str(n.get("last_updated") or ""),
            }
        )
    for h in hypotheses:
        if is_exposed_by_category(h, allow):
            bundle.hypotheses.append(
                {
                    "statement": redactor(str(h.get("claim") or h.get("title") or "")),
                    "horizon": str(h.get("horizon") or ""),
                    "status": str(h.get("stage") or ""),
                    "outcome": redactor(str(h.get("outcome") or "")),
                }
            )
    for g in guardrails:
        if is_exposed_by_category(g, allow):
            bundle.guardrails.append({"rule": redactor(str(g.get("rule") or ""))})
    for r in reasoning:
        # Reasoning-bank entries carry BOTH a reliable applies_to AND a category. Require
        # both: applies_to ∈ {domain, any} AND a domain-allowlisted category. applies_to
        # alone is too loose — "any"-tagged cross-cutting ENGINEERING lessons (category
        # framework-architecture / infrastructure / framework-maintenance) carry internal
        # class/constant/method identifiers in their prose that the id/path redaction does
        # not strip, and they are exactly the "cognitive plumbing" §10.3 suppresses. The
        # category allowlist (fail-closed, same as guardrails/hypotheses) is the reliable
        # "learned about its research" signal; the intersection is the safe cut.
        if is_exposed_reasoning(r) and is_exposed_by_category(r, allow):
            bundle.lessons.append(
                {
                    "title": redactor(str(r.get("title") or "")),
                    "lesson": redactor(str(r.get("failure_lesson") or r.get("content") or "")),
                }
            )
    return bundle


__all__ = [
    "Redactor",
    "redact",
    "top_level_category",
    "is_domain_tree_node",
    "domain_categories",
    "is_exposed_reasoning",
    "is_exposed_by_category",
    "ProjectedBundle",
    "project",
    "project_self",
    "SELF_EXPOSED_FM_FIELDS",
    "KNOWLEDGE_COUNT_KEYS",
    "FRAMEWORK_TREE_ROOT",
]
