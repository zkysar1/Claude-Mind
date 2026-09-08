"""inbound_directive_asp — the vessel-side mint of the member's directive aspiration ().

WHY THIS EXISTS. provision-env.sh (Ayoai-Environment-Server, wire_assigned_lane)
creates the vessel's world queue and writes INBOUND_ENVIRONMENT_KEY +
INBOUND_SPOOL_ROOT, but cannot mint the "Assigned by the member" aspiration:
creation is daemon-only and no daemon is bound to the vessel at provision time.
So every cold-started vessel arrived with INBOUND_DIRECTIVE_ASP_ID EMPTY and the
engine left every directive UNCLAIMED (unconfigured>0) — reachable, wired, and
never filing. The mint lives in the loop's own drain pass instead. These tests
pin the properties that make it safe to run on EVERY iteration:

  1. PRECEDENCE — env var, then .env.local, then the queue, then a mint; the
     first non-empty wins and nothing later runs.
  2. EXACT TITLE — read-back matches the aspiration title, never a goal's prose
     or a near-title, so a second aspiration is never minted beside the first.
  3. ONCE — a mint is followed by a write-back, and the next resolve reads back
     without a second add. A failed add is never re-run (rb-8227 / guard-751):
     the queue is re-read once instead, which also recovers a timed-out add
     that did write.
  4. FAIL-OPEN — a failed mint yields an EMPTY id and touches nothing; the
     engine then reports unconfigured>0 rather than filing under a guess.
  5. THE SLOT passes the resolved id to the engine EXPLICITLY (the daemon's
     environment predates the write-back), and never mints on a box that is
     not an environment host — it never reaches the helper before the guards.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash")

TITLE = "Assigned by the member"


def _load():
    spec = importlib.util.spec_from_file_location("inbound_directive_asp_under_test",
                                                  CORE / "inbound_directive_asp.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


idasp = _load()


def _q(p: Path) -> str:
    return shlex.quote(p.as_posix())


class _Fakes:
    """Shell-string stand-ins for the two daemon wrappers, counting every call."""

    def __init__(self, td: Path):
        self.td = td
        self.read_count = td / "read.count"
        self.add_count = td / "add.count"
        self.add_stdin = td / "add.stdin.json"

    def read(self, listing, rc: int = 0) -> str:
        body = shlex.quote(json.dumps(listing))
        return f"echo x >> {_q(self.read_count)}; printf '%s' {body}; exit {rc}"

    def read_empty_then(self, listing) -> str:
        """[] on the first call, `listing` afterwards — an add that wrote late."""
        flag = self.td / "read.flag"
        body = shlex.quote(json.dumps(listing))
        return (f"echo x >> {_q(self.read_count)}; "
                f"if [ -f {_q(flag)} ]; then printf '%s' {body}; "
                f"else touch {_q(flag)}; printf '%s' '[]'; fi")

    def add(self, record, rc: int = 0) -> str:
        body = shlex.quote(json.dumps(record)) if record is not None else "''"
        return (f"cat > {_q(self.add_stdin)}; echo x >> {_q(self.add_count)}; "
                f"printf '%s' {body}; exit {rc}")

    def reads(self) -> int:
        return self.read_count.read_text().count("x") if self.read_count.exists() else 0

    def adds(self) -> int:
        return self.add_count.read_text().count("x") if self.add_count.exists() else 0


FAIL = "echo should-not-run >&2; exit 97"
LISTED = [{"id": "asp-003", "title": TITLE, "status": "active"}]
MINTED = {"id": "asp-042", "title": TITLE, "status": "active", "priority": "HIGH"}


class Precedence(unittest.TestCase):

    def test_env_var_wins_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            env_local = Path(td) / ".env.local"
            got = idasp.resolve({"INBOUND_DIRECTIVE_ASP_ID": " asp-007 "}, env_local,
                                read_cmd=FAIL, add_cmd=FAIL)
            self.assertEqual(got, ("asp-007", "env"))
            self.assertFalse(env_local.exists(), "an env-sourced id is not written back")

    def test_env_local_wins_over_the_queue(self):
        with tempfile.TemporaryDirectory() as td:
            env_local = Path(td) / ".env.local"
            env_local.write_text('FOO=1\nINBOUND_DIRECTIVE_ASP_ID="asp-008"\n', encoding="utf-8")
            got = idasp.resolve({}, env_local, read_cmd=FAIL, add_cmd=FAIL)
            self.assertEqual(got, ("asp-008", "env.local"))

    def test_queue_read_back_is_written_to_env_local_without_an_add(self):
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            env_local = Path(td) / ".env.local"
            env_local.write_text("KEEP_ME=yes\nINBOUND_DIRECTIVE_ASP_ID=\n", encoding="utf-8")
            got = idasp.resolve({"INBOUND_DIRECTIVE_ASP_ID": ""}, env_local,
                                read_cmd=f.read(LISTED), add_cmd=f.add(MINTED))
            self.assertEqual(got, ("asp-003", "queue"))
            self.assertEqual(f.adds(), 0, "found by title — nothing to mint")
            text = env_local.read_text(encoding="utf-8")
            self.assertIn("KEEP_ME=yes\n", text)
            self.assertEqual(text.count("INBOUND_DIRECTIVE_ASP_ID="), 1, text)
            self.assertIn("INBOUND_DIRECTIVE_ASP_ID=asp-003\n", text)


class ExactTitle(unittest.TestCase):

    def test_near_titles_and_goal_prose_do_not_match(self):
        near = [
            {"id": "asp-004", "title": f"{TITLE} (old)", "status": "active"},
            {"id": "asp-005", "title": "Smoke", "status": "active",
             "goals": [{"id": "g-005-01", "title": TITLE}]},
            {"id": "asp-006", "title": TITLE.lower(), "status": "active"},
        ]
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            got = idasp.resolve({}, Path(td) / ".env.local",
                                read_cmd=f.read(near), add_cmd=f.add(MINTED))
            self.assertEqual(got, ("asp-042", "minted"))
            self.assertEqual(f.adds(), 1)

    def test_read_back_is_exact_through_a_parser(self):
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            self.assertEqual(idasp.find_by_title(f.read(LISTED)), "asp-003")
            self.assertEqual(idasp.find_by_title(f.read([])), "")
            self.assertEqual(idasp.find_by_title(f.read({"not": "a list"})), "")
            self.assertEqual(idasp.find_by_title("printf 'not json'"), "")
            self.assertEqual(idasp.find_by_title(f.read(LISTED, rc=1)), "",
                             "a failing wrapper is not a listing (guard-399)")


class Once(unittest.TestCase):

    def test_mint_then_the_next_pass_reads_back_without_a_second_add(self):
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            env_local = Path(td) / ".env.local"
            first = idasp.resolve({}, env_local, read_cmd=f.read([]), add_cmd=f.add(MINTED))
            self.assertEqual(first, ("asp-042", "minted"))
            self.assertEqual(f.adds(), 1)
            sent = json.loads(f.add_stdin.read_text(encoding="utf-8"))
            self.assertEqual(sent["title"], TITLE)
            self.assertEqual(sent["priority"], "HIGH")
            self.assertNotIn("id", sent, "the id is the daemon's to assign (guard-4850)")
            self.assertEqual(env_local.read_text(encoding="utf-8").strip(),
                             "INBOUND_DIRECTIVE_ASP_ID=asp-042")
            second = idasp.resolve({}, env_local, read_cmd=FAIL, add_cmd=FAIL)
            self.assertEqual(second, ("asp-042", "env.local"))
            self.assertEqual(f.adds(), 1, "one vessel, one aspiration")

    def test_a_failed_add_is_never_rerun_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            env_local = Path(td) / ".env.local"
            env_local.write_text("KEEP_ME=yes\n", encoding="utf-8")
            got = idasp.resolve({}, env_local, read_cmd=f.read([]), add_cmd=f.add(None, rc=1))
            self.assertEqual(got, ("", "none"))
            self.assertEqual(f.adds(), 1)
            self.assertEqual(f.reads(), 2, "the queue is re-read once after the failed add")
            self.assertEqual(env_local.read_text(encoding="utf-8"), "KEEP_ME=yes\n",
                             "fail-open: nothing written back")

    def test_a_timed_out_add_that_did_write_is_recovered_by_the_reread(self):
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            env_local = Path(td) / ".env.local"
            got = idasp.resolve({}, env_local, read_cmd=f.read_empty_then(LISTED),
                                add_cmd=f.add(None, rc=1))
            self.assertEqual(got, ("asp-003", "queue-after-add"))
            self.assertEqual(f.adds(), 1)
            self.assertIn("INBOUND_DIRECTIVE_ASP_ID=asp-003", env_local.read_text(encoding="utf-8"))


class EnvLocalFile(unittest.TestCase):

    def test_write_back_keeps_other_lines_replaces_the_key_and_keeps_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env.local"
            p.write_text("A=1\nINBOUND_DIRECTIVE_ASP_ID=\nB=2\n", encoding="utf-8")
            if os.name != "nt":
                os.chmod(p, 0o600)
            idasp.write_env_local(p, "asp-042")
            self.assertEqual(p.read_text(encoding="utf-8"), "A=1\nB=2\nINBOUND_DIRECTIVE_ASP_ID=asp-042\n")
            if os.name != "nt":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600, "secrets file stays 0600")
            self.assertEqual([x.name for x in Path(td).iterdir()], [".env.local"],
                             "no temp file left beside it")

    def test_read_takes_the_last_definition_and_tolerates_absence(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env.local"
            self.assertEqual(idasp.read_env_local(p), "")
            p.write_text("INBOUND_DIRECTIVE_ASP_ID=asp-001\nINBOUND_DIRECTIVE_ASP_ID='asp-002'\n",
                         encoding="utf-8")
            self.assertEqual(idasp.read_env_local(p), "asp-002")


class TheSlot(unittest.TestCase):
    """inbound-drain-default.sh resolves the id after its guards and hands it to the engine."""

    def _run(self, env: dict, *args: str) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        for k in ("INBOUND_ENVIRONMENT_KEY", "INBOUND_SPOOL_ROOT", "INBOUND_DIRECTIVE_ASP_ID",
                  "INBOUND_ENV_LOCAL", "INBOUND_ASP_READ_CMD", "INBOUND_ASP_ADD_CMD"):
            e.pop(k, None)
        e.update(env)
        return subprocess.run([BASH, (CORE / "inbound-drain-default.sh").as_posix(), *args],
                              capture_output=True, text=True, env=e, timeout=120)

    def test_resolves_from_the_queue_and_writes_back_before_the_engine_runs(self):
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            (Path(td) / "spool" / "env-a").mkdir(parents=True)
            env_local = Path(td) / ".env.local"
            p = self._run({
                "INBOUND_ENVIRONMENT_KEY": "env-a+char7",
                "INBOUND_SPOOL_ROOT": (Path(td) / "spool").as_posix(),
                "INBOUND_ENV_LOCAL": env_local.as_posix(),
                "INBOUND_ASP_READ_CMD": f.read(LISTED),
                "INBOUND_ASP_ADD_CMD": FAIL,
            }, "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("directive aspiration: queue asp-003", p.stderr)
            payload = json.loads(p.stdout)
            self.assertEqual(payload["environments"][0]["environment"], "env-a")
            self.assertIn("INBOUND_DIRECTIVE_ASP_ID=asp-003", env_local.read_text(encoding="utf-8"))

    def test_a_non_host_box_never_reaches_the_mint(self):
        with tempfile.TemporaryDirectory() as td:
            f = _Fakes(Path(td))
            p = self._run({"INBOUND_ENV_LOCAL": (Path(td) / ".env.local").as_posix(),
                           "INBOUND_ASP_READ_CMD": f.read([]),
                           "INBOUND_ASP_ADD_CMD": f.add(MINTED)}, "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue(json.loads(p.stdout)["not_a_vessel"])
            self.assertEqual(f.adds(), 0)
            self.assertEqual(f.reads(), 0)
            self.assertFalse((Path(td) / ".env.local").exists())


if __name__ == "__main__":
    unittest.main()
