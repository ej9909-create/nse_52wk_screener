# 📈 NSE 52-Week Pullback Screener

A shareable web app that screens **NSE main-board equities** for a specific setup:

1. **Old high** — the stock made its 52-week high **more than 3 months ago** (i.e. it has *not* crossed/reclaimed that high recently), **and**
2. **Shallow pullback** — the current close is **1%–10% below** that 52-week high.

Net: stocks quietly consolidating just under an older peak, no fresh breakout yet.

Data comes from Yahoo Finance (`yfinance`). The NSE symbol list is **bundled** in
`data/nse_equity_list.csv` so the hosted app never has to call NSE directly.

Access is protected by a **single shared passcode** — share the app URL + passcode
with your group.

---

## What each column means

| Column | Meaning |
|---|---|
| `LastClose` | Latest closing price |
| `52wHigh` | Highest price over the trailing 52 weeks (Intraday High or Daily Close — your choice) |
| `HighDate` | Most recent date that high was touched |
| `DaysSinceHigh` | Days since `HighDate` (must be > 90 to qualify) |
| `PctFromHigh` | How far below the high, in % (must be 1–10 to qualify) |
| `AvgVol20d` | Average daily volume over the last 20 sessions (shown always; optionally filtered) |
| `F&O` | Whether the stock has futures/options (Yes/No; shown always, optionally filtered) |
| `Band%` | Daily price band (20 / 10 / 5 / 2, `NB` = no band for F&O, `—` = unknown) |
| `Basis` | Whether the high used Intraday High or Daily Close |

Results are sorted by `PctFromHigh` ascending (closest to the high first).

---

## Optional filters and the AND/OR combiner

The **base screen** (old 52-week high + 1–10% pullback) always applies. On top of it
there are three **opt-in** filters under *Advanced filters*, all off by default:

- **Volume** — keep stocks above/below a 20-day avg volume threshold.
- **Futures-enabled (F&O) only** — keep only stocks with derivatives.
- **Upper circuit = 20% only** — keep only stocks whose daily price band is 20%.
  *(Disabled until the nightly band feed is configured — see below.)*

Combine them with **AND (match all)** or **OR (match any)**. With a single filter the
two are identical, so any filter also works standalone.

> ⚠️ **F&O** AND **Band = 20%** returns 0 — F&O stocks have no price band. The app
> nudges you upfront; use **OR**, or pick one. (Under OR it's a valid union.)

Refresh the F&O list occasionally (changes ~monthly), from an India IP:
```bash
python update_fo_list.py     # rewrites data/fo_stocks.csv; then git add/commit/push
```

---

## Activating the price-band feed (Angel One SmartAPI)

The **Band = 20%** filter reads `data/price_bands.csv`, produced nightly by
`.github/workflows/update-bands.yml` via Angel One SmartAPI (exact upper/lower
circuit per symbol — an authenticated API, so it runs from GitHub's cloud, no Mac,
no NSE IP-block). Until it's configured the band filter shows "pending".

To turn it on, add these **repo secrets** (Settings ▸ Secrets and variables ▸ Actions):

| Secret | Value |
|---|---|
| `ANGEL_API_KEY` | your SmartAPI app key |
| `ANGEL_CLIENT_CODE` | Angel client/login id |
| `ANGEL_PIN` | login PIN |
| `ANGEL_TOTP_SECRET` | the base32 TOTP secret (not a live 6-digit code) |

The job runs 19:00 IST on weekdays (or **Run workflow** manually), writes the CSV,
commits it, and the app redeploys with fresh bands. Without the secrets it no-ops.

---

## Run it locally

```bash
cd ~/nse_52wk_screener
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# set the passcode for local use
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
#   then edit .streamlit/secrets.toml -> app_password = "your-code"

streamlit run app.py
```

Open http://localhost:8501, enter the passcode, pick **Intraday High** or **Daily
Close**, and hit **Run screener**.

---

## Deploy so the group can use it (free, ~5 minutes)

The easiest host is **Streamlit Community Cloud** — one public URL, no install for
your group.

1. **Put this folder on GitHub**
   ```bash
   cd ~/nse_52wk_screener
   git init && git add . && git commit -m "NSE 52-week pullback screener"
   # create an empty repo on github.com, then:
   git remote add origin https://github.com/<you>/nse_52wk_screener.git
   git push -u origin main
   ```
   > `.gitignore` keeps `.streamlit/secrets.toml` (your real passcode) out of the repo.

2. **Deploy on Streamlit Cloud**
   - Go to https://share.streamlit.io → **Create app** → pick your repo.
   - Main file path: `app.py`. Click **Deploy**.

3. **Set the passcode** (this is your access control)
   - In the app page: **Manage app ▸ Settings ▸ Secrets**, paste:
     ```toml
     app_password = "your-group-code"
     ```
   - Save. The app restarts and is now gated.

4. **Share** the app URL (e.g. `https://<name>.streamlit.app`) **+ the passcode**
   with your group. That's it — they open the link on phone or laptop, no install.

### Rotating / revoking access
Change `app_password` in **Manage app ▸ Settings ▸ Secrets** at any time. The old
passcode stops working within seconds — no redeploy needed.

---

## Keeping the app awake (no cold starts)
The free tier sleeps an app after inactivity. A GitHub Action
(`.github/workflows/keep-warm.yml`) pings the app every 15 min on weekdays during
Indian market hours so the group always gets an instant load. It uses a cookie jar
so the ping completes Streamlit Cloud's redirect handshake and actually reaches the
app (HTTP 200), rather than bouncing off the edge.

- Watch it under the repo's **Actions** tab; run it manually via **Run workflow**.
- **Caveat:** GitHub auto-disables scheduled workflows after **60 days with no repo
  activity**. If you rarely push, either push a trivial commit occasionally, or
  re-enable it from the Actions tab. (Alternatively, a free external pinger like
  UptimeRobot avoids this.)

## Daily use
Run it **after market close**. The first person to run it that day triggers the
~2-minute fetch of ~2,000 stocks; everyone else gets the same cached list instantly
for the next 12 hours. The **↻ Refresh data** button forces a fresh fetch.

---

## Refreshing the NSE symbol list
The bundled universe (`data/nse_equity_list.csv`) changes only when NSE adds/removes
listings. To refresh it (run from an **India IP** — NSE blocks most cloud IPs):

```bash
python update_universe.py     # rewrites data/nse_equity_list.csv
git add data/nse_equity_list.csv && git commit -m "refresh universe" && git push
```

---

## Notes & caveats
- **Unadjusted prices.** The 52-week high uses raw prices to match NSE's quote page.
  A recent split/bonus can distort a stock's high — the app lists likely cases under
  a ⚠ expander so you can eyeball them.
- **Newly listed stocks** (< ~200 trading days of history) are skipped — a 52-week
  high isn't meaningful yet.
- **SME/Emerge not included** — this is main board only (Yahoo's SME coverage is
  patchy). Can be added later via an NSE-bhavcopy data layer if needed.
- Adjust the 3-month age and the 1–10% band any time under **Advanced filters**.
- **Volume filter is opt-in** (off by default). Tick *Apply volume filter* under
  Advanced filters, then the end user chooses the **direction**:
  *Above threshold* keeps only liquid stocks (avg vol ≥ threshold), *Below
  threshold* keeps only thin stocks (avg vol ≤ threshold). Threshold defaults to
  30,000 (20-day average daily volume).
