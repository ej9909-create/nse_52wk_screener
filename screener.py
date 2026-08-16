"""
Core screening logic for the NSE 52-Week Pullback Screener.

Filters (over the trailing 52 weeks, per stock):
  1. The 52-week high must be OLD  -> it was set more than `min_days` ago
     (i.e. price has NOT crossed/reclaimed that high in the last 3 months).
  2. The current close is a shallow PULLBACK -> it sits `band_low`..`band_high`
     percent BELOW the 52-week high (default 1%..10%).

Data source: Yahoo Finance via yfinance (works globally, incl. from cloud hosts).
The NSE symbol universe is loaded from a bundled CSV so the app never has to
call NSE directly (NSE blocks most cloud IPs).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

try:  # yfinance is only needed at run time, not for importing helpers
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

# ----------------------------------------------------------------------------
# Config defaults (all overridable from the UI)
# ----------------------------------------------------------------------------
LOOKBACK_WINDOW_DAYS = 365      # "52 weeks" for the high computation
FETCH_DAYS = 420                # calendar days of history to pull (buffer > 365)
MIN_TRADING_DAYS = 200          # skip stocks with too little history for a 52wk high
BATCH_SIZE = 100                # tickers per yfinance download call
AVG_VOL_DAYS = 20               # window for the average-volume column
SPLIT_JUMP_PCT = 35.0           # flag a likely split/bonus if a 1-day move exceeds this

DEFAULT_MIN_DAYS = 90           # "3 months"
DEFAULT_BAND_LOW = 1.0          # % below high (min pullback)
DEFAULT_BAND_HIGH = 10.0        # % below high (max pullback)

RESULT_COLUMNS = [
    "Symbol", "Company", "LastClose", "52wHigh", "HighDate",
    "DaysSinceHigh", "PctFromHigh", "AvgVol20d", "Basis",
]


@dataclass
class ScreenStats:
    universe: int = 0
    fetched: int = 0
    skipped_no_data: list[str] = field(default_factory=list)
    skipped_short_history: list[str] = field(default_factory=list)
    split_warnings: list[str] = field(default_factory=list)
    as_of: str | None = None          # latest trading date seen in the data


# ----------------------------------------------------------------------------
# Universe
# ----------------------------------------------------------------------------
def load_universe(path: str = "data/nse_equity_list.csv") -> pd.DataFrame:
    """Return the bundled universe as a DataFrame with columns Symbol, Company, Ticker."""
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"NAME OF COMPANY": "Company", "SYMBOL": "Symbol"})
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df["Company"] = df["Company"].astype(str).str.strip()
    df = df[df["Symbol"] != ""].drop_duplicates(subset="Symbol").reset_index(drop=True)
    df["Ticker"] = df["Symbol"] + ".NS"
    return df[["Symbol", "Company", "Ticker"]]


# ----------------------------------------------------------------------------
# Download helpers
# ----------------------------------------------------------------------------
def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _download_batch(tickers: list[str], start, retries: int = 2) -> pd.DataFrame | None:
    """Download one batch; retry a couple of times on transient failures/empties."""
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    last_err = None
    for attempt in range(retries + 1):
        try:
            data = yf.download(
                tickers,
                start=start,
                interval="1d",
                auto_adjust=False,      # match NSE's displayed (unadjusted) 52wk high
                group_by="ticker",
                threads=True,
                progress=False,
            )
            if data is not None and not data.empty:
                return data
        except Exception as e:  # network / rate-limit hiccup
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    if last_err is not None:
        # swallow: caller records these tickers as no-data
        pass
    return None


def _extract_one(data: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Pull a single ticker's OHLCV frame out of a multi-ticker download."""
    try:
        if isinstance(data.columns, pd.MultiIndex):
            if ticker not in data.columns.get_level_values(0):
                return None
            sub = data[ticker].copy()
        else:
            sub = data.copy()  # single-ticker fallback
    except Exception:
        return None
    sub = sub.dropna(how="all")
    if sub.empty or "Close" not in sub.columns:
        return None
    return sub


