// The marks of the dashboard charts (addendum i, decision 4): bar (grouped
// and stacked), line, area (stacked optional), donut, scatter. Each builder
// returns { svg, tips, hover, legend }: `tips` are the tooltip contents the
// hover layer (charts.js) shows for the `.hit` element whose data-tip is the
// index; `hover` is 'mark' (bars, slices) or 'cross' (line, area, scatter).
// Colours are attributes from the theme's palette; text, grid and the surface
// ring/gap are classes styled by the page (or the exported page's CSS).

import {
  svgEl, niceTicks, scaleLinear, scaleBand, formatValue, seriesOf, xValuesOf, foldSeries,
  stackSeries, seriesColor, palette, otherColor, isNum, isTimeAxis, xLabel, textWidth, clip,
  barPath, colIndex, rowsOf, OTHER,
} from './chartCore.js';

export const PLOT_H = 220;
const TICK_PX = 11;

function fmtOf(widget) {
  const format = ['number', 'percent', 'currency'].includes(widget?.format) ? widget.format : 'number';
  const unit = typeof widget?.unit === 'string' ? widget.unit : '';
  return {
    value: (v) => formatValue(v, format, unit),
    tick: (v) => formatValue(v, format, format === 'number' ? '' : unit, { compact: true }),
    format, unit,
  };
}

function root(W, H, label) {
  const svg = svgEl('svg', { class: 'chart-svg', role: 'img', 'aria-label': label, viewBox: `0 0 ${W} ${H}`, width: W, height: H, focusable: 'false' });
  return svg;
}

/** Y axis: gridlines at the ticks, the tick labels, the unit caption. Returns the scale and the left margin. */
function yAxis(svg, ticks, fmt, { top, bottom, left, right }) {
  const y = scaleLinear([ticks[0], ticks[ticks.length - 1]], [bottom, top]);
  const g = svgEl('g', { class: 'axis axis-y' });
  for (const t of ticks) {
    const py = Number(y(t).toFixed(2));
    g.appendChild(svgEl('line', { class: t === 0 ? 'grid baseline' : 'grid', x1: left, x2: right, y1: py, y2: py }));
    g.appendChild(svgEl('text', { class: 'tick', x: left - 8, y: py + 4, 'text-anchor': 'end' }, fmt.tick(t)));
  }
  if (fmt.format === 'number' && fmt.unit) g.appendChild(svgEl('text', { class: 'tick unit', x: left - 8, y: top - 8, 'text-anchor': 'end' }, clip(fmt.unit, 60)));
  svg.appendChild(g);
  return y;
}

function leftMargin(ticks, fmt) {
  return Math.ceil(Math.max(...ticks.map((t) => textWidth(fmt.tick(t), TICK_PX)), 12)) + 14;
}

/** X labels at the given positions, thinned so they never touch, clipped to their slot. */
function xAxis(svg, items, y0, slot) {
  const g = svgEl('g', { class: 'axis axis-x' });
  const widest = Math.max(1, ...items.map((it) => textWidth(it.label, TICK_PX)));
  const every = Math.max(1, Math.ceil((Math.min(widest, 120) + 10) / Math.max(1, slot)));
  const shown = [];
  for (let i = 0; i < items.length; i += every) shown.push(i);
  const last = items.length - 1;
  if (shown.length && shown[shown.length - 1] !== last && last - shown[shown.length - 1] >= every) shown.push(last);
  for (const i of shown) {
    const it = items[i];
    g.appendChild(svgEl('text', { class: 'tick', x: Number(it.x.toFixed(2)), y: y0 + 18, 'text-anchor': 'middle' }, clip(it.label, slot * every - 8, TICK_PX)));
  }
  svg.appendChild(g);
}

function hitRect(svg, x, y, w, h, i) {
  return svgEl('rect', { class: 'hit', x: Number(x.toFixed(2)), y: Number(y.toFixed(2)), width: Math.max(1, Number(w.toFixed(2))), height: Math.max(1, Number(h.toFixed(2))), 'data-tip': i });
}

function legendOf(series, theme, shape) {
  return series.length >= 2 ? series.map((s, i) => ({ name: s.name, color: seriesColor(s, i, theme), shape })) : [];
}

function seriesNames(series) {
  return series.map((s) => s.name).join(', ');
}

