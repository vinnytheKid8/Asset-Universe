#!/usr/bin/env bash
# Nightly universe pipeline: fetch -> score -> load -> report.
#
#   ./daily_job.sh              full run (refetches 30d of daily history)
#   ./daily_job.sh --reuse-deep skip the REST pull, re-score cached history
#   ./daily_job.sh --load-only  parquet -> ClickHouse only
#
# Install (runs 03:20 UTC daily; the machine's crontab is in local time, so check
# `date` before picking the hour):
#
#   crontab -e
#   20 3 * * *  /home/vhuang/darius/darius_analysis/asset_universe/daily_job.sh >> \
#               /home/vhuang/darius/darius_analysis/asset_universe/logs/cron.log 2>&1
#
# Exit codes: 0 ok | 2 missing artifacts | 3 too many unresolved instruments
#             4 validation gate failed | 5 a stage crashed | 75 already running
#
# Why 03:20 UTC: venue daily candles for the previous UTC day need to have
# settled everywhere, and OKX/Bitget close their native day at 16:00 UTC (we ask
# for 1Dutc, but a late-arriving bar is still a real risk). Anything after ~02:00
# UTC is safe; before 01:00 is not.

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="/home/vhuang/darius/.venv"
LOGDIR="$DIR/logs"
LOCK="$DIR/.daily_job.lock"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
LOG="$LOGDIR/daily_${STAMP}.log"

mkdir -p "$LOGDIR"

# One run at a time. A slow REST pull overlapping the next night's job would
# interleave writes to data/*.parquet and load a half-written screen.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) another daily_job is running (lock $LOCK), exiting" | tee -a "$LOG"
    exit 75
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
cd "$DIR" || exit 5

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }

run_stage() {
    local name="$1"; shift
    log "=== $name ==="
    if ! "$@" >>"$LOG" 2>&1; then
        local rc=$?
        log "STAGE FAILED: $name (exit $rc)"
        tail -25 "$LOG" >&2
        return "$rc"
    fi
    return 0
}

MODE="${1:-}"
t_start=$SECONDS

case "$MODE" in
    --load-only) ;;
    --reuse-deep)
        run_stage "screen (cached history)" python run_screen.py --reuse-deep || exit 5
        ;;
    *)
        # The REST pull is the long pole (~10-15 min for ~650 instruments x 30d).
        run_stage "screen (full fetch)" python run_screen.py || exit 5
        ;;
esac

run_stage "clickhouse load" python ch_load.py
rc=$?
if [ "$rc" -ne 0 ]; then
    log "LOAD FAILED (exit $rc) - ClickHouse may hold a partial or suspect load"
    exit "$rc"
fi

# Drop / new-listing history reads symbols_record and the ref snapshots. Downstream
# of the load (it needs instrument_ref in ClickHouse), and never blocks the report.
run_stage "change history" python history.py || log "WARNING history build failed"

# The HTML report is downstream of the load and never blocks it.
run_stage "html report" python build_report.py || log "WARNING report build failed"

log "done in $((SECONDS - t_start))s -> $LOG"

# Keep 30 days of logs.
find "$LOGDIR" -name 'daily_*.log' -mtime +30 -delete 2>/dev/null

exit 0
