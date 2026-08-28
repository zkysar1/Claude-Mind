"""Lane-pin claim gate — Layer B for the Standing Lane Pins registry ().

A lane pin is a durable USER-DIRECTED routing constraint on ONE agent: a grant
says "this action needs no user approval"; a pin says "this agent's work surface
is FIXED by user directive". The registry lives in the domain world at
``conventions/capability-routing.md`` under a ``## Standing Lane Pins`` heading,
as a markdown table. This module parses that table LIVE on every call, so a pin
is retired by deleting its row — no code change, no redeploy (outcome 2).

Layer A is the pinned agent's own ``self.md`` + the registry read at prime time
(honor-system). Layer B is this gate: it fires at goal CLAIM time in the daemon
claim endpoint and REFUSES an out-of-lane claim by a pinned agent. Layer C is a
guardrail on selection narration. Motivation, measured 2026-08-06: prose
directives and escalations did not hold — the pinned agent claimed 54 goals in
14 hours, ~80% of which did not need its box.

WHY THIS GATE REFUSES ONLY ON CONFIDENT EVIDENCE
------------------------------------------------
The two error directions are NOT symmetric, so the decision rule is not
symmetric either:

  * A false REFUSAL wedges a legitimate claim. The agent cannot proceed, and
    the loop has no recourse but an override — which is exactly the honor-system
    escape this gate exists to remove.
  * A false ALLOW merely leaves the status quo ante (Layer A honor-system),
    which is where the fleet already was.

So the gate refuses ONLY when the goal matches out-of-lane evidence AND matches
NO in-lane evidence. Any ambiguity — both columns matching, or neither — allows.
This is the same posture guard-142 prescribes for gates that block risky
decisions: fail OPEN on your own dependency.

DISCRIMINATING TOKENS
---------------------
The two prose columns share vocabulary. A pin that grants "trace capture to
shared storage" in-lane while forbidding "trace log analysis" out-of-lane puts
`trace` on BOTH sides, and the live pin this was tuned against does exactly
that with its own storage noun. A token present in both columns carries no
discriminating signal, so single-token
matching uses only the SET DIFFERENCE of the two columns' tokens. Multi-word
phrases are matched verbatim and are not subject to that filter — a phrase is
already specific enough to be evidence. Both are derived from the registry text
at parse time, so a re-worded pin re-tunes the matcher with no code change.

The two evidence kinds also read DIFFERENT surfaces of the goal: a single token
must appear in the title or category, while a phrase may appear anywhere
including the description. See `_goal_headline` for the measurement behind that
split — descriptions cite file paths procedurally, so a lone noun found there
says nothing about the work.

Public API:
    parse_pins(text) -> list[dict]
    evaluate(agent, goal, *, registry_text=None, world_dir=None,
             override_lane_pin=None, meta_dir=None) -> dict

Return shape (mirrors sibling blocking gates — gates.operator_offload):
    {"would_block": bool, "fired": bool, "reason": str, "pin_id": str|None,
     "verdict": "no-pin"|"in-lane"|"out-of-lane"|"ambiguous"|"unknown",
     "evidence": list[str], "override": str|None}

Daemon safety: no env reads. The ONLY file read is the registry markdown, and
every failure path (missing world dir, unreadable file, absent heading,
malformed table) returns a no-op ALLOW. Telemetry via _gate_log is best-effort
and never raises.
"""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path
from typing import Optional

from _gate_log import log as _gate_log  # type: ignore

GATE_ID = "lane-pin-gate"

