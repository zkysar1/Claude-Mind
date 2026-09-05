#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-prompt latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# user-prompt-retrieval-inject.sh — UserPromptSubmit hook: automatic retrieval
# pre-pass on every substantive user message (2026-08-21, owner-directed).
#
# WHY: assistant mode structurally under-retrieved — measured 271 of 56,605
# daemon calls (0.48%) were /v1/retrieve. The per-turn mandate lived in
# /respond Step 4, but (a) routing every message through /respond is
# honor-system, and (b) the skill-dedup gate refuses Skill(respond)
# re-invocation after its first load, so the mandate's own carrier was blocked
# by our own gate. This hook moves the FIRST retrieval below the model
# entirely: the search happens before the model starts thinking, and the
# ranked index it injects is scent for deeper voluntary retrieval.
#
# FAIL-OPEN IS STRUCTURAL: exit 2 on UserPromptSubmit BLOCKS AND ERASES the
# user's prompt (per hooks docs) — this script must NEVER exit 2. Every error
# path exits 0 (silent no-inject) or 1 (non-blocking notice). The ERR trap
# pins the failure exit to 1.
#
# Injection contract (hooks docs, confirmed 2026-08-21): stdout JSON
# {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
#   "additionalContext": "..."}} on exit 0; 10,000-char cap on the string.
#
# SKIPS (silent, exit 0): payload unparseable; expansion-typed prompts (slash
# commands / mcp — those are commands, not questions); text < 24 chars; bare
# acknowledgements; loop-continuation sentinels (<<…>> prompts); sessions
# whose live agent-mode is autonomous (the loop has its own retrieval
# discipline and its continuation prompts are machine-generated); no
# resolvable session→agent binding; retrieval failure.
#
# The user's text is NEVER shell-interpolated (guard-165 class): it travels
# via env into python and via argv-list into subprocess.
trap 'exit 1' ERR
stdin_payload="$(cat)" || exit 1
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

JSON_PAYLOAD="$stdin_payload" SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)" \
    PROJECT_ROOT="${PROJECT_ROOT:-}" \
    python3 - <<'PY' 2>/dev/null || exit 1
import json, os, re, subprocess, sys

payload = os.environ.get('JSON_PAYLOAD', '')
try:
    d = json.loads(payload) if payload else {}
except Exception:
    sys.exit(0)

# Expansion-typed prompts (slash_command / mcp_prompt) are commands, not
# questions — the UserPromptExpansion sibling handles their telemetry.
if d.get('expansion_type'):
    sys.exit(0)
text = ' '.join(str(d.get('user_input') or '').split())
sid = str(d.get('session_id') or '')
# '<<' prefix = loop-continuation sentinels (<<autonomous-loop-dynamic>> and
# the CronCreate <<autonomous-loop>> sibling, schedule-wakeup-correctness.md)
# — machine-generated re-entry prompts, never questions. Measured 2026-08-21:
# without this skip the sentinel burned a full junk retrieval per wakeup.
if (not text or text.startswith('/') or text.startswith('<<')
        or len(text) < 24):
    sys.exit(0)
if re.match(r'^(ok(ay)?|yes|no|go( ahead)?|continue|proceed|thanks?|thank you|'
            r'great|nice|cool|perfect|sounds good|do it|yep|sure|lgtm)[\s!.…]*$',
            text, re.I):
    sys.exit(0)

