"""Suite pin for whole-branch tree retrieval ().

The born-ready vessel's first retrieval for a pack question must reach the
pack's answer node at the depth the agent's /respond uses by default (medium).
The lever is a data mark (`whole_branch: true` on the pack branch node in
_tree.yaml) that retrieve.load_tree_nodes honours at EVERY depth via
tree_match._whole_branch_expand; the installer (provision-env store stage)
writes the mark at pack registration.

This pin drives load_tree_nodes over a pack-shaped tree — the two real
editions of the Ayoai knowledge store (u4 bundles: 24 nodes + 2 pack branch
nodes under intelligence/installed-packs, same shape the installer registers)
plus the Mind seed's 5-node skeleton, with and without the mark. Premise 4
of g-379-10: before this file, no suite-collected test drove load_tree_nodes
over a pack-shaped tree (the embedding-channel tests use a 2-node tmp tree).

The three named tasks are the u5 audit's own (real-editions-u5-20261010,
task ids topic-llm-en-c05 / teachers-assistant-en-c12 / topic-llm-en-c16):
each one's exam question, verbatim as the query, at --depth medium, must
return the task's source node once the mark is present. The unmarked column
pins the pre-fix behaviour (removed 0, additive only — guard-2201's class):
no mark anywhere in the tree => byte-identical retrieval for that tree.

Node key + summary data below is copied verbatim from the u4 bundles
(audit-reports/g-379-01/real-editions-u4-20261010/bundles/.../nodes/*.md
front matter) so the matcher sees the same key words and summaries the
installer would register. Same importlib-against-scratch-world bootstrap as
test_embedding_tree_channel.py.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="whole-branch-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_wholebranch_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import tree_match  # noqa: E402


# ── the tree: seed skeleton + both real packs, the installer's shape ──────────

SEED = {
    "root": {"summary": "Root.", "depth": 0,
             "children": ["execution", "intelligence", "performance", "system"]},
    "execution": {"summary": "Execution knowledge.", "depth": 1, "parent": "root", "children": []},
    "intelligence": {"summary": "Intelligence knowledge.", "depth": 1, "parent": "root",
                     "children": ["installed-packs"]},
    "performance": {"summary": "Performance knowledge.", "depth": 1, "parent": "root", "children": []},
    "system": {"summary": "System knowledge.", "depth": 1, "parent": "root", "children": []},
    "installed-packs": {"summary": "Knowledge packs this mind was born with, one branch per pack; "
                                   "every node below a branch carries its pack mark.",
                        "depth": 2, "parent": "intelligence", "children": []},
}

PACK_A = ("pack-how-language-models-decide-what-to-say-next-ea5a51",
          "Helps an agent explain to its owner how AI chat models write a reply: tokens, "
          "probabilities, how the next token is chosen, training, and why answers vary or go wrong.")
PACK_B = ("pack-teacher-s-assistant-special-education-methods-0d1280",
          "Methods for an agent that helps a special education teacher plan, adapt and check "
          "lessons and supports. Method only: it never needs student data and keeps its work private.")

# key: (summary, confidence) — verbatim from the u4 edition bundle front matter
# (baseline goal ; pinned here for ).
NODES_A = {
    "n01-one-token-at-a-time": ("A model builds its reply one token per step: score every token, turn the scores into probabilities, pick one, append it and repeat, until it ends its turn or hits a limit.", 0.9),
    "n02-tokens": ("A tokenizer cuts text into tokens, usually subword pieces, and numbers them. Tokens explain why limits and prices are counted in tokens and why tasks about single letters trip models up.", 0.9),
    "n03-scores-to-probabilities": ("At each step the model gives every token in its vocabulary a raw score, and softmax turns the scores into probabilities that add up to 1. They say what is likely next, not what is true.", 0.9),
    "n04-context-and-attention": ("A model sees only its training and its context, the text in front of it, capped by a context window counted in tokens. Attention weighs each earlier token by how relevant it is.", 0.9),
    "n05-where-probabilities-come-from": ("Pretraining tunes a model's parameters to predict the next token across a huge amount of text. It learns patterns rather than keeping documents, and its knowledge stops where its data stops.", 0.9),
    "n06-greedy-and-beam-search": ("Greedy decoding always takes the top token and beam search keeps several candidates. Both tend to repeat themselves, and text chosen for maximum probability reads bland and unnatural.", 0.9),
    "n07-sampling-and-temperature": ("Sampling picks the next token at random, weighted by probability. Temperature sharpens the distribution below 1 and flattens it above 1; lower means more consistent, not more correct.", 0.9),
    "n08-top-k-and-top-p": ("Top-k samples from the k most likely tokens; top-p samples from the smallest set whose probabilities reach p, so it adapts at each step. Both cut the unlikely tail that derails text.", 0.9),
    "n09-why-answers-vary": ("Replies differ because of deliberate sampling, a changed context, and serving machinery whose batching shifts the numbers slightly. Different wording is normal; different facts call for a check.", 0.85),
    "n10-from-predictor-to-assistant": ("A pretrained model only continues text. Training on demonstrations and on people's rankings of its answers (RLHF) makes a helpful answer the likely continuation, inside the same loop.", 0.85),
    "n11-planning-and-thinking": ("Research found a model choosing a rhyme before writing its line, and written reasoning steps improve multi-step answers. Those steps are more text from the same loop and are not always faithful.", 0.75),
    "n12-why-models-make-things-up": ("Nothing in the loop checks facts. Research links made-up answers to scoring that rewards guessing and to a \"don't know\" default that false recognition switches off. Context and checks help.", 0.75),
}
NODES_B = {
    "n01-the-assistants-role": ("The teacher plans, teaches and decides; an assistant prepares, reinforces and adapts under that direction, and keeps the students who struggle most close to the teacher.", 0.85),
    "n02-start-from-the-plan": ("Most students in special education have a written plan with goals and supports agreed by a team. Ask what it says for the lesson at hand, without names, before adapting anything.", 0.8),
    "n03-accommodations-and-modifications": ("An accommodation removes a barrier and keeps the expectation the same; a modification changes what the student is taught or expected to learn. Name which one you are suggesting.", 0.85),
    "n04-plan-for-every-learner": ("Universal Design for Learning plans lessons for the range of learners in the room: more than one way to take part, to take in the content and to show what was learned.", 0.8),
    "n05-teach-explicitly": ("Explicit instruction makes a new skill visible: clear examples and non-examples, a model of each step, supported practice, then independent practice with feedback as support is removed.", 0.85),
    "n06-break-a-skill-into-steps": ("Task analysis splits a complex skill into small, ordered, observable steps. Chaining then teaches them from the first step, from the last, or all together.", 0.8),
    "n07-least-help-first": ("Support is temporary. Start with the least help (let the student try, then a prompt, a clue, a model), and plan from the start how the help will come away.", 0.8),
    "n08-measure-progress": ("Progress monitoring uses a short, repeatable measure tied to the goal, graphed against a goal line, so the teacher can see early when teaching needs to change.", 0.8),
    "n09-behaviour-has-a-purpose": ("Challenging behaviour usually serves a function, such as getting attention or escaping a hard task. What happens just before and just after it gives the clues; prevention comes first.", 0.8),
    "n10-predictable-day": ("A predictable schedule, steady routines, clear starts and ends, notice of changes and visual supports that students are taught to use lower anxiety and free attention for learning.", 0.8),
    "n11-specific-feedback-and-praise": ("Praise that names the exact behaviour, given right after it and meant, teaches more than \"good job\". Feedback during practice catches errors before they settle in.", 0.8),
    "n12-accessible-materials": ("Offer materials in formats every student can use, write them in plain language for their reader, and check that the learning goal stays the same.", 0.8),
}

# The u5 audit's three named tasks: exam question verbatim -> source node.
NAMED_TASKS = [
    ("topic-llm-en-c05",
     "For a lesson on next-word prediction I want a worked example with made-up "
     "percentages. After \"I'd like a cup of\", what might a model predict? Give the "
     "top three candidates with their percentages and how much is left over for "
     "everything else.",
     "n03-scores-to-probabilities"),
    ("teachers-assistant-en-c12",
     "I'm a teaching assistant. When a pupil gets stuck partway through a maths "
     "task, what levels of help should I work through, starting from the least help?",
     "n07-least-help-first"),
    ("topic-llm-en-c16",
     "Why are chatbots so willing to guess an answer instead of admitting they don't know?",
     "n12-why-models-make-things-up"),
]


def _build_tree(tmp_path, mark: bool):
    """Write _tree.yaml + node .md files for the pack-shaped tree. `mark` adds
    whole_branch: true to both pack branch nodes (the installer's behaviour
    post-g-379-10)."""
    import yaml
    tree_root = tmp_path / "knowledge" / "tree"
    nodes = {}
    for key, spec in SEED.items():
        nodes[key] = dict(spec)
        nodes[key]["file"] = str(tree_root / (key.replace("-", "/") + ".md" if "/" in key else key + ".md"))
        nodes[key]["confidence"] = 0.5
    for (bkey, bsummary), node_map in ((PACK_A, NODES_A), (PACK_B, NODES_B)):
        nodes["installed-packs"]["children"].append(bkey)
        nodes[bkey] = {"file": str(tree_root / "intelligence" / "installed-packs" / (bkey + ".md")),
                       "summary": bsummary, "confidence": 0.5, "depth": 3,
                       "parent": "installed-packs", "children": []}
        if mark:
            nodes[bkey]["whole_branch"] = True
        for key, (summary, conf) in node_map.items():
            nodes[bkey]["children"].append(key)
            nodes[key] = {"file": str(tree_root / "intelligence" / "installed-packs" / bkey / (key + ".md")),
                          "summary": summary, "confidence": conf, "depth": 4,
                          "parent": bkey, "children": []}
    for key, node in nodes.items():
        p = Path(node["file"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\n---\nbody\n", encoding="utf-8")
    tree_yaml = tmp_path / "_tree.yaml"
    tree_yaml.write_text(yaml.safe_dump({"last_updated": "2026-10-11",
                                         "nodes": nodes, "entity_index": {}}, sort_keys=True),
                         encoding="utf-8")
    return nodes, tree_yaml


@pytest.fixture()
def unmarked_tree(tmp_path):
    nodes, tree_yaml = _build_tree(tmp_path, mark=False)
    saved = _retrieve.TREE_PATH
    _retrieve.TREE_PATH = tree_yaml
    yield nodes, tree_yaml
    _retrieve.TREE_PATH = saved


@pytest.fixture()
def marked_tree(tmp_path):
    nodes, tree_yaml = _build_tree(tmp_path, mark=True)
    saved = _retrieve.TREE_PATH
    _retrieve.TREE_PATH = tree_yaml
    yield nodes, tree_yaml
    _retrieve.TREE_PATH = saved


def _keys(nodes, tree_yaml, query, depth="medium"):
    saved = _retrieve.TREE_PATH
    _retrieve.TREE_PATH = tree_yaml
    try:
        results, _channels = _retrieve.load_tree_nodes([query], depth, read_only=True)
    finally:
        _retrieve.TREE_PATH = saved
    return [r["key"] for r in results], {r["key"]: r["match_channel"] for r in results}


# ── unit tests of the mechanism ───────────────────────────────────────────────

def test_whole_branch_score_pinned_like_sibling():
    assert tree_match.CHANNEL_SCORES["whole_branch"] == 1.0
    assert tree_match.CHANNEL_SCORES["whole_branch"] == tree_match.CHANNEL_SCORES["sibling"]


def test_expand_no_mark_is_noop():
    nodes = {
        "root": {"depth": 0, "children": ["a"]},
        "a": {"depth": 1, "parent": "root", "children": ["a1", "a2"]},
        "a1": {"depth": 2, "parent": "a", "children": []},
        "a2": {"depth": 2, "parent": "a", "children": []},
    }
    matched, keys, channels = [("a1", nodes["a1"])], {"a1"}, {"a1": "word_prefix"}
    out_keys, out_keys_set, out_channels, added = tree_match._whole_branch_expand(
        matched, keys, channels, nodes)
    assert [k for k, _ in out_keys] == ["a1"]
    assert added == []
    assert out_channels == {"a1": "word_prefix"}


def test_expand_marked_branch_pulls_all_children_direct_channels_only():
    nodes = {
        "root": {"depth": 0, "children": ["branch"]},
        "branch": {"depth": 1, "parent": "root", "children": ["c1", "c2"], "whole_branch": True},
        "c1": {"depth": 2, "parent": "branch", "children": []},
        "c2": {"depth": 2, "parent": "branch", "children": []},
    }
    # c1 direct (word_prefix); c2 arrives as a sibling channel — must NOT re-trigger
    matched, keys, channels = [("c1", nodes["c1"]), ("c2", nodes["c2"])], {"c1", "c2"}, \
                              {"c1": "word_prefix", "c2": "sibling"}
    _m, _k, out_channels, added = tree_match._whole_branch_expand(matched, keys, channels, nodes)
    assert out_channels["c1"] == "word_prefix"
    assert out_channels["c2"] == "sibling"
    assert added == []


def test_expand_mark_on_matched_node_itself():
    """A node whose own key is marked pulls its children (walk starts at self)."""
    nodes = {
        "branch": {"depth": 1, "parent": "root", "children": ["c1"], "whole_branch": True},
        "c1": {"depth": 2, "parent": "branch", "children": []},
    }
    matched, keys, channels = [("branch", nodes["branch"])], {"branch"}, {"branch": "exact_key"}
    _m, _k, out_channels, added = tree_match._whole_branch_expand(matched, keys, channels, nodes)
    assert added == ["c1"]
    assert out_channels["c1"] == "whole_branch"


def test_expand_no_recursion_from_included_nodes():
    """A node ADDED by the expansion (channel whole_branch) never triggers
    further expansion, even when it is itself marked — expansion fires on
    direct channels only. x1 is direct and marked: it adds x1a; x1a is
    marked too, but it was included on the whole_branch channel, so its
    child x1a1 stays out."""
    nodes = {
        "b": {"depth": 1, "parent": "root", "children": ["x", "y"], "whole_branch": True},
        "x": {"depth": 2, "parent": "b", "children": ["x1"]},
        "x1": {"depth": 3, "parent": "x", "children": ["x1a"], "whole_branch": True},
        "x1a": {"depth": 4, "parent": "x1", "children": ["x1a1"], "whole_branch": True},
        "x1a1": {"depth": 5, "parent": "x1a", "children": []},
        "y": {"depth": 2, "parent": "b", "children": []},
    }
    matched, keys, channels = [("x1", nodes["x1"])], {"x1"}, {"x1": "substring"}
    _m, _k, out_channels, added = tree_match._whole_branch_expand(matched, keys, channels, nodes)
    assert added == ["x1a"]
    assert out_channels["x1a"] == "whole_branch"
    assert "x1a1" not in out_channels
    # and the nearest-mark rule: b is marked too, but x1's own mark is
    # closer, so b's other children (x, y) are not pulled
    assert "y" not in out_channels


def test_expand_terminates_on_parent_cycle():
    """A malformed parent cycle (or self-parent) must end the ancestor walk,
    not hang retrieval: cycle-safe like tree_match._path_chain. The call runs
    in a watchdog thread so a regression FAILS instead of hanging the suite.
    The marked-cycle case pins that the guard still finds a mark inside it."""
    import threading
    cases = {
        "two-cycle, no mark": (
            {"p": {"depth": 1, "parent": "q", "children": ["q"]},
             "q": {"depth": 1, "parent": "p", "children": ["p"]}},
            "p", []),
        "self-parent, no mark": (
            {"p": {"depth": 1, "parent": "p", "children": []}},
            "p", []),
        "descendant of a cycle, no mark": (
            {"p": {"depth": 1, "parent": "q", "children": ["q", "c"]},
             "q": {"depth": 1, "parent": "p", "children": ["p"]},
             "c": {"depth": 2, "parent": "p", "children": []}},
            "c", []),
        "mark inside the cycle": (
            {"p": {"depth": 1, "parent": "q", "children": ["q"]},
             "q": {"depth": 1, "parent": "p", "children": ["p", "d"], "whole_branch": True},
             "d": {"depth": 2, "parent": "q", "children": []}},
            "p", ["d"]),
    }
    for label, (nodes, hit, want) in cases.items():
        box = {}

        def run(nodes=nodes, hit=hit, box=box):
            box["r"] = tree_match._whole_branch_expand(
                [(hit, nodes[hit])], {hit}, {hit: "exact_key"}, nodes)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(10)
        assert not t.is_alive(), label + ": _whole_branch_expand did not terminate"
        assert box["r"][3] == want, label


# ── the load_tree_nodes pin over the pack-shaped tree ─────────────────────────

def test_unmarked_tree_reaches_pre_fix_floor(unmarked_tree):
    """The 3 named tasks WITHOUT the mark: pins the pre-fix behaviour so the
    pin cannot pass vacuously (at least one must be a medium miss pre-fix, and
    the sets pin the additive-only base)."""
    nodes, tree_yaml = unmarked_tree
    results = []
    for _tid, query, source in NAMED_TASKS:
        keys, _ch = _keys(nodes, tree_yaml, query, "medium")
        results.append((source, source in keys, frozenset(keys)))
    # c05 and c16 miss at medium pre-fix (u5: 19/28 verbatim floor), c12 hits.
    assert results[0][1] is False
    assert results[1][1] is True
    assert results[2][1] is False


def test_marked_tree_reaches_all_three_named_tasks_at_medium(marked_tree):
    """THE pin: each named task's source node is returned at --depth medium
    (the /respond default) when the branch carries the mark — and the pack's
    whole node set comes back with it."""
    nodes, tree_yaml = marked_tree
    for tid, query, source in NAMED_TASKS:
        keys, _ch = _keys(nodes, tree_yaml, query, "medium")
        assert source in keys, f"{tid}: {source} not in medium result ({len(keys)} keys)"
        assert len(keys) <= _retrieve.DEPTH_LIMITS["medium"]


def test_marked_c05_returns_whole_pack_a_and_pack_b(marked_tree):
    """topic-llm-en-c05 marked: every pack-A node returned (the question's
    source branch pulls its 12 children) plus the direct pack-B hits — and
    NOT the seed skeleton's unrelated branches."""
    nodes, tree_yaml = marked_tree
    _tid, query, _source = NAMED_TASKS[0]
    keys, channels = _keys(nodes, tree_yaml, query, "medium")
    assert set(NODES_A) <= set(keys), "pack A not whole at medium"
    # the pack-A nodes that were NOT direct hits arrived on the new channel
    direct = {"n04-plan-for-every-learner", "n10-from-predictor-to-assistant",
              "n10-predictable-day", "n12-why-models-make-things-up"}
    added = [k for k in keys if k in set(NODES_A) - direct]
    assert len(added) >= 8
    assert all(channels[k] == "whole_branch" for k in added)
    # seed skeleton stays out of the pack question's result
    assert not ({"execution", "performance", "system"} & set(keys))


