#!/usr/bin/env python3
"""Worker-goal retrospective: run the reducer-only lanes a Body structurally skipped.

A WORKER Body executes goals but never runs the reducer-only close phases, so a
goal it completes reaches the shared store with its `outcome_note` written and
the reducer lanes — team-state, journal, findings gate, imp@k — simply absent.
Nothing later fills them in. This module is the fill-in, invoked from
`aspirations-consolidate` immediately AFTER `body-merge.py generalize-down`,
which is the only moment the reducer can name which goals arrived from a Body.

WHY IT TAKES ids RATHER THAN DISCOVERING THEM (g-306-198, measured 2026-08-07)
-----------------------------------------------------------------------------
The originating spec said to scan the merged `goals_completed_this_session` slot
for goals whose `claimed_by_sid != reducer SID`. Three measurements retire that:

  * the slot's rows carry no session id at all — the union of keys across all
    372 live rows on cc-02 is {goal_id, aspiration_id, recurring, work_class,
    _item_ts};
  * `claimed_by` and `claimed_by_sid` are ERASED at close — 0 of 4015 completed
    goals carry either, against 24 of 24 in-progress — and that erasure is
    deliberate (g-115-3176: a stamp outliving its claim makes the NEXT claimer
    inherit a stale SID), so it must not be reverted to make this easier;
  * `body-manifest.yaml` records no goal list.

So worker-completion is recorded in no durable store, and no amount of scanning
recovers it. `body-merge._stamp_merged_goal_ids` now derives it where both sides
of the merge are in hand and publishes `merged_goal_ids`; this module consumes
that. The alternative — inferring worker-completion from a MISSING journal entry
— would be an effect standing in for a cause, i.e. a criterion broadened until
it can be met (guard-2950). It is deliberately not implemented.

FIVE LANES RUN, THREE ARE REPORTED (and the split is the point)
---------------------------------------------------------------
The no-transcription contract is that this is a CALLER of the existing per-lane
writers, never a re-implementation. Held literally, that partitions the eight
lanes by whether a writer exists that can accept a goal id:

  RUN      team_state       team-state-update.sh --field recent_completions
           journal          journal-append.sh --goal
           findings         findings-gate.sh --goal
           experience       experience-archive-goal.sh --goal --trace-file
           impk             state-update-audit.sh velocity --goal

  REPORT   verification     post-hoc verify from goal record + commit diff
           execution_feedback  state-update-audit.sh execution-feedback needs
                            clarity/scope/verify RATINGS of the goal's spec
           user_notable     "would the user want to know?"

THE EXPERIENCE LANE IS THE ONLY ONE WITH AN INPUT (g-306-199)
-------------------------------------------------------------
The other four lanes derive everything they write from the goal RECORD, which
the reducer can always read. The experience lane cannot: an experience .md is a
narrative of an execution the reducer never witnessed, and reconstructing one
from the goal record is exactly the fabrication the REPORT lanes are withheld
for. Its input is the worker's own `exp_capture` working-memory slot, written at
worker-loop Phase 3.6 and carried to the reducer by `body-merge` at
generalize-down — so this lane is applicable only where the executing Body left
a capture, and is SKIPPED (never failed) where it did not. A skipped lane is not
counted in `lanes_written`, so it can neither inflate the imp@k artifact count
nor, on its own, license the marker.

It calls `experience-archive-goal.sh` ONLY, though the goal's contract names
`experience-add.sh` alongside it: the archive-goal endpoint already appends the
record itself (`experience_write.archive_goal` -> `_append_record`) after copying
the trace to the canonical `content_path`. Calling both would file the same
experience twice.

The three reported lanes have no mechanizable input: their writers exist but
consume LLM judgment, and a script that supplies its own judgment scores is
fabricating the measurement the lane was built to record. `state-update-audit`
already makes this exact distinction for imp@k — `cmd_velocity` SKIPS the
snapshot when no quality flags were passed rather than recording a false 0.0
(g-115-2441) — and calling `meta-impk.sh` directly here would bypass that guard,
which is why the impk lane routes through `state-update-audit.sh velocity`.

The `artifacts_count` handed to that lane counts THIS RUN'S successful lane
writes, not the worker's original execution. That is what makes it honest: the
retrospective can measure what the retrospective produced, and can measure
nothing about work it did not witness.

REDUCER-ONLY, AND ENFORCED (g-306-252)
--------------------------------------
`main` refuses with rc=3 when the invoking Body is a worker. The lanes are not
uniformly safe to run from one: `journal-append.sh` guards itself with a
BODY=worker skip that logs to stderr and exits 0, and `retrospect` reads
`rc == 0` as landed — so a worker would count an unwritten journal entry,
inflate `artifacts_count`, and stamp a marker that suppresses the retry forever
for a goal now permanently missing a lane. See the block in `main` for why the
refusal sits here rather than in a per-lane wrote-vs-declined protocol, and why
this guard fails CLOSED where journal-append.sh's fails open.

DEDUP LIVES ON THE GOAL RECORD, NOT IN A LEDGER
-----------------------------------------------
`retrospective_encoded` is written on the shared goal record. A per-agent
ledger under `session/` was the obvious alternative and is wrong for this
population: worker Bodies run on other boxes, the WM slot is per-agent and
per-box and ephemeral, and the goal record is the one store that is shared,
durable, and visible to every box that might retrospect the same goal.

Lanes run BEFORE the marker is written. A crash between the two re-runs the
lanes next pass — journal-append dedups by default and the team-state append is
additive — whereas marking first would lose the lanes permanently. Same trade,
same direction, as `body-merge._consume_staged` (consume only after the content
is durably written).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_bash import bash_cmd  # noqa: E402
# Top-level, not a call-local import: `_paths` pulls in nothing but stdlib
# (os, sys, pathlib) so there is no cycle to dodge, and a module-level binding
# is the one `body_role` can be exercised against without reaching the live
# agents root. CLAUDE.md forbids composing `PROJECT_ROOT / agent_name` by hand —
# `agent_dir` is the sanctioned resolver and tracks `AGENTS_PARENT_DIR`.
from _paths import agent_dir  # noqa: E402

MARKER_FIELD = "retrospective_encoded"
MARKER_SOURCE = "worker-retrospective"

# Lanes this module CALLS. Order matters: the four writers run first so the
# imp@k lane can report how many of them actually landed.
RUN_LANES = ("team_state", "journal", "findings", "experience", "encoding", "impk")

# The experience lane's input slot and the writer surface it calls. The slug is
# baked into the experience id as `exp-<goal-id>-<slug>`, which the endpoint
# validates against `^exp-[a-z0-9._-]+$` — keep it lowercase and hyphenated.
EXP_SLOT = "exp_capture"
EXP_SKILL_SLUG = "worker-retrospective"
EXP_TYPE = "goal_execution"

# The encoding lane (): `encoding_capture` was the LAST of the four
# worker->reducer capture lanes with no consumer anywhere — producer shipped,
# reducer half did not, and 132 entries rode the transport into the reducer WM
# to be read by nothing.
#
# WHY A LANE HERE RATHER THAN A STANDALONE DRAIN SCRIPT: the standalone shape was
# already tried and RETIRED for the sibling lane — `exp_capture_drain.py` was
# deleted in 659dbef14 () in favour of a lane in this module, and
# body-merge.sh:33 records that its placement argument survives independently of
# the ownership claim that surrounded it. Rebuilding that artifact for lane 4
# would re-create exactly what was removed for lane 2. A lane also rides a
# dispatch consolidation ALREADY calls, so it cannot go inert the way a fresh
# call site can (the hyp_capture guard shipped inert at its only call site with a
# fully green unit suite — guard-1943).
#
# WHY THE SINK IS `encoding_queue` AND NOT A DIRECT TREE WRITE: tree PLACEMENT is
# LLM judgment, which is why the worker's `suggested_node` is explicitly
# NON-binding ("the reducer decides placement", worker-loop SKILL.md ~L760).
# Every other lane in this module is mechanizable; this one is not. So the lane
# hands the fact to the queue that consolidation already drains toward the tree
# (learning-routing.md: `wm.encoding_queue[].target_article`) and lets the
# judgment happen where it already lives, instead of auto-writing nodes.
ENC_SLOT = "encoding_capture"
ENC_QUEUE_SLOT = "encoding_queue"
ENC_REPLAY_PRIORITY = "standard_deferred"

# The prose-key fallback chain (guard-4044, applied to THIS lane by ).
# `encoding_capture` has NO enforced schema -- many Bodies append whatever key
# they like -- so a literal single-key read silently drops every entry that used
# a different name, and the drop is invisible because the survivors replay
# normally. Measured on the live 936-entry slot (alpha worker, cc-07,
# 2026-08-24): 804 carry `fact`; 125 (13.4%) do NOT and were dropped by the
# single-key read. The chain below recovers 97 of those 125.
#
# ORDER IS PROSE-BEFORE-LABEL, deliberately. Where an entry carries several of
# these, the first match wins, so a short label ahead of real prose would
# TRUNCATE the observation rather than merely rename it -- `title` is therefore
# last. `content` leads the fallbacks on measurement, not taste: it appears on
# 36 dropped entries at a mean length of 717 chars and is absent from
# guard-4044's original chain, which was derived from `spark_capture`.
#
# The remaining 28 are NOT "empty" (guard-4093: a zero with a blind lane is
# unreachable, not empty) -- they carry prose under narrower keys, and 5 of them
# are exp_capture-shaped rows (execution_summary / key_decisions /
# surprise_level) that were MISROUTED into this slot. Widening the chain to
# swallow those would encode an execution narrative as a world-fact, so they are
# deliberately left to the REPORT half instead: the lane now names how many
# carried no prose key, which is the number that makes a misroute visible.
ENC_PROSE_KEYS = ("fact", "content", "body", "observation", "note", "insight",
                  "lesson", "text", "finding", "summary", "rule", "detail",
                  "title")

# The .md at content_path carries every anchor; the JSONL record carries a
# bounded head of them, because that record is re-read on every experience
# retrieval and a worker unit can capture 20+ anchors. When the bound bites, a
# final anchor SAYS SO and points at the trace — a silent truncation here would
# read as "that is all there was".
ANCHOR_RECORD_MAX = 25

# Narrative fields, in preference order. exp_capture is heterogeneous in the
# live slot (measured cc-08 2026-08-10: 8 of 12 entries carry the Phase-3.6
# shape, 4 carry a looser {summary,note,lesson,outcome,what_worked,what_failed}
# shape), so the lane reads whichever is present instead of assuming one.
EXP_NARRATIVE_FIELDS = ("execution_summary", "summary", "note", "lesson", "outcome")
# Lanes whose writer needs LLM judgment — surfaced for the reducer to act on,
# never auto-filled. See the module docstring.
REPORT_LANES = ("verification", "execution_feedback", "user_notable")

_GOAL_ID_RE = re.compile(r"^g-(\d{1,4})-\d{1,4}$")

SKIP_ALREADY = "already-retrospected"
SKIP_NO_RECORD = "goal-record-not-found"
SKIP_BAD_ID = "malformed-goal-id"
SKIP_NO_CAPTURE = "no-exp-capture-entry"
SKIP_NO_ENCODING = "no-encoding-capture-entry"

#: . Reported by a capture lane whose SLOT READ FAILED. Deliberately not
#: a `SKIP_*`: a skip means "there was nothing to encode" and permits the marker,
#: while this means "we could not see whether there was anything" and forbids it.
SLOT_UNREADABLE = "capture-slot-read-failed"

#: The lanes whose input is a worker capture slot, i.e. the only lanes whose
#: failure can lose something a retry cannot reconstruct. The other three write
#: from the goal record, which survives on disk.
CAPTURE_LANES = ("experience", "encoding")

REFUSE_NOT_REDUCER = "not-reducer-body"


# ─────────────────────── who is allowed to run this ──────────────────────

def body_role(agent: str, sid: str | None = None) -> str:
    """`worker` | `reducer` | `unknown` — which Body is running this process.

    Derived LOCALLY from the forked per-session working-memory file, never from
    the `BODY_ROLE` env var: guard-2445 (HIGH) establishes that every var
    `bash-agent-inject.py` exports exists only inside the command string of a
    Bash *tool* call, so a rail keyed on one is inert everywhere else. The same
    predicate is derived independently at `journal-append.sh`, `stop-hook.sh`,
    `post-recovery-edit-gate.py`, `worker_reducer_liveness` and
    `reducer_self_fence` — deliberately not shared, so no module can quietly
    change another's meaning of "which Body is this".

    `unknown` is returned when the session id is unavailable OR when the
    agent-dir resolver fails, and the caller must treat it as its own case
    rather than folding it into either verdict: an unevaluated check is not a
    passed one (guard-2913).

    Takes no `root`: this resolves through `agent_dir` and nothing else, so
    there is no path a caller could sandbox by handing one in. A `root`
    parameter used to feed the fallback deleted below (g-306-262); keeping it
    once that fallback was gone would have made the signature assert a
    root-relativity the function does not have.
    """
    sid = sid if sid is not None else os.environ.get("MIND_SID", "")
    if not agent or not sid:
        return "unknown"
    try:
        adir = Path(agent_dir(agent))
    except Exception:
        # NO fallback layout, deliberately. `agent_dir` is the single place
        # `AGENTS_PARENT_DIR` is tracked (CLAUDE.md), so re-deriving
        # `<root>/agents/<agent>` here would be a second, silently-drifting copy
        # of it — and worse, it fabricates a path that cannot exist, so
        # `.exists()` is False and the function falls through to a confident
        # `reducer` it never verified. That admits a WORKER into the
        # reducer-only path and stamps a marker suppressing the retry forever —
        # the exact miscount this predicate exists to prevent, and the opposite
        # of the "recoverable beats permanent" policy argued at the refusal site
        # below. A failed resolver means the role is genuinely UNKNOWN; say so.
        return "unknown"
    try:
        if (adir / "sessions" / sid / "working-memory.yaml").exists():
            return "worker"
    except OSError:
        return "unknown"
    return "reducer"


# ───────────────────────────── pure planning ─────────────────────────────

def aspiration_of(goal_id: str) -> str | None:
    """asp-NNN implied by a g-NNN-MM goal id, or None when the id is malformed."""
    m = _GOAL_ID_RE.match(goal_id or "")
    return f"asp-{m.group(1)}" if m else None


def decide(goal_ids, records: dict) -> dict:
    """Partition candidate goal ids into actionable and skipped.

    Pure: `records` maps goal_id -> the goal dict already read from the store.
    A goal is actionable iff its record exists and carries no marker. Order is
    preserved and duplicates collapse, so a caller handing the same id twice
    (a re-merge, a hand replay) plans it once.
    """
    plan, skipped, seen = [], [], set()
    for gid in goal_ids or []:
        if not gid or gid in seen:
            continue
        seen.add(gid)
        asp = aspiration_of(gid)
        if asp is None:
            skipped.append({"goal_id": gid, "reason": SKIP_BAD_ID})
            continue
        rec = records.get(gid)
        if not isinstance(rec, dict):
            skipped.append({"goal_id": gid, "reason": SKIP_NO_RECORD})
            continue
        if str(rec.get(MARKER_FIELD) or "").strip():
            skipped.append({"goal_id": gid, "reason": SKIP_ALREADY,
                            "marker": rec.get(MARKER_FIELD)})
            continue
        plan.append({
            "goal_id": gid,
            "aspiration_id": rec.get("aspiration_id") or asp,
            "source": rec.get("_src") or "world",
            "category": rec.get("category") or "uncategorized",
            "work_class": rec.get("work_class") or "",
            "title": rec.get("title") or gid,
            "completed_by": rec.get("completed_by") or "",
            "has_outcome_note": bool(str(rec.get("outcome_note") or "").strip()),
        })
    return {"plan": plan, "skipped": skipped}


def index_captures(doc) -> dict:
    """`exp_capture` slot value -> {goal_id: [entries]}, order preserved.

    Pure. Rows without a usable `goal_id` are dropped rather than bucketed under
    a placeholder: the id is what joins a capture to the goal being retrospected,
    and an unjoinable narrative encoded against the wrong goal is worse than one
    left in the slot for a human to look at. A non-list value yields {} — the
    slot is absent on any Body that never captured.
    """
    idx: dict = {}
    if not isinstance(doc, list):
        return idx
    for entry in doc:
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("goal_id") or "").strip()
        if gid:
            idx.setdefault(gid, []).append(entry)
    return idx


def exp_summary(entries, item) -> str:
    """One-line record summary from the first capture that carries narrative."""
    for entry in entries or []:
        for field in EXP_NARRATIVE_FIELDS:
            text = " ".join(str(entry.get(field) or "").split())
            if text:
                return text[:300]
    # No narrative anywhere: fall back to the goal's own title rather than
    # writing an empty summary the endpoint would only warn about.
    return f"Worker-executed goal {item['goal_id']}: {item['title']}"[:300]


def exp_anchor_objects(entries, losses=None) -> list:
    """Capture anchors -> the `{key, content}` objects the record schema requires.

    `exp_capture` writes `verbatim_anchors` as bare STRINGS (worker-loop Phase
    3.6), while `_validate_record` rejects any anchor that is not a dict with
    both `key` and `content`. That mismatch is the rb-245 class — two stores
    agreeing on a field NAME and disagreeing on its SHAPE — so the transform is
    explicit here rather than left to the writer.

    `losses`, when a dict is passed, is filled with the per-path drop counts
    (g-306-299 outcome 2). The transform NORMALISES rather than rejects, so a
    dropped anchor is invisible today: experience-add refuses a malformed anchor
    list at rc=0, meaning a silent drop produces no error anywhere. A count is
    what makes the conformance claim falsifiable instead of assumed.

    MEASURED 2026-08-24 (alpha worker, cc-08) over a real populated slot — 20
    entries / 173 anchors, ALL bare strings, 173 in -> 173 out, every path zero.
    Those zeros are vacuous for two of the paths and must not be read as
    coverage: there were 0 dict anchors, so `dict_missing_field` could not fire,
    and the largest group held 21 against a cap of 25, so `truncated` could not
    either. Each path was therefore positive-controlled separately with
    synthetic input (rb-245 / guard-1715: never accept a zero over a population
    that cannot exercise the path).

    Of the three paths, only TWO are genuinely silent. `truncated` already
    self-reports in-band — it appends an `anchors-truncated` marker naming the
    dropped count and the total — so the caller escalates on the other two only.
    """
    if losses is not None:
        losses.setdefault("dict_missing_field", 0)
        losses.setdefault("empty_content", 0)
        losses.setdefault("duplicate_content", 0)
        losses.setdefault("truncated", 0)
        losses.setdefault("input_anchors", 0)

    def _bump(name):
        if losses is not None:
            losses[name] += 1

    seen, objs = set(), []
    for entry in entries or []:
        for anchor in entry.get("verbatim_anchors") or []:
            _bump("input_anchors")
            if isinstance(anchor, dict):
                # Already the record shape (a future capture writer, or a
                # hand-authored entry) — take it as-is when it is well-formed.
                key, content = anchor.get("key"), anchor.get("content")
                if key is None or content is None:
                    _bump("dict_missing_field")
                    continue
                key, content = str(key), str(content).strip()
            else:
                key, content = "", str(anchor).strip()
            # Split from the original single `not content or content in seen`
            # so the two paths are counted apart: an EMPTY anchor is a malformed
            # input worth escalating, a DUPLICATE is the dedup working. Same
            # control flow, same output.
            if not content:
                _bump("empty_content")
                continue
            if content in seen:
                _bump("duplicate_content")
                continue
            seen.add(content)
            objs.append({"key": key or f"anchor-{len(objs) + 1:02d}",
                         "content": content})
    if len(objs) > ANCHOR_RECORD_MAX:
        dropped = len(objs) - ANCHOR_RECORD_MAX
        if losses is not None:
            losses["truncated"] = dropped
        objs = objs[:ANCHOR_RECORD_MAX] + [{
            "key": "anchors-truncated",
            "content": (f"{dropped} further anchor(s) omitted from this record; "
                        f"all {dropped + ANCHOR_RECORD_MAX} are in the trace at "
                        f"content_path"),
        }]
    return objs


def render_trace(item, entries, agent, now_iso) -> str:
    """Capture entries -> the experience .md body.

    Mechanical formatting of what the worker already wrote — NOT a
    reconstruction. Every field the slot carries is rendered; nothing is
    inferred, scored, or summarised into a judgement the reducer did not make.
    """
    out = [
        f"# {item['goal_id']} — {item['title']}",
        "",
        f"- **Aspiration**: {item['aspiration_id']}",
        f"- **Category**: {item['category']}",
        f"- **Encoded by**: `{MARKER_SOURCE}` (reducer-side) from the "
        f"`{EXP_SLOT}` working-memory slot",
        f"- **Reducer agent**: {agent}",
        f"- **Encoded at**: {now_iso}",
        f"- **Capture entries**: {len(entries)}",
        "",
        "> This goal was executed by a WORKER Body, which structurally skips the",
        "> reducer-only encoding phases. The narrative below is the worker's own",
        "> Phase 3.6 capture, carried here by generalize-down and encoded",
        "> verbatim. It is first-hand, not reconstructed from the goal record.",
        "",
    ]
    multi = len(entries) > 1
    for n, entry in enumerate(entries, start=1):
        if multi:
            out += [f"## Capture {n} of {len(entries)}", ""]
        for field in EXP_NARRATIVE_FIELDS:
            text = str(entry.get(field) or "").strip()
            if text:
                out += [f"### {field.replace('_', ' ').title()}", "", text, ""]
        for field in ("what_worked", "what_failed"):
            text = str(entry.get(field) or "").strip()
            if text:
                out += [f"### {field.replace('_', ' ').title()}", "", text, ""]
        meta = [f"- **{k.replace('_', ' ')}**: {entry[k]}"
                for k in ("outcome_class", "surprise_level", "_item_ts")
                if entry.get(k) is not None]
        if meta:
            out += ["### Capture metadata", ""] + meta + [""]
        decisions = [d for d in (entry.get("key_decisions") or []) if str(d).strip()]
        if decisions:
            out += ["### Key decisions", ""]
            out += [f"{i}. {str(d).strip()}" for i, d in enumerate(decisions, start=1)]
            out += [""]
        anchors = [str(a).strip() for a in (entry.get("verbatim_anchors") or [])
                   if str(a).strip()]
        if anchors:
            out += ["### Verbatim anchors", ""]
            out += [f"- `{a}`" for a in anchors]
            out += [""]
    return "\n".join(out).rstrip("\n") + "\n"


# ───────────────────────────── store access ──────────────────────────────

def _run(argv, timeout=90, stdin=None):
    # `stdin` carries a JSON record body for the two lanes whose callee takes one
    # that way: the experience lane (`experience-archive-goal.sh` reads its extra
    # record fields — verbatim_anchors, type — as optional stdin JSON, there being
    # no CLI flag for them) and the encoding lane (`wm-append.sh <slot>` takes the
    # item to append as its stdin body). Passing None keeps every other caller
    # byte-identical — subprocess.run's default — and passing a string gives the
    # callee a pipe that reaches EOF immediately, which matters because
    # experience-archive-goal.sh bounds a non-EOF stdin with a 10s timeout
    # (guard-664).
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           input=stdin)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return -1, "", str(e)


def _decode_first(raw: str, anchor: str):
    i = raw.find(anchor)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(raw[i:])[0]
    except (ValueError, json.JSONDecodeError):
        return None


def load_records(goal_ids, project_root: Path | None = None) -> dict:
    """Read each goal's record, trying the world queue then the agent queue.

    One read per distinct ASPIRATION, not per goal — `merged_goal_ids` from a
    single merge clusters heavily by aspiration. `_src` is stamped on each
    record so the marker write later targets the queue the goal actually lives
    in; guessing `world` there would silently no-op on agent-queue goals.
    """
    root = project_root or Path(__file__).resolve().parent.parent.parent
    wanted, by_asp = set(), {}
    for gid in goal_ids or []:
        asp = aspiration_of(gid)
        if asp:
            wanted.add(gid)
            by_asp.setdefault(asp, set()).add(gid)
    out: dict = {}
    for asp, gids in by_asp.items():
        for src in ("world", "agent"):
            if all(g in out for g in gids):
                break
            rc, stdout, _ = _run(bash_cmd(
                str(root / "core" / "scripts" / "aspirations-read.sh"),
                "--source", src, "--id", asp))
            if rc != 0:
                continue
            doc = _decode_first(stdout, "{")
            if not isinstance(doc, dict):
                continue
            for g in doc.get("goals") or []:
                gid = g.get("id") or g.get("goal_id")
                if gid in wanted and gid not in out:
                    g["_src"] = src
                    g.setdefault("aspiration_id", asp)
                    out[gid] = g
    return out


def load_exp_captures(root: Path) -> dict:
    """Read the merged `exp_capture` slot -> {goal_id: [entries]}.

    Read through `wm-read.sh`, never off disk: the slot is daemon-owned, and the
    path itself is role-dependent (`BODY_WM_PATH` redirects a forked Body's WM),
    so resolving it here would be a second copy of a rule that already has one.
    An unreadable slot yields the `UNREADABLE` sentinel (g-306-348), NOT {}. The
    lane then reports BLIND rather than SKIP and the marker is withheld, so the
    goal stays eligible for a later retrospective instead of being recorded as
    done. This docstring previously claimed the {} return "cannot stamp a marker
    on unencoded work"; that was false and is corrected here — see
    `load_enc_captures` for the measurement.
    """
    return _load_capture_slot(root, EXP_SLOT)


def load_enc_captures(root: Path) -> dict:
    """Read the merged `encoding_capture` slot -> {goal_id: [entries]}.

    Same contract as `load_exp_captures` — an unreadable slot yields the
    `UNREADABLE` sentinel, so the lane reports BLIND and the marker is withheld,
    while the DRIVER still completes (the loop is never blocked on a store fault).

    FIXED 2026-08-22 (g-306-348). The hazard this docstring used to describe, kept
    because the reasoning is what makes the fix legible: an unreadable slot
    returned {} — byte-identical to a genuinely empty slot — so the lane reported
    SKIP, and a SKIP does NOT withhold the marker. `wrote` counts the lanes that
    LANDED, so the other lanes succeeding was enough to fire `_write_marker`, and
    a marked goal is never retrospected again. A transient store fault therefore
    orphaned that goal's captures PERMANENTLY. Verified 2026-08-21: with the
    loader returning {} and the other lanes stubbed to succeed, `wrote=4` and the
    marker fired.

    Note the correction recorded on g-306-348: `_lane_encoding` returning rc=-1 on
    an all-malformed batch never protected against this from the inside either,
    because `wrote` is summed across ALL lanes and the three mechanizable ones run
    first. There was one defect, at the marker decision — not a good lane plus a
    leaky loader. The fix is therefore at the marker decision (`retrospect`), with
    the sentinel only supplying the information it needs.
    """
    return _load_capture_slot(root, ENC_SLOT)


class _UnreadableSlot:
    """Sentinel: the capture slot's READ FAILED — distinct from "slot is empty".

    Deliberately NOT a dict and deliberately truthy. Both choices are defensive:
    a caller that forgets to check for this sentinel and does `(captures or {})`
    would silently degrade it to "empty" — which is the exact bug this type
    exists to remove — so it must not be falsy and must not quack like a mapping.
    The only sanctioned consumer is `_capture_lane_input` below.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "<capture-slot-unreadable>"


#: Returned by `_load_capture_slot` when the slot could not be READ.
UNREADABLE = _UnreadableSlot()


def _load_capture_slot(root: Path, slot: str):
    """Shared body for the two capture loaders above (two live call sites).

    Read through `wm-read.sh`, never off disk: the slot is daemon-owned, and the
    path itself is role-dependent (`BODY_WM_PATH` redirects a forked Body's WM),
    so resolving it here would be a second copy of a rule that already has one.

    Returns `{goal_id: [entries]}` on a successful read — possibly EMPTY, which
    means "the slot really holds nothing" — or the `UNREADABLE` sentinel when the
    read itself failed. Those two were the same value ({}) until g-306-348, and
    collapsing them is what let a transient store fault stamp the marker on
    unencoded work and orphan a Body's captures permanently.
    """
    rc, stdout, _err = _run(bash_cmd(
        str(root / "core" / "scripts" / "wm-read.sh"), slot, "--json"))
    if rc != 0:
        return UNREADABLE
    return index_captures(_decode_first(stdout, "["))


def _capture_lane_input(captures, goal_id):
    """-> (entries, unreadable). The ONE place the UNREADABLE sentinel is decoded.

    Three inputs, three meanings, and the middle one is the whole point:
      * `None`       -> the caller supplied no captures at all (the default that
                        keeps every pre-existing caller working)  -> SKIP
      * `UNREADABLE` -> the slot read FAILED; we do not know what was in it -> the
                        lane is BLIND, which must withhold the marker
      * a dict       -> a real read; an absent goal_id genuinely means "nothing
                        was captured for this goal"                 -> SKIP
    """
    if captures is UNREADABLE:
        return [], True
    return ((captures or {}).get(goal_id) or []), False


def _slot_goal_ids(captures) -> list:
    """Goal ids a capture slot covers, for the run summary. UNREADABLE -> [].

    The empty list is NOT the whole story and must never be reported alone — an
    unreadable slot and an empty one both render as [] here, which is precisely
    the conflation g-306-348 removed from the lane logic. Callers pair this with
    `summary["unreadable_capture_slots"]`, which names the slots that were blind.
    """
    if captures is UNREADABLE:
        return []
    return sorted(captures or {})


def _unreadable_slots(captures, enc_captures) -> list:
    """Names of the capture slots whose READ failed this run, for the summary.

    Empty is the healthy case. A non-empty list means the run was partly BLIND,
    and every goal in it had its marker withheld — so the run is a RETRY
    CANDIDATE rather than a completed pass, however clean the counts look.
    """
    blind = []
    if captures is UNREADABLE:
        blind.append(EXP_SLOT)
    if enc_captures is UNREADABLE:
        blind.append(ENC_SLOT)
    return blind


def drain_consumed_captures(root: Path, drained_goal_ids, slots=None) -> dict:
    """Remove capture entries for goals whose retrospective MARKER was stamped.

    g-115-7366. `exp_capture` and `encoding_capture` had readers and no drain:
    grepping every clear site in core/scripts and .claude/skills finds two for
    `spark_capture` (the positive control, so the grep works) and ZERO for the
    other three lanes. WITHOUT A CLEAR SITE THE CAP DECIDES THE SYMPTOM, NOT
    WHETHER THERE IS ONE: a CAPPED lane parks at cap and evicts its oldest entry
    on every append (silent, unrecoverable — the slot is that entry's only copy),
    while an UNCAPPED one grows without bound. Measured on this Body 2026-08-24,
    all four at once: spark_capture 50/50, exp_capture 20/20, hyp_capture 10/10
    — three lanes sitting exactly ON their caps — and `encoding_capture`, which
    has no cap, at 931 entries across 481 distinct goals. Do not read the 931 as
    an eviction figure; it is the growth arm of the same root cause.

    THE PREDICATE IS THE EXISTING MARKER, NOT A NEW JUDGEMENT. `retrospect`
    stamps `marked` only when at least one lane landed AND no capture lane was
    BLIND or LOSSY — the invariant that already licenses suppressing the retry
    FOREVER. Anything safe to never retry is safe to drop the captures of, so
    reusing it adds no new way to be wrong. Every goal whose marker was withheld
    keeps its entries and stays a retry candidate.

    WHOLE-SLOT CLEAR WOULD BE WRONG HERE, and that is the one thing to carry
    forward if this is ever rewritten. `spark_capture`'s consumer replays EVERY
    entry, so its blanket `wm-clear.sh` is safe. This consumer processes only
    `plan` — `merged_goal_ids` capped at `--max` (25) — while the slot
    accumulates across every Body and every merge (481 goals against a 25-goal
    batch, measured). A blanket clear would destroy the other 456, and
    `capture_fast_lane`'s consumed-hash watermark (g-306-311) would make that
    loss PERMANENT by suppressing re-delivery. The watermark makes a clear safe
    from DUPLICATES; it does nothing for entries that were never processed.

    THE FILTER RUNS IN THE HANDLER, NOT HERE (guard-3881). An earlier draft of
    this function read the slot, filtered in this process, and POSTed the
    survivors to `wm-set.sh` — a full-slot overwrite of a stale snapshot, which
    destroys any entry appended in between. Holding the WM lock would not have
    fixed it: `wm_write.set_slot`'s loop_state CAS exists precisely because "a
    >10s stall lets a peer stale-break this lock and write". So the subtraction
    moved server-side to `wm-drain-goals.sh` -> POST /v1/wm/drain-goals, which
    re-reads and re-applies the predicate INSIDE the lock. What this function
    sends is the goal-id SET, never a surviving list.

    FAIL-CLOSED TOWARD KEEPING, on every branch: an empty marked-set, a
    non-zero rc, or an unparseable verdict all drain NOTHING. The handler keeps
    non-dict entries and entries carrying no `goal_id` for the same reason — an
    entry that cannot be classified must not be destroyed by the classifier.
    """
    if slots is None:
        slots = (EXP_SLOT, ENC_SLOT)
    ids = sorted({g for g in (drained_goal_ids or ()) if isinstance(g, str) and g})
    out = {}
    for slot in slots:
        rec = {"removed": 0, "kept": 0, "wrote": False, "error": ""}
        out[slot] = rec
        if not ids:
            rec["error"] = "no-marked-goals"
            continue
        rc, stdout, err = _run(
            bash_cmd(str(root / "core" / "scripts" / "wm-drain-goals.sh"), slot),
            stdin=json.dumps(ids))
        if rc != 0:
            rec["error"] = (err or stdout or "").strip()[-200:] or "drain-failed"
            continue
        verdict = _decode_first(stdout, "{")
        if not isinstance(verdict, dict):
            # Never infer "drained nothing" from a shape we did not expect, and
            # never infer success from rc=0 alone (guard-2298).
            rec["error"] = "unparseable-verdict"
            continue
        rec["removed"] = int(verdict.get("removed") or 0)
        rec["kept"] = int(verdict.get("kept") or 0)
        rec["wrote"] = rec["removed"] > 0
    return out


# ─────────────────────────────── the lanes ───────────────────────────────

def _lane_team_state(item, agent, now_iso, root):
    # Do NOT hand-escape backslashes/quotes here: `json.dumps` below already
    # escapes them, so a manual pass double-escapes and a title containing " or
    # \ round-trips with literal \" and \\ baked into the stored value. The
    # newline collapse is kept deliberately — it is cosmetic (recent_completions
    # renders on one line), not an escaping concern. json.dumps defaults to
    # ensure_ascii=True, which guard-662 requires here because a goal title is
    # arbitrary inbound content and cp1252 bytes would otherwise break the pipe.
    finding = f"[{MARKER_SOURCE}] {item['title']}"[:400].replace("\n", " ")
    value = json.dumps({
        "goal_id": item["goal_id"],
        "completed_by": item["completed_by"] or agent,
        "completed_at": now_iso,
        "key_finding": finding,
        "via": MARKER_SOURCE,
    })
    return _run(bash_cmd(str(root / "core" / "scripts" / "team-state-update.sh"),
                         "--field", "recent_completions",
                         "--operation", "append", "--value", value))


def _lane_journal(item, agent, now_iso, root):
    summary = (f"[{MARKER_SOURCE}] reducer-side retrospective for a goal closed "
               f"by a worker Body — {item['title']}")
    argv = [str(root / "core" / "scripts" / "journal-append.sh"),
            "--goal", item["goal_id"], "--outcome-class", "deep",
            "--summary", summary]
    if item["work_class"]:
        argv += ["--work-class", item["work_class"]]
    return _run(bash_cmd(*argv))


def _lane_findings(item, agent, now_iso, root):
    # --scan-outcome-note (): this lane passes NO --insight-file (a
    # worker Body has no LLM-assembled tree-insight temp file here), so before
    # this flag existed the call died rc=2 at argparse ("--insight-file is
    # required") and scanned NOTHING — the worker-side idea-capture path was a
    # silent no-op. --scan-outcome-note makes --insight-file optional and points
    # the gate at the goal's durable outcome_note, which is exactly where a
    # worker's end-of-goal RECOMMENDATION/follow-up lives. Reducer-side closes
    # get the same scan mechanically from iteration-close.sh do_state_update.
    return _run(bash_cmd(str(root / "core" / "scripts" / "findings-gate.sh"),
                         "--goal", item["goal_id"],
                         "--aspiration", item["aspiration_id"],
                         "--category", item["category"],
                         "--source", item["source"],
                         "--scan-outcome-note"))


def _lane_impk(item, agent, now_iso, root, artifacts_count):
    # artifacts_count is THIS run's successful lane writes — the only quantity
    # the retrospective actually witnessed. Passing it is what makes the close
    # MEASURED for cmd_velocity; omitting every quality flag would make it skip
    # the snapshot entirely (), which is correct behaviour there and
    # would leave outcome 3 with nothing to attribute.
    return _run(bash_cmd(str(root / "core" / "scripts" / "state-update-audit.sh"),
                         "velocity", "--goal", item["goal_id"],
                         "--category", item["category"],
                         "--outcome-class", "deep",
                         "--artifacts-count", str(artifacts_count)))


def _lane_experience(item, agent, now_iso, root, entries):
    """Encode the worker's captures as an experience .md + record.

    Called ONLY when `entries` is non-empty — the applicability test lives in
    `retrospect` so that "no capture" is a SKIP rather than a lane failure, and
    so this function keeps the plain `(rc, stdout, stderr)` shape every other
    lane has.

    The trace is written to a system temp file, not into the repo: the endpoint
    COPIES it to the canonical `agents/<agent>/experience/<id>.md` and unlinks
    the source once the record lands, so a repo-side staging file would be churn
    in a synced tree at best and an orphan on any failure path at worst. The
    endpoint also uniquifies the id, so a second pass over the same goal cannot
    409 — though `decide`'s marker check means there should never be one.
    """
    trace_path = None
    try:
        fd, trace_path = tempfile.mkstemp(
            prefix=f"exp-{item['goal_id']}-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_trace(item, entries, agent, now_iso))
        # verbatim_anchors and type have no CLI flag on the wrapper; they ride
        # the optional stdin JSON, which the endpoint merges as `extra`.
        losses = {}
        payload = json.dumps({"type": EXP_TYPE,
                              "verbatim_anchors": exp_anchor_objects(entries, losses)})
        rc, out, err = _run(
            bash_cmd(str(root / "core" / "scripts" / "experience-archive-goal.sh"),
                     "--goal", item["goal_id"],
                     "--skill-slug", EXP_SKILL_SLUG,
                     "--category", item["category"],
                     "--summary", exp_summary(entries, item),
                     "--trace-file", trace_path),
            stdin=payload)
        #  outcome 2. Escalate ONLY the two paths that are otherwise
        # invisible: dedup is the feature working, and truncation already
        # self-reports via its in-band `anchors-truncated` marker. Reporting
        # those two would be noise and would bury the ones that matter.
        malformed = losses["dict_missing_field"] + losses["empty_content"]
        if malformed:
            err = (err or "") + (
                f"\n[exp-anchors] {malformed} of {losses['input_anchors']} anchor(s) "
                f"DROPPED SILENTLY before the record for {item['goal_id']}: "
                f"dict-missing-key-or-content={losses['dict_missing_field']}, "
                f"empty-content={losses['empty_content']}. "
                f"(deduped={losses['duplicate_content']}, "
                f"truncated={losses['truncated']} — both by design.)")
        return rc, out, err
    except OSError as e:
        return -1, "", f"experience lane could not stage a trace file: {e}"
    finally:
        # The endpoint deletes the source after the record lands, so on the
        # success path there is nothing here; this clears the FAILURE paths.
        if trace_path:
            try:
                os.unlink(trace_path)
            except OSError:
                pass


def enc_prose(entry) -> "tuple[str, str | None]":
    """Return `(prose, key_it_came_from)` for one `encoding_capture` entry.

    The key is returned, not just the text, because the caller has to REPORT
    which fallback each entry used -- that reporting is the half of guard-4044
    that turns schema drift into a number instead of silence. `("", None)`
    means no key in `ENC_PROSE_KEYS` carried anything.
    """
    if not isinstance(entry, dict):
        return "", None
    for key in ENC_PROSE_KEYS:
        value = str(entry.get(key) or "").strip()
        if value:
            return value, key
    return "", None


def enc_observation(entry) -> str:
    """Render one `encoding_capture` entry as an encoding_queue observation.

    Read DEFENSIVELY: capture slots carry no enforced schema (guard-4044), and
    the sibling spark bridge broke on exactly this by assuming a field was
    present. The prose is taken from the first `ENC_PROSE_KEYS` hit rather than
    from `fact` alone -- `fact` is the CONVENTIONAL key, never the only one
    written -- and an entry carrying prose under none of them yields "" and is
    dropped by the caller rather than queued as an empty node request.

    Do NOT "fix" a future drop by tightening the WRITER instead (guard-4044's
    own action_hint): the slot is append-only from many Bodies, so a
    producer-side schema would drop entries at WRITE time, and a dropped write
    is unrecoverable where a skipped read is not.

    `evidence` is carried verbatim and is the field that earns this lane its
    keep: the reducer writes the node later and cannot re-measure what it never
    observed, so a queued fact with no traceable measurement is precisely the
    drift these captures exist to prevent.
    """
    fact, _key = enc_prose(entry)
    if not fact:
        return ""
    parts = [fact]
    evidence = str(entry.get("evidence") or "").strip()
    if evidence:
        parts.append(f"Evidence: {evidence}")
    supersedes = str(entry.get("supersedes") or "").strip()
    if supersedes and supersedes.lower() != "null":
        # Surfaced FIRST-CLASS, not buried: a fact that CORRECTS an encoded
        # belief is the highest-value thing a worker hands up, and it is exactly
        # what a free-text spark buries.
        parts.append(f"SUPERSEDES: {supersedes}")
    return " | ".join(parts)


def _lane_encoding(item, agent, now_iso, root, entries):
    """Hand worker-captured world-facts to `encoding_queue` for consolidation.

    Called ONLY when `entries` is non-empty — the applicability test lives in
    `retrospect`, so "no capture" is a SKIP rather than a lane failure and this
    function keeps the plain `(rc, stdout, stderr)` shape every other lane has.

    This lane does NOT write tree nodes. `suggested_node` rides along as
    `target_article` but is explicitly NON-binding; consolidation decides
    placement. See the ENC_* constants block for why.

    The WM write goes through `bash_cmd(wm-append.sh)` like every other
    subprocess in this file, NOT through `wm.py` directly. Two reasons, and the
    first one is the load-bearing one: `wm-append.sh` is DAEMON-ONLY since the
    2026-05-29 cutover, and `wm_write.py::append_slot` — not `wm.py::cmd_append`
    — is the live write path (guard-742). Under `own-cloud` with a daemon
    serving the fleet, a direct CLI append writes through a different backend
    resolution than every other writer on the box, which is the split-brain
    class `.claude/rules/no-python-cli-fallback.md` exists to prevent. Second,
    `bash_cmd` is what makes a bash wrapper safe to call from a Python
    subprocess at all (guard-580/581: resolved BASH, `as_posix()` script path),
    so the Windows-path hazard that might argue for the direct call is already
    handled here.
    """
    # Intra-batch dedup on the OBSERVATION text — the thing that would become a
    # node — so one fact captured twice cannot become two nodes. This is the
    # only one of the three duplication paths that was open:
    #   1. same goal retrospected twice  -> closed by the marker (a marked goal
    #      is never re-planned; `decide` skips it).
    #   2. duplicate entries in the SLOT -> closed upstream: `encoding_capture`
    #      is in wm.ARRAY_SLOTS, which is what body-merge's `_dedup_append`
    #      keys off, so identical captures collapse at generalize-down.
    #   3. duplicate facts in ONE batch  -> open until here. `encoding_queue` is
    #      deliberately NOT in ARRAY_SLOTS (verified), so nothing downstream
    #      would have collapsed them.
    # Keyed on observation ONLY, not on the whole payload: `suggested_node` is a
    # non-binding hint, so two captures of the same fact differing only in their
    # hint are still ONE node and must not both queue.
    queued = 0
    seen_observations = set()
    fell_through = {}   # fallback key -> count, for keys after the primary
    no_prose_key = 0    # entries carrying prose under NO key in the chain
    for entry in entries:
        observation = enc_observation(entry)
        if not observation:
            # guard-4044's REPORT half: a shape mismatch becomes a NUMBER in the
            # lane result, never a silent `continue`.
            no_prose_key += 1
            continue
        _prose, prose_key = enc_prose(entry)
        if prose_key != ENC_PROSE_KEYS[0]:
            fell_through[prose_key] = fell_through.get(prose_key, 0) + 1
        if observation in seen_observations:
            continue
        seen_observations.add(observation)
        suggested = entry.get("suggested_node") if isinstance(entry, dict) else None
        if isinstance(suggested, str) and suggested.strip().lower() in ("", "null"):
            suggested = None
        payload = json.dumps({
            "source_goal": item["goal_id"],
            "observation": observation,
            "target_article": suggested,
            "replay_priority": ENC_REPLAY_PRIORITY,
            "captured_by": agent,
            "captured_at": now_iso,
        })
        rc, _out, err = _run(
            bash_cmd(str(root / "core" / "scripts" / "wm-append.sh"),
                     ENC_QUEUE_SLOT),
            stdin=payload)
        if rc != 0:
            # RECORD WHAT LANDED (). The batch is appended one entry at
            # a time, so a failure here leaves `queued` entries in the slot and
            # the rest nowhere. Naming the count in the error is what makes the
            # partial state legible in the lane result — `retrospect` withholds
            # the marker on this, and whoever reads the retry needs to know that
            # the first `queued` will arrive a SECOND time (encoding_queue is not
            # in wm.ARRAY_SLOTS, so nothing collapses them across invocations).
            detail = (err or "").strip() or "encoding_queue append failed"
            return rc, "", (
                f"{detail} — after {queued} of {len(entries)} entr"
                f"{'y' if len(entries) == 1 else 'ies'} queued; a retry re-queues "
                f"those {queued}")
        queued += 1
    if queued == 0:
        # Entries existed but none carried usable prose. That is a lane that ran
        # and found nothing to queue, NOT a success — reporting rc=0 here would
        # let an all-malformed batch count toward `wrote` and stamp the marker,
        # suppressing the retry forever on work that was never encoded.
        return -1, "", (
            f"no encoding_capture entry carried usable prose under any of the "
            f"{len(ENC_PROSE_KEYS)} known keys "
            f"({no_prose_key} of {len(entries)} carried none)")
    summary = f"queued {queued}"
    if fell_through:
        detail = " ".join(f"{k}={fell_through[k]}" for k in sorted(fell_through))
        summary += f"; recovered via fallback key: {detail}"
    if no_prose_key:
        summary += f"; {no_prose_key} carried no prose key"
    return 0, summary, ""


def _write_marker(item, agent, now_iso, root):
    marker = f"{now_iso}|{agent}|{MARKER_SOURCE}"
    return _run(bash_cmd(str(root / "core" / "scripts" / "aspirations-update-goal.sh"),
                         "--source", item["source"], item["goal_id"],
                         MARKER_FIELD, marker))


def retrospect(item, agent, now_iso, root, captures=None,
               enc_captures=None) -> dict:
    """Run the mechanizable lanes for one goal, then mark it.

    The marker is written only when at least one lane landed: marking a goal
    whose every lane failed would record work that did not happen and suppress
    the retry forever, which is the failure mode the marker exists to prevent
    the opposite of.

    `captures` is {goal_id: [exp_capture entries]} from `load_exp_captures`.
    Defaulting it to None keeps every existing caller working and makes the
    experience lane SKIP — the correct behaviour when no capture reached this
    reducer, and the reason a skip is not counted as a write below.
    """
    lanes = {}
    for name, fn in (("team_state", _lane_team_state),
                     ("journal", _lane_journal),
                     ("findings", _lane_findings)):
        rc, _out, err = fn(item, agent, now_iso, root)
        lanes[name] = {"rc": rc, "ok": rc == 0,
                       "err": (err or "").strip()[-200:] if rc != 0 else ""}

    # Experience lane. Its input may legitimately be absent, which no other lane
    # can be — see the module docstring. A skip reports ok=False so it never
    # inflates `wrote`, and carries `skipped` so a reader can tell "there was
    # nothing to encode" from "encoding failed".
    entries, exp_blind = _capture_lane_input(captures, item["goal_id"])
    if exp_blind:
        lanes["experience"] = {"rc": None, "ok": False, "entries": 0,
                               "unreadable": True, "err": SLOT_UNREADABLE}
    elif entries:
        rc, _out, err = _lane_experience(item, agent, now_iso, root, entries)
        lanes["experience"] = {"rc": rc, "ok": rc == 0, "entries": len(entries),
                               "err": (err or "").strip()[-200:] if rc != 0 else ""}
    else:
        lanes["experience"] = {"rc": None, "ok": False, "entries": 0,
                               "skipped": SKIP_NO_CAPTURE, "err": ""}

    # Encoding lane (). Same optional-input contract as the experience
    # lane above, and the same reason a skip must report ok=False: it must never
    # inflate `wrote`, because `wrote > 0` is what stamps the marker that
    # suppresses the retry forever.
    enc_entries, enc_blind = _capture_lane_input(enc_captures, item["goal_id"])
    if enc_blind:
        lanes["encoding"] = {"rc": None, "ok": False, "entries": 0,
                             "unreadable": True, "err": SLOT_UNREADABLE}
    elif enc_entries:
        rc, _out, err = _lane_encoding(item, agent, now_iso, root, enc_entries)
        lanes["encoding"] = {"rc": rc, "ok": rc == 0, "entries": len(enc_entries),
                             "err": (err or "").strip()[-200:] if rc != 0 else ""}
    else:
        lanes["encoding"] = {"rc": None, "ok": False, "entries": 0,
                             "skipped": SKIP_NO_ENCODING, "err": ""}

    wrote = sum(1 for v in lanes.values() if v["ok"])
    rc, _out, err = _lane_impk(item, agent, now_iso, root, wrote)
    lanes["impk"] = {"rc": rc, "ok": rc == 0, "artifacts_count": wrote,
                     "err": (err or "").strip()[-200:] if rc != 0 else ""}

    # THE MARKER DECISION (). `wrote > 0` alone is not sufficient, and
    # that was the whole defect: the marker suppresses the retry FOREVER, so it
    # may only be stamped when nothing was lost or left unseen.
    #
    # Two ways a CAPTURE lane leaves work unrecorded, and neither shows up in
    # `wrote` because `wrote` counts what LANDED, never what was missed:
    #   * BLIND  — the slot read failed, so we cannot know what was in it. This
    #     is guard-4093's shape exactly: an aggregate with any blind lane is
    #     UNREACHABLE, not empty, and only an all-lanes-were-read result licenses
    #     the negative conclusion "there was nothing to encode".
    #   * LOSSY  — the lane HAD entries and failed. `_lane_encoding` appends one
    #     entry at a time and returns on the first failure, so a 5-entry batch
    #     failing at 3 leaves 1-2 queued and 3-5 nowhere.
    # Only the two capture lanes are consulted. team_state / journal / findings
    # write from the GOAL RECORD, which is still on disk for a later retry, so a
    # failure there loses nothing and must not withhold the marker indefinitely.
    #
    # WITHHOLDING COSTS DUPLICATES, AND THAT TRADE IS DELIBERATE. A retry re-runs
    # the whole batch, and `encoding_queue` is NOT in wm.ARRAY_SLOTS (verified
    # 2026-08-22: it sits in TOP_LEVEL_KEYS instead), so `_dedup_append` will not
    # collapse a re-queued entry and `seen_observations` is per-call. So a partial
    # failure that retries duplicates whatever already landed. That is the correct
    # direction: a duplicate queue entry is visible and removable, an orphaned
    # capture is neither. True idempotence needs a stable payload plus
    # `encoding_queue` in ARRAY_SLOTS, which drags in that slot's cap and eviction
    # semantics (guard-2552) — a separate change, not this one.
    blind_lanes = sorted(n for n, v in lanes.items() if v.get("unreadable"))
    lossy_lanes = sorted(n for n in CAPTURE_LANES
                         if not lanes[n]["ok"] and lanes[n].get("entries"))
    withheld = blind_lanes + lossy_lanes
    marked = False
    marker_err = ""
    if wrote > 0 and not withheld:
        rc, _out, err = _write_marker(item, agent, now_iso, root)
        marked = rc == 0
        marker_err = (err or "").strip()[-200:] if rc != 0 else ""
    return {
        "goal_id": item["goal_id"],
        "lanes": lanes,
        "lanes_written": wrote,
        "marked": marked,
        "marker_error": marker_err,
        "marker_withheld_for": withheld,
        "pending_judgment_lanes": list(REPORT_LANES),
    }


# ──────────────────────────────── driver ─────────────────────────────────

def _goal_ids_from(args) -> list:
    if args.goal_ids:
        return [g.strip() for g in args.goal_ids.split(",") if g.strip()]
    raw = sys.stdin.read() if args.from_merge_summary == "-" else \
        Path(args.from_merge_summary).read_text(encoding="utf-8")
    doc = _decode_first(raw, "{")
    if not isinstance(doc, dict):
        return []
    ids = doc.get("merged_goal_ids")
    return list(ids) if isinstance(ids, list) else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--goal-ids", help="comma-separated goal ids")
    src.add_argument("--from-merge-summary",
                     help="path to a generalize-down summary JSON, or - for stdin")
    ap.add_argument("--apply", action="store_true",
                    help="run the lanes; without it the plan is reported only")
    ap.add_argument("--max", type=int, default=25,
                    help="cap on goals processed in one run")
    ap.add_argument("--output", default="json", choices=["json", "text"])
    args = ap.parse_args(argv)

    if not args.agent:
        print(json.dumps({"error": "no_agent",
                          "detail": "pass --agent or set MIND_AGENT"}))
        return 2

    root = Path(__file__).resolve().parent.parent.parent

    # REDUCER-ONLY (). This module is reducer-only by construction —
    # its one wired call site is the consolidate sub-step right after
    # `body-merge.py generalize-down`, the only moment the reducer can name
    # which goals arrived from a Body — but the CLI is invocable from any Body,
    # and the lanes it calls are not uniformly safe to run from a worker.
    #
    # `journal-append.sh` (lines ~109-116) carries a defensive BODY=worker guard
    # that logs a SKIP to stderr and then `exit 0`. `retrospect` classifies a
    # lane as landed on `rc == 0` alone, so under that guard the journal lane
    # would be counted as written when nothing was written, inflate the
    # `artifacts_count` handed to the imp@k lane, and — because the marker is
    # stamped whenever `wrote > 0`, and the other three lanes DO write from a
    # worker — permanently suppress the retry for a goal now missing a lane.
    #
    # Refusing here rather than teaching each lane to distinguish wrote-from-
    # declined is the smaller fix AND the one matching the true invariant, and
    # the choice is measured, not inherited from the goal's phrasing: of the
    # five lane writers, `journal-append.sh` is the ONLY one with a
    # skip-and-exit-0 path — `team-state-update.sh`, `findings-gate.sh`,
    # `state-update-audit.sh` and `experience-archive-goal.sh` have no
    # early-exit-0 at all. A per-lane wrote-vs-declined protocol would
    # therefore be a general mechanism built for a population of one, against
    # writers whose skip paths do not exist (implementation-discipline: no
    # speculative features, no single-use abstractions). If a SECOND writer
    # ever grows this shape, revisit — that is the evidence this fix is
    # waiting on.
    #
    # Re-measured when the experience lane landed (), because that
    # lane's arrival is exactly the "second writer" event above and the count
    # had to be re-derived rather than assumed. It is NOT that event: the lane
    # calls `experience-archive-goal.sh`, which has no worker rail. Its SIBLING
    # `experience-add.sh` does carry one (), so the rail exists on the
    # add path and not on the archive-goal path that reaches the same store —
    # an asymmetry in the defence, not in this module, and inert here because
    # the refusal below stops a worker before either can be called.
    #
    # Fail direction is deliberate and OPPOSITE to journal-append.sh's. That
    # writer fails OPEN because it fires on every iteration close fleet-wide and
    # must never be blockable. This one fires rarely and its errors are
    # asymmetric: a wrongly-refused reducer simply retries next pass (no marker
    # is written, nothing is lost), while a wrongly-admitted worker stamps a
    # marker that suppresses the retry FOREVER. Recoverable beats permanent, so
    # a positively-detected worker is refused. `unknown` proceeds — the check is
    # unevaluated, not failed (guard-2913) — but is reported, never silently
    # folded into `reducer`.
    role = body_role(args.agent)
    if role == "worker":
        print(json.dumps({
            "error": REFUSE_NOT_REDUCER,
            "body_role": role,
            "agent": args.agent,
            "detail": ("worker-retrospective is reducer-only; a worker Body would "
                       "count journal-append.sh's skip-and-exit-0 as a landed lane "
                       "and stamp a marker that suppresses the retry permanently "
                       "(g-306-252). Run it from the reducer after generalize-down."),
        }))
        return 3

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    goal_ids = _goal_ids_from(args)
    records = load_records(goal_ids, root) if goal_ids else {}
    decided = decide(goal_ids, records)
    plan = decided["plan"][:max(0, args.max)]
    deferred = decided["plan"][max(0, args.max):]

    summary = {
        "agent": args.agent,
        "body_role": role,
        "now": now_iso,
        "candidates": len(goal_ids),
        "planned": len(plan),
        "skipped": decided["skipped"],
        "over_cap_deferred": [p["goal_id"] for p in deferred],
        "applied": [],
        "run_lanes": list(RUN_LANES),
        "pending_judgment_lanes": list(REPORT_LANES),
        "mode": "apply" if args.apply else "dry-run",
    }
    if args.apply:
        # Read the slot ONCE for the whole batch, not per goal: it is a single
        # daemon round-trip and `merged_goal_ids` routinely carries several
        # goals from the same Body.
        captures = load_exp_captures(root)
        enc_captures = load_enc_captures(root)
        summary["exp_capture_goals"] = _slot_goal_ids(captures)
        summary["encoding_capture_goals"] = _slot_goal_ids(enc_captures)
        summary["unreadable_capture_slots"] = _unreadable_slots(captures,
                                                                enc_captures)
        for item in plan:
            summary["applied"].append(
                retrospect(item, args.agent, now_iso, root, captures,
                           enc_captures))
        # DRAIN (). Runs AFTER every lane, on the marker predicate —
        # see `drain_consumed_captures` for why the marker and why not a
        # blanket clear. Ordered last on the same crash-safety reasoning the
        # spark_capture precedent gives: a crash between the lanes and the
        # drain leaves the entries in place to be re-processed (the marker
        # makes that a no-op), whereas draining first could lose a batch whose
        # lanes then failed.
        summary["drained_goals"] = [r["goal_id"] for r in summary["applied"]
                                    if r.get("marked")]
        summary["capture_drain"] = drain_consumed_captures(
            root, summary["drained_goals"])
    else:
        summary["would_apply"] = [p["goal_id"] for p in plan]
        # Dry-run must be able to answer "will the experience lane fire?" — the
        # whole point of the plan is to be inspectable before it writes. Same
        # for the encoding lane: a dry run that cannot show it firing would make
        # the lane's activation unverifiable before it writes to the queue.
        captures = load_exp_captures(root)
        enc_captures = load_enc_captures(root)
        summary["exp_capture_goals"] = _slot_goal_ids(captures)
        summary["encoding_capture_goals"] = _slot_goal_ids(enc_captures)
        summary["unreadable_capture_slots"] = _unreadable_slots(captures,
                                                                enc_captures)
        # An unreadable slot answers "will this lane fire?" with "unknown", not
        # "no" — `_capture_lane_input` is the one decoder, so the dry run reports
        # the same blindness the apply path would act on rather than a confident
        # empty list ().
        summary["would_encode_experience"] = [
            p["goal_id"] for p in plan
            if _capture_lane_input(captures, p["goal_id"])[0]]
        summary["would_encode_encoding"] = [
            p["goal_id"] for p in plan
            if _capture_lane_input(enc_captures, p["goal_id"])[0]]

    if args.output == "json":
        print(json.dumps(summary))
    else:
        print(f"worker-retrospective {args.agent}: {len(plan)} planned, "
              f"{len(summary['applied'])} applied, "
              f"{len(decided['skipped'])} skipped ({summary['mode']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
