#!/bin/bash

# scripts/sandbox_validate.sh
# AG35b — Shell Environment Sandbox Validator
# Validates minimal shell capabilities and sandbox integrity at boot.

LOG_FILE="/logs/agent_startup.log"
EXIT_CODE=0

log() {
    echo "[sandbox_validate] $1" | tee -a "$LOG_FILE"
}

log "Starting sandbox environment validation..."

# === Test 1: Check for basic shell utilities ===
BINS=("bash" "sh" "grep" "awk" "sed" "cat" "ls" "echo" "mkdir")
for bin in "${BINS[@]}"; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        log "❌ Missing essential binary: $bin"
        EXIT_CODE=1
    else
        log "✅ Found: $bin"
    fi
done

# === Test 2: Test filesystem write ===
TMP_TEST="/tmp/sandbox_write_test"
echo "sandbox test" > "$TMP_TEST" 2>/dev/null
if [[ $? -ne 0 || ! -f "$TMP_TEST" ]]; then
    log "❌ Filesystem write failed at /tmp"
    EXIT_CODE=1
else
    log "✅ Filesystem write to /tmp successful"
    rm -f "$TMP_TEST"
fi

# === Test 3: Check environment variables ===
REQUIRED_VARS=("HOME" "USER" "PATH")
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var}" ]]; then
        log "❌ Missing environment variable: $var"
        EXIT_CODE=1
    else
        log "✅ $var = ${!var}"
    fi
done

# === Test 4: Run a safe subprocess ===
OUTPUT=$(echo "sandbox" | grep "box")
if [[ "$OUTPUT" != "sandbox" ]]; then
    log "❌ Subprocess execution failed"
    EXIT_CODE=1
else
    log "✅ Subprocess execution succeeded"
fi

# === Final Result ===
if [[ $EXIT_CODE -eq 0 ]]; then
    log "✅ Sandbox environment validated successfully"
else
    log "⚠️  Sandbox environment failed validation (code: $EXIT_CODE)"
fi

exit $EXIT_CODE