def test_marked_c16_only_pack_a_nodes_arrive(marked_tree):
    """topic-llm-en-c16 marked: the single direct hit (pack A's
    n09-why-answers-vary — 'different facts call for a check') pulls pack
    A's whole branch; nothing of pack B is in the result (no cross-pack
    expansion — each mark governs only its own subtree)."""
    nodes, tree_yaml = marked_tree
    _tid, query, _source = NAMED_TASKS[2]
    keys, _ch = _keys(nodes, tree_yaml, query, "medium")
    assert set(NODES_A) <= set(keys)
    assert not (set(NODES_B) & set(keys))
    assert len(keys) == 12


def test_additive_only_mark_never_removes(marked_tree, unmarked_tree):
    """guard-2201's additive class, per task: marked set ⊇ unmarked set for
    every named task at both depths the goal measured."""
    u_nodes, u_yaml = unmarked_tree
    m_nodes, m_yaml = marked_tree
    for _tid, query, _source in NAMED_TASKS:
        for depth in ("medium", "deep"):
            u_keys, _ = _keys(u_nodes, u_yaml, query, depth)
            m_keys, _ = _keys(m_nodes, m_yaml, query, depth)
            assert set(u_keys) <= set(m_keys), \
                f"removed {set(u_keys) - set(m_keys)} at {depth}"


