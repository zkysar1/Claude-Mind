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
     untagged framework entry fails CLOSED (suppressed) rather than leaking. A
     guardrail must also be one the resident still follows (:func:`is_active_guardrail`).

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

import hashlib
import hmac
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

#: Goal ``work_class`` values that represent product-facing work (exposed). Every other
#: value ("framework", "hygiene", "unclassified", or missing) is suppressed — fail
#: closed, exactly like :data:`_EXPOSED_APPLIES_TO`. The goal queue is overwhelmingly
#: framework plumbing, so a dump would publish the agent's own maintenance backlog to a
#: member-facing board; ``work_class`` is the field that already draws that line.
_EXPOSED_WORK_CLASSES = frozenset({"product"})

#: Goal ``status`` -> the coarse word a member sees. An ALLOWLIST, fail-closed: a status
#: absent from this map is suppressed. ``blocked``/``skipped``/``expired``/``superseded``
#: /``decomposed`` are internal queue mechanics, and on a "Planned" board they would read
#: as broken promises rather than as the routine bookkeeping they are.
_GOAL_PUBLIC_STATUS: dict[str, str] = {
    "pending": "planned",
    "in-progress": "in progress",
    "completed": "done",
}

#: Cap on a projected goal title. Bounds a malformed record; it is not the filter —
#: :func:`project_goals` is.
_GOAL_TITLE_CAP = 200

#: Hex characters kept from a goal handle's HMAC (:func:`goal_handle`). 16 hex chars is
#: 64 bits: over a queue of a few hundred exposed goals a collision sits around 1e-15,
#: and a collision is not a silent wrong answer here anyway — :func:`resolve_goal_handle`
#: returns ``None`` when two exposed goals share a handle, so the failure mode is "does
#: nothing", never "mutates the wrong member's goal". Short on purpose: this is an
#: ADDRESSING token that rides in a URL or a JSON body, not a credential.
#: Two incidental safety properties worth keeping if this number ever moves: hex caps at
#: 4.0 bits/char, under :data:`_ENTROPY_THRESHOLD`, and 16 is under
#: :data:`_ENTROPY_MIN_LEN` — so a handle cannot be eaten by the high-entropy catch-all
#: if a future caller ever routes it through :func:`redact`.
_GOAL_HANDLE_HEX = 16

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

#: The opt-in markers that delimit the customer-facing region of a ``world/program.md``.
#: Everything outside them is suppressed, and a file carrying NEITHER marker publishes
#: NOTHING. This is the one place this module does not mirror :func:`project_self`, and
#: the divergence is measured rather than stylistic.
#:
#: ``self.md`` has an ENFORCED structure (``.claude/rules/self.md``): an identity
#: statement, then ``##`` sections that are uniformly plumbing. That is what makes a
#: structural "cut at the first ``##``" cut safe there. ``program.md`` has no such rule
#: and is free-form, so the same cut is unsafe on it -- MEASURED 2026-09-02 on the only
#: real ``program.md`` in existence (22,453 B): its pre-``##`` region is 3,216 chars and
#: a self-style 1200-char slice of it ships a verbatim competitive directive naming a
#: named competition track twice ("we are NOT entering ...", "Do NOT publish the ...
#: repo"), while the full region additionally carries internal repo names, an
#: ``EnvironmentAdapter`` contract, source-tree paths and ``AGENT_WRITE_PATH``. The
#: :class:`Redactor` strips none of them: it covers agent names, workspace paths and
#: secrets, which is a different set.
#:
#: So the world DECLARES what is customer-facing instead of a heuristic guessing --
#: filter-at-the-source (PEARL 10.3) applied honestly. Fail-closed is free here because
#: ``published`` already makes "nothing published" a first-class state the consumer
#: renders, so an unmarked world degrades to the honest answer rather than to a leak.
PROGRAM_PUBLIC_OPEN = "<!-- public:begin -->"
PROGRAM_PUBLIC_CLOSE = "<!-- public:end -->"

#: Front-matter keys of a ``world/program.md`` that may reach a customer. Same
#: allowlist, same two dates, same reason as :data:`SELF_EXPOSED_FM_FIELDS` -- "how
#: current is this?" is the only front-matter question a customer has. Kept as its own
#: constant rather than aliased: the two files' front matter evolve independently, and
#: widening one must never silently widen the other.
PROGRAM_EXPOSED_FM_FIELDS = frozenset({"created", "last_updated"})

