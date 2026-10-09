# Rationale: Phase 0.5a Counts the Matched Guardrails and Takes One Bounded Action

Referenced from `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5a. Why the phase no longer loops over each matched guardrail's `action_hint`, why its one action is a read-only infra streak report, and why the count rides on the phase-end note (g-115-5785).

## Why the per-guardrail `action_hint` loop was removed

The old body ran `<run {guardrail.action_hint}>` for every match. `action_hint` is not an authored command. `guardrail-check.py` computes it with `extract_action_hint` (L195-207, called at L254-256): the FIRST `*.sh` token anywhere in the rule's prose (`ACTION_HINT_RE`, L98-103), plus an optional subcommand word from {check, status, has, value, check-all, stale, read} and an optional `--flag N`, matched case-insensitively. It never reads the record's own `action_hint` field. The hint therefore names whatever script the rule mentions first, including a script the rule forbids.

The design intent is stated in the producer: the docstring at L196-200 tells authors to write full paths because "the path in the text is the path that runs", and L303-305 says callers "iterate this list to execute action_hint commands". A 2026-09-05 note in g-115-3762's description (marker ACTION-HINT-AMENDMENT-INVISIBLE-20260905) read that code and took the intent as the contract. The intent assumes every rule's first script mention is a deliberate command. That holds for a few rules and not for a corpus whose lessons mention scripts incidentally, which is what printing the hints beside their rules showed.

Measured 2026-10-09 (echo, hostname cc-03, uname -r 6.8.0-142-generic) on `guardrail-check.sh --context any --phase pre-selection --type both`: 976 matched, all guardrails; 342 carry a hint; 145 distinct script basenames. The commonest are goal-selector.sh (31), aspirations-query.sh (26), roblox-studio.sh (12), iteration-close.sh (12), retrieve.sh (11), aspirations-claim.sh (9), aspirations-update-goal.sh (8), then efs-ssh.sh, aspirations-add-goal.sh and deploy-hold-check.sh (7 each); recurring-close.sh has 6. Running the framework ones verbatim claims goals, closes iterations and re-runs the selector. The census script at the end of this file reproduces every figure on this page.

- guard-2261's rule says "do NOT shortcut by calling core/scripts/goal-selector.sh directly". Its extracted hint is `goal-selector.sh`, a script its own text says mutates drain-lane state on every call. The loop prescribes the forbidden action.
- Only 14 of the 342 hints carry a subcommand word. The regex captures a subcommand word and one numeric flag, never a key or a component name, so `env-read.sh has` lacks the key and `infra-health.sh check` lacks the component. One subcommand is English prose: guard-3907's text "wm-set.sh HAS A HARD 128 KiB PAYLOAD CEILING" yields the hint `wm-set.sh HAS`.
- By the patterns in the census script below, 70 hints name a writer (close, claim, release, add, update, set, append, create, delete, move, post, send, push or write in the script name), and 53 name their script within 70 characters after a negation word in their own rule. Both are heuristic counts over the same measured set, not a classification. An earlier pass of g-115-5785 read 73 and 45 with a pattern list that was not recorded; the script is the definition.
- The hint count is flat while the match set grows: 377 matched / 124 hints at filing (2026-08-11), 965 / 340 on 2026-09-26, 976 / 342 on 2026-10-09.

A cap or a ranking cannot repair this, because the members are not diagnostics. Ranking by `utilization.times_active` would put the most-retrieved rules first, and those are the selector and close-script rules (guard-5492: times_active is not evidence a rule works). A probe tag would need a flag on a store of about 7,000 active guardrails (6,977 on 2026-09-26) plus a second list of which scripts are safe to run.

`core/config/conventions/reasoning-guardrails.md` described the field as "executable script commands" extracted from rule text, which is the likely source of the "run it" reading. It now states what the field is.

## Why one read-only infra streak report

The phase's old issue branch (`→ CREATE_BLOCKER`) wanted one question answered before selection: is infrastructure failing? `infra-health.sh streak-alert --no-sync-blockers` answers it in one bounded call. It lists components whose `consecutive_failures` is at or above the configured threshold with a last failure inside the recency window (read 2026-10-09: threshold 3, window 6 h, `alert_count` 1, 620 bytes, rc 1). Alternatives measured and rejected:

- `status` returns all 90 components with 88 stored readings stale, so staleness is the norm and carries no signal.
- `check-all` live-probes every component (`cmd_check_all` docstring: 23+ components, up to 30 s each).
- The default `streak-alert` also rewrites the calling agent's `known_blockers` (`_sync_known_blockers`, called at infra-health.py L934-935 unless `--no-sync-blockers`; idempotent and self-clearing) and applies no skip list. The skip list for environment-unreachable components (`INFRA_STREAK_SKIP_COMPONENTS`) lives in `infra-streak-notify.sh` alone, which also adds the down-episode dedup. A phase that runs every iteration on every box therefore reads, and leaves escalation to Phase 0.5b and that wrapper. It neither double-files a blocker nor writes one for a component this box cannot reach.

For that reason the phase carries no `CREATE_BLOCKER` call of its own. AJ6 item 33 of `verification-checklist.md` credited "error emails before selection" to a pre-selection guardrail; that guardrail is absent from the store (`guardrails-read.sh --id guard-17` and `--id guard-017` both return not_found), and the always-run `inbox-alert-age-check` lane (Phase 0.5b.1b) covers the check.

## Why the count rides on the phase-end note

A skipped Phase 0.5a and a diligent one wrote the same diary, phase markers only. `execution-diary.sh phase-end --note` already exists, so the report costs no new script and no hand-built JSON line: `0.5a matched=<matched_count> hints=<entries with action_hint> hints_run=0 alerts=<alert_count>` (the leading label is there because the diary's text view prints a phase-end entry's note alone, without the phase name). A phase that did not run leaves no note. A phase that ran shows the volume it surfaced and that the hints stayed unexecuted by design. `hints_run` stays in the line so that a later change which runs a bounded subset shows up in the same field.

## Not covered here

- The match SET stays undifferentiated. The 976 rules carry 1,945,001 characters of rule text (median 1,587), too much to read in a turn, so the phase counts them. 111 contain the token "selector" (65 contain "goal-selector"; an earlier 107 used an unrecorded token list), and a reader cannot tell those from the rest (g-115-5785's 2026-08-19 corroboration: guard-2261 and guard-2331 were in a pre-selection set and unread). A bounded digest of that subset is g-115-12294.
- `.claude/skills/aspirations-execute/SKILL.md` L977-980 (and `core/config/execute-protocol-digest.md` L323) prescribes "Execute every matched guardrail's action_hint" for the post-execution check. Measured 2026-10-09 with `--dry-run`: context infrastructure 709 matched / 322 hints / 129 distinct scripts (recurring-close.sh 20, aspirations-query.sh 19, aspirations-update-goal.sh 16, iteration-close.sh 15); context testing 448 / 180 / 89; union 371 hints over 139 scripts. It reads the same regex-picked field over a set that is not narrower in practice, and is carried by g-115-3762.

## Re-deriving the figures

Every pre-selection count on this page comes from one saved output of `guardrail-check.sh --context any --phase pre-selection --type both` and the script below (`py -3 census.py gc.json`). The post-execution figures use `--context infrastructure` and `--context testing` with `--phase post-execution --type both --dry-run`, the same loading and the same basename counting, and the union is by guardrail id. Re-run 2026-10-09 (echo, hostname cc-03, uname -r 6.8.0-142-generic): 976 matched, 342 hinted, 145 distinct basenames, 14 multi-token hints, 1,945,001 characters of rule text (median 1,587, max 13,278), 111 rules containing "selector" of which 65 contain "goal-selector", 70 writer-named hints, 53 negation-adjacent hints; post-execution infrastructure 709 matched / 322 hints / 129 scripts, testing 448 / 180 / 89, union 845 matched / 371 hints / 139 scripts. An earlier pass of this goal read 107 for the selector count and 73 and 45 for the last two with token and pattern lists that were not recorded; those three are superseded by the figures here.

```python
import collections, json, os, re, statistics, sys

