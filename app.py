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

import importlib
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# Force-reload the local module every run. The file-watcher is off (to keep the
# thread count low on the free host), so Streamlit would otherwise hot-rerun this
# script against a STALE cached nse_screener after a redeploy — causing
# "AttributeError: module 'nse_screener' has no attribute ..." on new functions.
# reload() re-reads the current source from disk; cheap since the app is light.
import nse_screener
importlib.reload(nse_screener)
screener = nse_screener

import alerts_db  # Supabase-backed price alerts (degrades gracefully if unset)

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
# Snapshot loader — the app just READS the nightly precomputed table (no fetch,
# no heavy compute at request time). Filtering happens instantly in-memory.
# ----------------------------------------------------------------------------
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_snap():
    snap = screener.load_snapshot()
    return snap, snap.attrs.get("as_of")


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        df.to_excel(xl, index=False, sheet_name="Screener")
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Price-alert UI (writes to Supabase; shared with the alerter on the GCP VM)
# ----------------------------------------------------------------------------
def _snap_price_map(snap: pd.DataFrame) -> dict[str, tuple[float | None, float | None]]:
    """Map {Symbol: (last_close, intraday_52w_high)} from the snapshot."""
    out: dict[str, tuple[float | None, float | None]] = {}
    if snap is None or snap.empty:
        return out
    for _, r in snap.iterrows():
        close = float(r["LastClose"]) if pd.notna(r.get("LastClose")) else None
        high = float(r["HighH"]) if pd.notna(r.get("HighH")) else None
        out[str(r["Symbol"])] = (close, high)
    return out


@st.cache_data(ttl=60, show_spinner=False)
def _live_price(symbol: str) -> float | None:
    """Live-ish last price (~15 min delayed) via yfinance; None on failure.

    The snapshot's LastClose is the *previous day's* close, which can be well
    off intraday — so for setting alert levels we prefer a live quote. Cached
    60s so repeated reruns don't re-fetch.
    """
    try:
        import yfinance as yf
        p = yf.Ticker(f"{symbol}.NS").fast_info.last_price
        return round(float(p), 2) if p else None
    except Exception:
        return None


def _alert_form(symbol: str, snap_close: float, high52: float | None, *, key_prefix: str):
    """Reusable add-alert block with smart defaults. Used by quick-add + search.

    Prefers a live (~15 min delayed) price for the shown price and the threshold
    defaults, falling back to the snapshot's previous close if the fetch fails.
    """
    live = _live_price(symbol)
    current = live if live else snap_close
    src = "live ~15m delay" if live else "prev close"
    hi_txt = f"  ·  52w high ₹{high52:,.2f}" if high52 else ""
    st.markdown(f"**{symbol}** — {src} ₹{current:,.2f}{hi_txt}")

    c1, c2 = st.columns(2)
    up_on = c1.checkbox("Alert if it breaks ABOVE", value=bool(high52),
                        key=f"{key_prefix}_upon")
    up_default = float(high52) if high52 else round(current * 1.05, 2)
    up_val = c1.number_input("Above ₹", min_value=0.0, value=up_default, step=1.0,
                             key=f"{key_prefix}_upval", disabled=not up_on)
    lo_on = c2.checkbox("Alert if it breaks BELOW", value=False,
                        key=f"{key_prefix}_loon")
    lo_default = round(current * 0.95, 2) if current else 0.0
    lo_val = c2.number_input("Below ₹", min_value=0.0, value=lo_default, step=1.0,
                             key=f"{key_prefix}_loval", disabled=not lo_on)

    st.caption("Defaults: **Above** = 52-week high (breakout), **Below** = 5% under "
               "the current price. Tick either side, adjust, and add.")
    st.caption("⚡ The alert triggers on **real-time** price — the ₹ shown above is "
               "just a ~15 min delayed reference for picking levels.")
    note = st.text_input("Note (optional)", key=f"{key_prefix}_note",
                         placeholder="e.g. breakout watch")

    if st.button("➕ Add alert", key=f"{key_prefix}_add", type="primary"):
        upper = float(up_val) if up_on else None
        lower = float(lo_val) if lo_on else None
        if upper is None and lower is None:
            st.error("Tick at least one of Above / Below.")
        elif upper is not None and lower is not None and upper <= lower:
            st.error("'Above' must be higher than 'Below'.")
        else:
            try:
                alerts_db.add_alert(symbol, upper=upper, lower=lower, note=note or None)
                st.toast(f"Alert saved for {symbol}", icon="🔔")
                st.rerun()  # closes the dialog (if open) and refreshes the list
            except Exception as e:
                st.error(f"Couldn't save alert: {e}")


