"""Origin-signal gate logic — daemon-safe extraction (PR 7a/2).

Rejects make-work at goal-filing time. User-sourced goals (world queue,
no agent context) skip. Agent-sourced goals MUST cite a concrete signal
via `origin_signal`. Missing or invalid blocks unless override applied
or Layer-D auto-derive matches the title prefix.

Public API:
    evaluate(payload, *, override_signal, agent_name, world_dir) -> dict

Payload shape — single goal:
    {"title": "...", "origin_signal": "...", "source": "world"|"agent"}

Payload shape — batch:
    {"batch": [{"id": "g-X", "title": "...", "origin_signal": "..."}],
     "source": "world"|"agent"}

Return shape — single:
    {"would_block": bool, "reason": "...", "origin_signal": str|None,
     "override_applied"?: str, "auto_derived"?: True}

Return shape — batch:
    {"any_blocked": bool, "results": [single-shape-without-override-flag]}

Side effects (both happen inside evaluate()):
    1. When override fires AND would have blocked: append to
       <world_dir>/origin-signal-overrides.jsonl
    2. Always: emit one _gate_log() telemetry record per call
       (batch: one record for the whole batch, single: one record).

The world-user-context escape hatch (source="world" AND no agent bound)
is preserved: such payloads return would_block=False with reason
"world-source user context" and no side effects beyond a noop telemetry.

Daemon safety:
    - Reads no env directly. agent_name and world_dir are explicit args.
    - _gate_log() uses META_DIR cached at import-time in _paths.py — the
      daemon imports _paths once at startup so META_DIR is stable.
      _gate_log is best-effort and never raises (see _gate_log.py:18).
    - _gate_log decision strings: the auto-derive branch emits decision
      "pass" with extra.decision_path="auto_derive" -- a non-blocking PASS
      that required Layer-D title-prefix derivation, and a valid
      _VALID_DECISIONS value (guard-502). Earlier this branch emitted the
      non-canonical "auto_derive", which _gate_log coerced to "fail_open"
      with _invalid_decision_received="auto_derive"; that inflated the
      gate's fail_open count (gate-retirement-eval flagged origin-signal
      "investigate" on ~1023 phantom fail-opens). Fixed 2026-05-28; see the
      reasoning-bank entry on auto-derive mislabel.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from _gate_log import log as _gate_log  # type: ignore


# CANONICAL list of allowed origin_signal values. Bare tags (no colon) are
# standalone signals; prefix tags (ending in ":") require a non-empty id
# suffix. This tuple is the SINGLE SOURCE OF TRUTH — do not duplicate the
# values anywhere else; the CLI wrapper imports from here.
ALLOWED_PREFIXES = (
    "user_directive",
    "board_post:",
    "pending_question:",
    "failing_test:",
    "resolved_hypothesis:",
    "low_confidence_node:",
    "recurring_cadence:",
    "decomposition:",
    "parent_aspiration:",
    "unblock:",
    "investigate:",
    "idea:",
    "maintain:",
    "drift_detected:",
    # cycle-detector lane — automated detection-driven filers ().
    # These were filed in production (alert sweep, routing audit, insight
    # sweep) but passed the gate only via Layer-D title auto-derive or
    # --override-all, leaving their stored origin_signal unrecognized by
    # _goal_source.infer() (goal_source=null). Registering them here lets
    # is_valid() accept them as first-class signals. Keep locked with the
    # cycle-detector branch in _goal_source.infer().
    "alert-email:",             # inbox alert sweep auto-Unblocks
    "routing-mismatch:",        # post-decompose routing audit (specific-agent)
    "routing-either-resolve:",  # post-decompose routing audit (either-case)
    "insight_trigger:",         # insight-trigger-sweep Apply/Investigate goals
    # Same cycle-detector lane, second reconciliation (). Found by
    # diffing EVERY origin_signal literal prescribed across
    # .claude/skills/*/SKILL.md + core/config/**/*.md against this tuple:
    # 2 of 50 prescribed literals named a prefix that was never registered.
    # Both are automated detection-driven filers, so they belong in this lane
    # rather than being rewritten to a sanctioned agent-self prefix — a generic
    # `unblock:`/`maintain:` would erase the discriminator each one's own
    # reader matches on. Keep locked with the cycle-detector branch in
    # _goal_source.infer(); check-origin-signal-drift.py re-runs the diff.
    "skill-discovery-audit:",   # aspirations-evolve Step 9.5.5 forged-skill audit
    "blocker_pattern:",         # aspirations-all-blocked MW#5 T1 Unblock synthesis
    "idle_fallback",
    "program-change-proposal:",
    # _goal_source.infer() parity (). These prefixes were already
    # recognized by _goal_source.py infer() as legitimate goal sources
    # (user / recurring-cycle / cycle-detector / agent-self) but had drifted
    # out of this whitelist. Goals carrying them passed source-inference yet
    # were BLOCKED here, forcing callers to --override-signal -- the override
    # rate gate-retirement-eval flagged TIGHTEN (51.6% over its window). The
    # invariant: if infer() blesses a signal as a real source, the gate must
    # accept it (else legitimate work is make-work-rejected). Keep locked with
    # _goal_source.infer(); test_goal_source_infer_parity.py pins the full set.
    "user-directed:",           # infer -> user
    "user_directed:",           # infer -> user (underscore variant)
    "recurring:",               # infer -> recurring-cycle (sibling of recurring_cadence:)
    "monitor:",                 # infer -> cycle-detector (monitor proc-NNN goals)
    "investigation:",           # infer -> agent-self (sibling of investigate:)
    "apply:",                   # infer -> agent-self (Apply decomposition goals)
    "brief:",                   # infer -> agent-self (analyst briefs)
)

_BARE_TAGS = frozenset(t for t in ALLOWED_PREFIXES if not t.endswith(":"))


# Layer-D auto-derive (). Title-prefix → derived signal kind.
# When a goal's origin_signal is missing/invalid AND title starts with one
# of these prefixes, derive the signal from the body slug instead of
# refusing. Closes rb-765 "refusal alone produces repeat-invasion attempts."
# Apply: routes through decomposition (intentionally NOT a top-level kind —
# see _prefix_registry.py "tactical_correction" comment).
_AUTO_DERIVE_MAP = (
    ("Investigate:", "investigate"),
    ("Maintain:",    "maintain"),
    ("Idea:",        "idea"),
    ("Unblock:",     "unblock"),
    ("Apply:",       "decomposition"),
)


def is_valid(signal) -> bool:
    if not signal or not isinstance(signal, str):
        return False
    s = signal.strip()
    if not s:
        return False
    if s in _BARE_TAGS:
        return True
    for pfx in ALLOWED_PREFIXES:
        if pfx.endswith(":") and s.startswith(pfx):
            suffix = s[len(pfx):].strip()
            if suffix:
                return True
    return False


def slugify(text, max_len=60) -> str:
    """Kebab-case from arbitrary text. Lowercase, alphanumeric+hyphen only,
    runs collapsed, leading/trailing hyphens stripped, capped at max_len
    chars (no trailing hyphen)."""
    if not text:
        return ""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s


def try_auto_derive(payload: dict) -> Optional[str]:
    """Layer-D: derive origin_signal from title prefix. Returns the derived
    signal when a mapping matches AND the body slug is non-empty; else None.
    """
    title = (payload.get("title") or "").strip()
    if not title:
        return None
    for prefix, kind in _AUTO_DERIVE_MAP:
        if title.startswith(prefix):
            body = title[len(prefix):].strip()
            slug = slugify(body)
            if not slug:
                return None
            return f"{kind}:{slug}"
    return None


def _audit_override(world_dir: Optional[Path], agent_name: str,
                    justification: str, payload: dict) -> None:
    """Append to <world_dir>/origin-signal-overrides.jsonl. Best-effort:
    any error is swallowed (telemetry must not block goal filing)."""
    if world_dir is None:
        return
    try:
        out = world_dir / "origin-signal-overrides.jsonl"
        rec = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "agent": agent_name or None,
            "justification": justification,
            "title": payload.get("title"),
            "origin_signal": payload.get("origin_signal"),
        }
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def evaluate(payload: dict, *, override_signal: Optional[str] = None,
             agent_name: str = "", world_dir: Optional[Path] = None) -> dict:
    """Run the gate. Returns the JSON payload that the legacy CLI prints to
    stdout (byte-for-byte equivalent via json.dumps with the same kwargs).

    Args:
        payload: parsed input JSON. Either {batch: [...], source: ...}
            for batch mode, or {title, origin_signal, source, ...} for
            single mode.
        override_signal: justification string, or None. When non-empty
            AND the goal would have blocked, the audit log is written.
        agent_name: MIND_AGENT value (used for world_user_context check
            and for the audit-log "agent" field). Empty string is OK.
        world_dir: WORLD_DIR for the override audit log. None disables
            audit-log writes (matches legacy CLI when WORLD_DIR env is unset).

    Side effects: see module docstring. Both happen inside this call.
    """
    source = (payload.get("source") or "agent").strip().lower()
    world_user_context = (source == "world" and not agent_name)

    # ---- Batch mode --------------------------------------------------------
    if isinstance(payload.get("batch"), list):
        results = []
        any_blocked = False
        override_used = False
        auto_derived_count = 0
        for g in payload["batch"]:
            gid = g.get("id")
            gsig = g.get("origin_signal")
            if world_user_context:
                results.append({"id": gid, "would_block": False,
                                "reason": "world-source user context",
                                "origin_signal": gsig})
                continue
            if is_valid(gsig):
                results.append({"id": gid, "would_block": False,
                                "reason": "valid origin_signal",
                                "origin_signal": gsig})
                continue
            derived = try_auto_derive(g)
            if derived and not override_signal:
                auto_derived_count += 1
                results.append({
                    "id": gid, "would_block": False,
                    "reason": "auto-derived (Layer-D title-prefix mapping)",
                    "origin_signal": derived,
                    "auto_derived": True,
                })
                continue
            if override_signal:
                _audit_override(world_dir, agent_name, override_signal, g)
                override_used = True
                results.append({"id": gid, "would_block": False,
                                "reason": "override applied",
                                "origin_signal": gsig,
                                "override_applied": override_signal})
                continue
            any_blocked = True
            results.append({
                "id": gid, "would_block": True, "origin_signal": gsig,
                "reason": ("Invalid origin_signal "
                           + (f"'{gsig}'" if gsig else "(missing)")
                           + " on goal " + (gid or "<no-id>")),
            })

        # Decision derivation for telemetry.
        decision_path = None  # branch label (guard-502); only auto-derive labeled today
        if world_user_context:
            decision = "noop"
            trigger = None
        elif any_blocked:
            decision = "block"
            trigger = next(
                (r["origin_signal"] for r in results if r["would_block"]),
                "(missing)",
            )
        elif override_used:
            decision = "override"
            trigger = next(
                (r["origin_signal"] for r in results
                 if r.get("override_applied")),
                None,
            )
        elif auto_derived_count > 0:
            # Auto-derived: trigger matched, a valid signal was derived
            # (Layer-D), goal allowed -> a non-blocking PASS. decision_path
            # marks the branch so auto-derives stay countable (guard-502).
            # Was non-canonical "auto_derive" coerced to fail_open; fixed
            # 2026-05-28 (rb on auto-derive mislabel).
            decision = "pass"
            decision_path = "auto_derive"
            trigger = next(
                (r["origin_signal"] for r in results
                 if r.get("auto_derived")),
                None,
            )
        else:
            decision = "pass"
            trigger = None
        _gate_log(
            "origin-signal-gate",
            decision,
            trigger_matched=str(trigger) if trigger is not None else None,
            payload=("batch:" + str(len(payload["batch"]))),
            override_reason=override_signal,
            extra={
                "mode": "batch",
                "batch_size": len(payload["batch"]),
                "any_blocked": any_blocked,
                "override_used": override_used,
                "auto_derived_count": auto_derived_count,
                "world_user_context": world_user_context,
                "decision_path": decision_path,
            },
        )
        return {"any_blocked": any_blocked, "results": results}

    # ---- Single mode -------------------------------------------------------
    signal = payload.get("origin_signal")
    single_extra = {
        "mode": "single",
        "world_user_context": world_user_context,
        "title_present": bool(payload.get("title")),
    }

    if world_user_context:
        _gate_log(
            "origin-signal-gate", "noop",
            trigger_matched=None,
            payload=(payload.get("title") or "")[:500],
            extra=single_extra,
        )
        return {"would_block": False,
                "reason": "world-source user context",
                "origin_signal": signal}

    if is_valid(signal):
        _gate_log(
            "origin-signal-gate", "pass",
            trigger_matched=str(signal),
            payload=(payload.get("title") or "")[:500],
            extra=single_extra,
        )
        return {"would_block": False,
                "reason": "valid origin_signal",
                "origin_signal": signal}

    derived = try_auto_derive(payload)
    if derived and not override_signal:
        _gate_log(
            # Auto-derived = non-blocking PASS; decision_path marks the branch
            # (guard-502). Was non-canonical "auto_derive" coerced to fail_open.
            "origin-signal-gate", "pass",
            trigger_matched=derived,
            payload=(payload.get("title") or "")[:500],
            extra=dict(single_extra, derived_from=signal, decision_path="auto_derive"),
        )
        return {
            "would_block": False,
            "reason": "auto-derived (Layer-D title-prefix mapping)",
            "origin_signal": derived,
            "auto_derived": True,
        }

    if override_signal:
        _audit_override(world_dir, agent_name, override_signal, payload)
        _gate_log(
            "origin-signal-gate", "override",
            trigger_matched=str(signal) if signal else "(missing)",
            payload=(payload.get("title") or "")[:500],
            override_reason=override_signal,
            extra=single_extra,
        )
        return {
            "would_block": False,
            "reason": "override applied",
            "origin_signal": signal,
            "override_applied": override_signal,
        }

    # BLOCK — missing or invalid, no override.
    reason = (
        "Invalid origin_signal "
        + (f"'{signal}'" if signal else "(missing)")
        + ". Every agent-sourced goal must cite a concrete signal. Allowed: "
        + ", ".join(ALLOWED_PREFIXES)
        + ". To bypass with justification: --override-signal \"<reason>\"."
    )
    _gate_log(
        "origin-signal-gate", "block",
        trigger_matched=str(signal) if signal else "(missing)",
        payload=(payload.get("title") or "")[:500],
        extra=single_extra,
    )
    return {"would_block": True, "reason": reason, "origin_signal": signal}