#: Cap on the projected program text. Bounds a malformed or run-away marked block; it
#: is not the filter -- the markers are.
_PROGRAM_PURPOSE_CAP = 1200

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


#: Absolute paths: POSIX (``/home/…``) and Windows (``C:\…`` / ``C:/…``). NOT only rooted
#: paths, whatever an earlier note here said: the ``/`` branch has no left boundary, so
#: ordinary prose matches too — "and/or" becomes "and[path]" and "9/30/2026" becomes
#: "9[path]" (measured 2026-09-30, g-335-1726: 897 of 1,162 editable-size wiki bodies on
#: one box). It over-redacts, which is the safe direction here, and it is left as it is:
#: narrowing it changes what every bundle publishes and is its own change.
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[\w.\-\\/]{2,}")


def is_unredacted(text: str, redactor: Redactor) -> bool:
    """True when the published view of ``text`` is ``text`` itself.

    Trimmed ends do not count as a change, except leading spaces or tabs, which can
    indent a first line. Everything else does: a secret, a path, an agent name or a
    framework id replaced or removed, a high-entropy token dropped, a run of spaces
    collapsed.

    It answers the member's question before a correction (g-335-1726): "if I replace what
    I see, is anything I cannot see lost?" A correction replaces the stored text with the
    member's text, which starts from the view, so it is safe only when the view is all of
    the stored text. :func:`project` marks those rows ``unredacted``, and the member-edit
    applier refuses the rest (``view_redacted``).
    """
    return redactor(text) == text.strip() and text.lstrip("\r\n") == text.lstrip()


#: The field a correction replaces, per item kind, in the order the published row reads
#: them: a hypothesis with no claim shows its title.
_ITEM_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "node": ("body",),
    "hypothesis": ("claim", "title"),
    "guardrail": ("rule",),
}


def item_text(kind: str, record: Mapping[str, object]) -> str:
    """The stored text behind an item's published view, or ``""`` for an unknown kind.

    :func:`project` publishes ``redactor(item_text(...))`` as the row's text, and the
    member-edit applier hashes the same expression to check a correction's base
    (:func:`view_digest`). One reader for both, so the box can never check a field other
    than the one the member was shown.
    """
    for name in item_text_fields(kind):
        value = record.get(name)
        if value:
            return str(value)
    return ""


def item_text_fields(kind: str) -> tuple[str, ...]:
    """The stored fields that carry an item's member-visible text, in the order
    :func:`item_text` reads them, or ``()`` for an unknown kind.

    The member-edit applier retains and blanks exactly these when a member forgets a
    hypothesis, so what a forget removes is what the member was shown and what an edit
    replaces: one table, not a second list that could drift from it (guard-3970).
    """
    return _ITEM_TEXT_FIELDS.get(kind, ())


def view_digest(view: str) -> str:
    """The ``base`` a correction carries: lowercase hex SHA-256 of the UTF-8 of ``view``.

    ``view`` is the text a member corrected, exactly as the published row carries it. A
    browser hashes the string it parsed from the bundle, and a browser string is UTF-16:
    a surrogate pair is one character and a lone surrogate encodes as U+FFFD. The round
    trip below reads this str the same way, so both ends hash the same bytes, and a lone
    surrogate cannot make the encode raise.
    """
    as_browser = view.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return hashlib.sha256(as_browser.encode("utf-8")).hexdigest()


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


def is_active_guardrail(entry: Mapping[str, object]) -> bool:
    """A guardrail the resident still follows: ``status`` is exactly ``"active"``.

    The literal test the resident's own readers apply (retrieve.py and guardrail-check.py
    keep ``status == "active"``), so a member is shown exactly the rules a run can retrieve
    or fire. A retired rule is hidden, which is how a member's correction replaces the rule
    it superseded (g-335-1726). So is a record with no status or any other value, which no
    run reads either: an allowlist, so it fails closed.
    """
    return entry.get("status") == "active"


#: The field a member's forget stamps on a hypothesis record, holding the UTC instant of the
#: forget (g-335-1726). A pipeline record is never deleted and its lifecycle only moves
#: forward, so a forgotten hypothesis stays where it is with its statement blanked, and this
#: marker is what takes it out of the exposed set.
FORGOTTEN_FIELD = "forgotten_at"

