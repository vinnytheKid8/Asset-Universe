/* Minimal SVG charts: line, stacked area, scatter, hbar.
   No dependencies - the ClickHouse box may have no outbound internet, and a CDN
   script tag is one more thing to break at 4am.

   Palette is the validated categorical order (adjacent-pair CVD safe in both
   modes). Scatter uses at most the first three slots, which are the ones that
   pass the all-pairs check; series charts may use more. */
const PAL = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
export const seriesColor = i => cssv(PAL[i % PAL.length]);

const NS = 'http://www.w3.org/2000/svg';
const el = (t, a = {}) => { const e = document.createElementNS(NS, t);
  for (const k in a) if (a[k] != null) e.setAttribute(k, a[k]); return e; };

export const fmtUsd = v => v == null ? '—'
  : Math.abs(v) >= 1e9 ? '$' + (v / 1e9).toFixed(2) + 'B'
  : Math.abs(v) >= 1e6 ? '$' + (v / 1e6).toFixed(1) + 'M'
  : Math.abs(v) >= 1e3 ? '$' + (v / 1e3).toFixed(1) + 'K' : '$' + (+v).toFixed(0);
export const fmtNum = (v, d = 2) => v == null || Number.isNaN(v) ? '—' : (+v).toFixed(d);

function niceTicks(lo, hi, n = 5) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}
function logTicks(lo, hi) {
  const out = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++)
    for (const m of [1, 3]) { const v = m * Math.pow(10, e); if (v >= lo && v <= hi) out.push(v); }
  return out.length > 1 ? out : [lo, hi];
}

/* Shared frame: axes, grid, and a tooltip layer. Returns scales + the plot <g>. */
function frame(host, opt) {
  host.innerHTML = '';
  const W = host.clientWidth || 900, H = opt.height || 300;
  const M = Object.assign({ t: 14, r: 16, b: 34, l: 62 }, opt.margin);
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H,
                          class: 'chart', preserveAspectRatio: 'none' });
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const xlog = opt.xlog, ylog = opt.ylog;
  const tx = v => { if (!xlog) return M.l + iw * (v - opt.x0) / ((opt.x1 - opt.x0) || 1);
    const a = Math.log10(Math.max(v, 1e-12)), l0 = Math.log10(Math.max(opt.x0, 1e-12)),
          l1 = Math.log10(Math.max(opt.x1, 1e-12));
    return M.l + iw * (a - l0) / ((l1 - l0) || 1); };
  const ty = v => { if (!ylog) return M.t + ih - ih * (v - opt.y0) / ((opt.y1 - opt.y0) || 1);
    const a = Math.log10(Math.max(v, 1e-12)), l0 = Math.log10(Math.max(opt.y0, 1e-12)),
          l1 = Math.log10(Math.max(opt.y1, 1e-12));
    return M.t + ih - ih * (a - l0) / ((l1 - l0) || 1); };

  const grid = el('g');
  const yt = ylog ? logTicks(opt.y0, opt.y1) : niceTicks(opt.y0, opt.y1, opt.yticks || 5);
  for (const v of yt) {
    const y = ty(v);
    grid.appendChild(el('line', { x1: M.l, x2: W - M.r, y1: y, y2: y, class: 'grid' }));
    const t = el('text', { x: M.l - 8, y: y + 4, class: 'axis', 'text-anchor': 'end' });
    t.textContent = (opt.yfmt || (d => fmtNum(d, 0)))(v); grid.appendChild(t);
  }
  const xt = opt.xticksVals || (xlog ? logTicks(opt.x0, opt.x1)
                                     : niceTicks(opt.x0, opt.x1, opt.xticks || 6));
  for (const v of xt) {
    const x = tx(v);
    if (opt.xgrid !== false)
      grid.appendChild(el('line', { x1: x, x2: x, y1: M.t, y2: H - M.b, class: 'grid' }));
    const t = el('text', { x, y: H - M.b + 18, class: 'axis', 'text-anchor': 'middle' });
    t.textContent = (opt.xfmt || (d => fmtNum(d, 0)))(v); grid.appendChild(t);
  }
  grid.appendChild(el('line', { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, class: 'axisline' }));
  svg.appendChild(grid);
  const plot = el('g'); svg.appendChild(plot);
  host.appendChild(svg);

  let tip = host.querySelector('.tip');
  if (!tip) { tip = document.createElement('div'); tip.className = 'tip'; host.appendChild(tip); }
  const showTip = (html, px, py) => {
    tip.innerHTML = html; tip.style.display = 'block';
    const r = host.getBoundingClientRect();
    let x = px + 14, y = py - 10;
    if (x + tip.offsetWidth > r.width) x = px - tip.offsetWidth - 14;
    tip.style.left = Math.max(0, x) + 'px';
    tip.style.top = Math.max(0, Math.min(y, r.height - tip.offsetHeight)) + 'px';
  };
  const hideTip = () => { tip.style.display = 'none'; };
  return { svg, plot, tx, ty, M, W, H, iw, ih, showTip, hideTip };
}

