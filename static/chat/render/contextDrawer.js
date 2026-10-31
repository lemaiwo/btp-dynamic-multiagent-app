// The "Context" drawer (addendum e): what the agent gets on the next turn of
// the open conversation, in five sections, as a <dialog> that slides in from
// the right like the agent editor (showModal traps focus and binds Escape).
// Everything the server sent is inserted as text nodes; the view model is
// `contextViewModel` in state.js. Refreshed when opened and after each turn
// while open (the app calls `update`); a failed GET shows one fixed text.

import { contextViewModel } from '../state.js';
import { EXTENSIONS_BY_TYPE } from '../util.js';

export const CONTEXT_ERROR_TEXT = 'The context could not be read. Close the panel and open it again.';
export const IN_CONTEXT = 'In context';
export const ATTACHMENTS_EMPTY = 'No uploaded file in the history.';
export const CONTEXT_LOADING = 'Reading the context…';

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = text;
  return n;
}

function section(title, id) {
  const s = el('section', 'ctx-section');
  const h = el('h3', null, title);
  h.id = id;
  s.setAttribute('aria-labelledby', id);
  s.appendChild(h);
  return s;
}

function dl(rows) {
  const d = el('dl', 'ctx-dl');
  for (const [k, v, extra] of rows) {
    if (v == null || v === '') continue;
    d.appendChild(el('dt', null, k));
    const dd = el('dd', null, v);
    if (extra) { dd.appendChild(document.createTextNode(' ')); dd.appendChild(el('span', 'ctx-kind', extra)); }
    d.appendChild(dd);
  }
  return d;
}

function pre(text) {
  const p = el('pre', 'ctx-pre');
  p.textContent = text;
  return p;
}

function typeLabel(mediaType) {
  const ext = EXTENSIONS_BY_TYPE[mediaType];
  return ext ? ext.toUpperCase() : (mediaType || 'file');
}

function fillBar(who) {
  const wrap = el('div', 'fill');
  if (who.window) {
    const bar = el('div', `fill-bar level-${who.fill.level}`);
    bar.setAttribute('role', 'meter');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-valuenow', String(Math.min(100, Math.max(0, Math.round(who.fill.pct || 0)))));
    bar.setAttribute('aria-label', 'Context window fill');
    const inner = el('div', 'fill-in');
    inner.style.width = `${Math.min(100, Math.max(0, who.fill.pct || 0)).toFixed(1)}%`;
    bar.appendChild(inner);
    wrap.appendChild(bar);
  }
  const text = el('div', 'fill-text');
  const strong = el('strong', null, who.window ? who.pctText : '');
  if (who.window) { text.appendChild(strong); text.appendChild(document.createTextNode(' · ')); }
  text.appendChild(document.createTextNode(`${who.fillText} (${who.fillSource})`));
  wrap.appendChild(text);
  return wrap;
}

/**
 * Build the five sections into `body` from a context answer.
 * `onToggle(fileId, excluded)` returns a promise of the stored state; the
 * switch is disabled while it runs and reverts when it fails.
 */
export function renderContextBody(body, ctx, { onToggle } = {}) {
  const vm = contextViewModel(ctx);
  body.replaceChildren();

  // 1. Who answers
  const who = section('Who answers', 'ctx-who');
  who.appendChild(dl([
    ['Agent', vm.who.agent, vm.who.kind === vm.who.agent ? '' : vm.who.kind],
    ['Tier', vm.who.tier],
    ['Access', vm.who.access],
    ['Model', vm.who.model],
  ]));
  const winLabel = el('div', 'ctx-label', 'Context window');
  who.append(winLabel, fillBar(vm.who));
  body.appendChild(who);

  // 2. Instructions & skills
  const ins = section('Instructions & skills', 'ctx-instructions');
  if (vm.instructions.visible) ins.appendChild(pre(vm.instructions.text));
  else ins.appendChild(el('p', 'ctx-muted', vm.instructions.hiddenText));
  if (vm.instructions.skills.length) {
    ins.appendChild(el('div', 'ctx-label', 'Skills'));
    const ul = el('ul', 'ctx-rows');
    for (const s of vm.instructions.skills) {
      const li = el('li', 'ctx-row');
      const head = document.createDocumentFragment();
      head.appendChild(el('span', 'ctx-row-name', s.name));
      head.appendChild(el('span', `ctx-scope ${s.scope}`, s.scopeLabel));
      head.appendChild(el('span', 'ctx-row-meta', s.charsText));
      if (s.text != null) {
        const det = el('details', 'ctx-details');
        const sum = el('summary', 'ctx-summary');
        sum.appendChild(head);
        det.append(sum, pre(s.text));
        li.appendChild(det);
      } else {
        const line = el('div', 'ctx-summary static');
        line.appendChild(head);
        li.appendChild(line);
      }
      ul.appendChild(li);
    }
    ins.appendChild(ul);
  } else {
    ins.appendChild(el('p', 'ctx-muted', 'No skills attached.'));
  }
  body.appendChild(ins);

  // 3. History
  const hist = section('History', 'ctx-history');
  hist.appendChild(dl([
    ['In the context', vm.history.summary],
    ['Estimated', vm.history.estimatedText],
    ['Measured', vm.history.measuredText],
  ]));
  hist.appendChild(el('div', 'ctx-label', 'Uploaded files'));
  if (!vm.history.attachments.length) {
    hist.appendChild(el('p', 'ctx-muted', ATTACHMENTS_EMPTY));
  } else {
    const table = el('table', 'ctx-table');
    const thead = el('thead');
    const hr = el('tr');
    for (const h of ['Name', 'Type', 'Size', IN_CONTEXT]) hr.appendChild(el('th', null, h));
    thead.appendChild(hr);
    const tbody = el('tbody');
    for (const a of vm.history.attachments) {
      const tr = el('tr', a.excluded ? 'excluded' : '');
      tr.dataset.fileId = a.file_id;
      const name = el('td', 'ctx-file-name', a.name);
      name.title = a.name;
      tr.append(name, el('td', null, typeLabel(a.media_type)), el('td', 'num', a.sizeText));
      const cell = el('td', 'switch-cell');
      const sw = document.createElement('input');
      sw.type = 'checkbox';
      sw.className = 'switch';
      sw.setAttribute('role', 'switch');
      sw.checked = !a.excluded;
      sw.setAttribute('aria-label', `${IN_CONTEXT}: ${a.name}`);
      sw.addEventListener('change', async () => {
        if (typeof onToggle !== 'function') return;
        const excluded = !sw.checked;
        sw.disabled = true;
        try {
          const stored = await onToggle(a.file_id, excluded);
          sw.checked = !(stored === true);
          tr.classList.toggle('excluded', stored === true);
        } catch {
          sw.checked = excluded; // back to where it was (the app showed the toast)
          tr.classList.toggle('excluded', !excluded);
        } finally {
          sw.disabled = false;
        }
      });
      cell.appendChild(sw);
      tr.appendChild(cell);
      tbody.appendChild(tr);
    }
    table.append(thead, tbody);
    hist.appendChild(table);
    hist.appendChild(el('p', 'ctx-muted small', 'A file switched off stays downloadable; the model no longer sees it on the next turn.'));
  }
  body.appendChild(hist);

  // 4. Tools this turn
  const tools = section('Tools this turn', 'ctx-tools');
  if (!vm.tools.length) tools.appendChild(el('p', 'ctx-muted', 'No tools.'));
  for (const g of vm.tools) {
    tools.appendChild(el('div', 'ctx-label', g.label));
    const ul = el('ul', 'ctx-tools-list');
    for (const t of g.items) {
      const li = el('li');
      li.appendChild(el('code', null, t.name));
      if (t.kind === 'delegate' && t.agent) {
        li.appendChild(document.createTextNode(' → '));
        li.appendChild(el('span', 'ctx-target', t.agent));
      }
      ul.appendChild(li);
    }
    tools.appendChild(ul);
  }
  body.appendChild(tools);

  // 5. Usage so far
  const usage = section('Usage so far', 'ctx-usage');
  usage.appendChild(dl([
    ['Total', vm.usage.totalText],
    ['Detail', vm.usage.detail],
  ]));
  body.appendChild(usage);
  return vm;
}

