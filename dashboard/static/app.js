import { timeSeries, scatter, fmtUsd, fmtNum } from '/static/charts.js';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const api = async (p, q) => {
  const u = new URL(p, location.origin);
  for (const k in (q || {})) if (q[k] != null) u.searchParams.set(k, q[k]);
  const r = await fetch(u);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const showErr = e => { $('#err').innerHTML = `<div class="err">${e.message || e}</div>`;
  console.error(e); };

const S = { meta: null, screen: null, venues: null, health: null, params: {},
            vfilter: new Set(['keep', 'add', 'watch', 'drop']), movedOnly: false,
            detail: null, pending: null, sort: {}, mktFilter: '' };

const VCOL = { keep: 'var(--keep)', add: 'var(--add)', watch: 'var(--watch)', drop: 'var(--drop)' };
const pct1 = v => v == null ? '—' : (v * 100).toFixed(0) + '%';

/* ===================== knobs ===================== */
// [key, label, min, max, step, scale] - scale 'log' maps the slider geometrically
const KNOBS = [
  ['__g', 'Hard gates'],
  ['g_adv', 'ADV floor', 1e5, 1e9, null, 'log', v => '$' + fmtUsd(v).slice(1)],
  ['g_persist', 'Days above $5M', 0, 1, .05, null, pct1],
  ['g_venues', 'Perp venues', 1, 6, 1, null, v => v],
  ['g_oi', 'Open interest floor', 1e5, 1e9, null, 'log', v => '$' + fmtUsd(v).slice(1)],
  ['__d', 'Drop rule'],
  ['n_fail_drop', 'Gates failed to drop', 1, 4, 1, null, v => '≥ ' + v],
  ['decay_trend', 'Decay: 7d/30d below', .2, 1.2, .05, null, v => (+v).toFixed(2)],
  ['decay_slope', 'Decay: %/day below', -10, 0, .5, null, v => (+v).toFixed(1) + '%'],
  // stored as a share of the peak (off_peak < v), shown as the drop from it so the
  // knob reads in the same units as the "vs 14d peak" column
  ['decay_off_peak', 'Fast decay: below 14d peak by', 0, .8, .05, null,
   v => +v === 0 ? 'off' : '≥ ' + ((1 - v) * 100).toFixed(0) + '%'],
  ['decay_r37', 'Fast decay: 3d/7d below', .5, 1.5, .05, null, v => (+v).toFixed(2)],
  ['__l', 'Lanes'],
  ['lane_days', 'Lane A: days since onset', 3, 60, 1, null, v => v + 'd'],
  ['new_listing_days', 'Flag as new listing', 3, 60, 1, null, v => v + 'd'],
  ['tick_pinned_ratio', 'Tick-pinned if spread ≤ tick ×', 1, 2, .05, null, v => '×' + (+v).toFixed(2)],
  ['__w', 'Composite weights'],
  ['w_flow', 'Flow', 0, 1, .05, null, v => (+v).toFixed(2)],
  ['w_structure', 'Structure', 0, 1, .05, null, v => (+v).toFixed(2)],
  ['w_carry', 'Carry', 0, 1, .05, null, v => (+v).toFixed(2)],
  ['w_friction', 'Friction', 0, 1, .05, null, v => (+v).toFixed(2)],
];

function buildKnobs() {
  const D = S.meta.defaults, host = $('#knobs');
  host.innerHTML = '';
  for (const k of KNOBS) {
    if (k[0].startsWith('__')) {
      const g = document.createElement('div');
      g.className = 'knobgroup'; g.innerHTML = `<h3>${k[1]}</h3>`;
      host.appendChild(g); continue;
    }
    const [key, label, min, max, step, scale, fmt] = k;
    const d = document.createElement('div'); d.className = 'knob'; d.dataset.key = key;
    const v = S.params[key] ?? D[key];
    const pos = scale === 'log'
      ? (Math.log10(v) - Math.log10(min)) / (Math.log10(max) - Math.log10(min)) * 1000 : v;
    d.innerHTML = `<label>${label}<b id="lb-${key}">${fmt(v)}</b></label>
      <input type="range" id="in-${key}" min="${scale === 'log' ? 0 : min}"
             max="${scale === 'log' ? 1000 : max}" step="${scale === 'log' ? 1 : step}"
             value="${pos}">`;
    host.appendChild(d);
    d.querySelector('input').addEventListener('input', ev => {
      let val = +ev.target.value;
      if (scale === 'log')
        val = Math.pow(10, Math.log10(min) + val / 1000 * (Math.log10(max) - Math.log10(min)));
      S.params[key] = val;
      $(`#lb-${key}`).textContent = fmt(val);
      d.classList.toggle('changed', Math.abs(val - D[key]) > 1e-9);
      refreshScreen();
    });
  }
}

let timer = null;
function refreshScreen() { clearTimeout(timer); timer = setTimeout(loadScreen, 130); }

/* ===================== screen ===================== */
async function loadScreen() {
  try {
    const q = Object.assign({}, S.params);
    const rd = $('#run-date').value; if (rd) q.run_date = rd;
    S.screen = await api('/api/screen', q);
    $('#err').innerHTML = '';
    drawTiles(); drawScatter(); drawMoved(); drawScreenTable();
    drawMethod();
  } catch (e) { showErr(e); }
}

function drawTiles() {
  const c = S.screen.counts, b = S.screen.base_counts;
  const adv = S.screen.rows.reduce((a, r) => a + (r.vol_usd_med30 || 0), 0);
  const tiles = [
    ['keep', 'keep', c.keep || 0, b.keep || 0, 'we trade, passes'],
    ['add', 'add', c.add || 0, b.add || 0, 'candidates over the bar'],
    ['watch', 'watch', c.watch || 0, b.watch || 0, 'close but not yet'],
    ['drop', 'drop', c.drop || 0, b.drop || 0, 'we trade, failing'],
  ].map(([cls, k, n, bn, sub]) => {
    const d = n - bn;
    return `<div class="tile ${cls}"><div class="k">${k}</div><div class="v">${n}</div>
      <div class="d ${d > 0 ? 'up' : d < 0 ? 'dn' : ''}">${d === 0 ? sub
        : `${bn} → ${n} (${d > 0 ? '+' : ''}${d})`}</div></div>`;
  }).join('');
  $('#tiles').innerHTML = tiles +
    `<div class="tile"><div class="k">universe ADV</div><div class="v">${fmtUsd(adv)}</div>
      <div class="d">${S.screen.rows.length} assets screened</div></div>
     <div class="tile"><div class="k">add bar</div><div class="v">${S.screen.add_bar.toFixed(1)}</div>
      <div class="d">median composite of keeps</div></div>`;
}

const AXES = [
  ['vol_usd_med30', 'ADV, 30d median (USD)'], ['vol_usd_mean30', 'ADV, 30d mean (USD)'],
  ['oi_usd_med30', 'Open interest, 30d median (USD)'], ['oi_to_adv', 'OI / ADV'],
  ['composite', 'Composite score'], ['spread_bps_med', 'Spread (bps)'],
  ['tick_bps_med', 'Tick (bps)'], ['edge_headroom_bps', 'Edge headroom = spread − tick (bps)'],
  ['n_venues', 'Perp venues'], ['venues_spot', 'Spot venues (listed)'],
  ['spot_venues_live', 'Spot venues (with flow)'],
  ['spot_vol_usd_med30', 'Spot ADV, 30d median (USD)'],
  ['perp_spot_ratio', 'Perp ADV / spot ADV'], ['spot_share', 'Spot share of total'],
  ['vol_off_peak', 'Volume vs 14d peak'], ['vol_peak_age', 'Days since 14d peak'],
  ['vol_dow7', 'Volume vs same weekday last week'],
  ['vol_slope7_pct_day', 'Volume slope, last 7d (%/day)'],
  ['vol_r37', 'Volume 3d/7d (fast direction)'], ['vol_d1', 'Volume day-over-day'],
  ['vol_trend', '7d/30d volume ratio (LEVEL, not direction)'],
  ['days_above_5m', 'Share of days above $5M'],
  ['venue_hhi', 'Venue concentration (HHI)'], ['trades_med30', 'Trades/day (Binance)'],
  ['fund_spread_bps_med', 'Cross-venue funding spread (bps)'],
  ['days_since_onset', 'Days since volume onset'], ['age_days', 'Age (days)'],
];

function fillAxisSelects() {
  for (const [id, def] of [['#sx', 'vol_usd_med30'], ['#sy', 'oi_usd_med30']]) {
    $(id).innerHTML = AXES.map(([k, l]) => `<option value="${k}">${l}</option>`).join('');
    $(id).value = def;
  }
}

function screenRows() {
  return S.screen.rows.filter(r => S.vfilter.has(r.verdict) && (!S.movedOnly || r.moved));
}

function drawScatter() {
  const xk = $('#sx').value, yk = $('#sy').value;
  const rows = screenRows();
  const pts = rows.map(r => ({
    x: r[xk], y: r[yk], label: r.asset_key, group: r.verdict, moved: r.moved,
    r: 4 + Math.min(4, Math.log10(Math.max(r.vol_usd_med30, 1e5) / 1e5)), meta: r,
  }));
  const gates = { vol_usd_med30: S.params.g_adv ?? S.meta.defaults.g_adv,
                  oi_usd_med30: S.params.g_oi ?? S.meta.defaults.g_oi,
                  n_venues: S.params.g_venues ?? S.meta.defaults.g_venues,
                  days_above_5m: S.params.g_persist ?? S.meta.defaults.g_persist,
                  composite: S.screen.add_bar };
  scatter($('#scatter'), pts, {
    xlog: $('#sxlog').checked, ylog: $('#sylog').checked, height: 430,
    xfmt: v => AXES.find(a => a[0] === xk)[1].includes('USD') ? fmtUsd(v) : fmtNum(v, v < 10 ? 2 : 0),
    yfmt: v => AXES.find(a => a[0] === yk)[1].includes('USD') ? fmtUsd(v) : fmtNum(v, v < 10 ? 2 : 0),
    groupColor: g => VCOL[g] || 'var(--muted)',
    vlines: gates[xk] != null ? [{ x: gates[xk], label: 'gate' }] : [],
    hlines: gates[yk] != null ? [{ y: gates[yk], label: 'gate' }] : [],
    labelTop: 8,
    onClick: p => openDetail(p.label),
    tip: p => {
      const r = p.meta, g = (ok, s) => `<span class="${ok ? 'pass' : 'fail'}">${ok ? '✓' : '✗'} ${s}</span>`;
      return `<b>${r.asset_key}</b> <span class="v-${r.verdict}">${r.verdict}</span>
        ${r.moved ? `<span class="pill">was ${r.base_verdict}</span>` : ''}
        <table>
        <tr><td>ADV med / mean</td><td>${fmtUsd(r.vol_usd_med30)} / ${fmtUsd(r.vol_usd_mean30)}</td></tr>
        <tr><td>OI median</td><td>${fmtUsd(r.oi_usd_med30)}</td></tr>
        <tr><td>venues</td><td>${r.n_venues} perp · ${r.venues_spot} spot</td></tr>
        <tr><td>spread / tick</td><td>${fmtNum(r.spread_bps_med)} / ${fmtNum(r.tick_bps_med)} bps</td></tr>
        <tr><td>7d/30d</td><td>${fmtNum(r.vol_trend)}</td></tr>
        <tr><td>composite</td><td>${fmtNum(r.composite, 1)} vs bar ${fmtNum(r.add_bar, 1)}</td></tr>
        </table>
        <div style="margin-top:4px">${g(r.gate_adv, 'ADV')} ${g(r.gate_persist, 'persist')}
          ${g(r.gate_venues, 'venues')} ${g(r.gate_oi, 'OI')}</div>`;
    },
  });
  $('#scatter-sub').textContent = `${rows.length} of ${S.screen.rows.length} assets`;
}

const why = r => {
  const o = [];
  if (!r.gate_adv) o.push(`ADV ${fmtUsd(r.vol_usd_med30)}`);
  if (!r.gate_persist) o.push(`persist ${pct1(r.days_above_5m)}`);
  if (!r.gate_venues) o.push(`${r.n_venues} venues`);
  if (!r.gate_oi) o.push(`OI ${fmtUsd(r.oi_usd_med30)}`);
  if (r.decaying) o.push(`decaying ${fmtNum(r.vol_trend)}`);
  if (!o.length && r.composite < r.add_bar) o.push(`composite ${fmtNum(r.composite, 1)} < bar ${fmtNum(r.add_bar, 1)}`);
  return o.join(', ') || '—';
};

function drawMoved() {
  const mv = S.screen.rows.filter(r => r.moved)
    .sort((a, b) => (b.vol_usd_med30 || 0) - (a.vol_usd_med30 || 0));
  $('#moved-sub').textContent = mv.length
    ? `${mv.length} asset${mv.length > 1 ? 's' : ''} changed verdict versus the baseline gates`
    : 'nothing changed — these are the baseline settings';
  if (!mv.length) {
    $('#t-moved').innerHTML =
      '<tbody><tr><td class="empty">Move a gate or a weight on the left and the assets '
      + 'that change verdict appear here.</td></tr></tbody>';
    return;
  }
  renderTable($('#t-moved'), mv, [
    ['asset_key', 'Asset', 'k'], ['base_verdict', 'Was', 'v'], ['verdict', 'Now', 'v'],
    ['vol_usd_med30', 'ADV med', 'usd'], ['oi_usd_med30', 'OI med', 'usd'],
    ['n_venues', 'Venues', 'n0'], ['composite', 'Composite', 'n1'],
    ['n_fail', 'Gates failed', 'n0'], ['__why', 'Binding constraint', 't'],
  ], r => openDetail(r.asset_key));
}

const SCREEN_COLS = [
  ['asset_key', 'Asset', 'k'], ['verdict', 'Verdict', 'v'], ['lane', 'Lane', 't'],
  ['asset_class', 'Class', 't'], ['traded', 'We trade', 'b'],
  ['composite', 'Composite', 'n1'], ['add_bar', 'Bar', 'n1'],
  ['c_flow', 'Flow', 'n0'], ['c_structure', 'Struct', 'n0'], ['c_carry', 'Carry', 'n0'],
  ['c_friction', 'Frict', 'n0'],
  // fast + directional first: these can actually fall. vol_trend cannot (see method).
  ['vol_last', 'Vol last day', 'usd', 'now grp'],
  ['vol_d1', 'Δ 1d', 'chg', 'now'],
  ['vol_dow7', 'vs same wkday', 'chg', 'now'],
  ['vol_off_peak', 'vs 14d peak', 'chg', 'now'],
  ['vol_peak_age', 'peak age', 'age', 'now'],
  ['chg_through', 'Through', 'thru', 'now'],
  ['vol_slope7_pct_day', 'Slope 7d', 'slope', 'slow grp'],
  ['vol_r37', 'Δ 3d/7d', 'chg', 'slow'],
  ['vol_usd_med30', 'ADV 30d med', 'usd', 'slow'],
  ['vol_usd_mean30', 'ADV 30d mean', 'usd', 'slow'],
  ['vol_usd_med7', 'ADV 7d med', 'usd', 'slow'], ['vol_trend', '7d/30d', 'n2', 'slow'],
  ['vol_burstiness', 'Burst', 'n2', 'slow'],
  ['vol_slope_pct_day', 'Slope %/d', 'n1', 'slow'],
  ['days_above_5m', 'Days>5M', 'pct'], ['days_since_onset', 'Onset', 'n0'],
  ['oi_usd_med30', 'OI med', 'usd'], ['oi_usd_live', 'OI live', 'usd'],
  ['oi_to_adv', 'OI/ADV', 'n2'], ['oi_bgt_share', 'BGT OI%', 'pct'],
  ['trades_med30', 'Trades/d', 'n0'],
  ['n_venues', 'Venues', 'n0'], ['venues_spot', 'Spot listed', 'n0'],
  ['spot_venues_live', 'Spot live', 'n0'], ['spot_vol_usd_med30', 'Spot ADV med', 'usd'],
  ['perp_spot_ratio', 'Perp/spot', 'n1'], ['spot_share', 'Spot share', 'pct'],
  ['venue_hhi', 'HHI', 'n2'],
  ['spread_bps_med', 'Spread bps', 'n2'], ['tick_bps_med', 'Tick bps', 'n2'],
  ['edge_headroom_bps', 'Headroom', 'n2'], ['tick_pinned', 'Pinned', 'b'],
  ['rv_bps_med', 'RV bps', 'n0'],
  ['fund_bps_med', 'Funding bps', 'n2'], ['fund_spread_bps_med', 'Fund spread', 'n1'],
  ['maker_fee_min', 'Maker fee', 'n4'], ['min_notional_max', 'Min notional', 'n0'],
  ['age_days', 'Age d', 'n0'], ['n_fail', 'Fails', 'n0'], ['__why', 'Binding', 't'],
];

// newest daily row across the current row set — 'thru' cells flag anything behind it
function setThruMax(rows) {
  S.thruMax = rows.reduce((m, r) => (r.chg_through
    && String(r.chg_through).slice(0, 10) > m ? String(r.chg_through).slice(0, 10) : m), '');
}

function drawScreenTable() {
  const q = $('#q-screen').value.toLowerCase();
  const rows = screenRows().filter(r => !q || r.asset_key.toLowerCase().includes(q));
  setThruMax(rows);
  renderTable($('#t-screen'), rows, SCREEN_COLS, r => openDetail(r.asset_key), 't-screen');
}

/* ===================== generic table ===================== */
// shared by the tiles and the tables so a direction never renders two different ways
function arrowHtml(v) {
  if (v == null || Number.isNaN(v)) return '<span class="chg-flat">—</span>';
  const up = v > 1.02, dn = v < 0.98, p = (v - 1) * 100;
  const txt = Math.abs(p) >= 999 ? `${fmtNum(v, v >= 10 ? 0 : 1)}×`
    : `${p >= 0 ? '+' : ''}${fmtNum(p, Math.abs(p) < 10 ? 1 : 0)}%`;
  return `<span class="${up ? 'chg-up' : dn ? 'chg-dn' : 'chg-flat'}">`
       + `${up ? '▲' : dn ? '▼' : '—'} ${txt}</span>`;
}

// slope is a rate, not a ratio: flat is 0, not 1. Same arrow-first treatment.
function slopeHtml(v) {
  if (v == null || Number.isNaN(v)) return '<span class="chg-flat">—</span>';
  const up = v > 2, dn = v < -2;
  return `<span class="${up ? 'chg-up' : dn ? 'chg-dn' : 'chg-flat'}">`
       + `${up ? '▲' : dn ? '▼' : '—'} ${v >= 0 ? '+' : ''}${fmtNum(v, 1)} %/d</span>`;
}

function cell(r, key, kind) {
  const v = key === '__why' ? why(r) : r[key];
  if (v == null || (typeof v === 'number' && Number.isNaN(v)))
    return { html: '—', cls: kind === 't' || kind === 'k' ? '' : 'num' };
  switch (kind) {
    case 'usd': return { html: fmtUsd(v), cls: 'num' };
    case 'n0': return { html: fmtNum(v, 0), cls: 'num' };
    case 'n1': return { html: fmtNum(v, 1), cls: 'num' };
    case 'n2': return { html: fmtNum(v, 2), cls: 'num' };
    case 'n4': return { html: fmtNum(v, 4), cls: 'num' };
    case 'n6': return { html: fmtNum(v, v >= 100 ? 2 : 6), cls: 'num' };
    case 'pct': return { html: pct1(v), cls: 'num' };
    case 'b': return { html: v ? '✓' : '', cls: 'num' };
    case 'v': return { html: `<span class="v-${v}">${v}</span>`, cls: '' };
    case 'listed': return { html: listedCell(v), cls: 'num' };
    // ratio vs the 30d median. Neither direction is "good" — a surge is opportunity and
    // risk, a collapse is a dying leg — so colour marks magnitude, not virtue.
    case 'x': {
      const c = v >= 5 ? 'x-up' : v <= 0.2 ? 'x-dn' : '';
      return { html: `<span class="${c}">${fmtNum(v, v >= 10 ? 0 : 1)}×</span>`, cls: 'num' };
    }
    // Directional ratio: 1.0 is flat. The ARROW carries the direction and the colour
    // only reinforces it — red/green alone fails for ~8% of men, and this table is read
    // fast. Rendered as a % move because "-58%" reads quicker than "0.42x".
    case 'chg': return { html: arrowHtml(v), cls: 'num' };
    case 'slope': return { html: slopeHtml(v), cls: 'num' };
    // days since the trailing 14d peak. 0-2 days means it is still at/near the high;
    // a large number next to a big negative "vs peak" means it has been bleeding.
    case 'age': {
      if (v < 0) return { html: '—', cls: 'num' };
      return { html: `<span class="${v <= 1 ? 'chg-up' : ''}">${fmtNum(v, 0)}d</span>`,
               cls: 'num' };
    }
    // dates the last daily row; anything behind the newest row in the same table is
    // stale. S.thruMax is set synchronously by whoever is about to render.
    case 'thru': {
      const s = String(v).slice(0, 10);
      // weekday matters: Δ1d straddling a weekend is calendar, not flow (MU trades
      // 20x more Mon-Fri than Sat-Sun), so name the day rather than make you count
      const wd = new Date(s + 'T00:00:00Z')
        .toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'UTC' });
      const txt = `${s} <span class="chg-flat">${wd}</span>`;
      return { html: (S.thruMax && s < S.thruMax)
        ? `<span class="stale" title="behind ${S.thruMax}">${txt}</span>` : txt,
        cls: 'num' };
    }
    case 'k': return { html: v, cls: 'k' };
    default: return { html: String(v), cls: '' };
  }
}

