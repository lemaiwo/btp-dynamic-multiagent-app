// The viewer (addendum f): a panel beside the messages that shows a file of
// the conversation (uploaded or generated, through GET /files/{id}/preview)
// or, in an advanced conversation, a fenced code block the model wrote.
// Nothing is stored: the items are rebuilt from the conversation the client
// holds on every render. Everything a server or a model wrote goes into text
// nodes; the only innerHTML is highlight.js's escaped output and
// renderMarkdown (DOMPurify) for a markdown file.
//
// Public API
//   createViewer({ api, onToggle, onClose }) -> {
//     el,                       // <section class="viewer" role="region" aria-label="Viewer">
//     setConversation(conv),    // rebuild the items; another conversation closes the panel
//     items(),                  // the current items (files first, then code), see viewerItems
//     open(itemOrKey, { focus }), // show an item (an item object, its key, or {code, lang})
//     close(byUser = true),     // hide; byUser sets the "closed by the user" flag of the conversation
//     isOpen(), current(),      // state
//     noteSend(),               // a message was sent: auto-open is allowed again
//     autoOpen(message),        // at `done` of a turn: open what decision 3 says, unless closed by the user
//   }
//   `api` = { filePreview(id), fileUrl(id, inline), fetchFileBlob(id) } (defaults to ../api.js);
//   `onToggle(open)` is called on every open/close; `onClose()` after a close.
// Pure helpers (exported for the tests): viewerItems, codeBlocks,
// codeDownloadName, autoOpenItem, parseCsv, sheetTable, codeBody, MAX_ITEMS,
// the fixed texts. A dashboard (addendum i) is rendered by ./dashboard.js; its
// header has "Download HTML" (the standalone page, built here) and "Download
// data" (the stored JSON), and its title is the dashboard's once loaded.

import * as defaultApi from '../api.js';
import { renderMarkdown, libs } from './markdown.js';
import { formatSize } from '../util.js';
import { dashboardBody, dashboardHtml, dashboardFilename, isDashboardFile } from './dashboard.js';
import { onThemeChange } from '../theme.js';

export const MAX_ITEMS = 50;
export const AUTO_OPEN_MIN_LINES = 5;
export const TEXT_NO_PREVIEW = 'No preview for this file type';
export const TEXT_UNREADABLE = 'The file could not be read';
export const TEXT_PREVIEW_FAILED = 'The preview could not be loaded';
export const TEXT_LOADING = 'Loading the preview…';
const CSV_MAX_ROWS = 200;

