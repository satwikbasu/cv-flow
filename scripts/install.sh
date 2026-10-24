#!/usr/bin/env bash
#
# cv-flow installer. Safe to run repeatedly: every step checks the current
# state first and only does the work that is still missing, so a second run
# converges and exits 0.
#
# Usage: scripts/install.sh [--non-interactive] [--no-services] [--ref <git-ref>]
#
#   --non-interactive  never prompt; take the bot token, LLM key and user id
#                      from CVFLOW_BOT_TOKEN / CVFLOW_LLM_KEY / CVFLOW_USER_ID
#                      if set, otherwise leave config.yaml with placeholders.
#   --no-services      skip installing the systemd unit.
#   --ref <git-ref>    check the repo out at this ref before installing.
#
# The bot always runs as the invoking (unprivileged) user. Only apt is called
# through sudo, and the cached sudo ticket is dropped straight after.

set -euo pipefail

: "${HOME:?HOME must be set to run the installer}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/install-lib.sh
. "${SCRIPT_DIR}/install-lib.sh"

LLM_BASE_URL="https://api.mistral.ai/v1"
VENV_DIR="${REPO_ROOT}/.venv"
LOCAL_BIN="${HOME}/.local/bin"

# tectonic 0.17.0 release assets, pinned by sha256. The musl builds are fully
# static (no shared-library dependencies) so they run on a bare Ubuntu box.
TECTONIC_VERSION="0.17.0"
TECTONIC_SHA_X86_64="8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7"
TECTONIC_SHA_AARCH64="b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8"

main() {
    parse_install_args "$@" || { rc=$?; [ "${rc}" = "10" ] && exit 0; exit "${rc}"; }

    if [ "$(id -u)" = "0" ]; then
        log_err "do not run the installer as root; run it as the user the bot should run as"
        exit 1
    fi

    # Resolve config.yaml, profile/ and data/ relative to the repo, not the
    # directory the installer happened to be launched from.
    cd "${REPO_ROOT}"

    if [ "${FLAG_NONINTERACTIVE}" != "1" ] && ! { : < /dev/tty; } 2>/dev/null; then
        log_err "no terminal available for prompts; re-run with --non-interactive and set CVFLOW_BOT_TOKEN / CVFLOW_LLM_KEY"
        exit 1
    fi

    if [ -n "${FLAG_REF}" ]; then
        checkout_ref "${FLAG_REF}"
    fi

    run_preflight
    install_apt_deps
    # Drop any cached sudo credential so the later download and pip install
    # cannot silently reuse root.
    if cmd_exists sudo; then sudo -k 2>/dev/null || true; fi
    install_tectonic
    install_python_package
    secure_dirs
    write_config
    if [ "${FLAG_NO_SERVICES}" = "1" ]; then
        log "Skipping systemd unit (--no-services)."
    else
        install_service
    fi
    run_checkup

    log ""
    log "Install complete."
    log "This shell needs ~/.local/bin on PATH: run 'source ~/.profile' or open a new login shell."
    log "(Ubuntu's default ~/.profile already adds it at login; the appended line covers non-login shells.)"
    log ""
    log "Next step: build your profile from your existing resume with"
    log "    cd ${REPO_ROOT} && .venv/bin/cvflow onboard path/to/your-resume.pdf"
    if [ "${FLAG_NO_SERVICES}" != "1" ]; then
        log "Then start the bot with: systemctl --user start cvflow.service"
    fi
}