#: The field a member's undo stamps on a hypothesis record, holding the UTC instant the undo
#: began (g-335-1726 u7a). It never decides what is shown: :func:`is_forgotten` reads only the
#: marker above. It exists so the pipeline merge can order an undo against a stale copy of the
#: forget it reversed. A record whose marker was cleared carries no trace of the forget, so
#: without this stamp a copy that still holds the forget cannot be told from a newer one.
RESTORED_FIELD = "restored_at"


def is_forgotten(entry: Mapping[str, object]) -> bool:
    """A hypothesis a member forgot: its ``forgotten_at`` is set.

    ANY truthy value counts, so a marker written in an unexpected shape hides the record
    rather than showing it.
    """
    return bool(entry.get(FORGOTTEN_FIELD))


# ── projections (shape-preserving) ───────────────────────────────────────────

def project_goals(
    goals: Iterable[Mapping[str, object]],
    redactor: "Redactor",
    *,
    handle_secret: str = "",
    environment_id: str = "",
) -> list[dict[str, object]]:
    """Project the goal queue down to the customer-facing "what is planned" view.

    PEARL §10.3 is filter-at-the-source, so the cut is made HERE and the consumer holds
    no projection logic — same contract as :func:`project_self`.

    TWO independent fail-closed gates, because a goal record is the single most
    internal-leaking store in the bundle: its ``outcome_note`` and ``defer_reason`` carry
    verbatim measurements (cloud account ids, table ids, lambda ARNs, box hostnames,
    partner-agent names), and its ``title`` routinely names internal scripts. So this is
    an ALLOWLIST of three fields and everything else is dropped — notes, reasons,
    participants, claim data, priority, scores, ids and aspiration linkage never appear.

    A FOURTH field, ``handle``, is emitted only when ``handle_secret`` is supplied. It is
    an opaque per-environment HMAC of the goal id (:func:`goal_handle`) and exists so a
    member-facing WRITE can say WHICH goal without the board carrying an id — the gap
    that blocked the Planned-board verbs. It does not widen the allowlist: no additional
    goal FIELD is read, the id itself is not recoverable from it, and with no secret
    configured the row is byte-identical to the three-field shape. Title matching was the
    obvious alternative and is REJECTED: titles are capped at :data:`_GOAL_TITLE_CAP`,
    redactor-rewritten and not unique, so a near-miss mutates the wrong member's goal.

    Gate 1 is ``work_class``: only ``product`` is exposed. The queue is overwhelmingly
    framework plumbing, and publishing that would show a member the agent's own
    maintenance backlog rather than the product roadmap they came for.

    Gate 2 is ``status``, mapped through :data:`_GOAL_PUBLIC_STATUS`. An unmapped status
    is suppressed rather than passed through, so a new internal status value added later
    stays private until someone decides otherwise.

    The remaining ``title`` still goes through the redactor, which strips goal ids,
    filenames and agent names — belt and braces, since a product title can still cite a
    script. Returns ``[]`` when nothing is exposable.

    ⚠ THESE GATES ARE NECESSARY AND **NOT SUFFICIENT** — MEASURED, so do not ship a
    member-facing board on them alone without reading this. Live run 2026-08-30 (zeta,
    cc-02, 6.8.0-137-generic): 377 goals projected, and **129 of them (34%) came from
    the framework/maintenance lane**, carrying titles like "Verify remote-filesystem connectivity"
    and "Scan for stale background processes" — internal ops chores that are
    nevertheless tagged ``work_class: product`` in the store. That is a DATA-quality
    problem, not a bug here: those labels are individually defensible (the work *is*
    about platform services), they are simply not roadmap items a member asked to see.

    A category gate was the obvious second filter and was MEASURED AND REJECTED rather
    than assumed: :func:`is_exposed_by_category` against the tree-derived domain
    allowlist does NOT separate the two populations — ONE broad platform-services category covers
    102 of the mis-tagged chores AND 6 genuine goals in a real product lane, and it is a
    legitimate domain category in both. Adding it would narrow nothing while creating
    the impression the surface had been filtered. Filtering by aspiration id WOULD
    separate them cleanly, and is deliberately not done here: this is a core framework
    file and a hardcoded lane id is a domain leak
    (``.claude/rules/domain-free-examples.md``) that would rot at the first re-org.

    The fix belongs upstream in the goal records' ``work_class``. Until it lands, treat
    this projection as "product-classed work" and NOT as a curated roadmap.
    """
    out: list[dict[str, object]] = []
    for g in goals:
        row = _exposed_goal(g, redactor)
        if row is None:
            continue
        # Fail closed: no secret configured -> no handle key at all, so the emitted row
        # is byte-identical to the three-field shape every existing consumer already
        # reads. There is deliberately NO fallback to the goal id here; an unaddressable
        # board is the safe degradation, a published id is not.
        if handle_secret:
            handle = goal_handle(_goal_id_of(g), handle_secret, environment_id)
            if handle:
                row["handle"] = handle
        out.append(row)
    return out


