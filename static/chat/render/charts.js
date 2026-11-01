// Dashboard charts (addendum i, decision 4): our own SVG renderer, no chart
// library. Pure functions that build DOM; nothing a model wrote goes through
// innerHTML (every label is a text node or an attribute).
//
// Public API
//   chartSvg(widget, { theme, width }) -> <svg role="img" aria-label=…>, with
//       svg._chart = { tips, hover: 'mark'|'cross', legend: [{name, color, shape}] }
//   chartView(widget, { theme, width }) -> <div class="chart">: the legend (>= 2
//       series), the SVG and the hover layer (one tooltip per chart, inside the
//       card; crosshair on line/area/scatter, per-mark on bars and slices; every
//       hit target focusable with the same tooltip; arrow keys step)
//   kpiTile(widget, { theme }) -> <div class="kpi">: hero value + unit, delta
//       (sign, arrow glyph, status colour, label), optional sparkline
//   dataTable(data, { format, unit, columns }) -> the data as a table (the
//       table view of every chart); `columns` = names formatted by format/unit
//   Re-exported from chartCore.js: CHART_PALETTE, OTHER_COLOR, STATUS_COLOR,
//   scaleLinear, scaleBand, niceTicks, formatValue, foldSeries, stackSeries,
//   seriesOf; from chartMarks.js: slicesOf, PLOT_H.

import { svgEl, isNum, formatValue, palette, STATUS_COLOR } from './chartCore.js';
import { barChart, lineChart, areaChart, pieChart, scatterChart } from './chartMarks.js';

export {
  CHART_PALETTE, OTHER_COLOR, STATUS_COLOR, scaleLinear, scaleBand, niceTicks, formatValue,
  foldSeries, stackSeries, seriesOf, MAX_SERIES,
} from './chartCore.js';
export { slicesOf, PLOT_H } from './chartMarks.js';

export const CHART_TYPES = Object.freeze(['bar', 'line', 'area', 'pie', 'scatter']);
/** Above this many hit targets only one is in the Tab order; arrow keys move between them. */
export const MAX_TAB_STOPS = 60;

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = String(text);
  return n;
}

const BUILDERS = { bar: barChart, line: lineChart, area: areaChart, pie: pieChart, scatter: scatterChart };

export function chartSvg(widget, { theme = 'light', width = 560 } = {}) {
  const build = BUILDERS[widget?.type];
  if (!build) throw new Error('unknown chart type');
  const r = build(widget, { theme, width });
  r.svg._chart = { tips: r.tips, hover: r.hover, legend: r.legend };
  return r.svg;
}

/** The text of a tooltip, for a hit target's accessible name and the export's <title>. */
export function tipText(t) {
  return [t.title, ...t.rows.map((r) => `${r.name}: ${r.value}`)].filter(Boolean).join(', ');
}

export function legendEl(items) {
  const box = el('div', 'chart-legend');
  box.setAttribute('role', 'list');
  box.setAttribute('aria-label', 'Legend');
  for (const it of items) {
    const row = el('span', 'legend-item');
    row.setAttribute('role', 'listitem');
    const sw = el('span', `swatch ${it.shape === 'line' ? 'swatch-line' : 'swatch-rect'}`);
    sw.style.background = it.color;
    sw.setAttribute('aria-hidden', 'true');
    row.append(sw, el('span', 'legend-name', it.name));
    box.appendChild(row);
  }
  return box;
}

function tipContent(tip, t) {
  const head = el('div', 'chart-tip-title', t.title);
  const rows = t.rows.map((r) => {
    const row = el('div', `chart-tip-row${r.current ? ' current' : ''}${r.total ? ' total' : ''}`);
    const key = el('span', 'chart-tip-key');
    key.setAttribute('aria-hidden', 'true');
    if (r.color) key.style.background = r.color;
    row.append(key, el('strong', 'chart-tip-value', r.value), el('span', 'chart-tip-name', r.name));
    return row;
  });
  tip.replaceChildren(head, ...rows);
}