function renderTable(tbl, rows, cols, onClick, sortId) {
  const sid = sortId || tbl.id;
  const st = S.sort[sid];
  let data = rows;
  if (st) {
    const k = st.key;
    data = [...rows].sort((a, b) => {
      let x = k === '__why' ? why(a) : a[k], y = k === '__why' ? why(b) : b[k];
      if (x == null) return 1; if (y == null) return -1;
      if (typeof x === 'number' && typeof y === 'number') return st.asc ? x - y : y - x;
      return st.asc ? String(x).localeCompare(String(y)) : String(y).localeCompare(String(x));
    });
  }
  // cols entries are [key, label, kind, cls?] — cls rides on both the header and every
  // cell so a column can be visually promoted (.now) or demoted (.slow) as a unit.
  const head = cols.map(([k, l, kind, cls]) =>
    `<th class="${kind && !'tkv'.includes(kind) ? 'num' : ''} ${cls || ''} ${st && st.key === k ? 'sorted' + (st.asc ? ' asc' : '') : ''}"
        data-k="${k}">${l}</th>`).join('');
  const body = data.map(r => {
    const tds = cols.map(([k, , kind, cls]) => {
      const c = cell(r, k, kind);
      return `<td class="${c.cls} ${cls || ''}">${c.html}</td>`;
    }).join('');
    return `<tr class="${r.moved ? 'moved' : ''}" data-a="${r.asset_key || ''}">${tds}</tr>`;
  }).join('');
  tbl.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
  tbl.querySelectorAll('th').forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    S.sort[sid] = { key: k, asc: S.sort[sid] && S.sort[sid].key === k ? !S.sort[sid].asc : false };
    renderTable(tbl, rows, cols, onClick, sid);
  });
  if (onClick) tbl.querySelectorAll('tbody tr').forEach(tr => {
    tr.style.cursor = 'pointer';
    tr.onclick = () => onClick(rows.find(r => (r.asset_key || '') === tr.dataset.a) || {});
  });
  tbl._data = { rows: data, cols };
}

