#!/usr/bin/env bash
#
# Install the options-flow collector on the VM as a market-hours systemd service.
# Starts 09:15 IST, stops 15:31 IST, Mon-Fri — same box as the alerter, reusing
# its Angel + Supabase creds and its venv (which already has smartapi/supabase;
# yfinance was added during the probe).
#
#   bash options_flow/deploy/setup.sh
#
# Overridable:
#   OPTIONS_ENV=/path/to/.env   OPTIONS_PY=/path/to/python   bash …/setup.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_USER="$(whoami)"
ENV_FILE="${OPTIONS_ENV:-$HOME/stock_price_alerter/.env}"
PY="${OPTIONS_PY:-$HOME/stock_price_alerter/.venv/bin/python}"

echo "repo:   $REPO_DIR"
echo "user:   $RUN_USER"
echo "env:    $ENV_FILE   (Angel + Supabase creds)"
echo "python: $PY"

[[ -f "$ENV_FILE" ]] || { echo "ERROR: env file $ENV_FILE not found" >&2; exit 1; }
[[ -x "$PY" ]] || { echo "ERROR: python $PY not found" >&2; exit 1; }

sudo timedatectl set-timezone Asia/Kolkata

sudo tee /etc/systemd/system/optionsflow.service >/dev/null <<UNIT
[Unit]
Description=NSE options-flow collector (Angel -> Supabase, 2-min scan)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR/options_flow
EnvironmentFile=$ENV_FILE
ExecStart=$PY $REPO_DIR/options_flow/collector.py
Restart=on-failure
RestartSec=15
TimeoutStopSec=25

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/optionsflow.timer >/dev/null <<'UNIT'
[Unit]
Description=Start options-flow collector at market open (Mon-Fri 09:15 IST)

[Timer]
OnCalendar=Mon-Fri 09:15 Asia/Kolkata
Persistent=false

[Install]
WantedBy=timers.target
UNIT

sudo tee /etc/systemd/system/optionsflow-stop.service >/dev/null <<'UNIT'
[Unit]
Description=Stop options-flow collector at close

[Service]
Type=oneshot
ExecStart=/bin/systemctl stop optionsflow.service
UNIT

sudo tee /etc/systemd/system/optionsflow-stop.timer >/dev/null <<'UNIT'
[Unit]
Description=Stop options-flow collector after close (Mon-Fri 15:31 IST)

[Timer]
OnCalendar=Mon-Fri 15:31 Asia/Kolkata
Persistent=false

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now optionsflow.timer optionsflow-stop.timer

echo
echo "Installed. Timers:"
systemctl list-timers 'optionsflow*' --no-pager || true
echo
echo "Smoke test now (one cycle):"
echo "  set -a; source $ENV_FILE; set +a"
echo "  $PY $REPO_DIR/options_flow/collector.py --once"