// ---------------------------------------------------------------- bar
export function barChart(widget, { theme = 'light', width = 560 } = {}) {
  const fmt = fmtOf(widget);
  const series = foldSeries(seriesOf(widget));
  const xs = xValuesOf(widget);
  const n = xs.length;
  const stacked = widget.stacked === true && series.length > 1;
  const stack = stacked ? stackSeries(series) : null;
  let lo = 0;
  let hi = 0;
  series.forEach((s, j) => s.values.forEach((v, i) => {
    if (stacked) { const e = stack[j][i]; if (e) { lo = Math.min(lo, e.end, e.start); hi = Math.max(hi, e.end, e.start); } }
    else if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  }));
  const ticks = niceTicks(lo, hi);
  const W = Math.max(240, width);
  const top = 22;
  const bottom = top + PLOT_H;
  const left = leftMargin(ticks, fmt);
  const right = W - 8;
  const H = bottom + 30;
  const label = `Bar chart: ${widget.title || 'untitled'}, ${n} ${n === 1 ? 'category' : 'categories'}, series ${seriesNames(series)}`;
  const svg = root(W, H, label);
  const y = yAxis(svg, ticks, fmt, { top, bottom, left, right });
  const band = scaleBand(n, [left, right], 0.3);
  const k = series.length;
  const barW = stacked ? Math.max(1, Math.min(24, band.bandwidth)) : Math.max(1, Math.min(24, (band.bandwidth - 2 * (k - 1)) / Math.max(1, k)));
  const marks = svgEl('g', { class: 'marks' });
  const labels = svgEl('g', { class: 'labels' });
  const hits = svgEl('g', { class: 'hits' });
  const tips = [];
  const y0 = y(0);
  const totals = stacked ? xs.map((_, i) => series.reduce((acc, s) => {
    const v = s.values[i];
    if (isNum(v)) { if (v >= 0) acc.pos += v; else acc.neg += v; }
    return acc;
  }, { pos: 0, neg: 0 })) : null;
  const barCount = stacked ? n : n * k;
  const tipRows = (i, cur) => {
    const rows = series.map((s, j) => ({ name: s.name, color: seriesColor(s, j, theme), value: fmt.value(s.values[i]), current: j === cur }));
    if (stacked) rows.push({ name: 'Total', value: fmt.value(totals[i].pos + totals[i].neg), total: true });
    return rows;
  };
  for (let i = 0; i < n; i++) {
    const cx = band.center(i);
    const cat = xLabel(xs[i]);
    if (stacked) {
      const x = cx - barW / 2;
      series.forEach((s, j) => {
        const e = stack[j][i];
        if (!e || e.end === e.start) return;
        const pos = e.end > e.start || (e.end === e.start && e.end >= 0);
        let ys = y(e.start);
        const ye = y(e.end);
        if (e.start !== 0) ys += pos ? -2 : 2; // the 2 px surface gap to the segment below
        const outer = pos ? e.end === totals[i].pos : e.end === totals[i].neg;
        if ((pos && ys <= ye) || (!pos && ys >= ye)) return; // thinner than the gap
        const path = svgEl('path', { class: 'mark bar', d: barPath(x, barW, ys, ye, outer ? 4 : 0), fill: seriesColor(s, j, theme), 'data-value': s.values[i], 'data-start': e.start, 'data-end': e.end, 'data-series': j });
        marks.appendChild(path);
        const t = tips.length;
        tips.push({ title: cat, rows: tipRows(i, j), ax: cx, ay: Math.min(ys, ye), marks: [path] });
        hits.appendChild(hitRect(svg, cx - band.step / 2, Math.min(ys, ye) - 1, band.step, Math.abs(ys - ye) + 2, t));
      });
      const total = totals[i].pos + totals[i].neg;
      const lbl = fmt.value(total);
      if (barCount <= 12 && textWidth(lbl, 10) <= band.step - 4 && (totals[i].pos || totals[i].neg)) {
        const yt = totals[i].pos ? y(totals[i].pos) - 6 : y(totals[i].neg) + 14;
        labels.appendChild(svgEl('text', { class: 'data-label', x: cx, y: Number(yt.toFixed(2)), 'text-anchor': 'middle' }, lbl));
      }
    } else {
      const groupW = k * barW + 2 * (k - 1);
      const x0 = cx - groupW / 2;
      series.forEach((s, j) => {
        const v = s.values[i];
        const x = x0 + j * (barW + 2);
        const t = tips.length;
        let path = null;
        if (isNum(v) && v !== 0) {
          path = svgEl('path', { class: 'mark bar', d: barPath(x, barW, y0, y(v)), fill: seriesColor(s, j, theme), 'data-value': v, 'data-series': j });
          marks.appendChild(path);
          const lbl = fmt.value(v);
          const room = k === 1 ? band.step - 4 : barW + 4;
          if (barCount <= 12 && textWidth(lbl, 10) <= room) {
            const yt = v >= 0 ? y(v) - 6 : y(v) + 14;
            labels.appendChild(svgEl('text', { class: 'data-label', x: Number((x + barW / 2).toFixed(2)), y: Number(yt.toFixed(2)), 'text-anchor': 'middle' }, lbl));
          }
        }
        tips.push({ title: cat, rows: tipRows(i, j), ax: x + barW / 2, ay: isNum(v) ? Math.min(y(v), y0) : y0, marks: path ? [path] : [] });
        const slotL = j === 0 ? cx - band.step / 2 : x - 1;
        const slotR = j === k - 1 ? cx + band.step / 2 : x + barW + 1;
        hits.appendChild(hitRect(svg, slotL, top, slotR - slotL, PLOT_H, t));
      });
    }
  }
  svg.append(marks, labels);
  xAxis(svg, xs.map((v, i) => ({ x: band.center(i), label: xLabel(v) })), bottom, band.step);
  svg.appendChild(hits);
  return { svg, tips, hover: 'mark', legend: legendOf(series, theme, 'rect') };
}