REGISTRY_RELPATH = "conventions/capability-routing.md"
_HEADING_RE = re.compile(r"^##\s+Standing Lane Pins\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)

# Minimum length for a single token to be usable evidence. Below this, tokens
# are too generic to discriminate ("chat", "own", "box").
_MIN_TOKEN_LEN = 5
# Minimum length for a multi-word phrase to be usable evidence.
_MIN_PHRASE_LEN = 8

# A pin cell mixes two kinds of text: an ENUMERATION of lane items (short
# comma/semicolon-separated noun phrases) and COMMENTARY about how the agent
# should behave. Only the enumeration is lane evidence. These two caps separate
# them structurally, without encoding any domain vocabulary in core:
#
#   _MAX_ITEM_WORDS  — a fragment longer than this is prose, not a list item,
#                      so its tokens are not harvested.
#   _MAX_PARENT_WORDS — a parenthetical elaborates its parent clause. When that
#                      parent is a long prose clause rather than a short list
#                      item, the parenthetical is commentary too and is dropped
#                      with it.
#
# Both were set by measuring the live pin-001 row (). Without the
# parent cap, "(error text, session id, storage path)" — a parenthetical inside
# a commentary sentence — leaked the tokens `error` and `session`, which then
# refused two goals having nothing to do with the lane. Widening either cap
# re-admits that class; the failure is silent, so re-measure against real goals
# before changing them.
_MAX_ITEM_WORDS = 4
_MAX_PARENT_WORDS = 5

# Tokens that survive the set-difference filter but still carry no lane signal.
# Deliberately SHORT and domain-free: this is a floor against structural words,
# not a place to encode domain knowledge (that lives in the registry).
_GENERIC = frozenset({
    "agent", "agents", "goal", "goals", "claim", "claims", "work", "works",
    "never", "always", "every", "other", "others", "their", "there", "these",
    "those", "which", "while", "would", "should", "could", "about", "after",
    "before", "during", "under", "until", "where", "whose", "being", "doing",
    "using", "based", "given", "needs", "needed", "requires", "required",
    "including", "includes", "excluding", "standing", "recurring", "value",
    "valid", "invalid", "lane", "lanes", "pinned", "score", "selector",
    "justification", "coordination", "capability", "surface",
})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-/+_.]*")

# Metadata columns, read by HEADER NAME rather than by position.
#
# `expires` was read as `cells[6]` from the day this module shipped, and nothing
# anywhere in the tree ever read the parsed value -- a dead field ().
# `review_by` is read by name instead, and `expires` is moved onto the same path,
# because a positional read is invisible to the person who inserts a column in
# the middle of the registry table: both fields would silently start reading
# their neighbour's text, with no error and no test failure. Fail-open in the
# module's posture -- a missing header row or a missing column yields "", and the
# feature simply does not fire.
_METADATA_COLUMNS = ("granted", "expires", "review_by")


def _norm_header(cell: str) -> str:
    """Header cell -> canonical column key ('Review By' -> 'review_by').

    Deliberately NOT `_strip_markdown`: that strips `_` as emphasis, so the
    header `review_by` normalized to `reviewby` and the column silently read
    empty while its neighbours parsed fine (caught by round-tripping the live
    registry, g-115-5901). Only `*` and backtick are emphasis here; every other
    non-alphanumeric run collapses to `_`, so `Review By`, `review-by` and
    `review_by` all land on the same key.
    """
    return re.sub(r"[^a-z0-9]+", "_", _norm(re.sub(r"[*`]+", "", cell or ""))).strip("_")


def _norm(text) -> str:
    """Lowercase + collapse whitespace. Non-str input yields ''."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _strip_markdown(cell: str) -> str:
    """Remove emphasis/backticks so phrases match plain goal prose."""
    return re.sub(r"[*_`]+", "", cell or "")


def _roster():
    """Fleet agent names, lowercased. Never raises — an unreadable roster just
    means the pin's own agent name is the only exclusion (see parse_pins)."""
    try:
        from _agents import get_active_agents  # SSOT, same source as capability_route
        return {str(a).lower() for a in (get_active_agents() or ())}
    except Exception:
        return set()


_PAREN_RE = re.compile(r"\(([^()]*)\)")


