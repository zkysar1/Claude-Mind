""": every raw `open(path, "a")` in the daemon must materialize first.

THE DEFECT. An append does not READ the file, so nothing on a raw-append path
ever pulls it. On an own-cloud box whose local mirror is behind S3 the append
extends a STALE base. Measured on meta-log.jsonl during g-115-3534: the daemon
appended at byte 1,307,327 while the mirror sat at 1,306,073 — 1,254 bytes
behind.

NOT the mechanism, measured so this test does not cargo-cult a lock: concurrent
appender interleaving. 40 processes writing 2,400 / 60,000 / 500,000 /
2,000,000-byte records through the raw shape lost and corrupted nothing (Linux
O_APPEND is atomic for regular files). The fix is materialize-before-append, not
a lock.

WHY AN AST SWEEP AND NOT A PROXIMITY GREP. g-115-3541's own audit used a
~3000-char textual lookback and got two answers wrong in OPPOSITE directions:

  * FALSE GUARDED — endpoints/curriculum.py `_append_jsonl` was scored "guarded"
    because `_read_jsonl`'s ensure_local sits ~35 lines above it in the same
    file. That helper is never called on the write path, so the append was
    unprotected the whole time.
  * FALSE UNGUARDED — meta/meta_yaml.py `_append_log` has no ensure_local in its
    own body; its guard lives one call away in `_next_meta_change_id`. A 14-line
    lookback reports it as a defect that is not there.

So the check resolves ONE level of same-module indirection: a function is
guarded if it materializes, or if it calls a same-module function that does,
before the append. Both real cases above are covered, and both are asserted
below as positive controls — a sweep whose own known-answer cases are not pinned
is a sweep that can drift to always-clean (guard-1943).

ESCAPE HATCH. A genuinely machine-local append (nothing under a governed root)
does not need the call. Mark that line `# raw-append-guard-exempt: <reason>` and
the sweep skips it with the reason on the record. There are ZERO such sites
today; the hatch exists so a future one is opted out with a stated reason rather
than by loosening the predicate.

Run: py -3 -m pytest core/scripts/tests/test_raw_append_ensure_local_sweep.py -v
"""
import ast
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPTS.parent.parent
DAEMON_SRC = PROJECT_ROOT / "mind_api" / "src"

EXEMPT_MARKER = "raw-append-guard-exempt:"
GUARD_NAMES = {"ensure_local_before_append", "ensure_local"}


