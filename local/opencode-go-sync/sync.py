#!/usr/bin/env python3
"""OpenCode Go catalog sync — generator core + fetch layer (tickets 02/03).

Pure, offline, fixture-driven core of the serve-time catalog sync. Given a
snapshot of the live OpenCode Go model list, a snapshot of the official
endpoint-documentation markdown, a roster snapshot of other providers' bare
model IDs, the hand-maintained overrides master and the previously generated
overlay content, it produces:

  * the generated Rust overlay module (per contracts/overlay-module.md),
  * the machine-readable sync report (JSON), and
  * an outcome: no-change / updated / failed.

Ticket 03 adds a thin fetch layer around that pure core: when no local file is
given for an input it fetches the live list and the docs markdown (content
negotiated with ``Accept: text/markdown``) and extracts the other-provider
roster at run time by running the installed binary's ``models`` command
(falling back to parsing the working-copy sources). The core parsers and the
emitter remain pure functions; every fetch/extraction failure becomes a visible
``SyncFailure`` with the previous overlay left untouched.

Ticket 04 extends a *changed* run from generation to gated installation: run the
formatting check, the sync's own fixture suite (this file's ``--self-test``) and
the focused catalog/routing tests, then a release build, and only on full green
install the built binary atomically over the install target (backing up any
pre-existing target once). On any post-write failure (gate/build/install) the
previous overlay bytes are restored, so no partial state remains and the next
run retries. No-change runs skip tests, build and install entirely. The gate,
build and install commands and every path are injectable (flags/env or the
``Options`` struct) so tests simulate pass/fail without real cargo.

Ticket 05 (serve pre-flight) is not implemented here.

See --help for the overrides schema, environment variables and exit codes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

# urlparse is used only to isolate the path component of a documented endpoint
# URL (pure string parsing of a docs-table cell). The fetch layer below is the
# only code here that opens a socket.
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KIT_DEFAULT = os.path.expanduser("~/.local/share/claude-code-proxy-sync")
REPO_DEFAULT = os.path.expanduser("~/repos/claude-code-proxy")
REPORT_DEFAULT = os.path.expanduser("~/.local/state/claude-code-proxy/sync-report.json")
OVERLAY_REL = os.path.join("src", "providers", "opencode", "overlay_generated.rs")

# Live sources (ticket 03). Both endpoints are keyless; no credentials are read
# or logged. The docs endpoint serves HTML by default and the endpoint table
# only in the content-negotiated markdown variant.
LIVE_URL_DEFAULT = "https://opencode.ai/zen/go/v1/models"
DOCS_URL_DEFAULT = "https://opencode.ai/v2/docs/console/go/"
ROSTER_CMD_DEFAULT = os.path.expanduser("~/.local/bin/claude-code-proxy") + " models"
HTTP_TIMEOUT_DEFAULT = 15.0

# The endpoints sit behind Cloudflare, which rejects the default Python-urllib
# User-Agent (403, error 1010). An explicit identifying UA is required; it is
# not a secret and carries no credentials.
USER_AGENT = "claude-code-proxy-sync/0.1 (local catalog sync)"

# Exit codes (machine-readable; documented in --help and written to the report).
EXIT_NO_CHANGE = 0
EXIT_FAILED = 1
EXIT_UPDATED = 2
# A concurrent update (a live-PID update-state marker) is deliberately mapped to
# EXIT_FAILED: the serve hook already treats 1 as "warn once and serve the
# existing binary", which is exactly right when another run holds the update.
EXIT_BUSY = EXIT_FAILED

# Update-state marker (doubles as a single-machine PID lock). It lives in the
# report directory, alongside the report path override. It is claimed *before*
# the overlay is written and removed only after a successful install (or a
# handled failure's revert). A SIGKILL/OOM/power-loss between the overlay write
# and the install therefore leaves the marker behind: the next run sees a dead
# PID and forces the update even though the overlay already matches, so a stale
# binary can never be served indefinitely. If the marker cannot be written the
# run degrades gracefully (it proceeds without the marker).
UPDATE_STATE_NAME = "update.state"

# Ticket 04 gated install. The install target is the binary the serve hook
# re-execs into; a pre-existing target is backed up once (never overwritten).
INSTALL_TARGET_DEFAULT = os.path.expanduser("~/.local/bin/claude-code-proxy")
BACKUP_SUFFIX = ".release-bak"
# Build stamp: the sha256 of the overlay the installed binary was built from.
# A missing/mismatched stamp forces one gated rebuild even when the overlay
# bytes are unchanged (e.g. after --no-install, a manual edit, or a rollback).
INSTALL_STAMP_SUFFIX = ".catalog-sha256"
BUILT_BINARY_REL = os.path.join("target", "release", "claude-code-proxy")
# Stages that can only be reached after a change was detected and the overlay
# written; a failure here aborts an in-flight update (ticket 04).
POST_CHANGE_STAGES = ("gate", "build", "install")
# Generous ceiling: a cold release build on the owner's machine is minutes, not
# seconds. Only a hung process hits it; every gate/build/install command uses it.
COMMAND_TIMEOUT_DEFAULT = 1800.0

PROTOCOLS = ("ChatCompletions", "Messages", "Responses")

# Header line of the generated module (em dash is intentional, per contract).
GENERATED_HEADER = "// @generated by the OpenCode Go catalog sync — do not edit by hand."

# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #


class SyncFailure(Exception):
    """A visible, expected failure (drift, parse error, missing input)."""

    def __init__(self, reason: str, stage: str = "sync") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stage = stage


@dataclass(frozen=True)
class Scraped:
    """Docs-table scrape result for one model ID."""

    endpoint_path: str
    protocol: str | None  # None => unsupported endpoint shape


@dataclass(frozen=True)
class Pin:
    id: str
    protocol: str
    reason: str | None = None


@dataclass(frozen=True)
class Overrides:
    pins: dict[str, Pin] = field(default_factory=dict)
    exclude: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Entry:
    id: str
    protocol: str
    advert: str  # "BareAndQualified" | "QualifiedOnly"
    source: str  # "docs" | "pin"


@dataclass
class Plan:
    live_ids: list[str]
    scraped: dict[str, Scraped]
    roster: set[str]
    overrides: Overrides
    entries: list[Entry]
    exclusions: list[tuple[str, str]]  # (id, reason), sorted by id
    pins_applied: list[Pin]
    qualified_only: list[str]
    bare_and_qualified: list[str]

    @property
    def routed(self) -> list[str]:
        return [e.id for e in self.entries]


# --------------------------------------------------------------------------- #
# Parsers (pure functions over text)
# --------------------------------------------------------------------------- #


def parse_live_list(text: str) -> list[str]:
    """Parse an OpenAI-style model list snapshot into model IDs."""
    try:
        doc = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - surfaced as a visible failure
        raise SyncFailure(f"live list is not valid JSON: {exc}", "live-list") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise SyncFailure("live list is missing a 'data' array", "live-list")
    ids: list[str] = []
    for item in doc["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise SyncFailure("live list entry is missing a non-empty string 'id'", "live-list")
        ids.append(item["id"])
    if not ids:
        raise SyncFailure("live list contains no models", "live-list")
    if len(set(ids)) != len(ids):
        raise SyncFailure("live list contains duplicate ids", "live-list")
    return sorted(ids)


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator(line: str) -> bool:
    cells = _split_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def classify_endpoint(endpoint: str) -> Scraped:
    """Map an endpoint cell to a wire protocol; other shapes are unsupported."""
    raw = endpoint.strip().strip("`").strip()
    path = urlparse(raw).path if "://" in raw else raw
    if path.endswith("/chat/completions"):
        return Scraped(path, "ChatCompletions")
    if path.endswith("/messages"):
        return Scraped(path, "Messages")
    if path.endswith("/responses"):
        return Scraped(path, "Responses")
    return Scraped(path, None)


def parse_docs_table(text: str) -> dict[str, Scraped]:
    """Parse the docs endpoint table (model ID -> endpoint/protocol).

    Documentation drift — a missing endpoint table, missing required columns,
    a malformed table row, or no data rows — raises SyncFailure so the caller
    can fail visibly and leave output unchanged.
    """
    lines = text.splitlines()
    header_idx = None
    columns: list[str] = []
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        cells = [c.lower() for c in _split_row(line)]
        if "model id" in cells and "endpoint" in cells:
            header_idx = i
            columns = cells
            break
    if header_idx is None:
        raise SyncFailure(
            "docs drift: endpoint table with 'Model ID' and 'Endpoint' columns not found",
            "docs",
        )
    id_col = columns.index("model id")
    ep_col = columns.index("endpoint")
    sep = header_idx + 1
    if sep >= len(lines) or not _is_separator(lines[sep]):
        raise SyncFailure("docs drift: endpoint table separator row missing", "docs")

    scraped: dict[str, Scraped] = {}
    rows = 0
    j = sep + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            # Tolerate a blank line inside the table region, but only when a
            # later `|`-row follows: a blank line that separates the table from
            # the rest of the document still terminates the scan (trailing prose
            # is never misread as rows, and drift detection is not weakened).
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines) and lines[k].strip().startswith("|"):
                j = k
                continue
            break
        if not line.strip().startswith("|"):
            break
        if _is_separator(line):
            break
        cells = _split_row(line)
        if len(cells) <= max(id_col, ep_col):
            raise SyncFailure(
                f"docs drift: endpoint table row {j + 1} has too few columns", "docs"
            )
        model_id = cells[id_col]
        endpoint = cells[ep_col]
        if not model_id or not endpoint:
            raise SyncFailure(
                f"docs drift: endpoint table row {j + 1} has an empty Model ID or Endpoint",
                "docs",
            )
        scraped[model_id] = classify_endpoint(endpoint)
        rows += 1
        j += 1
    if rows == 0:
        raise SyncFailure("docs drift: endpoint table has no data rows", "docs")
    return scraped


def _is_bare_roster_id(mid: str) -> bool:
    """True for a bare model ID; provider-prefixed forms are not bare.

    ``opencode-go/x`` (slash) and ``cursor:composer-2.5`` (colon) are qualified
    forms, not the bare IDs another provider owns.
    """
    return bool(mid) and "/" not in mid and ":" not in mid


def parse_roster(text: str) -> set[str]:
    """Parse a roster snapshot of other providers' bare model IDs.

    Accepts either the `models` CLI text form (``provider: id, id, ...`` per
    line) or a JSON object mapping provider -> list of ids. The ``opencode``
    group is ignored entirely (those are the synced IDs themselves), the
    trailing ``;``-note on the ``cursor:`` line is dropped, and provider-
    prefixed forms (``/`` or ``:`` qualified) are not bare IDs.
    """
    text = text.strip()
    if not text:
        raise SyncFailure("roster snapshot is empty", "roster")

    roster: set[str] = set()
    if text.startswith("{"):
        try:
            doc = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise SyncFailure(f"roster snapshot is not valid JSON: {exc}", "roster") from exc
        if not isinstance(doc, dict):
            raise SyncFailure("roster JSON must be an object of provider -> id list", "roster")
        groups = 0
        for provider, ids in doc.items():
            groups += 1
            if provider == "opencode":
                continue
            if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
                raise SyncFailure(f"roster group {provider!r} must be a list of strings", "roster")
            for mid in ids:
                if _is_bare_roster_id(mid):
                    roster.add(mid)
        if groups == 0:
            raise SyncFailure("roster snapshot has no provider groups", "roster")
        return roster

    groups = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            raise SyncFailure(
                f"roster line {lineno} is not in 'provider: id, id' form", "roster"
            )
        groups += 1
        provider, rest = match.group(1), match.group(2)
        if provider == "opencode":
            continue
        # Drop the trailing informational note that follows a ';'.
        rest = rest.split(";", 1)[0]
        for token in rest.split(","):
            mid = token.strip()
            if _is_bare_roster_id(mid):
                roster.add(mid)
    if groups == 0:
        raise SyncFailure("roster snapshot has no provider groups", "roster")
    return roster


def parse_overrides(text: str) -> Overrides:
    """Parse the hand-maintained overrides TOML master."""
    try:
        doc = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise SyncFailure(f"overrides file is not valid TOML: {exc}", "overrides") from exc

    pins: dict[str, Pin] = {}
    raw_pins = doc.get("pins", [])
    if not isinstance(raw_pins, list):
        raise SyncFailure("overrides: 'pins' must be an array of tables ([[pins]])", "overrides")
    for item in raw_pins:
        if not isinstance(item, dict):
            raise SyncFailure("overrides: each pin must be a table", "overrides")
        pid = item.get("id")
        protocol = item.get("protocol")
        if not isinstance(pid, str) or not pid:
            raise SyncFailure("overrides: a pin is missing a non-empty string 'id'", "overrides")
        if protocol not in PROTOCOLS:
            raise SyncFailure(
                f"overrides: pin {pid!r} has invalid protocol {protocol!r} "
                f"(expected one of {', '.join(PROTOCOLS)})",
                "overrides",
            )
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise SyncFailure(f"overrides: pin {pid!r} reason must be a string", "overrides")
        pins[pid] = Pin(pid, protocol, reason)

    exclude = doc.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(x, str) and x for x in exclude):
        raise SyncFailure("overrides: 'exclude' must be an array of non-empty strings", "overrides")
    return Overrides(pins=pins, exclude=set(exclude))


def parse_previous_ids(text: str | None) -> set[str]:
    """Extract the entry ids previously emitted in a generated overlay."""
    if text is None:
        return set()
    return set(re.findall(r'SyncedEntry\s*\{\s*id:\s*"([^"]+)"', text))


# --------------------------------------------------------------------------- #
# Fetch layer (ticket 03) — thin wrappers, no core logic
# --------------------------------------------------------------------------- #


def http_get_text(
    url: str,
    timeout: float,
    stage: str,
    what: str,
    headers: dict[str, str] | None = None,
) -> str:
    """GET ``url`` and return the decoded body, mapping every error to SyncFailure.

    HTTP status errors, DNS/connection failures, TLS problems and timeouts all
    become a single visible ``SyncFailure`` (one stderr line, exit 1) so a
    flaky network can never produce a traceback or a partial write.
    """
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    # Cloudflare blocks the default urllib UA; always identify ourselves.
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SyncFailure(
            f"{what} fetch failed: HTTP {exc.code} {exc.reason} from {url}", stage
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise SyncFailure(f"{what} fetch failed: {reason} ({url})", stage) from exc
    except (TimeoutError, OSError) as exc:
        raise SyncFailure(f"{what} fetch failed: {exc} ({url})", stage) from exc
    return raw.decode("utf-8", errors="replace")


def fetch_live_list(url: str, timeout: float) -> str:
    """Fetch the keyless OpenCode Go model list (OpenAI-style)."""
    return http_get_text(url, timeout, "live-list", "live list")


def _looks_like_html(text: str) -> bool:
    head = text.lstrip("\ufeff \t\r\n").lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def fetch_docs(url: str, timeout: float) -> str:
    """Fetch the docs markdown variant (Accept: text/markdown).

    A plain GET returns the HTML page; only the content-negotiated markdown
    variant carries the endpoint table. An HTML body is documentation drift and
    fails visibly rather than being parsed as (empty) markdown.
    """
    body = http_get_text(url, timeout, "docs", "docs", headers={"Accept": "text/markdown"})
    if _looks_like_html(body):
        raise SyncFailure(
            "docs drift: got an HTML page instead of the markdown variant "
            "(content negotiation with 'Accept: text/markdown' failed)",
            "docs",
        )
    return body


# Other-provider roster extraction. The preferred mechanism is the installed
# binary's `models` command, whose output lists every provider's fully expanded
# catalog; the fallback parses the working-copy sources. Both feed the output
# through parse_roster (opencode group skipped, bare ids only).

_STR_ARRAY_RE = re.compile(
    r"const\s+([A-Z][A-Z0-9_]*)\s*:\s*&\[&str\]\s*=\s*&\[(.*?)\];", re.DOTALL
)
_STR_LITERAL_RE = re.compile(r'"([^"]*)"')
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _rust_str_arrays(text: str) -> dict[str, list[str]]:
    arrays: dict[str, list[str]] = {}
    for match in _STR_ARRAY_RE.finditer(text):
        arrays[match.group(1)] = _STR_LITERAL_RE.findall(match.group(2))
    return arrays


def roster_text_from_sources(repo: str | None) -> str:
    """Fallback: build a roster text from the working-copy registry sources.

    Mirrors the binary's expansion for the non-opencode providers: the codex
    group is ``CODEX_MODELS`` plus its ``-fast``/``-ultrafast`` tier variants
    plus the Anthropic-style aliases, joined with the kimi/grok/cursor consts.
    Raises SyncFailure (never returns a silent empty roster) on any miss.
    """
    if not repo:
        raise SyncFailure("roster extraction: no working copy to fall back to", "roster")
    registry_path = os.path.join(repo, "src", "registry.rs")
    allowlist_path = os.path.join(
        repo, "src", "providers", "codex", "translate", "model_allowlist.rs"
    )
    registry_text = _read_text(registry_path, "roster", "working-copy registry sources")
    arrays = _rust_str_arrays(registry_text)
    codex = arrays.get("CODEX_MODELS")
    if not codex:
        raise SyncFailure(
            "roster extraction: CODEX_MODELS not found in working-copy sources", "roster"
        )

    ultrafast: list[str] = []
    if os.path.exists(allowlist_path):
        with open(allowlist_path, "r", encoding="utf-8") as handle:
            ultrafast = _rust_str_arrays(handle.read()).get("ULTRAFAST_MODELS", [])

    def expand(models: list[str]) -> list[str]:
        out: list[str] = []
        for model in models:
            out.append(model)
            out.append(f"{model}-fast")
            if model in ultrafast:
                out.append(f"{model}-ultrafast")
        return out

    codex_group = sorted(
        set(expand(codex)) | set(arrays.get("ANTHROPIC_STYLE_ALIASES", []))
    )
    groups = {
        "codex": codex_group,
        "kimi": arrays.get("KIMI_MODELS", []),
        "grok": arrays.get("GROK_MODELS", []),
        "cursor": arrays.get("CURSOR_LEGACY_MODELS", []),
    }
    lines = []
    for name, ids in groups.items():
        bare = sorted({i for i in ids if _BARE_ID_RE.match(i)})
        lines.append(f"{name}: {', '.join(bare)}")
    return "\n".join(lines) + "\n"


def extract_roster_text(opts: "Options") -> tuple[str, str]:
    """Return ``(roster_text, how)`` for the runtime extraction path.

    Preferred: run the installed binary's ``models`` command. Any spawn error,
    non-zero exit or empty stdout falls back to parsing the working-copy
    sources. Never returns nothing: the caller turns an empty parse into a
    visible failure.
    """
    if opts.roster_command:
        argv = shlex.split(opts.roster_command)
        failure: str | None = None
        try:
            proc = subprocess.run(  # noqa: S603 - configured local command
                argv,
                capture_output=True,
                text=True,
                timeout=opts.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failure = f"{type(exc).__name__}: {exc}"
        else:
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout, f"command {opts.roster_command!r}"
            if proc.returncode != 0:
                stderr = proc.stderr.strip().splitlines()
                detail = f"exit {proc.returncode}"
                if stderr:
                    detail += f": {stderr[-1]}"
                failure = detail
            else:
                failure = "command produced no output"
        try:
            text = roster_text_from_sources(opts.repo)
        except SyncFailure as exc:
            raise SyncFailure(
                f"roster extraction failed: command {opts.roster_command!r} failed "
                f"({failure}) and fallback failed: {exc.reason}",
                "roster",
            ) from exc
        return text, f"fallback working-copy sources ({failure})"
    return roster_text_from_sources(opts.repo), "working-copy sources"


def resolve_roster(opts: "Options") -> set[str]:
    """Resolve the other-provider roster: local file, else run-time extraction."""
    if opts.roster:
        return parse_roster(_read_text(opts.roster, "roster", "roster snapshot"))
    if not opts.fetch:
        raise SyncFailure("missing input: no path given for roster snapshot", "roster")
    text, how = extract_roster_text(opts)
    roster = parse_roster(text)
    if not roster:
        raise SyncFailure(
            f"roster extraction produced no other-provider ids (via {how}); "
            "refusing to treat an empty roster as success",
            "roster",
        )
    return roster


def _live_input(opts: "Options") -> str:
    if opts.live:
        return _read_text(opts.live, "live-list", "live-list snapshot")
    if not opts.fetch:
        raise SyncFailure("missing input: no path given for live-list snapshot", "live-list")
    return fetch_live_list(opts.live_url, opts.timeout)


def _docs_input(opts: "Options") -> str:
    if opts.docs:
        return _read_text(opts.docs, "docs", "docs snapshot")
    if not opts.fetch:
        raise SyncFailure("missing input: no path given for docs snapshot", "docs")
    return fetch_docs(opts.docs_url, opts.timeout)


# --------------------------------------------------------------------------- #
# Core computation
# --------------------------------------------------------------------------- #


def compute(
    live_ids: list[str],
    scraped: dict[str, Scraped],
    roster: set[str],
    overrides: Overrides,
) -> Plan:
    """Join live list + docs table + roster + overrides into a deterministic plan.

    Precedence: exclude > pin > scraped. Everything else is a strict mirror of
    the live list; pins force inclusion with their explicit protocol even when
    the id is in neither the live list nor the docs.
    """
    exclusions: list[tuple[str, str]] = []
    pins_applied: list[Pin] = []
    entries: list[Entry] = []

    all_ids = sorted(set(live_ids) | set(overrides.pins))

    for model_id in all_ids:
        if model_id in overrides.exclude:
            exclusions.append((model_id, "excluded by overrides"))
            continue
        if model_id in overrides.pins:
            pin = overrides.pins[model_id]
            entries.append(Entry(model_id, pin.protocol, _advert(model_id, roster), "pin"))
            pins_applied.append(pin)
            continue
        scrape = scraped.get(model_id)
        if scrape is None:
            exclusions.append((model_id, "no documented endpoint"))
            continue
        if scrape.protocol is None:
            exclusions.append((model_id, f"unsupported endpoint: {scrape.endpoint_path}"))
            continue
        entries.append(Entry(model_id, scrape.protocol, _advert(model_id, roster), "docs"))

    qual: list[str] = []
    bare: list[str] = []
    for entry in entries:
        if entry.advert == "QualifiedOnly":
            qual.append(entry.id)
        else:
            bare.append(entry.id)

    return Plan(
        live_ids=list(live_ids),
        scraped=scraped,
        roster=roster,
        overrides=overrides,
        entries=entries,
        exclusions=exclusions,
        pins_applied=sorted(pins_applied, key=lambda p: p.id),
        qualified_only=qual,
        bare_and_qualified=bare,
    )


def _advert(model_id: str, roster: set[str]) -> str:
    """QualifiedOnly when another provider owns the bare id, else BareAndQualified."""
    return "QualifiedOnly" if model_id in roster else "BareAndQualified"


# --------------------------------------------------------------------------- #
# Emitter
# --------------------------------------------------------------------------- #


def render_module(plan: Plan) -> bytes:
    """Render the generated overlay module exactly per the binding contract."""
    n = len(plan.entries)
    lines = [
        GENERATED_HEADER,
        "//",
        f"// Synced {n} models. Regenerate via `claude-code-proxy serve` or the kit sync script.",
        "",
        "use crate::providers::opencode::model::EndpointKind;",
        "use crate::providers::opencode::overlay::{Advertisement, SyncedEntry};",
        "",
        "pub const SYNCED: bool = true;",
        "",
        "#[rustfmt::skip]",
        "pub const ENTRIES: &[SyncedEntry] = &[",
    ]
    for entry in plan.entries:  # already sorted by id (byte order)
        lines.append(
            f'    SyncedEntry {{ id: "{entry.id}", endpoint: EndpointKind::{entry.protocol}, '
            f"advert: Advertisement::{entry.advert} }},"
        )
    lines.append("];")
    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def build_report(
    plan: Plan,
    outcome: str,
    exit_code: int,
    added: list[str],
    removed: list[str],
    sources: dict[str, str | None],
    reason: str | None = None,
) -> dict:
    return {
        "timestamp": _now(),
        "outcome": outcome,
        "exit_code": exit_code,
        "reason": reason,
        "live_ids": plan.live_ids,
        "routable": plan.routed,
        "exclusions": [{"id": i, "reason": r} for i, r in plan.exclusions],
        "pins_applied": [
            {"id": p.id, "protocol": p.protocol, "reason": p.reason} for p in plan.pins_applied
        ],
        "added": added,
        "removed": removed,
        "advertisement": {
            "qualified_only": plan.qualified_only,
            "bare_and_qualified": plan.bare_and_qualified,
        },
        "counts": {
            "live": len(plan.live_ids),
            "routable": len(plan.entries),
            "pinned": len(plan.pins_applied),
            "excluded": len(plan.exclusions),
            "added": len(added),
            "removed": len(removed),
        },
        "sources": sources,
    }


def failure_report(
    reason: str,
    stage: str,
    sources: dict[str, str | None],
    install: dict | None = None,
) -> dict:
    report = {
        "timestamp": _now(),
        "outcome": "failed",
        "exit_code": EXIT_FAILED,
        "reason": reason,
        "stage": stage,
        "live_ids": [],
        "routable": [],
        "exclusions": [],
        "pins_applied": [],
        "added": [],
        "removed": [],
        "advertisement": {"qualified_only": [], "bare_and_qualified": []},
        "counts": {},
        "sources": sources,
    }
    if install is not None:
        report["install"] = install
    return report


def install_stub(opts: "Options") -> dict:
    """Self-contained ``install`` block for a post-change failure report.

    A gate/build/install failure aborts an update that had already written the
    overlay, so the failure report carries the same ``install`` shape as the
    success path with ``installed: false`` and ``aborted: true``. ``backup`` is
    the on-disk backup path when one exists, else ``None``.
    """
    target = install_target_path(opts)
    backup = target + BACKUP_SUFFIX
    return {
        "target": target,
        "installed": False,
        "backup": backup if os.path.exists(backup) else None,
        "aborted": True,
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #


def _read_text(path: str | None, stage: str, what: str) -> str:
    if not path:
        raise SyncFailure(f"missing input: no path given for {what}", stage)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise SyncFailure(f"cannot read {what} at {path}: {exc}", stage) from exc


def _read_bytes_optional(path: str | None) -> bytes | None:
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SyncFailure(f"cannot read existing overlay at {path}: {exc}", "overlay") from exc


def _read_text_optional(path: str | None) -> str | None:
    data = _read_bytes_optional(path)
    return None if data is None else data.decode("utf-8", errors="replace")


def atomic_write(path: str, data: bytes) -> None:
    """Write bytes atomically (temp file in the same directory + rename).

    ``mkstemp`` creates the temp file 0600; the destination's previous mode is
    preserved when the file already exists and a fresh file gets 0644, so
    generated artifacts (the overlay, the report) are never left unreadable.
    The chmod happens on the temp file before the rename, so atomicity is kept.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sync-tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Gated install (ticket 04)
