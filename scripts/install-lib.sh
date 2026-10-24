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

print_usage() {
    cat <<'EOF'
Usage: scripts/install.sh [--non-interactive] [--no-services] [--ref <git-ref>]

  --non-interactive  never prompt; take the bot token, LLM key and user id from
                     CVFLOW_BOT_TOKEN / CVFLOW_LLM_KEY / CVFLOW_USER_ID if set,
                     otherwise leave config.yaml with placeholders.
  --no-services      skip installing the systemd unit.
  --ref <git-ref>    check the repo out at this ref before installing.
  -h, --help         show this help and exit.
EOF
}

# Returns 10 after printing help, 2 on a bad argument, 0 otherwise.
parse_install_args() {
    FLAG_NONINTERACTIVE=0
    FLAG_NO_SERVICES=0
    FLAG_REF=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --non-interactive) FLAG_NONINTERACTIVE=1 ;;
            --no-services)     FLAG_NO_SERVICES=1 ;;
            -h|--help)         print_usage; return 10 ;;
            --ref)
                if [ $# -lt 2 ]; then
                    log_err "--ref needs a git ref argument"; return 2
                fi
                case "$2" in
                    -*) log_err "--ref needs a git ref, got the option '$2'"; return 2 ;;
                esac
                FLAG_REF="$2"; shift ;;
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
# dpkg-query is used rather than `dpkg -s` because the latter still reports
# success for a package that was removed but left its config behind.
apt_installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }
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

# --- value hygiene ---------------------------------------------------------
# Trim surrounding whitespace (including a trailing CR from a pasted value) and
# reject anything that would break the double-quoted YAML string it lands in.

trim_value() {
    local v="$1"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    printf '%s' "$v"
}

secret_is_safe() {
    case "$1" in
        *\"*)    return 1 ;;   # double quote
        *\\*)    return 1 ;;   # backslash
        *$'\n'*) return 1 ;;   # newline
    esac
    return 0
}

# --- /dev/tty prompts ------------------------------------------------------
# Read straight from the controlling terminal so prompts work even when the
# installer is driven through a pipe. The entered value is echoed to stdout.
# Both return non-zero when there is no controlling terminal to read from.

prompt_line() {
    local reply
    { printf '%s' "$1" > /dev/tty; } 2>/dev/null || return 1
    IFS= read -r reply < /dev/tty || return 1
    printf '%s' "$reply"
}

prompt_secret() {
    local reply
    { printf '%s' "$1" > /dev/tty; } 2>/dev/null || return 1
    IFS= read -rs reply < /dev/tty || return 1
    printf '\n' > /dev/tty
    printf '%s' "$reply"
}

# Ask on the terminal whether to proceed with a credential we could not reach
# the vendor to validate. Returns 0 only on an explicit yes.
confirm_continue_unvalidated() {
    local reply
    reply="$(prompt_line 'Continue without validating this credential? [y/N]: ')" || return 1
    case "$(trim_value "${reply}")" in
        y|Y|yes|YES) return 0 ;;
        *)           return 1 ;;
    esac
}

# --- config injection ------------------------------------------------------
# Copy the committed skeleton to the target, substituting the placeholder
# values. bash string replacement is used instead of sed so tokens containing
# slashes, colons or other metacharacters are handled verbatim. The replacement
# is quoted so an `&` or `\` in a token is kept literal (unquoted, bash's patsub
# treats `&` as the whole match and mangles backslashes). A value carrying a
# quote, backslash or newline is refused rather than written into broken YAML.

inject_config() {
    local example="$1" out="$2" bot_token="$3" llm_key="$4" user_id="${5:-}" line
    if ! secret_is_safe "${bot_token}"; then
        log_err "the bot token contains a quote, backslash or newline; cannot write it safely"
        return 1
    fi
    if ! secret_is_safe "${llm_key}"; then
        log_err "the LLM key contains a quote, backslash or newline; cannot write it safely"
        return 1
    fi
    : > "$out"
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line//PASTE_YOUR_BOT_TOKEN/"$bot_token"}"
        line="${line//PASTE_YOUR_MISTRAL_KEY/"$llm_key"}"
        if [ -n "${user_id}" ]; then
            line="${line//authorized_user_id: 0/authorized_user_id: "$user_id"}"
        fi
        printf '%s\n' "$line" >> "$out"
    done < "$example"
}

# --- live key validation ---------------------------------------------------
# Both hit the vendor's own endpoint with the supplied credential. They stay
# free: a getMe / list-models call costs nothing. Return codes:
#   0  the credential works
#   1  the vendor rejected it (try another)
#   2  could not reach the vendor (transport error, HTTP 000/5xx/429)

validate_telegram_token() {
    # The token sits in the URL path by Telegram's API design, so it is visible
    # to `ps` for the life of this short call; there is no header form for it.
    local token="$1" out code body
    out="$(curl -s -w '\n%{http_code}' --max-time 15 \
        "https://api.telegram.org/bot${token}/getMe" 2>/dev/null)" || return 2
    code="${out##*$'\n'}"
    body="${out%$'\n'*}"
    case "$code" in
        000|5[0-9][0-9]|429) return 2 ;;
    esac
    case "$body" in
        *'"ok":true'*) return 0 ;;
        *)             return 1 ;;
    esac
}

validate_llm_key() {
    local base_url="$1" api_key="$2" code
    # Pass the key through a process-substitution file so it never appears in
    # this process's argv (curl -H @file reads the header from the file).
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        -H @<(printf 'Authorization: Bearer %s' "$api_key") \
        "${base_url%/}/models" 2>/dev/null)" || return 2
    case "$code" in
        200)                 return 0 ;;
        000|5[0-9][0-9]|429) return 2 ;;
        *)                   return 1 ;;
    esac
}