function toCsv(tbl, name) {
  const { rows, cols } = tbl._data || { rows: [], cols: [] };
  const esc = v => v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v;
  const lines = [cols.map(c => c[1]).join(',')];
  for (const r of rows) lines.push(cols.map(c => esc(c[0] === '__why' ? why(r) : r[c[0]])).join(','));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv' }));
  a.download = name; a.click();
}

/* ===================== assets tab ===================== */
function drawAssets() {
  if (!S.screen) return;
  const q = $('#q-assets').value.toLowerCase(), cl = $('#f-class').value, tr = $('#f-traded').value;
  const rows = S.screen.rows.filter(r =>
    (!q || r.asset_key.toLowerCase().includes(q)) &&
    (!cl || r.asset_class === cl) &&
    (tr === '' || String(r.traded ? 1 : 0) === tr));
  $('#assets-sub').textContent = `${rows.length} assets · ${SCREEN_COLS.length} columns`;
  setThruMax(rows);
  renderTable($('#t-assets'), rows, SCREEN_COLS, r => openDetail(r.asset_key), 't-assets');
}

/* ===================== detail ===================== */
async function openDetail(asset) {
  if (!asset || S.pending === asset) return;
  S.pending = asset;          // claimed synchronously: S.detail is only set after
  switchView('detail');       // the await, so switchView would re-enter forever
  $('#d-asset').value = asset;
  try {
    const days = $('#d-days').value;
    const d = await api('/api/series', { asset, days });
    S.detail = { asset, ...d };
    drawDetail();
  } catch (e) { showErr(e); } finally { S.pending = null; }
}

