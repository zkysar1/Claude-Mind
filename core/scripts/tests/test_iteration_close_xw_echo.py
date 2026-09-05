"""do_verify's Completed: coordination post is ADDRESSED to the peer deployment
when the closed goal carries a peer origin (g-361-03). Structural pins over
iteration-close.sh (the close path needs a live daemon to run end-to-end;
xw_origin's resolution has its own functional tests in test_xw_origin.py).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bash_helpers import resolve_winpath  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "iteration-close.sh"


def _do_verify_body() -> str:
    # Resolve the `_winpath` path helper before pinning the call shape --
    # see _bash_helpers.resolve_winpath (a normalizer, not a loosened pattern).
    src = resolve_winpath(SCRIPT.read_text(encoding="utf-8"))
    start = src.index("do_verify() {")
    end = src.index("\ndo_state_update() {", start)
    return src[start:end]


def test_completed_post_resolves_peer_origin_through_xw_origin():
    body = _do_verify_body()
    assert 'python3 "$SCRIPT_DIR/xw_origin.py" --goal "$GOAL_ID"' in body
    # the lookup sits INSIDE the world+completed branch, before the post
    branch = body[body.index('if [[ "$SOURCE" == "world" && "$GOAL_STATUS" == "completed" ]]; then'):]
    branch = branch[:branch.index("board-post.sh") + 200]
    assert "xw_origin.py" in branch


def test_peer_addressed_post_carries_both_address_forms_and_the_default_stays_bare():
    body = _do_verify_body()
    # convention: bare `<agent>` tag (installed base) AND requires_action_by:<agent>@<env-id>
    assert 'post_tags="${post_tags},${xw_agent},requires_action_by:${xw_addr}"' in body
    assert 'post_tags="$GOAL_ID,cross-deployment,xw-completion,${xw_env}"' in body
    # a goal with no peer origin posts exactly what it posted before
    assert 'local post_tags="$GOAL_ID"' in body
    assert '--type complete --tags "$post_tags"' in body
    # env-only origin (no agent to address) must NOT fabricate a requires_action_by
    assert re.search(r'if \[\[ "\$xw_addr" != "-" \]\]; then', body)


def test_echo_asks_for_an_acknowledging_reply():
    body = _do_verify_body()
    assert "please --reply-to this post to acknowledge" in body
    assert "cross-world echo to ${xw_addr}" in body