checkout_ref() {
    local ref="$1"
    if ! cmd_exists git || [ ! -d "${REPO_ROOT}/.git" ]; then
        log_warn "not a git checkout; ignoring --ref ${ref}"
        return 0
    fi
    if [ "$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "${ref}" ] \
       || [ "$(git -C "${REPO_ROOT}" rev-parse "HEAD^{commit}" 2>/dev/null)" = "$(git -C "${REPO_ROOT}" rev-parse "${ref}^{commit}" 2>/dev/null)" ]; then
        log_ok "already on ${ref}"
        return 0
    fi
    git -C "${REPO_ROOT}" fetch --tags --quiet origin || true
    git -C "${REPO_ROOT}" checkout --quiet "${ref}"
    log_ok "checked out ${ref}"
    # Re-exec so the rest of the install runs the code of the ref just checked
    # out rather than the version this process started with.
    log "Re-running the installer on ${ref}"
    exec "${REPO_ROOT}/scripts/install.sh" "${CVFLOW_REEXEC_ARGS[@]}"
}

run_preflight() {
    if [ -r /etc/os-release ]; then
        local os_id=""
        # shellcheck source=/dev/null
        os_id="$(. /etc/os-release 2>/dev/null; printf '%s' "${ID:-}")"
        if [ -n "${os_id}" ] && [ "${os_id}" != "ubuntu" ] && [ "${os_id}" != "debian" ]; then
            log_warn "this installer targets Ubuntu/Debian; '${os_id}' may need manual steps"
        fi
    fi
    local arch
    arch="$(uname -m)"
    case "${arch}" in
        x86_64|aarch64|arm64) ;;
        *) log_err "unsupported CPU architecture '${arch}'; cv-flow needs x86_64 or aarch64"; exit 1 ;;
    esac
    if ! cmd_exists python3; then
        log_err "python3 is not installed; install Python 3.11 or newer and re-run"
        exit 1
    fi
    if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        log_err "python3 is older than 3.11 (Ubuntu 22.04 ships 3.10); install python3.11+ and re-run"
        exit 1
    fi
    log_ok "preflight passed (${arch}, python $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"
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
    if ! cmd_exists sudo; then
        log_err "need sudo to install:${missing}"
        log_err "ask an administrator to run: apt-get install -y${missing}"
        exit 1
    fi
    if ! sudo -n true 2>/dev/null && ! sudo -v; then
        log_err "could not obtain sudo to install:${missing}"
        log_err "ask an administrator to run: apt-get install -y${missing}"
        exit 1
    fi
    log "Installing apt packages:${missing}"
    # DPkg::Lock::Timeout rides out a fresh VM's unattended-upgrades holding the
    # dpkg lock instead of failing immediately.
    sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update
    # shellcheck disable=SC2086 # intentional word-splitting of the package list
    sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y ${missing}
    log_ok "apt dependencies installed"
}

install_tectonic() {
    local min="${TECTONIC_VERSION}" ver="" arch asset sha url
    # Prefer our own binary over any older system tectonic earlier on PATH.
    export PATH="${LOCAL_BIN}:${PATH}"

    if [ -x "${LOCAL_BIN}/tectonic" ]; then
        ver="$("${LOCAL_BIN}/tectonic" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
        if [ -n "${ver}" ] && version_ge "${ver}" "${min}"; then
            log_ok "tectonic ${ver} already present"
            ensure_local_bin_on_path
            return 0
        fi
        log "tectonic ${ver:-unknown} at ${LOCAL_BIN} is below ${min}; replacing it"
    fi

    arch="$(uname -m)"
    case "${arch}" in
        x86_64)        asset="tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz";  sha="${TECTONIC_SHA_X86_64}" ;;
        aarch64|arm64) asset="tectonic-${TECTONIC_VERSION}-aarch64-unknown-linux-musl.tar.gz"; sha="${TECTONIC_SHA_AARCH64}" ;;
        *) log_err "no prebuilt tectonic musl binary for CPU architecture '${arch}'"; exit 1 ;;
    esac
    url="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/${asset}"

    mkdir -p "${LOCAL_BIN}"
    local tmpdir archive
    tmpdir="$(mktemp -d)"
    # Clean the scratch dir up whichever way this function leaves.
    trap 'rm -rf "${tmpdir}"' RETURN
    archive="${tmpdir}/${asset}"

    log "Downloading tectonic ${TECTONIC_VERSION} (${arch})"
    if ! curl --proto '=https' --tlsv1.2 -fsSL "${url}" -o "${archive}"; then
        log_err "could not download tectonic from ${url}; check your network"
        return 1
    fi
    if ! printf '%s  %s\n' "${sha}" "${archive}" | sha256sum -c - >/dev/null 2>&1; then
        log_err "tectonic download failed sha256 verification; refusing to install it"
        return 1
    fi
    if ! tar -xzf "${archive}" -C "${LOCAL_BIN}" tectonic; then
        log_err "could not extract the tectonic binary from ${asset}"
        return 1
    fi
    chmod +x "${LOCAL_BIN}/tectonic"
    ensure_local_bin_on_path

    if ! "${LOCAL_BIN}/tectonic" --version >/dev/null 2>&1; then
        log_err "tectonic was installed but will not run (a corrupt download or a missing system library?):"
        "${LOCAL_BIN}/tectonic" --version >&2 || true
        return 1
    fi
    ver="$("${LOCAL_BIN}/tectonic" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
    if [ -z "${ver}" ] || ! version_ge "${ver}" "${min}"; then
        log_err "tectonic ${ver:-unknown} is below the required ${min}"
        return 1
    fi
    log_ok "tectonic ${ver} installed"
    warm_tectonic_bundle
}

