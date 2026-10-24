#!/usr/bin/env bats
#
# Unit tests for scripts/install-lib.sh. Sourcing the library must have no side
# effects, so every test loads it and then calls one function in isolation.

setup() {
    LIB="${BATS_TEST_DIRNAME}/../install-lib.sh"
    # shellcheck source=scripts/install-lib.sh
    . "${LIB}"
    STUBDIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUBDIR}"
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

# --- validation helpers (curl stubbed) -------------------------------------

@test "validate_telegram_token accepts an ok response" {
    cat > "${STUBDIR}/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s' '{"ok":true,"result":{"id":1}}'
EOF
    chmod +x "${STUBDIR}/curl"
    PATH="${STUBDIR}:${PATH}" validate_telegram_token "any-token"
}

@test "validate_telegram_token rejects a not-ok response" {
    cat > "${STUBDIR}/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s' '{"ok":false,"error_code":401}'
EOF
    chmod +x "${STUBDIR}/curl"
    run env PATH="${STUBDIR}:${PATH}" bash -c '. "'"${LIB}"'"; validate_telegram_token bad'
    [ "${status}" -ne 0 ]
}

@test "validate_llm_key accepts http 200" {
    cat > "${STUBDIR}/curl" <<'EOF'
#!/usr/bin/env bash
printf '200'
EOF
    chmod +x "${STUBDIR}/curl"
    PATH="${STUBDIR}:${PATH}" validate_llm_key "https://api.mistral.ai/v1" "good-key"
}

@test "validate_llm_key rejects http 401" {
    cat > "${STUBDIR}/curl" <<'EOF'
#!/usr/bin/env bash
printf '401'
EOF
    chmod +x "${STUBDIR}/curl"
    run env PATH="${STUBDIR}:${PATH}" bash -c '. "'"${LIB}"'"; validate_llm_key https://api.mistral.ai/v1 bad'
    [ "${status}" -ne 0 ]
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
