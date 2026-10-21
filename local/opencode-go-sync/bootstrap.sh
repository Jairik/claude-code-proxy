#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenCode Go catalog sync — one-command bootstrap (ticket 06).
#
# Takes a machine that has only the release binary installed and ends with the
# full local auto-sync loop active:
#
#   1. provision the durable kit's mutable file (the overrides master stub,
#      written only when missing — a hand-edited master is never touched);
#   2. ensure the disposable working copy exists (clone upstream at the pinned
#      revision when missing), apply the two kit patches idempotently and verify
#      the ten patched files;
#   3. run the FIRST LIVE SYNC with the kit sync script's real defaults
#      (fetch -> generate -> gate -> build -> backup-once -> install);
#   4. surface the models dropped from routing as pin candidates, the one manual
#      step, the two safety valves, and the retired upstream installer.
#
# Idempotent: re-running is a no-op (the sync detects no change, so there is no
# rebuild; the release backup is never overwritten; hand-edited overrides are
# preserved). Re-running after wiping the working copy restores the same state.
#
# Environment (same names the sync script and the serve hook use):
#   CCP_SYNC_KIT      kit root      (default ~/.local/share/claude-code-proxy-sync)
#   CCP_SYNC_REPO     working copy  (default ~/repos/claude-code-proxy)
#   CCP_SYNC_INSTALL  install path  (default ~/.local/bin/claude-code-proxy)
#   CCP_SYNC_SCRIPT   sync script   (default <kit>/sync.py)
# ---------------------------------------------------------------------------
set -euo pipefail

# Never leak the mktemp scratch files, even when die() or an early error exits.
trap 'rm -f "${OLD_MODELS:-}" "${NEW_MODELS:-}"' EXIT

KIT="${CCP_SYNC_KIT:-$HOME/.local/share/claude-code-proxy-sync}"
REPO="${CCP_SYNC_REPO:-$HOME/repos/claude-code-proxy}"
INSTALL="${CCP_SYNC_INSTALL:-$HOME/.local/bin/claude-code-proxy}"
SCRIPT="${CCP_SYNC_SCRIPT:-$KIT/sync.py}"
REPORT="${CCP_SYNC_REPORT:-$HOME/.local/state/claude-code-proxy/sync-report.json}"

CLONE_URL="https://github.com/raine/claude-code-proxy.git"
PIN_REV="2e2975aee754fff176765bcc2f3e582a79191751"

PATCH_0001="$KIT/patches/0001-catalog-overlay-seam.patch"
PATCH_0002="$KIT/patches/0002-serve-hook.patch"
BACKUP="$INSTALL.release-bak"

# The patched-file hash manifest (recorded on the first successful verification,
# then used to detect a half-wiped tree) and the pin-candidate log.
MANIFEST="$KIT/patched-files.sha256"
PIN_LOG="$KIT/pin-candidates.log"

# The generated overlay is rewritten by every sync, so its bytes are not a
# stable part of the provisioned tree and are excluded from the hash check.
GENERATED_OVERLAY="src/providers/opencode/overlay_generated.rs"

# Reference pin-candidate list captured by the first bootstrap (2026-10-14);
# seeded into a fresh pin-candidates.log so the file is never empty.
SEED_PIN_CANDIDATES=(
  "deepseek-flash"
  "deepseek-v4-flash"
  "deepseek-v4-flash-vision-exp"
  "glm-5"
  "glm-5.1"
  "grok-4.5"
  "hy3-preview"
  "kimi-k2.5"
  "mimo-v2-omni"
  "mimo-v2-pro"
  "minimax-m2.5"
  "omen-alpha"
  "qwen3.5-plus"
  "qwen3.6-plus"
  "qwen3.7-max"
)

# Sentinel files added by each patch (stable across syncs — unlike the generated
# overlay, whose bytes change once a sync has run — so they are the robust
# "already applied" probe; `git apply --check --reverse` is used as a fallback).
SENTINEL_0001="src/providers/opencode/overlay.rs"
SENTINEL_0002="src/serve_hook.rs"