# ----------------------------------------------------------------------------
# Per-stock computation
# ----------------------------------------------------------------------------
def _evaluate(sub: pd.DataFrame, basis: str) -> dict | None:
    """Compute 52wk metrics for one stock. Returns a metrics dict or None if unusable."""
    sub = sub.sort_index()
    close = sub["Close"].dropna()
    if close.empty:
        return None

    last_date = close.index[-1]
    cutoff = last_date - timedelta(days=LOOKBACK_WINDOW_DAYS)
    window = sub.loc[sub.index >= cutoff]
    if len(window) < MIN_TRADING_DAYS:
        return {"_short_history": True}

    price = window["High"] if basis == "high" else window["Close"]
    price = price.dropna()
    if price.empty:
        return None

    high_52w = float(price.max())
    if high_52w <= 0:
        return None
    # most-RECENT date the high was touched (strict about "no recent cross")
    high_date = price[price == high_52w].index.max()

    last_close = float(window["Close"].dropna().iloc[-1])
    days_since_high = int((last_date - high_date).days)
    pct_from_high = (high_52w - last_close) / high_52w * 100.0

    vol = window["Volume"].dropna()
    avg_vol = float(vol.tail(AVG_VOL_DAYS).mean()) if not vol.empty else 0.0

    # likely split/bonus flag (unadjusted data): a huge single-day close move
    daily_ret = window["Close"].pct_change().abs()
    split_flag = bool((daily_ret > SPLIT_JUMP_PCT / 100.0).any())

    return {
        "LastClose": round(last_close, 2),
        "52wHigh": round(high_52w, 2),
        "HighDate": high_date.date().isoformat(),
        "DaysSinceHigh": days_since_high,
        "PctFromHigh": round(pct_from_high, 2),
        "AvgVol20d": int(avg_vol),
        "_last_date": last_date,
        "_split_flag": split_flag,
    }


def fetch_and_screen(
    universe: pd.DataFrame,
    basis: str = "high",
    min_days: int = DEFAULT_MIN_DAYS,
    band_low: float = DEFAULT_BAND_LOW,
    band_high: float = DEFAULT_BAND_HIGH,
    progress_cb=None,
) -> tuple[pd.DataFrame, ScreenStats]:
    """
    Download prices for the whole universe and apply the two filters.

    Returns (results_df, stats). `progress_cb(done, total)` is called after each
    batch so the UI can render a progress bar.
    """
    basis = "high" if str(basis).lower().startswith("h") else "close"
    stats = ScreenStats(universe=len(universe))
    start = (datetime.now() - timedelta(days=FETCH_DAYS)).date()

    sym_by_ticker = dict(zip(universe["Ticker"], universe["Symbol"]))
    name_by_ticker = dict(zip(universe["Ticker"], universe["Company"]))
    tickers = list(universe["Ticker"])

    rows: list[dict] = []
    latest_seen = None
    total = len(tickers)
    done = 0

    for batch in _chunks(tickers, BATCH_SIZE):
        data = _download_batch(batch, start)
        if data is None:
            stats.skipped_no_data.extend(sym_by_ticker[t] for t in batch)
        else:
            for t in batch:
                sub = _extract_one(data, t)
                sym = sym_by_ticker[t]
                if sub is None:
                    stats.skipped_no_data.append(sym)
                    continue
                metrics = _evaluate(sub, basis)
                if metrics is None:
                    stats.skipped_no_data.append(sym)
                    continue
                if metrics.get("_short_history"):
                    stats.skipped_short_history.append(sym)
                    continue
                stats.fetched += 1
                if metrics["_split_flag"]:
                    stats.split_warnings.append(sym)
                ld = metrics["_last_date"]
                if latest_seen is None or ld > latest_seen:
                    latest_seen = ld
                # apply the two filters
                if metrics["DaysSinceHigh"] <= min_days:
                    continue
                if not (band_low <= metrics["PctFromHigh"] <= band_high):
                    continue
                rows.append({
                    "Symbol": sym,
                    "Company": name_by_ticker[t],
                    "LastClose": metrics["LastClose"],
                    "52wHigh": metrics["52wHigh"],
                    "HighDate": metrics["HighDate"],
                    "DaysSinceHigh": metrics["DaysSinceHigh"],
                    "PctFromHigh": metrics["PctFromHigh"],
                    "AvgVol20d": metrics["AvgVol20d"],
                    "Basis": "Intraday High" if basis == "high" else "Daily Close",
                })
        done += len(batch)
        if progress_cb:
            progress_cb(min(done, total), total)

    if latest_seen is not None:
        stats.as_of = latest_seen.date().isoformat()

    results = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    if not results.empty:
        results = results.sort_values("PctFromHigh").reset_index(drop=True)
    return results, stats