// ---------------------------------------------------------------- line / area
function lineOrArea(widget, { theme = 'light', width = 560 } = {}, area = false) {
  const fmt = fmtOf(widget);
  const series = foldSeries(seriesOf(widget));
  const rawX = xValuesOf(widget);
  const time = isTimeAxis(rawX);
  const order = rawX.map((_, i) => i);
  if (time) order.sort((a, b) => (Date.parse(rawX[a] ?? '') || 0) - (Date.parse(rawX[b] ?? '') || 0));
  const xs = order.map((i) => rawX[i]);
  const S = series.map((s) => ({ ...s, values: order.map((i) => s.values[i]) }));
  const n = xs.length;
  const stacked = area && widget.stacked === true && S.length > 1;
  const stack = stacked ? stackSeries(S) : null;
  let lo = Infinity;
  let hi = -Infinity;
  S.forEach((s, j) => s.values.forEach((v, i) => {
    const e = stacked ? stack[j][i] : null;
    const a = stacked ? (e ? [e.start, e.end] : []) : (isNum(v) ? [v] : []);
    for (const q of a) { lo = Math.min(lo, q); hi = Math.max(hi, q); }
  }));
  if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
  if (area) { lo = Math.min(0, lo); hi = Math.max(0, hi); }
  const ticks = niceTicks(lo, hi);
  const W = Math.max(240, width);
  const top = 18;
  const bottom = top + PLOT_H;
  const left = leftMargin(ticks, fmt);
  const direct = S.length <= 4;
  const lastIdx = (s) => { for (let i = s.values.length - 1; i >= 0; i--) if (isNum(s.values[i])) return i; return -1; };
  const endLabels = direct ? S.map((s) => { const i = lastIdx(s); return i >= 0 ? fmt.value(s.values[i]) : ''; }) : [];
  const right = W - 10 - (direct ? Math.ceil(Math.max(0, ...endLabels.map((l) => textWidth(l, 10)))) + 10 : 0);
  const H = bottom + 30;
  const kind = area ? 'Area chart' : 'Line chart';
  const svg = root(W, H, `${kind}: ${widget.title || 'untitled'}, ${n} ${n === 1 ? 'point' : 'points'}, series ${seriesNames(S)}`);
  const y = yAxis(svg, ticks, fmt, { top, bottom, left, right });
  let px;
  if (time) {
    const ms = xs.map((v) => Date.parse(v ?? ''));
    const xsc = scaleLinear([ms[0], ms[n - 1]], [left + 6, right - 6]);
    px = (i) => (ms[0] === ms[n - 1] ? (left + right) / 2 : xsc(ms[i]));
  } else {
    px = (i) => (n <= 1 ? (left + right) / 2 : left + 6 + (i * (right - left - 12)) / (n - 1));
  }
  const crossX = svgEl('line', { class: 'crosshair', x1: 0, x2: 0, y1: top, y2: bottom, visibility: 'hidden' });
  svg.appendChild(crossX);
  const marks = svgEl('g', { class: 'marks' });
  const f = (v) => Number(v.toFixed(2));
  const upper = (j, i) => (stacked ? stack[j][i]?.end : S[j].values[i]);
  const lower = (j, i) => (stacked ? stack[j][i]?.start : 0);
  S.forEach((s, j) => {
    const color = seriesColor(s, j, theme);
    const segs = [];
    let cur = [];
    for (let i = 0; i < n; i++) {
      if (isNum(upper(j, i))) cur.push(i);
      else if (cur.length) { segs.push(cur); cur = []; }
    }
    if (cur.length) segs.push(cur);
    for (const seg of segs) {
      if (area) {
        const upPts = seg.map((i) => `${f(px(i))},${f(y(upper(j, i)))}`);
        const bot = seg.slice().reverse().map((i) => `${f(px(i))},${f(y(lower(j, i)))}`);
        marks.appendChild(svgEl('path', { class: 'mark area-fill', d: `M${upPts.join('L')}L${bot.join('L')}Z`, fill: color, 'fill-opacity': stacked ? 0.22 : 0.1, 'data-series': j }));
      }
      marks.appendChild(svgEl('path', { class: 'mark line', d: `M${seg.map((i) => `${f(px(i))},${f(y(upper(j, i)))}`).join('L')}`, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'data-series': j }));
    }
    const li = lastIdx(s);
    const every = n <= 30;
    for (let i = 0; i < n; i++) {
      if (!isNum(upper(j, i)) || (!every && i !== li)) continue;
      marks.appendChild(svgEl('circle', { class: 'mark marker ring', cx: f(px(i)), cy: f(y(upper(j, i))), r: 4, fill: color, 'stroke-width': 2, 'data-series': j }));
    }
  });
  svg.appendChild(marks);
  if (direct) {
    const labels = svgEl('g', { class: 'labels' });
    const placed = [];
    S.map((s, j) => ({ j, i: lastIdx(s) })).filter((d) => d.i >= 0)
      .map((d) => ({ ...d, py: y(upper(d.j, d.i)) })).sort((a, b) => a.py - b.py)
      .forEach((d) => {
        if (placed.some((p) => Math.abs(p - d.py) < 12)) return; // converging ends: the legend and the tooltip carry it
        placed.push(d.py);
        labels.appendChild(svgEl('text', { class: 'data-label end-label', x: f(px(d.i) + 8), y: f(d.py + 4) }, endLabels[d.j]));
      });
    svg.appendChild(labels);
  }
  const slot = n > 1 ? (right - left) / (n - 1) : right - left;
  xAxis(svg, xs.map((v, i) => ({ x: px(i), label: xLabel(v) })), bottom, slot);
  const hits = svgEl('g', { class: 'hits' });
  const tips = [];
  for (let i = 0; i < n; i++) {
    const xL = i === 0 ? left : (px(i - 1) + px(i)) / 2;
    const xR = i === n - 1 ? right : (px(i) + px(i + 1)) / 2;
    const ys = S.map((_, j) => upper(j, i)).filter(isNum).map((v) => y(v));
    tips.push({
      title: xLabel(xs[i]),
      rows: S.map((s, j) => ({ name: s.name, color: seriesColor(s, j, theme), value: fmt.value(s.values[i]) })),
      ax: px(i), ay: ys.length ? Math.min(...ys) : bottom, cross: { x: px(i) }, marks: [],
    });
    hits.appendChild(hitRect(svg, xL, top, xR - xL, PLOT_H, i));
  }
  svg.appendChild(hits);
  svg._cross = { x: crossX };
  return { svg, tips, hover: 'cross', legend: legendOf(S, theme, area ? 'rect' : 'line') };
}