warm_tectonic_bundle() {
    # Opt-out for CI, where downloading the full TeX bundle on every run is a
    # waste; a real install should leave this unset.
    if [ "${CVFLOW_SKIP_TECTONIC_WARM:-0}" = "1" ]; then
        return 0
    fi
    local work
    work="$(mktemp -d)"
    printf '%s\n' '\documentclass{article}' '\begin{document}' 'hello' '\end{document}' > "${work}/hello.tex"
    log "Warming the tectonic bundle (the first compile fetches support files)"
    if ( cd "${work}" && tectonic -X compile hello.tex --outdir . >/dev/null 2>&1 ); then
        log_ok "tectonic bundle ready"
    else
        log_warn "could not warm the tectonic bundle (offline?); the first tailor will fetch it"
    fi
    rm -rf "${work}"
}

ensure_local_bin_on_path() {
    path_has_dir "${LOCAL_BIN}" || export PATH="${LOCAL_BIN}:${PATH}"
    local profile="${HOME}/.profile"
    # shellcheck disable=SC2016 # the literal $HOME is meant to land in .profile unexpanded
    local line='export PATH="$HOME/.local/bin:$PATH"'
    if ! file_has "${profile}" "${line}"; then
        printf '\n# added by cv-flow installer\n%s\n' "${line}" >> "${profile}"
        log_ok "added ~/.local/bin to PATH in ${profile}"
    fi
}

install_python_package() {
    # A half-built venv with no working pip is worse than none; rebuild it.
    if [ -d "${VENV_DIR}" ] \
       && ! { [ -x "${VENV_DIR}/bin/python" ] && "${VENV_DIR}/bin/python" -m pip --version >/dev/null 2>&1; }; then
        log_warn "virtualenv at ${VENV_DIR} is incomplete; recreating it"
        rm -rf "${VENV_DIR}"
    fi
    if [ ! -d "${VENV_DIR}" ]; then
        log "Creating virtualenv at ${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
        "${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip
    fi
    # Always install (a no-op when already satisfied) so a --ref bump that adds
    # a dependency actually pulls it in.
    log "Installing the cvflow package (editable)"
    "${VENV_DIR}/bin/python" -m pip install --quiet -e "${REPO_ROOT}" -c "${REPO_ROOT}/constraints.txt"
    log_ok "cvflow package installed"
}

secure_dirs() {
    # PII lives under a home that is often world-readable (755), so lock down
    # the directories that hold it.
    mkdir -p "${REPO_ROOT}/data"
    local d
    for d in "${REPO_ROOT}/data" "${REPO_ROOT}/profile"; do
        if [ -d "${d}" ]; then
            chmod 700 "${d}" 2>/dev/null || true
        fi
    done
}

write_config() {
    local cfg="${REPO_ROOT}/config.yaml" example="${REPO_ROOT}/config.example.yaml"

    if [ -f "${cfg}" ] && ! grep -q 'PASTE_YOUR_' "${cfg}"; then
        chmod 600 "${cfg}"
        log_ok "config.yaml already present (left untouched)"
        return 0
    fi
    if [ -f "${cfg}" ]; then
        log_warn "config.yaml still holds placeholder values; filling it in"
    fi

    local token key user_id
    if [ "${FLAG_NONINTERACTIVE}" = "1" ]; then
        token="$(trim_value "${CVFLOW_BOT_TOKEN:-PASTE_YOUR_BOT_TOKEN}")"
        key="$(trim_value "${CVFLOW_LLM_KEY:-PASTE_YOUR_MISTRAL_KEY}")"
        user_id="$(trim_value "${CVFLOW_USER_ID:-}")"
        case "${user_id}" in ''|*[!0-9]*) user_id="" ;; esac
        write_config_file "${example}" "${cfg}" "${token}" "${key}" "${user_id}"
        if [ "${token}" = "PASTE_YOUR_BOT_TOKEN" ] || [ "${key}" = "PASTE_YOUR_MISTRAL_KEY" ]; then
            log_warn "config.yaml written with placeholders; edit it before running the bot"
        else
            log_ok "config.yaml written from environment values"
        fi
        return 0
    fi

    token="$(prompt_for_token)"
    key="$(prompt_for_llm_key)"
    user_id="$(prompt_for_user_id)" || user_id=""
    write_config_file "${example}" "${cfg}" "${token}" "${key}" "${user_id}"
    log_ok "config.yaml written (chmod 600)"
}

# Build the config in a sibling temp file with a restrictive umask, then move it
# into place, so there is never a world-readable or half-written config.yaml for
# a concurrent run to mistake as complete.
write_config_file() {
    local example="$1" out="$2" token="$3" key="$4" user_id="$5"
    local dir tmp
    dir="$(dirname "${out}")"
    tmp="$(umask 077; mktemp "${dir}/.cvflow-config.XXXXXX")"
    if ! inject_config "${example}" "${tmp}" "${token}" "${key}" "${user_id}"; then
        rm -f "${tmp}"
        log_err "refusing to write config.yaml"
        exit 1
    fi
    chmod 600 "${tmp}"
    mv -f "${tmp}" "${out}"
}