@st.dialog("🔔 Add price alert")
def _alert_dialog(symbol: str, current: float, high52: float | None):
    """Modal add-alert form — pops up centered so it's visible however far the
    results table is scrolled (no more hunting below the fold)."""
    _alert_form(symbol, current, high52, key_prefix=f"dlg_{symbol}")


def _maybe_open_alert_dialog(event, results: pd.DataFrame):
    """Open the add-alert modal when the user selects a new row. Guarded by the
    last-handled symbol so dismissing the dialog doesn't immediately reopen it."""
    try:
        rows = event.selection.rows
    except Exception:
        rows = []
    if not rows:
        st.session_state.pop("_alert_dlg_symbol", None)
        return
    row = results.iloc[rows[0]]
    symbol = str(row["Symbol"])
    if st.session_state.get("_alert_dlg_symbol") == symbol:
        return  # this selection was already handled (dialog open or dismissed)
    if not alerts_db.configured():
        st.info("Price alerts aren't configured yet — add SUPABASE_URL and "
                "SUPABASE_SERVICE_KEY to the app secrets to enable them.")
        return
    st.session_state["_alert_dlg_symbol"] = symbol
    current = float(row["LastClose"]) if pd.notna(row["LastClose"]) else 0.0
    high52 = float(row["52wHigh"]) if pd.notna(row["52wHigh"]) else None
    _alert_dialog(symbol, current, high52)


