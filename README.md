# asset_universe

A framework for deciding which assets belong in our market-making universe, and for
rotating them in and out on evidence rather than on the last thing that happened.

Nightly it pulls every instrument on five venues from their REST APIs, normalises to
real USD, scores each **asset** on market structure, writes the result to ClickHouse,
and rebuilds an HTML report that is pushed to Hub. A web dashboard sits on top for
calibration.

**There is no local state.** Stages hand off through ClickHouse
(`asset_universe_cache`), so any stage can run on any box that can reach the server.

> **The cron is live on `Bruce`** (`vhuang`, 04:00 America/New_York = 08:00–09:00 UTC).
> `crontab -l` to see it. Everything below is reproducible on any box, but the
> scheduled run and its accumulating history exist only there — see §Running elsewhere.

---

## What it decides

Per **asset**, not per instrument. More venues carrying the same underlying means more
places to quote and hedge, so the unit of the decision is `BTC`, not `BIN-P-BTCUSDT`.

Four verdicts: `keep` / `drop` for what we trade, `add` / `watch` for what we don't.
Assets pass four hard gates (volume, persistence, venue count, open interest) and are
ranked on a weighted composite of **flow, structure, carry and friction** — every
component a cross-sectional percentile. New listings run in a separate lane judged on
days since volume onset, because a name listed mid-window has no 30 days to be
persistent over.

**Our own P&L is deliberately not an input.** The universe is chosen on market
structure alone, so the screen cannot learn to like whatever the current strategy
already does well on. Full formulas: the **Method** tab in the dashboard, or
`FRAMEWORK.md`.

---

## Quick start

```bash
git clone <this> && cd asset_universe
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp exchange_api_fetcher/general_config.json.example \
   exchange_api_fetcher/general_config.json     # set your proxy, or {} for none

export CH_URL=http://192.168.50.39:8123/ CH_USER=vinny CH_PASS=...

python ch_schema.py --create      # one-time; idempotent, safe to re-run
python run_screen.py              # ~25 min: REST pull -> asset_universe_cache
python ch_load.py                 # cache -> symbol_stats, with validation gates
python history.py                 # drop / new-listing history
python build_report.py            # -> reports/universe_review.html + Hub
python dashboard/app.py           # -> http://127.0.0.1:8815
```

Or all of it the way the cron does:

```bash
./daily_job.sh                    # full run
./daily_job.sh --reuse-deep       # re-score cached history, no REST pull (fast)
./daily_job.sh --load-only        # cache -> ClickHouse only (seconds)
```

---

## Layout

| path | what it is |
|---|---|
| `run_screen.py` | the pipeline: specs → asset keys → snapshot → shortlist → 30d deep history → metrics |
| `ch_schema.py` | ClickHouse DDL. Idempotent, with column migration; `--recreate` for sorting-key changes |
| `ch_load.py` | cache → `symbol_stats`: conforming, quarantine, validation gates |
| `ch_cache.py` | the stage handoff: frames as parquet blobs in ClickHouse, keyed by `run_date` |
| `hub.py` | pushes the HTML report to Hub (`additional_docs/HUB_ACCESS.md`) |
| `history.py` | what we stopped trading, and what is newly listed anywhere |
| `build_report.py` | scoring (`score()`, the single source of truth) + the static HTML report |
| `daily_job.sh` | cron entry point: `flock`, logging, stage sequencing, log rotation |
| `crontab` | the installed schedule, version-controlled |
| `dashboard/` | FastAPI + vanilla JS calibration UI — see `dashboard/README.md` |
| `exchange_api_fetcher/` | per-venue REST adapters, symbology, FX, capability probes |
| **`FRAMEWORK.md`** | why these metrics, the two lanes, hysteresis, and §3.0 where the level/trend split went wrong |
| **`PIPELINE.md`** | schema, grain, symbology rules, validation gates |
| **`DATA_COVERAGE.md`** | per-venue endpoint registry and the gaps — read this before trusting a column |
| **`GRAFANA.md`** | if you'd rather use Grafana than the bundled dashboard |

`reports/*.html` and `logs/` are gitignored: every one is regenerable, and
**ClickHouse is the source of truth**, not the working directory. `run_screen.py
--data-dir DIR` still mirrors frames to parquet, but only as a debugging aid — no
stage reads local disk by default.

---

## ClickHouse (`symbol_stats`)

| table | grain | note |
|---|---|---|
| `instrument_daily` | venue × symbol × market_type × date | the permanent fact table |
| `instrument_ref` | venue × symbol × kind | dimension: identity, tick, fees, listing dates |
| `screen_runs` | asset × run_date | **verdict history — only exists from the day the cron started** |
| `screen_runs_venue` | asset × venue × symbol × market_type × run_date | per-exchange breakdown |
| `traded_history` | internal instrument × run_date | active vs dropped from `symbols_record` |
| `asset_listings` | asset × venue × kind × run_date | listing dates and per-venue coverage |
| `asset_daily`, `asset_listing_matrix` | views | roll-ups; no backfill to worry about |
| `load_runs`, `load_rejects` | per load | "did last night run, was it clean" |

### The frame cache (`asset_universe_cache`)

One table, `frames`: the seven DataFrames `run_screen.py` produces, one row each per
`run_date`, stored as parquet bytes.

```sql
SELECT run_date, frame, rows, cols, round(nbytes/1e6, 2) AS mb
FROM asset_universe_cache.frames ORDER BY run_date DESC, frame
```

