"""Gated-install suite for the sync (ticket 04).

Standard library only; no cargo, no network. The gate, build and install steps
are simulated with tiny ``python -c`` commands injected through the ``Options``
struct, so the suite stays fast and offline (it is itself a pre-install gate).

Run with either:

    python3 -m unittest discover -s tests        # from the kit root
    python3 sync.py --self-test                  # canonical gate command
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

import sync  # noqa: E402  (import after sys.path setup)


CHAT = "https://opencode.ai/inference/go/v1/chat/completions"
DOCS_HEADER = (
    "| Model | Model ID | Endpoint | AI SDK Package |\n"
    "| ----- | -------- | -------- | -------------- |\n"
)

# The fixture-mode overlay (kit fixtures + empty overrides) must stay
# byte-stable: the ticket-02/03 regression hash.
FIXTURE_OVERLAY_SHA256 = (
    "e3481429cf49a32487258e31b68a1c565fedd71ce6f864d7c212d2cbc340c587"
)


def make_docs(rows) -> str:
    body = "".join(f"| {n} | {m} | `{e}` | `pkg` |\n" for n, m, e in rows)
    return "# Go\n\n" + DOCS_HEADER + body


def make_live(ids) -> str:
    data = [{"id": i, "object": "model", "created": 0, "owned_by": "opencode"} for i in ids]
    return json.dumps({"object": "list", "data": data})


class InstallCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sync-install-test-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.log = self.path("gate.log")

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write(self, name: str, text: str) -> str:
        p = self.path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as handle:
            handle.write(text)
        return p

    def write_bytes(self, name: str, data: bytes) -> str:
        p = self.path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as handle:
            handle.write(data)
        return p

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def read_json(self, path: str) -> dict:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    # -- injected commands -------------------------------------------------- #

    def marker_cmd(self, label: str) -> list[str]:
        """A passing step that records its label in the shared log."""
        code = f"open({self.log!r}, 'a').write({label!r} + '\\n')"
        return [sys.executable, "-c", code]

    def failing_cmd(self, label: str) -> list[str]:
        """A failing step that records its label and exits non-zero."""
        code = f"open({self.log!r}, 'a').write({label!r} + '\\n');import sys;sys.exit(3)"
        return [sys.executable, "-c", code]

    def markers(self) -> list[str]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return handle.read().split()

    # -- options ------------------------------------------------------------ #

    def options(self, live, docs, roster="codex: gpt-5.6-luna\n", overrides="exclude = []\n"):
        live_p = self.write("live.json", live)
        docs_p = self.write("docs.md", docs)
        roster_p = self.write("roster.txt", roster)
        ovr_p = self.write("overrides.toml", overrides)
        overlay = self.path("overlay_generated.rs")
        built = self.write_bytes("built-binary", b"BUILT-BINARY-v1\n")
        target = self.path("bin/claude-code-proxy")
        opts = sync.Options(
            live=live_p,
            docs=docs_p,
            roster=roster_p,
            overrides=ovr_p,
            previous=overlay,
            overlay=overlay,
            report=self.path("report.json"),
            repo=self.dir,
            install_enabled=True,
            install_target=target,
            built_binary=built,
        )
        self.passing_commands(opts)
        return opts

    def passing_commands(self, opts: sync.Options) -> None:
        opts.gate_fmt_cmd = self.marker_cmd("fmt")
        opts.gate_selftest_cmd = self.marker_cmd("selftest")
        opts.gate_test_cmds = [self.marker_cmd("test1"), self.marker_cmd("test2")]
        opts.build_cmd = self.marker_cmd("build")

    def run_sync(self, opts: sync.Options) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.execute(opts)
        return code, err.getvalue()


# --------------------------------------------------------------------------- #
# Happy path: gate + build + install + backup-once
# --------------------------------------------------------------------------- #


class InstallSuccessTests(InstallCase):
    def test_all_green_installs_atomically_and_backs_up_once(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        # A pre-existing release binary to back up.
        old = b"OLD-RELEASE-BINARY\n"
        self.write_bytes("bin/claude-code-proxy", old)

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(self.markers(), ["fmt", "selftest", "test1", "test2", "build"])
        # Installed: target == built binary, executable.
        self.assertEqual(self.read_bytes(opts.install_target), b"BUILT-BINARY-v1\n")
        self.assertTrue(os.access(opts.install_target, os.X_OK))
        # Backup created once with the previous bytes.
        backup = opts.install_target + sync.BACKUP_SUFFIX
        self.assertEqual(self.read_bytes(backup), old)
        # The generated overlay landed.
        self.assertIn("SYNCED: bool = true", self.read_bytes(opts.overlay).decode())
        report = self.read_json(opts.report)
        self.assertEqual(report["outcome"], "updated")
        self.assertTrue(report["install"]["installed"])
        self.assertEqual(report["install"]["backup"], backup)

        # Second change + install must NOT overwrite the backup.
        self.write_bytes("built-binary", b"BUILT-BINARY-v2\n")
        self.write("live.json", make_live(["glm-5.2", "kimi-k3"]))
        self.write("docs.md", make_docs([("GLM", "glm-5.2", CHAT), ("Kimi", "kimi-k3", CHAT)]))
        code2, _err2 = self.run_sync(opts)
        self.assertEqual(code2, sync.EXIT_UPDATED)
        self.assertEqual(self.read_bytes(opts.install_target), b"BUILT-BINARY-v2\n")
        self.assertEqual(self.read_bytes(backup), old, "existing backup must never be overwritten")

    def test_absent_target_needs_no_backup(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.assertFalse(os.path.exists(opts.install_target))
        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertTrue(os.path.exists(opts.install_target))
        self.assertFalse(os.path.exists(opts.install_target + sync.BACKUP_SUFFIX))

    def test_second_identical_install_does_not_rewrite_backup(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        old = b"OLD\n"
        self.write_bytes("bin/claude-code-proxy", old)
        self.run_sync(opts)
        backup = opts.install_target + sync.BACKUP_SUFFIX
        first_mtime = os.stat(backup).st_mtime_ns
        # A fresh change forces a second install; the backup mtime must not move.
        self.write("live.json", make_live(["glm-5.2", "hy3"]))
        self.write("docs.md", make_docs([("GLM", "glm-5.2", CHAT), ("Hy", "hy3", CHAT)]))
        self.run_sync(opts)
        self.assertEqual(os.stat(backup).st_mtime_ns, first_mtime)


# --------------------------------------------------------------------------- #
# Failure semantics: revert overlay, exit 1, target untouched
# --------------------------------------------------------------------------- #


class InstallFailureTests(InstallCase):
    def _prepare(self, previous: bytes | None):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        old = b"OLD-RELEASE-BINARY\n"
        self.write_bytes("bin/claude-code-proxy", old)
        if previous is not None:
            self.write_bytes("overlay_generated.rs", previous)
        return opts, old

    def test_gate_failure_reverts_overlay_and_keeps_target(self):
        opts, old = self._prepare(b"// PREVIOUS OVERLAY\n")
        opts.gate_fmt_cmd = self.failing_cmd("fmt")

        code, err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertEqual(self.read_bytes(opts.overlay), b"// PREVIOUS OVERLAY\n")
        self.assertEqual(self.read_bytes(opts.install_target), old)
        # Only the first (failing) gate step ran; no build/install markers.
        self.assertEqual(self.markers(), ["fmt"])
        self.assertIn("install aborted", err)
        self.assertIn("previous overlay restored", err)
        self.assertIn("failed", err)
        self.assertEqual(self.read_json(opts.report)["outcome"], "failed")

    def test_build_failure_reverts_overlay_and_keeps_target(self):
        opts, old = self._prepare(b"// PREVIOUS OVERLAY\n")
        opts.build_cmd = self.failing_cmd("build")

        code, err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertEqual(self.read_bytes(opts.overlay), b"// PREVIOUS OVERLAY\n")
        self.assertEqual(self.read_bytes(opts.install_target), old)
        self.assertEqual(self.markers(), ["fmt", "selftest", "test1", "test2", "build"])
        self.assertIn("install aborted", err)

    def test_install_failure_reverts_overlay_and_keeps_target(self):
        opts, old = self._prepare(b"// PREVIOUS OVERLAY\n")
        opts.install_cmd = self.failing_cmd("install")

        code, err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertEqual(self.read_bytes(opts.overlay), b"// PREVIOUS OVERLAY\n")
        self.assertEqual(self.read_bytes(opts.install_target), old)
        self.assertIn("install", self.markers())
        self.assertIn("install aborted", err)

    def test_failure_removes_new_overlay_when_none_existed(self):
        # No previous overlay: a post-write failure must leave no overlay at all.
        opts, old = self._prepare(None)
        opts.gate_selftest_cmd = self.failing_cmd("selftest")
        self.assertFalse(os.path.exists(opts.overlay))

        code, err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertFalse(os.path.exists(opts.overlay), "no partial overlay may remain")
        self.assertEqual(self.read_bytes(opts.install_target), old)
        self.assertIn("install aborted", err)

    def test_gate_failure_report_includes_install_stub(self):
        # A failure after the overlay was written still yields a self-contained
        # install block; the gate aborts before the backup step, so no backup.
        opts, _old = self._prepare(b"// PREVIOUS OVERLAY\n")
        opts.gate_fmt_cmd = self.failing_cmd("fmt")

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        report = self.read_json(opts.report)
        self.assertEqual(report["outcome"], "failed")
        install = report["install"]
        self.assertEqual(install["target"], opts.install_target)
        self.assertFalse(install["installed"])
        self.assertTrue(install["aborted"])
        self.assertIsNone(install["backup"])

    def test_install_failure_report_records_existing_backup(self):
        # An install failure happens after the backup step, so the stub points at
        # the backup taken from the pre-install target.
        opts, _old = self._prepare(b"// PREVIOUS OVERLAY\n")
        opts.install_cmd = self.failing_cmd("install")

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        install = self.read_json(opts.report)["install"]
        self.assertFalse(install["installed"])
        self.assertTrue(install["aborted"])
        self.assertEqual(install["backup"], opts.install_target + sync.BACKUP_SUFFIX)


# --------------------------------------------------------------------------- #
# No-change runs skip the gate, build and install
# --------------------------------------------------------------------------- #


class NoChangeTests(InstallCase):
    def test_no_change_skips_gate_build_and_install(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.write_bytes("bin/claude-code-proxy", b"OLD\n")

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        first_markers = self.markers()
        self.assertEqual(first_markers, ["fmt", "selftest", "test1", "test2", "build"])
        target_mtime = os.stat(opts.install_target).st_mtime_ns

        # Identical inputs: no change -> no gate, no build, no install.
        code2, _err2 = self.run_sync(opts)
        self.assertEqual(code2, sync.EXIT_NO_CHANGE)
        self.assertEqual(self.markers(), first_markers)
        self.assertEqual(os.stat(opts.install_target).st_mtime_ns, target_mtime)
        report = self.read_json(opts.report)
        self.assertEqual(report["outcome"], "no-change")
        self.assertFalse(report["install"]["installed"])


# --------------------------------------------------------------------------- #
# Install stamp: stale-binary recovery
# --------------------------------------------------------------------------- #


class StaleBinaryTests(InstallCase):
    def _installed(self) -> sync.Options:
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.write_bytes("bin/claude-code-proxy", b"OLD\n")
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        return opts

    def stamp(self, opts: sync.Options) -> str:
        return opts.install_target + sync.INSTALL_STAMP_SUFFIX

    def digest(self, opts: sync.Options) -> str:
        return hashlib.sha256(self.read_bytes(opts.overlay)).hexdigest()

    def test_successful_install_writes_stamp_matching_overlay(self):
        opts = self._installed()
        self.assertEqual(self.read_bytes(self.stamp(opts)).decode().strip(), self.digest(opts))
        install = self.read_json(opts.report)["install"]
        self.assertFalse(install["stale_binary"])
        self.assertEqual(install["stamp"], self.stamp(opts))

    def test_missing_stamp_forces_rebuild_without_rewriting_overlay(self):
        opts = self._installed()
        first_markers = self.markers()
        overlay_mtime = os.stat(opts.overlay).st_mtime_ns
        target_mtime = os.stat(opts.install_target).st_mtime_ns
        os.unlink(self.stamp(opts))

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(
            self.markers(), first_markers + ["fmt", "selftest", "test1", "test2", "build"]
        )
        self.assertEqual(
            os.stat(opts.overlay).st_mtime_ns,
            overlay_mtime,
            "a stale-binary-only run must not rewrite the matching overlay",
        )
        self.assertNotEqual(os.stat(opts.install_target).st_mtime_ns, target_mtime)
        self.assertEqual(self.read_bytes(self.stamp(opts)).decode().strip(), self.digest(opts))
        self.assertTrue(self.read_json(opts.report)["install"]["stale_binary"])

    def test_mismatched_stamp_forces_rebuild(self):
        opts = self._installed()
        self.write("bin/claude-code-proxy.catalog-sha256", "0" * 64 + "\n")

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(self.read_bytes(self.stamp(opts)).decode().strip(), self.digest(opts))

    def test_corrupt_stamp_forces_rebuild(self):
        opts = self._installed()
        self.write_bytes("bin/claude-code-proxy.catalog-sha256", b"\xff\xfegarbage")

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(self.read_bytes(self.stamp(opts)).decode().strip(), self.digest(opts))

    def test_absent_target_does_not_force_a_build(self):
        opts = self._installed()
        first_markers = self.markers()
        os.unlink(opts.install_target)
        os.unlink(self.stamp(opts))

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_NO_CHANGE)
        self.assertEqual(self.markers(), first_markers)
        self.assertFalse(os.path.exists(opts.install_target))

    def test_no_install_ignores_stale_stamp(self):
        opts = self._installed()
        first_markers = self.markers()
        target_mtime = os.stat(opts.install_target).st_mtime_ns
        os.unlink(self.stamp(opts))
        opts.install_enabled = False

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_NO_CHANGE)
        self.assertEqual(self.markers(), first_markers)
        self.assertFalse(os.path.exists(self.stamp(opts)))
        self.assertEqual(os.stat(opts.install_target).st_mtime_ns, target_mtime)

    def test_dry_run_with_stale_stamp_reports_update_but_writes_nothing(self):
        opts = self._installed()
        first_markers = self.markers()
        target_mtime = os.stat(opts.install_target).st_mtime_ns
        os.unlink(self.stamp(opts))
        opts.dry_run = True

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(self.markers(), first_markers)
        self.assertFalse(os.path.exists(self.stamp(opts)))
        self.assertEqual(os.stat(opts.install_target).st_mtime_ns, target_mtime)

    def test_failed_rebuild_keeps_previous_stamp(self):
        opts = self._installed()
        self.write("bin/claude-code-proxy.catalog-sha256", "stale\n")
        target_mtime = os.stat(opts.install_target).st_mtime_ns
        opts.gate_fmt_cmd = self.failing_cmd("fmt")

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertEqual(self.read_bytes(self.stamp(opts)), b"stale\n")
        self.assertEqual(os.stat(opts.install_target).st_mtime_ns, target_mtime)

    def test_live_marker_still_blocks_a_stale_rebuild(self):
        opts = self._installed()
        first_markers = self.markers()
        os.unlink(self.stamp(opts))
        marker = os.path.join(self.dir, sync.UPDATE_STATE_NAME)
        with open(marker, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "timestamp": "2026-10-14T00:00:00Z"}, handle)

        code, err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertIn("another update is in progress", err)
        self.assertEqual(self.markers(), first_markers)
        self.assertFalse(os.path.exists(self.stamp(opts)))

    def test_custom_install_cmd_skips_stamp_and_stale_check(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.write_bytes("bin/claude-code-proxy", b"OLD\n")
        opts.install_cmd = self.marker_cmd("install")

        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        self.assertIn("install", self.markers())
        self.assertFalse(os.path.exists(self.stamp(opts)))
        self.assertIsNone(self.read_json(opts.report)["install"]["stamp"])

        first_markers = self.markers()
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_NO_CHANGE)
        self.assertEqual(self.markers(), first_markers)


# --------------------------------------------------------------------------- #
# Explicit rebuild: --force
# --------------------------------------------------------------------------- #


class ForceRebuildTests(InstallCase):
    def test_force_rebuilds_unchanged_catalog_without_rewriting_overlay(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        first_markers = self.markers()
        overlay_mtime = os.stat(opts.overlay).st_mtime_ns
        stamp = opts.install_target + sync.INSTALL_STAMP_SUFFIX
        stamp_before = self.read_bytes(stamp)

        opts.force = True
        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(
            self.markers(), first_markers + ["fmt", "selftest", "test1", "test2", "build"]
        )
        self.assertEqual(
            os.stat(opts.overlay).st_mtime_ns,
            overlay_mtime,
            "--force must not rewrite an unchanged overlay",
        )
        self.assertEqual(self.read_bytes(stamp), stamp_before)
        install = self.read_json(opts.report)["install"]
        self.assertTrue(install["forced"])
        self.assertFalse(install["stale_binary"])

    def test_force_with_no_install_does_not_build(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        first_markers = self.markers()
        target_mtime = os.stat(opts.install_target).st_mtime_ns
        opts.force = True
        opts.install_enabled = False

        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        self.assertEqual(self.markers(), first_markers)
        self.assertEqual(os.stat(opts.install_target).st_mtime_ns, target_mtime)

    def test_force_dry_run_writes_nothing(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        first_markers = self.markers()
        opts.force = True
        opts.dry_run = True

        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        self.assertEqual(self.markers(), first_markers)


# --------------------------------------------------------------------------- #
# Injection defaults / CLI surface
# --------------------------------------------------------------------------- #


class CommandResolutionTests(InstallCase):
    def test_default_gate_and_build_commands(self):
        opts = sync.Options(
            live=None, docs=None, roster=None, overrides="", previous=None,
            overlay="", report="", repo="/tmp/example-repo", script="/kit/sync.py",
        )
        manifest = "/tmp/example-repo/Cargo.toml"
        commands = sync.gate_commands(opts)
        self.assertEqual(
            commands,
            [
                ["cargo", "fmt", "--check", "--manifest-path", manifest],
                [sys.executable, "/kit/sync.py", "--self-test"],
                ["cargo", "test", "--lib", "providers::opencode", "--manifest-path", manifest],
                ["cargo", "test", "--lib", "registry", "--manifest-path", manifest],
            ],
        )
        self.assertEqual(
            sync.build_command(opts),
            ["cargo", "build", "--release", "--manifest-path", manifest],
        )
        self.assertEqual(sync.install_target_path(opts), sync.INSTALL_TARGET_DEFAULT)
        self.assertEqual(
            sync.built_binary_path(opts),
            os.path.join("/tmp/example-repo", "target", "release", "claude-code-proxy"),
        )

    def test_cli_enables_install_by_default_and_no_install_disables(self):
        args = sync.build_parser().parse_args([])
        self.assertTrue(sync.resolve_paths(args).install_enabled)
        args = sync.build_parser().parse_args(["--no-install"])
        self.assertFalse(sync.resolve_paths(args).install_enabled)

    def test_help_documents_install_flags_and_env(self):
        help_text = sync.build_parser().format_help()
        for token in (
            "--install-target", "--no-install", "--built-binary", "--gate-fmt",
            "--gate-selftest", "--gate-test", "--build-cmd", "--install-cmd",
            "--command-timeout", "--force", "CCP_SYNC_INSTALL", "CCP_SYNC_NO_INSTALL",
            "CCP_SYNC_GATE_TEST", "release-bak", "catalog-sha256", "install stamp",
            # The spec requires the safety valves and the update-state marker to
            # be documented in the script help.
            "CCP_SYNC_ON_SERVE", "CCP_SYNC_JUST_EXECED", "update.state",
        ):
            self.assertIn(token, help_text)


# --------------------------------------------------------------------------- #
# Update-state marker / PID lock (interrupted-update recovery)
# --------------------------------------------------------------------------- #


class UpdateStateTests(InstallCase):
    def _marker(self) -> str:
        return os.path.join(self.dir, sync.UPDATE_STATE_NAME)

    def _dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def _write_marker(self, pid: int, sha: str = "deadbeef") -> None:
        payload = {"pid": pid, "timestamp": "2026-10-14T00:00:00Z", "overlay_sha256": sha}
        with open(self._marker(), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_stale_marker_forces_update_even_when_content_matches(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.write_bytes("bin/claude-code-proxy", b"OLD\n")
        # First run installs; the overlay now equals the generated content.
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        first_markers = self.markers()
        target_mtime = os.stat(opts.install_target).st_mtime_ns
        self.assertFalse(os.path.exists(self._marker()))

        # A dead-PID marker (interrupted update) must force the full update
        # even though the overlay bytes already match.
        self._write_marker(self._dead_pid())
        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertNotEqual(self.markers(), first_markers, "gate/build/install must re-run")
        self.assertNotEqual(os.stat(opts.install_target).st_mtime_ns, target_mtime)
        self.assertFalse(os.path.exists(self._marker()), "marker must be cleared after install")

    def test_live_pid_marker_skips_and_writes_nothing(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.write_bytes("bin/claude-code-proxy", b"OLD\n")
        self._write_marker(os.getpid())  # this test process is alive

        code, err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertIn("another update is in progress", err)
        # Nothing was written: no overlay, no install, no report; marker intact.
        self.assertFalse(os.path.exists(opts.overlay))
        self.assertEqual(self.read_bytes(opts.install_target), b"OLD\n")
        self.assertFalse(os.path.exists(opts.report))
        self.assertTrue(os.path.exists(self._marker()))
        self.assertEqual(self.read_json(self._marker())["pid"], os.getpid())

    def test_success_clears_marker(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        self.assertFalse(os.path.exists(self._marker()))

    def test_handled_failure_reverts_and_clears_marker(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.write_bytes("bin/claude-code-proxy", b"OLD\n")
        self.write_bytes("overlay_generated.rs", b"// PREVIOUS OVERLAY\n")
        opts.gate_fmt_cmd = self.failing_cmd("fmt")

        code, err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertEqual(self.read_bytes(opts.overlay), b"// PREVIOUS OVERLAY\n")
        self.assertFalse(os.path.exists(self._marker()), "handled failure must clear the marker")
        self.assertIn("install aborted", err)

    def test_unwritable_marker_dir_degrades_and_proceeds(self):
        if os.name == "posix" and os.geteuid() == 0:
            self.skipTest("running as root: directory permissions are not enforced")
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        # Put the report — and thus the marker — in its own directory and make
        # that directory unwritable, so the marker cannot be claimed. The run
        # must degrade to proceeding without the marker (never fail the update).
        report_dir = self.path("reportdir")
        os.makedirs(report_dir, exist_ok=True)
        opts.report = os.path.join(report_dir, "report.json")
        marker = os.path.join(report_dir, sync.UPDATE_STATE_NAME)
        os.chmod(report_dir, 0o500)
        self.addCleanup(os.chmod, report_dir, 0o700)

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertFalse(os.path.exists(marker), "no marker can be written in an unwritable dir")
        # Degraded means proceed: the overlay was written and the binary installed.
        self.assertTrue(os.path.exists(opts.overlay))
        self.assertEqual(self.read_bytes(opts.install_target), b"BUILT-BINARY-v1\n")

    def test_no_marker_keeps_the_no_change_fast_path(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.assertEqual(self.run_sync(opts)[0], sync.EXIT_UPDATED)
        first_markers = self.markers()
        self.assertFalse(os.path.exists(self._marker()))

        # Identical inputs and no marker: the no-change fast path is untouched.
        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_NO_CHANGE)
        self.assertEqual(self.markers(), first_markers)
        self.assertFalse(os.path.exists(self._marker()))

    def test_pre_existing_backup_is_never_clobbered(self):
        opts = self.options(make_live(["glm-5.2"]), make_docs([("GLM", "glm-5.2", CHAT)]))
        self.write_bytes("bin/claude-code-proxy", b"NEW-BINARY\n")
        backup = opts.install_target + sync.BACKUP_SUFFIX
        self.write_bytes(backup, b"PRISTINE-BACKUP\n")

        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(self.read_bytes(backup), b"PRISTINE-BACKUP\n")
        self.assertEqual(self.read_bytes(opts.install_target), b"BUILT-BINARY-v1\n")
        # The success report points at the existing backup (matches the stub).
        self.assertEqual(self.read_json(opts.report)["install"]["backup"], backup)


# --------------------------------------------------------------------------- #
# Fixture-mode overlay regression (byte-stable output)
# --------------------------------------------------------------------------- #


class FixtureOverlayRegressionTests(InstallCase):
    def _fixture_options(self) -> sync.Options:
        return sync.Options(
            live=os.path.join(KIT, "fixtures", "live-list.json"),
            docs=os.path.join(KIT, "fixtures", "docs.md"),
            roster=os.path.join(KIT, "fixtures", "roster.txt"),
            overrides=self.write("overrides.toml", "exclude = []\n"),
            previous=self.path("overlay_generated.rs"),
            overlay=self.path("overlay_generated.rs"),
            report=self.path("report.json"),
        )

    def test_fixture_overlay_hash_is_stable(self):
        opts = self._fixture_options()
        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        digest = hashlib.sha256(self.read_bytes(opts.overlay)).hexdigest()
        self.assertEqual(digest, FIXTURE_OVERLAY_SHA256)

    def test_fresh_overlay_is_world_readable_0644(self):
        # mkstemp creates 0600; a fresh generated overlay must not be left 0600.
        opts = self._fixture_options()
        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(os.stat(opts.overlay).st_mode & 0o777, 0o644)

    def test_existing_overlay_mode_is_preserved(self):
        # An overlay that already exists keeps its previous mode across a rewrite.
        opts = self._fixture_options()
        self.write_bytes("overlay_generated.rs", b"// PREVIOUS OVERLAY\n")
        os.chmod(opts.overlay, 0o640)
        code, _err = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertEqual(os.stat(opts.overlay).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