def _render_alerts_tab(snap: pd.DataFrame):
    st.subheader("🔔 Price Alerts")
    st.caption("⚡ Alerts are evaluated on **real-time** prices by the always-on "
               "alerter and pushed to Telegram the instant a level is crossed. "
               "Prices shown on this page are ~15 min delayed (reference only) — "
               "the alerts themselves are not.")
    st.caption("🎯 Each alert fires **once**, then retires (shown as *triggered*) — "
               "no repeat pings. Hit **Re-arm** to watch it again.")

    if not alerts_db.configured():
        st.info("Price alerts aren't configured. Add **SUPABASE_URL** and "
                "**SUPABASE_SERVICE_KEY** to the app secrets (Manage app ▸ Settings "
                "▸ Secrets) to enable creating and managing alerts.")
        return

    price_map = _snap_price_map(snap)

    # --- Add any stock by search ---
    with st.expander("➕ Add an alert (search any stock)", expanded=False):
        pick = st.selectbox(
            "Search by symbol", options=sorted(price_map.keys()), index=None,
            placeholder="Type a symbol, e.g. RELIANCE", key="alert_search_pick",
        )
        if pick:
            cur, hi = price_map.get(pick, (None, None))
            if cur is None:
                st.warning("No recent price for this symbol — enter levels manually.")
                cur = 0.0
            _alert_form(pick, cur, hi, key_prefix=f"search_{pick}")

    st.divider()

    # --- Existing alerts ---
    try:
        alerts = alerts_db.list_alerts()
    except Exception as e:
        st.error(f"Couldn't load alerts: {e}")
        return
    if not alerts:
        st.caption("No alerts yet. Add one above, or click a row in the Screener tab.")
        return

    st.markdown(f"##### Your alerts ({len(alerts)})")
    st.caption("Current = ~15 min delayed reference; your set levels are the other "
               "columns. ⚡ Alerts fire on real-time prices, not this delayed value.")

    def _fmt(v):
        return "—" if v is None else f"₹{float(v):,.2f}"

    widths = [2.0, 1.3, 1.3, 1.3, 1.2, 1.2, 0.7]
    hdr = st.columns(widths, vertical_alignment="center")
    for col, label in zip(hdr, ["Symbol", "Above", "Below", "Current",
                                 "Status", "", ""]):
        if label:
            col.caption(label)

    for a in alerts:
        cur = _live_price(a["symbol"]) or price_map.get(a["symbol"], (None, None))[0]
        c = st.columns(widths, vertical_alignment="center")
        sym_md = f"**{a['symbol']}**"
        if a.get("note"):
            sym_md += f"  \n:gray[{a['note']}]"
        active = a["active"]
        triggered = (not active) and a.get("last_alert_at")
        status = "✅ active" if active else ("🎯 triggered" if triggered else "⏸ paused")
        c[0].markdown(sym_md)
        c[1].write(_fmt(a["upper_price"]))
        c[2].write(_fmt(a["lower_price"]))
        c[3].write(_fmt(cur))
        c[4].write(status)
        btn = "Pause" if active else ("Re-arm" if triggered else "Resume")
        if c[5].button(btn, key=f"toggle_{a['id']}", use_container_width=True):
            if active:
                alerts_db.set_active(a["id"], False)
                st.toast(f"Paused {a['symbol']}", icon="🔔")
            else:
                alerts_db.rearm(a["id"])   # resume/re-arm: active + clear trigger
                st.toast(f"{'Re-armed' if triggered else 'Resumed'} {a['symbol']}",
                         icon="🔔")
            st.rerun()
        if c[6].button("🗑", key=f"delete_{a['id']}", use_container_width=True,
                       help=f"Delete the {a['symbol']} alert"):
            alerts_db.delete_alert(a["id"])
            st.toast(f"Deleted {a['symbol']} alert", icon="🗑️")
            st.rerun()


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
    # 52-week high is always the intraday high (NSE's official basis).
    basis = "high"

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
    if st.button("↻ Reload snapshot", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption("Results are instant — read from the nightly precomputed snapshot. "
               "It refreshes automatically after market close.")
    if st.button("Log out", use_container_width=True):
        st.session_state.auth_ok = False
        st.rerun()

snap, snap_as_of = load_snap()

tab_screen, tab_alerts = st.tabs(["📈 Screener", "🔔 Price Alerts"])

with tab_screen:
    # F&O (Filter 1) + Qty+Circuit (Filter 2) can never both be true — F&O stocks
    # have no price band, so band=20% is impossible for them.
    if fno_only and qty_circuit_only:
        st.warning(
            "⚠️ **Filter 1 (F&O) + Filter 2 (Qty + Upper circuit) will return 0 results.** "
            "F&O stocks have no price band, so none can have a 20% band. "
            "Use just one of the two."
        )

    if snap.empty:
        st.warning(
            "The daily snapshot isn't available yet. The nightly **build-snapshot** "
            "job populates it after market close — run that GitHub Action once if this "
            "persists."
        )
    elif run or st.session_state.get("has_run"):
        st.session_state.has_run = True
        results = screener.screen_snapshot(
            snap, basis=basis, min_days=int(min_days),
            band_low=float(band_low), band_high=float(band_high),
            fno_only=fno_only, qty_circuit_only=qty_circuit_only,
            qty_threshold=int(qty_threshold), qty_keep=qty_keep,
            listing_only=listing_only,
            listing_min_months=int(listing_min_months),
            listing_max_months=int(listing_max_months),
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("Priced universe", f"{len(snap):,}")
        c2.metric("Matches", f"{len(results):,}")
        c3.metric("As of", snap_as_of or "—")

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
            f"52w high older than **{int(min_days)}d**  •  "
            f"pullback **{band_low:g}–{band_high:g}%**  •  {opt_txt}"
        )

        if results.empty:
            st.warning("No stocks matched today's filters.")
        else:
            # Columns shown in the table + downloads (full `results` is still used
            # for the per-row quick-add, which reads LastClose/52wHigh).
            show_cols = ["Symbol", "52wHigh", "HighDate", "DaysSinceHigh",
                         "PctFromHigh", "AvgVol20d"]
            display = results[[c for c in show_cols if c in results.columns]]

            st.caption("💡 **Click any row** (checkbox on the left) to set a price "
                       "alert for that stock — a quick form pops up.")
            event = st.dataframe(
                display, use_container_width=True, hide_index=True,
                on_select="rerun", selection_mode="single-row",
            )
            _maybe_open_alert_dialog(event, results)   # full results (has LastClose)

            stamp = (snap_as_of or datetime.now(IST).strftime("%Y-%m-%d")).replace("-", "")
            d1, d2 = st.columns(2)
            d1.download_button(
                "⬇ Download CSV",
                display.to_csv(index=False).encode("utf-8"),
                file_name=f"nse_52wk_screener_{stamp}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            d2.download_button(
                "⬇ Download Excel",
                to_excel_bytes(display),
                file_name=f"nse_52wk_screener_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
    else:
        st.info("Pick your settings on the left and hit **Run screener**.")

with tab_alerts:
    _render_alerts_tab(snap)
