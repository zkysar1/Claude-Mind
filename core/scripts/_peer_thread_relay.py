"""_peer_thread_relay — pure predicate for peer-deployment thread replies that
never reached the peer.

THE DEFECT (g-115-5845, measured 2026-08-11): a user replies to an `[Omni]`
thread. alert-sweep files that reply as a goal in THIS world's queue, correctly.
Agents here correctly DO NOT work it — precedent is that a reply to a peer's
thread is not a directive to this world, and acting would mean two deployments
editing one bid. So it ages with no owner, and nothing escalates. Meanwhile the
peer's own poll can miss it entirely, because both worlds drain the SAME shared
S3 inbox and a manifest held for the peer can be archived by another world's
drain first. Every party behaves correctly and the message still reaches nobody.
It has already cost a real deadline, in the user's words: "It never reached you.
The deadline passed July 8 and nothing".

WHY THIS KEYS ON DELIVERY, NOT GOAL STATUS — the single most important line in
this file. The reclaim lanes (Q/P/B) key on pending-questions, participants
containing `user`, and blocked/defer_reason. These goals are participants
`[agent]`, no defer_reason, no blocker_ref, and — measured on the live queue —
`in-progress`, NOT `pending`. They are structurally invisible to all three lanes
AND to any status-keyed predicate. Measured 2026-08-12 (echo, cc-03): 8 live
`[Omni]` user replies aged 1.3-4.8d, of which 6 had no relay; a
`status == "pending"` predicate over that same population returns ZERO. That is
reclaim-routed-work.md rule 7 exactly — a predicate narrower than the population
it must cover reports clean forever. The regression test pins that zero.

DELIVERY is therefore evidenced OUTSIDE the goal record: a coordination-board
post tagged `relay` (or `forward-to:<agent>@<env>`) that carries the goal id as
a tag. That is what alpha's real relays look like — msg-20260811-121940-alpha-5716
carries tags `relay`, `forward-to:omni@zds-mind`, `g-115-5774`, `g-115-5777`,
`g-115-5807` — so the signal is structured and already in production use, not
invented here.

REPORT-ONLY ON THE OUTBOUND HALF, AND THAT IS A DESIGN DECISION, NOT AN
UNFINISHED EDGE. g-115-5890 carries the constraint verbatim: "relay reachability
is box-dependent, so any call site added must not auto-relay from whichever box
happens to run it." peer-board-post.sh genuinely refuses from some boxes (it
refused from cc-04 during the originating incident, which is why that relay went
via zeta). A sweep that auto-relayed would therefore succeed or fail based on
which machine's cadence fired it. So this module never SENDS.

THE INBOUND HALF — PEER ACKS — IS A DIFFERENT CLAIM WITH DIFFERENT EVIDENCE, and
it is the half that was missing (measured 2026-08-17, alpha, cc-13, with the
owner present). The `sweep()` docstring below says peer receipt "lives on the
PEER's board, which a box without peer reachability cannot read". That premise
is FALSIFIED by the peer's own behaviour: because the reverse route is closed,
omni@zds-mind posts its receipts and per-id dispositions on THIS board — verbatim
2026-08-16 (msg-20260816-063324-omni-5713): "Posting from inside this container
because peer-board-post.sh to zds-mind is exit 3 from every ayoai-mind box".
Those posts cite goal ids explicitly and ask for the closes: 2026-08-12
(msg-20260812-200239-omni-5555): "SIX OF THE EIGHT ARE ALREADY DONE ON THIS SIDE.
They are a RELAY-LEDGER artifact, not undone work — ayoai-mind holds them
in-progress HIGH because nobody closed the loop back to you ... Per-id below so
you can close them on evidence rather than on my say-so." Five days later all
five were still non-terminal, and two of them had just been mailed to the user
in the user-participant digest as open asks OF HIM — the user read that as being
ignored on questions the peer had answered within hours. The stale ledger
MANUFACTURED the signal.

So `peer_acked` is a bucket whose evidence is a PEER-AUTHORED post on the local
board citing the goal id — direct written evidence, not inference — and the
wrapper's `--close-acked` closes those RELAY goals. Closing is NOT box-dependent
(the board is world-shared), so g-115-5890's constraint does not reach it. What
gets closed is the relay ARTIFACT, whose only job was the handoff; the underlying
work belongs to the peer's world and is not touched (guard-3824: a peer's claim
that the work is done is a claim — the close note says "handoff confirmed", not
"work verified"). The predicate is a regex over the FULL text of every peer post,
never a prefix slice (guard-3712).
"""
import re
from datetime import datetime