# --------------------------------------------------------------------------- #


def _describe(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def run_checked(argv: list[str], cwd: str | None, stage: str, timeout: float) -> None:
    """Run ``argv``; raise SyncFailure on a non-zero exit, timeout or spawn error.

    Output is captured (never leaked to the summary line) and the last line of
    stderr/stdout becomes the failure reason so one clear warning is printed.
    """
    what = _describe(argv)
    try:
        proc = subprocess.run(  # noqa: S603 - configured local command
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncFailure(f"{what} timed out after {timeout:g}s", stage) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SyncFailure(f"{what} could not run: {type(exc).__name__}: {exc}", stage) from exc
    if proc.returncode != 0:
        tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        detail = tail[-1] if tail else "no output"
        raise SyncFailure(f"{what} failed (exit {proc.returncode}): {detail}", stage)


def _manifest_path(opts: "Options") -> str:
    repo = opts.repo or REPO_DEFAULT
    return os.path.join(repo, "Cargo.toml")


def gate_commands(opts: "Options") -> list[list[str]]:
    """The ordered pre-install gate commands, each a full argv list.

    Defaults: ``cargo fmt --check``, this script's own ``--self-test`` fixture
    suite, then the focused catalog/routing tests. Every command is injectable
    via the ``Options`` struct or the matching CLI flag/env so tests can
    simulate pass/fail without real cargo.
    """
    manifest = _manifest_path(opts)
    fmt = opts.gate_fmt_cmd or ["cargo", "fmt", "--check", "--manifest-path", manifest]
    selftest = opts.gate_selftest_cmd or [
        sys.executable,
        opts.script or os.path.join(KIT_DEFAULT, "sync.py"),
        "--self-test",
    ]
    tests = opts.gate_test_cmds or [
        ["cargo", "test", "--lib", "providers::opencode", "--manifest-path", manifest],
        ["cargo", "test", "--lib", "registry", "--manifest-path", manifest],
    ]
    return [fmt, selftest, *tests]


def build_command(opts: "Options") -> list[str]:
    manifest = _manifest_path(opts)
    return opts.build_cmd or ["cargo", "build", "--release", "--manifest-path", manifest]


def install_target_path(opts: "Options") -> str:
    return opts.install_target or INSTALL_TARGET_DEFAULT


def install_stamp_path(opts: "Options") -> str:
    return install_target_path(opts) + INSTALL_STAMP_SUFFIX


def installed_binary_is_stale(opts: "Options", digest: str) -> bool:
    """Whether the installed binary was built from a different catalog.

    The stamp is written beside the install target on every successful built-in
    install. A missing, unreadable or mismatched stamp means the target on disk
    is not known to contain ``digest`` and must be rebuilt. An absent target is
    *not* stale: provisioning is bootstrap's job, and forcing a release build
    when someone runs from a fresh checkout would be surprising. A custom
    ``--install-cmd`` is trusted to place whatever it wants at the target, so
    stamping (and this check) is disabled there.
    """
    if not opts.install_enabled or opts.install_cmd:
        return False
    target = install_target_path(opts)
    if not os.path.exists(target):
        return False
    try:
        with open(install_stamp_path(opts), encoding="utf-8") as handle:
            return handle.read().strip() != digest
    except (OSError, UnicodeError):
        # Missing, unreadable or garbage: treat as stale.
        return True


def write_install_stamp(opts: "Options", digest: str) -> None:
    """Record the overlay digest the target was built from.

    Best effort: a failed stamp write must never fail or revert an install that
    already succeeded. The next run just forces one extra rebuild.
    """
    path = install_stamp_path(opts)
    try:
        atomic_write(path, (digest + "\n").encode("utf-8"))
    except OSError as exc:
        print(
            f"opencode-go sync: warning: cannot write install stamp at {path}: {exc}",
            file=sys.stderr,
        )


def built_binary_path(opts: "Options") -> str:
    if opts.built_binary:
        return opts.built_binary
    repo = opts.repo or REPO_DEFAULT
    return os.path.join(repo, BUILT_BINARY_REL)


def _atomic_copy(src: str, dst: str, mode: int | None = None) -> None:
    """Copy ``src`` to ``dst`` atomically (temp file in the target dir + rename)."""
    directory = os.path.dirname(os.path.abspath(dst))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sync-install-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _exclusive_copy(src: str, dst: str, mode: int | None = None) -> bool:
    """Create ``dst`` from ``src`` only if it does not already exist.

    Returns True when the file was created, False when it already existed (and
    was left byte-for-byte untouched). Race-free: the content is staged in a
    temp file in the destination directory and then hard-linked into place —
    ``link`` fails with ``EEXIST`` when a concurrent run won the race, so a
    pre-existing backup is never clobbered (belt-and-braces alongside the
    update-state lock). Filesystems without hard links fall back to an
    exclusive ``O_CREAT|O_EXCL`` create.
    """
    directory = os.path.dirname(os.path.abspath(dst))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sync-backup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        try:
            os.link(tmp, dst)
            return True
        except FileExistsError:
            return False
        except OSError:
            # No hard-link support on this filesystem: exclusive create instead.
            try:
                out_fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode or 0o644)
            except FileExistsError:
                return False
            with os.fdopen(out_fd, "wb") as out, open(tmp, "rb") as inp:
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def gated_install(opts: "Options", stamp_digest: str | None = None) -> str | None:
    """Gate, build and install the built binary; return the backup path or None.

    Raises SyncFailure (stage ``gate``/``build``/``install``) on any failure; the
    caller restores the previous overlay bytes so no partial state remains. A
    pre-existing target is backed up to ``<target>.release-bak`` once — an
    existing backup is never overwritten. An absent target needs no backup.
    """
    repo = opts.repo or REPO_DEFAULT
    for argv in gate_commands(opts):
        run_checked(argv, cwd=repo, stage="gate", timeout=opts.command_timeout)

    build = build_command(opts)
    run_checked(build, cwd=repo, stage="build", timeout=opts.command_timeout)

    source = built_binary_path(opts)
    target = install_target_path(opts)

    # Order is deliberate: the backup is taken from the *pre-install* target
    # before any install command runs, so a failed external install still leaves
    # a correct backup of the binary that was in place. Do not reorder these
    # (e.g. running --install-cmd before the backup would back up the new file).
    # Creation is exclusive (link-based) so a concurrent change-install can never
    # clobber an existing backup; the returned path is reported whenever the
    # backup file exists (matching the failure stub's behavior).
    backup = None
    if os.path.exists(target):
        backup = target + BACKUP_SUFFIX
        try:
            _exclusive_copy(target, backup, mode=os.stat(target).st_mode & 0o7777)
        except OSError as exc:
            raise SyncFailure(
                f"cannot back up existing install at {target}: {exc}", "install"
            ) from exc

    if opts.install_cmd:
        # The external command is trusted to install at ``target``; the built-in
        # stamp is deliberately not written because we cannot verify that it did.
        run_checked(opts.install_cmd, cwd=repo, stage="install", timeout=opts.command_timeout)
    else:
        if not os.path.isfile(source):
            raise SyncFailure(f"built binary not found at {source}", "build")
        try:
            _atomic_copy(source, target, mode=0o755)
        except OSError as exc:
            raise SyncFailure(f"cannot install built binary at {target}: {exc}", "install") from exc
        if stamp_digest is not None:
            write_install_stamp(opts, stamp_digest)
    return backup


def restore_overlay(path: str, previous_bytes: bytes | None) -> None:
    """Restore the overlay to its pre-write state (bytes, or absence)."""
    if previous_bytes is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SyncFailure(
                f"cannot remove partially written overlay at {path}: {exc}", "overlay"
            ) from exc
    else:
        try:
            atomic_write(path, previous_bytes)
        except OSError as exc:
            raise SyncFailure(
                f"cannot restore previous overlay at {path}: {exc}", "overlay"
            ) from exc


# --------------------------------------------------------------------------- #
# Update-state marker / PID lock (interrupted-update recovery)
# --------------------------------------------------------------------------- #


def update_state_path(opts: "Options") -> str:
    """Path of the update-state marker: in the report dir, beside the report.

    Deriving it from ``opts.report`` keeps the marker next to the report even
    when ``--report`` is overridden, so tests and sandboxes stay isolated.
    """
    return os.path.join(os.path.dirname(os.path.abspath(opts.report)), UPDATE_STATE_NAME)


def _read_update_state(path: str) -> dict | None:
    """Read the marker, or None when it is absent or unreadable/corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: object) -> bool:
    """True when ``pid`` names a live process we can signal.

    ``PermissionError`` means the process exists but belongs to another user
    (still alive); any other failure is treated as dead. A recycled PID can
    only ever make us *defer* one run (the marker's owner is presumed alive),
    which is the safe direction.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _update_state_payload(content: bytes) -> dict:
    """Marker contents: owner PID + timestamp + the sha256 of the new overlay."""
    return {
        "pid": os.getpid(),
        "timestamp": _now(),
        "overlay_sha256": hashlib.sha256(content).hexdigest(),
    }


def _update_state_is_stale(path: str) -> bool:
    """True when a marker exists and is either dead-owned or unreadable/corrupt.

    A marker that exists but cannot be parsed is treated exactly like one whose
    owner PID is dead: an interrupted update, so the next run takes it over and
    forces the update even when the overlay bytes already match. This keeps the
    takeover rule uniform with ``_acquire_update_state`` (which already takes
    over a corrupt marker).
    """
    if not os.path.exists(path):
        return False
    marker = _read_update_state(path)
    return marker is None or not _pid_alive(marker.get("pid"))


def _acquire_update_state(path: str, payload: dict) -> tuple[str, bool]:
    """Claim the update-state marker before writing the overlay.

    Returns ``(status, held)``:

    * ``("claimed", True)``  — a fresh marker was created exclusively;
    * ``("takeover", True)`` — a stale/interrupted marker was replaced;
    * ``("live", False)``    — another update is in progress (live owner PID);
    * ``("degraded", False)``— the marker is unwritable; proceed without it.

    The fast path uses an ``O_CREAT|O_EXCL`` create (atomic claim). When the
    file already exists it is inspected: a live PID means a concurrent update,
    a dead PID (or a corrupt marker) means an interrupted run and is taken over.
    """
    data = (json.dumps(payload) + "\n").encode("utf-8")
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return "degraded", False

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        pass
    except OSError:
        return "degraded", False
    else:
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                pass
            return "degraded", False
        return "claimed", True

    existing = _read_update_state(path)
    if existing is not None and _pid_alive(existing.get("pid")):
        return "live", False
    try:
        atomic_write(path, data)
    except OSError:
        return "degraded", False
    return "takeover", True


def _release_update_state(path: str) -> None:
    """Remove the marker after a successful install or a handled failure.

    Removal failure is non-fatal (the marker is a recovery aid, not the
    artifact); a stale marker only forces one extra update on the next run.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(
            f"opencode-go sync: warning: cannot remove update marker at {path}: {exc}",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# Options & run
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    live: str | None
    docs: str | None
    roster: str | None
    overrides: str
    previous: str | None
    overlay: str
    report: str
    dry_run: bool = False
    # Operator override: force one gated rebuild+install even when the catalog
    # bytes and the install stamp both say nothing changed (e.g. after pulling
    # upstream Rust changes that are not part of the catalog overlay).
    force: bool = False
    # Ticket 03 fetch layer. Direct construction defaults to the offline,
    # fixture-driven behavior (fetch=False); the CLI sets fetch=True so absent
    # paths fall back to the live sources.
    fetch: bool = False
    live_url: str = LIVE_URL_DEFAULT
    docs_url: str = DOCS_URL_DEFAULT
    roster_command: str | None = ROSTER_CMD_DEFAULT
    timeout: float = HTTP_TIMEOUT_DEFAULT
    repo: str | None = None
    # Ticket 04 gated install. Direct construction defaults to generation only
    # (install_enabled=False) so the fixture suite stays fast and offline; the
    # CLI enables it unless --no-install is given. Every command is a full argv
    # list; None means "use the computed default for this repo/kit".
    install_enabled: bool = False
    install_target: str | None = None
    built_binary: str | None = None
    script: str | None = None
    gate_fmt_cmd: list[str] | None = None
    gate_selftest_cmd: list[str] | None = None
    gate_test_cmds: list[list[str]] | None = None
    build_cmd: list[str] | None = None
    install_cmd: list[str] | None = None
    command_timeout: float = COMMAND_TIMEOUT_DEFAULT


def _source_labels(opts: Options) -> dict[str, str | None]:
    """Describe where each input comes from (paths, URLs or the command)."""
    return {
        "live_list": opts.live or (opts.live_url if opts.fetch else None),
        "docs": opts.docs or (opts.docs_url if opts.fetch else None),
        "roster": opts.roster
        or (
            (opts.roster_command or "working-copy sources")
            if opts.fetch
            else None
        ),
        "overrides": opts.overrides,
        "previous": opts.previous,
        "overlay_out": opts.overlay,
        "report": opts.report,
        "update_state": update_state_path(opts),
        "install_target": install_target_path(opts) if opts.install_enabled else None,
        "install_stamp": (
            install_stamp_path(opts)
            if opts.install_enabled and not opts.install_cmd
            else None
        ),
    }


def _split_cmd(value: str | None) -> list[str] | None:
    """Split a CLI/env command string into argv (None when unset/blank)."""
    if not value or not value.strip():
        return None
    return shlex.split(value)


def _split_cmds(value: str | None) -> list[list[str]] | None:
    """Split a newline-separated list of command strings into argv lists."""
    if not value or not value.strip():
        return None
    return [_split_cmd(line) for line in value.splitlines() if line.strip()]


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value) and value.strip().lower() not in ("0", "false", "no", "off", "")


def resolve_paths(args: argparse.Namespace) -> Options:
    kit = args.kit or os.environ.get("CCP_SYNC_KIT") or KIT_DEFAULT
    repo = args.repo or os.environ.get("CCP_SYNC_REPO") or REPO_DEFAULT
    overlay = args.overlay or os.environ.get("CCP_SYNC_OVERLAY") or os.path.join(repo, OVERLAY_REL)
    report = args.report or os.environ.get("CCP_SYNC_REPORT") or REPORT_DEFAULT
    overrides = (
        args.overrides or os.environ.get("CCP_SYNC_OVERRIDES") or os.path.join(kit, "overrides.toml")
    )
    timeout_arg = args.timeout if args.timeout is not None else os.environ.get("CCP_SYNC_HTTP_TIMEOUT")
    timeout = float(timeout_arg) if timeout_arg is not None else HTTP_TIMEOUT_DEFAULT
    cmd_timeout_arg = (
        args.command_timeout
        if args.command_timeout is not None
        else os.environ.get("CCP_SYNC_COMMAND_TIMEOUT")
    )
    command_timeout = (
        float(cmd_timeout_arg) if cmd_timeout_arg is not None else COMMAND_TIMEOUT_DEFAULT
    )
    install_enabled = not (args.no_install or _env_flag("CCP_SYNC_NO_INSTALL"))
    # Each --gate-test value is a single command string (action="append"); split
    # it into argv with the same helper as --gate-fmt/the env twin, so a
    # multi-word command stays one argv list rather than one token.
    gate_tests = (
        [_split_cmd(value) for value in args.gate_test]
        if args.gate_test
        else _split_cmds(os.environ.get("CCP_SYNC_GATE_TEST"))
    )
    return Options(
        live=args.live or os.environ.get("CCP_SYNC_LIVE_LIST"),
        docs=args.docs or os.environ.get("CCP_SYNC_DOCS"),
        roster=args.roster or os.environ.get("CCP_SYNC_ROSTER"),
        overrides=overrides,
        previous=args.previous or os.environ.get("CCP_SYNC_PREVIOUS") or overlay,
        overlay=overlay,
        report=report,
        dry_run=args.dry_run,
        force=args.force,
        fetch=True,
        live_url=args.live_url or os.environ.get("CCP_SYNC_LIVE_URL") or LIVE_URL_DEFAULT,
        docs_url=args.docs_url or os.environ.get("CCP_SYNC_DOCS_URL") or DOCS_URL_DEFAULT,
        roster_command=args.roster_command
        or os.environ.get("CCP_SYNC_ROSTER_CMD")
        or ROSTER_CMD_DEFAULT,
        timeout=timeout,
        repo=repo,
        install_enabled=install_enabled,
        install_target=args.install_target
        or os.environ.get("CCP_SYNC_INSTALL")
        or INSTALL_TARGET_DEFAULT,
        built_binary=args.built_binary or os.environ.get("CCP_SYNC_BUILT_BINARY"),
        script=args.script or os.environ.get("CCP_SYNC_SCRIPT") or os.path.join(kit, "sync.py"),
        gate_fmt_cmd=_split_cmd(args.gate_fmt or os.environ.get("CCP_SYNC_GATE_FMT")),
        gate_selftest_cmd=_split_cmd(args.gate_selftest or os.environ.get("CCP_SYNC_GATE_SELFTEST")),
        gate_test_cmds=gate_tests,
        build_cmd=_split_cmd(args.build_cmd or os.environ.get("CCP_SYNC_BUILD_CMD")),
        install_cmd=_split_cmd(args.install_cmd or os.environ.get("CCP_SYNC_INSTALL_CMD")),
        command_timeout=command_timeout,
    )


def run(opts: Options) -> int:
    sources = _source_labels(opts)

    live_ids = parse_live_list(_live_input(opts))
    scraped = parse_docs_table(_docs_input(opts))
    roster = resolve_roster(opts)
    overrides = parse_overrides(_read_text(opts.overrides, "overrides", "overrides file"))

    plan = compute(live_ids, scraped, roster, overrides)
    content = render_module(plan)

    previous_ids = parse_previous_ids(_read_text_optional(opts.previous))
    added = sorted(set(plan.routed) - previous_ids)
    removed = sorted(previous_ids - set(plan.routed))

    current = _read_bytes_optional(opts.overlay)

    # Recovery: a marker left by an interrupted update (SIGKILL/OOM/power-loss
    # between the overlay write and the install) has a dead owner PID. The
    # overlay may already match the generated content while the binary is still
    # stale, so a stale marker (dead PID or corrupt/unreadable) forces the full
    # update even when the bytes match.
    state_path = update_state_path(opts)
    stale_marker = _update_state_is_stale(state_path)

    overlay_digest = hashlib.sha256(content).hexdigest()
    wrote_overlay = current != content
    # A catalog that already matches can still sit behind an older binary when
    # the overlay was regenerated with --no-install, edited by hand, or rolled
    # back. The install stamp catches that case and forces one rebuild. When the
    # overlay itself changed the install is already forced, so the flag is only
    # reported for the otherwise-no-change case.
    stale_binary = (not wrote_overlay) and installed_binary_is_stale(opts, overlay_digest)
    forced = opts.force

    changed = wrote_overlay or stale_marker or stale_binary or forced
    outcome = "updated" if changed else "no-change"
    exit_code = EXIT_UPDATED if changed else EXIT_NO_CHANGE

    backup: str | None = None
    installed = False
    if changed and not opts.dry_run:
        # Claim the update-state marker BEFORE writing the overlay. It doubles
        # as a single-machine PID lock: a live owner means another update is in
        # progress (write nothing, exit 1 = warn+serve); a dead owner (or an
        # unwritable marker directory) lets this run proceed.
        status, held = _acquire_update_state(state_path, _update_state_payload(content))
        if status == "live":
            print(
                "opencode-go sync: another update is in progress "
                f"(marker {state_path}); leaving it to that run",
                file=sys.stderr,
            )
            return EXIT_BUSY
        try:
            # A stale-binary-only update must not rewrite (or churn) the overlay:
            # the bytes already match. Restore-on-failure only applies when the
            # overlay was actually written.
            if wrote_overlay:
                try:
                    atomic_write(opts.overlay, content)
                except OSError as exc:
                    # Surface as an expected failure so execute() reports it cleanly
                    # (one stderr line, exit 1) and — since the report path is
                    # independent of the overlay path — still writes the failed report.
                    raise SyncFailure(
                        f"cannot write overlay at {opts.overlay}: {exc}", "overlay"
                    ) from exc

            if opts.install_enabled:
                # Gated install. If the overlay was written, restore the previous
                # bytes on ANY failure so no partial state remains and the next
                # run retries; the install target is never touched before the
                # gate and build pass.
                try:
                    backup = gated_install(opts, overlay_digest)
                    installed = True
                except SyncFailure as exc:
                    if wrote_overlay:
                        restore_overlay(opts.overlay, current)
                        raise SyncFailure(
                            f"{exc.reason} (install aborted; previous overlay restored)",
                            exc.stage,
                        ) from exc
                    raise
        finally:
            # Remove the marker after a successful install AND on handled
            # failures (the revert path above). SIGKILL leaves it behind — that
            # is the recovery signal the next run acts on.
            if held:
                _release_update_state(state_path)

    report = build_report(plan, outcome, exit_code, added, removed, sources)
    if opts.install_enabled:
        report["install"] = {
            "target": install_target_path(opts),
            "installed": installed,
            "backup": backup,
            "stale_binary": stale_binary,
            "forced": forced,
            "stamp": None if opts.install_cmd else install_stamp_path(opts),
        }
    if not opts.dry_run:
        try:
            atomic_write(opts.report, (json.dumps(report, indent=2) + "\n").encode("utf-8"))
        except OSError as exc:
            # The report is bookkeeping; the overlay is the artifact that
            # matters. If the overlay was written the run is still "updated"
            # (exit 2) so the serve hook (ticket 05) proceeds to
            # gate/build/install — a failed report write must never silently
            # defer that. With no overlay change the outcome stays 0. A single
            # clear stderr line is the only trace (no report path to write to).
            print(
                f"opencode-go sync: failed (report): cannot write report at "
                f"{opts.report}: {exc}",
                file=sys.stderr,
            )
            return exit_code

    _summarize(opts, plan, outcome, added, removed, stale_binary, forced)
    return exit_code


def execute(opts: Options) -> int:
    """Run the sync, converting a visible failure into exit code 1 + report."""
    try:
        return run(opts)
    except SyncFailure as exc:
        sources = _source_labels(opts)
        install = install_stub(opts) if exc.stage in POST_CHANGE_STAGES else None
        if not opts.dry_run:
            try:
                atomic_write(
                    opts.report,
                    (
                        json.dumps(
                            failure_report(exc.reason, exc.stage, sources, install), indent=2
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except OSError as write_exc:
                print(f"warning: could not write report: {write_exc}", file=sys.stderr)
        print(f"opencode-go sync: failed ({exc.stage}): {exc.reason}", file=sys.stderr)
        return EXIT_FAILED


def _summarize(
    opts: Options,
    plan: Plan,
    outcome: str,
    added: list[str],
    removed: list[str],
    stale_binary: bool = False,
    forced: bool = False,
) -> None:
    detail = "" if opts.dry_run else f" -> {opts.overlay}"
    reasons = []
    if stale_binary:
        reasons.append("installed binary stale")
    if forced:
        reasons.append("forced rebuild")
    note = f" ({'; '.join(reasons)})" if reasons else ""
    print(
        f"opencode-go sync: {outcome} | live={len(plan.live_ids)} "
        f"routable={len(plan.entries)} pinned={len(plan.pins_applied)} "
        f"excluded={len(plan.exclusions)} added={len(added)} removed={len(removed)}"
        f"{note}{detail}"
    )
    if added:
        print(f"  added: {', '.join(added)}")
    if removed:
        print(f"  removed: {', '.join(removed)}")
    for model_id, reason in plan.exclusions:
        print(f"  excluded {model_id}: {reason}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

EPILOG = """\
exit codes (also written to the report as "exit_code"):
  0  no-change   inputs produced the exact bytes already on disk; nothing written
  2  updated     an overlay was written, or a stale binary was rebuilt, or
                 --force requested one; unless --no-install the release binary
                 was gated, built and installed over the install target
  1  failed      visible failure (drift, parse error, missing input, gate/build/
                 install); on any post-write failure the previous overlay bytes
                 are restored so no partial state remains and the next run retries

gated install (ticket 04): when a change is detected the sync runs, in order,
  * cargo fmt --check
  * python3 <kit>/sync.py --self-test   (this fixture suite; fast and offline)
  * cargo test --lib providers::opencode
  * cargo test --lib registry
then 'cargo build --release', then installs the built binary atomically (temp
file in the target dir, chmod +x, rename over). A pre-existing install target is
backed up once to <install>.release-bak and an existing backup is never
overwritten; an absent target needs no backup. A no-change run skips the gate,
the build and the install entirely. Every command and path is overridable
(flags/env) so the flow can be exercised without real cargo.

explicit rebuild (--force):
  A no-change run is the common fast path, but it also means Rust changes that
  do not touch the catalog (an upstream merge, a local fix) leave the installed
  binary untouched. '--force' enters the same gated install path without waiting
  for a catalog change, and exits 2 so the serve hook re-execs. The overlay is
  not rewritten when its bytes already match. Pair it with --no-install or
  --dry-run to inspect the decision without building.

install stamp (stale-binary recovery):
  A successful built-in install writes <install>.catalog-sha256 containing the
  sha256 of the overlay it was built from. Every run compares that stamp with
  the freshly generated overlay; a missing or mismatched stamp forces one gated
  rebuild+install even when the overlay bytes are unchanged. This closes the
  gap left by --no-install, a hand-edited overlay, or a rollback: those can
  leave the catalog up to date while the installed binary is old. The target
  must already exist (bootstrap provisions it); an absent target never forces
  a build. A custom --install-cmd is trusted to install the target, so it
  disables the stamp. The first run after this behavior is introduced rebuilds
  once because no stamp exists yet.

update-state marker (interrupted-update recovery + single-machine PID lock):
  A change-run claims a marker in the report directory (default
  ~/.local/state/claude-code-proxy/update.state; it follows --report) BEFORE
  writing the overlay, with contents = owner PID + timestamp + the new overlay
  sha256. It is removed after a successful install and on a handled failure's
  revert. If the marker names a live PID, another update is in progress: nothing
  is written and the run exits 1 (the serve hook warns once and serves the
  existing binary). If it names a dead PID (SIGKILL/OOM/power-loss between the
  overlay write and the install), the next run takes over and FORCES the update
  even when the overlay bytes already match — so a stale binary is never served
  indefinitely. An unwritable marker directory degrades gracefully (the run
  proceeds without the marker).

safety valves (documented per the spec; also printed by bootstrap.sh):
  CCP_SYNC_ON_SERVE=0     kill switch: disables the serve-time sync entirely.
  CCP_SYNC_JUST_EXECED    loop guard: its mere presence makes the serve pre-flight
                          skip exactly once; only the hook's re-exec sets it.
  A --no-install run that still detects a change exits 2 (operator-only flag; a
  hook-driven serve would re-exec identically and skip via the loop guard, so it
  is harmless). The install stamp ensures that the skipped install is performed
  by the next install-enabled run.

overrides.toml schema (TOML, parsed with tomllib; hand-maintained master):
  # Exclusions suppress a model no matter what the live list says.
  exclude = ["omen-alpha"]

  # Pins force a model into the catalog with an explicit wire protocol, even
  # when the id is in neither the live list nor the documented endpoint table.
  # A pin's protocol wins over any scraped value.
  [[pins]]
  id = "gemini-4-argon-contributor"
  protocol = "Responses"            # ChatCompletions | Messages | Responses
  reason = "Google-native endpoint; pin to Responses"

precedence: exclude > pin > scraped.

environment variables (CLI flags win over environment):
  CCP_SYNC_KIT        kit root             (default ~/.local/share/claude-code-proxy-sync)
  CCP_SYNC_REPO       working copy         (default ~/repos/claude-code-proxy)
  CCP_SYNC_LIVE_LIST  live-list snapshot / --live
  CCP_SYNC_DOCS       docs snapshot / --docs
  CCP_SYNC_ROSTER     roster snapshot / --roster
  CCP_SYNC_OVERRIDES  overrides TOML / --overrides
  CCP_SYNC_PREVIOUS   previous overlay / --previous
  CCP_SYNC_OVERLAY    overlay output path / --overlay
  CCP_SYNC_REPORT     report JSON path / --report
  CCP_SYNC_LIVE_URL   live-list URL / --live-url   (default https://opencode.ai/zen/go/v1/models)
  CCP_SYNC_DOCS_URL   docs URL / --docs-url        (default https://opencode.ai/v2/docs/console/go/)
  CCP_SYNC_ROSTER_CMD roster command / --roster-command (default '<install>/claude-code-proxy models')
  CCP_SYNC_HTTP_TIMEOUT fetch timeout seconds / --timeout (default 15)
  CCP_SYNC_INSTALL    install target / --install-target (default ~/.local/bin/claude-code-proxy)
  CCP_SYNC_BUILT_BINARY release binary to install / --built-binary
  CCP_SYNC_SCRIPT     this script's path / --script (default <kit>/sync.py)
  CCP_SYNC_NO_INSTALL set to 1 to generate only / --no-install (skip gate/build/install)
  CCP_SYNC_GATE_FMT   formatting gate command / --gate-fmt
  CCP_SYNC_GATE_SELFTEST fixture-suite gate command / --gate-selftest
  CCP_SYNC_GATE_TEST  focused test gate commands, newline-separated / --gate-test (repeatable)
  CCP_SYNC_BUILD_CMD  build command / --build-cmd
  CCP_SYNC_INSTALL_CMD override the built-in install / --install-cmd
  CCP_SYNC_COMMAND_TIMEOUT gate/build/install timeout / --command-timeout (default 1800)

live sources (ticket 03): when --live / --docs / --roster are omitted the sync
fetches the live OpenCode Go model list and the docs markdown variant (sent with
'Accept: text/markdown') and extracts the other-provider roster by running the
installed binary's 'models' command (falling back to the working-copy sources).
Both endpoints are keyless; no credentials are read. Any fetch, HTTP, drift or
extraction failure is a visible failure (exit 1) that leaves the overlay
untouched. All four are overridable (flags/env) so tests can point at localhost.

fixture suite (the ticket-04 pre-install gate):
  python3 sync.py --self-test      exits 0 on success, 1 on any failure
"""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that exits 1 (not 2) on usage errors.

    Exit code 2 is reserved for the "updated" outcome so the serve hook can
    dispatch on it unambiguously.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAILED, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--live", metavar="PATH", help="live-list JSON snapshot (openai-style)")
    parser.add_argument("--docs", metavar="PATH", help="docs markdown snapshot with endpoint table")
    parser.add_argument("--roster", metavar="PATH", help="other-provider roster snapshot")
    parser.add_argument("--overrides", metavar="PATH", help="overrides TOML master")
    parser.add_argument("--previous", metavar="PATH", help="previous generated overlay content")
    parser.add_argument("--overlay", metavar="PATH", help="overlay output path")
    parser.add_argument("--report", metavar="PATH", help="report JSON output path")
    parser.add_argument("--repo", metavar="PATH", help="working-copy root (overlay base)")
    parser.add_argument("--kit", metavar="PATH", help="kit root (overrides base)")
    parser.add_argument(
        "--live-url", metavar="URL", help="live model-list URL (default: OpenCode Go /models)"
    )
    parser.add_argument(
        "--docs-url", metavar="URL", help="docs URL, fetched as text/markdown (default: console/go)"
    )
    parser.add_argument(
        "--roster-command",
        metavar="CMD",
        help="command whose 'models'-style stdout yields the other-provider roster",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="SEC",
        help="fetch/subprocess timeout in seconds (default 15)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="compute and print; write neither overlay nor report"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="force a gated rebuild+install even when the catalog is unchanged",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run the fixture suite and exit"
    )
    # Ticket 04 gated install (see the help epilog for the env equivalents).
    parser.add_argument(
        "--install-target",
        metavar="PATH",
        help="installed binary path (default ~/.local/bin/claude-code-proxy)",
    )
    parser.add_argument(
        "--built-binary",
        metavar="PATH",
        help="release binary to install (default <repo>/target/release/claude-code-proxy)",
    )
    parser.add_argument(
        "--script", metavar="PATH", help="this sync script's path (default <kit>/sync.py)"
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="generate only: skip the gate, build and install (tests/inspection)",
    )
    parser.add_argument(
        "--gate-fmt", metavar="CMD", help="formatting gate command (default 'cargo fmt --check ...')"
    )
    parser.add_argument(
        "--gate-selftest", metavar="CMD", help="fixture-suite gate command (default 'python3 <kit>/sync.py --self-test')"
    )
    parser.add_argument(
        "--gate-test",
        metavar="CMD",
        action="append",
        help="focused test gate command; repeat for each (default the two cargo test filters)",
    )
    parser.add_argument(
        "--build-cmd", metavar="CMD", help="build command (default 'cargo build --release ...')"
    )
    parser.add_argument(
        "--install-cmd",
        metavar="CMD",
        help="override the built-in atomic install with an external command (advanced/tests)",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        metavar="SEC",
        help="gate/build/install command timeout in seconds (default 1800)",
    )
    return parser


def self_test() -> int:
    import unittest

    tests_dir = os.path.join(SCRIPT_DIR, "tests")
    suite = unittest.TestLoader().discover(tests_dir, pattern="test_*.py", top_level_dir=SCRIPT_DIR)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return EXIT_NO_CHANGE if result.wasSuccessful() else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    return execute(resolve_paths(args))


if __name__ == "__main__":
    sys.exit(main())