function wireHover(plot, svg) {
  const { tips, hover } = svg._chart;
  const tip = el('div', 'chart-tip');
  tip.hidden = true;
  tip.setAttribute('aria-hidden', 'true');
  plot.appendChild(tip);
  const hits = [...svg.querySelectorAll('.hit')];
  const roving = hits.length > MAX_TAB_STOPS;
  hits.forEach((h, i) => {
    h.setAttribute('tabindex', roving && i !== 0 ? '-1' : '0');
    const t = tips[Number(h.getAttribute('data-tip'))];
    if (t) h.setAttribute('aria-label', tipText(t));
  });
  let active = [];
  let shown = -1;
  const hide = () => {
    tip.hidden = true;
    shown = -1;
    for (const m of active) m.classList.remove('active');
    active = [];
    const c = svg._cross;
    if (c) for (const line of Object.values(c)) line.setAttribute('visibility', 'hidden');
  };
  const show = (i) => {
    const t = tips[i];
    if (!t) return hide();
    if (i !== shown) {
      for (const m of active) m.classList.remove('active');
      active = t.marks || [];
      for (const m of active) m.classList.add('active');
      tipContent(tip, t);
      shown = i;
    }
    tip.hidden = false;
    const c = svg._cross;
    if (hover === 'cross' && c && t.cross) {
      c.x.setAttribute('x1', t.cross.x); c.x.setAttribute('x2', t.cross.x); c.x.setAttribute('visibility', 'visible');
      if (c.y && t.cross.y != null) { c.y.setAttribute('y1', t.cross.y); c.y.setAttribute('y2', t.cross.y); c.y.setAttribute('visibility', 'visible'); }
    }
    // position inside the card: right of the anchor, flipped left when it would overflow
    const vbW = Number(svg.getAttribute('width')) || 1;
    const shownW = svg.getBoundingClientRect().width || vbW;
    const k = shownW / vbW;
    const ax = t.ax * k;
    const ay = t.ay * k;
    const wrapW = plot.clientWidth || shownW;
    const wrapH = plot.clientHeight || 0;
    const tw = tip.offsetWidth || 160;
    const th = tip.offsetHeight || 60;
    let left = ax + 14;
    if (left + tw > wrapW - 4) left = ax - 14 - tw;
    left = Math.max(4, Math.min(left, Math.max(4, wrapW - tw - 4)));
    let top = ay - th / 2;
    if (wrapH) top = Math.max(4, Math.min(top, wrapH - th - 4));
    else top = Math.max(4, top);
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  };
  const idx = (target) => {
    const h = target && target.closest ? target.closest('.hit') : null;
    return h ? Number(h.getAttribute('data-tip')) : -1;
  };
  const onPoint = (e) => { const i = idx(e.target); if (i >= 0) show(i); };
  svg.addEventListener('pointerover', onPoint);
  svg.addEventListener('pointermove', onPoint);
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('focusin', (e) => { const i = idx(e.target); if (i >= 0) show(i); });
  svg.addEventListener('focusout', hide);
  svg.addEventListener('keydown', (e) => {
    const at = hits.indexOf(e.target);
    if (at < 0) return;
    if (e.key === 'Escape') { hide(); return; }
    const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = hits[(at + step + hits.length) % hits.length];
    if (roving) { e.target.setAttribute('tabindex', '-1'); next.setAttribute('tabindex', '0'); }
    next.focus();
  });
  return { show, hide, tip };
}

/** The chart with its legend and hover layer. */
export function chartView(widget, { theme = 'light', width = 560 } = {}) {
  const wrap = el('div', `chart chart-${widget?.type}`);
  const svg = chartSvg(widget, { theme, width });
  if (svg._chart.legend.length >= 2) wrap.appendChild(legendEl(svg._chart.legend));
  const plot = el('div', 'chart-plot');
  plot.appendChild(svg);
  wrap.appendChild(plot);
  wrap._hover = wireHover(plot, svg);
  return wrap;
}

// ---------------------------------------------------------------- KPI tile
export const ARROW = Object.freeze({ up: '▲', down: '▼', flat: '▶' });