# Mirrors insight-trigger-sweep.TERMINAL_GOAL_STATES, itself synced to the SSOT
# aspirations.TERMINAL_GOAL_STATUSES (). A goal in any of these is done
# with; only non-terminal goals can be stranded.
TERMINAL_GOAL_STATES = {"completed", "skipped", "expired", "decomposed", "superseded"}

# First bracketed token in a subject line: `Re: [Omni] DoDEA is a no-bid` -> Omni.
BRACKET_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)\]")

GOAL_ID_RE = re.compile(r"^g-\d+-\d+$")

# Goal ids ANYWHERE in a post's text or tags. guard-3712: a containment question
# ("does this peer post cite this goal") is answered by a regex over the FULL
# text, never a prefix slice — per-id dispositions put their answers in later
# sections BY CONSTRUCTION, so the more thorough the peer was, the further past
# any slice the citation sits.
GOAL_ID_ANYWHERE_RE = re.compile(r"\bg-\d+-\d+\b")

# Inbound-email origin_signal prefixes. THREE forms carry keys live, measured
# 2026-08-12 over the world queue while building email_goal_dedup ():
# `alert-email:` 202, `user_directed:` 3, `user-directed:` 1. Pinning only the
# first silently drops the other two — the same partial-predicate class this
# module exists to fix, so all three are matched.
INBOUND_ORIGIN_PREFIXES = ("alert-email", "user_directed", "user-directed")


def extract_bracket(title):
    """First `[Token]` in a title, or None. Free text — most titles have none."""
    m = BRACKET_RE.search(str(title or ""))
    return m.group(1) if m else None


def is_inbound_email_goal(goal):
    """Was this goal filed from an inbound email rather than by an agent?"""
    sig = str(goal.get("origin_signal") or "")
    return any(sig.startswith(p) for p in INBOUND_ORIGIN_PREFIXES)


def is_non_terminal(goal):
    """NOT status-keyed to `pending` — see the module docstring. The live
    population is `in-progress`, and a pending-only predicate returns zero."""
    return str(goal.get("status") or "") not in TERMINAL_GOAL_STATES


def routed_agents(tags, known_agents):
    """Which REAL agents a board post's tags actually notify ().

    RELAYED AND ROUTED ARE DIFFERENT CLAIMS, and conflating them is the defect
    this answers: a post can satisfy the relay predicate below while notifying
    nobody. `requires_action_by:` is the ONLY recognised routing prefix
    (board.py's own warning, verbatim: `forward-to:omni@zds-mind` parses to
    agent `forward-to:omni` and matches nothing), and a bare agent name also
    routes.

    THE DEFINITION IS BORROWED, NOT RE-STATED (guard-3935: the instrument's
    classification axis must be the directive's own). `parse_routing_tag` is
    the SSOT that board.py and peer_surface already decide routing with, so a
    second local parser here could disagree with the thing it is measuring.

    `known_agents` IS LOAD-BEARING AND ITS ABSENCE IS NOT A DEGRADED MODE — it
    is a different measurement. Board tags are free-form, so almost any word
    (`peer-thread`, `revenue`, `worker-body`) parses to a bare "agent" name.
    Measured on the live 720h coordination window: WITHOUT a roster check, 25
    of 25 relay posts read as routed and the defect vanishes; WITH it, 13 route
    and 12 do not. A probe that cannot see the defect it was written for is the
    first hypothesis to test, not a clean result (guard-2421). Passing an empty
    set therefore reports NOTHING as routed rather than everything.
    """
    known = {str(a).lower() for a in (known_agents or ())}
    if not known:
        # Behaviourally REDUNDANT with the loop below (an empty set matches
        # nothing either way) and therefore not mutation-verifiable — it is here
        # to state the contract at the top and skip the import. Do not read it
        # as the thing that enforces the empty-roster semantics; the loop is.
        return []
    from peer_surface import parse_routing_tag

    hits = []
    for tag in tags:
        if GOAL_ID_RE.match(str(tag)):
            continue
        agent, _env = parse_routing_tag(tag)
        # The roster check alone rejects `forward-to:` — the unrecognised prefix
        # survives INTO the parsed agent name (`forward-to:omni`), and no agent
        # name contains a colon. An extra `":" not in agent` guard was written
        # here and REMOVED: no mutation could turn it red, and an unfalsifiable
        # branch reads as protection that is not there.
        if agent and agent.lower() in known:
            hits.append(agent.lower())
    return sorted(set(hits))