def _split_fragments(cell: str):
    """Split a prose table cell into candidate fragments.

    A pin's most specific terms often sit inside parentheses ("(relay,
    portproxy, host plugin, ...)"), so parentheticals are LIFTED into their own
    fragments — but only when their parent clause is short enough to be a list
    item (see _MAX_PARENT_WORDS). A parenthetical hanging off a long prose
    clause is commentary and is dropped with it.
    """
    text = _strip_markdown(cell)
    lifted = []

    def _take(m):
        # The parent clause is the text back to the previous clause boundary.
        head = text[:m.start()]
        parent = re.split(r"[;.]", head)[-1].strip()
        if len(parent.split()) <= _MAX_PARENT_WORDS:
            lifted.append(m.group(1))
        return " "  # remove the parenthetical from the outer text either way

    outer = _PAREN_RE.sub(_take, text)

    out = []
    for chunk in [outer] + lifted:
        for frag in re.split(r"[;,.]", chunk):
            frag = _norm(frag)
            if not frag:
                continue
            # Split a leading label ("run roblox worlds: ..." -> both halves).
            if ":" in frag:
                head, _, tail = frag.partition(":")
                for piece in (head.strip(), tail.strip()):
                    if piece:
                        out.append(piece)
                continue
            out.append(frag)
    return out


def _build_matcher(cell: str, exclude_tokens=frozenset()):
    """Return (phrases, tokens) usable as evidence for one column.

    Tokens are harvested ONLY from fragments short enough to be enumeration
    items (_MAX_ITEM_WORDS); longer fragments are prose commentary and
    contribute nothing. `exclude_tokens` drops names that can never be lane
    evidence (the fleet roster — an agent's own name appearing in its pin row
    must not classify a goal that merely mentions that agent).
    """
    phrases, tokens = set(), set()
    for frag in _split_fragments(cell):
        words = frag.split()
        # Multi-word phrases are specific enough to stand alone as evidence.
        if len(words) > 1 and len(frag) >= _MIN_PHRASE_LEN:
            phrases.add(frag)
        if len(words) <= _MAX_ITEM_WORDS:
            for tok in _WORD_RE.findall(frag):
                tok = tok.strip(".,;:/-")
                if len(tok) >= _MIN_TOKEN_LEN and tok not in exclude_tokens:
                    tokens.add(tok)
    return phrases, tokens


def parse_pins(text: str):
    """Parse the ``## Standing Lane Pins`` markdown table.

    Returns a list of pin dicts. ANY failure — no heading, no table, malformed
    rows — returns [] so the caller no-ops. Never raises.
    """
    pins = []
    try:
        if not isinstance(text, str) or not text:
            return pins
        m = _HEADING_RE.search(text)
        if not m:
            return pins
        section = text[m.end():]
        nxt = _NEXT_HEADING_RE.search(section)
        if nxt:
            section = section[:nxt.start()]
        header = []
        for line in section.splitlines():
            line = line.strip()
            if not line.startswith("|") or not line.endswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4:
                continue
            pin_id = _strip_markdown(cells[0]).strip()
            # Skip the header row and the |----|----| separator.
            if not pin_id or pin_id.lower() == "id" or set(pin_id) <= set("-: "):
                if pin_id.lower() == "id":
                    header = [_norm_header(c) for c in cells]
                continue
            agent = _norm(_strip_markdown(cells[1]))
            if not agent:
                continue
            in_cell, out_cell = cells[2], cells[3]
            # An AGENT NAME is never lane evidence. The pin row names its own
            # agent in prose ("Foxtrot FILES code defects..."), so without this
            # any goal merely mentioning that agent matched the out-of-lane
            # column. The roster is the same SSOT gates.capability_route uses.
            exclude = set(_roster()) | {agent}
            in_phrases, in_tokens = _build_matcher(in_cell, exclude)
            out_phrases, out_tokens = _build_matcher(out_cell, exclude)
            # A token in BOTH columns discriminates nothing — drop it from both.
            shared = in_tokens & out_tokens
            pin = {
                "id": pin_id,
                "agent": agent,
                "in_phrases": in_phrases,
                "out_phrases": out_phrases,
                "in_tokens": (in_tokens - shared) - _GENERIC,
                "out_tokens": (out_tokens - shared) - _GENERIC,
            }
            for col in _METADATA_COLUMNS:
                idx = header.index(col) if col in header else -1
                pin[col] = _norm(cells[idx]) if 0 <= idx < len(cells) else ""
            pins.append(pin)
        #  defect TWO: FOUR registry states produce ONE observable.
        # Measured against the live file — reformatting the table to a bullet
        # list, removing a column, renaming the heading, and the registry being
        # genuinely ABSENT all yield zero pins with no error and no log, against
        # a baseline that parses one. The fail-open is CORRECT and stays: a
        # broken registry must never block a claim.
        #
        # But the gate CAN tell a human reformat from a deliberate retirement,
        # and was simply not saying so. When the heading still MATCHES and the
        # section still HAS CONTENT and yet zero rows parsed, that is not a
        # retired registry — every pin was deleted by editing, or the table
        # shape changed underneath the parser. Say it loudly and keep allowing.
        #
        # Deliberately NOT warned: heading absent (that is the retired/absent
        # case and is indistinguishable from "this world has no pins" — warning
        # there would fire on every world that never adopted the feature).
        if not pins and section.strip():
            print(
                "[lane-pin] WARNING: '%s' matched the Standing Lane Pins "
                "heading and the section is non-empty, but ZERO pin rows "
                "parsed. Every pin is unenforced right now. This is a table-"
                "shape or row-content problem, not a retirement — a retired "
                "registry has no heading. Allowing the claim (fail-open by "
                "design)." % (REGISTRY_RELPATH,),
                file=sys.stderr,
            )
    except Exception:
        return []
    return pins


