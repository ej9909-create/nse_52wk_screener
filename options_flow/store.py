"""
Supabase writer for the live options scan.

The collector (VM) upserts the whole enriched scan each cycle into one table
(`options_scan`), keyed by token — so the table always holds the latest snapshot
(~2,500 rows), which the Streamlit app reads. Mirrors the alerter's Supabase auth
(SUPABASE_URL + SUPABASE_SERVICE_KEY from env).
"""
import os

_client = None
CHUNK = 500


def _get_client():
    global _client
    if _client is None:
        from supabase import create_client
        url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
        if not (url and key):
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
        _client = create_client(url, key)
    return _client


def upsert_scan(rows):
    """Upsert enriched contract rows (list of dicts) keyed on token."""
    cli = _get_client()
    n = 0
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        cli.table("options_scan").upsert(chunk, on_conflict="token").execute()
        n += len(chunk)
    return n


def prune_stale(before_iso):
    """Remove rows older than a cutoff (contracts that dropped out of scope,
    e.g. after an expiry roll) so the table doesn't accumulate dead tokens."""
    _get_client().table("options_scan").delete().lt("as_of", before_iso).execute()
