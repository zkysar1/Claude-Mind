#!/usr/bin/env bash
# Behavioral proof for the  worker rail in context-reads-skill-gate.sh.
# Extracts the real python block from the live script (never a hand-copy) and
# runs it in BOTH branches with the production env shape (guard-920).
set -uo pipefail
cd "$(cd "$(dirname "$0")/../../.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
AG="$TMP/agents/zeta"
mkdir -p "$AG/sessions/SID-TEST"

# Extract the block verbatim between the heredoc markers of the LAST PY block.
awk '/python3 - <<.PY./{f=1;next} f&&/^PY$/{exit} f' \
    core/scripts/context-reads-skill-gate.sh > "$TMP/block.py"
echo "extracted $(wc -l < "$TMP/block.py") lines of REAL block"
grep -q "g-306-125" "$TMP/block.py" || { echo "FAIL: rail not in extracted block"; exit 1; }

run_block () {
    AGENT_DIR="$AG" AGENT_NAME=zeta SKILL_NAME=some-skill SESSION_ID="$1" \
        SCRIPTS_DIR="$PWD/core/scripts" python3 "$TMP/block.py" 2>/dev/null || true
}

# --- Branch A: REDUCER (no per-session body-WM file) -> MUST append ---
rm -f "$AG/skill-invocations.jsonl"
run_block "SID-TEST"
a=$( [ -f "$AG/skill-invocations.jsonl" ] && wc -l < "$AG/skill-invocations.jsonl" || echo 0 )
echo "A reducer  (no body-WM): rows=$a  expect>=1"

# --- Branch B: WORKER (per-session body-WM file present) -> MUST skip ---
rm -f "$AG/skill-invocations.jsonl"
touch "$AG/sessions/SID-TEST/working-memory.yaml"
run_block "SID-TEST"
b=$( [ -f "$AG/skill-invocations.jsonl" ] && wc -l < "$AG/skill-invocations.jsonl" || echo 0 )
echo "B worker   (body-WM present): rows=$b  expect=0"

# --- Branch C: the INERT form the rail must NOT be. Proves BODY_ROLE alone
#     would not have discriminated: set it and confirm branch A still appends. ---
rm -f "$AG/skill-invocations.jsonl" "$AG/sessions/SID-TEST/working-memory.yaml"
BODY_ROLE=worker run_block "SID-TEST"
c=$( [ -f "$AG/skill-invocations.jsonl" ] && wc -l < "$AG/skill-invocations.jsonl" || echo 0 )
echo "C BODY_ROLE=worker but no body-WM: rows=$c (documents that env alone is not the predicate)"

# ================= SIBLING WRITER: user-prompt-skill-record.sh =================
# Different production shape: this block reads the hook payload from STDIN.
awk '/python3 - <<.PY./{f=1;next} f&&/^PY$/{exit} f' \
    core/scripts/user-prompt-skill-record.sh > "$TMP/block2.py"
echo "extracted $(wc -l < "$TMP/block2.py") lines of REAL sibling block"
grep -q "g-306-125" "$TMP/block2.py" || { echo "FAIL: rail not in sibling block"; exit 1; }

# Production shape (guard-920), measured from the live script, NOT guessed:
# payload arrives via the JSON_PAYLOAD **env var** (not stdin), and the block
# hard-requires expansion_type=="slash_command" or it sys.exit(0)s at line 40.
# A first pass omitted both and produced a 0-row reducer branch that looked
# exactly like an over-firing rail.
run_block2 () {
    JSON_PAYLOAD="{\"expansion_type\":\"slash_command\",\"command_name\":\"some-skill\",\"session_id\":\"$1\"}" \
    AGENT_DIR="$AG" AGENT_NAME=zeta SCRIPTS_DIR="$PWD/core/scripts" \
        python3 "$TMP/block2.py" 2>/dev/null || true
}

rm -f "$AG/skill-invocations.jsonl" "$AG/sessions/SID-TEST/working-memory.yaml"
run_block2 "SID-TEST"
d=$( [ -f "$AG/skill-invocations.jsonl" ] && wc -l < "$AG/skill-invocations.jsonl" || echo 0 )
echo "D sibling reducer (no body-WM): rows=$d  expect>=1"