prompt_for_token() {
    local token attempts=0 rc
    while [ "${attempts}" -lt 5 ]; do
        attempts=$((attempts + 1))
        token="$(prompt_secret 'Telegram bot token (from @BotFather): ')" || return 1
        token="$(trim_value "${token}")"
        if [ -z "${token}" ]; then
            log_err "empty value; try again" >/dev/tty
            continue
        fi
        if [ "${CVFLOW_SKIP_VALIDATION:-0}" = "1" ]; then
            printf '%s' "${token}"; return 0
        fi
        rc=0; validate_telegram_token "${token}" || rc=$?
        case "${rc}" in
            0) log_ok "Telegram token accepted" >/dev/tty; printf '%s' "${token}"; return 0 ;;
            2) log_warn "couldn't reach Telegram to validate; check your network"
               if confirm_continue_unvalidated; then printf '%s' "${token}"; return 0; fi ;;
            *) log_err "Telegram rejected that token; try again" >/dev/tty ;;
        esac
    done
    log_err "giving up after 5 attempts; re-run once you have a working token" >/dev/tty
    return 1
}

prompt_for_llm_key() {
    local key attempts=0 rc
    while [ "${attempts}" -lt 5 ]; do
        attempts=$((attempts + 1))
        key="$(prompt_secret 'Mistral API key: ')" || return 1
        key="$(trim_value "${key}")"
        if [ -z "${key}" ]; then
            log_err "empty value; try again" >/dev/tty
            continue
        fi
        if [ "${CVFLOW_SKIP_VALIDATION:-0}" = "1" ]; then
            printf '%s' "${key}"; return 0
        fi
        rc=0; validate_llm_key "${LLM_BASE_URL}" "${key}" || rc=$?
        case "${rc}" in
            0) log_ok "Mistral key accepted" >/dev/tty; printf '%s' "${key}"; return 0 ;;
            2) log_warn "couldn't reach Mistral to validate; check your network"
               if confirm_continue_unvalidated; then printf '%s' "${key}"; return 0; fi ;;
            *) log_err "Mistral rejected that key; try again" >/dev/tty ;;
        esac
    done
    log_err "giving up after 5 attempts; re-run once you have a working key" >/dev/tty
    return 1
}

prompt_for_user_id() {
    local id attempts=0
    while [ "${attempts}" -lt 5 ]; do
        attempts=$((attempts + 1))
        id="$(prompt_line 'Your numeric Telegram user id (message @userinfobot to get it): ')" || return 1
        id="$(trim_value "${id}")"
        case "${id}" in
            ''|*[!0-9]*) log_err "that is not a numeric id; try again" >/dev/tty ;;
            0)           log_err "0 is not a valid id; try again" >/dev/tty ;;
            *)           printf '%s' "${id}"; return 0 ;;
        esac
    done
    log_warn "no user id entered; leaving authorized_user_id at 0 (set it in config.yaml before running the bot)"
    return 1
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

    local user uid
    user="$(id -un)"
    uid="$(id -u)"
    : "${XDG_RUNTIME_DIR:=/run/user/${uid}}"
    export XDG_RUNTIME_DIR

    systemctl --user daemon-reload || log_warn "systemctl --user daemon-reload failed"

    # Enable for the next login, but do NOT start it now: the bot needs an
    # onboarded profile and a filled-in config, so starting it here would make
    # it crash-loop every RestartSec until the user runs onboard.
    systemctl --user enable cvflow.service \
        || log_warn "could not enable the user service; run: systemctl --user enable cvflow.service"

    # Lingering keeps the user manager (and the bot) alive after logout.
    if [ "$(loginctl show-user "${user}" -p Linger --value 2>/dev/null)" != "yes" ]; then
        if loginctl enable-linger "${user}" 2>/dev/null; then
            log_ok "enabled lingering for ${user}"
        else
            log_warn "lingering is OFF for ${user}; the bot would stop when you log out."
            log_warn "enable it once with: sudo loginctl enable-linger ${user}"
        fi
    fi

    log "Once you have onboarded, start the bot with: systemctl --user start cvflow.service"
    log "Watch it with: journalctl --user -u cvflow.service -f"
}

service_unit_text() {
    cat <<EOF
[Unit]
Description=cv-flow job digest bot

[Service]
Type=simple
WorkingDirectory=${REPO_ROOT}
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart="${VENV_DIR}/bin/python" -m cvflow.app
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

# Preserve the original argument list so checkout_ref can re-exec faithfully.
CVFLOW_REEXEC_ARGS=("$@")
main "$@"