/**
 * createContextDrawer({onRefresh, onToggle}) -> {open, close, isOpen, update}
 *   onRefresh()                 the app re-reads the context (called on open and by the Refresh button)
 *   onToggle(fileId, excluded)  the app posts the switch; resolves to the stored `excluded`
 *   update(ctx, error)          the latest answer (or a failure) while the drawer is open
 */
export function createContextDrawer({ onRefresh, onToggle }) {
  const dialog = document.createElement('dialog');
  dialog.className = 'panel context';
  dialog.setAttribute('aria-labelledby', 'ctx-title');
  document.body.appendChild(dialog);

  const frame = el('div', 'panel-frame');
  const head = el('header', 'panel-head');
  const h = el('h2', null, 'Context');
  h.id = 'ctx-title';
  const headRight = el('div', 'panel-head-actions');
  const refresh = el('button', 'btn small ghost', 'Refresh');
  refresh.type = 'button';
  refresh.addEventListener('click', () => { showLoading(); onRefresh?.(); });
  const close = el('button', 'icon-btn panel-close');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close');
  close.textContent = '×';
  close.addEventListener('click', () => dialog.close());
  headRight.append(refresh, close);
  head.append(h, headRight);
  const sub = el('p', 'panel-sub', 'What the agent gets on the next turn of this conversation.');
  const body = el('div', 'panel-body ctx-body');
  const foot = el('footer', 'panel-foot');
  const done = el('button', 'btn', 'Close');
  done.type = 'button';
  done.addEventListener('click', () => dialog.close());
  foot.appendChild(done);
  frame.append(head, sub, body, foot);
  dialog.appendChild(frame);

  let sig = null;
  function showLoading() {
    sig = null;
    body.replaceChildren(el('p', 'ctx-muted ctx-loading', CONTEXT_LOADING));
  }
  function showError() {
    sig = 'error';
    const p = el('p', 'ctx-error', CONTEXT_ERROR_TEXT);
    p.setAttribute('role', 'alert');
    body.replaceChildren(p);
  }
  function update(ctx, error = false) {
    if (!dialog.open) return;
    if (error) { if (sig !== 'error') showError(); return; }
    if (!ctx) { if (sig !== 'loading') { showLoading(); sig = 'loading'; } return; }
    const next = JSON.stringify(ctx);
    if (next === sig) return;
    sig = next;
    // a re-render after a switch: keep the keyboard focus on that file's switch
    const focused = document.activeElement;
    const keepId = focused && body.contains(focused) && focused.classList.contains('switch') ? focused.closest('tr')?.dataset.fileId : null;
    renderContextBody(body, ctx, { onToggle });
    if (keepId) body.querySelector(`tr[data-file-id="${CSS.escape(keepId)}"] input.switch`)?.focus();
  }
  dialog.addEventListener('close', () => { sig = null; body.replaceChildren(); });

  return {
    open(ctx, error = false) {
      if (dialog.open) return;
      dialog.showModal();
      sig = null;
      if (error) showError(); else if (ctx) update(ctx); else showLoading();
      onRefresh?.();
      refresh.focus();
    },
    close: () => dialog.open && dialog.close(),
    isOpen: () => dialog.open,
    update,
  };
}