function legend(host, names, colors, onToggle, hidden) {
  const box = document.createElement('div'); box.className = 'legend';
  names.forEach((n, i) => {
    const s = document.createElement('span'); s.className = 'lg';
    // Re-rendering wipes the legend and rebuilds it, so the 'off' class has to be
    // derived from the hidden set rather than carried on the element. Toggling the
    // class and then reading it back made every click a re-hide.
    if (hidden && hidden.has(n)) s.classList.add('off');
    s.innerHTML = `<i style="background:${colors[i]}"></i>${n}`;
    if (onToggle) s.onclick = () => onToggle(n, !(hidden && hidden.has(n)));
    box.appendChild(s);
  });
  host.appendChild(box);
}

/* ---------------- time series: multi-line or stacked area ----------------- */
export function timeSeries(host, data, opt = {}) {
  // data: {dates:[...], series:[{name, values:[]}]}
  const { dates, series } = data;
  if (!dates.length || !series.length) { host.innerHTML = '<div class="empty">no data</div>'; return; }
  const hidden = host._hidden || (host._hidden = new Set());
  const vis = series.filter(s => !hidden.has(s.name));
  const stack = opt.stack;
  let y1 = 0, y0 = opt.ylog ? Infinity : 0;
  if (stack) {
    for (let i = 0; i < dates.length; i++) {
      let sum = 0; for (const s of vis) sum += s.values[i] || 0;
      y1 = Math.max(y1, sum);
    }
  } else {
    for (const s of vis) for (const v of s.values) {
      if (v == null || Number.isNaN(v)) continue;
      y1 = Math.max(y1, v); y0 = Math.min(y0, v);
    }
    if (!opt.ylog) y0 = Math.min(0, y0);
  }
  if (opt.ylog) { y0 = Math.max(y0, y1 / 1e5) || 1; } else if (y1 === y0) y1 = y0 + 1;
  if (opt.y0 != null) y0 = opt.y0;
  y1 *= 1.05;

  const f = frame(host, Object.assign({
    x0: 0, x1: dates.length - 1, y0, y1,
    xticksVals: dates.map((_, i) => i).filter((_, i, a) =>
      i % Math.max(1, Math.ceil(a.length / 7)) === 0),
    xfmt: i => (dates[Math.round(i)] || '').slice(5),
  }, opt));

  const colors = series.map((_, i) => seriesColor(i));
  if (stack) {
    const acc = new Array(dates.length).fill(0);
    vis.forEach(s => {
      const i0 = series.indexOf(s);
      let d = '';
      for (let i = 0; i < dates.length; i++) d += `${i ? 'L' : 'M'}${f.tx(i)},${f.ty(acc[i] + (s.values[i] || 0))}`;
      for (let i = dates.length - 1; i >= 0; i--) d += `L${f.tx(i)},${f.ty(acc[i])}`;
      f.plot.appendChild(el('path', { d: d + 'Z', fill: colors[i0], 'fill-opacity': .85,
                                      stroke: 'var(--surface)', 'stroke-width': .5 }));
      for (let i = 0; i < dates.length; i++) acc[i] += s.values[i] || 0;
    });
  } else {
    vis.forEach(s => {
      const i0 = series.indexOf(s);
      let d = '', pen = false;
      for (let i = 0; i < dates.length; i++) {
        const v = s.values[i];
        if (v == null || Number.isNaN(v)) { pen = false; continue; }
        d += `${pen ? 'L' : 'M'}${f.tx(i)},${f.ty(v)}`; pen = true;
      }
      f.plot.appendChild(el('path', { d, fill: 'none', stroke: colors[i0],
                                      'stroke-width': 2, 'stroke-linejoin': 'round' }));
    });
  }

  // crosshair + tooltip on the nearest date
  const hair = el('line', { class: 'hair', y1: f.M.t, y2: f.H - f.M.b, style: 'display:none' });
  f.plot.appendChild(hair);
  const hit = el('rect', { x: f.M.l, y: f.M.t, width: f.iw, height: f.ih, fill: 'transparent' });
  f.plot.appendChild(hit);
  const fmt = opt.vfmt || fmtUsd;
  hit.addEventListener('mousemove', ev => {
    const r = f.svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * f.W;
    const i = Math.max(0, Math.min(dates.length - 1,
      Math.round((px - f.M.l) / f.iw * (dates.length - 1))));
    hair.setAttribute('x1', f.tx(i)); hair.setAttribute('x2', f.tx(i));
    hair.style.display = '';
    const rows = vis.map(s => ({ n: s.name, v: s.values[i] }))
      .filter(o => o.v != null && !Number.isNaN(o.v))
      .sort((a, b) => b.v - a.v)
      .map(o => `<tr><td><i style="background:${colors[series.findIndex(s => s.name === o.n)]}"></i>${o.n}</td><td>${fmt(o.v)}</td></tr>`).join('');
    const tot = stack ? `<tr class="tot"><td>total</td><td>${fmt(vis.reduce((a, s) => a + (s.values[i] || 0), 0))}</td></tr>` : '';
    f.showTip(`<b>${dates[i]}</b><table>${rows}${tot}</table>`,
      (ev.clientX - r.left) / r.width * (host.clientWidth || f.W), ev.clientY - r.top);
  });
  hit.addEventListener('mouseleave', () => { f.hideTip(); hair.style.display = 'none'; });

  if (series.length > 1)
    legend(host, series.map(s => s.name), colors, (name, off) => {
      off ? hidden.add(name) : hidden.delete(name);
      timeSeries(host, data, opt);
    }, hidden);
}

