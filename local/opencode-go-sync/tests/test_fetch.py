"""Fetch-layer suite for the sync (ticket 03).

Standard library only. No public internet: a localhost ``http.server`` thread
simulates the OpenCode Go endpoints, a closed localhost port simulates a
refused connection, and ``/bin/cat`` / a deliberately failing command stand in
for the installed binary's ``models`` command.

Run with either:

    python3 -m unittest discover -s tests        # from the kit root
    python3 sync.py --self-test                  # canonical gate command
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

import sync  # noqa: E402  (import after sys.path setup)


# --------------------------------------------------------------------------- #
# Samples
# --------------------------------------------------------------------------- #

CHAT = "https://opencode.ai/inference/go/v1/chat/completions"
RESP = "https://opencode.ai/inference/go/v1/responses"
GOOGLE = "https://opencode.ai/inference/go/v1/models/gemini-4-argon-contributor"

DOCS_HEADER = (
    "| Model | Model ID | Endpoint | AI SDK Package |\n"
    "| ----- | -------- | -------- | -------------- |\n"
)


def make_live(ids) -> str:
    data = [{"id": i, "object": "model", "created": 0, "owned_by": "opencode"} for i in ids]
    return json.dumps({"object": "list", "data": data})


def make_docs(rows) -> str:
    body = "".join(f"| {name} | {mid} | `{ep}` | `pkg` |\n" for name, mid, ep in rows)
    return "# Go\n\n" + DOCS_HEADER + body


LIVE_IDS = ["glm-5.2", "gpt-5.6-luna", "gemini-4-argon-contributor", "omen-alpha"]
LIVE_JSON = make_live(LIVE_IDS)
DOCS_MD = make_docs(
    [
        ("GLM-5.2", "glm-5.2", CHAT),
        ("GPT 5.6 Luna", "gpt-5.6-luna", RESP),
        ("Gemini", "gemini-4-argon-contributor", GOOGLE),
    ]
)
HTML_DOCS = "<!DOCTYPE html><html><body>Go</body></html>"

# A realistic `claude-code-proxy models`-style roster: an opencode group to
# skip, a cursor group carrying the trailing `;` note, and codex/kimi/grok.
REALISTIC_ROSTER = (
    "codex: claude-opus-5, fable, gpt-5.6-luna, gpt-6-sol, gpt-6-sol-fast, haiku\n"
    "kimi: kimi-for-coding, kimi-k2.6, kimi-k3\n"
    "grok: grok-4.6, grok-4.7\n"
    "opencode: glm-5.2, glm-5.3, opencode-go/glm-5.2, omen-alpha\n"
    "cursor: composer-2.5, composer-2.5-fast, cursor, cursor-plan; "
    "0 cursor model aliases run `claude-code-proxy models --full` for all aliases\n"
)
REALISTIC_ROSTER_IDS = {
    "claude-opus-5",
    "fable",
    "gpt-5.6-luna",
    "gpt-6-sol",
    "gpt-6-sol-fast",
    "haiku",
    "kimi-for-coding",
    "kimi-k2.6",
    "kimi-k3",
    "grok-4.6",
    "grok-4.7",
    "composer-2.5",
    "composer-2.5-fast",
    "cursor",
    "cursor-plan",
}


# --------------------------------------------------------------------------- #
# Localhost server harness
# --------------------------------------------------------------------------- #


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # keep the suite quiet
        pass

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        route = self.server.routes.get(self.path)
        if route is None:
            status, ctype, body = 404, "text/plain", "not found"
        elif callable(route):
            status, ctype, body = route(dict(self.headers))
        else:
            status, ctype, body = route
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def docs_negotiated(headers):
    """HTML unless the client asked for markdown (mirrors the real endpoint)."""
    if "text/markdown" in headers.get("Accept", ""):
        return 200, "text/markdown; charset=utf-8", DOCS_MD
    return 200, "text/html", HTML_DOCS


class FetchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sync-fetch-test-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        # Make localhost requests deterministic even on a machine with *_proxy set.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        urllib.request.install_opener(opener)
        self.addCleanup(
            lambda: urllib.request.install_opener(urllib.request.build_opener())
        )

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write(self, name: str, text: str) -> str:
        p = self.path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as handle:
            handle.write(text)
        return p

    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def start_server(self, routes) -> str:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.routes = routes
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def closed_url(attempts: int = 20) -> str:
        # A bound-then-closed port is *usually* free, but another process could
        # rebind it before the client connects, making the "connection refused"
        # test flaky. Confirm refusal by probing each candidate port; on a
        # successful connect (someone is listening) pick a fresh port and retry.
        for _ in range(attempts):
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            with socket.socket() as check:
                check.settimeout(2.0)
                try:
                    check.connect(("127.0.0.1", port))
                except ConnectionRefusedError:
                    return f"http://127.0.0.1:{port}"
                except OSError:
                    continue  # timeout / transient error: try a fresh port
                else:
                    continue  # the port was rebound: try a fresh port
        raise AssertionError("could not find a closed localhost port after retries")

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.main(argv)
        return code, out.getvalue(), err.getvalue()

    def base_args(self, overlay: str, report: str, repo: str) -> list[str]:
        return [
            "--overrides",
            self.write("overrides.toml", "exclude = []\n"),
            "--overlay",
            overlay,
            "--report",
            report,
            "--repo",
            repo,
            # These cases exercise fetch/generation only; the ticket-04 gated
            # install (on by default in the CLI) is covered by test_install.py.
            "--no-install",
        ]


# --------------------------------------------------------------------------- #
# Content negotiation
# --------------------------------------------------------------------------- #


class NegotiationTests(FetchCase):
    def test_docs_fetch_requests_markdown_and_parses_table(self):
        base = self.start_server({"/docs": docs_negotiated})
        body = sync.fetch_docs(base + "/docs", 5.0)
        self.assertIn("| Model ID |", body)
        self.assertIn("glm-5.2", sync.parse_docs_table(body))

    def test_plain_accept_would_get_html(self):
        base = self.start_server({"/docs": docs_negotiated})
        html = sync.http_get_text(base + "/docs", 5.0, "docs", "docs")
        self.assertTrue(sync._looks_like_html(html))

    def test_html_body_is_a_visible_drift_failure(self):
        base = self.start_server({"/docs": (200, "text/html", HTML_DOCS)})
        with self.assertRaises(sync.SyncFailure) as raised:
            sync.fetch_docs(base + "/docs", 5.0)
        self.assertEqual(raised.exception.stage, "docs")
        self.assertIn("HTML", raised.exception.reason)

    def test_requests_identify_client_not_python_urllib(self):
        # Cloudflare rejects the default urllib UA (403, error 1010).
        def cloudflare_like(headers):
            if "python-urllib" in headers.get("User-Agent", "").lower():
                return 403, "text/plain", "error code: 1010"
            return 200, "application/json", LIVE_JSON

        base = self.start_server({"/models": cloudflare_like})
        ids = sync.parse_live_list(sync.fetch_live_list(base + "/models", 5.0))
        self.assertEqual(ids, sorted(LIVE_IDS))


# --------------------------------------------------------------------------- #
# Roster extraction
# --------------------------------------------------------------------------- #


class RosterExtractionTests(FetchCase):
    def test_parse_realistic_models_output(self):
        roster = sync.parse_roster(REALISTIC_ROSTER)
        self.assertEqual(roster, REALISTIC_ROSTER_IDS)
        # opencode group skipped entirely; cursor `;` note dropped.
        for mid in ("glm-5.2", "opencode-go/glm-5.2", "omen-alpha"):
            self.assertNotIn(mid, roster)
        self.assertNotIn("for all aliases", roster)

    def test_colon_prefixed_forms_are_not_bare_ids(self):
        roster = sync.parse_roster("cursor: composer-2.5, cursor:composer-2.5, cursor-agent\n")
        self.assertEqual(roster, {"composer-2.5", "cursor-agent"})

    def test_preferred_command_extraction(self):
        roster_file = self.write("roster.txt", REALISTIC_ROSTER)
        opts = sync.Options(
            live=None, docs=None, roster=None, overrides="", previous=None,
            overlay="", report="", fetch=True, roster_command=f"/bin/cat {roster_file}",
        )
        text, how = sync.extract_roster_text(opts)
        self.assertIn("command", how)
        self.assertEqual(sync.parse_roster(text), REALISTIC_ROSTER_IDS)

    def test_failing_command_falls_back_to_working_copy_sources(self):
        repo = os.path.join(self.dir, "repo")
        self.write(
            "repo/src/registry.rs",
            'pub const ANTHROPIC_STYLE_ALIASES: &[&str] = &["haiku", "opus"];\n'
            'pub(crate) const CODEX_MODELS: &[&str] = &["gpt-6-astra", "gpt-5.6-luna"];\n'
            'pub(crate) const KIMI_MODELS: &[&str] = &["kimi-k3"];\n'
            'pub(crate) const GROK_MODELS: &[&str] = &["grok-4.7"];\n'
            'const CURSOR_LEGACY_MODELS: &[&str] = &["cursor", "composer-2.5"];\n',
        )
        self.write(
            "repo/src/providers/codex/translate/model_allowlist.rs",
            'pub const ULTRAFAST_MODELS: &[&str] = &["gpt-6-astra"];\n',
        )
        opts = sync.Options(
            live=None, docs=None, roster=None, overrides="", previous=None,
            overlay="", report="", fetch=True, repo=repo,
            roster_command="/nonexistent/claude-code-proxy models",
        )
        text, how = sync.extract_roster_text(opts)
        self.assertIn("fallback", how)
        self.assertEqual(
            sync.parse_roster(text),
            {
                "haiku", "opus", "gpt-6-astra", "gpt-6-astra-fast",
                "gpt-6-astra-ultrafast", "gpt-5.6-luna", "gpt-5.6-luna-fast",
                "kimi-k3", "grok-4.7", "cursor", "composer-2.5",
            },
        )


# --------------------------------------------------------------------------- #
# End-to-end from injected localhost responses
# --------------------------------------------------------------------------- #


class EndToEndTests(FetchCase):
    def test_live_run_writes_overlay_and_report(self):
        base = self.start_server({"/models": (200, "application/json", LIVE_JSON), "/docs": docs_negotiated})
        roster_file = self.write("roster.txt", REALISTIC_ROSTER)
        overlay = self.path("overlay_generated.rs")
        report = self.path("report.json")
        args = self.base_args(overlay, report, self.dir) + [
            "--live-url", base + "/models",
            "--docs-url", base + "/docs",
            "--roster-command", f"/bin/cat {roster_file}",
        ]
        code, _out, err = self.run_cli(args)
        self.assertEqual(code, sync.EXIT_UPDATED, err)

        out = self.read(overlay)
        self.assertIn('id: "glm-5.2", endpoint: EndpointKind::ChatCompletions', out)
        self.assertIn('id: "gpt-5.6-luna", endpoint: EndpointKind::Responses', out)
        self.assertIn('id: "gpt-5.6-luna"', out)
        self.assertIn("advert: Advertisement::QualifiedOnly", out)
        self.assertIn('id: "glm-5.2"', out)
        self.assertNotIn("gemini-4-argon-contributor", out)
        self.assertNotIn("omen-alpha", out)

        report_doc = json.loads(self.read(report))
        self.assertEqual(report_doc["outcome"], "updated")
        self.assertEqual(report_doc["counts"]["live"], 4)
        self.assertEqual(report_doc["counts"]["routable"], 2)
        excl = {e["id"]: e["reason"] for e in report_doc["exclusions"]}
        self.assertIn("unsupported endpoint", excl["gemini-4-argon-contributor"])
        self.assertEqual(excl["omen-alpha"], "no documented endpoint")
        self.assertIn(base + "/models", report_doc["sources"]["live_list"])

        # Idempotent second run.
        again, _o, _e = self.run_cli(args)
        self.assertEqual(again, sync.EXIT_NO_CHANGE)


# --------------------------------------------------------------------------- #
# Failures leave the prior overlay byte-identical
# --------------------------------------------------------------------------- #


class FetchFailureTests(FetchCase):
    def assert_untouched(self, live_url, docs_url, roster_cmd, reason_fragment, *, repo=None):
        overlay = self.path("overlay_generated.rs")
        report = self.path("report.json")
        with open(overlay, "wb") as handle:
            handle.write(b"// sentinel: do not touch\n")
        args = self.base_args(overlay, report, repo or self.dir) + [
            "--live-url", live_url,
            "--docs-url", docs_url,
            "--roster-command", roster_cmd,
        ]
        code, _out, err = self.run_cli(args)
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertEqual(self.read_bytes(overlay), b"// sentinel: do not touch\n")
        self.assertIn(reason_fragment, err)
        self.assertIn("failed", err)
        doc = json.loads(self.read(report))
        self.assertEqual(doc["outcome"], "failed")
        return doc

    def test_connection_refused(self):
        base = self.start_server({})
        doc = self.assert_untouched(
            self.closed_url() + "/models", base + "/docs",
            f"/bin/cat {self.write('roster.txt', REALISTIC_ROSTER)}",
            "live list fetch failed",
        )
        self.assertEqual(doc["stage"], "live-list")

    def test_http_error(self):
        base = self.start_server({"/models": (500, "text/plain", "boom"), "/docs": docs_negotiated})
        doc = self.assert_untouched(
            base + "/models", base + "/docs",
            f"/bin/cat {self.write('roster.txt', REALISTIC_ROSTER)}",
            "HTTP 500",
        )
        self.assertEqual(doc["stage"], "live-list")

    def test_malformed_docs_html_where_markdown_expected(self):
        base = self.start_server({"/models": (200, "application/json", LIVE_JSON), "/docs": (200, "text/html", HTML_DOCS)})
        doc = self.assert_untouched(
            base + "/models", base + "/docs",
            f"/bin/cat {self.write('roster.txt', REALISTIC_ROSTER)}",
            "docs drift",
        )
        self.assertEqual(doc["stage"], "docs")

    def test_malformed_docs_table_missing(self):
        base = self.start_server({"/models": (200, "application/json", LIVE_JSON), "/docs": (200, "text/markdown", "# Go\n\nNo table.\n")})
        doc = self.assert_untouched(
            base + "/models", base + "/docs",
            f"/bin/cat {self.write('roster.txt', REALISTIC_ROSTER)}",
            "docs drift",
        )
        self.assertEqual(doc["stage"], "docs")

    def test_roster_command_failure_no_fallback(self):
        base = self.start_server({"/models": (200, "application/json", LIVE_JSON), "/docs": docs_negotiated})
        empty_repo = os.path.join(self.dir, "empty-repo")
        os.makedirs(empty_repo)
        doc = self.assert_untouched(
            base + "/models", base + "/docs",
            "/nonexistent/claude-code-proxy models",
            "roster extraction failed",
            repo=empty_repo,
        )
        self.assertEqual(doc["stage"], "roster")

    def test_roster_empty_extraction(self):
        base = self.start_server({"/models": (200, "application/json", LIVE_JSON), "/docs": docs_negotiated})
        only_opencode = self.write(
            "only-opencode.txt", "opencode: glm-5.2, opencode-go/glm-5.2\n"
        )
        doc = self.assert_untouched(
            base + "/models", base + "/docs",
            f"/bin/cat {only_opencode}",
            "no other-provider ids",
        )
        self.assertEqual(doc["stage"], "roster")


if __name__ == "__main__":
    unittest.main()