def _goal_id_of(goal: Mapping[str, object]) -> str:
    """The goal's own id, read from the field the RAW store actually carries.

    MEASURED 2026-09-04 over the live world queue (2,887 goal records read through
    ``knowledge-export._read_goals``, the canonical reader): **2,887 carry ``id`` and 0
    carry ``goal_id``**. ``goal_id`` is synthesised by the QUERY layer, so a reader who
    checks a goal through ``aspirations-query.sh`` sees both keys and would naturally
    reach for ``goal_id`` first — against the store, that resolves to nothing and every
    handle silently disappears (the projection would simply emit no ``handle``, which
    looks exactly like "no secret configured"). Hence the order below, and hence this
    note: do not "tidy" it the other way round without re-measuring the STORE.
    """
    return str(goal.get("id") or goal.get("goal_id") or "").strip()


def handle_inputs_present(secret: str, environment_id: str) -> bool:
    """Whether a box holding these two values can key ANY handle (g-335-1726).

    The provisioning half of the emptiness guard :func:`goal_handle` and :func:`item_handle`
    both apply, as one rule: the secret counts as given unstripped, and ``environment_id``
    counts only when it is not blank. A resolver folds an unprovisioned box into the same
    miss as an unknown handle, so a caller that must not treat "this box cannot resolve" as
    "no such item" asks this first.
    """
    return bool(str(secret or "")) and bool(str(environment_id or "").strip())


def goal_handle(goal_id: str, secret: str, environment_id: str = "") -> str:
    """An opaque, per-environment handle for one goal id — or ``""`` when unavailable.

    A truncated HMAC-SHA256 of the goal id under a per-environment ``secret``. It exists
    so a member-facing write ("pause THIS one") can name a target without the board ever
    carrying an id, which :func:`project_goals` strips by design.

    Three properties, all of which the caller depends on:

    * **The id is not recoverable.** HMAC is preimage-resistant, and the goal-id space is
      small enough to enumerate (``g-NNN-NNNN``) — so the *key* is what makes this true,
      not the hash. A publicly-derivable key would let anyone brute-force the id space and
      invert the handle. Never derive ``secret`` from a published value.
    * **No cross-environment correlation.** ``environment_id`` is mixed into the MESSAGE
      (not just relied upon through the key), so the same goal id in two environments
      yields two unrelated handles even if an operator provisions one secret fleet-wide.
    * **No stored mapping.** The box recomputes handles over its own goals to resolve one
      (:func:`resolve_goal_handle`), so there is no side table to keep in sync, migrate,
      or leak.

    Returns ``""`` on an empty id, an empty secret, **or an empty ``environment_id``** —
    fail closed, so an unprovisioned box publishes a board with no handles rather than one
    with a guessable token.

    The ``environment_id`` arm is the one that is easy to leave out, and leaving it out is
    what guard-6312 names as the dangerous shape: a fail-closed guard covering only SOME of
    the message inputs. The covered fields refuse loudly while the uncovered one degrades
    into a well-formed token that matches nothing. Worse, it silently retires the property
    this component exists to carry — with ``environment_id`` empty under one fleet-wide
    secret, every environment publishes IDENTICAL handles and the no-cross-environment-
    correlation promise two bullets up fails open with nothing logged. There is no
    "no environment" mode: the parameter defaults to ``""`` for call-site convenience only,
    and that default now yields ``""`` rather than a correlatable handle.

    ⚠ OPERATIONAL: every box serving one ``environment_id`` must hold the SAME secret.
    The export can run on one box and the write can land on another; with divergent
    secrets the handle computed at export never matches the one computed at resolve, and
    the failure is silent-and-correct (the write resolves to nothing and is dropped).
    """
    gid = str(goal_id or "").strip()
    key = str(secret or "")
    env = str(environment_id or "").strip()
    # EVERY component of the message gets an emptiness guard, not just the obvious secret
    # (guard-6312). `env` is hoisted so the guard and the message read the same value.
    if not gid or not handle_inputs_present(key, env):
        return ""
    # NUL-separated so ("env", "g-1-2") and ("envg", "-1-2") cannot collide.
    msg = f"{env}\x00{gid}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:_GOAL_HANDLE_HEX]