/* ------------------------------- scatter ---------------------------------- */
export function scatter(host, pts, opt = {}) {
  // pts: [{x,y,label,group,r,meta}]
  const good = pts.filter(p => p.x != null && p.y != null && !Number.isNaN(p.x) && !Number.isNaN(p.y)
    && (!opt.xlog || p.x > 0) && (!opt.ylog || p.y > 0));
  if (!good.length) { host.innerHTML = '<div class="empty">no data</div>'; return; }
  const xs = good.map(p => p.x), ys = good.map(p => p.y);
  const pad = (lo, hi, log) => log ? [lo / 1.6, hi * 1.6] : [lo - (hi - lo) * .06 || lo - 1, hi + (hi - lo) * .06 || hi + 1];
  const [x0, x1] = pad(Math.min(...xs), Math.max(...xs), opt.xlog);
  const [y0, y1] = pad(Math.min(...ys), Math.max(...ys), opt.ylog);
  const f = frame(host, Object.assign({ x0, x1, y0, y1, height: opt.height || 440 }, opt));

  const groups = [...new Set(good.map(p => p.group || ''))];
  const gcolor = g => opt.groupColor ? opt.groupColor(g) : seriesColor(groups.indexOf(g));

  // reference lines (e.g. the ADV gate) - drawn under the marks
  (opt.vlines || []).forEach(v => {
    if (v.x < x0 || v.x > x1) return;
    f.plot.appendChild(el('line', { x1: f.tx(v.x), x2: f.tx(v.x), y1: f.M.t, y2: f.H - f.M.b, class: 'refline' }));
    const t = el('text', { x: f.tx(v.x) + 4, y: f.M.t + 12, class: 'reflabel' });
    t.textContent = v.label || ''; f.plot.appendChild(t);
  });
  (opt.hlines || []).forEach(v => {
    if (v.y < y0 || v.y > y1) return;
    f.plot.appendChild(el('line', { x1: f.M.l, x2: f.W - f.M.r, y1: f.ty(v.y), y2: f.ty(v.y), class: 'refline' }));
    const t = el('text', { x: f.W - f.M.r - 4, y: f.ty(v.y) - 5, class: 'reflabel', 'text-anchor': 'end' });
    t.textContent = v.label || ''; f.plot.appendChild(t);
  });

  good.forEach(p => {
    const c = el('circle', { cx: f.tx(p.x), cy: f.ty(p.y), r: p.r || 5,
      fill: gcolor(p.group || ''), 'fill-opacity': p.moved ? 1 : .72,
      stroke: p.moved ? 'var(--ink)' : 'var(--surface)', 'stroke-width': p.moved ? 1.6 : 1.5,
      class: 'pt', 'data-k': p.label });
    c.addEventListener('mouseenter', ev => {
      c.setAttribute('r', (p.r || 5) + 3);
      const r = f.svg.getBoundingClientRect();
      f.showTip(opt.tip ? opt.tip(p) : `<b>${p.label}</b>`,
        (ev.clientX - r.left) / r.width * (host.clientWidth || f.W), ev.clientY - r.top);
    });
    c.addEventListener('mouseleave', () => { c.setAttribute('r', p.r || 5); f.hideTip(); });
    if (opt.onClick) { c.style.cursor = 'pointer'; c.addEventListener('click', () => opt.onClick(p)); }
    f.plot.appendChild(c);
  });

  // label only the extremes, never every point
  if (opt.labelTop) {
    [...good].sort((a, b) => (b.x * b.y) - (a.x * a.y)).slice(0, opt.labelTop).forEach(p => {
      const t = el('text', { x: f.tx(p.x) + 8, y: f.ty(p.y) + 4, class: 'ptlabel' });
      t.textContent = p.label; f.plot.appendChild(t);
    });
  }
  if (groups.length > 1 && opt.legend !== false)
    legend(host, groups, groups.map(gcolor));
}

/* ------------------------------ horizontal bars --------------------------- */
export function hbar(host, rows, opt = {}) {
  // rows: [{label, value, group}]
  host.innerHTML = '';
  if (!rows.length) { host.innerHTML = '<div class="empty">no data</div>'; return; }
  const max = Math.max(...rows.map(r => Math.abs(r.value))) || 1;
  const box = document.createElement('div'); box.className = 'hbars';
  rows.forEach(r => {
    const d = document.createElement('div'); d.className = 'hbar';
    const col = opt.groupColor ? opt.groupColor(r.group) : seriesColor(0);
    d.innerHTML = `<span class="hl" title="${r.label}">${r.label}</span>
      <span class="ht"><i style="width:${Math.abs(r.value) / max * 100}%;background:${col}"></i></span>
      <span class="hv">${(opt.fmt || fmtUsd)(r.value)}</span>`;
    if (opt.onClick) { d.style.cursor = 'pointer'; d.onclick = () => opt.onClick(r); }
    box.appendChild(d);
  });
  host.appendChild(box);
}