const EXT_BY_LANG = {
  abap: 'abap', js: 'js', javascript: 'js', ts: 'ts', typescript: 'ts', py: 'py', python: 'py',
  cds: 'cds', xml: 'xml', json: 'json', sql: 'sql', sh: 'sh', bash: 'sh', shell: 'sh', zsh: 'sh',
  yaml: 'yaml', yml: 'yaml',
};
const FILE_LINE = /^\s*(?:\/\/|#|--|\*)\s*file:\s*(\S+)\s*$/i;

/** The text of an assistant message, as the message list shows it. */
function textOf(message) {
  if (message.content) return message.content;
  return (message.parts || []).filter((p) => p.type === 'text' && p.text).map((p) => p.text.trim()).filter(Boolean).join('\n\n');
}

/**
 * The closed ```lang fences of a markdown text: [{lang, code}]. `mermaid` is
 * a diagram, not code, and is left out; a fence still open (streaming) too.
 */
export function codeBlocks(markdown) {
  const out = [];
  const lines = String(markdown || '').split(/\r?\n/);
  let open = null;
  for (const line of lines) {
    if (!open) {
      const m = /^ {0,3}(`{3,})\s*([\w+#.-]*)[^`]*$/.exec(line);
      if (m) open = { fence: m[1], lang: (m[2] || '').toLowerCase(), body: [] };
    } else if (new RegExp(`^ {0,3}${open.fence}\`*\\s*$`).test(line)) {
      if (open.lang !== 'mermaid') out.push({ lang: open.lang, code: open.body.join('\n') });
      open = null;
    } else open.body.push(line);
  }
  return out;
}

/** The download name of a code item: its `file:` line, else snippet-<n>.<ext>. */
export function codeDownloadName(code, lang, n) {
  const first = String(code || '').split(/\r?\n/).find((l) => l.trim()) || '';
  const m = FILE_LINE.exec(first);
  if (m) {
    const base = m[1].split(/[\\/]/).pop().replace(/[^\w.@+-]/g, '');
    if (base && base !== '.' && base !== '..') return base;
  }
  return `snippet-${n}.${EXT_BY_LANG[String(lang || '').toLowerCase()] || 'txt'}`;
}

function lineCount(code) {
  const s = String(code || '').replace(/\n+$/, '');
  return s ? s.split('\n').length : 0;
}

function fileItem(f, messageId) {
  const id = String(f.id || f.file_id || '');
  const it = { key: `file:${id}`, type: 'file', id, name: f.name || 'file', media_type: f.media_type || '', size: f.size, messageId };
  if (typeof f.title === 'string' && f.title && isDashboardFile(it)) it.title = f.title; // a dashboard part carries its title (addendum i)
  return it;
}

/**
 * The items of a conversation: { files, code }. Files: generated `file`
 * parts and the user's uploads (message.files); code: fenced blocks of
 * assistant text, only when `conversation.access === 'advanced'`. The latest
 * MAX_ITEMS of each, in conversation order.
 */
export function viewerItems(conversation) {
  const files = [];
  const code = [];
  const seen = new Set();
  const advanced = conversation?.access === 'advanced';
  let n = 0;
  for (const m of conversation?.messages || []) {
    const add = (f) => {
      const it = fileItem(f, m.id);
      if (!it.id || seen.has(it.id)) return;
      seen.add(it.id);
      files.push(it);
    };
    for (const f of m.files || []) add(f);
    for (const p of m.parts || []) if (p.type === 'file') add(p);
    if (advanced && m.role === 'assistant') {
      codeBlocks(textOf(m)).forEach((b, i) => {
        n += 1;
        const name = codeDownloadName(b.code, b.lang, n);
        const lines = lineCount(b.code);
        const named = !name.startsWith('snippet-');
        code.push({
          key: `code:${m.id}:${i}`, type: 'code', messageId: m.id, index: i, n,
          lang: b.lang || 'text', code: b.code, lines, name,
          title: named ? name : `${b.lang || 'text'} snippet ${n}`,
        });
      });
    }
  }
  return { files: files.slice(-MAX_ITEMS), code: code.slice(-MAX_ITEMS) };
}

/**
 * What decision 3 opens at `done` of a turn (the closed flag aside): the last
 * generated file of the message, else (advanced only) its last code block of
 * at least five lines; null otherwise.
 */
export function autoOpenItem(conversation, message) {
  if (!message) return null;
  const items = viewerItems(conversation);
  const generated = (message.parts || []).filter((p) => p.type === 'file').map((p) => `file:${p.file_id || p.id}`);
  for (const f of (message.files || [])) if (f.kind === 'generated') generated.push(`file:${f.id}`);
  for (let i = generated.length - 1; i >= 0; i--) {
    const hit = items.files.find((f) => f.key === generated[i]);
    if (hit) return hit;
  }
  if (conversation?.access !== 'advanced') return null;
  const blocks = items.code.filter((c) => c.messageId === message.id && c.lines >= AUTO_OPEN_MIN_LINES);
  return blocks.length ? blocks[blocks.length - 1] : null;
}

/** RFC 4180-ish CSV: rows of strings, or null when it does not parse as one table. */
export function parseCsv(text, maxRows = CSV_MAX_ROWS) {
  const rows = [];
  let row = [];
  let cell = '';
  let quoted = false;
  const s = String(text || '').replace(/\r\n?/g, '\n');
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (quoted) {
      if (ch === '"') {
        if (s[i + 1] === '"') { cell += '"'; i++; } else quoted = false;
      } else cell += ch;
    } else if (ch === '"' && cell === '') quoted = true;
    else if (ch === ',') { row.push(cell); cell = ''; }
    else if (ch === '\n') {
      row.push(cell); rows.push(row); row = []; cell = '';
      if (rows.length > maxRows) return null;
    } else cell += ch;
  }
  if (quoted) return null;
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  if (!rows.length || rows.length > maxRows) return null;
  const width = rows[0].length;
  if (width < 2 || rows.some((r) => r.length !== width)) return null;
  return rows;
}

