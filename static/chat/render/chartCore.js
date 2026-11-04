// The pure core of the dashboard charts (addendum i, decision 4): the fixed
// categorical palette, scales, nice ticks, number formatting, the "Other"
// fold and the series read from a widget's data. No DOM except the small SVG
// element helper. Imported by chartMarks.js and re-exported by charts.js.

export const SVG_NS = 'http://www.w3.org/2000/svg';

/** Categorical colours in fixed order, never cycled (validated for both surfaces). */
export const CHART_PALETTE = Object.freeze({
  light: Object.freeze(['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948']),
  dark: Object.freeze(['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']),
});
/** The de-emphasis hue of a folded "Other" series. */
export const OTHER_COLOR = Object.freeze({ light: '#8b97a5', dark: '#6f7c8a' });
/** Status colours, for a KPI delta only (always with an arrow glyph and the number). */
export const STATUS_COLOR = Object.freeze({ good: '#0ca30c', critical: '#d03b3b' });
export const MAX_SERIES = 8;
export const OTHER = 'Other';

export function palette(theme) { return theme === 'dark' ? CHART_PALETTE.dark : CHART_PALETTE.light; }
export function otherColor(theme) { return theme === 'dark' ? OTHER_COLOR.dark : OTHER_COLOR.light; }

/** Series colour by index; a folded "Other" wears the de-emphasis hue. */
export function seriesColor(series, i, theme) {
  if (series && series.other) return otherColor(theme);
  return palette(theme)[Math.min(i, MAX_SERIES - 1)];
}

// ---------------------------------------------------------------- numbers
export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * "Nice" ticks covering [min, max]: steps of 1, 2, 2.5 or 5 x 10^n, about
 * `count` of them, the first <= min and the last >= max. A flat domain is
 * widened so there is always an axis.
 */
export function niceTicks(min, max, count = 5) {
  let lo = Number(min);
  let hi = Number(max);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) { lo = 0; hi = 1; }
  if (lo > hi) [lo, hi] = [hi, lo];
  if (lo === hi) {
    const pad = lo === 0 ? 1 : Math.abs(lo) * 0.5;
    lo -= lo === 0 ? 0 : pad;
    hi += pad;
  }
  const raw = (hi - lo) / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(lo / step + 1e-9) * step;
  const end = Math.ceil(hi / step - 1e-9) * step;
  const ticks = [];
  for (let v = start, n = 0; v <= end + step * 1e-6 && n < 50; v += step, n++) {
    ticks.push(Number(v.toPrecision(12)));
  }
  return ticks;
}

/** A linear scale: s(v) maps domain to range; s.invert, s.domain, s.range. */
export function scaleLinear(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  const s = (v) => r0 + (Number(v) - d0) * k;
  s.invert = (px) => (k === 0 ? d0 : d0 + (px - r0) / k);
  s.domain = [d0, d1];
  s.range = [r0, r1];
  return s;
}

/** A band scale over `n` slots: start(i), bandwidth, step. */
export function scaleBand(n, range, padding = 0.25) {
  const [r0, r1] = range;
  const step = n > 0 ? (r1 - r0) / n : 0;
  const bandwidth = step * (1 - padding);
  return { start: (i) => r0 + i * step + (step - bandwidth) / 2, center: (i) => r0 + i * step + step / 2, bandwidth, step };
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$/;

/** True when every non-null x value is an ISO date string (a time axis). */
export function isTimeAxis(values) {
  const vs = values.filter((v) => v != null);
  return vs.length > 1 && vs.every((v) => typeof v === 'string' && ISO_DATE.test(v) && Number.isFinite(Date.parse(v)));
}

/** An x label: a date as YYYY-MM-DD, a number formatted, a string as given. */
export function xLabel(v) {
  if (v == null) return '';
  if (typeof v === 'string' && ISO_DATE.test(v)) return v.slice(0, 10);
  if (isNum(v)) return formatValue(v);
  return String(v);
}

const NF = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const NF1 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });

function compactNumber(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return `${NF1.format(v / 1e9)}B`;
  if (a >= 1e6) return `${NF1.format(v / 1e6)}M`;
  if (a >= 1e4) return `${NF1.format(v / 1e3)}K`;
  return NF.format(v);
}

/**
 * A value by the widget's `format` and `unit`: `1,234`, `1,234 GB`, `12.5%`
 * (the value is the percentage), `€ 1,234` (the unit as given, in front).
 * `compact` writes 12.9K / 4.2M from 10,000 on (axis ticks, KPI heroes).
 * Not a number: an en dash.
 */