# The ten files the two patches touch (verified after application).
PATCHED_FILES=(
  "src/providers/opencode/model.rs"
  "src/providers/opencode/mod.rs"
  "src/providers/opencode/overlay.rs"
  "src/providers/opencode/overlay_generated.rs"
  "src/registry.rs"
  "src/lib.rs"
  "src/main.rs"
  "src/serve_hook.rs"
  "tests/cli.rs"
  "tests/serve_hook.rs"
)

usage() {
  cat <<'EOF'
usage: bootstrap.sh [--help]

One-command bootstrap for the OpenCode Go catalog auto-sync. Provisions the
durable kit's mutable overrides master (only when missing), the disposable
working copy (cloned at the pinned revision when missing), applies the two kit
patches idempotently, then runs the first live sync (fetch -> generate -> gate
-> build -> back up the release binary once -> install). Prints the one manual
step, the two safety valves, the overrides master path, the dropped-model pin
candidates, and the retired-upstream-installer note.

Idempotent: re-running is a no-op; the release backup and hand-edited overrides
are never overwritten.

No arguments. Configuration is via the environment (CCP_SYNC_KIT, CCP_SYNC_REPO,
CCP_SYNC_INSTALL, CCP_SYNC_SCRIPT), matching the sync script and serve hook.
EOF
}

log()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'bootstrap: error: %s\n' "$*" >&2; exit 1; }