#: The knowledge-item kinds a member can address (g-335-1726): a wiki node, a hypothesis
#: and a guardrail. The same three stores :func:`project` exposes by one predicate.
KNOWLEDGE_ITEM_KINDS = ("node", "hypothesis", "guardrail")


def item_handle(kind: str, item_id: str, secret: str, environment_id: str = "") -> str:
    """An opaque, per-environment handle for one knowledge item — or ``""``.

    The item-level sibling of :func:`goal_handle` (g-335-1726): it lets a member-facing
    write ("correct THIS page", "forget THAT rule") name its target while the projection
    keeps hiding hypothesis and guardrail ids. Same key, same width and the same three
    properties as :func:`goal_handle`. The kind is its own message component, so a node, a
    hypothesis and a guardrail that share an id never share a handle, and an item message
    (two NULs) can never equal a goal message (one NUL).

    Fails closed on EVERY message component (guard-6312): an unknown kind, or an empty id,
    secret or ``environment_id``, returns ``""``. So does a NUL inside the id or the
    environment, which could otherwise forge the separator.
    """
    k = str(kind or "").strip()
    iid = str(item_id or "").strip()
    key = str(secret or "")
    env = str(environment_id or "").strip()
    if k not in KNOWLEDGE_ITEM_KINDS or not iid or not handle_inputs_present(key, env):
        return ""
    if "\x00" in iid or "\x00" in env:
        return ""
    msg = f"{env}\x00{k}\x00{iid}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:_GOAL_HANDLE_HEX]


def _node_key(node: Mapping[str, object]) -> str:
    """A wiki node's id, read the way :func:`project` publishes its ``key``."""
    return str(node.get("key") or node.get("id") or "").strip()


def _record_id(record: Mapping[str, object]) -> str:
    """A hypothesis's or guardrail's id. Both stores carry ``id`` (measured 2026-09-30)."""
    return str(record.get("id") or "").strip()


def _exposed_goal(
    goal: Mapping[str, object], redactor: "Redactor"
) -> dict[str, object] | None:
    """The projected row for one goal, or ``None`` when the goal is not exposable.

    THE single exposure predicate. :func:`project_goals` and :func:`resolve_goal_handle`
    both route through it so the set a member can SEE and the set a member can ADDRESS are
    the same set by construction rather than by two implementations agreeing. Splitting
    them would let a handle resolve to a goal that was never published — the fail-open
    direction, and the one nothing would alert on.
    """
    if str(goal.get("work_class") or "").strip().lower() not in _EXPOSED_WORK_CLASSES:
        return None
    status = _GOAL_PUBLIC_STATUS.get(str(goal.get("status") or "").strip().lower())
    if status is None:
        return None
    title = redactor(str(goal.get("title") or "")).strip()
    if not title:
        return None
    return {
        "title": title[:_GOAL_TITLE_CAP],
        "status": status,
        # NOT redacted, for the same reason tree `last_updated` is not: an ISO
        # date carries no secret and no agent name. "" when absent, so a
        # consumer must read "" as unknown rather than as old.
        "updated": str(
            goal.get("completed_date") or goal.get("started") or goal.get("created_at") or ""
        ),
    }


def resolve_goal_handle(
    handle: str,
    goals: Iterable[Mapping[str, object]],
    secret: str,
    redactor: "Redactor",
    environment_id: str = "",
) -> str | None:
    """Resolve an inbound handle back to exactly ONE goal id, or ``None``.

    The box-side half of :func:`goal_handle`: recompute the handle over the goals this box
    holds and return the id of the one that matches. No stored mapping is read or written.

    Every branch that is not "exactly one exposed goal matches" returns ``None``, because
    the caller is a WRITE path against live member data and a near-miss there mutates the
    wrong member's goal:

    * unknown handle → ``None``; a handle for a goal that has since stopped being
      exposable (status moved out of :data:`_GOAL_PUBLIC_STATUS`, work_class re-tagged)
      also stops resolving, by the same predicate that stopped publishing it;
    * two exposed goals sharing a handle → ``None``, never an arbitrary pick;
    * empty handle, unconfigured secret, or unconfigured ``environment_id`` → ``None``, so
      an unprovisioned box refuses every addressed write instead of resolving them all to
      the same goal. The ``environment_id`` arm is enforced inside :func:`goal_handle`
      rather than by the early return below, so an unprovisioned box still walks the goal
      list before refusing — correct, but see that function's docstring before assuming a
      resolve implies a configured environment.

    Comparison is :func:`hmac.compare_digest` rather than ``==``: a dict lookup keyed on
    the handle would be shorter, and would also hand an attacker a timing oracle over the
    one token that addresses a member's goal. The scan is O(goals) over a few hundred
    records — the cost is not worth the oracle.
    """
    want = str(handle or "").strip().lower()
    if not want or not secret:
        return None
    found: str | None = None
    for g in goals:
        if _exposed_goal(g, redactor) is None:
            continue
        gid = _goal_id_of(g)
        computed = goal_handle(gid, secret, environment_id)
        if computed and hmac.compare_digest(computed, want):
            # A repeated record for the SAME id is not ambiguity; two DIFFERENT ids are.
            if found is not None and found != gid:
                return None
            found = gid
    return found


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


