# shellcheck shell=bash
#
# Helper functions for the cv-flow installer. Sourcing this file defines
# functions only and has no side effects, so it can be exercised directly by
# the bats suite in scripts/tests/.

# --- logging ---------------------------------------------------------------
# Success and failure lines use the same glyphs as cvflow.checkup so the whole
# install reads consistently. Failures go to stderr.

log()      { printf '%s\n' "$*"; }
log_ok()   { printf '\xe2\x9c\x93 %s\n' "$*"; }        # checkmark
log_warn() { printf '%s\n' "$*" >&2; }
log_err()  { printf '\xe2\x9c\x97 %s\n' "$*" >&2; }    # ballot x

# --- flag parsing ----------------------------------------------------------
# Sets FLAG_NONINTERACTIVE, FLAG_NO_SERVICES and FLAG_REF. Exported so a
# standalone shellcheck of this file does not read them as unused.
export FLAG_NONINTERACTIVE FLAG_NO_SERVICES FLAG_REF

parse_install_args() {
    FLAG_NONINTERACTIVE=0
    FLAG_NO_SERVICES=0
    FLAG_REF=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --non-interactive) FLAG_NONINTERACTIVE=1 ;;
            --no-services)     FLAG_NO_SERVICES=1 ;;
            --ref)             shift; FLAG_REF="${1:-}" ;;
            --ref=*)           FLAG_REF="${1#--ref=}" ;;
            *) log_err "unknown option: $1"; return 2 ;;
        esac
        shift
    done
}

# --- idempotency checks ----------------------------------------------------
# Each is a pure query: it reports current state and changes nothing, so the
# installer can decide whether a step still has work to do.

cmd_exists()    { command -v "$1" >/dev/null 2>&1; }
apt_installed() { dpkg -s "$1" >/dev/null 2>&1; }
file_has()      { [ -f "$1" ] && grep -qF -- "$2" "$1"; }

path_has_dir() {
    case ":${PATH}:" in
        *":$1:"*) return 0 ;;
        *)        return 1 ;;
    esac
}

# version_ge A B -> true when A >= B (dotted version strings).
version_ge() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

# --- /dev/tty prompts ------------------------------------------------------
# Read straight from the controlling terminal so prompts work even when the
# installer is driven through a pipe. The entered value is echoed to stdout.

prompt_line() {
    local reply
    printf '%s' "$1" > /dev/tty
    IFS= read -r reply < /dev/tty
    printf '%s' "$reply"
}

prompt_secret() {
    local reply
    printf '%s' "$1" > /dev/tty
    IFS= read -rs reply < /dev/tty
    printf '\n' > /dev/tty
    printf '%s' "$reply"
}

# --- config injection ------------------------------------------------------
# Copy the committed skeleton to the target, substituting the two placeholder
# values. bash string replacement is used instead of sed so tokens containing
# slashes, colons or other metacharacters are handled verbatim.

inject_config() {
    local example="$1" out="$2" bot_token="$3" llm_key="$4" line
    : > "$out"
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line//PASTE_YOUR_BOT_TOKEN/$bot_token}"
        line="${line//PASTE_YOUR_MISTRAL_KEY/$llm_key}"
        printf '%s\n' "$line" >> "$out"
    done < "$example"
}

# --- live key validation ---------------------------------------------------
# Both hit the vendor's own endpoint with the supplied credential and report
# whether it is accepted. They stay free: a getMe / list-models call costs
# nothing. Return 0 when the key works, non-zero otherwise.

validate_telegram_token() {
    local token="$1" body
    body="$(curl -s --max-time 15 "https://api.telegram.org/bot${token}/getMe")" || return 1
    case "$body" in
        *'"ok":true'*) return 0 ;;
        *)             return 1 ;;
    esac
}

validate_llm_key() {
    local base_url="$1" api_key="$2" code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        -H "Authorization: Bearer ${api_key}" \
        "${base_url%/}/models")" || return 1
    [ "$code" = "200" ]
}