~4.3 MB per run, `TTL 180 days` (`AU_CACHE_TTL_DAYS`). Blobs rather than seven typed
tables because **nothing SELECTs a column out of these** — every consumer reads the
whole frame into pandas — so typed tables would buy nothing and cost a migration each
time `run_screen` gains a column. Parquet also round-trips dtypes exactly, which
matters: several of these frames carry nullable ints and all-NaN float columns that a
naive columnar round-trip quietly changes. What you *do* query is `symbol_stats`.

`python ch_cache.py --list` / `--check` to see what is cached and whether the last run
is complete. A `run_date` missing any of `instrument_ref`, `deep_daily`,
`asset_metrics`, `venue_metrics` is treated as an incomplete run and skipped, so a
fetch that died halfway is never loaded as though it were whole.

Panels need **neither `FINAL` nor `argMax`** — the loader `OPTIMIZE`s these tables
after each run, so raw counts already equal deduplicated counts. That is affordable
only because they are small; the loader refuses above 5M rows.

**Sorting keys are deduplication keys.** `ReplacingMergeTree` silently collapses rows
sharing an `ORDER BY` tuple. Each key here has a non-obvious member found by loading
real data and counting rows: `instrument_ref` needs `kind` (spot and perp share a
symbol string — `BIN BTCUSDT` is both, 1,381 collisions); `screen_runs_venue` needs
`symbol` **and** `market_type`; `instrument_daily` needs `market_type`. `ch_load`
asserts this on every load, before inserting.

**`vol_trend` (7d/30d) is a level, not a direction — and neither is a 30-day slope.**
Both stay large and *rising* for weeks after a volume spike while the asset collapses,
which left the decay rule structurally unable to fire on a post-pump fade. Read
`vol_off_peak` + `vol_peak_age` instead. Why, and what the alternatives cost:
`FRAMEWORK.md` §3.0.

---

## Operations

```sql
-- did last night's job run, and was it clean?
SELECT run_ts, table, rows_loaded, rows_rejected, status, notes
FROM symbol_stats.load_runs ORDER BY run_ts DESC LIMIT 10
```

Logs: `logs/cron.log` (wrapper) and `logs/daily_<stamp>.log` (per-run, self-pruning
after 30 days).

Exit codes — anything non-zero is worth an alert:

| code | meaning |
|---|---|
| 0 | ok |
| 2 | nothing complete in the frame cache (run `run_screen.py` first) |
| 3 | too many instruments failed identity resolution (>2%) — nothing loaded |
| 4 | a validation gate failed — data loaded but treat it as suspect |
| 5 | a stage crashed |
| 75 | another run still holds the lock |

Four gates run on every load, all verified to fire against corrupted data: identity
resolution, **cross-venue price agreement** on BTC/ETH/SOL (>25 bps fails),
**volume self-consistency** (`vol_usd / (vol_base × px_close × fx)` is VWAP/close; the
per-venue × market median must stay within 1.5×, and today sits at 1.000–1.002), and per-venue instrument-count drift. The price
gate caught the UTC+8 candle trap; the volume gate caught OKX spot storing quote volume
in `vol_base`. Both now run nightly instead of once. Full list, including the gates
still designed but unbuilt: `PIPELINE.md` §8.

---

## Running elsewhere

Nothing is machine-specific except the cron and the accumulated history. To stand it
up on another box: install, set `CH_URL`/`CH_USER`/`CH_PASS`, run `ch_schema.py
--create`, then `./daily_job.sh`. Point it at a different database with `CH_DB` if you
don't want to share `symbol_stats`, or `AU_CACHE_DB` for the frame cache.

Because the handoff is in ClickHouse, the stages do not have to share a machine:
`run_screen.py` on one box and `ch_load.py` / `build_report.py` on another both see
the same `run_date`. `--load-only` and `--reuse-deep` work from a box that never ran
the fetch.

**Do not install a second cron against the same database.** Two writers would
interleave `run_date` snapshots. `flock` only guards one machine.

To move the schedule, edit `crontab` in this repo and `crontab crontab` — installing
from the file avoids the editor wrapping the long command line, which is what a
`bad minute` error means.

---

## Known gaps

Kept here rather than in a tracker because each one changes how you read a number.

- **`ON` is two different assets** under one `asset_key` — OKX's `ON-USDT-SWAP` at
  \$76.84 vs everyone else's at \$0.272, 39,762 bps median disagreement over 31 days.
  Its venue count and volume are overstated. Needs a within-`asset_key` price guard in
  `run_screen.py`; the between-key check misses it.
- **`OKX-P-BTCUSDC` / `OKX-P-ETHUSDC`** take real fills and receive funding, but OKX's
  public API lists **no** USDC-settled swap on any regional domain. Native ID unknown.
- **Bitget publishes no OI history** — its OI only accrues from our snapshots. It
  holds >20% of live OI for 101 of 126 assets, so OI charts read low.
- **Trade counts are Binance-only.** Tagged with their source, never imputed.
- **Spot listing dates exist only on OKX.** Other venues' spot listings are dated from
  when our own snapshot first saw them (2026-08-07 onward). `listed_src` says which.
- **Gate's USDC futures tickers now require an API key** (was public until ~2026-08).
  Gate USDC perps are missing from the snapshot.
- **Spread is sampled, not time-weighted**, and there is no spread history before the
  BBO collector runs. See `PIPELINE.md` §9 step 2 — still open.