// ---------------------------------------------------------------- DOM helpers
function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = String(text);
  return n;
}

function cellText(v) {
  if (v == null) return '';
  if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE';
  return String(v);
}

function table(columns, rows, className = 'viewer-table') {
  const t = el('table', className);
  if (columns && columns.length) {
    const thead = el('thead');
    const tr = el('tr');
    for (const c of columns) tr.appendChild(el('th', null, cellText(c)));
    thead.appendChild(tr);
    t.appendChild(thead);
  }
  const tbody = el('tbody');
  for (const r of rows || []) {
    const tr = el('tr');
    for (const v of Array.isArray(r) ? r : []) {
      const td = el('td', typeof v === 'number' ? 'num' : null, cellText(v));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  t.appendChild(tbody);
  return t;
}

/**
 * A sheet like a spreadsheet: `columns` are the column letters of what is
 * shown (the header), every row of `rows` is a data row, numbered from 1 in
 * a sticky first column; a blank row is a row of nulls.
 */
export function sheetTable(columns, rows) {
  const t = el('table', 'viewer-table viewer-grid');
  const thead = el('thead');
  const htr = el('tr');
  const corner = el('th', 'rownum');
  corner.setAttribute('aria-label', 'Row');
  htr.appendChild(corner);
  for (const c of Array.isArray(columns) ? columns : []) {
    const th = el('th', null, cellText(c));
    th.scope = 'col';
    htr.appendChild(th);
  }
  thead.appendChild(htr);
  const tbody = el('tbody');
  const width = Array.isArray(columns) ? columns.length : 0;
  (Array.isArray(rows) ? rows : []).forEach((r, i) => {
    const tr = el('tr');
    const th = el('th', 'rownum', String(i + 1));
    th.scope = 'row';
    tr.appendChild(th);
    const cells = Array.isArray(r) ? r : [];
    for (let j = 0; j < Math.max(width, cells.length); j++) {
      const v = cells[j];
      tr.appendChild(el('td', typeof v === 'number' ? 'num' : null, cellText(v)));
    }
    tbody.appendChild(tr);
  });
  t.append(thead, tbody);
  return t;
}

function svgIcon(d) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '18');
  svg.setAttribute('height', '18');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', d);
  svg.appendChild(p);
  return svg;
}

/** The body of a code item: <pre><code> with highlight.js colours. */
export function codeBody(code, lang) {
  const pre = el('pre', 'viewer-code');
  const c = el('code');
  c.textContent = code;
  const { hljs } = libs();
  if (hljs && lang && hljs.getLanguage && hljs.getLanguage(lang)) {
    try {
      c.innerHTML = hljs.highlight(code, { language: lang, ignoreIllegals: true }).value; // escaped by hljs
      c.classList.add('hljs');
    } catch { c.textContent = code; }
  }
  pre.appendChild(c);
  return pre;
}

