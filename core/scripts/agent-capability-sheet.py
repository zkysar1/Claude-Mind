#!/usr/bin/env python3
"""
agent-capability-sheet.py — Render a per-agent capability sheet from the live
source-of-truth files (g-336-11 / GS-044).

The problem this closes: agents learn their permissions by trying and failing —
there is no single surface answering "what CAN this agent do." This tool RENDERS
that surface on demand.

Design invariant (verification outcome 2): NO second hand-maintained permission
list. Every section is a rendered VIEW of an authoritative file, cited inline.
Regenerate anytime; when a source-of-truth file changes, the sheet changes with
it — nothing here duplicates a permission that lives elsewhere.

Sources of truth (read live):
  1. CLAUDE.md  § "Tool Usage + Write Permissions"     -> stores read/write + share scope
  2. core/config/conventions/constitutional-rings.md   -> influence rights + limits (modification tiers)
  3. core/config/gates.yaml                            -> gated actions + the gates that guard them
  4. agents/<agent>/self.md § Agent-Provisionable Actions + § Primary Workspace

Usage:
  agent-capability-sheet.py [<agent>]        # default: $MIND_AGENT
  agent-capability-sheet.py --list-sources   # print the SoT file list + presence, exit 0

Output: the capability sheet (Markdown) to stdout. Exit 0 on success, 1 if the
named agent has no self.md, 2 on an unreadable core source.
"""
import os
import re
import sys

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a framework dependency
    yaml = None


def _root():
    # PROJECT_ROOT is two levels up from core/scripts/<this file>.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


ROOT = _root()

SOURCES = {
    "permissions": "CLAUDE.md",
    "rings": "core/config/conventions/constitutional-rings.md",
    "gates": "core/config/gates.yaml",
}


