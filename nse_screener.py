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

import gc
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
BATCH_SIZE = 50                 # tickers per yfinance download call (smaller = lower peak RAM)
AVG_VOL_DAYS = 20               # window for the average-volume column
SPLIT_JUMP_PCT = 35.0           # flag a likely split/bonus if a 1-day move exceeds this

DEFAULT_MIN_DAYS = 90           # "3 months"
DEFAULT_BAND_LOW = 1.0          # % below high (min pullback)
DEFAULT_BAND_HIGH = 10.0        # % below high (max pullback)
DEFAULT_MIN_AVG_VOL = 30000     # min 20-day avg daily volume when the filter is ON
CIRCUIT_BAND_PCT = 20           # the "upper circuit = 20%" filter target

FO_PATH = "data/fo_stocks.csv"        # bundled F&O underlyings list
BANDS_PATH = "data/price_bands.csv"   # nightly per-symbol price bands (broker feed)

RESULT_COLUMNS = [
    "Symbol", "Company", "LastClose", "52wHigh", "HighDate",
    "DaysSinceHigh", "PctFromHigh", "AvgVol20d", "F&O", "Band%", "Basis",
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
def load_universe(
    path: str = "data/nse_equity_list.csv",
    fo_path: str = FO_PATH,
    bands_path: str = BANDS_PATH,
) -> pd.DataFrame:
    """
    Return the bundled universe with columns:
      Symbol, Company, Ticker, is_fno (bool), Band (float %, NaN if unknown).

    F&O flag comes from the bundled F&O list; Band comes from the nightly broker
    feed. Both are optional — if a file is missing the column defaults sensibly.
    Reads `df.attrs["band_as_of"]` / `df.attrs["has_band_data"]` for feed status.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"NAME OF COMPANY": "Company", "SYMBOL": "Symbol"})
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df["Company"] = df["Company"].astype(str).str.strip()
    df = df[df["Symbol"] != ""].drop_duplicates(subset="Symbol").reset_index(drop=True)
    df["Ticker"] = df["Symbol"] + ".NS"

    # F&O flag
    fno_set = _load_fno_set(fo_path)
    df["is_fno"] = df["Symbol"].isin(fno_set)

    # Price bands (nightly broker feed)
    band_map, band_as_of = _load_bands(bands_path)
    df["Band"] = df["Symbol"].map(band_map).astype("float")

    df.attrs["band_as_of"] = band_as_of
    df.attrs["has_band_data"] = bool(band_map)
    return df[["Symbol", "Company", "Ticker", "is_fno", "Band"]]


def _load_fno_set(fo_path: str) -> set[str]:
    try:
        fo = pd.read_csv(fo_path)
        col = "Symbol" if "Symbol" in fo.columns else fo.columns[0]
        return set(fo[col].astype(str).str.strip())
    except Exception:
        return set()


def _load_bands(bands_path: str) -> tuple[dict, str | None]:
    """Return ({Symbol: band%}, as_of_date_or_None). Empty if the feed is absent."""
    try:
        b = pd.read_csv(bands_path)
        b.columns = [c.strip() for c in b.columns]
        sym = "Symbol" if "Symbol" in b.columns else b.columns[0]
        band_col = "Band" if "Band" in b.columns else b.columns[1]
        b[sym] = b[sym].astype(str).str.strip()
        band_map = dict(zip(b[sym], pd.to_numeric(b[band_col], errors="coerce")))
        as_of = None
        if "AsOf" in b.columns and not b["AsOf"].dropna().empty:
            as_of = str(b["AsOf"].dropna().iloc[0])
        return band_map, as_of
    except Exception:
        return {}, None


def band_feed_status(bands_path: str = BANDS_PATH) -> tuple[bool, str | None]:
    """(has_data, as_of) — lets the UI enable/disable the 20% band filter."""
    band_map, as_of = _load_bands(bands_path)
    return bool(band_map), as_of


# ----------------------------------------------------------------------------
# Download helpers
# ----------------------------------------------------------------------------
def _band_num(v) -> int | None:
    """Coerce a band value to an int %, or None if missing/NaN/no-band."""
    try:
        if v is None or v != v:      # None or NaN
            return None
        n = int(round(float(v)))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


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
    # keep only what we use and downcast to float32 to trim memory
    keep = [c for c in ("High", "Close", "Volume") if c in sub.columns]
    sub = sub[keep].copy()
    for c in keep:
        sub[c] = pd.to_numeric(sub[c], downcast="float")
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
    vol_threshold: float | None = None,
    vol_keep: str = "above",
    fno_only: bool = False,
    band20_only: bool = False,
    combine: str = "AND",
    progress_cb=None,
) -> tuple[pd.DataFrame, ScreenStats]:
    """
    Download prices for the whole universe and apply the filters.

    The BASE screen (old 52-week high + shallow pullback) is ALWAYS applied.
    On top of it, up to three OPTIONAL filters are combined by `combine`:
      - volume  (enabled when vol_threshold is not None; direction via vol_keep)
      - F&O     (enabled when fno_only=True)   -> stock has futures/options
      - band20  (enabled when band20_only=True) -> daily price band == 20%
    `combine` = "AND" (a stock must pass every enabled optional filter) or
    "OR" (must pass at least one). With one optional filter the two are identical.
    With no optional filter enabled, only the base screen applies.

    Returns (results_df, stats). `progress_cb(done, total)` is called after each
    batch so the UI can render a progress bar.
    """
    basis = "high" if str(basis).lower().startswith("h") else "close"
    combine = "OR" if str(combine).upper() == "OR" else "AND"
    stats = ScreenStats(universe=len(universe))
    start = (datetime.now() - timedelta(days=FETCH_DAYS)).date()

    sym_by_ticker = dict(zip(universe["Ticker"], universe["Symbol"]))
    name_by_ticker = dict(zip(universe["Ticker"], universe["Company"]))
    fno_by_sym = dict(zip(universe["Symbol"], universe.get("is_fno", False)))
    band_by_sym = dict(zip(universe["Symbol"], universe.get("Band", float("nan"))))
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
                # BASE screen — always applied
                if metrics["DaysSinceHigh"] <= min_days:
                    continue
                if not (band_low <= metrics["PctFromHigh"] <= band_high):
                    continue

                # OPTIONAL filters — combined by AND/OR
                is_fno = bool(fno_by_sym.get(sym, False))
                band_num = _band_num(band_by_sym.get(sym))
                passes: list[bool] = []
                if vol_threshold is not None:
                    v = metrics["AvgVol20d"]
                    passes.append(v <= vol_threshold if vol_keep == "below"
                                  else v >= vol_threshold)
                if fno_only:
                    passes.append(is_fno)
                if band20_only:
                    passes.append(band_num == CIRCUIT_BAND_PCT)
                if passes:
                    ok = all(passes) if combine == "AND" else any(passes)
                    if not ok:
                        continue

                if band_num is not None:
                    band_disp = band_num
                elif is_fno:
                    band_disp = "NB"       # F&O stocks have no price band
                else:
                    band_disp = "—"
                rows.append({
                    "Symbol": sym,
                    "Company": name_by_ticker[t],
                    "LastClose": metrics["LastClose"],
                    "52wHigh": metrics["52wHigh"],
                    "HighDate": metrics["HighDate"],
                    "DaysSinceHigh": metrics["DaysSinceHigh"],
                    "PctFromHigh": metrics["PctFromHigh"],
                    "AvgVol20d": metrics["AvgVol20d"],
                    "F&O": "Yes" if is_fno else "No",
                    "Band%": band_disp,
                    "Basis": "Intraday High" if basis == "high" else "Daily Close",
                })
        # release this batch's raw frame before fetching the next one
        del data
        gc.collect()
        done += len(batch)
        if progress_cb:
            progress_cb(min(done, total), total)

    if latest_seen is not None:
        stats.as_of = latest_seen.date().isoformat()

    results = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    if not results.empty:
        results = results.sort_values("PctFromHigh").reset_index(drop=True)
    return results, stats