scripts_dir = os.environ.get('SCRIPTS_DIR', '')
sys.path.insert(0, scripts_dir)
try:
    from pathlib import Path
    from _paths import SESSION_DIRNAME
    from _session_binding import resolve_binding, _agent_dir
    from _runtime_bash import bash_cmd
    # MIND_PROMPT_HOOK_ROOT is a test seam (same pattern as the freshness
    # tick's EMBED_FRESHNESS_INDEX_DIR): _paths.sh unconditionally exports
    # the real PROJECT_ROOT above, so subprocess tests can only redirect the
    # agent-tree root through a dedicated variable.
    root = Path(os.environ.get('MIND_PROMPT_HOOK_ROOT')
                or os.environ.get('PROJECT_ROOT', '') or '.')
    agent = ''
    b = resolve_binding(sid, root) if sid else None
    if b is not None:
        agent = b.agent
    if not agent and sid and not any(c in sid for c in ('/', '\\', '\n', ' ')):
        # Same fallback order as bash-agent-inject: the per-SID resolution memo
        # (core/logs/bash-inject-resolved/<sid>) carries the agent this SID
        # last resolved to. A continued/compacted session routinely has NO
        # binding.yaml yet a live memo (measured on this very session,
        # 2026-08-21). NEVER fall further to first-conf — this is a
        # multi-agent box and that is the  wrong-agent hazard.
        try:
            memo = (root / 'core' / 'logs' / 'bash-inject-resolved' / sid)
            name = memo.read_text(encoding='utf-8').strip()
            if re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                agent = name
        except Exception:
            pass
    if not agent:
        sys.exit(0)
    # Autonomous sessions are excluded: the loop retrieves for itself (Phase 4
    # election + gates), and an auto-retrieval on every machine-generated
    # continuation prompt would be fleet-wide waste plus latency on the
    # resurrection turn's re-arm-FIRST path.
    #
    # SESSION-FIRST mode read, deliberately: binding.yaml's mode is THIS
    # session's mode, while agents/<a>/session/agent-mode is AGENT-wide — an
    # observer session (reader/assistant beside a RUNNING loop, the owner's
    # main pattern) shares the agent file, which reads 'autonomous' exactly
    # when the observer most wants injection. Binding wins; the agent file is
    # only the fallback for binding-less sessions (memo-resolved compaction
    # resumes). Known benign miss: post-/stop chat in a formerly-autonomous
    # session keeps its stale binding mode and stays uninjected.
    mode = ''
    if b is not None and getattr(b, 'mode', None):
        mode = str(b.mode).strip().lower()
    if not mode:
        try:
            mode = (_agent_dir(root, agent) / SESSION_DIRNAME /
                    'agent-mode').read_text(encoding='utf-8').strip().lower()
        except Exception:
            mode = ''
    if mode == 'autonomous':
        sys.exit(0)
    # Test seam (freshness-tick EMBED_FRESHNESS_DRYRUN pattern): prove the
    # gates passed without a daemon-dependent retrieval. Vacuous-pin defense —
    # without a marker, "gate exited" and "retrieval failed" are both empty
    # stdout and a gate test can pass while never reaching the gate (caught
    # live 2026-08-21: a binding fixture missing session_id/local-paths.conf
    # resolved to None and the test went green at the wrong exit).
    if os.environ.get('MIND_PROMPT_HOOK_DRYRUN'):
        print(json.dumps({'dryrun': True, 'agent': agent, 'mode': mode or None}))
        sys.exit(0)
except Exception:
    sys.exit(0)

query = text[:400]
env = dict(os.environ)
env['MIND_AGENT'] = agent
# Mark this retrieval AUTOMATIC (). retrieve.sh records every successful
# consultation into the session provenance manifest; this one was not requested
# by the model and may never be read by it, so it must not count as evidence the
# agent consulted its knowledge. Without this flag the retrieval-floor query
# would pass for every session in which a human typed a substantive sentence --
# a floor that can never fail, which is worse than no floor because it reads as
# coverage. See DELIBERATE_RETRIEVAL_KINDS in context-reads.py.
env['MIND_RETRIEVAL_AUTO'] = '1'

# HARD LATENCY BUDGET. This hook runs BEFORE the model starts thinking, so
# every second here is dead air the user watches. The ceiling is therefore a
# UX bound, not a correctness bound -- blowing it costs one un-injected
# message, which is exactly the pre-hook state and never an error.
#
# MEASURED (alpha worker Body, hostname cc-08, uname -r 6.8.0-138-generic,
# own-cloud, 2026-09-03) on this script's exact invocation -- same flags, warm
# daemon: 957ms / 1037ms / 1575ms across three unrelated queries. 5s is ~3.2x
# the observed max, while capping worst-case dead air at 5s.
#
# It was 18s until now, against a goal () that specified "~2s": an
# 18s ceiling means a pathological retrieval blocks the user's prompt for 18
# seconds, which defeats the point of having a budget at all. 2s exactly was
# NOT taken -- it leaves only 27% headroom over the measured max, so ordinary
# jitter (cold daemon, contention, a slower box) would silently stop injecting
# on the boxes that need it most. The number is a measurement, not the spec's
# literal value, and the spec's "~" is read as licensing that.
RETRIEVE_TIMEOUT_S = 5

# Test seam, same family as MIND_PROMPT_HOOK_DRYRUN above: supply the
# retrieval JSON directly so the dedup lattice below can be exercised
# hermetically. Without it a dedup test needs a live daemon AND a stable
# corpus, which makes it an integration test that cannot pin the branch.
_fake = os.environ.get('MIND_PROMPT_HOOK_FAKE_RETRIEVAL')
if _fake:
    try:
        data = json.loads(_fake)
    except Exception:
        sys.exit(0)
else:
    try:
        r = subprocess.run(
            bash_cmd(os.path.join(scripts_dir, 'retrieve.sh'),
                     '--category', query, '--depth', 'shallow',
                     '--read-only', '--include-framework'),
            capture_output=True, text=True, timeout=RETRIEVE_TIMEOUT_S, env=env,
            cwd=os.environ.get('PROJECT_ROOT') or None)
        out = r.stdout
        data = json.loads(out[out.find('{'):])
    except Exception:
        sys.exit(0)

def _clip(s, n):
    s = ' '.join(str(s or '').split())
    return s if len(s) <= n else s[:n - 1] + '…'