d = json.JSONDecoder().raw_decode(open(sys.argv[1]).read().lstrip())[0]
ms = d["matched"]
hinted = [g for g in ms if g.get("action_hint")]
base = collections.Counter(os.path.basename(g["action_hint"].split()[0]) for g in hinted)
lens = [len(g.get("rule") or "") for g in ms]
WRITER = re.compile(r"(close|claim|release|add|update|set|append|create|delete|move|post|send|push|write)")
NEG = re.compile(r"(?i)\b(not|never|don't|do not|no|without|instead of|rather than|avoid)\b")
writers = near_neg = 0
for g in hinted:
    name = os.path.basename(g["action_hint"].split()[0])
    if WRITER.search(name.removesuffix(".sh")):
        writers += 1
    rule = g.get("rule") or ""
    i = rule.find(name)
    if i >= 0 and NEG.search(rule[max(0, i - 70):i]):
        near_neg += 1
print("matched", d["matched_count"], "hinted", len(hinted), "distinct basenames", len(base),
      "multi-token hints", sum(1 for g in hinted if len(g["action_hint"].split()) > 1))
print("commonest", base.most_common(10))
print("rule text chars", sum(lens), "median", statistics.median(lens), "max", max(lens))
print("rules containing 'selector'", sum("selector" in (g.get("rule") or "") for g in ms),
      "| 'goal-selector'", sum("goal-selector" in (g.get("rule") or "") for g in ms))
print("writer-named hints", writers, "| negation within 70 chars before the first mention", near_neg)
```

## Cross-references

- g-115-5785 — the goal; its progress note carries the 2026-08-11, 2026-08-19, 2026-09-26 and 2026-10-09 readings
- rb-6588 — the phase filter is a second join keyed on the rule's own text; volume conceals what is missing
- rb-10448 — citing a guardrail without running its measurement; it governs the ONE rule you cite, not a blanket loop
- guard-2261 — the selector is not idempotent
- `core/scripts/guardrail-check.py`, `core/scripts/infra-health.py`, `core/scripts/infra-streak-notify.sh`
- `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5a and Phase 0.5b
