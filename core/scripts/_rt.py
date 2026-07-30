#!/usr/bin/env python3
"""Canonical Python -> daemon client. The sys.executable/urllib analog of
_runtime.sh's rt_call, for framework *.py scripts that must invoke a
daemon-migrated operation.

Why this exists: the 2026-05-14 cutover deleted the migrated CLI subcommands
(aspirations.py add-goal/read, wm.py read, ...). Framework Python scripts
called those via subprocess([sys.executable, X.py, subcmd, ...]) (the
guard-468 pattern) because a Python child cannot reach the daemon-aware .sh
wrapper on Windows -- `bash` resolves to WSL bash, which cannot open a
C:\\ drive path (rb-225/rb-247, verified 2026-05-15: every bash invocation
form fails rc=127 / WinError 193). The cutover removed the CLI path without
a Python->daemon replacement; this module is that replacement.

Posture (.claude/rules/no-python-cli-fallback.md): daemon-only. If the daemon
is unreachable this RAISES with a diagnostic -- there is no CLI fallback. The
SessionStart hook (d3e5e6e) proactively spawns the daemon, so any framework
session has it up; a hard failure here means the daemon genuinely needs
attention, not a silent degrade.

Wire behavior is byte-identical to _runtime.sh rt_curl (same URL, headers,
query, raw -d body) so the daemon sees one contract regardless of client.
"""
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class RtError(RuntimeError):
    """Daemon unreachable, or answered 4xx/5xx. No fallback is attempted."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _port():
    # Resolution mirrors _runtime.sh exactly: RT_PORT_FILE, else
    # RT_DIR/daemon.port, else PROJECT_ROOT/mind_api/state/daemon.port.
    pf = os.environ.get("RT_PORT_FILE")
    if not pf:
        rt_dir = os.environ.get("RT_DIR") or str(_PROJECT_ROOT / "mind_api" / "state")
        pf = str(pathlib.Path(rt_dir) / "daemon.port")
    try:
        return pathlib.Path(pf).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def rt_call(method, path, query=None, body=None, headers=None):
    """Call the daemon; return the response body (str) on HTTP 2xx.

    query: str ("a=1&b=2") or dict (urlencoded). body: str (sent raw, like
    curl -d). Raises RtError on 4xx/5xx or unreachable -- never falls back.
    """
    port = _port()
    if not port:
        raise RtError(
            "daemon not reachable: no mind_api/state/daemon.port. Ensure the daemon "
            "is running (SessionStart hook / core/scripts/mind-api-start.sh). "
            "Daemon-only: no CLI fallback (.claude/rules/no-python-cli-fallback.md)."
        )
    url = "http://127.0.0.1:%s%s" % (port, path)
    if query:
        if not isinstance(query, str):
            query = urllib.parse.urlencode(query)
        url += "?" + query

    h = {
        "X-Runtime-Client": "python",
        "X-Mind-Tenant": os.environ.get("MIND_TENANT", "default"),
    }
    agent = os.environ.get("MIND_AGENT")
    if agent:
        h["X-Mind-Agent"] = agent
    # FR-4 (BRD Shared-State-API): attach the daemon bearer token when
    # MIND_API_TOKEN is set (parity with _runtime.sh rt_curl). Unset → no header
    # → byte-identical to today's localhost-only calls.
    _api_token = os.environ.get("MIND_API_TOKEN", "").strip()
    if _api_token:
        h["Authorization"] = "Bearer " + _api_token
    if headers:
        h.update(headers)

    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        # curl -d default; daemon reads the raw body regardless. Matching the
        # header keeps the wire identical to the .sh client.
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=data, method=method, headers=h)
    timeout = float(os.environ.get("RT_CURL_TIMEOUT", "30"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")
        raise RtError(
            "daemon HTTP %s for %s %s" % (e.code, method, path),
            status=e.code, body=err_body)
    except urllib.error.URLError as e:
        raise RtError(
            "daemon unreachable for %s %s: %s. Daemon-only: no CLI fallback."
            % (method, path, e.reason))


# --- Helpers for the daemon-migrated operations framework .py callers need.
# Query/body contracts copied verbatim from the canonical .sh wrappers
# (aspirations-{add-goal,read,complete-by}.sh, wm-read.sh) so there is one
# contract, owned by the daemon endpoint, with two thin clients.

def _q(s):
    return urllib.parse.quote(str(s), safe="")


def aspirations_add_goal(asp_id, record, source="world", overrides=None):
    """POST /v1/aspirations/add-goal. record: dict|json-str. overrides:
    {"Duplication": "why", ...} -> X-Mind-Override-Duplication header.
    Returns the parsed daemon response (dict)."""
    query = "asp_id=%s&source=%s" % (_q(asp_id), _q(source))
    hdr = {}
    for key, val in (overrides or {}).items():
        hdr["X-Mind-Override-%s" % key] = val
    payload = record if isinstance(record, str) else json.dumps(record)
    return json.loads(
        rt_call("POST", "/v1/aspirations/add-goal", query=query,
                body=payload, headers=hdr))


def aspirations_read(source="world", active=False, active_compact=False,
                     asp_id=None, limit=None, archive=False):
    """GET /v1/aspirations/read. Returns the raw JSON text the deleted
    `aspirations.py read` CLI used to print (callers json.loads as before).

    `archive=True` returns aspirations-archive.jsonl instead of the live
    store — a BARE list of aspirations (each with its nested `goals`), not
    the `{"aspirations": [...]}` envelope the active reads return. Callers
    resolving a goal-id MUST read both stores: a goal inside a COMPLETED,
    archived aspiration is absent from every live read, so a live-only
    lookup reports not-found — indistinguishable from an id that never
    existed (guard-1555; g-115-3916 measured a 37-day defer freeze from
    exactly this).
    """
    query = "source=%s" % _q(source)
    if active:
        query += "&active=1"
    if active_compact:
        query += "&active_compact=1"
    if archive:
        query += "&archive=1"
    if asp_id:
        query += "&id=%s" % _q(asp_id)
    if limit is not None:
        query += "&limit=%s" % _q(limit)
    return rt_call("GET", "/v1/aspirations/read", query=query)


def aspirations_complete_by(goal_id, source="world", agent_name=None,
                            key_finding=None):
    """POST /v1/aspirations/complete-by. Returns parsed daemon response."""
    query = "goal_id=%s&source=%s" % (_q(goal_id), _q(source))
    if agent_name:
        query += "&agent_name=%s" % _q(agent_name)
    if key_finding:
        query += "&key_finding=%s" % _q(key_finding)
    return json.loads(
        rt_call("POST", "/v1/aspirations/complete-by", query=query))


def wm_read(slot=None, as_json=True):
    """GET /v1/wm/read. Returns the raw body the deleted `wm.py read` CLI
    printed (JSON when as_json, else YAML)."""
    parts = []
    if slot:
        parts.append("slot=%s" % _q(slot))
    if as_json:
        parts.append("json=1")
    return rt_call("GET", "/v1/wm/read", query="&".join(parts) or None)


def store_increment(store, rec_id, field):
    """POST /v1/store/increment. Contract copied verbatim from
    reasoning-bank-increment.sh / guardrails-increment.sh
    (store=<key>&id=<id>&field=<dotted>). `field` is the full dotted path,
    e.g. 'utilization.times_helpful'. Returns the parsed daemon response."""
    query = "store=%s&id=%s&field=%s" % (_q(store), _q(rec_id), _q(field))
    return json.loads(rt_call("POST", "/v1/store/increment", query=query))


def store_set_field(store, rec_id, field, value):
    """POST /v1/store/set-field. Contract copied verbatim from
    reasoning-bank-update-field.sh / guardrails-update-field.sh
    (store=<key>&id=<id>&field=<field>&value=<value>). Returns the parsed
    daemon response."""
    query = "store=%s&id=%s&field=%s&value=%s" % (
        _q(store), _q(rec_id), _q(field), _q(value))
    return json.loads(rt_call("POST", "/v1/store/set-field", query=query))


# --- Tolerant decode helpers () -----------------------------------
# Shared decode primitives for daemon aspirations_read bodies that need the
#  raw_decode recovery + guard-383 fatal contract. Extracted from
# 4 near-identical sites (consolidation-health.py, defer-recheck.py inline,
# parent-supersession-sweep.py, precondition-defer-recheck.py).
#
# Contract per guard-383 (N>=2-source aggregators):
#   - Empty / whitespace-only body: return empty marker (list or None per shape).
#     A genuinely-empty queue is NOT a source error.
#   - Valid JSON: return decoded object.
#   - Valid prefix + trailing garbage ( shape): raw_decode recovers
#     the prefix; recovery is NOT a source error.
#   - JSONDecodeError: emit ONE stderr diagnostic, sys.exit(1).
#   - Wrong-type aggregate: emit stderr diagnostic, sys.exit(1).
#
# The single fail-open boundary is the caller's shell wrapper (`|| echo WARN`),
# never inside the aggregator (rb-347). `source` is treated as an opaque tag
# in stderr — callers pass e.g. "consolidation-health: world" to preserve the
# existing diagnostic identity.

def _raw_decode_or_fatal(source, stripped):
    """Shared body of tolerant_decode_*: raw_decode + JSONDecodeError fatal.
    Returns the decoded object; never returns on JSONDecodeError."""
    import sys
    try:
        obj, _ = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            "%s JSONDecodeError (%s); body prefix: %r" % (source, exc, body_prefix),
            file=sys.stderr,
        )
        sys.exit(1)
    return obj


def tolerant_decode_list(source, raw):
    """Tolerant decode of daemon aspirations_read body that MUST be a list.

    Returns [] on genuinely-empty body; the decoded list on success.
    sys.exit(1) on JSONDecodeError or non-list aggregate (guard-383).

    Used by sites that expect a list-only queue shape (consolidation-health.py).
    """
    import sys
    stripped = (raw or "").lstrip()
    if not stripped:
        return []  # genuinely empty — valid state, not a source error
    obj = _raw_decode_or_fatal(source, stripped)
    if not isinstance(obj, list):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            "%s non-list aggregate (type=%s); body prefix: %r"
            % (source, type(obj).__name__, body_prefix),
            file=sys.stderr,
        )
        sys.exit(1)
    return obj


def tolerant_decode_aggregate(source, raw):
    """Tolerant decode of daemon aspirations_read body that accepts dict-or-list.

    Returns None on genuinely-empty body (caller maps None -> []); dict or list
    on success. sys.exit(1) on JSONDecodeError or non-dict-and-non-list
    aggregate (guard-383).

    Used by sites that accept either a dict (with "aspirations" key) or a bare
    list (parent-supersession-sweep.py, precondition-defer-recheck.py,
    defer-recheck.py).
    """
    import sys
    stripped = (raw or "").lstrip()
    if not stripped:
        return None  # genuinely empty — valid state, not a source error
    obj = _raw_decode_or_fatal(source, stripped)
    if not isinstance(obj, (dict, list)):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            "%s non-dict-and-non-list aggregate (type=%s); body prefix: %r"
            % (source, type(obj).__name__, body_prefix),
            file=sys.stderr,
        )
        sys.exit(1)
    return obj