export function formatValue(v, format = 'number', unit = '', { compact = false } = {}) {
  if (!isNum(v)) return v == null || v === '' ? '–' : String(v);
  const n = compact ? compactNumber(v) : NF.format(v);
  const u = typeof unit === 'string' ? unit.trim() : '';
  if (format === 'percent') return `${n}%`;
  if (format === 'currency') return u ? `${u} ${n}` : n;
  return u ? `${n} ${u}` : n;
}

// ---------------------------------------------------------------- data
/** The column index of a name, or -1. */
export function colIndex(data, name) {
  return Array.isArray(data?.columns) ? data.columns.indexOf(name) : -1;
}

export function rowsOf(data) {
  return Array.isArray(data?.rows) ? data.rows.filter(Array.isArray) : [];
}

/** The widget's y series: [{name, values}] over its rows (non-numbers become null). */
export function seriesOf(widget) {
  const data = widget?.data || {};
  const rows = rowsOf(data);
  const ys = Array.isArray(widget?.y) ? widget.y : [];
  return ys.map((name) => {
    const j = colIndex(data, name);
    return { name: String(name), values: rows.map((r) => (j >= 0 && isNum(r[j]) ? r[j] : null)) };
  }).filter((s) => s.name);
}

export function xValuesOf(widget) {
  const data = widget?.data || {};
  const j = colIndex(data, widget?.x);
  return rowsOf(data).map((r) => (j >= 0 ? r[j] ?? null : null));
}

/**
 * At most MAX_SERIES coloured series: from the 9th on they fold into one
 * "Other" series (value per point = the sum of the folded ones; null when
 * all of them are null), marked `other: true`.
 */
export function foldSeries(series, max = MAX_SERIES) {
  const list = Array.isArray(series) ? series : [];
  if (list.length <= max) return list.slice();
  const keep = list.slice(0, max);
  const rest = list.slice(max);
  const len = Math.max(...rest.map((s) => s.values.length));
  const values = Array.from({ length: len }, (_, i) => {
    let sum = null;
    for (const s of rest) if (isNum(s.values[i])) sum = (sum ?? 0) + s.values[i];
    return sum;
  });
  return [...keep, { name: OTHER, values, other: true }];
}

/** Stacked extents per point: for each series {start, end} (positives up, negatives down). */
export function stackSeries(series) {
  const len = Math.max(0, ...series.map((s) => s.values.length));
  const pos = new Array(len).fill(0);
  const neg = new Array(len).fill(0);
  return series.map((s) => s.values.map((v, i) => {
    if (!isNum(v)) return null;
    const base = v >= 0 ? pos : neg;
    const start = base[i];
    base[i] += v;
    return { start, end: base[i] };
  }));
}

// ---------------------------------------------------------------- SVG
export function svgEl(tag, attrs = {}, text) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) n.setAttribute(k, String(v));
  if (text != null) n.textContent = String(text);
  return n;
}

/** Estimated text width at 11 px (no layout in jsdom, none needed). */
export function textWidth(s, px = 11) { return String(s).length * px * 0.6; }

export function clip(s, maxWidth, px = 11) {
  const t = String(s);
  const max = Math.max(1, Math.floor(maxWidth / (px * 0.6)));
  return t.length <= max ? t : `${t.slice(0, Math.max(1, max - 1))}…`;
}

/**
 * A bar from the baseline y0 to the data end y1: square at the baseline,
 * corners of radius r (<= 4, <= half the width, <= the height) at the data end.
 */
export function barPath(x, w, y0, y1, r = 4) {
  const h = Math.abs(y1 - y0);
  const rr = Math.max(0, Math.min(r, w / 2, h));
  const up = y1 <= y0; // the data end is above the baseline (a positive value)
  const f = (n) => Number(n.toFixed(2));
  if (up) {
    return `M${f(x)},${f(y0)}V${f(y1 + rr)}Q${f(x)},${f(y1)} ${f(x + rr)},${f(y1)}H${f(x + w - rr)}Q${f(x + w)},${f(y1)} ${f(x + w)},${f(y1 + rr)}V${f(y0)}Z`;
  }
  return `M${f(x)},${f(y0)}V${f(y1 - rr)}Q${f(x)},${f(y1)} ${f(x + rr)},${f(y1)}H${f(x + w - rr)}Q${f(x + w)},${f(y1)} ${f(x + w)},${f(y1 - rr)}V${f(y0)}Z`;
}
