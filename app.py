"""
NSE 52-Week Pullback Screener — shareable web app.

Finds NSE main-board stocks that:
  1. made their 52-week high MORE than N days ago (default 90 = 3 months), and
  2. are currently 1%-10% BELOW that high (a shallow pullback / consolidation).

Access is gated by a single shared passcode stored in Streamlit secrets.
Share the app URL + passcode with your group.
"""

import os

# Cap numpy/BLAS thread pools BEFORE importing numpy/pandas. On the free host
# these pools spawn ~one thread per visible CPU core and exhaust the container's
# thread limit ("RuntimeError: can't start new thread"). Must run before pandas.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import nse_screener as screener   # unique module name avoids stale-cache / name clashes

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="NSE 52-Week Pullback Screener",
    page_icon="📈",
    layout="wide",
)


# ----------------------------------------------------------------------------
# Access control: single shared passcode
# ----------------------------------------------------------------------------
def _configured_passcode() -> str | None:
    # Streamlit Cloud secrets first, then env var (handy for local dev).
    try:
        if "app_password" in st.secrets:
            return str(st.secrets["app_password"])
    except Exception:
        pass
    return os.getenv("APP_PASSWORD")


def require_passcode():
    passcode = _configured_passcode()
    if not passcode:
        st.error(
            "No passcode is configured. Set `app_password` in Streamlit secrets "
            "(Manage app ▸ Settings ▸ Secrets) or the APP_PASSWORD env var."
        )
        st.stop()

    if st.session_state.get("auth_ok"):
        return

    st.title("📈 NSE 52-Week Pullback Screener")
    st.caption("Enter the group passcode to continue.")

    def _check():
        if st.session_state.get("pw_input") == passcode:
            st.session_state.auth_ok = True
            st.session_state.pop("pw_input", None)
        else:
            st.session_state.auth_ok = False

    st.text_input("Passcode", type="password", key="pw_input", on_change=_check)
    if st.session_state.get("auth_ok") is False:
        st.error("Incorrect passcode.")
    st.stop()


require_passcode()


# ----------------------------------------------------------------------------
# Cached screen — computed once per (day, settings), shared across all users
# ----------------------------------------------------------------------------
@st.cache_data(ttl=12 * 3600, show_spinner=False)
def run_screen(trade_date: str, basis: str, min_days: int,
               band_low: float, band_high: float,
               fno_only: bool, qty_circuit_only: bool, qty_threshold, qty_keep: str,
               listing_only: bool, listing_min_months: int, listing_max_months: int):
    universe = screener.load_universe()
    bar = st.progress(0.0, text="Fetching prices from Yahoo Finance…")

    def _cb(done, total):
        bar.progress(done / total, text=f"Fetching prices… {done}/{total} symbols")

    results, stats = screener.fetch_and_screen(
        universe, basis=basis, min_days=min_days,
        band_low=band_low, band_high=band_high,
        fno_only=fno_only, qty_circuit_only=qty_circuit_only,
        qty_threshold=qty_threshold, qty_keep=qty_keep,
        listing_only=listing_only,
        listing_min_months=listing_min_months, listing_max_months=listing_max_months,
        progress_cb=_cb,
    )
    bar.empty()
    # ScreenStats isn't picklable-friendly for display; return a plain dict too
    return results, {
        "universe": stats.universe,
        "fetched": stats.fetched,
        "matched": len(results),
        "skipped_no_data": len(stats.skipped_no_data),
        "skipped_short_history": len(stats.skipped_short_history),
        "split_warnings": stats.split_warnings,
        "as_of": stats.as_of,
    }


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        df.to_excel(xl, index=False, sheet_name="Screener")
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Main UI
# ----------------------------------------------------------------------------
st.title("📈 NSE 52-Week Pullback Screener")
st.markdown(
    "Stocks that made their **52-week high a while ago** and are now sitting "
    "**just below it** — an old peak, a shallow pullback, no fresh breakout yet."
)