rm -f "$AG/skill-invocations.jsonl"
touch "$AG/sessions/SID-TEST/working-memory.yaml"
run_block2 "SID-TEST"
e=$( [ -f "$AG/skill-invocations.jsonl" ] && wc -l < "$AG/skill-invocations.jsonl" || echo 0 )
echo "E sibling worker  (body-WM present): rows=$e  expect=0"

# --- Branch F/G/H: the REAL /stop worker short-circuit, extracted from the live
# SKILL.md (never a hand-copy). This is mechanism (1) of : a /stop typed
# on a WORKER box must not set the agent-wide stop-requested signal, because the
# REDUCER on another machine reads it. The predicate is an executable one-liner, so
# it is testable even though it ships inside prose -- which is the point: an
# unverified rail that merely READS correctly is the guard-2445 class.
SK=".claude/skills/stop/SKILL.md"
python3 - "$SK" > "$TMP/pred.txt" <<'PYEOF'
import sys, pathlib
lines = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
pred = [i for i, l in enumerate(lines) if 'echo "worker"' in l and 'sessions' in l]
if len(pred) != 1:
    sys.exit('FAIL: expected exactly 1 worker-predicate line, got %d' % len(pred))
inner = lines[pred[0]].split('`')[1]
if 'BODY_ROLE' in inner:
    sys.exit('FAIL: predicate keys on BODY_ROLE -- present in Bash-tool context but '
             'inert everywhere else; derive from the body-WM file (guard-2445)')
if 'working-memory.yaml' not in inner or 'MIND_SID' not in inner:
    sys.exit('FAIL: predicate must test sessions/$MIND_SID/working-memory.yaml')
# ORDERING carries the safety: the short-circuit must precede the agent-wide
# stop-requested write, or a worker signals the reducer before ever reaching it.
sig = [i for i, l in enumerate(lines) if 'session-signal-set.sh stop-requested' in l]
if not sig:
    sys.exit('FAIL: could not locate the stop-requested write to order against')
if pred[0] >= min(sig):
    sys.exit('FAIL: worker short-circuit at L%d is NOT before stop-requested at L%d'
             % (pred[0] + 1, min(sig) + 1))
# <agent-name> is a placeholder the skill requires its caller to substitute at
# runtime, so substituting it here reproduces the production call shape (guard-920).
print(inner.replace('<agent-name>', 'zeta'))
PYEOF
PRED="$(cat "$TMP/pred.txt")"
echo "extracted REAL predicate from $SK (ordering + non-BODY_ROLE asserted)"

PROOT="$TMP/proot"; mkdir -p "$PROOT/agents/zeta/sessions/SID-TEST"

rm -f "$PROOT/agents/zeta/sessions/SID-TEST/working-memory.yaml"
f=$(cd "$PROOT" && MIND_SID=SID-TEST bash -c "$PRED")
echo "F reducer (no body-WM):     $f  expect=reducer-or-single"

touch "$PROOT/agents/zeta/sessions/SID-TEST/working-memory.yaml"
g=$(cd "$PROOT" && MIND_SID=SID-TEST bash -c "$PRED")
echo "G worker  (body-WM present): $g  expect=worker"

# Fail-open: an unset MIND_SID must never classify a reducer as a worker, which
# would silently turn /stop into a no-op on the box that owns the state.
h=$(cd "$PROOT" && bash -c "$PRED")
echo "H unset MIND_SID:           $h  expect=reducer-or-single"

echo "---"
if [ "$a" -ge 1 ] && [ "$b" -eq 0 ] && [ "$d" -ge 1 ] && [ "$e" -eq 0 ] \
   && [ "$f" = "reducer-or-single" ] && [ "$g" = "worker" ] \
   && [ "$h" = "reducer-or-single" ]; then
    echo "VERDICT: PASS (writers: reducer appends, worker skips; /stop: worker short-circuits before stop-requested)"; exit 0
else echo "VERDICT: FAIL (a=$a b=$b d=$d e=$e f=$f g=$g h=$h)"; exit 1; fi
