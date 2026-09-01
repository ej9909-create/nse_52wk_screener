#!/usr/bin/env python3
"""
Daily trigger for the NSE screener's same-day data update, run from the always-on
GCP VM. GitHub Actions' scheduled cron drops runs unreliably, so instead the VM's
systemd timer fires this once after market close (~16:05 IST).

It dispatches the `update-daily` workflow (the Angel fetch + commit still run in
GitHub Actions, where the Angel secrets already live), waits for it to finish,
and on failure retries a few times then alerts via Telegram. Stdlib only — no
venv or third-party deps, so it runs on the VM's system python3.

Env (see .env.example):
  GH_TOKEN            GitHub token with Actions: read & write on the repo
  GH_OWNER, GH_REPO   repo coordinates (default ej9909-create/nse_52wk_screener)
  GH_WORKFLOW         workflow filename (default update-daily.yml)
  TELEGRAM_BOT_TOKEN  bot token for failure alerts (optional but recommended)
  TELEGRAM_CHAT_ID    chat/channel id for failure alerts
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
API = "https://api.github.com"
ATTEMPTS = 3
RETRY_GAP = 30            # seconds between whole-cycle retries


def _env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        print(f"ERROR: missing env var {name}", file=sys.stderr)
        sys.exit(2)
    return v


GH_TOKEN = _env("GH_TOKEN")          # required for a real run; not for --test-alert
OWNER = _env("GH_OWNER", "ej9909-create")
REPO = _env("GH_REPO", "nse_52wk_screener")
WF = _env("GH_WORKFLOW", "update-daily.yml")
TG_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TG_CHAT = _env("TELEGRAM_CHAT_ID")


def _gh(method, path, body=None):
    url = path if path.startswith("http") else f"{API}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {GH_TOKEN}",
        "User-Agent": "nse-screener-vm-trigger",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print("(no Telegram creds; skipping alert)", file=sys.stderr)
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TG_CHAT, "text": text}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False


def newest_run_id():
    _, raw = _gh("GET", f"/repos/{OWNER}/{REPO}/actions/workflows/{WF}/runs?per_page=1")
    runs = json.loads(raw).get("workflow_runs", [])
    return runs[0]["id"] if runs else None


def dispatch():
    status, _ = _gh("POST",
                    f"/repos/{OWNER}/{REPO}/actions/workflows/{WF}/dispatches",
                    body={"ref": "main"})
    return status == 204


def run_state(run_id):
    _, raw = _gh("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}")
    j = json.loads(raw)
    return j.get("status"), j.get("conclusion")


def snapshot_as_of():
    """Max LastDate in the committed snapshot (freshness check / logging)."""
    url = (f"https://raw.githubusercontent.com/{OWNER}/{REPO}/main/"
           f"data/screener_snapshot.csv")
    req = urllib.request.Request(
        url, headers={"Cache-Control": "no-cache", "User-Agent": "nse-vm"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = list(csv.reader(io.StringIO(r.read().decode())))
    hdr = rows[0]
    idx = hdr.index("LastDate")
    dates = [row[idx] for row in rows[1:] if len(row) > idx and row[idx]]
    return max(dates) if dates else None


def one_cycle():
    """A single dispatch → wait-for-completion cycle. Returns (ok, detail)."""
    before = newest_run_id()
    if not dispatch():
        return False, "dispatch returned non-204"
    new_id = None
    for _ in range(24):                       # ~2 min for the run to register
        cur = newest_run_id()
        if cur and cur != before:
            new_id = cur
            break
        time.sleep(5)
    if not new_id:
        return False, "dispatched run never registered"
    for _ in range(120):                      # ~10 min to complete
        status, concl = run_state(new_id)
        if status == "completed":
            return (concl == "success"), f"run {new_id} -> {concl}"
        time.sleep(5)
    return False, f"run {new_id} timed out"


def test_alert():
    """Send one test message to the Telegram channel and exit — verifies the
    alert path without dispatching anything or touching data."""
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    ok = telegram(f"✅ NSE screener test alert — Telegram path OK ({stamp}). "
                  "Daily-refresh failures will be reported here.")
    if ok:
        print("Test alert sent — check the price-alerts channel.")
    else:
        print("Test alert NOT sent (see error above). Check TELEGRAM_BOT_TOKEN / "
              "TELEGRAM_CHAT_ID in vm/.env.", file=sys.stderr)
        sys.exit(1)


def main():
    if "--test-alert" in sys.argv[1:]:
        test_alert()
        return
    if not GH_TOKEN:
        print("ERROR: missing env var GH_TOKEN", file=sys.stderr)
        sys.exit(2)

    today = datetime.now(IST).strftime("%Y-%m-%d")
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M IST}] triggering update-daily "
          f"for {today}", flush=True)

    last = ""
    for i in range(1, ATTEMPTS + 1):
        ok, detail = one_cycle()
        print(f"  attempt {i}/{ATTEMPTS}: {detail}", flush=True)
        if ok:
            try:
                print(f"  snapshot as_of = {snapshot_as_of()}", flush=True)
            except Exception as e:
                print(f"  as_of check failed (non-fatal): {e}", flush=True)
            print("DONE: update succeeded", flush=True)
            return
        last = detail
        if i < ATTEMPTS:
            time.sleep(RETRY_GAP)

    msg = (f"⚠️ NSE screener daily update FAILED for {today} "
           f"after {ATTEMPTS} tries.\nLast: {last}\n"
           f"https://github.com/{OWNER}/{REPO}/actions")
    print(msg, file=sys.stderr, flush=True)
    telegram(msg)
    sys.exit(1)


if __name__ == "__main__":
    main()