# A half-provisioned working copy (e.g. the sentinel exists but other patched
# files are missing or altered) is unrecoverable by re-applying patches; the
# operator must move it aside. Print that hint and exit.
die_half_wiped() {
  printf 'bootstrap: error: %s\n' "$*" >&2
  printf '  the working copy looks half-provisioned; move it aside and re-run bootstrap:\n' >&2
  printf '    mv %s %s.bad\n' "$REPO" "$REPO" >&2
  exit 1
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $arg (try --help)" ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 is required but not on PATH"
command -v git >/dev/null 2>&1 || die "git is required but not on PATH"

log "bootstrap: OpenCode Go catalog auto-sync"
log "  kit:             $KIT"
log "  working copy:    $REPO"
log "  install target:  $INSTALL"
log "  sync script:     $SCRIPT"

# ---------------------------------------------------------------------------
# 1. Kit mutable files.
# ---------------------------------------------------------------------------
step "provisioning kit"

[ -f "$SCRIPT" ] || die "kit sync script missing: $SCRIPT"
[ -f "$PATCH_0001" ] || die "kit patch missing: $PATCH_0001"
[ -f "$PATCH_0002" ] || die "kit patch missing: $PATCH_0002"

if [ -f "$KIT/overrides.toml" ]; then
  log "  overrides master present (left untouched): $KIT/overrides.toml"
else
  mkdir -p "$KIT"
  cat > "$KIT/overrides.toml" <<'OVERRIDES'
# Overrides master for the OpenCode Go catalog sync (hand-maintained).
#
# This file is the durable master kept in the kit; it is never regenerated.
# It is applied on top of the live OpenCode Go model list joined with the
# official endpoint table. Precedence: exclude > pin > scraped.
#
# Parsed with tomllib (Python 3 stdlib). Edit by hand, then restart serve.

# ---------------------------------------------------------------------------
# Exclusions: suppress a model no matter what the live list says.
# A list of bare model IDs (the ID as it appears in the live list / docs table).
# ---------------------------------------------------------------------------
exclude = []

# ---------------------------------------------------------------------------
# Pins: force a model into the catalog with an explicit wire protocol.
# A pin is included even when the id is in NEITHER the live list NOR the
# documented endpoint table (fully manual), and its protocol wins over any
# scraped value. protocol must be one of:
#   ChatCompletions | Messages | Responses
# ---------------------------------------------------------------------------
# [[pins]]
# id = "gemini-4-argon-contributor"
# protocol = "Responses"
# reason = "Google-native endpoint; pin to Responses"
#
# [[pins]]
# id = "kimi-k3"
# protocol = "ChatCompletions"
OVERRIDES
  log "  wrote overrides master stub: $KIT/overrides.toml"
fi

# ---------------------------------------------------------------------------
# 2. Working copy: clone at the pinned revision when missing; apply patches.
# ---------------------------------------------------------------------------
step "provisioning working copy"

if [ -d "$REPO/.git" ]; then
  log "  working copy present: $REPO"
elif [ -e "$REPO" ]; then
  die "$REPO exists but is not a git working copy; move it aside and re-run"
else
  log "  cloning $CLONE_URL"
  mkdir -p "$(dirname "$REPO")"
  git clone --quiet "$CLONE_URL" "$REPO" || die "git clone failed"
  # Landing on the pinned revision is the only other git call; there are no
  # commits, pulls, fetches or pushes anywhere in this pipeline.
  git -C "$REPO" checkout --quiet --detach "$PIN_REV" \
    || die "cannot check out pinned revision $PIN_REV"
  log "  checked out pinned revision ${PIN_REV:0:7}"
fi

# Idempotent patch application. Sentinel presence means "already applied"; a
# clean forward apply means "apply now"; a clean reverse-check is the fallback
# probe. The generated overlay is deliberately NOT used as a sentinel because a
# prior sync rewrites its bytes.
apply_patch() {
  local patch="$1" sentinel="$2" name
  name="$(basename "$patch")"
  if [ -e "$REPO/$sentinel" ]; then
    # Sentinel present: still run the reverse-check. A clean reverse-apply means
    # the patch is fully applied; a failure is expected once a sync has rewritten
    # the generated overlay, so the hash manifest below is the real authority.
    if git -C "$REPO" apply --reverse --check "$patch" >/dev/null 2>&1; then
      log "  already applied (reverse-check): $name"
    else
      log "  already applied: $name"
    fi
    return 0
  fi
  if git -C "$REPO" apply --check "$patch" >/dev/null 2>&1; then
    git -C "$REPO" apply "$patch" || die_half_wiped "failed to apply $name"
    log "  applied: $name"
  elif git -C "$REPO" apply --reverse --check "$patch" >/dev/null 2>&1; then
    log "  already applied (reverse-check): $name"
  else
    die_half_wiped "patch neither applies nor is already applied: $name"
  fi
}

apply_patch "$PATCH_0001" "$SENTINEL_0001"
apply_patch "$PATCH_0002" "$SENTINEL_0002"

# Verify the ten patched files, and detect a half-wiped tree on later runs via
# the hash manifest recorded on the first successful verification.
verify_patched_files() {
  local missing=0 f bad="" hash path
  for f in "${PATCHED_FILES[@]}"; do
    [ -f "$REPO/$f" ] || { log "  MISSING patched file: $f"; missing=1; }
  done
  [ "$missing" -eq 0 ] || die_half_wiped "patched-file verification failed (missing files)"

  if [ ! -f "$MANIFEST" ]; then
    ( cd "$REPO" && sha256sum "${PATCHED_FILES[@]}" ) > "$MANIFEST" \
      || die "cannot write patched-file manifest: $MANIFEST"
    log "  recorded patched-file manifest (${#PATCHED_FILES[@]} files): $MANIFEST"
    return 0
  fi

  while read -r hash path; do
    [ -n "$path" ] || continue
    # The generated overlay is rewritten by every sync; skip its hash.
    [ "$path" = "$GENERATED_OVERLAY" ] && continue
    if [ ! -f "$REPO/$path" ] \
       || [ "$(sha256sum "$REPO/$path" | cut -d' ' -f1)" != "$hash" ]; then
      bad="$bad $path"
    fi
  done < "$MANIFEST"
  [ -z "$bad" ] || die_half_wiped "patched-file hash mismatch:$bad"
  log "  verified ${#PATCHED_FILES[@]} patched files (manifest $MANIFEST)"
}

verify_patched_files

# ---------------------------------------------------------------------------
# 3. First live sync (capture the old binary's models first for pin candidates).
# ---------------------------------------------------------------------------
step "running live sync (fetch -> generate -> gate -> build -> install)"

OLD_MODELS=""
if [ -x "$INSTALL" ]; then
  log "  capturing current binary catalog: $INSTALL models"
  OLD_MODELS="$(mktemp)"
  "$INSTALL" models > "$OLD_MODELS" 2>/dev/null || true
else
  log "  no existing binary at $INSTALL (nothing to back up or diff against)"
fi

set +e
env CCP_SYNC_KIT="$KIT" CCP_SYNC_REPO="$REPO" CCP_SYNC_INSTALL="$INSTALL" \
    CCP_SYNC_SCRIPT="$SCRIPT" \
    python3 "$SCRIPT"
sync_rc=$?
set -e
case "$sync_rc" in
  0) log "  sync: no change" ;;
  2) log "  sync: updated and installed" ;;
  *) die "sync failed (exit $sync_rc); the existing binary was kept" ;;
