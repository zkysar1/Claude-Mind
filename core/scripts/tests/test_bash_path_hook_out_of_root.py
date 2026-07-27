#!/usr/bin/env python3
""" — write-intent out-of-root refusal on the Bash tool surface.

Drives BOTH L1 hooks end-to-end via subprocess + stdin payload with
PROJECT_ROOT and MIND_AGENT set, which is the invocation shape production
uses. Pattern (b) from the `hook-authoring-pitfalls` tree node: it exercises
the resolver and the governed/allowed-root logic, not just the regex — a
regex-only test measures a branch production never takes.

Covers three axes:
  * tool-surface PARITY — the same out-of-root target must produce a SIGNAL on
    the Bash surface, where it previously produced silence (the gap this goal
    closes). Parity is now FULL: both surfaces deny.
  * FALSE-POSITIVE floor — the exempt sinks, in-root writes, reads, and the
    heredoc/`0:` extractor defects that a naive refusal would have denied
  * NO REGRESSION in the pre-existing new-top-level cruft branch, in BOTH
    directions (rb-401: tightening a static-scan regex must be re-checked for
    the false-negative direction too)

The out-of-root branch is a DENY. It shipped as a deny; a same-iteration
fresh-eyes review then replayed a WIDER corpus (48,348 Bash calls / 4
transcripts vs the original 11,559 / 2) and found two live false-positive
classes at 0.062% — remote-exec wrapper arguments, and quoted command-text
beyond echo/printf, including a real `iteration-close.sh --summary` whose
PROSE contained `>>/tee`. It was demoted to a stderr advisory while g-115-3349
fixed the cause: verbs and redirect operators are now located only OUTSIDE
quoted spans, with a bounded `bash -c "..."` allowlist so a locally-executed
quoted command is still descended into. Both FP classes are pinned here as
`approve`, the local-exec false-NEGATIVE direction is pinned as flagged, and a
full-corpus replay (48,646 calls / 4 transcripts) returned an EMPTY residual —
so the deny is restored. The three-valued `bash_verdict` is retained: it is
what makes a silent demotion back to advisory a visible test failure rather
than a pass.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = PROJECT_ROOT / "core" / "scripts"
BASH_HOOK = SCRIPTS / "bash-path-resolution-hook.py"
EDIT_HOOK = SCRIPTS / "path-resolution-hook.py"
AGENT = os.environ.get("MIND_AGENT") or "zeta"

sys.path.insert(0, str(SCRIPTS))
from _path_roots import (  # noqa: E402
    compute_allowed_roots,
    is_write_exempt_sink,
    norm_path,
    read_paths_conf,
)


def _conf_present():
    return (PROJECT_ROOT / "agents" / AGENT / "local-paths.conf").is_file()


def _run(hook, payload):
    env = dict(os.environ)
    env["PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["MIND_AGENT"] = AGENT
    p = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )
    return p.stdout.strip(), p.stderr


def bash_verdict(cmd):
    """Three-valued: deny (stdout payload) | advisory (stderr banner) | approve.

    BOTH branches — out-of-root (restored by g-115-3349) and the pre-existing
    new-top-level cruft check — are hard DENYs, so no test expects `advisory`
    today. The third value is kept deliberately: `advisory` is the shape the
    out-of-root branch wore while its false positives were being fixed, and
    collapsing it into `approve` would let a silent demotion back to a stderr
    banner read as a passing test instead of a failing one.
    """
    out, err = _run(BASH_HOOK, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    if out:
        return "deny"
    if "[l1-bash-path] ADVISORY" in err:
        return "advisory"
    return "approve"


def edit_verdict(file_path):
    out, _ = _run(EDIT_HOOK, {"tool_name": "Write",
                              "tool_input": {"file_path": file_path,
                                             "content": "x"}})
    return "deny" if out else "approve"


OUT_OF_ROOT = "/opt/definitely-not-a-configured-root/g1153338.txt"


@unittest.skipUnless(_conf_present(),
                     "local-paths.conf absent — hooks fail open by design")
class TestOutOfRootRefusal(unittest.TestCase):

    # --- the gap this goal closes -------------------------------------
    def test_bash_write_outside_all_roots_is_flagged(self):
        self.assertEqual(bash_verdict(f"echo hi > {OUT_OF_ROOT}"), "deny")

    def test_tool_surface_parity_with_edit_hook(self):
        """Before  the Bash surface was SILENT on a target the Edit
        surface refused — the write was refused via Edit and completed via Bash
        minutes later with no signal at all. The Bash side now flags it.

        Parity is now FULL — both surfaces deny (g-115-3349). It was
        deliberately partial for one iteration (Edit denied, Bash advised)
        while two live false-positive classes were open: remote-exec wrapper
        arguments, and quoted command-text beyond echo/printf, including a real
        `iteration-close.sh --summary` whose prose contained `>>/tee`. The
        asymmetry was never the goal — it was a demotion held only until the
        cause was fixed. It stays worth naming because the two surfaces are NOT
        equally safe to refuse on: an Edit is a write by definition, while a
        shell command's write intent is INFERRED and so can be inferred
        wrongly. That is why restoring the deny required a measured
        false-positive floor rather than a plausibility argument."""
        self.assertEqual(edit_verdict(OUT_OF_ROOT), "deny")
        self.assertEqual(bash_verdict(f"echo hi > {OUT_OF_ROOT}"), "deny")

    def test_all_write_verbs_covered(self):
        for cmd in (
            f"cp a.txt {OUT_OF_ROOT}",
            f"mv a.txt {OUT_OF_ROOT}",
            f"echo hi | tee {OUT_OF_ROOT}",
            f"touch {OUT_OF_ROOT}",
            f"mkdir -p /opt/definitely-not-a-configured-root/sub",
            f"cat > {OUT_OF_ROOT} <<'EOF'\nbody\nEOF",
        ):
            with self.subTest(cmd=cmd.split("\n")[0]):
                self.assertEqual(bash_verdict(cmd), "deny")

    def test_remote_exec_wrapper_argument_is_approved(self):
        """FIXED by  (was a pinned false positive). `efs-ssh.sh
        "mkdir -p /home/..."` names a REMOTE path; EFS access through that
        wrapper is an agent-provisionable action, and the deny broke it. The
        span rule now ignores a verb whose offset lies inside a quoted span,
        so the wrapper's DATA argument no longer reads as local command text."""
        self.assertEqual(
            bash_verdict('bash /some/wrapper/efs-ssh.sh "mkdir -p /home/ec2-user/x"'),
            "approve")

    def test_prose_redirect_in_quoted_summary_is_approved(self):
        """FIXED by  (was a pinned false positive). A real goal-close
        whose --summary PROSE contained `>>/tee -a` was denied outright — the
        loop's own close path. The `>>` sits inside a quoted span, so the span
        rule no longer reads it as a redirect operator."""
        self.assertEqual(
            bash_verdict('bash core/scripts/iteration-close.sh --phase verify '
                         '--summary "FAIL on >>/tee -a resurrection"'),
            "approve")

    def test_prose_redirect_in_git_commit_message_is_approved(self):
        """The FP-2 class is BROADER than --summary. Full-corpus replay
        (g-115-3349, 48,639 calls / 4 transcripts) found 3 residual `/tee`
        cases: two were `iteration-close.sh --summary`, but the third was a
        `git commit -m` message. Any quoted prose argument containing `>>` on
        any verb hits this — a second, differently-shaped instance, so a fix
        that special-cased `--summary` alone would fail here."""
        self.assertEqual(
            bash_verdict('git commit -m "guard against raw >>/tee -a append"'),
            "approve")

    # --- false-NEGATIVE floor ( criterion 3) ------------------
    # The FP classes above and these FN cases are STRUCTURALLY IDENTICAL:
    # `<verb> "<text containing a path>"`. Quoted-span position alone cannot
    # tell them apart — the only discriminator is WHICH command receives the
    # quoted argument. `bash -c` runs it locally; `efs-ssh.sh` ships it to a
    # remote host; `git commit -m` never executes it at all.
    #
    # That measurement is why the fix must be span-offset PLUS a local-exec
    # allowlist whose quoted argument is RESCANNED — not span-offset alone
    # (which would silently approve every case below, opening 5 bypasses of
    # exactly the kind rb-401 warns about when tightening a static scan), and
    # not a remote-exec denylist alone (which cannot address the prose class
    # at all, since neither iteration-close.sh nor git is an exec wrapper).
    #
    # These assertions must hold BOTH before and after the deny restore. They
    # are the guard on the tradeoff, so do NOT relax them to make a span rule
    # pass — a regression here is a real bypass, not a test inconvenience.
    def test_local_exec_quoted_write_must_stay_flagged(self):
        for cmd in (
            'bash -c "mkdir -p /opt/definitely-not-a-configured-root/evil"',
            "bash -c 'echo x > /opt/definitely-not-a-configured-root/evil.txt'",
            'sh -c "touch /opt/definitely-not-a-configured-root/evil2"',
            'env FOO=1 bash -c "mkdir /opt/definitely-not-a-configured-root/evil3"',
            'bash -lc "echo x >> /opt/definitely-not-a-configured-root/evil4"',
        ):
            with self.subTest(cmd=cmd):
                self.assertIn(bash_verdict(cmd), ("advisory", "deny"))

    def test_local_exec_allowlist_does_not_readmit_wrapper_or_prose(self):
        """The allowlist itself re-opened the FP class it sits beside, and the
        corpus replay did NOT catch it — the corpus contained bare
        `efs-ssh.sh "..."` but not the FLAGGED variants. That is guard-1557
        exactly: a clean rate bounds only the FP classes the corpus CONTAINS.
        Found by the same-iteration fresh-eyes review of g-115-3349.

        Two independent defects compounded, so both are pinned here:
          * `\\bsh\\b` matched the `sh` in `efs-ssh.sh` — `.` is a word
            boundary, so a remote-exec wrapper re-entered the allowlist.
          * `-[a-z]*c[a-z]*` matched any LONG flag containing a c —
            `--exec`, `--check`, `--cached`, `--category`.

        The last case is the sharpest: `retrieve.sh --category "..."` is the
        pre-apply consultation shape `code-review-protocol.md` step 4
        MANDATES. Denying it would refuse a call the framework requires."""
        for cmd in (
            'bash /some/wrapper/efs-ssh.sh --exec "mkdir -p /home/ec2-user/x"',
            'bash world/scripts/efs-ssh.sh --check "mkdir -p /home/ec2-user/x"',
            'bash deploy.sh --cached "touch /opt/not-a-root/f"',
            'bash core/scripts/retrieve.sh --category '
            '"why does mkdir /opt/other/thing fail"',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_verdict(cmd), "approve")

    # --- false-positive floor -----------------------------------------
    def test_reads_outside_roots_are_approved(self):
        """Shell commands are mostly READS. A refusal keyed on the presence of
        an outside path rather than on write INTENT would refuse these."""
        for cmd in (
            "cat /etc/hostname",
            "ls -la /opt",
            "grep -r foo /usr/share",
            "git -C /opt/some/other/repo log --oneline -3",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_verdict(cmd), "approve")

    def test_exempt_sinks_are_approved(self):
        """Device sinks and the system temp tree (which contains the sanctioned
        session scratchpad) carry ~26pp of the measured false-positive mass."""
        for cmd in (
            "bash core/scripts/team-state-read.sh --json > /dev/null",
            "echo x >> /dev/null",
            "echo x > /tmp/scratch-g1153338.json",
            "echo x > /tmp/claude-0/some-session/scratch/notes.md",
            "echo x > /var/tmp/probe.txt",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_verdict(cmd), "approve")

    def test_in_root_writes_are_approved(self):
        # .as_posix(), NOT str() — guard-581. str(WindowsPath) yields
        # `C:\<WORKSPACE>\...`, and PATH_CHARS (hook L91) deliberately excludes
        # backslash, so the extractor reads the target as bare `C:` — under no
        # configured root, so an IN-ROOT write drew a spurious advisory and this
        # test failed on every Windows box while passing on Linux. Excluding
        # backslash is CORRECT, not a hook bug: unquoted `C:\x\y` is mangled by
        # bash itself into `C:xy` (verified: echo C:\Zak\Git -> C:ZakGit), so the
        # command the old form built never meant what the test assumed. Quoted
        # `"C:\x\y"` survives bash but matches no PATH_CHARS run, so it extracts
        # nothing and fails OPEN — safe direction, no false positive there.
        # Production paths are forward-slash throughout (norm_path lowercases the
        # drive and forward-slashes), so .as_posix() is also the shape production
        # actually presents to this hook.
        for cmd in (
            f"echo x > {PROJECT_ROOT.as_posix()}/agents/{AGENT}/temp/probe.txt",
            f"echo x > {PROJECT_ROOT.as_posix()}/core/logs/probe.txt",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_verdict(cmd), "approve")

    def test_product_repo_write_path_stays_usable(self):
        """Goal outcome (3): the AGENT_WRITE_PATH carve-out exists on the Edit
        side and must behave identically here — including the ';'-separated
        multi-root form."""
        conf = PROJECT_ROOT / "agents" / AGENT / "local-paths.conf"
        paths = read_paths_conf(str(conf))
        awps = [norm_path(p.strip())
                for p in (paths.get("AGENT_WRITE_PATH") or "").split(";")
                if p.strip()]
        if not awps:
            self.skipTest("no AGENT_WRITE_PATH configured for this agent")
        for awp in awps:
            with self.subTest(root=awp):
                self.assertEqual(bash_verdict(f"echo x > {awp}/probe-g1153338.txt"),
                                 "approve")

    def test_heredoc_body_is_not_scanned_as_command(self):
        """Every out-of-root candidate that survived the allowlist over 11,559
        real Bash calls came from a heredoc body — prose or inline source read
        as command text."""
        prose = ("cat <<'JSON' | bash core/scripts/reasoning-bank-add.sh\n"
                 '{"content": "goals that touch / are risky; write > report.txt"}\n'
                 "JSON")
        self.assertEqual(bash_verdict(prose), "approve")
        inline = "py -3 - <<'PY'\nif n > 0:\n    print(n)\nPY"
        self.assertEqual(bash_verdict(inline), "approve")

    def test_echo_payload_span_is_not_scanned_as_command(self):
        """`echo '<json>' | script` is the dominant framework idiom. When the
        payload quotes a path the extractor read it as a redirect target and
        refused an ordinary data write. Zero occurrences in 16,830 historical
        calls, then it fired on the FIRST live probe of this feature — a corpus
        bounds only the FP classes it happens to contain."""
        payload = ("echo '{\"tool_input\":{\"command\":\"echo hi > "
                   "/opt/not-a-root/x.txt\"}}' | bash core/scripts/board-post.sh")
        self.assertEqual(bash_verdict(payload), "approve")
        self.assertEqual(
            bash_verdict("printf '%s' 'see /opt/elsewhere/notes.txt' | cat"),
            "approve")

    def test_quoted_redirect_target_survives_payload_strip(self):
        """The strip must not blank a quoted TARGET — it sits outside the span.
        rb-401: check the false-NEGATIVE direction when tightening."""
        self.assertEqual(bash_verdict("echo hi > '/opt/not-a-root/y.txt'"), "deny")
        self.assertEqual(bash_verdict('echo hi > "/opt/not-a-root/z.txt"'), "deny")

    def test_python_comparison_is_not_a_windows_drive(self):
        """`if n > 0:` yields the redirect token `0:`; the drive-prefix test
        must require an ALPHABETIC first character or it reads as absolute."""
        self.assertEqual(bash_verdict("py -3 -c 'print(1 if x > 0 else 2)'"),
                         "approve")

    def test_left_shift_does_not_swallow_later_lines(self):
        """`<<` is ALSO left-shift. `1 << n` matched the heredoc-opener regex
        with delimiter `n`, and every line after it was dropped as heredoc
        body — so a following out-of-root write extracted NO target and sailed
        past the refusal. Fresh-eyes probe F-1 (g-115-3338); the terminator-
        exists guard in strip_heredoc_bodies is the fix. False-NEGATIVE
        direction, per rb-401."""
        self.assertEqual(bash_verdict("py -3 -c 'x = 1 << n'"), "approve")
        self.assertEqual(
            bash_verdict("py -3 -c 'x = 1 << n'\nmkdir -p /opt/definitely-not-a-configured-root/e"),
            "deny")

    def test_herestring_is_not_a_heredoc(self):
        """`<<<` matched the opener regex at its SECOND `<`, so
        `cat <<< "hello"` read as a heredoc delimited by `hello` and swallowed
        the rest of the command. Fresh-eyes probe F-2 (g-115-3338)."""
        self.assertEqual(
            bash_verdict('cat <<< "hello"\nmkdir -p /opt/definitely-not-a-configured-root/e'),
            "deny")

    # --- no regression in the pre-existing cruft branch ----------------
    def test_new_toplevel_under_agent_dir_still_denied(self):
        self.assertEqual(
            bash_verdict(f"mkdir -p agents/{AGENT}/invented-toplevel-g1153338"),
            "deny")

    def test_long_flag_and_multi_arg_bypasses_still_denied(self):
        """rb-401 — the false-NEGATIVE direction. These four shapes were the
        2026-05-21 fresh-eyes bypasses; tightening must not resurrect them."""
        for cmd in (
            f"mkdir --parents agents/{AGENT}/invented-longflag-g1153338",
            f"mkdir -- agents/{AGENT}/invented-endflags-g1153338",
            f"mkdir agents/{AGENT}/temp agents/{AGENT}/invented-secondarg-g1153338",
            f"touch agents/{AGENT}/invented-touch-g1153338/x",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_verdict(cmd), "deny")