def build_delivery_index(board_rows, known_agents=None):
    """{goal_id: [{"msg_id":..., "targets":[env,...], "routes_to":[agent,...]}]}

    A row counts as a relay when it carries the `relay` tag or any
    `forward-to:<agent>@<env>` tag. Both are read because the two have appeared
    together in production and neither alone is guaranteed by a convention.

    `routes_to` is REPORTED, never used to reject a relay — see the sweep's
    `relayed_unrouted` bucket for why the acceptance predicate deliberately
    stays where it was.
    """
    index = {}
    for row in board_rows or ():
        tags = [str(t) for t in (row.get("tags") or ())]
        lowered = [t.lower() for t in tags]
        forwards = [t for t in lowered if t.startswith("forward-to:")]
        if "relay" not in lowered and not forwards:
            continue
        targets = []
        for f in forwards:
            _, _, addr = f.partition(":")
            _, _, env = addr.partition("@")
            if env:
                targets.append(env)
        routes = routed_agents(tags, known_agents)
        for t in tags:
            if GOAL_ID_RE.match(t):
                index.setdefault(t, []).append(
                    {"msg_id": row.get("id"), "targets": sorted(set(targets)),
                     "routes_to": routes}
                )
    return index


def build_peer_ack_index(board_rows, registry, self_env, roster):
    """{goal_id: [{"msg_id":..., "author":..., "peer_env":..., "timestamp":...}]}
    from posts AUTHORED BY A PEER-DEPLOYMENT AGENT that cite the goal id.

    Author acceptance is borrowed from the SSOT classifier, not re-stated: the
    author must classify `peer` — declared by a peer's known_agents and NOT in
    the local roster. `ambiguous` (e.g. `zeta`, both local and zds-mind's) is
    REFUSED: a local zeta post citing a goal id is a local agent talking, and
    reading it as a peer receipt would launder a handoff into an all-clear.
    Both live author forms are accepted — bare `omni` (40 of 42 peer posts in
    the last 30d) and qualified `omni@zds-mind` (2 of 42) — and when the
    qualified form names an env it must be the env the classifier resolved,
    so `omni@ayoai-mind` (nonsense) matches nothing.

    Citation is text OR tags, over the FULL body (guard-3712). A peer post
    that mentions a goal id in passing is still an ack of that id's receipt:
    the peer cannot cite an id it never received. What it does NOT evidence is
    that the peer AGREES or has DONE the work — see the module docstring; the
    consumer closes the relay artifact only.
    """
    from _peer_registry import classify_agent_name

    index = {}
    for row in board_rows or ():
        author = str(row.get("author") or "").strip()
        if not author:
            continue
        name, _, env_suffix = author.partition("@")
        verdict, peer_env = classify_agent_name(name.lower(), registry, self_env, roster)
        if verdict != "peer":
            continue
        if env_suffix and env_suffix.strip().lower() != str(peer_env).lower():
            continue
        text = str(row.get("text") or "")
        cited = set(GOAL_ID_ANYWHERE_RE.findall(text))
        cited.update(t for t in (str(x) for x in (row.get("tags") or ())) if GOAL_ID_RE.match(t))
        for gid in cited:
            index.setdefault(gid, []).append(
                {"msg_id": row.get("id"), "author": author, "peer_env": peer_env,
                 "timestamp": row.get("timestamp")}
            )
    return index


def _age_days(created_at, now=None):
    try:
        ts = datetime.fromisoformat(str(created_at).replace("Z", ""))
    except (ValueError, TypeError):
        return None
    return round(((now or datetime.now()) - ts).total_seconds() / 86400.0, 1)