export const lineChart = (w, o) => lineOrArea(w, o, false);
export const areaChart = (w, o) => lineOrArea(w, o, true);

// ---------------------------------------------------------------- donut
export const MAX_SLICES = 8;

/** The slices of a pie widget: positive values only, from the 8th on folded into "Other". */
export function slicesOf(widget) {
  const data = widget?.data || {};
  const xi = colIndex(data, widget?.x);
  const yi = colIndex(data, Array.isArray(widget?.y) ? widget.y[0] : null);
  const all = rowsOf(data).map((r) => ({ name: xLabel(xi >= 0 ? r[xi] : ''), value: yi >= 0 && isNum(r[yi]) ? r[yi] : null }))
    .filter((s) => isNum(s.value) && s.value > 0);
  if (all.length <= MAX_SLICES) return all;
  const keep = all.slice(0, MAX_SLICES - 1);
  const rest = all.slice(MAX_SLICES - 1).reduce((a, s) => a + s.value, 0);
  return [...keep, { name: OTHER, value: rest, other: true }];
}

function arcPath(cx, cy, r0, r1, a0, a1) {
  const f = (v) => Number(v.toFixed(2));
  const pt = (r, a) => `${f(cx + r * Math.sin(a))},${f(cy - r * Math.cos(a))}`;
  const g1 = Math.min(1 / r1, (a1 - a0) / 4);
  const g0 = Math.min(1 / r0, (a1 - a0) / 4);
  const large = a1 - a0 - 2 * g1 > Math.PI ? 1 : 0;
  return `M${pt(r1, a0 + g1)}A${f(r1)},${f(r1)} 0 ${large} 1 ${pt(r1, a1 - g1)}L${pt(r0, a1 - g0)}A${f(r0)},${f(r0)} 0 ${large} 0 ${pt(r0, a0 + g0)}Z`;
}