def _read(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


def _header_level(line):
    m = re.match(r"^(#{1,6})\s", line)
    return len(m.group(1)) if m else None


def extract_section(text, header_regex):
    """Lines from the first header matching header_regex up to (excluding) the
    next markdown header at the same-or-higher level. Returns [] if not found."""
    if not text:
        return []
    lines = text.splitlines()
    out, started, start_level = [], False, None
    for line in lines:
        lvl = _header_level(line)
        if not started:
            if lvl is not None and re.search(header_regex, line):
                started, start_level = True, lvl
                out.append(line)
            continue
        if lvl is not None and lvl <= start_level:
            break
        out.append(line)
    return out


def md_table_rows(lines):
    """Parse '| a | b | c |' rows, skipping the |---| separator. Header row is
    returned as the first tuple; caller decides how to use it."""
    rows = []
    for line in lines:
        s = line.strip()
        if not s.startswith("|"):
            continue
        if re.match(r"^\|[\s:\-|]+\|?\s*$", s):  # separator row (|---|---|)
            continue
        rows.append([c.strip() for c in s.strip("|").split("|")])
    return rows


def _bullets(lines):
    """Return logical bullets: each '- '/'* ' item joined with its wrapped
    continuation lines (until the next bullet, a blank line, or a header) so a
    multi-line self.md bullet renders as one complete description rather than a
    truncated first physical line (fresh-eyes-code §3, g-336-11)."""
    out, cur = [], None
    for line in lines:
        s = line.strip()
        if s.startswith(("- ", "* ")):
            if cur is not None:
                out.append(cur)
            cur = s[2:].strip()
        elif cur is not None:
            if not s or s.startswith("#"):
                out.append(cur)
                cur = None
            else:
                cur += " " + s
    if cur is not None:
        out.append(cur)
    return out


# ---- section renderers -------------------------------------------------------

def render_stores(agent):
    text = _read(SOURCES["permissions"])
    if text is None:
        return "_(source unreadable: CLAUDE.md)_\n"
    sec = extract_section(text, r"Tool Usage \+ Write Permissions")
    rows = md_table_rows(sec)
    # First data row is the table header (Path | Permission | Purpose).
    body = [r for r in rows if len(r) >= 3 and r[0].lower() not in ("path",)]
    out = ["Source: `CLAUDE.md` § Tool Usage + Write Permissions "
           "(the Purpose column doubles as the **share scope** — collective vs per-agent).\n",
           "| Path | Permission | Share scope / purpose |",
           "|------|------------|-----------------------|"]
    for r in body:
        path = r[0].replace("<agent>", agent)
        out.append(f"| {path} | {r[1]} | {r[2]} |")
    if not body:
        out.append("| _(permission table not parsed)_ | | |")
    return "\n".join(out) + "\n"


def render_rings(agent):
    text = _read(SOURCES["rings"])
    if text is None:
        return "_(source unreadable: constitutional-rings.md)_\n"
    out = ["Source: `core/config/conventions/constitutional-rings.md` — the ring "
           "determines this agent's **influence right + limit** over each file class.\n"]
    # Each ring is a '## Ring N — Title (modification rule)' header.
    for m in re.finditer(r"^##\s+(Ring\s+\d[^\n]*)$", text, re.MULTILINE):
        title = m.group(1).strip()
        # Governed files: the table under this header.
        sec = extract_section(text, re.escape(title))
        files = [r[0] for r in md_table_rows(sec) if r and r[0].lower() not in ("file",)]
        files = files[:8]
        gov = ", ".join(files) if files else "(see convention)"
        out.append(f"- **{title}**")
        out.append(f"  - governs: {gov}")
    if len(out) == 1:
        out.append("- _(ring sections not parsed)_")
    return "\n".join(out) + "\n"


def render_gates(agent):
    if yaml is None:
        return "_(pyyaml unavailable — cannot render gates.yaml)_\n"
    text = _read(SOURCES["gates"])
    if text is None:
        return "_(source unreadable: core/config/gates.yaml)_\n"
    try:
        data = yaml.safe_load(text) or {}
    except Exception as exc:
        return f"_(gates.yaml parse error: {exc})_\n"
    gates = data.get("gates", []) if isinstance(data, dict) else []
    out = [f"Source: `core/config/gates.yaml` ({len(gates)} active gates). These "
           "guard agent actions; each names its bypass/override flag.\n",
           "| Gate | Script | Override | Fires at (sites) |",
           "|------|--------|----------|------------------|"]
    for g in gates:
        gid = g.get("id", "?")
        script = g.get("script", "")
        override = g.get("override_flag") or "—"
        sites = g.get("sites", []) or []
        nsites = len(sites)
        first = ""
        if sites and isinstance(sites[0], dict):
            f = sites[0].get("file", "").split("/")[-1]
            ph = sites[0].get("phase", "")
            first = f"{f} ({ph})" if ph else f
        site_desc = f"{nsites} site(s)" + (f"; e.g. {first}" if first else "")
        out.append(f"| `{gid}` | `{script}` | `{override}` | {site_desc} |")
    if not gates:
        out.append("| _(no gates parsed)_ | | | |")
    return "\n".join(out) + "\n"


def render_provisionable(agent):
    rel = f"agents/{agent}/self.md"
    text = _read(rel)
    if text is None:
        return None  # signals: agent has no self.md
    prov = extract_section(text, r"Agent-Provisionable Actions")
    work = extract_section(text, r"Primary Workspace")
    out = [f"Source: `agents/{agent}/self.md` § Agent-Provisionable Actions — "
           "actions this agent performs itself (asking the user is a routing violation).\n"]
    bullets = _bullets(prov)
    if bullets:
        for b in bullets[:20]:
            # bullets are joined across wrapped lines; cap at a word boundary so
            # a description never ends on a dangling fragment.
            line = b if len(b) <= 220 else b[:220].rsplit(" ", 1)[0] + " …"
            out.append(f"- {line}")
    else:
        out.append("- _(no Agent-Provisionable Actions section found)_")
    if work:
        wtext = "\n".join(l for l in work[1:] if l.strip())[:400]
        if wtext:
            out.append("\n**Primary workspace** (source: same self.md):")
            out.append(wtext)
    return "\n".join(out) + "\n"


def build_sheet(agent):
    prov = render_provisionable(agent)
    if prov is None:
        return None
    parts = [
        f"# Capability Sheet — {agent}",
        "",
        "_Generated by `core/scripts/agent-capability-sheet.py` — a RENDERED VIEW "
        "of the live source-of-truth files, not a hand-maintained list. Regenerate "
        "anytime; it changes when the sources change. Every section cites its source._",
        "",
        "## 1. Stores — read/write + share scope",
        render_stores(agent),
        "## 2. Influence rights + limits (constitutional rings)",
        render_rings(agent),
        "## 3. Gated actions + the gates that guard them",
        render_gates(agent),
        "## 4. Agent-provisionable actions (this agent)",
        prov,
    ]
    return "\n".join(parts).rstrip() + "\n"


def main(argv):
    if "--list-sources" in argv:
        print("Capability-sheet sources of truth (read live):")
        for key, rel in SOURCES.items():
            present = "ok" if _read(rel) is not None else "MISSING"
            print(f"  [{present}] {key:12} {rel}")
        print("  [per-agent]  provisionable  agents/<agent>/self.md")
        return 0
    args = [a for a in argv if not a.startswith("-")]
    agent = args[0] if args else os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        sys.stderr.write("agent-capability-sheet: no agent given and MIND_AGENT unset\n")
        return 1
    for rel in SOURCES.values():
        if _read(rel) is None:
            sys.stderr.write(f"agent-capability-sheet: core source unreadable: {rel}\n")
            return 2
    sheet = build_sheet(agent)
    if sheet is None:
        sys.stderr.write(f"agent-capability-sheet: no self.md for agent '{agent}'\n")
        return 1
    sys.stdout.write(sheet)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
