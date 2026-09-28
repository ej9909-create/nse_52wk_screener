# Options-flow scanner (live intraday)

Scans "what's changing right now" across ~2,500 NSE option contracts (indices +
~40 liquid stocks, ATM ±15, nearest expiry) — volume/OI/price/IV moves over
5/10/15 min, build-up, Greeks. Separate from the EOD 52-week screener; shares the
Angel creds, VM and Supabase.

```
VM collector (Mon-Fri 09:15–15:31 IST, systemd)
   │  every ~2 min: getMarketData FULL over option + future tokens
   │  compute IV/Greeks (Black-76 off the future), 5/10/15-min deltas, build-up
   ▼
Supabase  options_scan  (latest snapshot, ~2,500 rows, upserted each cycle)
   ▲
Streamlit app  "Options Flow" tab  (Phase 2 — reads Supabase, ranks/filters)
```

## Files
- `greeks.py` — Black-76 IV + Greeks (off the future).
- `universe.py` — scoped universe: option tokens + nearest future per underlying.
- `collector.py` — the 2-min loop → enrich → upsert to Supabase (`--once` for a single cycle).
- `store.py` — Supabase upsert.
- `schema.sql` — the `options_scan` table (run once in Supabase SQL editor).
- `deploy/setup.sh` — systemd service + start/stop timers.
- `probe.py` — Phase-0 feasibility probe (throwaway; can be deleted).

## Deploy (on the VM)

1. **Create the table** — paste `schema.sql` into the Supabase SQL editor and run it.
2. **Smoke-test one cycle during market hours** (reuses the alerter's creds + venv):
   ```bash
   cd ~/nse_52wk_screener && git pull
   set -a; source ~/stock_price_alerter/.env; set +a
   ~/stock_price_alerter/.venv/bin/python options_flow/collector.py --once
   ```
   Expect `fetched …/2551 -> upserted ~2507 contracts`. Check the `options_scan`
   table in Supabase.
3. **Install the market-hours timers:**
   ```bash
   bash options_flow/deploy/setup.sh
   ```

Handy:
```bash
journalctl -u optionsflow -f            # live logs while running
systemctl list-timers 'optionsflow*'    # next start/stop
sudo systemctl start optionsflow        # start now (test the service unit)
```

## Notes
- Creds (Angel + Supabase) are reused from `~/stock_price_alerter/.env` via the
  service's `EnvironmentFile` — nothing new to configure.
- IV uses the underlying **future** as the forward (Black-76); the collector
  fetches the nearest future per underlying alongside the options.
- Universe rebuilds at each new trading day (expiry roll, ATM drift).