def project_program(
    front_matter: Mapping[str, object] | None,
    body: str,
    redactor: "Redactor",
) -> dict[str, object]:
    """Project a world ``program.md`` down to the customer-facing "what is this FOR" view.

    The consumer-side twin of :func:`project_self`: same return contract, same
    ``{}``-means-nothing-published signal, same allowlisted dated front matter, same
    redact-then-cap ordering. A member who can read WHO their agent is should be able to
    read what it is FOR.

    The ONE deliberate divergence is the prose cut, and it is fail-closed: only the
    region between :data:`PROGRAM_PUBLIC_OPEN` and :data:`PROGRAM_PUBLIC_CLOSE` is
    published. A ``program.md`` with no markers -- which today is every one of them,
    including the zero-byte placeholder ``init-world.sh`` creates -- publishes ``{}``.
    See those constants for the measurement that ruled out mirroring ``self.md``'s
    structural ``##`` cut here.

    Redaction happens BEFORE the cap, never after, so a forbidden token straddling the
    cap boundary cannot survive by being truncated into a different shape -- the same
    ordering :func:`project_self` uses and the same one its cap test pins.

    Returns ``{}`` when there is nothing exposable -- an absent, empty, unmarked or
    unreadable ``program.md`` yields no key rather than a hollow one, so a consumer can
    distinguish "no program published" from "published and blank".
    """
    fm = dict(front_matter or {})
    out: dict[str, object] = {}

    text = body or ""
    start = text.find(PROGRAM_PUBLIC_OPEN)
    end = text.find(PROGRAM_PUBLIC_CLOSE, start + len(PROGRAM_PUBLIC_OPEN)) if start != -1 else -1
    # BOTH markers required, in order. A lone opener is a half-written edit, and
    # publishing "everything after it" would turn a typo into a leak.
    if start != -1 and end != -1:
        purpose = text[start + len(PROGRAM_PUBLIC_OPEN):end].strip()
        if purpose:
            out["purpose"] = redactor(purpose)[:_PROGRAM_PURPOSE_CAP]

    for key in sorted(PROGRAM_EXPOSED_FM_FIELDS):
        value = fm.get(key)
        if value in (None, ""):
            continue
        # Dates are NOT redacted, for the same reason as everywhere else in this module:
        # an ISO date carries no secret and no agent name, and the redactor could only
        # mangle it.
        out[key] = str(value)

    # A dates-only projection is not a program -- refuse to publish a husk, exactly as
    # project_self refuses a dates-only identity.
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
    #: The customer-facing "what is planned" view. A LIST (unlike ``agent_self``), so
    #: emptiness is a length, not a shape change; ``[]`` means nothing exposable.
    goals: list[dict[str, object]] = field(default_factory=list)
    #: The customer-facing "what is this FOR" view, or ``{}`` when nothing is
    #: exposable. An OBJECT like ``agent_self`` (not a list like ``goals``): it is one
    #: projected view, so emptiness is a shape, and ``{}`` is what distinguishes "no
    #: program published" from "published and blank". Emitted under the JSON key
    #: ``program``.
    program: dict[str, object] = field(default_factory=dict)

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
            # A real length, unlike `self` above: goals is a store-shaped list. In
            # counts() for the guard-5144 reason -- a projection absent from counts is a
            # projection no verifier can check. NOT in KNOWLEDGE_COUNT_KEYS: goals are
            # work items, not knowledge, and a world can legitimately publish zero
            # product goals while its four knowledge stores are healthy -- folding it in
            # would let a broken export pass the refusal gate.
            "goals": len(self.goals),
            # 0/1 like `self`, and for the same reason: one projected view, not a store.
            # In counts() because guard-5144 makes counts the verification signal for an
            # export. NOT in KNOWLEDGE_COUNT_KEYS: a world can legitimately publish no
            # program while its four knowledge stores are healthy, so folding it in
            # would let a genuinely broken export pass the refusal gate.
            "program": 1 if self.program else 0,
        }