export function sparkline(trend, { theme = 'light', width = 120, height = 32 } = {}) {
  const vs = (Array.isArray(trend) ? trend : []).filter(isNum);
  if (vs.length < 2) return null;
  const lo = Math.min(...vs);
  const hi = Math.max(...vs);
  const f = (v) => Number(v.toFixed(2));
  const px = (i) => 3 + (i * (width - 6)) / (vs.length - 1);
  const py = (v) => (hi === lo ? height / 2 : 3 + ((hi - v) * (height - 6)) / (hi - lo));
  const svg = svgEl('svg', { class: 'kpi-spark', viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', 'aria-label': `Trend, ${vs.length} points, from ${formatValue(vs[0])} to ${formatValue(vs[vs.length - 1])}` });
  const color = palette(theme)[0];
  svg.appendChild(svgEl('path', { class: 'spark-line', d: `M${vs.map((v, i) => `${f(px(i))},${f(py(v))}`).join('L')}`, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  svg.appendChild(svgEl('circle', { class: 'ring', cx: f(px(vs.length - 1)), cy: f(py(vs[vs.length - 1])), r: 3, fill: color, 'stroke-width': 1.5 }));
  return svg;
}

export function kpiTile(widget, { theme = 'light' } = {}) {
  const box = el('div', 'kpi');
  const hero = el('div', 'kpi-value');
  const v = widget?.value;
  const unit = typeof widget?.unit === 'string' ? widget.unit.trim() : '';
  hero.appendChild(el('span', 'kpi-number', isNum(v) ? formatValue(v, 'number', '', { compact: true }) : String(v ?? '–')));
  if (unit) hero.appendChild(el('span', 'kpi-unit', unit));
  box.appendChild(hero);
  if (isNum(widget?.delta)) {
    const d = widget.delta;
    const dir = d > 0 ? 'up' : d < 0 ? 'down' : 'flat';
    const status = dir === 'up' ? 'good' : dir === 'down' ? 'critical' : 'neutral';
    const delta = el('div', `kpi-delta ${dir} ${status}`);
    const num = `${d > 0 ? '+' : d < 0 ? '−' : '±'}${formatValue(Math.abs(d))}${unit === '%' ? '%' : ''}`;
    const arrow = el('span', 'kpi-arrow', ARROW[dir]);
    arrow.setAttribute('aria-hidden', 'true');
    const val = el('span', 'kpi-delta-value', num);
    if (status !== 'neutral') { arrow.style.color = STATUS_COLOR[status]; val.style.color = STATUS_COLOR[status]; }
    delta.append(arrow, val);
    const label = typeof widget.delta_label === 'string' ? widget.delta_label : '';
    if (label) delta.appendChild(el('span', 'kpi-delta-label', label));
    delta.setAttribute('aria-label', `${dir === 'flat' ? 'unchanged' : dir} ${num}${label ? ` ${label}` : ''}`);
    box.appendChild(delta);
  }
  const spark = sparkline(widget?.trend, { theme });
  if (spark) box.appendChild(spark);
  return box;
}

// ---------------------------------------------------------------- table view
export function dataTable(data, { format = 'number', unit = '', columns = [] } = {}) {
  const wrap = el('div', 'chart-table-wrap');
  const t = el('table', 'viewer-table dash-table');
  const cols = Array.isArray(data?.columns) ? data.columns : [];
  const formatted = new Set(Array.isArray(columns) ? columns : []);
  const thead = el('thead');
  const htr = el('tr');
  for (const c of cols) { const th = el('th', null, c); th.scope = 'col'; htr.appendChild(th); }
  thead.appendChild(htr);
  const tbody = el('tbody');
  for (const r of Array.isArray(data?.rows) ? data.rows : []) {
    if (!Array.isArray(r)) continue;
    const tr = el('tr');
    cols.forEach((c, j) => {
      const v = r[j];
      const text = v == null ? '' : isNum(v) ? (formatted.has(c) ? formatValue(v, format, unit) : formatValue(v)) : String(v);
      tr.appendChild(el('td', isNum(v) ? 'num' : null, text));
    });
    tbody.appendChild(tr);
  }
  t.append(thead, tbody);
  wrap.appendChild(t);
  return wrap;
}
