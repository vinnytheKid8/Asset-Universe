# Universe calibration dashboard

A local web UI over `symbol_stats`. Reads live from ClickHouse, writes nothing.

```bash
source /home/vhuang/darius/.venv/bin/activate
python /home/vhuang/darius/darius_analysis/asset_universe/dashboard/app.py
# -> http://127.0.0.1:8815

python dashboard/app.py --host 0.0.0.0        # serve it from the ClickHouse box
python dashboard/app.py --port 9000 --reload  # dev
```

No build step, no npm, no CDN. Four files: a FastAPI server and three static
assets. Charts are hand-rolled SVG because the ClickHouse box may have no
outbound internet and a CDN script tag is one more thing to break at 4am.

---

## What it's for

Grafana answers "what changed overnight". This answers a question Grafana
structurally can't: **"what would the universe look like if the ADV gate were
$20M instead of $5M?"** That is what calibration actually is, and it needs the
scoring function in the loop, not a chart of its output.

So the **Screen** tab puts every gate, threshold and composite weight on a
slider. Move one and the verdicts recompute, the tiles show
`50 → 39 (−11)`, the assets that changed verdict get a dark ring in the scatter,
and a table names the binding constraint for each one.

**The recompute happens server-side, calling `build_report.score()`** — the same
function the nightly loader and the HTML report use. It is parameterised
(`build_report.DEFAULTS`) rather than duplicated. A JS reimplementation would
have drifted from the real screen inside a week, and then the tool would be
confidently telling you about a universe the pipeline doesn't produce.

---

## Tabs

| tab | what's there |
|---|---|
| **Screen** | the calibration surface. Sliders, verdict tiles with deltas vs baseline, a configurable scatter (log toggles, gate reference lines), the movers table, the full decision list, and a collapsible **Method section** with every formula, the current slider values substituted in. |
| **Assets** | all 130 assets × 51 columns (perp + spot), sortable, filterable by class and traded/not, CSV export. Click a row for detail. |
| **Asset detail** | per-venue time series — volume stacked, OI stacked, venue share %, funding APR, scale-normalised price, cross-venue agreement — plus composite history across runs and every instrument on every venue (28 columns, banded). `log volume / OI` switches those two to a log axis (and to lines, since a stacked log axis is not a sum). |
| **Venues** | all 1,327 asset × venue × market rows, 27 columns. Filter by perp/spot. Filter `we don't quote it` to see the legs we're missing on assets we already trade. |
| **Changes** | dropped from the config (dated from `symbols_record`), newly listed anywhere with a per-venue P/S matrix, announced-for-the-future listings, venue gaps. |
| **Data health** | `load_runs` (did the nightly job run, was it clean), price agreement over time, instruments per venue, worst disagreements, duplicate tickers, load rejects, unmapped internal symbols. |
| **SQL** | read-only escape hatch for anything the tabs don't cover. `SELECT`/`WITH` only, 5,000 rows. |

Everything is clickable through to asset detail. Charts have hover tooltips and
click-to-toggle legends. Light/dark via the ◑ button, and the palette is the
CVD-validated categorical order. `?asset=TUT` deep-links to one asset's detail page.

**Columns are banded by how fast they move.** Tinted = *now* (last 24h, last complete
day, change vs yesterday, distance off the 14d peak). Greyed = the 30-day medians the
screen actually scores on. Both matter, but they are not equals and the table should
not read as if they were. Direction is carried by an **arrow first** and colour second —
teal up, orange down — so it survives both greyscale and colour-blindness.

---

## Sharing a calibration

The **Link** button copies a URL with the current parameters:

```
http://127.0.0.1:8815/?g_adv=50000000&g_venues=4
```

Open it and the sliders, verdicts and movers come back exactly. That's the unit
of an argument about the universe — not a screenshot.

---

## Things worth knowing

**Verdict history is thin.** `screen_runs` has one row per asset per nightly run,
so the composite-history chart and the state timeline only become useful once the
cron has run for a week or two. The empty state says so rather than drawing a
one-point line.

**`ON` will look broken on the health tab, and it is.** Worst cross-venue price
disagreement 39,860 bps, median 39,762 over 31 days — that is not a listing-day
artifact, it's two different assets sharing one `asset_key` (OKX's `ON` at $76.84
vs everyone else's at $0.272). Its venue count and volume in the screen are
overstated until that's fixed upstream in `run_screen.py`.

