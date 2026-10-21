"""Fixture suite for the sync generator core (ticket 02).

Standard library only; no network, no cargo. Run with either:

    python3 -m unittest discover -s tests        # from the kit root
    python3 sync.py --self-test                  # canonical gate command
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

import sync  # noqa: E402  (import after sys.path setup)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

DOCS_HEADER = (
    "| Model | Model ID | Endpoint | AI SDK Package |\n"
    "| ----- | -------- | -------- | -------------- |\n"
)


def make_docs(rows, header: str = DOCS_HEADER, prefix: str = "# Go\n\n") -> str:
    body = "".join(f"| {name} | {mid} | `{ep}` | `@ai-sdk/openai-compatible` |\n" for name, mid, ep in rows)
    return prefix + header + body


def make_live(ids) -> str:
    data = [{"id": i, "object": "model", "created": 0, "owned_by": "opencode"} for i in ids]
    return json.dumps({"object": "list", "data": data})


def make_roster(groups: dict[str, list[str]]) -> str:
    if not groups:
        # A provider line with no ids is a valid (empty) roster; an empty file
        # is deliberately treated as extraction failure by the parser.
        return "codex:\n"
    return "\n".join(f"{p}: {', '.join(ids)}" for p, ids in groups.items()) + "\n"


CHAT = "https://opencode.ai/inference/go/v1/chat/completions"
MSG = "https://opencode.ai/inference/go/v1/messages"
RESP = "https://opencode.ai/inference/go/v1/responses"
GOOGLE = "https://opencode.ai/inference/go/v1/models/gemini-4-argon-contributor"


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sync-core-test-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write(self, name: str, text: str) -> str:
        p = self.path(name)
        with open(p, "w", encoding="utf-8") as handle:
            handle.write(text)
        return p

    def options(
        self,
        live: str,
        docs: str,
        roster: str,
        overrides: str,
        previous: str | None = None,
        overlay_name: str = "overlay_generated.rs",
    ) -> sync.Options:
        live_p = self.write("live.json", live)
        docs_p = self.write("docs.md", docs)
        roster_p = self.write("roster.txt", roster)
        ovr_p = self.write("overrides.toml", overrides)
        overlay_p = self.path(overlay_name)
        return sync.Options(
            live=live_p,
            docs=docs_p,
            roster=roster_p,
            overrides=ovr_p,
            previous=previous or overlay_p,
            overlay=overlay_p,
            report=self.path("report.json"),
        )

    def run_sync(self, opts: sync.Options) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sync.execute(opts)

    def run_cli(self, argv: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sync.main(argv)

    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


# --------------------------------------------------------------------------- #
# Protocol resolution
# --------------------------------------------------------------------------- #


class ProtocolTests(TempCase):
    def test_each_protocol_family_resolves(self):
        live = make_live(["m-chat", "m-msg", "m-resp"])
        docs = make_docs([("Chat", "m-chat", CHAT), ("Msg", "m-msg", MSG), ("Resp", "m-resp", RESP)])
        opts = self.options(live, docs, make_roster({"codex": ["gpt-5.6-luna"]}), "exclude = []\n")
        code = self.run_sync(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        out = self.read(opts.overlay)
        self.assertIn('id: "m-chat", endpoint: EndpointKind::ChatCompletions', out)
        self.assertIn('id: "m-msg", endpoint: EndpointKind::Messages', out)
        self.assertIn('id: "m-resp", endpoint: EndpointKind::Responses', out)

    def test_unsupported_endpoint_excluded_with_reason(self):
        live = make_live(["gemini-4-argon-contributor", "glm-5.2"])
        docs = make_docs([("Gemini", "gemini-4-argon-contributor", GOOGLE), ("GLM", "glm-5.2", CHAT)])
        opts = self.options(live, docs, make_roster({}), "exclude = []\n")
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertNotIn("gemini-4-argon-contributor", report["routable"])
        excl = {e["id"]: e["reason"] for e in report["exclusions"]}
        self.assertIn("gemini-4-argon-contributor", excl)
        self.assertIn("unsupported endpoint", excl["gemini-4-argon-contributor"])
        self.assertIn("/models/gemini-4-argon-contributor", excl["gemini-4-argon-contributor"])
        self.assertNotIn("gemini-4-argon-contributor", self.read(opts.overlay))

    def test_undocumented_live_model_excluded(self):
        live = make_live(["glm-5.2", "omen-alpha"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        opts = self.options(live, docs, make_roster({}), "exclude = []\n")
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        excl = {e["id"]: e["reason"] for e in report["exclusions"]}
        self.assertEqual(excl["omen-alpha"], "no documented endpoint")


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #


class OverrideTests(TempCase):
    def test_pin_forces_inclusion_when_in_neither_source(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        overrides = (
            "exclude = []\n"
            "[[pins]]\n"
            'id = "brand-new-model"\n'
            'protocol = "Responses"\n'
            'reason = "fast-tracked before docs"\n'
        )
        opts = self.options(live, docs, make_roster({}), overrides)
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertIn("brand-new-model", report["routable"])
        self.assertEqual(report["pins_applied"], [
            {"id": "brand-new-model", "protocol": "Responses", "reason": "fast-tracked before docs"}
        ])
        self.assertIn('id: "brand-new-model", endpoint: EndpointKind::Responses', self.read(opts.overlay))

    def test_pin_protocol_wins_over_scraped(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        overrides = 'exclude = []\n[[pins]]\nid = "glm-5.2"\nprotocol = "Messages"\n'
        opts = self.options(live, docs, make_roster({}), overrides)
        self.run_sync(opts)
        out = self.read(opts.overlay)
        self.assertIn('id: "glm-5.2", endpoint: EndpointKind::Messages', out)
        self.assertNotIn("ChatCompletions", out)

    def test_exclude_suppresses_a_live_model(self):
        live = make_live(["glm-5.2", "omen-alpha"])
        docs = make_docs([("GLM", "glm-5.2", CHAT), ("Omen", "omen-alpha", CHAT)])
        overrides = 'exclude = ["omen-alpha"]\n'
        opts = self.options(live, docs, make_roster({}), overrides)
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertNotIn("omen-alpha", report["routable"])
        excl = {e["id"]: e["reason"] for e in report["exclusions"]}
        self.assertEqual(excl["omen-alpha"], "excluded by overrides")
        self.assertNotIn('id: "omen-alpha"', self.read(opts.overlay))

    def test_exclude_beats_pin(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        overrides = (
            'exclude = ["glm-5.2"]\n[[pins]]\nid = "glm-5.2"\nprotocol = "Responses"\n'
        )
        opts = self.options(live, docs, make_roster({}), overrides)
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertNotIn("glm-5.2", report["routable"])
        self.assertEqual(report["pins_applied"], [])

    def test_pin_survives_mirroring(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        overrides = 'exclude = []\n[[pins]]\nid = "kept-alive"\nprotocol = "ChatCompletions"\n'
        opts = self.options(live, docs, make_roster({}), overrides)
        self.run_sync(opts)
        self.assertIn('id: "kept-alive"', self.read(opts.overlay))
        # Mirror-changed live list without the pin still keeps it.
        live2 = make_live(["glm-5.2", "new-model"])
        docs2 = make_docs([("GLM", "glm-5.2", CHAT), ("New", "new-model", CHAT)])
        self.write("live.json", live2)
        self.write("docs.md", docs2)
        self.run_sync(opts)
        out = self.read(opts.overlay)
        self.assertIn('id: "kept-alive"', out)
        self.assertIn('id: "new-model"', out)

    def test_invalid_pin_protocol_fails(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        overrides = 'exclude = []\n[[pins]]\nid = "x"\nprotocol = "Grpc"\n'
        opts = self.options(live, docs, make_roster({}), overrides)
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)

    def test_invalid_overrides_toml_fails(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        opts = self.options(live, docs, make_roster({}), "this is = not = toml\n")
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)


# --------------------------------------------------------------------------- #
# Advertisement forms
# --------------------------------------------------------------------------- #


class AdvertisementTests(TempCase):
    def test_qualified_only_when_owned_elsewhere(self):
        live = make_live(["gpt-5.6-luna", "glm-5.2"])
        docs = make_docs([("GPT", "gpt-5.6-luna", RESP), ("GLM", "glm-5.2", CHAT)])
        roster = make_roster({"codex": ["gpt-5.6-luna", "gpt-6-luna"]})
        opts = self.options(live, docs, roster, "exclude = []\n")
        self.run_sync(opts)
        out = self.read(opts.overlay)
        self.assertIn('id: "gpt-5.6-luna"', out)
        self.assertIn("advert: Advertisement::QualifiedOnly", out)
        self.assertIn("advert: Advertisement::BareAndQualified", out)
        report = json.loads(self.read(opts.report))
        self.assertEqual(report["advertisement"]["qualified_only"], ["gpt-5.6-luna"])
        self.assertEqual(report["advertisement"]["bare_and_qualified"], ["glm-5.2"])

    def test_roster_opencode_group_is_ignored(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        roster = "opencode: glm-5.2, opencode-go/glm-5.2\ncodex: gpt-5.6-luna\n"
        opts = self.options(live, docs, roster, "exclude = []\n")
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertEqual(report["advertisement"]["bare_and_qualified"], ["glm-5.2"])
        self.assertEqual(report["advertisement"]["qualified_only"], [])

    def test_qualified_forms_are_not_bare_roster_ids(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        # A codex group carrying an opencode-go/ prefixed token must not make
        # glm-5.2 qualified-only.
        roster = "codex: opencode-go/glm-5.2, gpt-5.6-luna\n"
        opts = self.options(live, docs, roster, "exclude = []\n")
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertEqual(report["advertisement"]["bare_and_qualified"], ["glm-5.2"])


# --------------------------------------------------------------------------- #
# Determinism / emitter
# --------------------------------------------------------------------------- #


class EmitterTests(TempCase):
    def test_exact_template(self):
        live = make_live(["a", "b"])
        docs = make_docs([("A", "a", CHAT), ("B", "b", MSG)])
        roster = make_roster({"codex": ["b"]})
        opts = self.options(live, docs, roster, "exclude = []\n")
        self.run_sync(opts)
        expected = (
            "// @generated by the OpenCode Go catalog sync — do not edit by hand.\n"
            "//\n"
            "// Synced 2 models. Regenerate via `claude-code-proxy serve` or the kit sync script.\n"
            "\n"
            "use crate::providers::opencode::model::EndpointKind;\n"
            "use crate::providers::opencode::overlay::{Advertisement, SyncedEntry};\n"
            "\n"
            "pub const SYNCED: bool = true;\n"
            "\n"
            "#[rustfmt::skip]\n"
            "pub const ENTRIES: &[SyncedEntry] = &[\n"
            '    SyncedEntry { id: "a", endpoint: EndpointKind::ChatCompletions, advert: Advertisement::BareAndQualified },\n'
            '    SyncedEntry { id: "b", endpoint: EndpointKind::Messages, advert: Advertisement::QualifiedOnly },\n'
            "];\n"
        )
        self.assertEqual(self.read(opts.overlay), expected)

    def test_sorted_by_id_byte_order(self):
        live = make_live(["zeta", "Alpha", "beta", "glm-5.2", "GLM-5.3"])
        names = ["z", "A", "b", "g", "G"]
        docs = make_docs(list(zip(names, ["zeta", "Alpha", "beta", "glm-5.2", "GLM-5.3"], [CHAT] * 5)))
        opts = self.options(live, docs, make_roster({}), "exclude = []\n")
        self.run_sync(opts)
        out = self.read(opts.overlay)
        order = [line.split('id: "', 1)[1].split('"', 1)[0] for line in out.splitlines() if 'id: "' in line]
        self.assertEqual(order, sorted(order))

    def test_deterministic_identical_bytes(self):
        live = make_live(["glm-5.2", "kimi-k3", "gpt-5.6-luna"])
        docs = make_docs([("GLM", "glm-5.2", CHAT), ("Kimi", "kimi-k3", CHAT), ("GPT", "gpt-5.6-luna", RESP)])
        roster = make_roster({"kimi": ["kimi-k3"], "codex": ["gpt-5.6-luna"]})
        opts_a = self.options(live, docs, roster, "exclude = []\n", overlay_name="a.rs")
        opts_b = self.options(live, docs, roster, "exclude = []\n", overlay_name="b.rs")
        self.run_sync(opts_a)
        self.run_sync(opts_b)
        self.assertEqual(self.read_bytes(opts_a.overlay), self.read_bytes(opts_b.overlay))

    def test_trailing_newline_and_lf(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        opts = self.options(live, docs, make_roster({}), "exclude = []\n")
        self.run_sync(opts)
        data = self.read_bytes(opts.overlay)
        self.assertTrue(data.endswith(b"\n"))
        self.assertNotIn(b"\r", data)


# --------------------------------------------------------------------------- #
# Outcome / idempotency / report
# --------------------------------------------------------------------------- #


class OutcomeTests(TempCase):
    def test_first_run_updated_then_no_change(self):
        live = make_live(["glm-5.2", "kimi-k3"])
        docs = make_docs([("GLM", "glm-5.2", CHAT), ("Kimi", "kimi-k3", CHAT)])
        opts = self.options(live, docs, make_roster({}), "exclude = []\n")

        self.assertEqual(self.run_sync(opts), sync.EXIT_UPDATED)
        first = self.read_bytes(opts.overlay)
        self.assertEqual(json.loads(self.read(opts.report))["outcome"], "updated")

        self.assertEqual(self.run_sync(opts), sync.EXIT_NO_CHANGE)
        self.assertEqual(self.read_bytes(opts.overlay), first)
        self.assertEqual(json.loads(self.read(opts.report))["outcome"], "no-change")

    def test_report_contents(self):
        live = make_live(["glm-5.2", "gemini-4-argon-contributor", "omen-alpha"])
        docs = make_docs([
            ("GLM", "glm-5.2", CHAT),
            ("Gemini", "gemini-4-argon-contributor", GOOGLE),
        ])
        overrides = 'exclude = []\n[[pins]]\nid = "pinned-x"\nprotocol = "Messages"\n'
        opts = self.options(live, docs, make_roster({"kimi": ["kimi-k3"]}), overrides)
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        for key in (
            "timestamp", "outcome", "exit_code", "live_ids", "routable",
            "exclusions", "pins_applied", "added", "removed", "sources",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["live_ids"], ["gemini-4-argon-contributor", "glm-5.2", "omen-alpha"])
        self.assertEqual(report["routable"], ["glm-5.2", "pinned-x"])
        self.assertEqual(report["exit_code"], sync.EXIT_UPDATED)
        self.assertEqual(report["outcome"], "updated")
        self.assertTrue(report["exclusions"])
        self.assertEqual(report["pins_applied"][0]["id"], "pinned-x")
        self.assertIn("overlay_out", report["sources"])

    def test_added_and_removed_vs_previous(self):
        # Previous generated content holds "old-model"; live list dropped it and
        # gained "glm-5.2".
        previous = (
            'pub const ENTRIES: &[SyncedEntry] = &[\n'
            '    SyncedEntry { id: "old-model", endpoint: EndpointKind::ChatCompletions, advert: Advertisement::BareAndQualified },\n'
            "];\n"
        )
        prev_path = self.write("prev.rs", previous)
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        opts = self.options(live, docs, make_roster({}), "exclude = []\n", previous=prev_path)
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertEqual(report["added"], ["glm-5.2"])
        self.assertEqual(report["removed"], ["old-model"])

    def test_empty_routable_set_still_emits_synced_module(self):
        live = make_live(["gemini-4-argon-contributor"])
        docs = make_docs([("Gemini", "gemini-4-argon-contributor", GOOGLE)])
        opts = self.options(live, docs, make_roster({}), "exclude = []\n")
        self.assertEqual(self.run_sync(opts), sync.EXIT_UPDATED)
        out = self.read(opts.overlay)
        self.assertIn("pub const SYNCED: bool = true;", out)
        self.assertIn("// Synced 0 models.", out)


# --------------------------------------------------------------------------- #
# Docs drift
# --------------------------------------------------------------------------- #


class DriftTests(TempCase):
    def _drift(self, docs_text: str, reason_fragment: str):
        live = make_live(["glm-5.2"])
        opts = self.options(live, docs_text, make_roster({}), "exclude = []\n")
        sentinel = b"// sentinel: do not touch\n"
        os.makedirs(os.path.dirname(opts.overlay), exist_ok=True)
        with open(opts.overlay, "wb") as handle:
            handle.write(sentinel)
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)
        self.assertEqual(self.read_bytes(opts.overlay), sentinel)
        report = json.loads(self.read(opts.report))
        self.assertEqual(report["outcome"], "failed")
        self.assertIn(reason_fragment, report["reason"])
        # A failure before any change (docs drift) carries no install block.
        self.assertNotIn("install", report)

    def test_missing_table(self):
        self._drift("# Go\n\nNo table here.\n", "not found")

    def test_missing_required_column(self):
        docs = make_docs(
            [("GLM", "glm-5.2", CHAT)],
            header="| Model | Identifier | Endpoint |\n| ----- | ---------- | -------- |\n",
        )
        self._drift(docs, "not found")

    def test_row_too_few_columns(self):
        docs = make_docs([("GLM", "glm-5.2", CHAT)]) + "| Broken | row |\n"
        self._drift(docs, "too few columns")

    def test_no_data_rows(self):
        docs = "# Go\n\n" + DOCS_HEADER
        self._drift(docs, "no data rows")

    def test_cli_drift_exit_code_and_report(self):
        docs = "# Go\n\nnothing.\n"
        live_p = self.write("live.json", make_live(["glm-5.2"]))
        roster_p = self.write("roster.txt", make_roster({}))
        ovr_p = self.write("overrides.toml", "exclude = []\n")
        overlay_p = self.path("out.rs")
        report_p = self.path("rep.json")
        docs_p = self.write("docs.md", docs)
        code = self.run_cli([
            "--live", live_p, "--docs", docs_p, "--roster", roster_p,
            "--overrides", ovr_p, "--overlay", overlay_p, "--report", report_p,
        ])
        self.assertEqual(code, sync.EXIT_FAILED)
        self.assertFalse(os.path.exists(overlay_p))
        self.assertEqual(json.loads(self.read(report_p))["outcome"], "failed")


# --------------------------------------------------------------------------- #
# Docs table parsing (blank-line tolerance)
# --------------------------------------------------------------------------- #


class DocsTableTests(TempCase):
    def test_blank_line_mid_table_still_parses_all_rows(self):
        docs = (
            "# Go\n\n"
            + DOCS_HEADER
            + f"| GLM | glm-5.2 | `{CHAT}` | `pkg` |\n"
            + "\n"
            + f"| Kimi | kimi-k3 | `{CHAT}` | `pkg` |\n"
        )
        parsed = sync.parse_docs_table(docs)
        self.assertEqual(set(parsed), {"glm-5.2", "kimi-k3"})
        self.assertEqual(parsed["kimi-k3"].protocol, "ChatCompletions")

    def test_blank_line_after_table_still_terminates_scan(self):
        # A blank line with no following `|`-row ends the table: trailing prose
        # is never misread as rows (drift detection stays intact).
        docs = make_docs([("GLM", "glm-5.2", CHAT)]) + "\nSome trailing prose.\n"
        self.assertEqual(set(sync.parse_docs_table(docs)), {"glm-5.2"})

    def test_blank_line_mid_table_matches_contiguous_overlay(self):
        live = make_live(["glm-5.2", "kimi-k3"])
        contiguous = make_docs([("GLM", "glm-5.2", CHAT), ("Kimi", "kimi-k3", CHAT)])
        blank = contiguous.replace("| Kimi |", "\n| Kimi |")
        self.assertEqual(sync.parse_docs_table(contiguous), sync.parse_docs_table(blank))
        overlays = []
        for name, docs in (("a", contiguous), ("b", blank)):
            opts = sync.Options(
                live=self.write(f"live-{name}.json", live),
                docs=self.write(f"docs-{name}.md", docs),
                roster=self.write(f"roster-{name}.txt", make_roster({})),
                overrides=self.write(f"overrides-{name}.toml", "exclude = []\n"),
                previous=self.path(f"overlay-{name}.rs"),
                overlay=self.path(f"overlay-{name}.rs"),
                report=self.path(f"report-{name}.json"),
            )
            self.assertEqual(self.run_sync(opts), sync.EXIT_UPDATED)
            overlays.append(self.read_bytes(opts.overlay))
        self.assertEqual(overlays[0], overlays[1])


# --------------------------------------------------------------------------- #
# Input failures
# --------------------------------------------------------------------------- #


class InputFailureTests(TempCase):
    def test_invalid_live_json_fails(self):
        opts = self.options("{not json", make_docs([("A", "a", CHAT)]), make_roster({}), "exclude = []\n")
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)

    def test_empty_live_list_fails(self):
        opts = self.options('{"object":"list","data":[]}', make_docs([("A", "a", CHAT)]), make_roster({}), "exclude = []\n")
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)

    def test_empty_roster_fails(self):
        opts = self.options(make_live(["a"]), make_docs([("A", "a", CHAT)]), "", "exclude = []\n")
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)

    def test_missing_roster_file_fails(self):
        live = make_live(["a"])
        opts = self.options(live, make_docs([("A", "a", CHAT)]), make_roster({"codex": ["x"]}), "exclude = []\n")
        os.unlink(opts.roster)
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)

    def test_missing_live_path_fails(self):
        opts = self.options(make_live(["a"]), make_docs([("A", "a", CHAT)]), make_roster({}), "exclude = []\n")
        opts.live = None
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)


# --------------------------------------------------------------------------- #
# Output-write failures (success path)
# --------------------------------------------------------------------------- #


class WriteFailureTests(TempCase):
    """A failing success-path write is clean, never a traceback.

    A failed overlay write is a failure (exit 1, output untouched); a failed
    report write after a successful overlay write is still "updated" (exit 2).
    """

    def _base_options(self) -> sync.Options:
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        live_p = self.write("live.json", live)
        docs_p = self.write("docs.md", docs)
        roster_p = self.write("roster.txt", make_roster({}))
        ovr_p = self.write("overrides.toml", "exclude = []\n")
        overlay_p = self.path("overlay_generated.rs")
        return sync.Options(
            live=live_p,
            docs=docs_p,
            roster=roster_p,
            overrides=ovr_p,
            previous=overlay_p,
            overlay=overlay_p,
            report=self.path("report.json"),
        )

    def test_report_write_oserror_still_reports_updated(self):
        # The report path's parent is a regular file -> ENOTDIR on write. The
        # overlay itself is writable, so the run is still "updated": the serve
        # hook (ticket 05) must see exit 2 and proceed to gate/build/install
        # rather than silently deferring the build.
        blocker = self.write("blocker", "not a directory\n")
        opts = self._base_options()
        opts.report = os.path.join(blocker, "report.json")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = sync.execute(opts)
        self.assertEqual(code, sync.EXIT_UPDATED)
        self.assertIn("glm-5.2", self.read(opts.overlay))
        self.assertIn("cannot write report", err.getvalue())

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "permission-based scenario is meaningless as root",
    )
    def test_overlay_write_oserror_is_clean_failure(self):
        # The overlay's file is absent (so the pre-read returns None) but its
        # directory is unwritable -> EACCES on the atomic write.
        ro = self.path("readonly")
        os.makedirs(ro)
        self.addCleanup(os.chmod, ro, 0o700)
        os.chmod(ro, 0o500)
        opts = self._base_options()
        opts.previous = os.path.join(ro, "overlay_generated.rs")
        opts.overlay = os.path.join(ro, "overlay_generated.rs")
        self.assertEqual(self.run_sync(opts), sync.EXIT_FAILED)
        # The report path is writable, so a failed report must still land.
        report = json.loads(self.read(opts.report))
        self.assertEqual(report["outcome"], "failed")
        self.assertIn("cannot write overlay", report["reason"])


# --------------------------------------------------------------------------- #
# Real seed fixtures
# --------------------------------------------------------------------------- #


class SeedFixtureTests(TempCase):
    """End-to-end against the captured live list + docs and the roster fixture."""

    def _opts(self) -> sync.Options:
        return sync.Options(
            live=os.path.join(KIT, "fixtures", "live-list.json"),
            docs=os.path.join(KIT, "fixtures", "docs.md"),
            roster=os.path.join(KIT, "fixtures", "roster.txt"),
            overrides=self.write("overrides.toml", "exclude = []\n"),
            previous=self.path("overlay_generated.rs"),
            overlay=self.path("overlay_generated.rs"),
            report=self.path("report.json"),
        )

    def test_seed_run(self):
        opts = self._opts()
        self.assertEqual(self.run_sync(opts), sync.EXIT_UPDATED)
        report = json.loads(self.read(opts.report))
        self.assertEqual(len(report["live_ids"]), 37)
        # gemini is documented but Google-native -> excluded with a reason.
        excl = {e["id"]: e["reason"] for e in report["exclusions"]}
        self.assertIn("gemini-4-argon-contributor", excl)
        self.assertIn("unsupported endpoint", excl["gemini-4-argon-contributor"])
        # undocumented live ids -> excluded
        for mid in ("glm-5.1", "omen-alpha"):
            self.assertEqual(excl.get(mid), "no documented endpoint")
        # Codex/Kimi/Grok-owned ids are qualified-only
        for mid in ("gpt-5.6-luna", "kimi-k3", "grok-4.7"):
            self.assertIn(mid, report["advertisement"]["qualified_only"])
        self.assertIn("glm-5.2", report["advertisement"]["bare_and_qualified"])
        # idempotent
        first = self.read_bytes(opts.overlay)
        self.assertEqual(self.run_sync(opts), sync.EXIT_NO_CHANGE)
        self.assertEqual(self.read_bytes(opts.overlay), first)

    def test_seed_pin_overrides_google_endpoint(self):
        overrides = (
            "exclude = []\n"
            "[[pins]]\n"
            'id = "gemini-4-argon-contributor"\n'
            'protocol = "Responses"\n'
        )
        opts = self._opts()
        self.write("overrides.toml", overrides)
        self.run_sync(opts)
        report = json.loads(self.read(opts.report))
        self.assertIn("gemini-4-argon-contributor", report["routable"])
        self.assertIn(
            'id: "gemini-4-argon-contributor", endpoint: EndpointKind::Responses',
            self.read(opts.overlay),
        )


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #


class CliTests(TempCase):
    def test_usage_error_exits_one_not_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                sync.build_parser().parse_args(["--nope"])
        self.assertEqual(raised.exception.code, sync.EXIT_FAILED)

    def test_dry_run_writes_nothing(self):
        live = make_live(["glm-5.2"])
        docs = make_docs([("GLM", "glm-5.2", CHAT)])
        opts = self.options(live, docs, make_roster({}), "exclude = []\n")
        opts.dry_run = True
        self.assertEqual(self.run_sync(opts), sync.EXIT_UPDATED)
        self.assertFalse(os.path.exists(opts.overlay))
        self.assertFalse(os.path.exists(opts.report))


if __name__ == "__main__":
    unittest.main()