class TestPathRootsHelper(unittest.TestCase):
    """The shared module is the SSOT both hooks now import."""

    def test_exempt_sink_membership(self):
        for p in ("/dev/null", "/dev/fd/2", "/proc/self/fd/1",
                  "/tmp/x.json", "/tmp/claude-0/s/scratch/n.md", "/var/tmp/y"):
            self.assertTrue(is_write_exempt_sink(p), p)
        for p in ("/devious/x", "/tmpfoo/y", "/opt/other/z", "", "/"):
            self.assertFalse(is_write_exempt_sink(p), p)

    def test_multi_root_agent_write_path_splits(self):
        roots = compute_allowed_roots(
            "/repo", {"WORLD_PATH": "/world", "META_PATH": "/meta",
                      "AGENT_WRITE_PATH": "/opt/one;/opt/two"})
        self.assertEqual(
            roots,
            [("PROJECT_ROOT", "/repo"), ("WORLD_PATH", "/world"),
             ("META_PATH", "/meta"), ("AGENT_WRITE_PATH", "/opt/one"),
             ("AGENT_WRITE_PATH", "/opt/two")])

    def test_single_letter_first_segment_normalizes_to_a_drive(self):
        """PINNED, pre-existing behavior — NOT introduced here. norm_path's
        MSYS2 rule rewrites `/c/foo` -> `c:/foo`, and its guard is only
        "second char is alphabetic", so ANY absolute POSIX path whose first
        segment is a single letter is rewritten into a Windows drive path
        (`/a/one` -> `a:/one`). Harmless on Windows/MSYS2, wrong on Linux.
        Both hooks carried this identically before consolidation, so pinning
        it here documents the property rather than silently changing
        normalization under a gate that fires on every write. Tracked
        separately — do NOT "fix" this without measuring the blast radius."""
        self.assertEqual(norm_path("/a/one"), "a:/one")
        self.assertEqual(norm_path("/opt/one"), "/opt/one")

    def test_empty_and_missing_values_are_dropped(self):
        self.assertEqual(compute_allowed_roots("/repo", {}),
                         [("PROJECT_ROOT", "/repo")])
        self.assertEqual(compute_allowed_roots("/repo", None),
                         [("PROJECT_ROOT", "/repo")])
        self.assertEqual(
            compute_allowed_roots("/repo", {"AGENT_WRITE_PATH": "; ;"}),
            [("PROJECT_ROOT", "/repo")])

    def test_payload_span_blanks_by_offset_not_str_replace(self):
        """`str.replace(inner, ...)` hit the payload's first occurrence inside
        the MATCHED TEXT, which can be the verb: `echo 'o'` blanked the 'o' of
        "echo" and left the payload intact. Harmless (a 1-char payload is never
        a path token) but wrong. Fresh-eyes probe F-3 (g-115-3338)."""
        sys.path.insert(0, str(SCRIPTS))
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("_bh_probe", str(BASH_HOOK))
        mod = importlib.util.module_from_spec(spec)
        real, sys.stdin = sys.stdin, io.StringIO("")
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        finally:
            sys.stdin = real
        self.assertEqual(mod.strip_payload_spans("echo 'o'"), "echo ' '")
        self.assertEqual(mod.strip_payload_spans("printf 'p'"), "printf ' '")
        # the long-payload case must keep working
        self.assertEqual(
            mod.strip_payload_spans("echo 'cp /opt/a /opt/b'"),
            "echo '                '")

    def test_both_hooks_import_the_same_helper(self):
        """Regression guard for the drift this goal removed: neither hook may
        re-declare a local copy of the shared primitives."""
        for hook in (BASH_HOOK, EDIT_HOOK):
            src = hook.read_text(encoding="utf-8")
            self.assertIn("from _path_roots import", src, hook.name)
            for fn in ("norm_path", "is_under", "is_new_toplevel",
                       "read_paths_conf"):
                self.assertNotIn(f"\ndef {fn}(", src,
                                 f"{hook.name} re-declares {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