**`vol_trend` (7d/30d) is a level ratio, not a change — don't read it as direction.**
After a pump the 30d median lags for weeks, so the ratio stays enormous *and rising*
while the asset collapses. BLESS on 2026-08-12 read **38.8×** with volume 90% off its
peak and falling. Because `decaying` required `vol_trend < 0.6`, the drop rule could
never fire on a post-pump collapse — the case you most want out of.

`vol_slope_pct_day` has the same defect for the same reason — it regresses over all 30
days, so it read **+19.6 %/day for BLESS** while BLESS collapsed.

Two traps make the obvious replacements wrong:

**Window.** Any window still containing a pump reports "up" for weeks after it ends. TUT
was halving daily and read **+97 %/day** on a 7d slope, **+74 %/day** on a 5d compound
rate, **+14,287%** week-over-week — all arithmetically true, none of them answering "is it
rolling over". A 5-day average of daily changes cannot fix this: the geometric mean of n
daily changes *is* the endpoint-to-endpoint rate.

**Calendar.** Volume has a large weekday cycle and it is brutal on RWA — **MU trades
$1.2–3.0B Mon–Fri and $96–433M Sat–Sun, a 20× swing**; BTC is 2–3×. On a Monday every RWA
name shows a +1000% "1d change" that is pure calendar.

So the promoted band is:

| | meaning | TUT | BLESS |
|---|---|---|---|
| `Δ 1d` | last / prev day — **not** calendar-adjusted, weekday shown in `Through` | −58% | +14% |
| `vs same wkday` | last / same weekday a week ago — calendar cancels | +1144% | −62% |
| `vs 14d peak` | last vs trailing 14d max | **−83%** | **−90%** |
| `peak age` | days since that peak | **2d** | **6d** |

`vs 14d peak` + `peak age` is the pair that answers the question, because it measures from
the high rather than over a window. TUT reads "up 11× on the week, but 83% off a peak set
two days ago" — which is exactly what is happening. `Slope 7d` and `Δ 3d/7d` are demoted to
the slow band: calendar-neutral and useful for the week's shape, but not a "now" read.

`decay_off_peak` adds a second decay path off `vol_off_peak`. It ships **off** (`0.0`)
because it changes which assets get dropped, and that is a trading decision, not a
default. Set it to `0.25` to see: 11 assets flip `keep → drop`.

**The ADV tile and the volume chart can differ by 1000× and both be right.** The tile is a
30-day *median*, deliberately robust to a pump; the chart is the daily series. TUT on
2026-08-09 had a median of $2.8M and a single day of $2.9B. The tile now prints
`last $X (Nx median)` next to the median so the gap is legible, and the log toggle makes
the pre-spike history readable instead of a flat line on the floor. When they disagree,
suspect a pump before you suspect a unit bug — then confirm with the health tab.

**The `/api/query` guard is a guard, not a sandbox.** It rejects anything that
isn't `SELECT`/`WITH` and blocks DDL/DML keywords, which is enough for a local
tool. It is not enough to expose to an untrusted network — if you bind
`--host 0.0.0.0`, put it behind something that authenticates.

**Bitget contributes no OI history**, so OI charts read low for any asset where
BGT carries a large share (>20% for 101 of 126 assets). That closes once the
snapshot collector from `PIPELINE.md` §9 runs.

---

## Files

```
dashboard/
  app.py              FastAPI server + ClickHouse queries + the /api/screen recompute
  static/index.html   layout
  static/app.js       state, tables, tabs, the knobs
  static/charts.js    SVG line / stacked area / scatter / hbar, ~250 lines
  static/style.css    theme tokens, light + dark
```

One CSS rule is load-bearing and easy to delete by accident: `min-width:0` on
grid and flex children. Without it a wide table refuses to shrink, stretches the
page instead of scrolling inside its own wrapper, and silently clips the stat
tiles and the right-hand side of every chart.

---

## If the UI looks broken after an edit

Hard-reload (Ctrl-Shift-R). A cached `app.js` is indistinguishable from a broken
feature: the markup is current, the click does nothing, and the server log is clean.
The app now sends `Cache-Control: no-store` on `/` and `/static/*` so this should not
recur, but a browser that cached the bundle *before* that header existed keeps it
until you force a reload.
