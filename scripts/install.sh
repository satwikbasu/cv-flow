#!/usr/bin/env bash
#
# cv-flow installer. Safe to run repeatedly: every step checks the current
# state first and only does the work that is still missing, so a second run
# converges and exits 0.
#
# Usage: scripts/install.sh [--non-interactive] [--no-services] [--ref <git-ref>]
#
#   --non-interactive  never prompt; take the bot token and LLM key from the
#                      CVFLOW_BOT_TOKEN / CVFLOW_LLM_KEY environment variables
#                      if set, otherwise leave config.yaml with placeholders.
#   --no-services      skip installing the systemd unit.
#   --ref <git-ref>    check the repo out at this ref before installing.
#
# The bot always runs as the invoking (unprivileged) user. Only apt is called
# through sudo; nothing else is.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/install-lib.sh
. "${SCRIPT_DIR}/install-lib.sh"

LLM_BASE_URL="https://api.mistral.ai/v1"
VENV_DIR="${REPO_ROOT}/.venv"
LOCAL_BIN="${HOME}/.local/bin"

main() {
    parse_install_args "$@"

    if [ "$(id -u)" = "0" ]; then
        log_err "do not run the installer as root; run it as the user the bot should run as"
        exit 1
    fi

    if [ -n "${FLAG_REF}" ]; then
        checkout_ref "${FLAG_REF}"
    fi

    install_apt_deps
    install_tectonic
    install_python_package
    write_config
    if [ "${FLAG_NO_SERVICES}" = "1" ]; then
        log "Skipping systemd unit (--no-services)."
    else
        install_service
    fi
    run_checkup

    log ""
    log "Install complete. Next step: tailor your profile from your existing resume with"
    log "    ${VENV_DIR}/bin/cvflow onboard path/to/your-resume.pdf"
}

checkout_ref() {
    local ref="$1"
    if ! cmd_exists git || [ ! -d "${REPO_ROOT}/.git" ]; then
        log_warn "not a git checkout; ignoring --ref ${ref}"
        return 0
    fi
    if [ "$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "${ref}" ] \
       || [ "$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null)" = "$(git -C "${REPO_ROOT}" rev-parse "${ref}" 2>/dev/null)" ]; then
        log_ok "already on ${ref}"
        return 0
    fi
    git -C "${REPO_ROOT}" fetch --tags --quiet origin || true
    git -C "${REPO_ROOT}" checkout --quiet "${ref}"
    log_ok "checked out ${ref}"
}

install_apt_deps() {
    local pkgs="python3 python3-venv python3-pip git curl ca-certificates"
    local missing="" p
    for p in ${pkgs}; do
        apt_installed "${p}" || missing="${missing} ${p}"
    done
    if [ -z "${missing# }" ]; then
        log_ok "apt dependencies already present"
        return 0
    fi
    log "Installing apt packages:${missing}"
    sudo apt-get update
    # shellcheck disable=SC2086 # intentional word-splitting of the package list
    sudo apt-get install -y ${missing}
    log_ok "apt dependencies installed"
}

install_tectonic() {
    local min="0.17.0" ver=""
    if cmd_exists tectonic; then
        ver="$(tectonic --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
        if [ -n "${ver}" ] && version_ge "${ver}" "${min}"; then
            log_ok "tectonic ${ver} already present"
            ensure_local_bin_on_path
            return 0
        fi
        log "tectonic ${ver:-unknown} is below ${min}; installing a newer build"
    fi

    mkdir -p "${LOCAL_BIN}"
    # The official tectonic installer drops the binary into the current
    # directory. This is the one remote script we trust, pinned to https.
    # Download then run rather than piping into sh: a pipe closes once sh is
    # done reading, leaving curl to exit non-zero and trip pipefail. The
    # version check below is the real gate, so a noisy installer exit is fine.
    local installer
    installer="$(mktemp)"
    curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net -o "${installer}"
    ( cd "${LOCAL_BIN}" && sh "${installer}" ) \
        || log_warn "tectonic installer exited non-zero; checking for the binary anyway"
    rm -f "${installer}"
    ensure_local_bin_on_path

    ver="$(tectonic --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
    if [ -z "${ver}" ] || ! version_ge "${ver}" "${min}"; then
        log_err "tectonic ${ver:-unknown} is still below the required ${min}"
        exit 1
    fi
    log_ok "tectonic ${ver} installed"
}

ensure_local_bin_on_path() {
    path_has_dir "${LOCAL_BIN}" || export PATH="${LOCAL_BIN}:${PATH}"
    local profile="${HOME}/.profile"
    # shellcheck disable=SC2016 # the literal $HOME is meant to land in .profile unexpanded
    local line='export PATH="$HOME/.local/bin:$PATH"'
    if ! file_has "${profile}" "${line}"; then
        printf '\n# added by cv-flow installer\n%s\n' "${line}" >> "${profile}"
    fi
}

