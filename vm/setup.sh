#!/usr/bin/env bash
#
# Install the daily data-refresh trigger on a Linux VM (e.g. the GCP e2-micro
# that already runs the alerter). A systemd timer fires the trigger once after
# market close (Mon-Fri 16:05 IST); the trigger dispatches the update-daily
# workflow, waits for it, retries, and alerts via Telegram on failure.
#
# Prereqs on the VM:
#   - this repo cloned/copied here
#   - vm/.env filled in (copy from vm/.env.example)
#
# Then run:  bash vm/setup.sh
#
# Stdlib-only trigger: uses the system python3, no venv needed.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
ENV_FILE="$REPO_DIR/vm/.env"
PY="$(command -v python3)"

echo "repo:   $REPO_DIR"
echo "user:   $RUN_USER"
echo "python: $PY"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found. Create it first (cp vm/.env.example vm/.env)." >&2
    exit 1
fi

# Timezone so OnCalendar + logs read in IST (harmless if already set).
sudo timedatectl set-timezone Asia/Kolkata

sudo tee /etc/systemd/system/nse-update.service >/dev/null <<UNIT
[Unit]
Description=NSE screener daily data refresh (dispatch update-daily workflow)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY $REPO_DIR/vm/trigger_daily.py
UNIT

sudo tee /etc/systemd/system/nse-update.timer >/dev/null <<'UNIT'
[Unit]
Description=Fire the NSE daily refresh after close (Mon-Fri 16:05 IST)

[Timer]
OnCalendar=Mon-Fri 16:05 Asia/Kolkata
# If the VM was off at 16:05, run as soon as it's back up (don't miss a day).
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now nse-update.timer

echo
echo "Installed. Next run:"
systemctl list-timers 'nse-update*' --no-pager || true
echo
echo "Test the trigger now:   sudo systemctl start nse-update.service && journalctl -u nse-update -f"