def _is_append_open(node: ast.AST) -> bool:
    """True for `open(x, "a"...)` — positional or `mode=` keyword."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if not (isinstance(fn, ast.Name) and fn.id == "open"):
        return False
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and mode.startswith("a")


def _called_names(fn_node: ast.AST, before_line: int):
    """Names of every function called inside fn_node at or before before_line.

    Both bare `f(...)` and attribute `obj.f(...)` forms; the attribute form is
    what `get_backend().ensure_local(p)` looks like.
    """
    out = set()
    for n in ast.walk(fn_node):
        if not isinstance(n, ast.Call):
            continue
        if getattr(n, "lineno", 10**9) > before_line:
            continue
        f = n.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


def _module_functions(tree: ast.AST):
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _enclosing_function(tree: ast.AST, lineno: int):
    """Innermost function containing lineno (max start line wins)."""
    best = None
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(n, "end_lineno", None)
        if n.lineno <= lineno and (end is None or lineno <= end):
            if best is None or n.lineno > best.lineno:
                best = n
    return best


def audit_source(src: str, label: str = "<src>"):
    """Return [{line, function, guarded, why}] for every raw append in `src`."""
    tree = ast.parse(src)
    lines = src.splitlines()
    funcs = _module_functions(tree)
    findings = []
    for node in ast.walk(tree):
        if not _is_append_open(node):
            continue
        lineno = node.lineno
        raw_line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        if EXEMPT_MARKER in raw_line:
            findings.append({"line": lineno, "function": None, "guarded": True,
                             "why": "exempt marker", "file": label})
            continue
        fn = _enclosing_function(tree, lineno)
        if fn is None:
            findings.append({"line": lineno, "function": None, "guarded": False,
                             "why": "module-level append, no enclosing function",
                             "file": label})
            continue
        direct = _called_names(fn, lineno) & GUARD_NAMES
        if direct:
            findings.append({"line": lineno, "function": fn.name, "guarded": True,
                             "why": "direct " + sorted(direct)[0], "file": label})
            continue
        # ONE level of same-module indirection — the meta_yaml `_append_log` ->
        # `_next_meta_change_id` shape. Deliberately not transitive: an unbounded
        # walk would call almost anything guarded through a deep enough chain,
        # and "guarded five hops away" is not a property a reader can verify.
        via = None
        for callee in sorted(_called_names(fn, lineno)):
            target = funcs.get(callee)
            if target is None or target is fn:
                continue
            if _called_names(target, getattr(target, "end_lineno", 10**9)) & GUARD_NAMES:
                via = callee
                break
        findings.append({
            "line": lineno, "function": fn.name, "guarded": via is not None,
            "why": ("via " + via) if via else "no ensure_local before the append",
            "file": label,
        })
    return findings


def _audit_tree():
    out = []
    for p in sorted(DAEMON_SRC.rglob("*.py")):
        rel = p.relative_to(PROJECT_ROOT).as_posix()
        if "/tests/" in rel or p.name.startswith("test_"):
            continue
        out.extend(audit_source(p.read_text(encoding="utf-8"), rel))
    return out


# --------------------------------------------------------------------------
# The sweep itself
# --------------------------------------------------------------------------

def test_no_unguarded_raw_append_in_daemon_source():
    findings = _audit_tree()
    assert findings, (
        "the sweep found ZERO raw appends in mind_api/src — that is a BROKEN "
        "PROBE, not a clean tree (rb-245). There were 7 when this test was "
        "written; check DAEMON_SRC and _is_append_open before believing it."
    )
    bad = [f for f in findings if not f["guarded"]]
    assert not bad, (
        "raw append(s) to a possibly-S3-backed store with no materialize:\n"
        + "\n".join(f"  {f['file']}:{f['line']} in {f['function']}() — {f['why']}"
                    for f in bad)
        + "\n\nAn append never reads the file, so nothing else on this path will "
          "pull it: on an own-cloud box behind S3 the record extends a stale "
          "base. Add, before the open:\n"
          "    from storage_backend import ensure_local_before_append\n"
          "    ensure_local_before_append(path)\n"
          "It is fail-open by return value and never raises. If the path is "
          "genuinely machine-local, mark the open line "
          f"`# {EXEMPT_MARKER} <reason>` instead."
    )


# --------------------------------------------------------------------------
# Positive controls — the two cases the proximity heuristic got WRONG.
# These pin known answers so the sweep cannot silently drift to always-clean.
# --------------------------------------------------------------------------

def test_control_direct_guard_is_detected():
    """The shape a proximity grep gets right, so a failure here is the parser."""
    src = textwrap.dedent('''
        def writer(path, item):
            from storage_backend import ensure_local_before_append
            ensure_local_before_append(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(item)
    ''')
    f = audit_source(src)[0]
    assert f["guarded"] and f["why"].startswith("direct"), f


def test_control_indirect_guard_is_detected():
    """meta_yaml `_append_log` -> `_next_meta_change_id`: a 14-line lookback
    calls this a defect. It is not one."""
    src = textwrap.dedent('''
        def _next_id(ctx):
            from storage_backend import get_backend
            get_backend().ensure_local(ctx.p)
            return "mc-001"

        def _append_log(ctx, item):
            mc = _next_id(ctx)
            with open(ctx.p, "a", encoding="utf-8") as f:
                f.write(item)
            return mc
    ''')
    f = [x for x in audit_source(src) if x["function"] == "_append_log"][0]
    assert f["guarded"] and f["why"] == "via _next_id", f


def test_control_sibling_helper_does_not_confer_a_guard():
    """curriculum.py's real shape: a guarded READ helper in the same module that
    the WRITE path never calls. The proximity audit scored this guarded and the
    append was unprotected for weeks."""
    src = textwrap.dedent('''
        def _read_jsonl(path):
            from storage_backend import get_backend
            get_backend().ensure_local(path)
            return []

        def _append_jsonl(path, item):
            with open(path, "a", encoding="utf-8") as f:
                f.write(item)
    ''')
    f = [x for x in audit_source(src) if x["function"] == "_append_jsonl"][0]
    assert not f["guarded"], (
        "a guarded sibling helper that is never called must NOT confer a guard — "
        "this is the false-GUARDED half of the g-115-3541 audit: " + repr(f)
    )


def test_control_guard_after_the_append_does_not_count():
    """Ordering is the property that prevents the hang-equivalent here: a
    materialize that runs after the write cannot un-stale the base it wrote on."""
    src = textwrap.dedent('''
        def writer(path, item):
            from storage_backend import ensure_local_before_append
            with open(path, "a", encoding="utf-8") as f:
                f.write(item)
            ensure_local_before_append(path)
    ''')
    f = audit_source(src)[0]
    assert not f["guarded"], f


def test_control_exempt_marker_is_honoured():
    src = (
        'def writer(path, item):\n'
        '    with open(path, "a") as f:  # raw-append-guard-exempt: machine-local telemetry\n'
        '        f.write(item)\n'
    )
    f = audit_source(src)[0]
    assert f["guarded"] and f["why"] == "exempt marker", f


# --------------------------------------------------------------------------
# DISCRIMINATION — prove the sweep can go RED (self.md corollary 3, rb-5828).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode_expr", ['"a"', '"ab"', 'mode="a"'])
def test_sweep_reddens_on_an_injected_unguarded_append(mode_expr):
    """Without this, every green above could be a parser that finds nothing."""
    src = textwrap.dedent(f'''
        def brand_new_writer(path, item):
            with open(path, {mode_expr}) as f:
                f.write(item)
    ''')
    findings = audit_source(src, "injected.py")
    assert len(findings) == 1, findings
    assert not findings[0]["guarded"], (
        "the sweep did NOT flag an unguarded append — it is not discriminating, "
        "so its green on the real tree proves nothing: " + repr(findings)
    )


def test_read_mode_open_is_not_flagged():
    """The complement: a sweep that flags every open() would be equally useless."""
    src = 'def r(p):\n    with open(p, "r") as f:\n        return f.read()\n'
    assert audit_source(src) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
