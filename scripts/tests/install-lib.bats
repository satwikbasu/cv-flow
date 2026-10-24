#!/usr/bin/env bats
#
# Unit tests for scripts/install-lib.sh and the pure helpers in bootstrap.sh.
# Sourcing either must have no side effects, so every test loads it and then
# calls one function in isolation.

setup() {
    LIB="${BATS_TEST_DIRNAME}/../install-lib.sh"
    BOOT="${BATS_TEST_DIRNAME}/../bootstrap.sh"
    # shellcheck source=scripts/install-lib.sh
    . "${LIB}"
    STUBDIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUBDIR}"
}

stub_curl() {
    cat > "${STUBDIR}/curl"
    chmod +x "${STUBDIR}/curl"
}

# --- flag parsing ----------------------------------------------------------

@test "parse_install_args defaults to interactive with services" {
    parse_install_args
    [ "${FLAG_NONINTERACTIVE}" = "0" ]
    [ "${FLAG_NO_SERVICES}" = "0" ]
    [ "${FLAG_REF}" = "" ]
}

@test "parse_install_args reads all flags" {
    parse_install_args --non-interactive --no-services --ref v1.2.3
    [ "${FLAG_NONINTERACTIVE}" = "1" ]
    [ "${FLAG_NO_SERVICES}" = "1" ]
    [ "${FLAG_REF}" = "v1.2.3" ]
}

@test "parse_install_args accepts --ref=value form" {
    parse_install_args --ref=main
    [ "${FLAG_REF}" = "main" ]
}

@test "parse_install_args rejects unknown flags" {
    run parse_install_args --bogus
    [ "${status}" -eq 2 ]
}

@test "parse_install_args rejects a trailing --ref with no value" {
    run parse_install_args --no-services --ref
    [ "${status}" -eq 2 ]
}

@test "parse_install_args rejects a --ref value that looks like an option" {
    run parse_install_args --ref --no-services
    [ "${status}" -eq 2 ]
}

@test "parse_install_args prints usage for --help and returns 10" {
    run parse_install_args --help
    [ "${status}" -eq 10 ]
    [[ "${output}" == *"Usage:"* ]]
}

# --- config injection ------------------------------------------------------

@test "inject_config replaces both placeholders" {
    example="${BATS_TEST_TMPDIR}/example.yaml"
    out="${BATS_TEST_TMPDIR}/config.yaml"
    cat > "${example}" <<'EOF'
telegram:
  bot_token: "PASTE_YOUR_BOT_TOKEN"
llm:
  distillation:
    api_key: "PASTE_YOUR_MISTRAL_KEY"
  tailoring:
    api_key: "PASTE_YOUR_MISTRAL_KEY"
EOF
    inject_config "${example}" "${out}" "123:abc" "sk-test-key"
    grep -q 'bot_token: "123:abc"' "${out}"
    [ "$(grep -c 'api_key: "sk-test-key"' "${out}")" = "2" ]
    ! grep -q PASTE_YOUR "${out}"
}

@test "inject_config keeps tokens with slashes and colons verbatim" {
    example="${BATS_TEST_TMPDIR}/example.yaml"
    out="${BATS_TEST_TMPDIR}/config.yaml"
    printf 'bot_token: "PASTE_YOUR_BOT_TOKEN"\n' > "${example}"
    inject_config "${example}" "${out}" '99:A/b+c=d' 'unused'
    grep -qF 'bot_token: "99:A/b+c=d"' "${out}"
}

@test "inject_config keeps an ampersand literal (patsub quoting)" {
    example="${BATS_TEST_TMPDIR}/example.yaml"
    out="${BATS_TEST_TMPDIR}/config.yaml"
    printf 'bot_token: "PASTE_YOUR_BOT_TOKEN"\n' > "${example}"
    inject_config "${example}" "${out}" '9:x&y' 'unused'
    grep -qF 'bot_token: "9:x&y"' "${out}"
}

@test "inject_config writes the authorized user id when given" {
    example="${BATS_TEST_TMPDIR}/example.yaml"
    out="${BATS_TEST_TMPDIR}/config.yaml"
    printf '  authorized_user_id: 0                # comment\n' > "${example}"
    inject_config "${example}" "${out}" tok key 12345
    grep -qF 'authorized_user_id: 12345' "${out}"
    grep -qF '# comment' "${out}"
}

@test "inject_config rejects a value with a backslash" {
    example="${BATS_TEST_TMPDIR}/example.yaml"
    out="${BATS_TEST_TMPDIR}/config.yaml"
    printf 'bot_token: "PASTE_YOUR_BOT_TOKEN"\n' > "${example}"
    run inject_config "${example}" "${out}" '9:bad\back' 'unused'
    [ "${status}" -ne 0 ]
}

@test "inject_config rejects a value with a double quote" {
    example="${BATS_TEST_TMPDIR}/example.yaml"
    out="${BATS_TEST_TMPDIR}/config.yaml"
    printf 'bot_token: "PASTE_YOUR_BOT_TOKEN"\n' > "${example}"
    run inject_config "${example}" "${out}" '9:qu"ote' 'unused'
    [ "${status}" -ne 0 ]
}

@test "inject_config rejects a value with a newline" {
    example="${BATS_TEST_TMPDIR}/example.yaml"
    out="${BATS_TEST_TMPDIR}/config.yaml"
    printf 'bot_token: "PASTE_YOUR_BOT_TOKEN"\n' > "${example}"
    run inject_config "${example}" "${out}" "$(printf '9:a\nb')" 'unused'
    [ "${status}" -ne 0 ]
}

# --- value hygiene ---------------------------------------------------------

@test "trim_value strips surrounding whitespace and a trailing CR" {
    [ "$(trim_value "  hello  ")" = "hello" ]
    [ "$(trim_value "$(printf 'tok\r')")" = "tok" ]
}