def test_noop_when_mark_absent(unmarked_tree, monkeypatch):
    """The no-op guarantee is in the DATA, not the code path: with no mark in
    the tree, running the new engine stage adds nothing — every named task's
    set at both measured depths is identical with the stage enabled (today's
    engine) and disabled (pre-g-379-10 engine)."""
    nodes, tree_yaml = unmarked_tree

    def _identity(matched, matched_keys, channels, all_nodes):
        return matched, matched_keys, channels, []

    saved = _retrieve.TREE_PATH
    _retrieve.TREE_PATH = tree_yaml
    try:
        for _tid, query, _source in NAMED_TASKS:
            for depth in ("medium", "deep"):
                _retrieve._whole_branch_expand = tree_match._whole_branch_expand
                r2, _ = _retrieve.load_tree_nodes([query], depth, read_only=True)
                keys2 = frozenset(x["key"] for x in r2)
                monkeypatch.setattr(_retrieve, "_whole_branch_expand", _identity)
                r1, _ = _retrieve.load_tree_nodes([query], depth, read_only=True)
                keys1 = frozenset(x["key"] for x in r1)
                _retrieve._whole_branch_expand = tree_match._whole_branch_expand
                assert keys1 == keys2, \
                    f"no-op violated at {depth} for {query[:30]}...: {keys1 ^ keys2}"
    finally:
        _retrieve.TREE_PATH = saved
        _retrieve._whole_branch_expand = tree_match._whole_branch_expand
