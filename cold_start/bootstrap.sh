#!/bin/bash

# bootstrap/bootstrap.sh
# AG35d — Hardened Boot Logic with Retry Counter and Safe Mode Fallback

LOG_DIR="/logs"
SCRIPT_DIR="/scripts"
SERVICE_DIR="/etc/systemd/system"
RECOVERY_DIR="/recovery"

LOG_FILE="$LOG_DIR/agent_startup.log"
RECOVERY_LOG="$LOG_DIR/recovery.log"
BOOT_FLAG="$RECOVERY_DIR/.last_boot_failed"
BOOT_STATE="$RECOVERY_DIR/boot_state.json"

mkdir -p "$LOG_DIR"
mkdir -p "$SCRIPT_DIR"
mkdir -p "$RECOVERY_DIR"
touch "$LOG_FILE"
touch "$RECOVERY_LOG"

log() {
    echo "[bootstrap] $1" | tee -a "$LOG_FILE"
}

log "Starting system bootstrap..."

# === Mark boot as failed initially ===
touch "$BOOT_FLAG"
echo "[bootstrap] Created boot failure flag" >> "$RECOVERY_LOG"

# === Copy and enable log_uploader.service ===
if [[ -f "$(dirname "$0")/log_uploader.service" ]]; then
    cp "$(dirname "$0")/log_uploader.service" "$SERVICE_DIR/"
    systemctl daemon-reexec
    systemctl daemon-reload
    systemctl enable log_uploader.service
    systemctl start log_uploader.service
    log "✅ log_uploader.service installed and started"
else
    log "❌ log_uploader.service not found"
    echo "[bootstrap] log_uploader.service missing" >> "$RECOVERY_LOG"
fi

# === Copy and enable watchdog.service ===
if [[ -f "$(dirname "$0")/watchdog.service" ]]; then
    cp "$(dirname "$0")/watchdog.service" "$SERVICE_DIR/"
    systemctl daemon-reload
    systemctl enable watchdog.service
    systemctl start watchdog.service
    log "✅ watchdog.service installed and started"
else
    log "❌ watchdog.service not found"
    echo "[bootstrap] watchdog.service missing" >> "$RECOVERY_LOG"
fi

# === Run post-boot verifications (services + sandbox) ===
if [[ -x "$(dirname "$0")/verify_services.sh" ]]; then
    bash "$(dirname "$0")/verify_services.sh"
    if [[ $? -eq 0 ]]; then
        rm -f "$BOOT_FLAG"
        echo "[bootstrap] Boot success — removed failure flag" >> "$RECOVERY_LOG"
    else
        echo "[bootstrap] Boot verification failed — flag remains" >> "$RECOVERY_LOG"
        # === Increment retry count and check for safe mode transition ===
        python3 -c 'from backend.core.context import increment_retry_counter; increment_retry_counter()'
    fi
else
    log "⚠️ verify_services.sh missing or not executable"
    echo "[bootstrap] verify_services.sh missing — degraded boot" >> "$RECOVERY_LOG"
    python3 -c 'from backend.core.context import increment_retry_counter; increment_retry_counter()'
fi

log "Bootstrap completed at $(date)"