export function pieChart(widget, { theme = 'light', width = 560 } = {}) {
  const fmt = fmtOf(widget);
  const slices = slicesOf(widget);
  const total = slices.reduce((a, s) => a + s.value, 0);
  const W = Math.max(240, width);
  const R = Math.max(50, Math.min(110, (W - 140) / 2));
  const r0 = R * 0.62;
  const cx = W / 2;
  const cy = R + 26;
  const H = 2 * R + 52;
  const svg = root(W, H, `Donut chart: ${widget.title || 'untitled'}, ${slices.length} ${slices.length === 1 ? 'slice' : 'slices'}`);
  const marks = svgEl('g', { class: 'marks' });
  const labels = svgEl('g', { class: 'labels' });
  const hits = svgEl('g', { class: 'hits' });
  const tips = [];
  const pal = palette(theme);
  const yName = Array.isArray(widget.y) ? String(widget.y[0] ?? '') : '';
  let a = 0;
  slices.forEach((s, i) => {
    const share = total ? s.value / total : 0;
    const a0 = a;
    const a1 = a + share * Math.PI * 2;
    a = a1;
    const color = s.other ? otherColor(theme) : pal[i];
    const full = slices.length === 1;
    const d = full ? `${arcPath(cx, cy, r0, R, 0, Math.PI)}${arcPath(cx, cy, r0, R, Math.PI, Math.PI * 2)}` : arcPath(cx, cy, r0, R, a0, a1);
    const path = svgEl('path', { class: 'mark slice', d, fill: color, 'data-value': s.value, 'data-share': Number(share.toFixed(4)) });
    marks.appendChild(path);
    const pct = `${(share * 100).toFixed(share >= 0.1 ? 0 : 1).replace(/\.0$/, '')}%`;
    if (share >= 0.05) {
      const mid = (a0 + a1) / 2;
      const lx = cx + (R + 10) * Math.sin(mid);
      const ly = cy - (R + 10) * Math.cos(mid);
      const anchor = Math.abs(Math.sin(mid)) < 0.2 ? 'middle' : Math.sin(mid) > 0 ? 'start' : 'end';
      labels.appendChild(svgEl('text', { class: 'data-label slice-label', x: Number(lx.toFixed(2)), y: Number((ly + 4 + (Math.cos(mid) < -0.8 ? 6 : 0)).toFixed(2)), 'text-anchor': anchor }, pct));
    }
    tips.push({ title: s.name, rows: [{ name: yName || 'Value', color, value: `${fmt.value(s.value)} · ${pct}`, current: true }], ax: cx + R * Math.sin((a0 + a1) / 2), ay: cy - R * Math.cos((a0 + a1) / 2), marks: [path] });
    const hd = full ? `${arcPath(cx, cy, r0 - 6, R + 6, 0, Math.PI)}${arcPath(cx, cy, r0 - 6, R + 6, Math.PI, Math.PI * 2)}` : arcPath(cx, cy, Math.max(4, r0 - 6), R + 6, a0, a1);
    hits.appendChild(svgEl('path', { class: 'hit', d: hd, 'data-tip': i }));
  });
  svg.append(marks, labels);
  svg.appendChild(svgEl('text', { class: 'donut-total', x: cx, y: cy + 2, 'text-anchor': 'middle' }, fmt.tick(total)));
  svg.appendChild(svgEl('text', { class: 'tick donut-caption', x: cx, y: cy + 20, 'text-anchor': 'middle' }, 'Total'));
  svg.appendChild(hits);
  const legend = slices.length >= 2 ? slices.map((s, i) => ({ name: s.name, color: s.other ? otherColor(theme) : pal[i], shape: 'rect' })) : [];
  return { svg, tips, hover: 'mark', legend };
}

