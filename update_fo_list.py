"""
Refresh the bundled list of F&O (futures & options) underlying stocks.

NSE publishes the F&O market-lots file, which lists every underlying that has
derivatives. We keep just the symbols -> data/fo_stocks.csv. The screener uses it
to flag `is_fno` per stock.

Run from a machine that can reach NSE (an India IP is most reliable), then commit
data/fo_stocks.csv. F&O inclusions/exclusions change ~monthly, so occasional
refreshes are enough.

    python update_fo_list.py
"""

import io
import sys

import pandas as pd
import requests

FO_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
OUT_PATH = "data/fo_stocks.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def fetch() -> pd.DataFrame:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://www.nseindia.com/", timeout=15)
    except Exception:
        pass
    r = s.get(FO_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [str(c).strip() for c in df.columns]
    # the symbol column is typically "SYMBOL" (sometimes with stray spaces)
    sym_col = next((c for c in df.columns if c.upper() == "SYMBOL"), None)
    if sym_col is None:
        raise ValueError(f"no SYMBOL column found; got {list(df.columns)}")
    syms = (
        df[sym_col].astype(str).str.strip().str.upper()
        .replace({"": pd.NA}).dropna().drop_duplicates()
    )
    # drop obvious index underlyings; stock intersection with the equity
    # universe drops the rest anyway.
    drop = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SYMBOL"}
    syms = syms[~syms.isin(drop)]
    return pd.DataFrame({"Symbol": sorted(syms)})


def main():
    try:
        out = fetch()
    except Exception as e:
        print(f"ERROR: could not fetch F&O list from NSE: {e}", file=sys.stderr)
        print("Tip: run this from an India IP.", file=sys.stderr)
        sys.exit(1)
    out.to_csv(OUT_PATH, index=False)
    print(f"Wrote {len(out)} F&O symbols to {OUT_PATH}")


if __name__ == "__main__":
    main()
