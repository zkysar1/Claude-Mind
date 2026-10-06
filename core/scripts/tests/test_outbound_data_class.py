"""outbound_data_class.py -- the outbound DATA-CLASS gate ().

guard-4061 (the owner's personal address is written only in shape) and
guard-4525 (no credential reaches a log or a message) were honor-system: no
outbound chokepoint inspected the text. This file pins the shared checker -- a
positive control per class, the negative controls beside it, and the remedy
(the shaped address) that must pass -- and the board-post.sh chokepoint through
a stub-runtime harness. The other two chokepoints are tested where their
fixtures already live: test_notify_dispatch.py and test_peer_board_post.py.

Fixtures use example.com only. No literal credential appears in this file:
every key-shaped string is assembled at run time (_j), so neither the
pre-commit secret scanner nor a grep of the repo finds a key-shaped literal.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
REPO = SCRIPTS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import outbound_data_class as odc  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402

CHECKER = SCRIPTS / "outbound_data_class.py"
BOARD_POST = SCRIPTS / "board-post.sh"
OWNER = "operator@example.com"
SHAPED = "o***@e***.com"


def _j(*parts):
    return "".join(parts)


HEX32 = "0123456789abcdef0123456789abcdef"
HEX40 = HEX32 + "01234567"
AWS = _j("AKI", "AQWERTYUIOPASDFGH")
BEARER = _j("Bear", "er ", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA")
VIN = _j("vi", "n_", HEX40)

CREDENTIALS = [
    ("AWS access key id", AWS),
    ("AWS access key id", _j("ASI", "AQWERTYUIOPASDFGH")),
    ("GitHub token", _j("gh", "p_", "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6")),
    ("GitHub token", _j("github", "_pat_", "11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789")),
    ("product API key", VIN),
    ("product API key", _j("lo", "d_", HEX40)),
    ("product API key", _j("ay", "o_", HEX40)),
    ("legacy Ayoai API key", _j("ay", "o", HEX32)),
    ("legacy Ayoai API key", _j("AY", "O", HEX32.upper())),
    ("model-provider API key", _j("s", "k-", "abcDEF0123456789ghiJKL")),
    ("model-provider API key", _j("gs", "k_", "abcDEF0123456789ghiJKL")),
    ("private key block", _j("-----BEGIN ", "RSA ", "PRIVATE KEY-----")),
    ("bearer token", BEARER),
]

NOT_CREDENTIALS = [
    ("zero-filled product key", _j("vi", "n_", "0" * 40)),
    ("the AWS documentation key", _j("AKIA", "IOSFODNN7", "EXAMPLE")),
    ("one repeated character after Bearer", _j("Bear", "er ", "x" * 24)),
    ("AWS id one char short", _j("AKI", "AQWERTYUIOPASDFG")),
    ("product key with 39 hex", _j("vi", "n_", HEX40[:-1])),
    ("product key with 41 hex", _j("vi", "n_", HEX40 + "a")),
    ("a bare git sha", HEX40),
    ("a hyphenated word containing sk-", "risk-assessment-and-mitigation-planning"),
    ("prose after Bearer", "Bearer tokens are rotated weekly by the vault"),
    ("GitHub token one char under the minimum", _j("gh", "p_", "Ab3dE6gH9jK2mN5pQ8s")),
]


# --- scan(): the classes ------------------------------------------------------------------

def test_owner_address_in_full_is_found_case_insensitively_with_its_line():
    text = f"first line\nping {OWNER.upper()} now"
    found = odc.scan(text, user_email=OWNER)
    assert [(f.cls, f.line, f.length) for f in found] == [(odc.CLASS_OWNER_EMAIL, 2, len(OWNER))]
    assert OWNER not in repr(found) and OWNER.upper() not in repr(found), "a finding must carry no value"


def test_the_shaped_address_is_the_remedy_and_it_passes():
    assert odc.shape_email(OWNER) == SHAPED
    assert odc.scan(f"ping {SHAPED} now", user_email=OWNER) == []


def test_other_addresses_pass():
    text = "cc fleet@example.com and colleague@example.com, not the owner"
    assert odc.scan(text, user_email=OWNER) == []


def test_owner_class_is_skipped_when_no_address_is_configured():
    assert odc.scan(f"ping {OWNER}", user_email="") == []
    assert odc.scan(f"ping {OWNER}", user_email="not-an-address") == []


@pytest.mark.parametrize("kind,secret", CREDENTIALS, ids=[f"{k}:{s[:4]}" for k, s in CREDENTIALS])
def test_each_credential_shape_is_found_and_the_finding_carries_no_value(kind, secret):
    found = odc.scan(f"rotated the key {secret} today", user_email="")
    assert [f.kind for f in found] == [kind], found
    assert found[0].cls == odc.CLASS_CREDENTIAL and found[0].line == 1 and found[0].length == len(secret)
    assert secret not in repr(found)


@pytest.mark.parametrize("name,text", NOT_CREDENTIALS, ids=[n for n, _ in NOT_CREDENTIALS])
def test_placeholders_and_near_misses_are_not_credentials(name, text):
    assert odc.scan(f"see {text} here", user_email="") == [], name


def test_trailing_characters_do_not_hide_a_key():
    """The trailing guard is 'not another key character', not a word boundary: a key
    pasted against a suffix is still a key."""
    for tail in ("_backup", "xyz", ")", ","):
        assert [f.kind for f in odc.scan(f"{AWS}{tail}", user_email="")] == ["AWS access key id"], tail
    assert [f.kind for f in odc.scan(f"x{_j('ay', 'o', HEX32)}", user_email="")] == ["legacy Ayoai API key"]


def test_overlapping_shapes_report_the_specific_one_once():
    found = odc.scan(f"Authorization: {_j('Bear', 'er ', VIN)}", user_email="")
    assert [f.kind for f in found] == ["product API key"]


def test_redact_masks_every_class():
    assert odc.redact(f"mail {OWNER.upper()} key {AWS}", user_email=OWNER) == f"mail {SHAPED} key <redacted:len=20>"
    assert odc.redact("", user_email=OWNER) == ""


def test_iter_strings_walks_nesting_and_skips_the_envelope():
    payload = {"Title": "t", "Body": f"x {OWNER}", "RecipientEmail": OWNER, "FromEmail": OWNER,
               "XPayloadProvenance": OWNER, "Nested": {"a": [f"k {AWS}"]}, "n": 5}
    assert dict(odc.iter_strings(payload)) == {"Title": "t", "Body": f"x {OWNER}", "Nested.a[0]": f"k {AWS}"}


# --- check(): the verdicts ----------------------------------------------------------------

def _check(texts, **kw):
    kw.setdefault("user_email", OWNER)
    return odc.check("unit", texts, record=False, **kw)


def test_verdicts_allow_refuse_override_unavailable():
    v = _check([("body", "plain status")])
    assert (v.decision, v.findings, v.notes) == ("allow", [], [])
    v = _check([("body", f"ping {OWNER}")])
    assert v.decision == "refuse" and len(v.findings) == 1
    assert OWNER not in v.message and SHAPED in v.message and "owner-email" in v.message
    v = _check([("body", f"ping {OWNER}")], override="a fixture")
    assert v.decision == "override" and v.message == ""
    v = _check([("body", "plain status")], user_email="")
    assert v.decision == "unavailable" and "USER_EMAIL" in v.notes[0] and "NOT" in v.notes[0]


def test_a_missing_address_never_disables_the_credential_class():
    v = _check([("body", f"key {AWS}")], user_email="")
    assert v.decision == "refuse" and v.findings[0].cls == odc.CLASS_CREDENTIAL and v.notes


def test_a_text_under_two_field_names_is_judged_once():
    v = _check([("subject", f"x {OWNER}"), ("Title", f"x {OWNER}")])
    assert [f.where for f in v.findings] == ["subject"]


def test_refusal_text_is_masked_and_names_only_fixes_that_can_be_true():
    cred = _check([("body", f"key {AWS}")]).message
    assert "name a credential by its variable NAME" in cred and "address in shape" not in cred
    assert "<redacted:len=20>" in cred and AWS not in cred
    own = _check([("body", f"ping {OWNER}")]).message
    assert "address in shape" in own and "variable NAME" not in own
    both = _check([("body", f"ping {OWNER} key {AWS}")]).message
    assert "address in shape" in both and "variable NAME" in both
    for msg in (cred, own, both):
        assert OWNER not in msg and AWS not in msg
    # the waiver is advertised only where its justification can be true (guard-5593, rb-9686):
    # a credential-shaped string can be a documentation example; an exact owner address IS the address
    assert odc.OVERRIDE_FLAG in cred and "FALSE POSITIVE" in cred and "A live credential is never waived" in cred
    assert odc.OVERRIDE_FLAG not in own and odc.OVERRIDE_FLAG not in both


def test_a_long_finding_list_is_summarised():
    keys = " ".join(_j("AKI", "A") + f"{i:016d}" for i in range(1, 11))
    v = _check([("body", keys)])
    assert len(v.findings) == 10 and "(+2 more)" in v.message


# --- the CLI: exit codes, telemetry, the override ledger (real tmp dirs) -------------------

def _cli(tmp_path, text, *args, env_extra=None):
    (tmp_path / "meta").mkdir(exist_ok=True)
    (tmp_path / "world").mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"STORAGE_BACKEND": "local", "MIND_META": str(tmp_path / "meta"),
                "MIND_WORLD": str(tmp_path / "world"), "GATE_LOG_ALLOW_PYTEST": "1",
                "MIND_AGENT": "alpha", "USER_EMAIL": OWNER})
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(CHECKER), "--surface", "unit", *args], input=text,
                          env=env, capture_output=True, text=True, timeout=60)


def _rows(tmp_path, pattern, sub):
    out = []
    for p in sorted((tmp_path / sub).glob(pattern)):
        out += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def _blob(tmp_path):
    return "\n".join(p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file())


def test_cli_exit_codes_and_a_masked_refusal(tmp_path):
    clean = _cli(tmp_path, "an ordinary status line")
    assert (clean.returncode, clean.stderr) == (0, "")
    own = _cli(tmp_path, f"line one\nping {OWNER}")
    assert own.returncode == odc.REFUSAL_RC == 5
    assert "REFUSED" in own.stderr and "line 2" in own.stderr and SHAPED in own.stderr
    assert OWNER not in own.stderr + own.stdout
    cred = _cli(tmp_path, f"key {BEARER}")
    assert cred.returncode == 5 and BEARER not in cred.stderr + cred.stdout


def test_a_usage_error_cannot_masquerade_as_a_refusal(tmp_path):
    p = subprocess.run([sys.executable, str(CHECKER)], input="x", capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 != odc.REFUSAL_RC


def test_a_crash_inside_the_checker_fails_open_and_loud(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(odc, "check", boom)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"hi")))
    assert odc.main(["--surface", "unit"]) == 0
    assert "UNAVAILABLE" in capsys.readouterr().err


def test_every_verdict_leaves_one_firing_row_and_never_the_value(tmp_path):
    assert _cli(tmp_path, "plain").returncode == 0
    assert _cli(tmp_path, f"key {AWS}").returncode == 5
    assert _cli(tmp_path, f"key {AWS}", "--override", f"doc example {AWS}").returncode == 0
    rows = _rows(tmp_path, "gate-firings*.jsonl", "meta")
    assert [r["decision"] for r in rows] == ["noop", "block", "override"]
    assert {r["gate_id"] for r in rows} == {odc.GATE_ID} and {r["caller"] for r in rows} == {"unit"}
    assert rows[1]["trigger_matched"] == "credential" and rows[1]["extra"]["finding_count"] == 1
    blob = _blob(tmp_path)
    assert AWS not in blob and OWNER not in blob, "no store may receive the matched value"


def test_override_writes_one_ledger_row_with_the_justification_redacted(tmp_path):
    p = _cli(tmp_path, f"key {AWS}", "--override", f"doc example {AWS}, not live")
    assert p.returncode == 0 and "OVERRIDDEN" in p.stderr
    [row] = _rows(tmp_path, "override-bypass-ledger.jsonl", "world")
    just = row["justification"]
    assert just == "doc example <redacted:len=20>, not live"
    assert row["override_token"] == hashlib.sha1(just.encode()).hexdigest()[:12]
    assert row["gate"] == odc.GATE_ID and row["agent"] == "alpha"
    ctx = row["context"]
    assert (ctx["surface"], ctx["classes"], ctx["kinds"], ctx["finding_count"]) == (
        "unit", ["credential"], ["AWS access key id"], 1) and len(ctx["body_sha12"]) == 12


def test_an_override_with_nothing_to_override_writes_nothing(tmp_path):
    assert _cli(tmp_path, "plain", "--override", "just in case").returncode == 0
    assert not (tmp_path / "world" / "override-bypass-ledger.jsonl").exists()


def test_no_record_gives_the_verdict_and_leaves_no_rows(tmp_path):
    assert _cli(tmp_path, f"key {AWS}", "--no-record").returncode == 5
    assert _cli(tmp_path, f"key {AWS}", "--no-record", "--override", "why").returncode == 0
    assert list((tmp_path / "meta").glob("gate-firings*")) == []
    assert not (tmp_path / "world" / "override-bypass-ledger.jsonl").exists()


def test_an_unset_owner_address_is_loud_logged_and_never_disables_credentials(tmp_path):
    p = _cli(tmp_path, f"ping {OWNER}", env_extra={"USER_EMAIL": ""})
    assert p.returncode == 0 and "no USER_EMAIL in the environment or .env.local" in p.stderr
    row = _rows(tmp_path, "gate-firings*.jsonl", "meta")[-1]
    assert row["decision"] == "fail_open" and "user_email_unset" in row["gate_error"]
    assert _cli(tmp_path, f"key {AWS}", env_extra={"USER_EMAIL": ""}).returncode == 5


def test_the_owner_address_comes_from_the_environment_first_then_env_local(monkeypatch):
    import env as env_mod
    # the environment wins, and an EMPTY value means "deliberately none": .env.local is not consulted
    monkeypatch.setattr(env_mod, "parse_local", lambda: pytest.fail(".env.local consulted"))
    monkeypatch.setenv("USER_EMAIL", f"  {OWNER} ")
    assert odc.owner_email() == OWNER
    monkeypatch.setenv("USER_EMAIL", "")
    assert odc.owner_email() == ""
    # absent from the environment: the framework's own .env.local parser answers
    monkeypatch.delenv("USER_EMAIL")
    monkeypatch.setattr(env_mod, "parse_local", lambda: {"USER_EMAIL": f" {OWNER} "})
    assert odc.owner_email() == OWNER
    monkeypatch.setattr(env_mod, "parse_local", lambda: {})
    assert odc.owner_email() == ""

    def broken():
        raise OSError("unreadable")
    monkeypatch.setattr(env_mod, "parse_local", broken)
    assert odc.owner_email() == "", "an unreadable .env.local must degrade to 'unavailable', never raise"


def test_check_resolves_the_address_when_the_caller_passes_none(monkeypatch):
    monkeypatch.setenv("USER_EMAIL", OWNER)
    assert odc.check("unit", [("body", f"ping {OWNER}")], record=False).decision == "refuse"


# --- parity with the alert-sweep predicate this checker reuses ------------------------------

def _alert_dedup():
    try:
        from _paths import WORLD_DIR
        p = Path(WORLD_DIR) / "scripts" / "alert_dedup.py"
        if not p.is_file():
            pytest.skip("this deployment ships no alert_dedup.py")
        spec = importlib.util.spec_from_file_location("alert_dedup_parity", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        pytest.skip(f"alert_dedup.py not importable here: {type(exc).__name__}")


def test_the_owner_predicate_and_the_shape_agree_with_alert_dedup():
    ad = _alert_dedup()
    for addr in (OWNER, "x@mail.example.co.uk", "not-an-address", ""):
        assert odc.shape_email(addr) == ad.shape_email(addr), addr
    for text in (f"ping {OWNER} now", f"ping {OWNER.upper()}", f"<{OWNER}>", f"x{OWNER}", f"ping {SHAPED}",
                 "ping colleague@example.com", ""):
        hit = ad.redact_user_email(text, OWNER) != text
        assert hit == bool(odc.scan(text, user_email=OWNER)), text


def test_the_legacy_key_shape_agrees_with_alert_dedup():
    ad = _alert_dedup()
    for text in (_j("ay", "o", HEX32), f"x{_j('ay', 'o', HEX32)}y", _j("AY", "O", HEX32.upper()),
                 _j("ay", "o", HEX32[:-1]), "ayo", HEX32):
        legacy = any(f.kind == "legacy Ayoai API key" for f in odc.scan(text, user_email=""))
        assert legacy == bool(ad._API_KEY_SHAPE_RE.search(text)), text


# --- the registry names the gate and every site really calls it ------------------------------

def test_the_gate_is_registered_and_every_registered_site_calls_the_checker():
    import yaml
    reg = yaml.safe_load((REPO / "core" / "config" / "gates.yaml").read_text(encoding="utf-8"))
    gate = {g["id"]: g for g in reg["gates"]}[odc.GATE_ID]
    assert gate["script"] == "core/scripts/outbound_data_class.py" and (REPO / gate["script"]).is_file()
    assert gate["instrumented"] is True and gate["override_flag"] == odc.OVERRIDE_FLAG
    assert {Path(s["file"]).name for s in gate["sites"]} == {"board-post.sh", "peer_board_post.py", "notify_dispatch.py"}
    for site in gate["sites"]:
        assert "outbound_data_class" in (REPO / site["file"]).read_text(encoding="utf-8"), site["file"]


# --- board-post.sh: the chokepoint, through a stub runtime ----------------------------------

STUB_RUNTIME = """#!/usr/bin/env bash
rt_python_launcher() { echo "$STUB_PY"; }
rt_url_encode() { printf '%s' "$1"; }
rt_call() { echo "rt_call $*" >> "$STUB_LOG"; echo '{"ok":true,"id":"msg-stub-001"}'; }
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "stub: no daemon" >&2; exit 1; }
"""


@pytest.fixture
def board_tree(tmp_path):
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(BOARD_POST, scripts / "board-post.sh")
    shutil.copy(CHECKER, scripts / "outbound_data_class.py")
    (scripts / "_runtime.sh").write_text(STUB_RUNTIME)
    return tmp_path


def _post(tree, text, *args):
    env = dict(os.environ)
    env.update({"STUB_PY": sys.executable, "STUB_LOG": str(tree / "stub.log"), "USER_EMAIL": OWNER,
                "MIND_META": str(tree / "meta"), "MIND_WORLD": str(tree / "world"), "STORAGE_BACKEND": "local"})
    return subprocess.run(bash_cmd(tree / "core" / "scripts" / "board-post.sh", "--channel", "coordination", *args),
                          input=text, env=env, capture_output=True, text=True, timeout=60)


def _posted(tree):
    log = tree / "stub.log"
    return log.read_text().count("rt_call POST") if log.exists() else 0


@pytest.mark.parametrize("secret", [OWNER, AWS, BEARER, VIN], ids=["owner", "aws", "bearer", "product-key"])
def test_board_post_refuses_an_owner_address_or_a_credential_and_never_posts(board_tree, secret):
    p = _post(board_tree, f"status: reached {secret} today")
    assert p.returncode == 1, p.stderr
    assert "REFUSED by the outbound data-class gate" in p.stderr
    assert secret not in p.stderr + p.stdout
    assert _posted(board_tree) == 0


def test_board_post_passes_clean_text_and_the_shaped_remedy(board_tree):
    for text in ("status: all green", f"status: reached {SHAPED} today"):
        p = _post(board_tree, text)
        assert (p.returncode, p.stdout.strip()) == (0, "msg-stub-001"), p.stderr
    assert _posted(board_tree) == 2


def test_board_post_override_posts_and_says_so(board_tree):
    p = _post(board_tree, f"status: doc example {AWS}", "--override-data-class", "a documentation example")
    assert p.returncode == 0 and "OVERRIDDEN" in p.stderr, p.stderr
    assert _posted(board_tree) == 1


@pytest.mark.parametrize("breakage", ["exits-1", "missing"])
def test_board_post_fails_open_and_loud_when_the_checker_cannot_run(board_tree, breakage):
    checker = board_tree / "core" / "scripts" / "outbound_data_class.py"
    if breakage == "missing":
        checker.unlink()
    else:
        checker.write_text("import sys\nsys.exit(1)\n")
    p = _post(board_tree, f"status: reached {OWNER} today")
    assert p.returncode == 0, p.stderr
    assert "posting UNCHECKED" in p.stderr
    assert _posted(board_tree) == 1
