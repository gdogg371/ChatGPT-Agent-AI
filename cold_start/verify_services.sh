#!/bin/bash

# bootstrap/verify_services.sh
# AG35c — Verifies services and shell environment, logs recovery status

LOG_FILE="/logs/agent_startup.log"
RECOVERY_LOG="/logs/recovery.log"
SANDBOX_SCRIPT="/scripts/sandbox_validate.sh"
EXIT_CODE=0

log() {
    echo "[verify_services] $1" | tee -a "$LOG_FILE"
}

log "=== Verifying system services and runtime environment ==="

# === Check if log_uploader.service is active ===
if systemctl is-active --quiet log_uploader.service; then
    log "✅ log_uploader.service is running"
else
    log "❌ log_uploader.service is NOT running"
    EXIT_CODE=1
    echo "[verify_services] log_uploader.service failure" >> "$RECOVERY_LOG"
fi

# === Check if watchdog.service is active ===
if systemctl is-active --quiet watchdog.service; then
    log "✅ watchdog.service is running"
else
    log "❌ watchdog.service is NOT running"
    EXIT_CODE=1
    echo "[verify_services] watchdog.service failure" >> "$RECOVERY_LOG"
fi

# === Validate sandbox shell environment ===
if [[ -x "$SANDBOX_SCRIPT" ]]; then
    log "📦 Running sandbox validation..."
    bash "$SANDBOX_SCRIPT"
    if [[ $? -ne 0 ]]; then
        log "❌ Sandbox validation FAILED"
        EXIT_CODE=1
        echo "[verify_services] Sandbox environment invalid" >> "$RECOVERY_LOG"
    else
        log "✅ Sandbox environment is valid"
    fi
else
    log "⚠️ Sandbox validator script not found or not executable: $SANDBOX_SCRIPT"
    EXIT_CODE=1
    echo "[verify_services] Sandbox script missing" >> "$RECOVERY_LOG"
fi

# === Final outcome ===
if [[ $EXIT_CODE -eq 0 ]]; then
    log "✅ All critical services and runtime conditions validated"
else
    log "⚠️  One or more startup checks failed (exit: $EXIT_CODE)"
    echo "[verify_services] Boot degraded. Exit code: $EXIT_CODE" >> "$RECOVERY_LOG"
fi

exit $EXIT_CODE


