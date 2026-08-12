# tardis_stats

Tardis-only fetch layer for a multi-exchange perp/spot stats dashboard.
See ../DATA_SOURCE_SPEC.md for the endpoint/dataset spec.

    pip install requests pandas pyarrow      # pyarrow optional (parquet)
    export TARDIS_API_KEY=TD....

    python -m tardis_stats.cli check                       # key + plan + exportedUntil per feed
    python -m tardis_stats.cli universe --out-file data/universe.csv
    python -m tardis_stats.cli contracts                   # contract multipliers (pro plan)
    python -m tardis_stats.cli backfill --days 14 --workers 4
    python -m tardis_stats.cli day --feed okex-swap --date 2026-07-28
    python -m tardis_stats.cli dashboard                   # data/daily.csv + data/dashboard.csv

Modules: config (feed registry) · client (HTTP + streaming gzip) ·
aggregate (single-pass reducers) · contracts (multipliers + USD normalisation) · pipeline (orchestration) ·
store (parquet/csv.gz output) · rollup (daily + wide dashboard frame) · cli.