function sheetsBody(preview) {
  const wrap = el('div', 'viewer-sheets');
  const sheets = Array.isArray(preview.sheets) ? preview.sheets : [];
  if (!sheets.length) { wrap.appendChild(el('p', 'viewer-muted', 'The workbook has no sheet.')); return wrap; }
  const tabs = el('div', 'viewer-tabs');
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', 'Sheets');
  const panel = el('div', 'viewer-sheet');
  panel.setAttribute('role', 'tabpanel');
  const buttons = [];
  const show = (i) => {
    const s = sheets[i] || {};
    buttons.forEach((b, j) => { b.setAttribute('aria-selected', String(i === j)); b.tabIndex = i === j ? 0 : -1; });
    panel.setAttribute('aria-labelledby', buttons[i].id);
    const scroller = el('div', 'viewer-table-wrap');
    scroller.appendChild(sheetTable(s.columns, s.rows));
    const parts = [scroller];
    const shown = Array.isArray(s.rows) ? s.rows.length : 0;
    if (s.truncated) parts.push(el('p', 'viewer-note', `Showing the first ${shown} of ${Number(s.row_count) || shown} rows.`));
    panel.replaceChildren(...parts);
  };
  sheets.forEach((s, i) => {
    const b = el('button', 'viewer-tab', s.name || `Sheet ${i + 1}`);
    b.type = 'button';
    b.id = `viewer-tab-${i}-${Math.random().toString(36).slice(2, 7)}`;
    b.setAttribute('role', 'tab');
    b.addEventListener('click', () => show(i));
    b.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const j = (i + (e.key === 'ArrowRight' ? 1 : -1) + sheets.length) % sheets.length;
      buttons[j].focus();
      show(j);
    });
    buttons.push(b);
    tabs.appendChild(b);
  });
  wrap.append(tabs, panel);
  show(0);
  if (preview.truncated && !sheets.some((s) => s.truncated)) wrap.appendChild(el('p', 'viewer-note', 'Only the first sheets are shown.'));
  return wrap;
}

function documentBody(preview) {
  const doc = el('div', 'viewer-doc');
  for (const b of Array.isArray(preview.blocks) ? preview.blocks : []) {
    if (!b || typeof b !== 'object') continue;
    if (b.type === 'heading') {
      const level = Math.min(6, Math.max(1, Number(b.level) || 1));
      doc.appendChild(el(`h${level}`, null, b.text || ''));
    } else if (b.type === 'paragraph') doc.appendChild(el('p', null, b.text || ''));
    else if (b.type === 'list') {
      const list = el(b.ordered === true ? 'ol' : 'ul');
      for (const it of Array.isArray(b.items) ? b.items : []) list.appendChild(el('li', null, cellText(it)));
      doc.appendChild(list);
    } else if (b.type === 'table') {
      const rows = Array.isArray(b.rows) ? b.rows : [];
      const scroller = el('div', 'viewer-table-wrap');
      scroller.appendChild(table(rows[0] || [], rows.slice(1)));
      doc.appendChild(scroller);
    }
  }
  if (preview.truncated) doc.appendChild(el('p', 'viewer-note', 'The document is longer; download it to read the rest.'));
  return doc;
}

function textBody(preview) {
  const lang = preview.language;
  const text = String(preview.text ?? '');
  let body;
  if (lang === 'markdown') {
    body = el('div', 'md viewer-md');
    renderMarkdown(body, text, { final: true });
  } else if (lang === 'csv' && parseCsv(text)) {
    const rows = parseCsv(text);
    body = el('div', 'viewer-table-wrap');
    body.appendChild(table(rows[0], rows.slice(1)));
  } else {
    body = el('pre', 'viewer-pre');
    body.textContent = text;
  }
  if (!preview.truncated) return body;
  const wrap = el('div');
  wrap.append(body, el('p', 'viewer-note', 'The file is longer; download it to read the rest.'));
  return wrap;
}