_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _as_date(cell) -> Optional[datetime.date]:
    """First ISO date inside a cell, or None. Prose around it is fine."""
    m = _ISO_DATE_RE.search(cell or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def review_status(pin: dict, today=None) -> Optional[dict]:
    """Is this pin PAST its review_by date? None when it is not, or cannot say.

    THE PIN IS NEVER WEAKENED BY THIS, AND `evaluate` DELIBERATELY DOES NOT CALL
    IT. A hard expiry would silently hand an agent back a work surface the user
    took away on purpose — a lapsed review date is evidence that nobody has
    looked lately, which is the opposite of evidence that the constraint should
    end. So a lapsed `review_by` changes exactly one thing: the pin starts SAYING
    SO at startup, and keeps saying so until a human confirms or retires it. Only
    the user retires a pin, by deleting the row.

    Returning None on an absent/unparseable date is the fail-open direction that
    matches the rest of this module: a registry that cannot be read must never
    manufacture a prompt, because a prompt nobody can act on trains the reader to
    scroll past the ones that matter.
    """
    try:
        if not isinstance(pin, dict):
            return None
        due = _as_date(pin.get("review_by"))
        if due is None:
            return None
        today = today or datetime.date.today()
        if isinstance(today, str):
            today = _as_date(today)
            if today is None:
                return None
        if today <= due:
            return None
        granted = _as_date(pin.get("granted"))
        days_since_grant = (today - granted).days if granted else None
        age = ("granted %d days ago" % days_since_grant if days_since_grant is not None
               else "grant date not recorded")
        return {
            "pin_id": pin.get("id", ""),
            "agent": pin.get("agent", ""),
            "review_by": due.isoformat(),
            "granted": granted.isoformat() if granted else "",
            "days_overdue": (today - due).days,
            "days_since_grant": days_since_grant,
            "message": (
                "%s is past review_by (%s, %d days ago), %s, STILL ENFORCED — "
                "confirm or retire." % (pin.get("id", "pin"), due.isoformat(),
                                        (today - due).days, age)),
        }
    except Exception:
        return None


def _goal_text(goal: dict) -> str:
    """Everything a PHRASE may match against: title + description + category."""
    if not isinstance(goal, dict):
        return ""
    parts = [goal.get("title") or "", goal.get("description") or "",
             goal.get("category") or ""]
    return _norm(" ".join(str(p) for p in parts))


def _goal_headline(goal: dict) -> str:
    """Everything a single TOKEN may match against: title + category.

    A goal's WORK TYPE — the thing a lane pin decides about — lives in its
    title and category. The description is supporting detail: prior context,
    error text, and above all FILE PATHS cited procedurally. So a bare noun in
    a description is incidental vocabulary, not evidence about the work.

    Measured over all 1,505 live pending goals (g-115-5152): matching single
    tokens against the description too refused "Check agent email inbox for
    alerts and messages" — a routine inbox sweep with no code in it — on the
    lone token `scripts`, harvested from `bash world/scripts/inbox-watch-pull.sh`
    inside its procedure. That is the false-refusal class this gate is built to
    avoid, and it is invisible from the title alone.

    Phrases keep the full surface (see `_hits`): "framework scripts" appearing
    anywhere is genuine signal, because a two-word phrase from the registry is
    already specific enough to be evidence.
    """
    if not isinstance(goal, dict):
        return ""
    parts = [goal.get("title") or "", goal.get("category") or ""]
    return _norm(" ".join(str(p) for p in parts))


def _hits(phrase_text: str, token_text: str, phrases, tokens):
    """Evidence for one column, longest phrase first.

    TWO surfaces on purpose — see `_goal_headline` for why tokens get the
    narrower one. Callers pass (full text, headline); passing the same string
    twice restores the pre-g-115-5152 behavior and its false-refusal class.
    """
    found = [p for p in phrases if p in phrase_text]
    found.sort(key=len, reverse=True)
    for tok in sorted(tokens, key=len, reverse=True):
        if re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", token_text):
            found.append(tok)
    return found


def _block_message(pin, evidence) -> str:
    ev = ", ".join(repr(e) for e in evidence[:4])
    return (
        f"lane-pin {pin['id']}: agent '{pin['agent']}' is PINNED by user directive "
        f"and this goal is OUT OF LANE (matched {ev}; no in-lane evidence).\n"
        f"A lane pin is a durable user-directed routing constraint — the selector "
        f"score is NOT a justification for an out-of-lane claim, and a premise "
        f"re-probe cannot clear it (only the user can, by deleting the row).\n"
        f"Registry: world/{REGISTRY_RELPATH} -> '## Standing Lane Pins' -> {pin['id']}.\n"
        f"CORRECT ALTERNATIVE: leave this goal for the fleet — a pin does not "
        f"restrict the other agents, it GUARANTEES them this surface. Claim an "
        f"in-lane goal instead.\n"
        f"Genuine edge case: re-claim with --override-lane-pin \"<written "
        f"justification>\" (audited to world/override-bypass-ledger.jsonl)."
    )


def _read_registry(world_dir) -> Optional[str]:
    """Read the registry markdown. Any failure -> None (caller no-ops)."""
    if world_dir is None:
        return None
    try:
        return Path(world_dir).joinpath(REGISTRY_RELPATH).read_text(encoding="utf-8")
    except Exception:
        return None


def evaluate(agent, goal, *, registry_text=None, world_dir=None,
             override_lane_pin: Optional[str] = None,
             meta_dir=None) -> dict:
    """Run the gate. See module docstring for the contract.

    `registry_text` short-circuits the file read (tests, and any caller that
    already holds the registry). When it is None the registry is read from
    `world_dir`; when that is also None the gate no-ops.
    """
    allow = {"would_block": False, "fired": False, "reason": "no-pin",
             "pin_id": None, "verdict": "no-pin", "evidence": [],
             "override": None}
    try:
        agent_name = _norm(agent)
        if not agent_name:
            return allow

        text = registry_text if registry_text is not None else _read_registry(world_dir)
        if not text:
            #  / foxtrot N=45 (2026-08-13): the FIFTH state, reachable
            # from the CALLER side rather than the registry side. A caller that
            # supplies NEITHER registry_text NOR world_dir gets
            # would_block=False / reason="no-pin" -- byte-identical to "this
            # agent has no pin". Measured: that call returned no-pin for all 8
            # probed goals INCLUDING one aspirations-claim.sh had refused with
            # lane_pin_refused ~19h earlier; supplying world_dir reproduced the
            # block exactly. A clean, confident, structurally-inert PASS with
            # nothing to notice.
            #
            # Same observable and same direction as the heading-matched/
            # zero-rows case parse_pins warns about, so it gets the same
            # treatment and the same posture: warn loudly, still fail open.
            # The asymmetry this closes is that the `except` branch below
            # already WARNs (citing guard-1977 -- a gate that silently declines
            # to run reports success by default) while this degrade path stayed
            # silent; the lesson had been applied to the raise-path only.
            #
            # DELIBERATELY NARROW: a world_dir that WAS supplied but holds no
            # registry is a legitimate absence (a fresh world), the analogue of
            # parse_pins' silent heading-absent branch. Only the
            # nothing-to-look-at case is a caller wiring defect.
            if registry_text is None and world_dir is None:
                print(
                    "[lane-pin] WARNING: evaluate() was given NEITHER "
                    "registry_text NOR world_dir, so the gate could not look at "
                    "the registry at all. Returning 'no-pin', which is "
                    "indistinguishable from 'this agent has no pin' -- every "
                    "pin is unenforced for this call. This is a CALLER wiring "
                    "problem, not a retirement. Allowing the claim (fail-open "
                    "by design).",
                    file=sys.stderr,
                )
            return allow  # no registry reachable -> fail open

        pins = [p for p in parse_pins(text) if p["agent"] == agent_name]
        if not pins:
            # THE HOT PATH: no pin row for this agent. Auto-lift lives here —
            # delete the row and every claim by that agent takes this branch.
            # No telemetry: an unpinned claim is not a gate event.
            return allow

        haystack = _goal_text(goal)      # phrases match here
        headline = _goal_headline(goal)  # single tokens match here only
        if not haystack:
            return allow  # nothing to classify -> fail open

        for pin in pins:
            out_hits = _hits(haystack, headline,
                             pin["out_phrases"], pin["out_tokens"])
            if not out_hits:
                continue  # no out-of-lane evidence from this pin
            in_hits = _hits(haystack, headline,
                            pin["in_phrases"], pin["in_tokens"])
            if in_hits:
                # Both columns match — the pin does not settle this goal.
                # ALLOW: a false refusal wedges a claim, a false allow only
                # leaves the Layer-A honor system where it already was.
                _gate_log(GATE_ID, "noop", caller="aspirations-claim",
                          payload={"pin_id": pin["id"], "agent": agent_name,
                                   "verdict": "ambiguous",
                                   "in": in_hits[:3], "out": out_hits[:3]},
                          meta_dir=meta_dir, agent_name=agent_name)
                return {"would_block": False, "fired": True,
                        "reason": "ambiguous — matched both lanes; allowing",
                        "pin_id": pin["id"], "verdict": "ambiguous",
                        "evidence": out_hits[:4] + in_hits[:4], "override": None}

            if override_lane_pin:
                _gate_log(GATE_ID, "override", caller="aspirations-claim",
                          payload={"pin_id": pin["id"], "agent": agent_name,
                                   "out": out_hits[:3]},
                          override_reason=override_lane_pin,
                          meta_dir=meta_dir, agent_name=agent_name)
                return {"would_block": False, "fired": True,
                        "reason": "override", "pin_id": pin["id"],
                        "verdict": "out-of-lane", "evidence": out_hits[:4],
                        "override": override_lane_pin}

            _gate_log(GATE_ID, "block", caller="aspirations-claim",
                      payload={"pin_id": pin["id"], "agent": agent_name,
                               "out": out_hits[:3]},
                      meta_dir=meta_dir, agent_name=agent_name)
            return {"would_block": True, "fired": True,
                    "reason": _block_message(pin, out_hits),
                    "pin_id": pin["id"], "verdict": "out-of-lane",
                    "evidence": out_hits[:4], "override": None}

        # A pin exists for this agent but nothing matched its out-of-lane column.
        return {"would_block": False, "fired": True, "reason": "in-lane-or-unmatched",
                "pin_id": pins[0]["id"], "verdict": "in-lane", "evidence": [],
                "override": None}
    except Exception:
        # Requirement (3): a broken gate must NEVER wedge the fleet's claims.
        return allow
