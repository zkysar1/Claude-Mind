#!/usr/bin/env python3
"""tree-visualize.py -- Generate a self-contained, read-only HTML visualizer of
the knowledge tree as an interactive node graph (hierarchy + co-reference
backlinks).

Reuses the prebuilt knowledge-graph triple store (meta/knowledge-graph.jsonl)
for cross-references rather than re-deriving links. Emits ONE .html file with
embedded JSON data + vanilla JS/CSS: NO external dependencies, NO network
calls, NO node mutation. A read-only observability / onboarding tool for a
large knowledge tree.

The tree stores hierarchy (parent/children) but no direct node->node links;
cross-tree connectivity is derived by REVERSING the node->entity reference
edges (entity -> {nodes that cite it}) and surfacing nodes that share an
entity. That reversal is the "reverse the link graph for backlinks" idea
applied at the entity level.

Spawned by the OKF feature evaluation Item 2 GO (g-306-55 -> g-306-56).

Usage:
  tree-visualize.py                          # -> agents/<agent>/temp/knowledge-tree.html
  tree-visualize.py --output path/to.html    # custom output path
  tree-visualize.py --max-coref 12           # cap co-referenced neighbors per node
  tree-visualize.py --output json            # machine-readable summary to stdout (no HTML)
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, META_DIR, agent_dir  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a hard dependency of the tree store
    yaml = None

_TREE_REL = ("knowledge", "tree", "_tree.yaml")
_GRAPH_NAME = "knowledge-graph.jsonl"
# Per-node fields surfaced in the detail panel (kept small to bound payload size).
_NODE_FIELDS = (
    "summary", "file", "capability_level", "retrieval_count",
    "last_updated", "growth_state", "node_type", "depth", "article_count",
)


def _tree_path() -> Path:
    return Path(WORLD_DIR).joinpath(*_TREE_REL)


def _graph_path() -> Path:
    return Path(META_DIR) / _GRAPH_NAME


def load_tree(path: Path) -> dict:
    """Return the {key: node-dict} map from _tree.yaml."""
    if yaml is None:
        raise RuntimeError("PyYAML is required to read _tree.yaml")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    nodes = data.get("nodes") or {}
    if not isinstance(nodes, dict):
        raise RuntimeError("_tree.yaml 'nodes' is not a mapping")
    return nodes


def load_tree_references(path: Path):
    """Parse tree-store 'references' triples.

    Returns (node_to_entities, entity_to_nodes) where node keys are the bare
    tree-node keys (the 'node:' prefix is stripped) and entities are the cited
    goal/rb/aspiration IDs.
    """
    node_to_entities: dict[str, set[str]] = {}
    entity_to_nodes: dict[str, set[str]] = {}
    if not path.exists():
        return node_to_entities, entity_to_nodes
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t.get("p") != "references" or t.get("store") != "tree":
                continue
            subj = t.get("s") or ""
            obj = t.get("o") or ""
            if not subj.startswith("node:") or not obj:
                continue
            # Graph keys nodes by full path (node:l1/.../leaf); _tree.yaml keys by
            # bare leaf. Map to the leaf so references join the tree node map.
            nkey = subj[len("node:"):].rsplit("/", 1)[-1]
            node_to_entities.setdefault(nkey, set()).add(obj)
            entity_to_nodes.setdefault(obj, set()).add(nkey)
    return node_to_entities, entity_to_nodes


def compute_backlinks(node_to_entities, entity_to_nodes, max_coref):
    """For each node, rank co-referencing nodes by count of shared entities.

    This is the reversed-edge derivation: two nodes that cite the same entity
    are conceptually linked. Returns {node_key: [[other_key, shared_count], ...]}.
    """
    backlinks: dict[str, list] = {}
    for nkey, entities in node_to_entities.items():
        shared: dict[str, int] = {}
        for ent in entities:
            for other in entity_to_nodes.get(ent, ()):  # type: ignore[union-attr]
                if other == nkey:
                    continue
                shared[other] = shared.get(other, 0) + 1
        ranked = sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))[:max_coref]
        if ranked:
            backlinks[nkey] = [[k, c] for k, c in ranked]
    return backlinks


def l1_domain(key: str, nodes: dict) -> str:
    """Walk the parent chain to the top-level (L1) ancestor under root."""
    seen = set()
    cur = key
    last = key
    while cur and cur not in seen:
        seen.add(cur)
        node = nodes.get(cur) or {}
        parent = node.get("parent")
        if parent in (None, "root", ""):
            return cur if parent == "root" else last
        last = cur
        cur = parent
    return last


def build_payload(nodes, node_to_entities, backlinks, max_coref):
    """Assemble the compact JSON payload embedded in the HTML."""
    out_nodes = {}
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        rec = {f: node.get(f) for f in _NODE_FIELDS if node.get(f) is not None}
        rec["children"] = list(node.get("children") or [])
        rec["parent"] = node.get("parent")
        rec["l1"] = l1_domain(key, nodes)
        refs = sorted(node_to_entities.get(key, ()))
        if refs:
            rec["refs"] = refs
        bl = backlinks.get(key)
        if bl:
            bl = [[k, c] for k, c in bl if k in nodes]  # drop orphan (stale) targets
            if bl:
                rec["backlinks"] = bl
        out_nodes[key] = rec
    roots = sorted(
        k for k, n in nodes.items()
        if isinstance(n, dict) and n.get("parent") in (None, "")
    )
    total_edges = sum(len(n.get("children") or []) for n in nodes.values() if isinstance(n, dict))
    return {
        "nodes": out_nodes,
        "roots": roots,
        "stats": {
            "node_count": len(out_nodes),
            "hierarchy_edges": total_edges,
            "nodes_with_refs": sum(1 for k in out_nodes if "refs" in out_nodes[k]),
            "nodes_with_backlinks": sum(1 for k in out_nodes if "backlinks" in out_nodes[k]),
            "max_coref": max_coref,
        },
    }


def render_html(payload: dict) -> str:
    """Wrap the payload in a self-contained HTML document (no external deps)."""
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # Defend against an accidental </script> inside any summary string.
    data_json = data_json.replace("</", "<\\/")
    stats = payload["stats"]
    return _HTML_TEMPLATE.replace("__DATA__", data_json).replace(
        "__SUBTITLE__",
        f"{stats['node_count']} nodes &middot; {stats['hierarchy_edges']} hierarchy edges &middot; "
        f"{stats['nodes_with_backlinks']} nodes with derived backlinks",
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate a read-only HTML knowledge-tree visualizer.")
    ap.add_argument("--output", default=None,
                    help="Output .html path, or the literal 'json' for a stdout summary "
                         "(default: agents/<agent>/temp/knowledge-tree.html).")
    ap.add_argument("--max-coref", type=int, default=12,
                    help="Max co-referenced neighbor nodes per node (default 12).")
    args = ap.parse_args(argv)

    tree_path = _tree_path()
    if not tree_path.exists():
        print(f"tree-visualize: tree not found at {tree_path}", file=sys.stderr)
        return 2
    nodes = load_tree(tree_path)
    node_to_entities, entity_to_nodes = load_tree_references(_graph_path())
    backlinks = compute_backlinks(node_to_entities, entity_to_nodes, max(1, args.max_coref))
    payload = build_payload(nodes, node_to_entities, backlinks, args.max_coref)

    if args.output == "json":
        print(json.dumps(payload["stats"], indent=2))
        return 0

    if args.output:
        out_path = Path(args.output)
    else:
        agent = os.environ.get("MIND_AGENT", "").strip()
        if not agent:
            print("tree-visualize: MIND_AGENT unset and no --output given", file=sys.stderr)
            return 2
        out_path = agent_dir(agent) / "temp" / "knowledge-tree.html"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_doc = render_html(payload)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(html_doc, encoding="utf-8")
    os.replace(tmp, out_path)

    s = payload["stats"]
    print(f"tree-visualize: wrote {out_path}")
    print(f"  nodes={s['node_count']} hierarchy_edges={s['hierarchy_edges']} "
          f"backlinked_nodes={s['nodes_with_backlinks']} bytes={out_path.stat().st_size}")
    return 0


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge Tree Visualizer</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --ink:#d7dbe0; --dim:#878e99; --accent:#6ca0ff; --edge:#3a4252; --hl:#ffcf6c; }
  * { box-sizing:border-box; }
  body { margin:0; font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--ink); }
  header { padding:10px 16px; border-bottom:1px solid var(--edge); display:flex; gap:14px; align-items:baseline; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header .sub { color:var(--dim); font-size:12px; }
  #search { margin-left:auto; background:var(--panel); border:1px solid var(--edge); color:var(--ink); padding:5px 9px; border-radius:6px; width:240px; }
  .wrap { display:flex; height:calc(100vh - 49px); }
  #tree { width:42%; overflow:auto; padding:8px 4px 40px 8px; border-right:1px solid var(--edge); }
  #detail { flex:1; overflow:auto; padding:14px 18px 40px; }
  ul.tree { list-style:none; margin:0; padding-left:14px; }
  ul.tree.root { padding-left:2px; }
  li.node { margin:1px 0; }
  .row { display:flex; align-items:center; gap:4px; padding:1px 4px; border-radius:5px; cursor:pointer; }
  .row:hover { background:#222732; }
  .row.sel { background:#2b3344; outline:1px solid var(--accent); }
  .twist { width:13px; text-align:center; color:var(--dim); user-select:none; flex:none; }
  .twist.leaf { color:transparent; }
  .label { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .kids { display:none; }
  li.open > .kids { display:block; }
  .cap { font-size:10px; color:var(--dim); border:1px solid var(--edge); border-radius:4px; padding:0 4px; margin-left:4px; flex:none; }
  .hidden { display:none !important; }
  .match > .row > .label { color:var(--hl); }
  h2.key { margin:0 0 2px; font-size:17px; color:var(--accent); word-break:break-all; }
  .crumbs { color:var(--dim); font-size:11px; margin-bottom:10px; }
  .meta { display:flex; flex-wrap:wrap; gap:6px 14px; margin:8px 0 12px; color:var(--dim); font-size:12px; }
  .meta b { color:var(--ink); font-weight:500; }
  .summary { background:var(--panel); border:1px solid var(--edge); border-radius:8px; padding:10px 12px; white-space:pre-wrap; }
  .sect { margin-top:16px; }
  .sect h3 { font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); margin:0 0 6px; }
  .chips { display:flex; flex-wrap:wrap; gap:5px; }
  .chip { background:var(--panel); border:1px solid var(--edge); border-radius:12px; padding:2px 9px; font-size:12px; }
  .chip.node { cursor:pointer; }
  .chip.node:hover { border-color:var(--accent); color:var(--accent); }
  .chip .ct { color:var(--dim); margin-left:5px; font-size:10px; }
  svg { background:#0c0e12; border:1px solid var(--edge); border-radius:8px; width:100%; height:300px; }
  .ego-node { cursor:pointer; }
  .placeholder { color:var(--dim); margin-top:40px; text-align:center; }
  footer { color:var(--dim); font-size:11px; padding:8px 16px; border-top:1px solid var(--edge); }
</style>
</head>
<body>
<header>
  <h1>Knowledge Tree Visualizer</h1>
  <span class="sub">__SUBTITLE__</span>
  <input id="search" type="search" placeholder="filter nodes (key / summary)..." autocomplete="off">
</header>
<div class="wrap">
  <div id="tree"></div>
  <div id="detail"><div class="placeholder">Select a node to inspect its summary, references, and derived backlinks.</div></div>
</div>
<footer>Read-only. Generated from <code>_tree.yaml</code> + <code>knowledge-graph.jsonl</code>. No network, no mutation.</footer>
<script>
"use strict";
const DATA = __DATA__;
const N = DATA.nodes, ROOTS = DATA.roots;
const treeEl = document.getElementById("tree");
const detailEl = document.getElementById("detail");
let selected = null;

function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c])); }

function makeNode(key){
  const node = N[key]; if(!node) return null;
  const li = document.createElement("li");
  li.className = "node"; li.dataset.key = key;
  const kids = node.children || [];
  const row = document.createElement("div");
  row.className = "row";
  const tw = document.createElement("span");
  tw.className = "twist" + (kids.length ? "" : " leaf");
  tw.textContent = kids.length ? "▸" : "·";
  const lab = document.createElement("span");
  lab.className = "label"; lab.textContent = key;
  row.appendChild(tw);
  row.appendChild(lab);
  if(node.capability_level){ const c=document.createElement("span"); c.className="cap"; c.textContent=node.capability_level; row.appendChild(c); }
  li.appendChild(row);
  row.addEventListener("click", e=>{
    if(e.target===tw && kids.length){ li.classList.toggle("open"); return; }
    select(key);
    if(kids.length && !li.classList.contains("open")) li.classList.add("open");
  });
  if(kids.length){
    const ul = document.createElement("ul");
    ul.className = "tree kids";
    kids.slice().sort().forEach(c=>{ const ch=makeNode(c); if(ch) ul.appendChild(ch); });
    li.appendChild(ul);
  }
  return li;
}

function renderTree(){
  const ul = document.createElement("ul");
  ul.className = "tree root";
  ROOTS.forEach(r=>{ const n=makeNode(r); if(n){ n.classList.add("open"); ul.appendChild(n); } });
  treeEl.innerHTML = ""; treeEl.appendChild(ul);
}

function pathTo(key){
  const chain=[]; let cur=key, guard=0;
  while(cur && guard++<64){ chain.unshift(cur); cur=(N[cur]||{}).parent; }
  return chain;
}

function revealAndSelect(key){
  if(!N[key]) return;
  const chain = pathTo(key);
  chain.forEach(k=>{ const li=treeEl.querySelector('li.node[data-key="'+CSS.escape(k)+'"]'); if(li) li.classList.add("open"); });
  const li=treeEl.querySelector('li.node[data-key="'+CSS.escape(key)+'"]');
  if(li){ li.scrollIntoView({block:"center"}); }
  select(key);
}

function egoSvg(key){
  const node=N[key]||{}; const nb=(node.backlinks||[]).slice(0,10);
  const W=560,H=300,cx=W/2,cy=H/2,R=110;
  let s='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet">';
  nb.forEach((b,i)=>{ const a=(2*Math.PI*i)/nb.length - Math.PI/2; const x=cx+R*Math.cos(a), y=cy+R*Math.sin(a);
    s+='<line x1="'+cx+'" y1="'+cy+'" x2="'+x+'" y2="'+y+'" stroke="#3a4252" stroke-width="1"/>'; });
  nb.forEach((b,i)=>{ const a=(2*Math.PI*i)/nb.length - Math.PI/2; const x=cx+R*Math.cos(a), y=cy+R*Math.sin(a);
    const short=b[0].length>16?b[0].slice(0,15)+"…":b[0];
    s+='<g class="ego-node" data-key="'+esc(b[0])+'"><circle cx="'+x+'" cy="'+y+'" r="5" fill="#6ca0ff"/>'+
       '<text x="'+x+'" y="'+(y- (Math.sin(a)<0?9:-15))+'" fill="#878e99" font-size="10" text-anchor="middle">'+esc(short)+' ('+b[1]+')</text></g>'; });
  s+='<circle cx="'+cx+'" cy="'+cy+'" r="8" fill="#ffcf6c"/>';
  const sk=key.length>22?key.slice(0,21)+"…":key;
  s+='<text x="'+cx+'" y="'+(cy+22)+'" fill="#ffcf6c" font-size="11" text-anchor="middle">'+esc(sk)+'</text></svg>';
  return s;
}

function select(key){
  const node=N[key]; if(!node) return;
  selected=key;
  treeEl.querySelectorAll(".row.sel").forEach(r=>r.classList.remove("sel"));
  const li=treeEl.querySelector('li.node[data-key="'+CSS.escape(key)+'"]');
  if(li) li.querySelector(".row").classList.add("sel");
  const crumbs = pathTo(key).join("  ›  ");
  const refs = node.refs||[], bl = node.backlinks||[];
  let h = '<h2 class="key">'+esc(key)+'</h2>';
  h += '<div class="crumbs">'+esc(crumbs)+'</div>';
  h += '<div class="meta">';
  if(node.l1) h+='<span>domain <b>'+esc(node.l1)+'</b></span>';
  if(node.capability_level) h+='<span>capability <b>'+esc(node.capability_level)+'</b></span>';
  if(node.growth_state) h+='<span>growth <b>'+esc(node.growth_state)+'</b></span>';
  if(node.retrieval_count!=null) h+='<span>retrievals <b>'+esc(node.retrieval_count)+'</b></span>';
  if(node.last_updated) h+='<span>updated <b>'+esc(node.last_updated)+'</b></span>';
  if(node.file) h+='<span>file <b>'+esc(node.file)+'</b></span>';
  h += '</div>';
  h += '<div class="summary">'+esc(node.summary||"(no summary)")+'</div>';
  if(bl.length){
    h += '<div class="sect"><h3>Derived backlinks &mdash; nodes sharing references ('+bl.length+')</h3>';
    h += egoSvg(key);
    h += '<div class="chips" style="margin-top:10px">';
    bl.forEach(b=>{ h+='<span class="chip node" data-key="'+esc(b[0])+'">'+esc(b[0])+'<span class="ct">'+b[1]+'</span></span>'; });
    h += '</div></div>';
  }
  if(refs.length){
    h += '<div class="sect"><h3>References out &mdash; entities cited ('+refs.length+')</h3><div class="chips">';
    refs.forEach(r=>{ h+='<span class="chip">'+esc(r)+'</span>'; });
    h += '</div></div>';
  }
  if((node.children||[]).length){
    h += '<div class="sect"><h3>Children ('+node.children.length+')</h3><div class="chips">';
    node.children.slice().sort().forEach(c=>{ h+='<span class="chip node" data-key="'+esc(c)+'">'+esc(c)+'</span>'; });
    h += '</div></div>';
  }
  detailEl.innerHTML = h;
  detailEl.scrollTop = 0;
  detailEl.querySelectorAll(".chip.node").forEach(el=>el.addEventListener("click",()=>revealAndSelect(el.dataset.key)));
  detailEl.querySelectorAll(".ego-node").forEach(el=>el.addEventListener("click",()=>revealAndSelect(el.dataset.key)));
}

let searchTimer=null;
document.getElementById("search").addEventListener("input", e=>{
  clearTimeout(searchTimer);
  const q=e.target.value.trim().toLowerCase();
  searchTimer=setTimeout(()=>applyFilter(q), 120);
});
function applyFilter(q){
  const lis = treeEl.querySelectorAll("li.node");
  if(!q){ lis.forEach(li=>{ li.classList.remove("hidden","match"); }); return; }
  // mark matches, then reveal matches + their ancestors
  const keep=new Set();
  lis.forEach(li=>{
    const key=li.dataset.key, node=N[key]||{};
    const hit = key.toLowerCase().includes(q) || String(node.summary||"").toLowerCase().includes(q);
    li.classList.toggle("match", hit);
    if(hit){ let c=key, g=0; while(c && g++<64){ keep.add(c); c=(N[c]||{}).parent; } }
  });
  lis.forEach(li=>{
    const show = keep.has(li.dataset.key);
    li.classList.toggle("hidden", !show);
    if(show && (N[li.dataset.key]||{}).children && N[li.dataset.key].children.length) li.classList.add("open");
  });
}

renderTree();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
