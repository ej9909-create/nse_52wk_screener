# Reliable daily refresh from the VM

GitHub Actions' scheduled cron drops runs unpredictably, so the daily data
refresh is triggered from the always-on GCP VM instead (the same e2-micro that
runs the alerter). A systemd timer fires once after market close and dispatches
the `update-daily` workflow — the Angel fetch + commit still run in GitHub
Actions, where the Angel secrets already live. On failure it retries, then
alerts you on Telegram.

```
VM systemd timer (Mon-Fri 16:05 IST)
        │  dispatch update-daily.yml  (GH_TOKEN)
        ▼
GitHub Actions ── update_angel.py ── Angel EOD ── commit snapshot
        │  (on failure after 3 tries)
        ▼
   Telegram alert
```

## Deploy (on the VM)

SSH into the VM (the GCP console's **SSH** button is easiest), then:

```bash
# 1. get the code
git clone https://github.com/ej9909-create/nse_52wk_screener.git
cd nse_52wk_screener

# 2. secrets (never committed)
cp vm/.env.example vm/.env
nano vm/.env        # paste GH_TOKEN (+ Telegram token/chat id)

# 3. install the timer
bash vm/setup.sh
```

`GH_TOKEN` = a GitHub **fine-grained** token with **Actions: Read and write** on
this repo, or a **classic** token with the `repo` + `workflow` scopes.

## Verify

```bash
# fire it once now (any time — it just refreshes to the latest close)
sudo systemctl start nse-update.service
journalctl -u nse-update -f          # watch it dispatch + wait

systemctl list-timers 'nse-update*'  # confirm the next 16:05 IST run
```

A successful run logs `DONE: update succeeded` and the snapshot `as_of` date.
If it fails 3× you get a Telegram message with the Actions link.

## Update later

```bash
cd ~/nse_52wk_screener && git pull && bash vm/setup.sh
```

## Notes

- The GitHub Actions `schedule:` cron on `update-daily.yml` is left in place as a
  secondary trigger — harmless if it fires, and the same-day refresh fix means a
  later run of the day just overwrites an earlier one (last run wins).
- Data not advancing on a market **holiday** is normal (Angel returns no newer
  trade date) and is **not** treated as a failure — no alert.