def _exposed_knowledge(
    tree_nodes: Iterable[Mapping[str, object]],
    hypotheses: Iterable[Mapping[str, object]],
    guardrails: Iterable[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], frozenset[str], list[Mapping[str, object]], list[Mapping[str, object]]]:
    """THE exposure predicate for the three addressable knowledge stores.

    Returns ``(nodes, allow, hypotheses, guardrails)``: the exposed records of each store
    and the domain allowlist they were cut with. :func:`project` publishes exactly these
    records and :func:`resolve_item_handle` addresses exactly these records, so what a
    member can SEE and what a member can ADDRESS are one set by construction (rb-10157,
    the same tie :func:`_exposed_goal` makes for goals). A guardrail is cut by status as
    well as category, so a superseded rule leaves both sets. A hypothesis is cut by its
    forgotten marker as well as category, so a forgotten one leaves both.
    """
    nodes = [n for n in tree_nodes if is_domain_tree_node(str(n.get("category") or n.get("file") or ""))]
    allow = domain_categories(nodes)
    hyps = [h for h in hypotheses if is_exposed_by_category(h, allow) and not is_forgotten(h)]
    guards = [g for g in guardrails if is_active_guardrail(g) and is_exposed_by_category(g, allow)]
    return nodes, allow, hyps, guards


def _with_handle(
    row: dict[str, object], kind: str, item_id: str, secret: str, environment_id: str
) -> None:
    """Add ``handle`` to a projected row when :func:`item_handle` yields one.

    No key at all otherwise (an unprovisioned box, or an id-less record), so a consumer
    reads a missing handle as "not addressable" rather than meeting an empty string.
    """
    handle = item_handle(kind, item_id, secret, environment_id)
    if handle:
        row["handle"] = handle


def _with_unredacted(row: dict[str, object], text: str, redactor: Redactor) -> None:
    """Add ``unredacted: True`` to an addressable row whose editable ``text`` shows whole.

    Only beside a ``handle``, so an unprovisioned box keeps every row's exact shape. No key
    at all otherwise: a consumer reads a missing flag as "not safe to correct".
    """
    if "handle" in row and is_unredacted(text, redactor):
        row["unredacted"] = True


