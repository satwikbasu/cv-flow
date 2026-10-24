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
DEST="${CVFLOW_DIR:-${HOME}/cv-flow}"

# Pull --ref out of the arguments to decide which ref to clone, but still pass
# every original argument through to the installer.
ref=""
prev=""
for arg in "$@"; do
    case "${prev}" in
        --ref) ref="${arg}" ;;
    esac
    case "${arg}" in
        --ref=*) ref="${arg#--ref=}" ;;
    esac
    prev="${arg}"
done

latest_tag() {
    git ls-remote --tags --refs "${REPO_URL}" 2>/dev/null \
        | awk '{print $2}' | sed 's#refs/tags/##' \
        | sort -V | tail -n1
}

if [ -z "${ref}" ]; then
    ref="$(latest_tag || true)"
fi

if [ -d "${DEST}/.git" ]; then
    echo "Updating existing checkout at ${DEST}"
    git -C "${DEST}" fetch --tags --quiet origin
else
    echo "Cloning ${REPO_URL} into ${DEST}"
    git clone --quiet "${REPO_URL}" "${DEST}"
fi

if [ -n "${ref}" ]; then
    echo "Using ref ${ref}"
    git -C "${DEST}" checkout --quiet "${ref}"
fi

exec "${DEST}/scripts/install.sh" "$@"
