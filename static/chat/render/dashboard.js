// The dashboard page of the viewer (addendum i, decision 3) and its
// standalone HTML export. A dashboard is the stored spec of create_dashboard
// ({title, description, widgets}); every text in it was written by a model,
// so it reaches the page through textContent only and the export is the
// serialised DOM (escaped by the serialiser), with no script.
//
// Public API
//   DASHBOARD_MEDIA_TYPE, DASHBOARD_SUFFIX
//   isDashboardFile({media_type, name}) -> bool
//   dashboardStem(name) -> the name without ".dashboard.json"
//   dashboardBody(preview, { theme, width, onDownloadHtml, onDownloadData }) -> <div class="dash">
//       el.setTheme(theme) re-renders charts and KPI tiles in the other palette
//   dashboardHtml(spec) -> '<!doctype html>…' (light palette, inline SVG, the
//       tables in <details>, the CSS inlined, no <script>)
//   dashboardFilename(spec) -> '<title>.html' (safe characters only)

import { chartView, chartSvg, kpiTile, dataTable, legendEl, tipText, CHART_TYPES } from './charts.js';
import { svgEl } from './chartCore.js';

export const DASHBOARD_MEDIA_TYPE = 'application/vnd.sap-btp-agent.dashboard+json';
export const DASHBOARD_SUFFIX = '.dashboard.json';
const FALLBACK_WIDTH = 480;

export function isDashboardFile(f) {
  return String(f?.media_type || '').toLowerCase() === DASHBOARD_MEDIA_TYPE;
}

export function dashboardStem(name) {
  const n = String(name || 'dashboard');
  return n.toLowerCase().endsWith(DASHBOARD_SUFFIX) ? n.slice(0, -DASHBOARD_SUFFIX.length) : n;
}

export function dashboardFilename(spec) {
  const base = String(spec?.title || 'dashboard').replace(/[^\p{L}\p{N} ._-]+/gu, ' ').replace(/\s+/g, ' ').trim().slice(0, 100);
  return `${base || 'dashboard'}.html`;
}

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = String(text);
  return n;
}

const widgetsOf = (spec) => (Array.isArray(spec?.widgets) ? spec.widgets.filter((w) => w && typeof w === 'object') : []);
const isChart = (w) => CHART_TYPES.includes(w.type);

function tableOpts(w) {
  return { format: w.format, unit: w.unit, columns: Array.isArray(w.y) ? w.y : [] };
}

let uid = 0;

function card(w, i, state) {
  const sec = el('section', `dash-card dash-${w.type}${w.size === 2 ? ' size-2' : ''}`);
  const head = el('header', 'dash-card-head');
  const h = el('h3', 'dash-card-title', w.title || (w.type === 'kpi' ? 'KPI' : `Widget ${i + 1}`));
  h.id = `dash-w-${++uid}`;
  sec.setAttribute('aria-labelledby', h.id);
  head.appendChild(h);
  const body = el('div', 'dash-card-body');
  sec.append(head, body);
  let showTable = false;
  let lastWidth = 0;
  const render = () => {
    if (w.type === 'kpi') { body.replaceChildren(kpiTile(w, { theme: state.theme })); return; }
    if (w.type === 'table') { body.replaceChildren(dataTable(w.data)); return; }
    if (showTable) { body.replaceChildren(dataTable(w.data, tableOpts(w))); return; }
    const width = Math.round(body.clientWidth || state.width || FALLBACK_WIDTH);
    lastWidth = width;
    try {
      body.replaceChildren(chartView(w, { theme: state.theme, width }));
    } catch {
      body.replaceChildren(el('p', 'viewer-muted', 'This chart could not be drawn.'));
    }
  };
  if (isChart(w)) {
    const toggle = el('button', 'btn small dash-toggle', 'Table');
    toggle.type = 'button';
    toggle.setAttribute('aria-pressed', 'false');
    toggle.setAttribute('aria-label', `Show ${w.title || 'the chart'} as a table`);
    toggle.addEventListener('click', () => {
      showTable = !showTable;
      toggle.setAttribute('aria-pressed', String(showTable));
      toggle.textContent = showTable ? 'Chart' : 'Table';
      toggle.setAttribute('aria-label', showTable ? `Show ${w.title || 'the table'} as a chart` : `Show ${w.title || 'the chart'} as a table`);
      render();
    });
    head.appendChild(toggle);
  }
  if (typeof w.note === 'string' && w.note) sec.appendChild(el('p', 'dash-note', w.note));
  render();
  return {
    el: sec,
    render,
    resized: () => { if (isChart(w) && !showTable && Math.abs((body.clientWidth || 0) - lastWidth) > 8) render(); },
    body,
  };
}

export function dashboardBody(preview, { theme = 'light', width, onDownloadHtml, onDownloadData } = {}) {
  const spec = preview?.dashboard || {};
  const state = { theme, width };
  const root = el('div', 'dash');
  const head = el('header', 'dash-head');
  head.appendChild(el('h1', 'dash-title', spec.title || 'Dashboard'));
  if (typeof spec.description === 'string' && spec.description) head.appendChild(el('p', 'dash-desc', spec.description));
  if (typeof onDownloadHtml === 'function' || typeof onDownloadData === 'function') {
    const bar = el('div', 'dash-actions');
    if (typeof onDownloadHtml === 'function') {
      const b = el('button', 'btn small', 'Download HTML');
      b.type = 'button';
      b.addEventListener('click', () => onDownloadHtml(spec));
      bar.appendChild(b);
    }
    if (typeof onDownloadData === 'function') {
      const b = el('button', 'btn small', 'Download data');
      b.type = 'button';
      b.addEventListener('click', () => onDownloadData(spec));
      bar.appendChild(b);
    }
    head.appendChild(bar);
  }
  const grid = el('div', 'dash-grid');
  const cards = widgetsOf(spec).map((w, i) => card(w, i, state));
  for (const c of cards) grid.appendChild(c.el);
  if (!cards.length) grid.appendChild(el('p', 'viewer-muted', 'The dashboard has no widget.'));
  root.append(head, grid);
  if (typeof ResizeObserver === 'function') {
    const ro = new ResizeObserver(() => { if (!root.isConnected) { ro.disconnect(); return; } cards.forEach((c) => c.resized()); });
    cards.forEach((c) => ro.observe(c.body));
  }
  root.setTheme = (t) => {
    if (t === state.theme) return;
    state.theme = t;
    cards.forEach((c) => c.render());
  };
  return root;
}

