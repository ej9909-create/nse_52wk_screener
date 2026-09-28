"""
Read side of the live options-flow scan (the `options_scan` table the VM collector
upserts every ~2 min). Same Supabase creds as the price alerts (st.secrets on
Cloud, env locally). Degrades gracefully when Supabase isn't configured.
"""
from __future__ import annotations

import os

try:
    from supabase import create_client
    _HAS_SUPABASE = True
except Exception:
    _HAS_SUPABASE = False

_client = None
_PAGE = 1000


def _cred(name: str) -> str | None:
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name)


def configured() -> bool:
    return _HAS_SUPABASE and bool(_cred("SUPABASE_URL")) and bool(
        _cred("SUPABASE_SERVICE_KEY"))


def _get_client():
    global _client
    if _client is None:
        url, key = _cred("SUPABASE_URL"), _cred("SUPABASE_SERVICE_KEY")
        if not (_HAS_SUPABASE and url and key):
            raise RuntimeError("Supabase is not configured.")
        _client = create_client(url, key)
    return _client


def load_scan():
    """All rows of options_scan as a DataFrame (paginated past PostgREST's 1000
    row cap). Empty df if unconfigured/empty."""
    import pandas as pd
    if not configured():
        return pd.DataFrame()
    cli = _get_client()
    rows, start = [], 0
    while True:
        r = cli.table("options_scan").select("*").range(start, start + _PAGE - 1).execute()
        d = r.data or []
        rows.extend(d)
        if len(d) < _PAGE:
            break
        start += _PAGE
    return pd.DataFrame(rows)