# --- idempotency checks ----------------------------------------------------

@test "cmd_exists finds present and absent commands" {
    cmd_exists sh
    ! cmd_exists definitely-not-a-real-command-xyz
}

@test "file_has detects a present line and a missing one" {
    f="${BATS_TEST_TMPDIR}/f.txt"
    printf 'alpha\nbeta\n' > "${f}"
    file_has "${f}" "beta"
    ! file_has "${f}" "gamma"
    ! file_has "${BATS_TEST_TMPDIR}/missing.txt" "beta"
}

@test "path_has_dir reflects PATH membership" {
    PATH="/opt/one:/opt/two" path_has_dir /opt/two
    ! PATH="/opt/one:/opt/two" path_has_dir /opt/three
}

@test "version_ge compares dotted versions" {
    version_ge 0.17.0 0.17.0
    version_ge 0.18.0 0.17.0
    version_ge 1.0.0 0.17.0
    ! version_ge 0.16.9 0.17.0
}

# --- /dev/tty prompts ------------------------------------------------------
# setsid -w runs the function in a fresh session with no controlling terminal,
# so the /dev/tty redirect fails and the prompt must return non-zero rather
# than block forever.

@test "prompt_line returns non-zero with no controlling terminal" {
    run setsid -w bash -c '. "'"${LIB}"'"; prompt_line "name: "'
    [ "${status}" -ne 0 ]
}

@test "prompt_secret returns non-zero with no controlling terminal" {
    run setsid -w bash -c '. "'"${LIB}"'"; prompt_secret "secret: "'
    [ "${status}" -ne 0 ]
}

# --- validation helpers (curl stubbed) -------------------------------------

@test "validate_telegram_token accepts an ok response" {
    stub_curl <<'EOF'
#!/usr/bin/env bash
printf '%s' '{"ok":true,"result":{"id":1}}'
EOF
    PATH="${STUBDIR}:${PATH}" validate_telegram_token "any-token"
}

@test "validate_telegram_token rejects a not-ok response with code 1" {
    stub_curl <<'EOF'
#!/usr/bin/env bash
printf '%s' '{"ok":false,"error_code":401}'
EOF
    run env PATH="${STUBDIR}:${PATH}" bash -c '. "'"${LIB}"'"; validate_telegram_token bad'
    [ "${status}" -eq 1 ]
}

@test "validate_telegram_token reports a 5xx as unreachable (code 2)" {
    stub_curl <<'EOF'
#!/usr/bin/env bash
printf '%s\n%s' '{"ok":false}' '503'
EOF
    run env PATH="${STUBDIR}:${PATH}" bash -c '. "'"${LIB}"'"; validate_telegram_token tok'
    [ "${status}" -eq 2 ]
}

@test "validate_telegram_token reports a transport failure as unreachable (code 2)" {
    stub_curl <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
    run env PATH="${STUBDIR}:${PATH}" bash -c '. "'"${LIB}"'"; validate_telegram_token tok'
    [ "${status}" -eq 2 ]
}

@test "validate_llm_key accepts http 200" {
    stub_curl <<'EOF'
#!/usr/bin/env bash
printf '200'
EOF
    PATH="${STUBDIR}:${PATH}" validate_llm_key "https://api.mistral.ai/v1" "good-key"
}

@test "validate_llm_key rejects http 401 with code 1" {
    stub_curl <<'EOF'
#!/usr/bin/env bash
printf '401'
EOF
    run env PATH="${STUBDIR}:${PATH}" bash -c '. "'"${LIB}"'"; validate_llm_key https://api.mistral.ai/v1 bad'
    [ "${status}" -eq 1 ]
}

@test "validate_llm_key reports http 000 as unreachable (code 2)" {
    stub_curl <<'EOF'
#!/usr/bin/env bash
printf '000'
EOF
    run env PATH="${STUBDIR}:${PATH}" bash -c '. "'"${LIB}"'"; validate_llm_key https://api.mistral.ai/v1 key'
    [ "${status}" -eq 2 ]
}

# --- logging ---------------------------------------------------------------

@test "log_ok uses the checkmark glyph" {
    run log_ok "done"
    [ "${status}" -eq 0 ]
    [ "${output}" = "✓ done" ]
}

@test "log_err uses the ballot-x glyph on stderr" {
    run log_err "nope"
    [ "${output}" = "✗ nope" ]
}

# --- bootstrap.sh pure helpers ---------------------------------------------

@test "select_latest_tag picks the highest final release tag" {
    run bash -c '. "'"${BOOT}"'"; printf "%s\n" v0.1.0 v0.2.0 v0.10.0 v0.3.0 | select_latest_tag'
    [ "${output}" = "v0.10.0" ]
}

@test "select_latest_tag ignores pre-release tags" {
    run bash -c '. "'"${BOOT}"'"; printf "%s\n" v0.2.0 v0.2.0-rc1 v0.2.0-beta | select_latest_tag'
    [ "${output}" = "v0.2.0" ]
}

@test "select_latest_tag yields nothing when there are no final tags" {
    run bash -c '. "'"${BOOT}"'"; printf "%s\n" v0.2.0-rc1 nightly | select_latest_tag'
    [ -z "${output}" ]
}

@test "parse_ref extracts both --ref forms and is empty otherwise" {
    run bash -c '. "'"${BOOT}"'"; parse_ref --no-services --ref v1.2.0'
    [ "${output}" = "v1.2.0" ]
    run bash -c '. "'"${BOOT}"'"; parse_ref --ref=v3.4.5 --no-services'
    [ "${output}" = "v3.4.5" ]
    run bash -c '. "'"${BOOT}"'"; parse_ref --no-services'
    [ -z "${output}" ]
}