def sweep(goals, board_rows, registry, self_env, roster, now=None):
    """Partition inbound-email goals into relayed / undelivered / ambiguous.

    Returns a dict with `undelivered` — the declined-without-relay bucket that
    g-115-5890 asks to escalate — plus the two buckets that must NOT be silently
    folded into it:

      ambiguous   — the bracket token names an agent declared by BOTH a peer and
                    the local roster. Never auto-routed (see
                    _peer_registry.classify_agent_name).
      relayed     — a relay post carries this goal id.

    READ `relayed` PRECISELY: it evidences that a RELAY WAS ISSUED, **not** that
    the peer received anything. The distinction is not pedantic — it is the
    parent incident's own mechanism. Real production relays are HANDOFFS: alpha's
    msg-20260811-121940-alpha-5716 carries `forward-to:omni@zds-mind` AND
    `requires_action_by:zeta@ayoai-mind`, because peer-board-post.sh refused from
    that box and a local agent with the route had to carry it. A relay tag alone
    cannot prove the peer read anything, and a bucket named `delivered` keyed on
    OUR tags would assert exactly what those tags cannot see — turning a handoff
    into a laundered all-clear, which is reclaim-routed-work.md rule 2
    ("well-formed is not valid") applied to relays. Naming it `relayed` keeps the
    claim inside the evidence.

    (An earlier version of this paragraph said receipt "lives on the PEER's
    board, which this box cannot read". That was true of the peer's OWN board
    and is still true — but the peer answers on THIS board precisely because
    the reverse route is closed, so receipt IS observable here in one specific
    form: a peer-AUTHORED post citing the id. That is the `peer_acked` bucket,
    added 2026-08-17; see the module docstring for the incident that showed the
    unconsumed acks were manufacturing a false "ignored" signal to the user.)

      peer_acked  — a post authored by an agent that classifies `peer` (never
                    `ambiguous`), from the SAME peer env the thread names, cites
                    this goal id in its full text or tags. Supersedes the other
                    three buckets for that goal. The wrapper's `--close-acked`
                    closes these relay artifacts.

    A relay post whose forward-to targets EXCLUDE the thread's own peer env does
    NOT count as delivery: it evidences a relay to somebody else. Failing toward
    "undelivered" is deliberate — an over-report costs one line in a precheck
    banner, an under-report re-creates the silent-strand defect.

    `relayed_unrouted` (g-115-6347) SPLITS the relayed bucket without moving
    anything out of it. TWO DENOMINATORS, and confusing them makes the counter
    look broken (guard-3542). Measured 2026-08-15/16 on the live 720h window:
    across ALL relay posts carrying a goal id, **12 of 25 route to NOBODY** —
    but this sweep's own population is only the inbound-email goals whose
    bracket token names a PEER agent, which was **1 of 9** the same hour. The
    small number is not an under-report; it is the same defect seen through a
    deliberately narrower instrument. THE VERDICT IS
    DELIBERATELY UNCHANGED. The bare-`relay`-satisfies-the-check behaviour is
    DOCUMENTED breadth (this docstring's own "A relay post whose forward-to
    targets EXCLUDE..." paragraph, and `not r["targets"]` in the matcher), and
    guard-3628 says a sweep leaving a record untouched may be KEEPING it
    deliberately — so tightening here would re-flag 8 days of historical posts
    on a rule nobody agreed to change. That is the judgment call g-115-6347
    scope (c) asks for, made explicitly: DO NOT TIGHTEN, REPORT THE SPLIT. The
    count is what lets a reader see the gap that the flat verdict hides, and it
    is what a future decision to tighten would be argued from.
    """
    from _peer_registry import classify_agent_name

    # Every agent name this world can name: the local roster plus every peer's
    # declared roster. Union rather than local-only — a relay to `omni@zds-mind`
    # routes perfectly well and must not read as unrouted.
    known_agents = set(roster or ())
    for env_cfg in (registry or {}).values():
        for name in ((env_cfg or {}).get("known_agents") or []):
            known_agents.add(str(name).strip())

    delivery = build_delivery_index(board_rows, known_agents)
    acks = build_peer_ack_index(board_rows, registry, self_env, roster)
    # `peer_acked` is a FOURTH bucket and it SUPERSEDES the other three for a
    # goal: a peer-authored post citing the id is stronger evidence than any
    # relay tag this side wrote, so an acked goal is not also reported as
    # undelivered or relayed. `peer_ack_posts_scanned` travels with the result
    # so a zero can be read (guard-3712: matched-count beside scanned-count).
    out = {"undelivered": [], "relayed": [], "ambiguous": [], "peer_acked": [],
           "scanned": 0, "relayed_unrouted": 0,
           "peer_ack_posts_scanned": sum(len(v) for v in acks.values())}

    for goal in goals or ():
        if not is_non_terminal(goal) or not is_inbound_email_goal(goal):
            continue
        token = extract_bracket(goal.get("title"))
        if not token:
            continue
        # Subject lines carry the display form `[Omni]`; agent names are
        # lowercase kebab-case everywhere (CLAUDE.md naming rules) and
        # `known_agents` stores them that way. Comparing the raw token matches
        # NOTHING — every peer thread reads as `unknown` and the sweep reports a
        # permanently clean queue, which is this file's own defect class turned
        # on itself. Caught pre-ship by running classify against live data.
        verdict, peer_env = classify_agent_name(token.lower(), registry, self_env, roster)
        if verdict not in ("peer", "ambiguous"):
            continue
        out["scanned"] += 1
        gid = goal.get("id")
        rec = {
            "goal_id": gid,
            "title": goal.get("title"),
            "status": goal.get("status"),
            "priority": goal.get("priority"),
            "peer_agent": token,
            "peer_env": peer_env,
            "age_days": _age_days(goal.get("created_at"), now),
        }
        if verdict == "ambiguous":
            # Advise the CANONICAL lowercase name, not the subject line's display
            # form. routing_tag_targets_agent compares component-wise against the
            # agent name (deliberately not a prefix/glob, guard-2860), so a
            # poster who copied `Zeta@zds-mind` from this advice would emit a tag
            # that matches nothing. Same display-case-vs-canonical-name defect as
            # the classification call above, resurfacing in the human-facing half
            # — which is why the test asserts on the advice string too.
            rec["reason"] = (
                "%s is declared by peer %s AND is a local agent — cannot route on "
                "the bare name; ask the poster for %s@<env-id>"
                % (token, peer_env, token.lower())
            )
            out["ambiguous"].append(rec)
            continue
        # Peer ack — the ack must come from the SAME peer env the thread names.
        # An omni@zds-mind post cannot ack a goal whose bracket token resolved to
        # some other peer deployment.
        acked = [a for a in (acks.get(gid) or []) if a.get("peer_env") == peer_env]
        if acked:
            rec["peer_acked_via"] = [a["msg_id"] for a in acked]
            rec["peer_ack_authors"] = sorted({a["author"] for a in acked})
            out["peer_acked"].append(rec)
            continue
        relays = delivery.get(gid) or []
        matched = [
            r for r in relays
            if not r["targets"] or (peer_env and peer_env in r["targets"])
        ]
        if matched:
            rec["relayed_via"] = [r["msg_id"] for r in matched]
            rec["routes_to"] = sorted({a for r in matched for a in r.get("routes_to") or ()})
            if not rec["routes_to"]:
                # The relay was ISSUED and notifies NOBODY. Still `relayed` —
                # see the docstring's relayed_unrouted paragraph for why the
                # verdict does not move.
                rec["routing_gap"] = "relay carries no tag that notifies a known agent"
                out["relayed_unrouted"] += 1
            out["relayed"].append(rec)
        else:
            if relays:
                rec["reason"] = (
                    "relay post(s) %s carry this goal id but forward to %s, not %s"
                    % ([r["msg_id"] for r in relays],
                       sorted({e for r in relays for e in r["targets"]}) or "nobody",
                       peer_env)
                )
            else:
                rec["reason"] = "no relay post carries this goal id"
            out["undelivered"].append(rec)

    for bucket in ("undelivered", "relayed", "ambiguous", "peer_acked"):
        out[bucket].sort(key=lambda r: -(r.get("age_days") or 0))
    return out


def status_keyed_control(goals, registry, self_env, roster):
    """The FALSIFYING control  requires: what a status-keyed predicate
    finds over THE SAME POPULATION this module sweeps. Returns the pending-only
    count.

    The peer-classification filter is load-bearing and was missing in the first
    cut — without it this counted every inbound-email goal carrying any bracket
    token at all, a strictly larger population, and returned 1 on live data
    where the like-for-like answer is 0. A control measured over a different
    population than the claim is not a control; it is a second number that
    happens to sit next to the first. Caught by running it against live data and
    noticing it disagreed with the hand measurement it was meant to reproduce.

    Kept in the module rather than only in the test so the claim stays checkable
    from production: if this ever stops returning 0 on a live stranded set, the
    delivery-keying argument above needs re-measuring, not re-asserting.
    """
    from _peer_registry import classify_agent_name

    n = 0
    for g in goals or ():
        if not is_inbound_email_goal(g):
            continue
        token = extract_bracket(g.get("title"))
        if not token:
            continue
        verdict, _ = classify_agent_name(token.lower(), registry, self_env, roster)
        if verdict in ("peer", "ambiguous") and str(g.get("status") or "") == "pending":
            n += 1
    return n
