#!/usr/bin/env bash
# Sync canonical shared configs from jr200-labs/github-action-templates into this repo.
# Driven by shared/MANIFEST.json — see that file for the file lists.
#
# Usage:
#   ./sync.sh [python|go|node|docker] # single-language override
#   ./sync.sh "python node"           # explicit multi-language override
#   ./sync.sh all                     # sync every language in MANIFEST
#   ./sync.sh                         # auto-detect from present marker files
#
# Auto-detect iterates every marker file (pyproject.toml, go.mod,
# package.json, Dockerfile / docker/Dockerfile / images/*/Dockerfile)
# and syncs each language's configs — polyglot-safe.
# Use `all` when you want every language regardless of markers (e.g. a
# tooling repo that doesn't check in any marker file).
#
# Three destination modes per file (declared in MANIFEST.json):
#   - cache     → written to .shared/<file> (gitignored; re-fetched in CI)
#   - committed → written to <file> at repo root (must be committed; drift-checked)
#   - merged    → fetch <source> from shared/, deep-merge with repo-local
#                 <local> via yq, write to <target> at repo root. Entry shape:
#                 {"source":"...","local":"...","target":"..."}. Merge order:
#                 local first, base last (arrays append) — consumers override
#                 template defaults via first-match-wins group ordering.
#
# Offline tolerance:
#   - SYNC_OFFLINE=1       → skip entirely, exit 0 (explicit opt-out)
#   - network failures     → warn to stderr, skip that file, exit 0 (never block commit)
#   - missing jq/curl      → warn + exit 0 (local dev without deps still commits)
#   - missing yq           → skip merged entries, preserve last-good target, exit 0

set -uo pipefail

REPO="jr200-labs/github-action-templates"
DEFAULT_REF="master"
SHARED_DIR=".shared"
CONFIG=".github/.shared-config.yaml"

warn() { echo "sync: $*" >&2; }

if [ "${SYNC_OFFLINE:-0}" = "1" ]; then
    warn "SYNC_OFFLINE=1 — skipping shared config sync"
    exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
    warn "curl not found — skipping shared config sync"
    exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
    warn "jq not found — skipping shared config sync"
    exit 0
fi

CONFIG_REF=""
if [ -f "$CONFIG" ]; then
    CONFIG_REF=$(sed -nE 's/^[[:space:]]*ref:[[:space:]]*["'\'']?([^"'\''][^[:space:]"'\'']*).*$/\1/p' "$CONFIG" | head -n 1)
fi
if [ -n "${SYNC_REF:-}" ]; then
    REF="$SYNC_REF"
elif [ -n "$CONFIG_REF" ]; then
    REF="$CONFIG_REF"
else
    REF="$DEFAULT_REF"
fi
BASE_URL="${SYNC_BASE_URL:-https://raw.githubusercontent.com/${REPO}/${REF}/shared}"

detect_languages() {
    # Emit every language whose marker file is present. Polyglot repos
    # (e.g. Go backend + Node frontend at root) need all their configs.
    # docker marker: Dockerfile at repo root, docker/Dockerfile, or any
    # images/*/Dockerfile (monorepo image-variant layout).
    local langs=()
    [ -f "pyproject.toml" ] || [ -f "setup.py" ] && langs+=("python")
    [ -f "go.mod" ] && langs+=("go")
    [ -f "package.json" ] && langs+=("node")
    if [ -f "Dockerfile" ] || [ -f "docker/Dockerfile" ] || compgen -G "images/*/Dockerfile" > /dev/null 2>&1; then
        langs+=("docker")
    fi
    echo "${langs[*]}"
}

download_to() {
    local remote_path="$1"
    local local_path="$2"
    mkdir -p "$(dirname "$local_path")"
    if curl -sfL --max-time 10 "${BASE_URL}/${remote_path}" -o "${local_path}.tmp"; then
        mv "${local_path}.tmp" "$local_path"
        echo "synced: ${remote_path} -> ${local_path}"
    else
        rm -f "${local_path}.tmp"
        warn "offline or fetch failed — skipped ${remote_path}"
        return 1
    fi
}

ensure_gitignore() {
    if [ -f ".gitignore" ]; then
        if ! grep -qxF "${SHARED_DIR}/" .gitignore 2>/dev/null; then
            echo "${SHARED_DIR}/" >> .gitignore
            echo "added ${SHARED_DIR}/ to .gitignore"
        fi
    fi
}

ensure_pnpm_workspace_defaults() {
    local workspace_file="pnpm-workspace.yaml"

    [ -f "$workspace_file" ] || return 0

    if ! command -v yq >/dev/null 2>&1; then
        warn "yq not found — skipped ${workspace_file} minimumReleaseAge default"
        return 0
    fi

    if yq -e '.minimumReleaseAge == 0' "$workspace_file" >/dev/null 2>&1; then
        return 0
    fi

    if yq '.minimumReleaseAge = 0' "$workspace_file" > "${workspace_file}.tmp" 2>/dev/null; then
        mv "${workspace_file}.tmp" "$workspace_file"
        echo "updated: ${workspace_file} minimumReleaseAge -> 0"
    else
        rm -f "${workspace_file}.tmp"
        warn "failed to update ${workspace_file} minimumReleaseAge default"
    fi
}