install_python_package() {
    if [ ! -d "${VENV_DIR}" ]; then
        log "Creating virtualenv at ${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
    fi
    if "${VENV_DIR}/bin/python" -c "import cvflow" >/dev/null 2>&1 \
       && [ -x "${VENV_DIR}/bin/cvflow" ]; then
        log_ok "cvflow package already installed in the virtualenv"
        return 0
    fi
    log "Installing the cvflow package (editable)"
    "${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip
    "${VENV_DIR}/bin/python" -m pip install --quiet -e "${REPO_ROOT}"
    log_ok "cvflow package installed"
}

write_config() {
    local cfg="${REPO_ROOT}/config.yaml" example="${REPO_ROOT}/config.example.yaml"
    if [ -f "${cfg}" ]; then
        chmod 600 "${cfg}"
        log_ok "config.yaml already present (left untouched)"
        return 0
    fi

    local token key
    if [ "${FLAG_NONINTERACTIVE}" = "1" ]; then
        token="${CVFLOW_BOT_TOKEN:-PASTE_YOUR_BOT_TOKEN}"
        key="${CVFLOW_LLM_KEY:-PASTE_YOUR_MISTRAL_KEY}"
        inject_config "${example}" "${cfg}" "${token}" "${key}"
        chmod 600 "${cfg}"
        if [ "${token}" = "PASTE_YOUR_BOT_TOKEN" ] || [ "${key}" = "PASTE_YOUR_MISTRAL_KEY" ]; then
            log_warn "config.yaml written with placeholders; edit it before running the bot"
        else
            log_ok "config.yaml written from environment values"
        fi
        return 0
    fi

    token="$(prompt_for_token)"
    key="$(prompt_for_llm_key)"
    inject_config "${example}" "${cfg}" "${token}" "${key}"
    chmod 600 "${cfg}"
    log_ok "config.yaml written (chmod 600)"
}

prompt_for_token() {
    local token
    while :; do
        token="$(prompt_secret 'Telegram bot token (from @BotFather): ')"
        if [ "${CVFLOW_SKIP_VALIDATION:-0}" = "1" ]; then
            printf '%s' "${token}"; return 0
        fi
        if validate_telegram_token "${token}"; then
            log_ok "Telegram token accepted" >/dev/tty
            printf '%s' "${token}"; return 0
        fi
        log_err "Telegram rejected that token; try again" >/dev/tty
    done
}

prompt_for_llm_key() {
    local key
    while :; do
        key="$(prompt_secret 'Mistral API key: ')"
        if [ "${CVFLOW_SKIP_VALIDATION:-0}" = "1" ]; then
            printf '%s' "${key}"; return 0
        fi
        if validate_llm_key "${LLM_BASE_URL}" "${key}"; then
            log_ok "Mistral key accepted" >/dev/tty
            printf '%s' "${key}"; return 0
        fi
        log_err "Mistral rejected that key; try again" >/dev/tty
    done
}

install_service() {
    local unit_dir="${HOME}/.config/systemd/user"
    local unit="${unit_dir}/cvflow.service"
    mkdir -p "${unit_dir}"
    local desired
    desired="$(service_unit_text)"
    if [ -f "${unit}" ] && [ "$(cat "${unit}")" = "${desired}" ]; then
        log_ok "systemd user unit already up to date"
    else
        printf '%s\n' "${desired}" > "${unit}"
        log_ok "wrote systemd user unit ${unit}"
    fi

    if ! cmd_exists systemctl; then
        log_warn "systemctl not found; start the bot manually with ${VENV_DIR}/bin/python -m cvflow.app"
        return 0
    fi
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now cvflow.service 2>/dev/null \
        || log_warn "could not enable the user service now; run: systemctl --user enable --now cvflow.service"
    log "To keep the bot running after you log out, enable lingering once:"
    log "    sudo loginctl enable-linger ${USER}"
}

service_unit_text() {
    cat <<EOF
[Unit]
Description=cv-flow job digest bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_ROOT}
ExecStart=${VENV_DIR}/bin/python -m cvflow.app
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
}

run_checkup() {
    log "Running the install self-test"
    local args=""
    if [ "${CVFLOW_SKIP_VALIDATION:-0}" = "1" ]; then
        args="--offline"
    fi
    # The self-test is a report, not a gate: a fresh box has no profile yet, so
    # it is expected to flag missing pieces without failing the install.
    # shellcheck disable=SC2086 # args is a single optional flag
    "${VENV_DIR}/bin/python" -m cvflow.checkup ${args} || true
}

main "$@"