// ---------------------------------------------------------------- export
const EXPORT_CSS = `
:root{--surface:#fff;--canvas:#f6f7f9;--ink:#1b2733;--ink-2:#3d4b59;--muted:#5e6c7b;--line:#dfe4ea;--surface-2:#f1f4f7}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.dash{max-width:1120px;margin:0 auto;padding:28px 20px 40px}
.dash-title{font-size:24px;margin:0 0 6px}
.dash-desc{color:var(--ink-2);margin:0 0 18px;max-width:72ch;white-space:pre-wrap}
.dash-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
@media (max-width:760px){.dash-grid{grid-template-columns:1fr}}
.dash-card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.dash-card.size-2{grid-column:1/-1}
.dash-card-title{font-size:14px;font-weight:600;margin:0 0 8px}
.dash-note{color:var(--muted);font-size:12.5px;margin:8px 0 0}
.chart-svg{display:block;max-width:100%;height:auto;overflow:visible}
.chart-svg .grid{stroke:var(--line);stroke-width:1}
.chart-svg .tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.chart-svg .data-label{fill:var(--ink-2);font-size:10px;font-variant-numeric:tabular-nums}
.chart-svg .donut-total{fill:var(--ink);font-size:20px;font-weight:600}
.chart-svg .ring{stroke:var(--surface)}
.chart-svg .hit{fill:transparent}
.chart-svg .crosshair{display:none}
.chart-legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:0 0 8px;font-size:12px;color:var(--ink-2)}
.legend-item{display:inline-flex;align-items:center;gap:6px}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px}
.swatch-line{height:2px;width:14px;border-radius:1px}
.kpi-value{display:flex;align-items:baseline;gap:6px}
.kpi-number{font-size:36px;font-weight:600;line-height:1.1}
.kpi-unit{font-size:15px;color:var(--ink-2)}
.kpi-delta{display:flex;align-items:baseline;gap:6px;margin-top:6px;font-size:13px}
.kpi-delta-value{font-weight:600}
.kpi-delta-label{color:var(--muted)}
.kpi-spark{display:block;margin-top:10px}
.kpi-spark .ring{stroke:var(--surface)}
details{margin-top:8px}
summary{cursor:pointer;color:var(--muted);font-size:12.5px}
.chart-table-wrap{overflow:auto;max-height:420px;border:1px solid var(--line);border-radius:8px;margin-top:6px}
table{border-collapse:collapse;font-size:12.5px;min-width:100%}
th,td{padding:4px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
thead th{background:var(--surface-2);font-weight:600;color:var(--ink-2)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
@media print{body{background:#fff}.dash-card{break-inside:avoid}details{display:none}}
`;

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function exportChart(w) {
  const width = w.size === 2 ? 1040 : 500;
  const box = el('div', `chart chart-${w.type}`);
  let svg;
  try { svg = chartSvg(w, { theme: 'light', width }); } catch { return el('p', 'dash-note', 'This chart could not be drawn.'); }
  if (svg._chart.legend.length >= 2) box.appendChild(legendEl(svg._chart.legend));
  // no script in the export: the native tooltip (<title>) carries each mark's values
  for (const h of svg.querySelectorAll('.hit')) {
    const t = svg._chart.tips[Number(h.getAttribute('data-tip'))];
    if (t) h.appendChild(svgEl('title', {}, tipText(t)));
  }
  svg.removeAttribute('height');
  box.appendChild(svg);
  return box;
}

export function dashboardHtml(spec) {
  const root = el('main', 'dash');
  root.appendChild(el('h1', 'dash-title', spec?.title || 'Dashboard'));
  if (typeof spec?.description === 'string' && spec.description) root.appendChild(el('p', 'dash-desc', spec.description));
  const grid = el('div', 'dash-grid');
  widgetsOf(spec).forEach((w, i) => {
    const sec = el('section', `dash-card dash-${w.type}${w.size === 2 ? ' size-2' : ''}`);
    sec.appendChild(el('h2', 'dash-card-title', w.title || (w.type === 'kpi' ? 'KPI' : `Widget ${i + 1}`)));
    if (w.type === 'kpi') sec.appendChild(kpiTile(w, { theme: 'light' }));
    else if (w.type === 'table') sec.appendChild(dataTable(w.data));
    else if (isChart(w)) {
      sec.appendChild(exportChart(w));
      const det = el('details');
      det.appendChild(el('summary', null, 'Table'));
      det.appendChild(dataTable(w.data, tableOpts(w)));
      sec.appendChild(det);
    }
    if (typeof w.note === 'string' && w.note) sec.appendChild(el('p', 'dash-note', w.note));
    grid.appendChild(sec);
  });
  root.appendChild(grid);
  return `<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>${escapeHtml(spec?.title || 'Dashboard')}</title>\n<style>${EXPORT_CSS}</style>\n</head>\n<body>\n${root.outerHTML}\n</body>\n</html>\n`;
}