with st.sidebar:
    st.header("Settings")
    basis_label = st.radio(
        "52-week high based on",
        ["Intraday High", "Daily Close"],
        help="Intraday High matches NSE's official 52-week high. "
             "Daily Close ignores intraday spikes.",
    )
    basis = "high" if basis_label == "Intraday High" else "close"

    with st.expander("Advanced filters"):
        min_days = st.number_input(
            "High must be older than (days)", min_value=1, max_value=364,
            value=screener.DEFAULT_MIN_DAYS, step=1,
            help="Exclude stocks that made a new 52-week high within this many days.",
        )
        band_low, band_high = st.slider(
            "Pullback band (% below 52w high)",
            min_value=0.0, max_value=25.0,
            value=(screener.DEFAULT_BAND_LOW, screener.DEFAULT_BAND_HIGH),
            step=0.5,
            help="Keep stocks whose close is this % below the 52-week high.",
        )

        st.divider()
        st.caption("Optional filters — each is off by default; enabled ones all "
                   "apply together (AND). The base screen above always applies.")

        # --- Filter 1: F&O only ---
        fno_only = st.checkbox(
            "1 · Futures-enabled (F&O) only",
            value=False,
            help="Keep only stocks that have futures/options contracts.",
        )

        # --- Filter 2: Qty + Upper circuit (both required) ---
        has_band, band_as_of = screener.band_feed_status()
        f2_help = ("Keep stocks whose avg 20-day volume passes the threshold "
                   "(direction below) AND whose daily price band is 20%."
                   if has_band else
                   "Disabled — needs the nightly price-band feed (Angel SmartAPI).")
        qty_circuit_only = st.checkbox(
            "2 · Qty + Upper circuit (20%)",
            value=False,
            disabled=not has_band,
            help=f2_help,
        )
        qty_dir_label = st.radio(
            "Qty direction",
            ["Above threshold (liquid)", "Below threshold (thin)"],
            index=0,
            disabled=not (has_band and qty_circuit_only),
            help="Above: avg vol ≥ threshold. Below: avg vol ≤ threshold. "
                 "Combined with band = 20%.",
        )
        qty_keep = "above" if qty_dir_label.startswith("Above") else "below"
        qty_threshold = st.number_input(
            "Qty threshold (20-day avg volume)",
            min_value=0, value=screener.DEFAULT_MIN_AVG_VOL, step=5000,
            disabled=not (has_band and qty_circuit_only),
        )
        if not has_band:
            st.caption("ℹ️ Filter 2 pending: broker band feed not configured yet.")
        elif band_as_of:
            st.caption(f"Band data as of {band_as_of}")

        # --- Filter 3: Listing window ---
        listing_only = st.checkbox(
            "3 · Listing window",
            value=False,
            help="Keep stocks listed between the two ages below (months since "
                 "NSE listing).",
        )
        lc1, lc2 = st.columns(2)
        listing_min_months = lc1.number_input(
            "Listed ≥ (months)", min_value=0, max_value=600,
            value=screener.DEFAULT_LISTING_MIN_MONTHS, step=1,
            disabled=not listing_only, help="Minimum age since listing.",
        )
        listing_max_months = lc2.number_input(
            "Listed ≤ (months)", min_value=1, max_value=600,
            value=screener.DEFAULT_LISTING_MAX_MONTHS, step=1,
            disabled=not listing_only, help="Maximum age since listing.",
        )

    run = st.button("▶ Run screener", type="primary", use_container_width=True)
    if st.button("↻ Refresh data (clear cache)", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption("First run of the day fetches ~2,000 stocks (a few minutes). "
               "After that everyone gets the cached list instantly.")
    if st.button("Log out", use_container_width=True):
        st.session_state.auth_ok = False
        st.rerun()

# Upfront nudge: F&O (Filter 1) + Qty+Circuit (Filter 2) can never both be true —
# F&O stocks have no price band, so band=20% is impossible for them.
if fno_only and qty_circuit_only:
    st.warning(
        "⚠️ **Filter 1 (F&O) + Filter 2 (Qty + Upper circuit) will return 0 results.** "
        "F&O stocks have no price band, so none can have a 20% band. "
        "Use just one of the two."
    )

if run or st.session_state.get("has_run"):
    st.session_state.has_run = True
    trade_date = datetime.now(IST).strftime("%Y-%m-%d")
    with st.spinner("Running screener…"):
        results, stats = run_screen(
            trade_date, basis, int(min_days), float(band_low), float(band_high),
            fno_only, qty_circuit_only, int(qty_threshold), qty_keep,
            listing_only, int(listing_min_months), int(listing_max_months),
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Universe", stats["universe"])
    c2.metric("Priced", stats["fetched"])
    c3.metric("Matches", stats["matched"])
    c4.metric("As of", stats["as_of"] or "—")

    opt = []
    if fno_only:
        opt.append("F&O only")
    if qty_circuit_only:
        _op = "≥" if qty_keep == "above" else "≤"
        opt.append(f"qty {_op} {int(qty_threshold):,} + band 20%")
    if listing_only:
        opt.append(f"listed {int(listing_min_months)}–{int(listing_max_months)}mo ago")
    opt_txt = (f"filters: **{'  AND  '.join(opt)}**" if opt
               else "optional filters **off**")
    st.caption(
        f"Basis: **{basis_label}**  •  high older than **{int(min_days)}d**  •  "
        f"pullback **{band_low:g}–{band_high:g}%**  •  {opt_txt}  •  "
        f"no data: {stats['skipped_no_data']}, short history: "
        f"{stats['skipped_short_history']}"
    )

    if results.empty:
        st.warning("No stocks matched today's filters.")
    else:
        st.dataframe(results, use_container_width=True, hide_index=True)

        stamp = datetime.now(IST).strftime("%Y%m%d")
        d1, d2 = st.columns(2)
        d1.download_button(
            "⬇ Download CSV",
            results.to_csv(index=False).encode("utf-8"),
            file_name=f"nse_52wk_screener_{stamp}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        d2.download_button(
            "⬇ Download Excel",
            to_excel_bytes(results),
            file_name=f"nse_52wk_screener_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    if stats["split_warnings"]:
        with st.expander(f"⚠ {len(stats['split_warnings'])} possible split/bonus "
                         "(unadjusted prices — eyeball these)"):
            st.write(", ".join(stats["split_warnings"]))
else:
    st.info("Pick your settings on the left and hit **Run screener**.")