esac

# ---------------------------------------------------------------------------
# 4. Pin candidates + handover summary.
# ---------------------------------------------------------------------------
step "handover"

CANDIDATES=""
if [ -n "$OLD_MODELS" ] && [ -x "$INSTALL" ]; then
  NEW_MODELS="$(mktemp)"
  "$INSTALL" models > "$NEW_MODELS" 2>/dev/null || true
  CANDIDATES="$(python3 - "$OLD_MODELS" "$NEW_MODELS" <<'PY'
import re
import sys

def opencode_ids(path):
    """Underlying OpenCode Go ids advertised by a `models` output file.

    The `opencode:` group lists bare ids and `opencode-go/<id>` qualified forms;
    strip the prefix so a model that merely switched to qualified-only is not
    mistaken for a drop.
    """
    ids = set()
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = re.match(r"^opencode:\s*(.*)$", line.rstrip("\n"))
            if not match:
                continue
            body = match.group(1).split(";")[0]
            for token in body.split(","):
                token = token.strip()
                if not token:
                    continue
                if token.startswith("opencode-go/"):
                    token = token[len("opencode-go/"):]
                ids.add(token)
    return ids

old = opencode_ids(sys.argv[1])
new = opencode_ids(sys.argv[2])
print("\n".join(sorted(old - new)))
PY
)"
  rm -f "$NEW_MODELS"
fi
[ -n "$OLD_MODELS" ] && rm -f "$OLD_MODELS"

# Persist pin candidates: seed the reference list once, then append this run's.
if [ ! -f "$PIN_LOG" ]; then
  {
    printf '# pin candidates — reference list from the first bootstrap, 2026-10-14\n'
    printf '%s\n' "${SEED_PIN_CANDIDATES[@]}"
  } > "$PIN_LOG"
  log "  seeded pin-candidates.log: $PIN_LOG"
fi
if [ -n "$CANDIDATES" ]; then
  {
    printf '\n# %s — dropped from routing on this run\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    while IFS= read -r id; do
      [ -n "$id" ] && printf '%s\n' "$id"
    done <<< "$CANDIDATES"
  } >> "$PIN_LOG"
  log "  appended this run's pin candidates: $PIN_LOG"
fi

backup_state="(none — no pre-existing binary)"
[ -f "$BACKUP" ] && backup_state="$BACKUP"

log ""
log "=============================================================================="
log "OpenCode Go catalog sync — bootstrap complete"
log "=============================================================================="
log "Working copy:      $REPO"
log "Kit:               $KIT"
log "Overrides master:  $KIT/overrides.toml"
log "Installed binary:  $INSTALL"
log "Rollback backup:   $backup_state"
log "Sync report:       $REPORT"
log "Pin candidates:    $PIN_LOG"
log ""

if [ -n "$CANDIDATES" ]; then
  log "Dropped from routing — pin candidates (see the overrides master):"
  while IFS= read -r id; do
    [ -n "$id" ] && log "  $id"
  done <<< "$CANDIDATES"
  log "  (add a [[pins]] entry with an explicit protocol to keep any of these"
  log "   routable, or leave them dropped)"
else
  log "Dropped from routing — pin candidates: none"
fi

log ""
log "One manual step:"
log "  Start the proxy when ready:  claude-code-proxy serve"
log "  (the sync runs automatically on every serve; the first one after a"
log "   catalog change rebuilds, installs and transparently re-executes)"
log ""
log "Safety valves:"
log "  CCP_SYNC_ON_SERVE=0    kill switch — disables the serve-time sync entirely."
log "  CCP_SYNC_JUST_EXECED   loop guard — set by the transparent re-exec; its"
log "                         mere presence makes serve skip the sync once."
log "                         Only the hook sets it; do not set it yourself."
log ""
log "Note: the upstream install.sh is retired as the update path. Updates now come"
log "from this bootstrap plus the serve-time sync; running install.sh would"
log "clobber the synced build."
log "=============================================================================="