// ---------------------------------------------------------------- scatter
export function scatterChart(widget, { theme = 'light', width = 560 } = {}) {
  const fmt = fmtOf(widget);
  const data = widget?.data || {};
  const xi = colIndex(data, widget?.x);
  const yName = Array.isArray(widget?.y) ? widget.y[0] : null;
  const yi = colIndex(data, yName);
  const cols = Array.isArray(data.columns) ? data.columns : [];
  const rows = rowsOf(data);
  const li = cols.findIndex((_, j) => j !== xi && j !== yi && rows.some((r) => typeof r[j] === 'string'));
  const pts = rows.map((r) => ({ x: r[xi], y: r[yi], label: li >= 0 && r[li] != null ? String(r[li]) : '' })).filter((p) => isNum(p.x) && isNum(p.y));
  const xt = niceTicks(Math.min(...pts.map((p) => p.x), pts.length ? Infinity : 0), Math.max(...pts.map((p) => p.x), pts.length ? -Infinity : 1));
  const yt = niceTicks(Math.min(...pts.map((p) => p.y), pts.length ? Infinity : 0), Math.max(...pts.map((p) => p.y), pts.length ? -Infinity : 1));
  const W = Math.max(240, width);
  const top = 18;
  const bottom = top + PLOT_H;
  const left = leftMargin(yt, fmt);
  const right = W - 14;
  const H = bottom + 30;
  const svg = root(W, H, `Scatter chart: ${widget.title || 'untitled'}, ${pts.length} points, ${widget.x} against ${yName}`);
  const y = yAxis(svg, yt, fmt, { top, bottom, left, right });
  const x = scaleLinear([xt[0], xt[xt.length - 1]], [left + 6, right - 6]);
  const gx = svgEl('g', { class: 'axis axis-x' });
  const xFmt = (v) => formatValue(v, 'number', '', { compact: true });
  const slot = (right - left) / Math.max(1, xt.length - 1);
  const every = Math.max(1, Math.ceil((Math.max(...xt.map((t) => textWidth(xFmt(t)))) + 10) / slot));
  xt.forEach((t, i) => {
    gx.appendChild(svgEl('line', { class: 'grid', x1: Number(x(t).toFixed(2)), x2: Number(x(t).toFixed(2)), y1: top, y2: bottom }));
    if (i % every === 0) gx.appendChild(svgEl('text', { class: 'tick', x: Number(x(t).toFixed(2)), y: bottom + 18, 'text-anchor': 'middle' }, xFmt(t)));
  });
  svg.appendChild(gx);
  const crossX = svgEl('line', { class: 'crosshair', x1: 0, x2: 0, y1: top, y2: bottom, visibility: 'hidden' });
  const crossY = svgEl('line', { class: 'crosshair', x1: left, x2: right, y1: 0, y2: 0, visibility: 'hidden' });
  svg.append(crossX, crossY);
  const color = palette(theme)[0];
  const marks = svgEl('g', { class: 'marks' });
  const hits = svgEl('g', { class: 'hits' });
  const tips = [];
  const f = (v) => Number(v.toFixed(2));
  pts.forEach((p, i) => {
    const c = svgEl('circle', { class: 'mark marker ring', cx: f(x(p.x)), cy: f(y(p.y)), r: 4, fill: color, 'stroke-width': 2 });
    marks.appendChild(c);
    tips.push({
      title: p.label || `${widget.x} ${formatValue(p.x)}`,
      rows: [...(p.label ? [{ name: String(widget.x), value: formatValue(p.x) }] : []), { name: String(yName), color, value: fmt.value(p.y), current: true }],
      ax: x(p.x), ay: y(p.y), cross: { x: x(p.x), y: y(p.y) }, marks: [c],
    });
    hits.appendChild(svgEl('circle', { class: 'hit', cx: f(x(p.x)), cy: f(y(p.y)), r: 12, 'data-tip': i }));
  });
  svg.append(marks, hits);
  svg._cross = { x: crossX, y: crossY };
  return { svg, tips, hover: 'cross', legend: [] };
}