ensure_git_hooks_path() {
    if ! command -v git >/dev/null 2>&1; then
        warn "git not found — skipped core.hooksPath=.githooks"
        return 0
    fi

    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        warn "not inside a git work tree — skipped core.hooksPath=.githooks"
        return 0
    fi

    if [ "$(git config --get core.hooksPath || true)" = ".githooks" ]; then
        return 0
    fi

    git config core.hooksPath .githooks
    echo "updated: git core.hooksPath -> .githooks"
}

has_language() {
    local needle="$1" lang
    for lang in $LANGS; do
        [ "$lang" = "$needle" ] && return 0
    done
    return 1
}

# Fetch manifest — if network down, skip the whole sync cleanly.
MANIFEST_JSON=$(curl -sfL --max-time 10 "${BASE_URL}/MANIFEST.json" 2>/dev/null) || {
    warn "cannot fetch MANIFEST.json (offline?) — skipping sync"
    exit 0
}

LANGS="${1:-$(detect_languages)}"
if [ "$LANGS" = "all" ]; then
    # Expand to every language section declared in the manifest (excluding
    # 'common' and any non-language top-level keys like $schema / _comment).
    LANGS=$(echo "$MANIFEST_JSON" | jq -r 'keys[] | select(. != "common" and (startswith("$") or startswith("_") | not))' | tr '\n' ' ')
fi
if [ -z "$LANGS" ]; then
    warn "cannot detect project language(s) — pass python, go, node, or docker as argument; skipping"
    exit 0
fi

get_files() {
    # $1 = section (common | <lang>), $2 = mode (cache | committed)
    echo "$MANIFEST_JSON" | jq -r --arg s "$1" --arg m "$2" '(.[$s][$m] // []) | .[]'
}

get_merged_entries() {
    # $1 = section. Emit tab-separated source\tlocal\ttarget lines.
    echo "$MANIFEST_JSON" | jq -r --arg s "$1" \
        '(.[$s].merged // []) | .[] | [.source, .local, .target] | @tsv'
}

merge_entry() {
    # $1 = source (remote path under shared/), $2 = local repo file,
    # $3 = target repo file. yq-missing / merge-failure → warn + preserve
    # last-good target (never fail the sync).
    local source_path="$1" local_path="$2" target_path="$3"
    local base_tmp="${SHARED_DIR}/syncpack-base.tmp.yaml"

    mkdir -p "$(dirname "$base_tmp")"
    if ! curl -sfL --max-time 10 "${BASE_URL}/${source_path}" -o "$base_tmp"; then
        rm -f "$base_tmp"
        warn "offline or fetch failed — skipped merge ${source_path} -> ${target_path} (preserved last-good)"
        return 0
    fi

    if ! command -v yq >/dev/null 2>&1; then
        warn "yq not found — skipped merge ${source_path} -> ${target_path} (preserved last-good)"
        rm -f "$base_tmp"
        return 0
    fi

    mkdir -p "$(dirname "$target_path")"
    if [ -f "$local_path" ]; then
        if yq ea '. as $i ireduce ({}; . *+ $i)' "$local_path" "$base_tmp" > "${target_path}.tmp" 2>/dev/null; then
            mv "${target_path}.tmp" "$target_path"
            echo "merged: ${source_path} + ${local_path} -> ${target_path}"
        else
            rm -f "${target_path}.tmp"
            warn "yq merge failed — preserved last-good ${target_path}"
        fi
    else
        cp "$base_tmp" "$target_path"
        echo "merged (base-only, no ${local_path}): ${source_path} -> ${target_path}"
    fi
    rm -f "$base_tmp"
}

# Sync committed files (common + language-specific) → repo root
for section in common $LANGS; do
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        download_to "$f" "$f" || true
    done < <(get_files "$section" "committed")
done

# Sync cache files (common + language-specific) → .shared/
for section in common $LANGS; do
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        download_to "$f" "${SHARED_DIR}/$f" || true
    done < <(get_files "$section" "cache")
done

# Sync merged files (common + language-specific). Each entry is a
# {source, local, target} triple emitted as tab-separated values.
for section in common $LANGS; do
    while IFS=$'\t' read -r src loc tgt; do
        [ -z "$src" ] && continue
        merge_entry "$src" "$loc" "$tgt" || true
    done < <(get_merged_entries "$section")
done

# Refresh self for future syncs (best-effort)
download_to "sync.sh" "${SHARED_DIR}/sync.sh" || true
[ -f "${SHARED_DIR}/sync.sh" ] && chmod +x "${SHARED_DIR}/sync.sh"
[ -f "${SHARED_DIR}/lint-release-please-config.sh" ] && chmod +x "${SHARED_DIR}/lint-release-please-config.sh"
[ -f ".githooks/commit-msg" ] && chmod +x ".githooks/commit-msg"
[ -f ".githooks/lint-message-text.sh" ] && chmod +x ".githooks/lint-message-text.sh"
[ -f ".githooks/pre-commit" ] && chmod +x ".githooks/pre-commit"

if has_language node; then
    ensure_pnpm_workspace_defaults
fi

ensure_git_hooks_path
ensure_gitignore

echo "done."