// ---------------------------------------------------------------- the panel
export function createViewer({ api = defaultApi, onToggle, onClose } = {}) {
  const root = el('section', 'viewer');
  root.setAttribute('role', 'region');
  root.setAttribute('aria-label', 'Viewer');
  root.hidden = true;

  const head = el('header', 'viewer-head');
  const pickLabel = el('label', 'viewer-pick');
  pickLabel.appendChild(el('span', 'sr-only', 'Item'));
  const select = el('select');
  select.setAttribute('aria-label', 'Viewer item');
  pickLabel.appendChild(select);
  const titleBox = el('div', 'viewer-titlebox');
  const title = el('h2', 'viewer-title');
  title.tabIndex = -1;
  const meta = el('div', 'viewer-meta');
  titleBox.append(title, meta);
  const actions = el('div', 'viewer-actions');
  const download = el('a', 'btn small viewer-download', 'Download');
  const downloadCode = el('button', 'btn small viewer-download-code', 'Download');
  downloadCode.type = 'button';
  const copy = el('button', 'btn small viewer-copy', 'Copy');
  copy.type = 'button';
  const downloadHtml = el('button', 'btn small viewer-download-html', 'Download HTML');
  downloadHtml.type = 'button';
  downloadHtml.hidden = true;
  const closeBtn = el('button', 'icon-btn viewer-close');
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'Close viewer');
  closeBtn.title = 'Close';
  closeBtn.appendChild(svgIcon('M6 6l12 12M18 6L6 18'));
  actions.append(copy, downloadCode, downloadHtml, download, closeBtn);
  const top = el('div', 'viewer-top');
  top.append(titleBox, actions);
  head.append(top, pickLabel);
  const body = el('div', 'viewer-body');
  root.append(head, body);

  let conv = null;
  let convId = null;
  let items = { files: [], code: [] };
  let selectSig = '';
  let currentItem = null;
  let seq = 0;
  let objectUrl = null;
  const closedByUser = new Set(); // conversation ids
  const previews = new Map(); // file id -> preview answer (successes only)
  let dashSpec = null; // the spec of the dashboard shown, for "Download HTML"
  const theme = () => (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');

  function all() { return [...items.files, ...items.code]; }

  function fillSelect() {
    const sig = JSON.stringify([items.files.map((f) => [f.key, f.name]), items.code.map((c) => [c.key, c.title, c.lines])]);
    if (sig !== selectSig) {
      selectSig = sig;
      select.replaceChildren();
      const group = (label, list, text) => {
        if (!list.length) return;
        const og = el('optgroup');
        og.label = label;
        for (const it of list) {
          const o = el('option', null, text(it));
          o.value = it.key;
          og.appendChild(o);
        }
        select.appendChild(og);
      };
      group('Files', items.files, (f) => f.title || f.name);
      group('Code', items.code, (c) => `${c.title} (${c.lines === 1 ? '1 line' : `${c.lines} lines`})`);
      if (currentItem && !all().some((i) => i.key === currentItem.key)) {
        // an item no longer listed (an ad hoc block, a streaming one): keep it selectable
        const o = el('option', null, currentItem.title || currentItem.name);
        o.value = currentItem.key;
        select.appendChild(o);
      }
    }
    if (currentItem) select.value = currentItem.key;
    pickLabel.hidden = all().length < 2 && !!currentItem && all().some((i) => i.key === currentItem.key);
  }

  function revoke() {
    if (objectUrl) { try { URL.revokeObjectURL(objectUrl); } catch { /* jsdom */ } objectUrl = null; }
  }

  function setOpen(open) {
    const was = !root.hidden;
    root.hidden = !open;
    if (was !== open && typeof onToggle === 'function') onToggle(open);
  }

  function errorState(status, item) {
    const box = el('div', 'viewer-error');
    box.setAttribute('role', 'status');
    const text = status === 415 ? TEXT_NO_PREVIEW : status === 422 ? TEXT_UNREADABLE : TEXT_PREVIEW_FAILED;
    box.appendChild(el('p', null, text));
    const a = el('a', 'btn small', 'Download');
    a.href = api.fileUrl(item.id);
    a.setAttribute('download', item.name || '');
    box.appendChild(a);
    return box;
  }

  function inlineBody(preview, item) {
    const mt = String(preview.media_type || item.media_type || '');
    const wrap = el('div', 'viewer-inline');
    const url = typeof preview.url === 'string' && preview.url.startsWith('/chat/api/files/') ? preview.url : api.fileUrl(item.id, true);
    if (mt === 'application/pdf') {
      const frame = el('iframe', 'viewer-frame');
      frame.title = item.name || 'PDF';
      frame.src = url;
      wrap.appendChild(frame);
    } else {
      const img = el('img', 'viewer-img');
      img.alt = item.name || '';
      wrap.appendChild(img);
      const mine = seq;
      Promise.resolve(api.fetchFileBlob ? api.fetchFileBlob(item.id) : null).then((blob) => {
        if (mine !== seq) return;
        if (!blob) { img.src = url; return; }
        revoke();
        objectUrl = URL.createObjectURL(blob);
        img.src = objectUrl;
      }).catch(() => { if (mine === seq) wrap.replaceChildren(errorState(0, item)); });
    }
    return wrap;
  }

  function dashboardPreview(preview, item) {
    const spec = preview.dashboard && typeof preview.dashboard === 'object' ? preview.dashboard : {};
    dashSpec = spec;
    const n = Array.isArray(spec.widgets) ? spec.widgets.length : 0;
    if (spec.title) { title.textContent = String(spec.title); title.title = title.textContent; }
    meta.textContent = ['Dashboard', n === 1 ? '1 widget' : `${n} widgets`, item.size != null ? formatSize(item.size) : ''].filter(Boolean).join(' · ');
    downloadHtml.hidden = false;
    downloadHtml.disabled = false;
    download.textContent = 'Download data';
    return dashboardBody(preview, { theme: theme() });
  }

  function previewBody(preview, item) {
    switch (preview?.kind) {
      case 'sheets': return sheetsBody(preview);
      case 'document': return documentBody(preview);
      case 'text': return textBody(preview);
      case 'inline': return inlineBody(preview, item);
      case 'dashboard': return dashboardPreview(preview, item);
      default: return errorState(415, item);
    }
  }

  function header(item) {
    title.textContent = item.title || item.name;
    title.title = title.textContent;
    const isCode = item.type === 'code';
    download.hidden = isCode;
    downloadCode.hidden = !isCode;
    copy.hidden = !isCode;
    const dash = !isCode && isDashboardFile(item);
    dashSpec = null;
    downloadHtml.hidden = !dash;
    downloadHtml.disabled = true; // until the preview is loaded
    download.textContent = dash ? 'Download data' : 'Download';
    if (isCode) {
      meta.textContent = `${item.lang} · ${item.lines === 1 ? '1 line' : `${item.lines} lines`}`;
      downloadCode.setAttribute('aria-label', `Download ${item.name}`);
    } else {
      meta.textContent = item.size != null ? formatSize(item.size) : '';
      download.href = api.fileUrl(item.id);
      download.setAttribute('download', item.name || '');
      download.setAttribute('aria-label', `Download ${item.name}`);
    }
  }

  async function show(item) {
    const mine = ++seq;
    revoke();
    currentItem = item;
    header(item);
    fillSelect();
    if (item.type === 'code') {
      body.replaceChildren(codeBody(item.code, item.lang));
      return;
    }
    if (previews.has(item.id)) {
      body.replaceChildren(previewBody(previews.get(item.id), item));
      return;
    }
    const loading = el('p', 'viewer-muted viewer-loading', TEXT_LOADING);
    loading.setAttribute('role', 'status');
    body.replaceChildren(loading);
    try {
      const preview = await api.filePreview(item.id);
      if (mine !== seq) return;
      if (preview && typeof preview === 'object') previews.set(item.id, preview);
      body.replaceChildren(previewBody(preview, item));
    } catch (e) {
      if (mine !== seq) return;
      body.replaceChildren(errorState(e?.status, item));
    }
  }

  function resolve(target) {
    if (!target) return null;
    if (typeof target === 'string') return all().find((i) => i.key === target) || null;
    if (target.key) return all().find((i) => i.key === target.key) || target;
    if (typeof target.code === 'string') {
      // from a code block's "Open" button: the listed item with the same text, else an ad hoc one
      const want = target.code.replace(/\s+$/, '');
      const lang = String(target.lang || '').toLowerCase();
      const hit = [...items.code].reverse().find((c) => c.code.replace(/\s+$/, '') === want && (!lang || c.lang === lang));
      if (hit) return hit;
      const n = items.code.length + 1;
      const name = codeDownloadName(target.code, lang, n);
      return { key: `code:adhoc:${n}`, type: 'code', n, lang: lang || 'text', code: target.code, lines: lineCount(target.code), name, title: name.startsWith('snippet-') ? `${lang || 'text'} snippet ${n}` : name };
    }
    return null;
  }

  function open(target, { focus = false } = {}) {
    const item = resolve(target);
    if (!item) return false;
    setOpen(true);
    show(item);
    if (focus) title.focus();
    return true;
  }

  function close(byUser = true) {
    if (root.hidden) return;
    seq++;
    revoke();
    if (byUser && convId) closedByUser.add(convId);
    currentItem = null;
    body.replaceChildren();
    setOpen(false);
    if (typeof onClose === 'function') onClose(byUser);
  }

  function setConversation(next) {
    const id = next?.id ?? null;
    if (id !== convId) {
      close(false);
      previews.clear();
      convId = id;
    }
    if (next && next === conv) return; // the same object: nothing to rebuild
    conv = next || null;
    items = viewerItems(conv);
    if (currentItem?.type === 'code' && !all().some((i) => i.key === currentItem.key) && conv?.access !== 'advanced') {
      close(false); // access went basic: code is no viewer item any more
      return;
    }
    if (!root.hidden) fillSelect();
  }

  function autoOpen(message) {
    if (!conv || (convId && closedByUser.has(convId))) return false;
    const item = autoOpenItem(conv, message);
    return item ? open(item) : false;
  }

  function noteSend() { if (convId) closedByUser.delete(convId); }

  // ---- events ----
  select.addEventListener('change', () => { const it = resolve(select.value); if (it) show(it); });
  closeBtn.addEventListener('click', () => close(true));
  root.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.defaultPrevented) { e.preventDefault(); close(true); }
  });
  copy.addEventListener('click', async () => {
    if (currentItem?.type !== 'code') return;
    try {
      await navigator.clipboard.writeText(currentItem.code);
      copy.textContent = 'Copied';
    } catch { copy.textContent = 'Copy failed'; }
    setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
  });
  downloadHtml.addEventListener('click', () => {
    if (!dashSpec) return;
    const blob = new Blob([dashboardHtml(dashSpec)], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = el('a');
    a.href = url;
    a.download = dashboardFilename(dashSpec);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => { try { URL.revokeObjectURL(url); } catch { /* jsdom */ } }, 1000);
  });
  onThemeChange((t) => {
    const dash = body.querySelector('.dash');
    if (dash && typeof dash.setTheme === 'function') dash.setTheme(t);
  });
  downloadCode.addEventListener('click', () => {
    if (currentItem?.type !== 'code') return;
    const blob = new Blob([currentItem.code.endsWith('\n') ? currentItem.code : `${currentItem.code}\n`], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = el('a');
    a.href = url;
    a.download = currentItem.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => { try { URL.revokeObjectURL(url); } catch { /* jsdom */ } }, 1000);
  });

  return {
    el: root,
    setConversation,
    items: () => ({ files: [...items.files], code: [...items.code] }),
    open,
    close,
    isOpen: () => !root.hidden,
    current: () => currentItem,
    noteSend,
    autoOpen,
    isClosedByUser: () => !!convId && closedByUser.has(convId),
  };
}