ec = (data.get('meta') or {}).get('embedding_channel', '?')

# ---- SESSION DEDUP ( outcome 3) --------------------------------
# A focused conversation re-retrieves the same nodes turn after turn, so
# without this the identical index is re-injected on every message -- pure
# context burn, and it crowds out the hits that ARE new.
#
# FAIL-OPEN IS IN THE *INJECT* DIRECTION, and that asymmetry is the whole
# design. Any failure reading or writing the state file leaves every hit
# unfiltered: a dedup fault must never be able to SUPPRESS retrieval, because
# that converts a cache miss into a silent regression of the entire hook and
# looks identical to "no matches" from the outside. Over-injecting costs
# tokens; under-injecting costs the feature.
#
# A repeat is never dropped in SILENCE either. When every hit is one this
# session already saw, the recap line below still names the ids -- the scent
# survives at ~1 line instead of ~6, and the model can still expand them.
def _hid(kind, e):
    return '%s:%s' % (kind, e.get('key') or e.get('id') or e.get('path')
                      or e.get('file') or '')

seen, state_path = set(), None
try:
    from _paths import SESSIONS_DIRNAME
    if sid and re.fullmatch(r'[A-Za-z0-9._-]+', sid):
        state_path = (_agent_dir(root, agent) / SESSIONS_DIRNAME / sid /
                      'prompt-injected-hits.txt')
        if state_path.exists():
            seen = {ln.strip() for ln
                    in state_path.read_text(encoding='utf-8').splitlines()
                    if ln.strip()}
except Exception:
    seen, state_path = set(), None

fresh_ids, repeat_ids = [], []

def _fresh(kind, entries, cap):
    """Filter BEFORE capping, so a session that already saw the top N gets
    the next N rather than nothing -- the repeats are what we are paying to
    remove, not the depth."""
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        h = _hid(kind, e)
        if h in seen:
            repeat_ids.append(h)
            continue
        out.append(e)
        fresh_ids.append(h)
        if len(out) >= cap:
            break
    return out

lines = []
tree = _fresh('t', data.get('tree_nodes'), 4)
rb = _fresh('r', data.get('reasoning_bank'), 3)
guards = _fresh('g', data.get('guardrails'), 3)
fw = _fresh('f', data.get('framework_rules'), 2)
if tree:
    lines.append('tree: ' + '; '.join(
        '%s — %s' % (e.get('key'), _clip(e.get('summary'), 60)) for e in tree))
if rb:
    lines.append('rb: ' + '; '.join(
        '%s — %s' % (e.get('id'), _clip(e.get('title'), 60)) for e in rb))
if guards:
    lines.append('guards: ' + '; '.join(
        '%s — %s' % (e.get('id'), _clip(e.get('rule'), 70)) for e in guards))
if fw:
    lines.append('rules: ' + '; '.join(
        _clip(e.get('path') or e.get('file'), 60) for e in fw))

# Persist AFTER building, and only what was actually injected -- recording a
# hit the caller never saw would suppress it forever on the strength of a
# message that never carried it.
if state_path is not None and fresh_ids:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with state_path.open('a', encoding='utf-8') as fh:
            fh.write('\n'.join(fresh_ids) + '\n')
    except Exception:
        pass

if lines:
    body = ('[auto-retrieval pre-pass | embedding_channel: %s] Store matches for '
            'this message — an INDEX, not an answer. Expand what is relevant '
            '(Read the node file / retrieve.sh --id / guardrails-read.sh --id — '
            'a truncated rule head is not the rule, guard-1421) BEFORE answering; '
            'respond Step 4 escalation still applies for depth.\n' % ec
            + '\n'.join(lines))
    if repeat_ids:
        body += ('\n(%d further match(es) were already injected earlier this '
                 'session and are not repeated.)' % len(repeat_ids))
elif repeat_ids:
    # EVERY hit is a repeat. Silence here would be a REGRESSION dressed as a
    # saving: the model would read "no output" identically to "nothing in the
    # stores matches", which is the opposite of the truth. Recap the ids only.
    body = ('[auto-retrieval pre-pass | embedding_channel: %s] Every store match '
            'for this message was already injected earlier this session — not '
            'repeating the index. Still relevant, expand on demand: %s'
            % (ec, ', '.join(h.split(':', 1)[1] for h in repeat_ids[:12] if ':' in h)))
else:
    body = ('[auto-retrieval pre-pass | embedding_channel: %s] No store matches '
            'for this message. If it needs domain knowledge, that absence means '
            'escalate: Tier 2 codebase grep → Tier 2.5 peer-retrieve.sh → Tier 3 '
            'web (retrieval-escalation.md).' % ec)

print(json.dumps({'hookSpecificOutput': {
    'hookEventName': 'UserPromptSubmit',
    'additionalContext': body[:9500]}}))
PY
exit 0
