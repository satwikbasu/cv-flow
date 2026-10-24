#!/usr/bin/env bash
#
# One-command entry point for a fresh machine: clone cv-flow at the latest
# release tag (or a given ref) and hand off to scripts/install.sh.
#
# Usage:
#   curl -fsSL <raw-url>/scripts/bootstrap.sh | bash
#   curl -fsSL <raw-url>/scripts/bootstrap.sh | bash -s -- --ref v1.2.0 --no-services
#
# Any arguments are passed straight through to install.sh. Override the clone
# location with CVFLOW_DIR and the repo URL with CVFLOW_REPO.

set -euo pipefail

REPO_URL="${CVFLOW_REPO:-https://github.com/satwikbasu/cv-flow.git}"
DEST="${CVFLOW_DIR:-${HOME:-/root}/cv-flow}"

log() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }

# Pick the highest final release tag (vX.Y.Z) from tag names on stdin.
# Pre-release tags such as v0.2.0-rc1 are dropped so they cannot outrank the
# final v0.2.0 under `sort -V`.
select_latest_tag() {
    grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1
}

latest_tag() {
    git ls-remote --tags --refs "${REPO_URL}" 2>/dev/null \
        | awk '{print $2}' | sed 's#refs/tags/##' \
        | select_latest_tag
}

# Echo the value passed to --ref (either form) from the argument list.
parse_ref() {
    local ref="" prev="" arg
    for arg in "$@"; do
        case "${prev}" in
            --ref) ref="${arg}" ;;
        esac
        case "${arg}" in
            --ref=*) ref="${arg#--ref=}" ;;
        esac
        prev="${arg}"
    done
    printf '%s' "${ref}"
}

ensure_tools() {
    local missing=""
    command -v git  >/dev/null 2>&1 || missing="${missing} git"
    command -v curl >/dev/null 2>&1 || missing="${missing} curl"
    if [ -z "${missing# }" ]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        log "Installing required tools:${missing}"
        sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update
        # shellcheck disable=SC2086 # intentional word-splitting of the package list
        sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y ${missing}
    else
        err "missing required tools:${missing}; install them and re-run"
        exit 1
    fi
}

main() {
    if [ "$(id -u)" = "0" ]; then
        err "do not run the bootstrap as root; run it as the user the bot should run as"
        exit 1
    fi

    ensure_tools

    local ref
    ref="$(parse_ref "$@")"
    if [ -z "${ref}" ]; then
        ref="$(latest_tag || true)"
    fi

    if [ -d "${DEST}/.git" ]; then
        log "Updating existing checkout at ${DEST}"
        git -C "${DEST}" fetch --tags --quiet origin
        if [ -z "${ref}" ]; then
            # No ref requested: actually advance the current branch, not just fetch.
            git -C "${DEST}" pull --ff-only --quiet \
                || log "could not fast-forward the checkout; leaving it as-is"
        fi
    else
        log "Cloning ${REPO_URL} into ${DEST}"
        git clone --quiet "${REPO_URL}" "${DEST}"
    fi

    if [ -n "${ref}" ]; then
        log "Using ref ${ref}"
        git -C "${DEST}" checkout --quiet "${ref}"
    else
        log "no release tags found; installing $(git -C "${DEST}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD) HEAD"
    fi

    cd "${DEST}"
    exec "${DEST}/scripts/install.sh" "$@"
}

# Allow the pure helpers to be sourced by the test suite without running main.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