def project(
    *,
    tree_nodes: Iterable[Mapping[str, object]],
    reasoning: Iterable[Mapping[str, object]],
    guardrails: Iterable[Mapping[str, object]],
    hypotheses: Iterable[Mapping[str, object]],
    redactor: Redactor,
    self_front_matter: Mapping[str, object] | None = None,
    self_body: str = "",
    goals: Iterable[Mapping[str, object]] = (),
    program_front_matter: Mapping[str, object] | None = None,
    program_body: str = "",
    goal_handle_secret: str = "",
    environment_id: str = "",
    item_handle_secret: str = "",
) -> ProjectedBundle:
    """Filter + redact all four stores into a :class:`ProjectedBundle`.

    Every input is an already-parsed iterable of records (store I/O lives in the CLI
    wrapper, so this core is pure and unit-testable). The domain allowlist is derived
    from the exposed tree, then applied to the applies_to-less stores.

    ``item_handle_secret`` adds an opaque ``handle`` to each exposed wiki node,
    hypothesis and guardrail row (:func:`item_handle`, g-335-1726). Empty by default, so
    an existing caller keeps its exact shape. A node, hypothesis or guardrail row with a
    handle also carries ``unredacted: True`` when the text a member would correct is
    published whole (:func:`is_unredacted`). A node whose record says ``body_truncated``
    never carries it.
    """
    nodes, allow, hyps, guards = _exposed_knowledge(tree_nodes, hypotheses, guardrails)

    bundle = ProjectedBundle()
    # Keyword-only with defaults, so every existing caller keeps its exact behaviour and
    # gets an empty `agent_self` rather than a changed shape.
    bundle.agent_self = project_self(self_front_matter, self_body, redactor)
    bundle.program = project_program(program_front_matter, program_body, redactor)
    bundle.goals = project_goals(
        goals, redactor, handle_secret=goal_handle_secret, environment_id=environment_id
    )
    for n in nodes:
        # One local for the published text and the flag, so the two can never read
        # different sources.
        body = item_text("node", n)
        bundle.tree.append(
            {
                "key": str(n.get("key") or n.get("id") or ""),
                "title": redactor(str(n.get("title") or "")),
                "summary": redactor(str(n.get("summary") or "")),
                "body": redactor(body),
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
        _with_handle(bundle.tree[-1], "node", _node_key(n), item_handle_secret, environment_id)
        # A body the reader cut at its cap is not the stored body, so it is never offered
        # for correction: the member would replace the whole page with an edit of its first
        # part, which the applier refuses (view_truncated). g-335-1726 finding 7.
        if not n.get("body_truncated"):
            _with_unredacted(bundle.tree[-1], body, redactor)
    for h in hyps:
        statement = item_text("hypothesis", h)
        bundle.hypotheses.append(
            {
                "statement": redactor(statement),
                "horizon": str(h.get("horizon") or ""),
                "status": str(h.get("stage") or ""),
                "outcome": redactor(str(h.get("outcome") or "")),
            }
        )
        _with_handle(
            bundle.hypotheses[-1], "hypothesis", _record_id(h), item_handle_secret, environment_id
        )
        _with_unredacted(bundle.hypotheses[-1], statement, redactor)
    for g in guards:
        rule = item_text("guardrail", g)
        bundle.guardrails.append({"rule": redactor(rule)})
        _with_handle(
            bundle.guardrails[-1], "guardrail", _record_id(g), item_handle_secret, environment_id
        )
        _with_unredacted(bundle.guardrails[-1], rule, redactor)
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


def resolve_item_handle(
    handle: str,
    *,
    tree_nodes: Iterable[Mapping[str, object]],
    hypotheses: Iterable[Mapping[str, object]],
    guardrails: Iterable[Mapping[str, object]],
    secret: str,
    environment_id: str = "",
) -> tuple[str, str] | None:
    """Resolve an inbound item handle back to exactly ONE ``(kind, id)``, or ``None``.

    The box-side half of :func:`item_handle`, with :func:`resolve_goal_handle`'s rules:
    recompute over the records this box holds, walking ONLY the exposed set
    (:func:`_exposed_knowledge`), so a handle for anything the projection hides
    resolves to nothing. Every branch that is not "exactly one exposed item matches"
    returns ``None``: an unknown handle, a missing secret or environment, and two
    different items sharing a handle. The caller is a write path against a member's
    data, so a near-miss must never be picked.
    """
    want = str(handle or "").strip().lower()
    if not want or not secret:
        return None
    nodes, _allow, hyps, guards = _exposed_knowledge(tree_nodes, hypotheses, guardrails)
    found: tuple[str, str] | None = None
    for kind, records, id_of in (
        ("node", nodes, _node_key),
        ("hypothesis", hyps, _record_id),
        ("guardrail", guards, _record_id),
    ):
        for record in records:
            iid = id_of(record)
            computed = item_handle(kind, iid, secret, environment_id)
            if computed and hmac.compare_digest(computed, want):
                # A repeated record for the SAME item is not ambiguity; two items are.
                if found is not None and found != (kind, iid):
                    return None
                found = (kind, iid)
    return found


__all__ = [
    "Redactor",
    "redact",
    "top_level_category",
    "is_domain_tree_node",
    "domain_categories",
    "is_exposed_reasoning",
    "is_exposed_by_category",
    "is_active_guardrail",
    "FORGOTTEN_FIELD",
    "RESTORED_FIELD",
    "is_forgotten",
    "ProjectedBundle",
    "project",
    "project_self",
    "project_program",
    "project_goals",
    "handle_inputs_present",
    "goal_handle",
    "resolve_goal_handle",
    "KNOWLEDGE_ITEM_KINDS",
    "item_handle",
    "is_unredacted",
    "item_text",
    "item_text_fields",
    "view_digest",
    "resolve_item_handle",
    "SELF_EXPOSED_FM_FIELDS",
    "PROGRAM_EXPOSED_FM_FIELDS",
    "PROGRAM_PUBLIC_OPEN",
    "PROGRAM_PUBLIC_CLOSE",
    "KNOWLEDGE_COUNT_KEYS",
    "FRAMEWORK_TREE_ROOT",
]