function pivot(rows, key, valKey) {
  const dates = [...new Set(rows.map(r => r.date))].sort();
  const names = [...new Set(rows.map(r => r[key]))].sort();
  const idx = Object.fromEntries(dates.map((d, i) => [d, i]));
  const series = names.map(n => ({ name: n, values: new Array(dates.length).fill(null) }));
  const smap = Object.fromEntries(series.map(s => [s.name, s]));
  for (const r of rows) {
    const s = smap[r[key]]; if (!s) continue;
    s.values[idx[r.date]] = r[valKey];
  }
  return { dates, series };
}

function drawDetail() {
  const { asset, total, history, legs } = S.detail;
  const mkt = $('#d-mkt').value;
  // In 'all' mode a venue contributes two rows per date (perp and spot). pivot() keys
  // on one field and last-write-wins, so keying on venue alone would silently plot only
  // whichever row arrived last — an under-count that looks like a real series. Give the
  // two markets their own series instead: summing them into one "BIN" line would be
  // neither market, and hiding one is worse.
  const by_venue = S.detail.by_venue.filter(r =>
    mkt === 'all' ? true : mkt === 'spot' ? r.market_type === 'spot'
                                          : r.market_type !== 'spot')
    .map(r => ({ ...r, vkey: mkt === 'all'
      ? `${r.venue} ${r.market_type === 'spot' ? 'spot' : 'perp'}` : r.venue }));
  const row = (S.screen && S.screen.rows.find(r => r.asset_key === asset)) || {};
  $('#d-badges').innerHTML = row.verdict
    ? `<span class="v-${row.verdict}" style="font-size:15px">${row.verdict}</span>
       <span class="pill">${row.asset_class || ''}</span>
       <span class="pill">${row.lane || ''}</span>
       ${row.traded ? '<span class="pill">we trade it</span>' : ''}
       ${row.tick_pinned ? '<span class="pill">tick-pinned</span>' : ''}
       ${row.decaying ? '<span class="pill" style="color:var(--drop)">decaying</span>' : ''}` : '';
  // The median is deliberately robust to a pump, so on a spiking asset the tile and the
  // volume chart can differ by 100-1000x and both be right. Show the latest day next to
  // it so that gap is visible instead of looking like a unit bug.
  const lastDate = by_venue.length ? by_venue[by_venue.length - 1].date : null;
  const lastVol = lastDate
    ? by_venue.filter(r => r.date === lastDate).reduce((a, r) => a + (r.vol_usd || 0), 0)
    : null;
  const spike = lastVol && row.vol_usd_med30 ? lastVol / row.vol_usd_med30 : null;

  $('#d-tiles').innerHTML = [
    ['ADV median', fmtUsd(row.vol_usd_med30),
     `mean ${fmtUsd(row.vol_usd_mean30)} · last ${fmtUsd(lastVol)}` +
     (spike && (spike > 5 || spike < 0.2) ? ` (${fmtNum(spike, 1)}x median)` : '')],
    ['OI median', fmtUsd(row.oi_usd_med30), `OI/ADV ${fmtNum(row.oi_to_adv)}`],
    ['Venues', `${row.n_venues ?? '—'}`, `${row.venues_spot ?? 0} spot · HHI ${fmtNum(row.venue_hhi)}`],
    ['Spread', `${fmtNum(row.spread_bps_med)} bps`, `tick ${fmtNum(row.tick_bps_med)} · headroom ${fmtNum(row.edge_headroom_bps)}`],
    ['Spot ADV', fmtUsd(row.spot_vol_usd_med30),
     row.perp_spot_ratio ? `${fmtNum(row.perp_spot_ratio, 1)}x perp · ${row.spot_venues_live ?? 0} venues` : 'no spot flow'],
    ['vs 14d peak', arrowHtml(row.vol_off_peak),
     `peaked ${row.vol_peak_age != null && row.vol_peak_age >= 0
        ? row.vol_peak_age + 'd ago' : '—'} · Δ1d ${arrowHtml(row.vol_d1)}`
     + ` · vs same wkday ${arrowHtml(row.vol_dow7)}`],
    ['Composite', fmtNum(row.composite, 1), `bar ${fmtNum(row.add_bar, 1)} · ${row.n_fail ?? 0} gate fails`],
  ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join('');

  // Stacking a log axis is meaningless (the visual height stops being the sum), so log
  // mode draws the venues as plain lines instead.
  const lg = $('#d-log').checked;
  const volOpt = { stack: !lg, ylog: lg, height: 250, yfmt: fmtUsd, vfmt: fmtUsd };
  timeSeries($('#d-vol'), pivot(by_venue, 'vkey', 'vol_usd'), volOpt);
  timeSeries($('#d-oi'), pivot(by_venue, 'vkey', 'oi_usd'), volOpt);

  const share = pivot(by_venue, 'vkey', 'vol_usd');
  for (let i = 0; i < share.dates.length; i++) {
    let sum = 0; for (const s of share.series) sum += s.values[i] || 0;
    for (const s of share.series) s.values[i] = sum ? (s.values[i] || 0) / sum * 100 : null;
  }
  timeSeries($('#d-share'), share, { stack: true, height: 230, vfmt: v => fmtNum(v, 1) + '%',
    yfmt: v => fmtNum(v, 0) + '%' });
  timeSeries($('#d-fund'), pivot(by_venue, 'vkey', 'funding_apr_pct'),
    { height: 230, vfmt: v => fmtNum(v, 2) + '%', yfmt: v => fmtNum(v, 1) + '%' });
  timeSeries($('#d-px'), pivot(by_venue, 'vkey', 'px'),
    { height: 230, vfmt: v => fmtNum(v, 6), yfmt: v => fmtNum(v, v > 100 ? 0 : 4) });
  timeSeries($('#d-agree'), {
    dates: total.map(r => r.date),
    series: [{ name: 'cross-venue spread', values: total.map(r => r.px_spread_bps) },
             { name: 'USDC basis', values: total.map(r => r.usdc_basis_bps) }],
  }, { height: 230, vfmt: v => fmtNum(v, 2) + ' bps', yfmt: v => fmtNum(v, 1) });

  if (history.length > 1) {
    timeSeries($('#d-hist'), {
      dates: history.map(r => r.run_date),
      series: [{ name: 'composite', values: history.map(r => r.composite) },
               { name: 'add bar', values: history.map(r => r.add_bar) }],
    }, { height: 200, vfmt: v => fmtNum(v, 1), yfmt: v => fmtNum(v, 0) });
    $('#d-hist-note').textContent =
      `${history.length} runs · verdicts: ${history.map(h => h.run_date.slice(5) + ' ' + h.verdict).join('  →  ')}`;
  } else {
    $('#d-hist').innerHTML = '<div class="empty">one screen run so far — history builds from here</div>';
    $('#d-hist-note').textContent =
      'Verdict history needs the nightly job to accumulate. Come back in a week.';
  }

  S.thruMax = legs.reduce(
    (m, r) => (r.through && String(r.through).slice(0, 10) > m
      ? String(r.through).slice(0, 10) : m), '');
  $('#legs-sub').textContent = `${legs.length} instruments · sorted by last 24h`;
  renderTable($('#t-legs'), legs, [
    ['venue', 'Venue', 't'], ['market_type', 'Market', 't'],
    ['symbol', 'Native symbol', 't'], ['quote', 'Quote', 't'],
    ['we_quote', 'We quote', 'b'],
    // ---- now ----
    ['vol24h_usd', 'Vol 24h', 'usd', 'now grp'],
    ['vol_usd_last', 'Vol last day', 'usd', 'now'],
    ['vol_d1', 'Δ 1d', 'chg', 'now'],
    ['vol_dow7', 'vs same wkday', 'chg', 'now'],
    ['vol_off_peak', 'vs 14d peak', 'chg', 'now'],
    ['vol_peak_age', 'peak age', 'age', 'now'],
    ['oi_usd_latest', 'OI now', 'usd', 'now'],
    ['vol_share', 'Share', 'pct', 'now'],
    ['through', 'Through', 'thru', 'now'],
    ['vol_slope7_pct_day', 'Slope 7d', 'slope', 'slow grp'],
    ['vol_vs_med30', 'vs 30d', 'x', 'slow'],
    // ---- slow: what the screen actually scores on ----
    ['vol_usd_med7', 'ADV 7d med', 'usd', 'slow'],
    ['vol_usd_med30', 'ADV 30d med', 'usd', 'slow'],
    ['oi_usd_med30', 'OI 30d med', 'usd', 'slow'],
    // ---- cost to quote ----
    ['spread_bps', 'Spread bps', 'n2', 'grp'], ['tick_bps', 'Tick bps', 'n2'],
    ['fund_bps_med', 'Funding bps', 'n2'], ['fund_iv_h', 'Interval h', 'n0'],
    ['maker_fee', 'Maker fee', 'n4'], ['min_notional', 'Min notional', 'n0'],
    ['px_close', 'Price', 'n6'],
    ['listed_since', 'Listed', 't'], ['days', 'Days', 'n0'],
  ], null, 't-legs');
}

/* ===================== venues ===================== */
function drawVenues() {
  if (!S.venues) return;
  const q = $('#q-venues').value.toLowerCase(), v = $('#f-venue').value, wq = $('#f-quote').value;
  const rows = S.venues.filter(r =>
    (!q || (r.asset_key + ' ' + r.symbol).toLowerCase().includes(q)) &&
    (!v || r.venue === v) && (wq === '' || String(r.we_quote) === wq)
    && (!S.mktFilter || r.market_type === S.mktFilter));
  $('#venues-sub').textContent = `${rows.length} of ${S.venues.length} instruments`;

  // the actionable slice: legs on assets we already trade that we do not quote
  const traded = new Set((S.screen ? S.screen.rows.filter(r => r.traded) : []).map(r => r.asset_key));
  const gaps = S.venues.filter(r => !r.we_quote && traded.has(r.asset_key))
    .sort((a, b) => b.vol_usd_med30 - a.vol_usd_med30);
  $('#gap-note').innerHTML = gaps.length
    ? `<b>${gaps.length} instruments</b> belong to assets we already trade but we don't quote them — biggest: `
      + gaps.slice(0, 4).map(g => `<span class="clickable" data-a="${g.asset_key}">${g.asset_key}/${g.venue} ${fmtUsd(g.vol_usd_med30)}</span>`).join(', ')
    : '';
  $$('#gap-note .clickable').forEach(s => s.onclick = () => openDetail(s.dataset.a));

  renderTable($('#t-venues'), rows, [
    ['asset_key', 'Asset', 'k'], ['venue', 'Venue', 't'], ['market_type', 'Market', 't'],
    ['symbol', 'Native symbol', 't'], ['quote', 'Quote', 't'], ['we_quote', 'We quote', 'b'],
    // same promotion as the leg table: what is trading now, then the slow medians
    ['vol24h_usd', 'Vol 24h', 'usd', 'now grp'],
    ['vol_trend', 'vs 30d', 'x', 'slow'],
    ['oi_usd_live', 'OI live', 'usd', 'now'],
    ['vol_share', 'Venue share', 'pct', 'now'],
    ['vol_usd_med7', 'ADV 7d med', 'usd', 'slow grp'],
    ['vol_usd_med30', 'ADV 30d med', 'usd', 'slow'],
    ['vol_usd_mean30', 'ADV 30d mean', 'usd', 'slow'],
    ['oi_usd_med30', 'OI 30d med', 'usd', 'slow'],
    ['trades_med30', 'Trades/d', 'n0', 'slow'], ['oi_days', 'OI days', 'n0', 'slow'],
    ['spread_bps', 'Spread bps', 'n2', 'grp'], ['tick_bps', 'Tick bps', 'n2'],
    ['fund_bps_med', 'Funding bps', 'n2'], ['fund_iv_h', 'Interval h', 'n0'],
    ['maker_fee', 'Maker fee', 'n4'], ['taker_fee', 'Taker fee', 'n4'],
    ['min_notional', 'Min notional', 'n0'], ['contract_mult', 'Contract mult', 'n4'],
    ['px_close', 'Last px', 'n4'], ['listed_since', 'Listed', 't'], ['days', 'Days', 'n0'],
  ], r => openDetail(r.asset_key), 't-venues');
}

/* ===================== health ===================== */
function drawHealth() {
  const h = S.health;
  renderTable($('#t-loads'), h.loads, [
    ['run_ts', 'Run', 't'], ['table', 'Table', 't'], ['rows_in', 'In', 'n0'],
    ['rows_loaded', 'Loaded', 'n0'], ['rows_rejected', 'Rejected', 'n0'],
    ['status', 'Status', 't'], ['duration_s', 'Sec', 'n2'], ['notes', 'Notes', 't'],
  ], null, 't-loads');

  const ag = pivot(h.agreement, 'asset_key', 'px_spread_bps');
  timeSeries($('#h-agree'), ag, { height: 250, vfmt: v => fmtNum(v, 2) + ' bps',
    yfmt: v => fmtNum(v, 1) });

  const cov = pivot(h.coverage, 'venue', 'instruments');
  timeSeries($('#h-cov'), cov, { height: 250, vfmt: v => fmtNum(v, 0),
    yfmt: v => fmtNum(v, 0) });

  renderTable($('#t-worst'), h.worst, [
    ['asset_key', 'Asset', 'k'], ['worst_bps', 'Worst bps', 'n0'],
    ['median_bps', 'Median bps', 'n1'], ['days', 'Days', 'n0'],
  ], r => openDetail(r.asset_key), 't-worst');
  renderTable($('#t-dups'), h.dups, [
    ['asset_a', 'A', 'k'], ['asset_b', 'B', 't'], ['class', 'Class', 't'],
    ['px_a', 'Px A', 'n4'], ['px_b', 'Px B', 'n4'], ['diff_bps', 'Diff bps', 'n1'],
    ['vol_a', 'Vol A', 'usd'], ['vol_b', 'Vol B', 'usd'],
    ['venues_a', 'Venues A', 'n0'], ['venues_b', 'Venues B', 'n0'],
  ], null, 't-dups');
  renderTable($('#t-rejects'), h.rejects, [
    ['run_ts', 'Run', 't'], ['table', 'Table', 't'], ['reason', 'Reason', 't'],
    ['venue', 'Venue', 't'], ['symbol', 'Symbol', 't'], ['detail', 'Detail', 't'],
  ], null, 't-rejects');
  renderTable($('#t-unmapped'), h.unmapped, [
    ['name', 'Internal name', 'k'], ['venue', 'Venue', 't'], ['kind', 'Kind', 't'],
    ['symbol', 'Symbol', 't'], ['fills', 'Fills', 'n0'], ['match_rule', 'Rule', 't'],
  ], null, 't-unmapped');
}


/* ===================== history (changes) ===================== */
const VENUE_COLS = ['BIN', 'OKX', 'BGT', 'GAT', 'KCN'];
// 0 not listed | 1 perp only | 2 spot only | 3 both
const LISTED = {1: ['P', 'perp only'], 2: ['S', 'spot only'], 3: ['P+S', 'perp and spot']};
const listedCell = v => LISTED[v]
  ? `<b title="${LISTED[v][1]}">${LISTED[v][0]}</b>` : '<span class="pass">·</span>';

function drawHistory() {
  const h = S.history; if (!h) return;
  const dq = $('#q-drop').value.toLowerCase();
  const dropped = h.dropped.filter(r => !dq || (r.name + ' ' + (r.asset_key || '')).toLowerCase().includes(dq));
  const assetsDropped = new Set(h.dropped.map(r => r.asset_key).filter(Boolean));
  $('#hist-tiles').innerHTML = [
    ['configured now', h.active.length, 'instruments in the live symbol list'],
    ['dropped', h.dropped.length, `${assetsDropped.size} distinct assets`],
    ['new listings', h.new.length, `first seen anywhere in ${$('#new-days').value}d`],
    ['announced', h.upcoming.length, 'listing date in the future'],
    ['venue gaps', h.gaps.length, 'assets we trade, not on every venue'],
  ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join('');

  $('#drop-sub').textContent =
    `${h.dropped.length} instruments across ${assetsDropped.size} assets`;
  renderTable($('#t-dropped'), dropped, [
    ['name', 'Internal name', 'k'], ['server', 'Srv', 't'], ['asset_key', 'Asset', 't'],
    ['asset_class', 'Class', 't'], ['venue', 'Venue', 't'], ['kind', 'Kind', 't'],
    ['last_seen', 'Last configured', 't'], ['days_since_drop', 'Days ago', 'n0'],
    ['first_seen', 'First configured', 't'], ['config_days', 'Days in config', 'n0'],
    ['fills_30d', 'Fills 30d', 'n0'], ['last_fill', 'Last fill', 't'],
  ], r => r.asset_key && openDetail(r.asset_key), 't-dropped');

  const nq = $('#q-new').value.toLowerCase();
  const multi = $('#new-multi').checked;
  const nrows = h.new.filter(r => (!multi || r.venues_perp >= 2)
    && (!nq || (r.asset_key + ' ' + r.asset_class).toLowerCase().includes(nq)));
  $('#new-sub').textContent = `${nrows.length} assets`;
  const venueCols = VENUE_COLS.map(v => [v, v, 'listed']);
  renderTable($('#t-new'), nrows, [
    ['asset_key', 'Asset', 'k'], ['asset_class', 'Class', 't'],
    ['first_listed', 'First listed', 't'], ['age_days', 'Age d', 'n0'],
    ['we_trade', 'We trade', 'b'], ...venueCols,
    ['venues_perp', 'Perp venues', 'n0'], ['venues_spot', 'Spot venues', 'n0'],
    ['BIN_since', 'BIN since', 't'], ['OKX_since', 'OKX since', 't'],
    ['BGT_since', 'BGT since', 't'], ['GAT_since', 'GAT since', 't'],
    ['KCN_since', 'KCN since', 't'],
  ], r => openDetail(r.asset_key), 't-new');

  renderTable($('#t-upcoming'), h.upcoming, [
    ['asset_key', 'Asset', 'k'], ['asset_class', 'Class', 't'],
    ['first_listed', 'Opens', 't'], ['age_days', 'Days away', 'n0'],
    ...venueCols, ['venues_perp', 'Perp venues', 'n0'],
  ], null, 't-upcoming');

  renderTable($('#t-gaps'), h.gaps, [
    ['asset_key', 'Asset', 'k'], ['asset_class', 'Class', 't'],
    ['first_listed', 'First listed', 't'], ...venueCols,
    ['venues_perp', 'Perp venues', 'n0'], ['venues_spot', 'Spot venues', 'n0'],
  ], r => openDetail(r.asset_key), 't-gaps');
}

/* ===================== methodology ===================== */
function drawMethod() {
  const D = S.meta.defaults, P = k => (S.params[k] ?? D[k]);
  const w = { f: P('w_flow'), s: P('w_structure'), c: P('w_carry'), r: P('w_friction') };
  const sum = w.f + w.s + w.c + w.r;
  const usd = v => '$' + (v / 1e6).toFixed(v < 1e7 ? 1 : 0) + 'M';
  const chg = k => Math.abs(P(k) - D[k]) > 1e-9 ? ' class="chgd"' : '';
  $('#method').innerHTML = `
<div class="meth">
<h3>0. The ranking primitive</h3>
<p>Every component is a <b>cross-sectional percentile</b>, never a raw value — so a
composite is a statement about an asset's rank among the ${S.screen ? S.screen.rows.length : '126'}
screened assets on that day, not an absolute score. Two runs are only comparable to
the extent the screened set is.</p>
<pre>pct(x, asc=true) = rank(x) as a percentile in [0,100]
                   ties get the average rank; missing values rank last
                   (pandas: x.rank(pct=True, ascending=asc, na_option="bottom") * 100)</pre>

<h3>1. Components</h3>
<p><b>Flow</b> — is there earnable two-way volume, and is it there every day?
Trade count is Binance-only (no other venue publishes it via REST), so it is a proxy
tagged with its source, never imputed to the other four.</p>
<pre>c_flow = [ pct(log10(max(vol_usd_med30, 1)))
         + pct(days_above_5m)
         + pct(log10(max(trades_med30, 1))) ] / 3</pre>
<p><b>Structure</b> — how many places can we quote and hedge, and is the flow spread
across them? <span class="mono">venue_hhi</span> is the Herfindahl index of each
venue's share of 24h volume, so <span class="mono">1 − hhi</span> rewards dispersion:
one venue with 100% scores 0.</p>
<pre>c_structure = [ pct(n_venues) + pct(1 − venue_hhi) + pct(venues_spot) ] / 3
venue_hhi   = Σ_venue ( venue 24h volume / asset 24h volume )²</pre>
<p><b>Carry</b> — cross-venue funding dispersion is a tradable spread, and its
<i>size</i> is what matters, not its sign; hence the absolute value.</p>
<pre>c_carry = pct( |fund_spread_bps_med| )
fund_spread_bps = (max_venue funding − min_venue funding) × 10⁴, median over 30d</pre>
<p><b>Friction</b> — how much room is there between the spread and the tick, and how
small a clip can we show? Min-notional is ranked <b>descending</b>: lower is better.</p>
<pre>c_friction = [ pct(edge_headroom_bps) + pct(min_notional_max, asc=false) ] / 2
edge_headroom_bps = spread_bps_med − tick_bps_med</pre>

<h3>2. Composite</h3>
<pre>composite = ( ${w.f.toFixed(2)}·c_flow + ${w.s.toFixed(2)}·c_structure
            + ${w.c.toFixed(2)}·c_carry + ${w.r.toFixed(2)}·c_friction ) / ${sum.toFixed(2)}</pre>
<p>Weights are normalised by their sum, so moving one slider re-weights the rest
rather than changing the scale.</p>

<h3>3. Lanes</h3>
<p>A name listed mid-window has no 30 days to be persistent over. Judging it on the
full window is what made BLESS and RATS read as drops while running 47× and 133×
7d/30d.</p>
<pre>lane = days_since_onset &lt; <span${chg('lane_days')}>${P('lane_days')}</span> ? "new" : "established"
onset = first day with volume ≥ 10% of the asset's own recent median volume</pre>

<h3>4. Hard gates</h3>
<pre>gate_adv     = lane=="new" ? vol_usd_med7  ≥ <span${chg('g_adv')}>${usd(P('g_adv'))}</span>
                           : vol_usd_med30 ≥ <span${chg('g_adv')}>${usd(P('g_adv'))}</span>
gate_persist = lane=="new" ? days_above_5m_live ≥ <span${chg('g_persist')}>${(P('g_persist') * 100).toFixed(0)}%</span>
                           : days_above_5m      ≥ <span${chg('g_persist')}>${(P('g_persist') * 100).toFixed(0)}%</span>
gate_venues  = n_venues       ≥ <span${chg('g_venues')}>${P('g_venues')}</span>
gate_oi      = oi_usd_med30   ≥ <span${chg('g_oi')}>${usd(P('g_oi'))}</span>
n_fail       = count of the four that are false

decay_slow   = vol_trend &lt; <span${chg('decay_trend')}>${P('decay_trend').toFixed(2)}</span>
               AND vol_slope_pct_day &lt; <span${chg('decay_slope')}>${P('decay_slope').toFixed(1)}%</span>
               AND lane == "established"
decay_fast   = vol_off_peak &lt; <span${chg('decay_off_peak')}>${P('decay_off_peak') === 0
                 ? 'off' : (P('decay_off_peak') * 100).toFixed(0) + '% of peak'}</span>
               i.e. at least <span${chg('decay_off_peak')}>${P('decay_off_peak') === 0
                 ? '—' : ((1 - P('decay_off_peak')) * 100).toFixed(0) + '%'}</span> below the 14d peak
               AND vol_r37 &lt; <span${chg('decay_r37')}>${P('decay_r37').toFixed(2)}</span>
decaying     = decay_slow OR decay_fast
tick_pinned  = spread_bps_med ≤ tick_bps_med × <span${chg('tick_pinned_ratio')}>${P('tick_pinned_ratio').toFixed(2)}</span></pre>

<h3>5. Verdict</h3>
<p>The add bar is <b>self-calibrating</b>: a candidate must be at least as attractive
as the median asset we already keep. The raw gates alone would be near-tautological,
because the shortlist is selected by perp volume in the first place: almost everything
on it passes an ADV gate, on a list built by ranking on ADV.</p>
<pre>add_bar = median( composite ) over { traded AND n_fail &lt; ${P('n_fail_drop')} }

 traded AND (n_fail ≥ <span${chg('n_fail_drop')}>${P('n_fail_drop')}</span> OR decaying)      → drop
 traded AND  n_fail &lt; ${P('n_fail_drop')} AND NOT decaying   → keep
!traded AND  n_fail = 0 AND composite ≥ add_bar → add
!traded AND (n_fail &gt; 0  OR composite &lt; add_bar) → watch</pre>

<h3>6. Underlying metrics</h3>
<pre>vol_usd_med30      median of the 30 daily per-asset volume sums (perp venues only)
vol_usd_mean30     mean of the same — total capturable flow scales with this
vol_burstiness     mean / median; &gt;1.5 means the flow is a few big days
vol_trend          median(last 7 days) / median(30 days). NOTE this is a LEVEL
                   ratio, not a change: after a pump the 30d median lags for weeks, so
                   it stays huge and RISING while the asset collapses. BLESS read 38.8
                   with volume 90% off its peak. Do not read it as direction.
vol_d1             last day / previous day. NOT calendar-adjusted — volume has a
                   big weekday cycle (MU trades $1.2–3.0B Mon–Fri vs $96–433M
                   Sat–Sun, 20×), so read it next to the weekday in "Through".
vol_dow7           last day / same weekday a week ago. The calendar cancels, so
                   this is the 1-step change you can compare across assets.
vol_off_peak       last day / max(last 14d), shown as the delta from that peak.
vol_peak_age       days since that peak. off_peak + peak_age are the ONLY pair
                   that says "rolling over" while a pump is still inside every
                   window: TUT was halving daily yet read +97 %/day on a 7d
                   slope, +74 %/day on a 5d compound rate and +14,287% week-
                   over-week. All true; none of them answer the question.
                   (A 5d average of daily changes cannot fix this — the geometric
                   mean of n daily changes IS the endpoint-to-endpoint rate.)
vol_slope7_pct_day (exp(β) − 1) × 100 over the LAST 7 DAYS, β = OLS slope of
                   ln(volume) on day index. The week's shape, calendar-neutral
                   (a 7d window holds one of every weekday). vol_slope_pct_day
                   above uses all 30 days and stays positive for weeks after a
                   pump ends — BLESS read +19.6 %/day on 30d vs −35.6 on 7d.
vol_r37            median(last 3d) / median(last 7d) — direction, weekend noise removed
vol_off_peak       last day / max(last 14d), shown as the delta from that peak
                   (0.10 renders as −90%). This is the one that falls when an asset
                   dies, so decay_fast reads it.
vol_slope_pct_day  (exp(β) − 1) × 100, β = OLS slope of ln(volume) on day index,
                   computed only with ≥ 8 days of data
days_above_5m      share of the 30 days with volume ≥ $5M
oi_to_adv          oi_usd_med30 / vol_usd_med30
spot_vol_usd_med30 same as above over SPOT venues — kept out of the gates on
                   purpose, so adding spot did not move any calibrated threshold
perp_spot_ratio    perp ADV / spot ADV; high = derivative floating free of a
                   deliverable market, which is where hedging gets expensive</pre>

<h3>7. What is deliberately not in here</h3>
<p>Our own P&amp;L. The universe is chosen on market structure only — volume, open
interest, venue count, spread, funding — so the screen cannot learn to like whatever
the current strategy already happens to do well on. Rotation is per <b>asset</b>, not
per instrument: more venues carrying the same underlying is more places to quote and
hedge.</p>
<p>Also absent: <b>time-weighted</b> spread (we sample), tick-level microstructure,
and trade counts outside Binance. See <span class="mono">DATA_COVERAGE.md</span>.</p>
</div>`;
}

/* ===================== sql ===================== */
async function runSql() {
  try {
    const t0 = performance.now();
    const r = await api('/api/query', { sql: $('#sql').value });
    const cols = r.columns.map(c => [c, c, typeof (r.rows[0] || {})[c] === 'number' ? 'n2' : 't']);
    renderTable($('#t-sql'), r.rows, cols, null, 't-sql');
    $('#sql-note').textContent = `${r.rows.length} rows · ${Math.round(performance.now() - t0)} ms`;
  } catch (e) { showErr(e); }
}

/* ===================== shell ===================== */
function switchView(v) {
  $$('nav button[data-v]').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  $$('.view').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  location.hash = v;
  if (v === 'assets') drawAssets();
  if (v === 'detail' && !S.detail && !S.pending && S.screen)
    openDetail(S.screen.rows[0].asset_key);
  if (v === 'venues' && !S.venues) api('/api/venues').then(d => { S.venues = d; drawVenues(); }).catch(showErr);
  else if (v === 'venues') drawVenues();
  if (v === 'health' && !S.health) api('/api/health').then(d => { S.health = d; drawHealth(); }).catch(showErr);
  if (v === 'history') {
    if (!S.history) loadHistory(); else drawHistory();
  }
}

async function loadHistory() {
  try { S.history = await api('/api/history', { new_days: +$('#new-days').value });
        drawHistory(); } catch (e) { showErr(e); }
}

async function init() {
  try {
    S.meta = await api('/api/meta');
    $('#run-date').innerHTML = S.meta.run_dates.map(d => `<option>${d}</option>`).join('');
    $('#hdr-sub').textContent =
      `${S.meta.daily.rows || 0} daily rows · ${S.meta.daily.d0 || '?'} → ${S.meta.daily.d1 || '?'} · `
      + `${S.meta.venues.join(' ')}`;
    $('#f-class').innerHTML += S.meta.classes
      .map(c => `<option value="${c.asset_class}">${c.asset_class} (${c.n})</option>`).join('');
    $('#f-venue').innerHTML += S.meta.venues.map(v => `<option>${v}</option>`).join('');

    // params from the URL so a calibration can be shared
    const qp = new URLSearchParams(location.search);
    for (const k in S.meta.defaults) if (qp.has(k)) S.params[k] = +qp.get(k);

    fillAxisSelects(); buildKnobs();
    await loadScreen();
    $('#d-asset').innerHTML = S.screen.rows.map(r => r.asset_key).sort()
      .map(a => `<option>${a}</option>`).join('');
    // ?asset=TUT deep-links straight to one asset's detail page
    const deep = qp.get('asset');
    if (deep && S.screen.rows.some(r => r.asset_key === deep)) openDetail(deep);
    else if (location.hash) switchView(location.hash.slice(1));
  } catch (e) { showErr(e); }
}

$$('nav button[data-v]').forEach(b => b.onclick = () => switchView(b.dataset.v));
$('#theme').onclick = () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
  next ? document.documentElement.setAttribute('data-theme', next)
       : document.documentElement.removeAttribute('data-theme');
  if (S.screen) { drawScatter(); if (S.detail) drawDetail(); if (S.health) drawHealth(); }
};
$('#reset').onclick = () => { S.params = {}; buildKnobs(); loadScreen(); };
$('#copylink').onclick = () => {
  const u = new URL(location.href); u.search = '';
  for (const k in S.params) u.searchParams.set(k, S.params[k]);
  navigator.clipboard.writeText(u.toString());
  $('#copylink').textContent = 'copied'; setTimeout(() => $('#copylink').textContent = 'Link', 1200);
};
$('#run-date').onchange = loadScreen;
['#sx', '#sy', '#sxlog', '#sylog'].forEach(s => $(s).onchange = drawScatter);
$$('.chip[data-vf]').forEach(c => c.onclick = () => {
  c.classList.toggle('on');
  c.classList.contains('on') ? S.vfilter.add(c.dataset.vf) : S.vfilter.delete(c.dataset.vf);
  drawScatter(); drawScreenTable();
});
$('#movedonly').onchange = e => { S.movedOnly = e.target.checked; drawScatter(); drawScreenTable(); };
$('#q-screen').oninput = drawScreenTable;
$('#csv-screen').onclick = () => toCsv($('#t-screen'), 'screen.csv');
['#q-assets', '#f-class', '#f-traded'].forEach(s => $(s).oninput = drawAssets);
$('#csv-assets').onclick = () => toCsv($('#t-assets'), 'assets.csv');
['#q-venues', '#f-venue', '#f-quote'].forEach(s => $(s).oninput = drawVenues);
$('#f-mkt').onchange = e => { S.mktFilter = e.target.value; drawVenues(); };
$('#csv-venues').onclick = () => toCsv($('#t-venues'), 'venues.csv');
$('#d-asset').onchange = e => openDetail(e.target.value);
$('#d-days').onchange = () => openDetail($('#d-asset').value);
$('#d-mkt').onchange = () => { if (S.detail) drawDetail(); };
$('#d-log').onchange = () => { if (S.detail) drawDetail(); };
$('#run-sql').onclick = runSql;
$('#new-days').onchange = loadHistory;
['#q-drop', '#q-new', '#new-multi'].forEach(x => $(x).oninput = drawHistory);
$('#csv-drop').onclick = () => toCsv($('#t-dropped'), 'dropped.csv');
$('#csv-new').onclick = () => toCsv($('#t-new'), 'new_listings.csv');
$('#csv-sql').onclick = () => toCsv($('#t-sql'), 'query.csv');
addEventListener('resize', () => { clearTimeout(window._rz); window._rz = setTimeout(() => {
  if (S.screen) drawScatter(); if (S.detail) drawDetail(); if (S.health) drawHealth(); }, 200); });

init();
