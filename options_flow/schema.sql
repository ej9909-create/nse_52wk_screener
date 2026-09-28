-- Live options-flow scan. One row per option contract; the collector upserts
-- the whole scoped universe (~2,500 rows) every ~2 min, keyed on token, so this
-- table always holds the latest snapshot. Run once in the Supabase SQL editor.

create table if not exists options_scan (
  token           text primary key,
  as_of           timestamptz,
  symbol          text,
  underlying      text,
  kind            text,            -- CE / PE
  strike          numeric,
  expiry          date,
  dte             integer,

  ltp             numeric,
  chg_pct         numeric,         -- vs previous close
  oi              bigint,
  oi_chg_day      bigint,          -- vs day-open OI (~prev-day OI)
  oi_chg_day_pct  numeric,
  volume          bigint,
  vol_oi          numeric,
  notional        numeric,         -- traded value proxy (ltp * volume)

  iv              numeric,
  delta           numeric,
  gamma           numeric,
  vega            numeric,

  d5_oi           bigint,          -- intraday deltas (5/10/15 min)
  d10_oi          bigint,
  d15_oi          bigint,
  d5_price_pct    numeric,
  d10_price_pct   numeric,
  d15_price_pct   numeric,
  d5_iv           numeric,
  d10_iv          numeric,
  d15_iv          numeric,
  d5_vol          bigint,
  d10_vol         bigint,
  d15_vol         bigint,

  buildup         text,            -- Long Buildup / Short Buildup / Short Covering / Long Unwinding
  spread_pct      numeric,
  forward         numeric          -- future price used for IV
);

create index if not exists options_scan_underlying_idx on options_scan (underlying);
create index if not exists options_scan_asof_idx on options_scan (as_of);
