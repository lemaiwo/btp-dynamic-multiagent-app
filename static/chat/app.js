// Bootstrap: layout, router (#/c/<id>, #/agents), store wiring, streaming
// turns; the Basic | Advanced access switch of addendum (d) (developers only:
// it PATCHes the open conversation, is the default of the next one and is
// remembered per principal), the catalogue per access and the picker.

import * as api from './api.js';
import { createStore, initialState, newUserMessage, newAssistantMessage, applyStreamEvent } from './state.js';
import { createSidebar } from './render/sidebar.js';
import { createMessageList, renderEmpty } from './render/messages.js';
import { createComposer } from './composer.js';
import { createAgentsPage } from './render/agents.js';
import { createAgentEditor } from './render/agentEditor.js';
import { createContextDrawer } from './render/contextDrawer.js';
import { createViewer } from './render/viewer.js';
import { pickerGroups, agentNameOf, refOffered, isAdvanced } from './myagents.js';
import { initTheme, setTheme, onSkinChange, THEMES } from './theme.js';
import { titleFromText, formatTokens, formatPct, fillLevel, contextIndicatorText, normalizeAccess, rememberedAccess, rememberAccess } from './util.js';
import { excludedFileIds } from './state.js';

const store = createStore({ ...initialState, catalog: null, myAgents: [], draftAgentRef: null, view: 'chat' });
const root = document.getElementById('app');

// ---------- layout (static, trusted markup: nothing of the model in here) ----------
root.innerHTML = `
<div class="shell">
  <aside class="sidebar" id="sidebar" aria-label="Conversations"></aside>
  <div class="scrim" hidden></div>
  <main class="main" id="main">
    <header class="topbar">
      <button type="button" class="icon-btn hamburger" aria-label="Open conversations" aria-controls="sidebar" aria-expanded="false">
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
      </button>
      <h1 class="title" id="title">SAP BTP Multi-Agent</h1>
      <div class="topbar-right">
        <span class="usage-chip" id="usage-chip" hidden tabindex="0" role="status" aria-label="Conversation usage"></span>
        <button type="button" class="context-btn" id="context-btn" hidden aria-haspopup="dialog" title="What the agent gets on the next turn">
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M4 6h16M4 12h10M4 18h14"/><circle cx="18" cy="12" r="2.5"/></svg>
          <span>Context</span><span class="context-pct" id="context-pct" hidden></span>
        </button>
        <div class="access-switch" id="access" role="radiogroup" aria-label="Access" hidden>
          <button type="button" class="seg" role="radio" aria-checked="true" data-access="basic" title="Basic access: the standard agents">Basic</button>
          <button type="button" class="seg" role="radio" aria-checked="false" data-access="advanced" tabindex="-1" title="Advanced access: the developer agents (ABAP, UI5/Fiori, CAP)">Advanced</button>
        </div>
        <label class="model-pick agent-pick"><span class="sr-only">Agent</span><select id="agent" aria-label="Agent"></select></label>
        <label class="model-pick"><span class="sr-only">Model</span><select id="model" aria-label="Model"></select></label>
        <button type="button" class="icon-btn theme" id="theme" aria-label="Theme" title="Theme" aria-haspopup="menu" aria-expanded="false" aria-controls="theme-menu">
          <svg class="sun" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
          <svg class="moon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"/></svg>
        </button>
        <div class="menu theme-menu" id="theme-menu" role="menu" aria-label="Theme" hidden></div>
        <a class="classic" href="/">Classic chat</a>
      </div>
    </header>
    <div class="banner" id="banner" role="alert" hidden>
      <span>Your session has expired.</span>
      <button type="button" class="btn small" id="reload">Reload</button>
    </div>
    <section class="log" role="log" aria-live="polite" aria-label="Messages">
      <div class="column" id="messages"></div>
    </section>
    <div class="composer-wrap"><div class="column" id="composer"></div></div>
    <section class="page-wrap" id="page" hidden aria-label="Agents and skills"></section>
  </main>
  <div class="toasts" id="toasts" aria-live="polite"></div>
</div>`;

const $ = (sel) => root.querySelector(sel);
const sidebarEl = $('#sidebar');
const mainEl = $('#main');
const scrim = $('.scrim');
const hamburger = $('.hamburger');
const titleEl = $('#title');
const modelSel = $('#model');
const agentSel = $('#agent');
const accessEl = $('#access');
const accessBtns = [...accessEl.querySelectorAll('[role="radio"]')];
const logEl = $('.log');
const composerWrap = $('.composer-wrap');
const pageEl = $('#page');
const usageChip = $('#usage-chip');
const contextBtn = $('#context-btn');
const contextPct = $('#context-pct');
const themeBtn = $('#theme');
const bannerEl = $('#banner');
const messagesEl = $('#messages');
const toastsEl = $('#toasts');

// ---------- toasts ----------
// While a modal <dialog> (the editor, the context drawer) is open it sits in
// the top layer above the page's toast box, so the toast goes into a box of
// the dialog's own (same class, same fixed position) and is seen.
function toastBox() {
  const open = document.querySelector('dialog[open]');
  if (!open) return toastsEl;
  let box = open.querySelector(':scope > .toasts');
  if (!box) {
    box = document.createElement('div');
    box.className = 'toasts';
    box.setAttribute('aria-live', 'polite');
    open.appendChild(box);
  }
  return box;
}
function notify(text, kind = 'info') {
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = text;
  toastBox().appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, 4500);
}

// ---------- theme ----------
// The button opens a menu of radio items (THEMES, grouped); the choice is
// remembered in localStorage by theme.js.
const themeMenu = $('#theme-menu');
// In <body>, not the top bar: its backdrop-filter would make a fixed menu
// position against the bar instead of the viewport.
document.body.appendChild(themeMenu);
const themeItems = [];
{
  let group = null;
  for (const t of THEMES) {
    if (t.group !== group) {
      group = t.group;
      const h = document.createElement('div');
      h.className = 'menu-group';
      h.setAttribute('role', 'presentation');
      h.textContent = group;
      themeMenu.appendChild(h);
    }
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitemradio');
    b.dataset.themeId = t.id;
    b.tabIndex = -1;
    const sw = document.createElement('span');
    sw.className = 'theme-swatch';
    sw.dataset.swatch = t.id;
    sw.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.textContent = t.label;
    b.append(sw, label);
    themeMenu.appendChild(b);
    themeItems.push(b);
  }
}
function reflectTheme(id, mode) {
  const t = THEMES.find((x) => x.id === id);
  themeBtn.setAttribute('aria-label', `Theme: ${t ? t.label : id}`);
  themeBtn.title = `Theme: ${t ? t.label : id}`;
  themeBtn.dataset.theme = mode;
  for (const b of themeItems) b.setAttribute('aria-checked', String(b.dataset.themeId === id));
}
onSkinChange(reflectTheme);
initTheme();
function closeThemeMenu(refocus = false) {
  if (themeMenu.hidden) return;
  themeMenu.hidden = true;
  themeBtn.setAttribute('aria-expanded', 'false');
  if (refocus) themeBtn.focus();
}
function openThemeMenu() {
  themeMenu.hidden = false;
  themeBtn.setAttribute('aria-expanded', 'true');
  const r = themeBtn.getBoundingClientRect();
  themeMenu.style.top = `${Math.max(8, Math.min(r.bottom + 4, window.innerHeight - themeMenu.offsetHeight - 8))}px`;
  themeMenu.style.left = `${Math.max(8, Math.min(r.right - themeMenu.offsetWidth, window.innerWidth - themeMenu.offsetWidth - 8))}px`;
  (themeItems.find((b) => b.getAttribute('aria-checked') === 'true') || themeItems[0]).focus();
}
themeBtn.addEventListener('click', () => { if (themeMenu.hidden) openThemeMenu(); else closeThemeMenu(); });
themeMenu.addEventListener('click', (e) => {
  const b = e.target.closest('[data-theme-id]');
  if (!b) return;
  setTheme(b.dataset.themeId);
  closeThemeMenu(true);
});
themeMenu.addEventListener('keydown', (e) => {
  const i = themeItems.indexOf(document.activeElement);
  const n = themeItems.length;
  if (e.key === 'Escape') { e.preventDefault(); closeThemeMenu(true); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); themeItems[(i + 1) % n].focus(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); themeItems[(i - 1 + n) % n].focus(); }
  else if (e.key === 'Home') { e.preventDefault(); themeItems[0].focus(); }
  else if (e.key === 'End') { e.preventDefault(); themeItems[n - 1].focus(); }
  else if (e.key === 'Tab') closeThemeMenu();
});
document.addEventListener('pointerdown', (e) => {
  if (!themeMenu.hidden && !themeMenu.contains(e.target) && !themeBtn.contains(e.target)) closeThemeMenu();
});
window.addEventListener('resize', () => closeThemeMenu());

// ---------- session expiry ----------
api.setUnauthorizedHandler(() => {
  store.set({ banner: 'expired' });
  bannerEl.hidden = false;
  composer.setDisabled(true);
});
$('#reload').addEventListener('click', () => location.reload());

// ---------- sidebar drawer (narrow screens) ----------
function setSidebar(open) {
  store.set({ sidebarOpen: open });
  sidebarEl.classList.toggle('open', open);
  scrim.hidden = !open;
  hamburger.setAttribute('aria-expanded', String(open));
  if (open) sidebar.focusSearch();
}
hamburger.addEventListener('click', () => setSidebar(!store.get().sidebarOpen));
scrim.addEventListener('click', () => setSidebar(false));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && store.get().sidebarOpen) setSidebar(false);
});

// ---------- sidebar ----------
const sidebar = createSidebar(sidebarEl, {
  onNew: () => { setSidebar(false); composer.clearFiles(); goTo(null); },
  onAgents: () => setSidebar(false),
  onSelect: () => setSidebar(false),
  onSearch: (q) => store.set({ query: q }),
  onClose: () => setSidebar(false),
  onRename: async (id, title) => {
    try {
      const conv = await api.patchConversation(id, { title });
      store.set((s) => ({
        conversations: s.conversations.map((c) => (c.id === id ? { ...c, title: conv.title ?? title } : c)),
        conversation: s.conversation?.id === id ? { ...s.conversation, title: conv.title ?? title } : s.conversation,
      }));
    } catch (e) { notify(e.detail || 'Rename failed.', 'error'); store.set((s) => ({ ...s })); }
  },
  onDelete: async (id) => {
    try {
      await api.deleteConversation(id);
      store.set((s) => ({ conversations: s.conversations.filter((c) => c.id !== id) }));
      if (store.get().currentId === id) goTo(null);
      notify('Conversation deleted.');
    } catch (e) { notify(e.detail || 'Delete failed.', 'error'); }
  },
});

// ---------- the viewer (addendum f): files and, in advanced access, code beside the log ----------
const viewer = createViewer({
  onToggle: (open) => mainEl.classList.toggle('with-viewer', open),
  onClose: (byUser) => {
    // focus was inside the panel that is now hidden: hand it to the composer
    if (byUser && (!document.activeElement || document.activeElement === document.body || viewer.el.contains(document.activeElement))) composer.focus();
  },
});
mainEl.insertBefore(viewer.el, pageEl);

// ---------- messages + composer ----------
const list = createMessageList(messagesEl, {
  onOpenFile: (f) => viewer.open({ key: `file:${f.id}`, type: 'file', id: String(f.id), name: f.name || 'file', media_type: f.media_type || '', size: f.size, ...(f.title ? { title: f.title } : {}) }, { focus: true }),
  onOpenCode: (code, lang) => viewer.open({ code, lang }, { focus: true }),
});
const composer = createComposer($('#composer'), {
  getLimits: () => store.get().me?.limits || {},
  notify,
  dropZone: mainEl,
  onSend: (text, files) => sendTurn(text, files),
  onStop: async () => {
    const run = store.get().run;
    if (!run) return;
    try { await api.cancelRun(run.conversationId); }
    catch (e) { notify(e.detail || 'Stop failed.', 'error'); }
  },
});

// ---------- the context drawer (addendum e) ----------
let contextSeq = 0;
/** One GET .../context for the open conversation; a stale answer (another conversation meanwhile) is dropped. */
async function loadContext(id) {
  const seq = ++contextSeq;
  try {
    const ctx = await api.getContext(id);
    if (seq !== contextSeq || store.get().conversation?.id !== id) return;
    store.set({ context: ctx && typeof ctx === 'object' ? ctx : null, contextError: !(ctx && typeof ctx === 'object') });
  } catch (e) {
    if (seq !== contextSeq || store.get().conversation?.id !== id) return;
    store.set({ context: null, contextError: true });
  }
}
function refreshContext() {
  const id = store.get().conversation?.id;
  if (id) loadContext(id);
}
const contextDrawer = createContextDrawer({
  onRefresh: refreshContext,
  onToggle: async (fileId, excluded) => {
    const conv = store.get().conversation;
    if (!conv) throw new Error('no conversation');
    const name = (store.get().context?.history?.attachments || []).find((a) => String(a.file_id) === String(fileId))?.name || 'The file';
    try {
      const r = await api.setAttachmentExcluded(conv.id, fileId, excluded);
      const stored = r?.excluded === true;
      store.set((s) => (s.context?.history ? {
        context: {
          ...s.context,
          history: { ...s.context.history, attachments: (s.context.history.attachments || []).map((a) => (String(a.file_id) === String(fileId) ? { ...a, excluded: stored } : a)) },
        },
      } : null));
      notify(stored ? `${name} is no longer in the context.` : `${name} is back in the context.`);
      loadContext(conv.id); // the estimate moved
      return stored;
    } catch (e) {
      if (e.status === 409) notify('Wait until the turn has finished, then try again.', 'error');
      else if (e.status === 404) notify('Only an uploaded file can be switched off.', 'error');
      else if (e.status !== 401) notify(e.detail || 'The change could not be saved.', 'error');
      throw e;
    }
  },
});
contextBtn.addEventListener('click', () => {
  const s = store.get();
  if (!s.conversation) return;
  contextDrawer.open(s.context, s.contextError);
});
let contextSig = null;
// The header button ("Context 12%"), the composer indicator and the open drawer follow the store.
function reflectContext(s) {
  const show = !!s.conversation && s.view !== 'agents';
  contextBtn.hidden = !show;
  const h = s.context?.history;
  const fill = h ? fillLevel(h.estimated_tokens, h.measured_tokens, s.context.model?.context_window) : null;
  const pct = fill && fill.pct != null ? formatPct(fill.pct) : '';
  const text = contextIndicatorText(s.context);
  const level = fill?.level || 'ok';
  const sig = `${show}|${pct}|${text}|${level}`;
  if (sig !== contextSig) {
    contextSig = sig;
    contextPct.textContent = pct;
    contextPct.hidden = !pct;
    contextBtn.classList.toggle('warn', level === 'warn');
    contextBtn.classList.toggle('danger', level === 'danger');
    contextBtn.setAttribute('aria-label', pct ? `Context, ${pct} of the model's window used` : 'Context');
    const window = Number(s.context?.model?.context_window) || 0;
    const title = window ? `${pct} of the model's context window (${formatTokens(window)} tokens)` : (text ? 'The size of the model\'s context; this model\'s window is unknown' : '');
    composer.setContext({ text, level, title });
  }
  contextDrawer.update(s.context, s.contextError);
}

// ---------- model picker ----------
function fillModels(models) {
  modelSel.replaceChildren();
  for (const m of models || []) {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = m.label || m.id;
    if (m.default) o.dataset.default = '1';
    modelSel.appendChild(o);
  }
  modelSel.hidden = !(models && models.length);
}
function selectedModel() {
  const s = store.get();
  const models = s.me?.models || [];
  const wanted = s.conversation?.model || s.draftModel || models.find((m) => m.default)?.id || models[0]?.id;
  return wanted || null;
}
function reflectModel() {
  const id = selectedModel();
  if (id && modelSel.value !== id) modelSel.value = id;
  // an admin agent runs on its own model: the pick does not apply to it
  const s = store.get();
  const ref = s.conversation ? (s.conversation.agent_ref || null) : (s.draftAgentRef || null);
  const adminAgent = typeof ref === 'string' && ref.startsWith('admin:');
  modelSel.disabled = adminAgent;
  modelSel.title = adminAgent ? 'Admin agents run on their own model' : '';
}
modelSel.addEventListener('change', async () => {
  const model = modelSel.value;
  const conv = store.get().conversation;
  store.set({ draftModel: model });
  if (conv) {
    try {
      await api.patchConversation(conv.id, { model });
      store.set((s) => ({ conversation: s.conversation?.id === conv.id ? { ...s.conversation, model } : s.conversation }));
      refreshContext(); // the window may differ
    } catch (e) { notify(e.detail || 'The model could not be changed.', 'error'); }
  }
});

// ---------- agent picker ----------
let agentsSig = '';
function fillAgents() {
  const s = store.get();
  const conv = s.conversation;
  const current = conv ? (conv.agent_ref || null) : (s.draftAgentRef || null);
  const access = effectiveAccess();
  const groups = pickerGroups(s.catalog, s.myAgents, current, conv?.agent_name || '', { access });
  const sig = JSON.stringify([groups, current, access]);
  if (sig === agentsSig) return;
  agentsSig = sig;
  agentSel.replaceChildren();
  for (const g of groups) {
    const og = document.createElement('optgroup');
    og.label = g.label;
    for (const o of g.options) {
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      if (o.disabled) opt.disabled = true;
      og.appendChild(opt);
    }
    agentSel.appendChild(og);
  }
  agentSel.value = current || '';
  agentSel.hidden = !s.catalog && !s.myAgents.length;
}
function currentAgentName() {
  const s = store.get();
  const conv = s.conversation;
  if (conv) return conv.agent_name || agentNameOf(conv.agent_ref || null, s.catalog, s.myAgents, '');
  return agentNameOf(s.draftAgentRef || null, s.catalog, s.myAgents, '');
}
agentSel.addEventListener('change', async () => {
  const ref = agentSel.value || null;
  const s = store.get();
  const name = agentNameOf(ref, s.catalog, s.myAgents, '');
  const conv = s.conversation;
  store.set({ draftAgentRef: ref });
  if (!conv) { renderDraftEmpty(); reflectModel(); return; }
  try {
    const fresh = await api.patchConversation(conv.id, { agent_ref: ref });
    const agent_ref = fresh && 'agent_ref' in fresh ? fresh.agent_ref : ref;
    const agent_name = fresh?.agent_name || name;
    store.set((st) => ({
      conversation: st.conversation?.id === conv.id ? { ...st.conversation, agent_ref, agent_name } : st.conversation,
      conversations: st.conversations.map((c) => (c.id === conv.id ? { ...c, agent_ref, agent_name } : c)),
    }));
    renderMessages();
    reflectModel();
    refreshContext();
  } catch (e) {
    if (e.status === 422) notify('That agent cannot be used in this conversation.', 'error');
    else if (e.status !== 401) notify(e.detail || 'The agent could not be changed.', 'error');
    fillAgents();
  }
});
async function loadAgentLists(access = 'basic') {
  const want = isDeveloper() ? normalizeAccess(access) : 'basic';
  try {
    // a non-developer asks without the parameter (the server answers basic either way)
    const [catalog, mine] = await Promise.all([api.getCatalog(isDeveloper() ? want : undefined), api.listMyAgents()]);
    store.set({ catalog: catalog && typeof catalog === 'object' ? catalog : null, myAgents: Array.isArray(mine) ? mine : [], catalogAccess: want });
  } catch { /* the picker keeps what it has */ }
}
/** The catalogue of `access` is loaded (a developer's picker follows the switch). */
async function ensureCatalog(access) {
  const want = isDeveloper() ? normalizeAccess(access) : 'basic';
  const s = store.get();
  if (s.catalog && s.catalogAccess === want) return;
  await loadAgentLists(want);
}

// ---------- access switch: Basic | Advanced (developers only) ----------
const ACCESS_DENIED_TEXT = 'Advanced access needs the developer role.';
function isDeveloper() { return store.get().me?.developer === true; }
/** The access of the open conversation, else the switch's value for the next one. */
function effectiveAccess() {
  const s = store.get();
  return normalizeAccess(s.conversation ? s.conversation.access : s.draftAccess);
}
function reflectAccess() {
  const s = store.get();
  accessEl.hidden = !isDeveloper() || s.view === 'agents';
  const cur = effectiveAccess();
  for (const b of accessBtns) {
    const on = b.dataset.access === cur;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  }
}
let accessBusy = false;
async function setAccess(next) {
  next = normalizeAccess(next);
  if (accessBusy || next === effectiveAccess()) { reflectAccess(); return; }
  if (next === 'advanced' && !isDeveloper()) { notify(ACCESS_DENIED_TEXT, 'error'); reflectAccess(); return; }
  const s = store.get();
  const conv = s.conversation;
  const currentRef = conv ? (conv.agent_ref || null) : (s.draftAgentRef || null);
  // an agent the new tier does not offer: back to the orchestrator, in the same PATCH
  const keepRef = refOffered(currentRef, s.catalog, s.myAgents, next);
  accessBusy = true;
  try {
    if (conv) {
      let fresh;
      try {
        fresh = await api.patchConversation(conv.id, keepRef ? { access: next } : { access: next, agent_ref: null });
      } catch (e) {
        if (e.status === 403) notify(ACCESS_DENIED_TEXT, 'error');
        else if (e.status !== 401) notify(e.detail || 'The access could not be changed.', 'error');
        return; // the switch reverts in `finally`
      }
      const access = normalizeAccess(fresh?.access, next);
      const agent_ref = keepRef ? (conv.agent_ref || null) : (fresh && 'agent_ref' in fresh ? fresh.agent_ref : null);
      const agent_name = keepRef ? conv.agent_name : (fresh?.agent_name || agentNameOf(agent_ref, s.catalog, s.myAgents, ''));
      store.set((st) => ({
        draftAccess: access,
        draftAgentRef: agent_ref || null,
        conversation: st.conversation?.id === conv.id ? { ...st.conversation, access, agent_ref, agent_name } : st.conversation,
        conversations: st.conversations.map((c) => (c.id === conv.id ? { ...c, access, agent_ref, agent_name } : c)),
      }));
    } else {
      store.set({ draftAccess: next, draftAgentRef: keepRef ? (s.draftAgentRef || null) : null });
    }
    rememberAccess(s.me?.principal, next);
    if (!keepRef) notify('That agent is not offered at this access level; the conversation continues with the orchestrator.');
    await ensureCatalog(next);
    agentsSig = '';
    fillAgents();
    reflectModel();
    emptySig = null;
    if (store.get().conversation) { renderMessages(); refreshContext(); } else renderDraftEmpty();
  } finally {
    accessBusy = false;
    reflectAccess();
  }
}
accessEl.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('[role="radio"]');
  if (b && accessEl.contains(b)) setAccess(b.dataset.access);
});
accessEl.addEventListener('keydown', (e) => {
  const keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
  if (!keys.includes(e.key)) return;
  e.preventDefault();
  const i = Math.max(0, accessBtns.findIndex((b) => b.getAttribute('aria-checked') === 'true'));
  const n = accessBtns.length;
  const j = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : (i + (e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1) + n) % n;
  accessBtns[j].focus();
  setAccess(accessBtns[j].dataset.access);
});

// ---------- Agents & skills page ----------
const editor = createAgentEditor({
  notify,
  onSaved: () => agentsPage.load(),
});
const agentsPage = createAgentsPage(pageEl, {
  editor,
  notify,
  getModels: () => store.get().me?.models || [],
  isDeveloper,
  onChanged: (d) => store.set({ catalog: d.catalog, myAgents: d.agents, catalogAccess: d.catalogAccess || 'basic' }),
  onChat: async (ref, name, agent) => {
    // an advanced agent needs an advanced conversation; otherwise the switch's value
    const access = isDeveloper() && (store.get().draftAccess === 'advanced' || isAdvanced(agent)) ? 'advanced' : 'basic';
    try {
      const created = await api.createConversation({ title: 'New chat', agent_ref: ref, model: selectedModel() || undefined, ...(isDeveloper() ? { access } : {}) });
      store.set((st) => ({ draftAgentRef: ref, conversations: [{ ...created, access: normalizeAccess(created.access, access), agent_ref: created.agent_ref ?? ref, agent_name: created.agent_name || name }, ...st.conversations] }));
      goTo(created.id);
    } catch (e) {
      if (e.status !== 401) notify('The conversation could not be created.', 'error');
    }
  },
});
function showView(view) {
  const agents = view === 'agents';
  store.set({ view });
  if (agents) viewer.close(false);
  logEl.hidden = agents;
  composerWrap.hidden = agents;
  pageEl.hidden = !agents;
  agentSel.parentElement.hidden = agents;
  modelSel.parentElement.hidden = agents;
  usageChip.hidden = agents || usageChip.hidden;
  contextBtn.hidden = agents || contextBtn.hidden;
  reflectAccess();
  if (agents) agentsPage.show(); else agentsPage.hide();
}

// ---------- router ----------
function parseHash() {
  const m = /^#\/c\/([^/?#]+)/.exec(location.hash || '');
  return m ? decodeURIComponent(m[1]) : null;
}
function isAgentsHash() { return /^#\/agents(?:[/?#]|$)/.test(location.hash || ''); }
function goTo(id) {
  const target = id ? `#/c/${encodeURIComponent(id)}` : '#/';
  if (location.hash === target) route();
  else location.hash = target;
}
let routeSeq = 0;
let emptySig = null;
// The empty state for a draft (the picked ref) or for a conversation without
// messages yet (one made by "Chat with this agent"); redrawn when the agent changes.
function renderEmptyFor(ref, storedName = '') {
  const s = store.get();
  const name = ref ? agentNameOf(ref, s.catalog, s.myAgents, storedName) : '';
  const desc = ref ? (s.myAgents.find((a) => (a.ref || `mine:${a.id}`) === ref)?.description || s.catalog?.agents?.find((a) => (a.ref || `admin:${a.name}`) === ref)?.description || '') : '';
  const access = effectiveAccess();
  const sig = `${ref || ''}|${name}|${access}`;
  if (messagesEl.classList.contains('empty-state') && emptySig === sig) return;
  emptySig = sig;
  renderEmpty(messagesEl, { appName: s.me?.app_name, onPrompt: (t) => composer.setText(t), agentName: name, agentDescription: desc, advanced: access === 'advanced' });
}
function renderDraftEmpty() { renderEmptyFor(store.get().draftAgentRef || null); }
async function route() {
  if (isAgentsHash()) {
    routeSeq++;
    store.set({ currentId: null, conversation: null, context: null, contextError: false });
    showView('agents');
    return;
  }
  if (store.get().view !== 'chat') showView('chat');
  const id = parseHash();
  const s = store.get();
  if (id && s.conversation?.id === id) { store.set({ currentId: id }); return; }
  const seq = ++routeSeq;
  if (!id) {
    store.set({ currentId: null, conversation: null, context: null, contextError: false });
    viewer.setConversation(null);
    list.clear();
    renderDraftEmpty();
    composer.focus();
    await ensureCatalog(store.get().draftAccess);
    return;
  }
  store.set({ currentId: id, loading: true, context: null, contextError: false });
  try {
    const conv = await api.getConversation(id);
    if (seq !== routeSeq) return;
    list.clear();
    list.resetStick();
    const normalized = normalizeConversation(conv);
    store.set({ conversation: normalized, loading: false, draftAgentRef: conv.agent_ref || null });
    renderMessages();
    list.scrollToBottom(true);
    loadContext(id); // one call, not awaited: the log is already on screen
    await ensureCatalog(normalized.access);
  } catch (e) {
    if (seq !== routeSeq) return;
    store.set({ loading: false });
    if (e.status === 404) { notify('That conversation does not exist.'); goTo(null); }
    else if (e.status !== 401) notify(e.detail || 'The conversation could not be loaded.', 'error');
  }
}
function normalizeConversation(conv) {
  return {
    ...conv,
    access: normalizeAccess(conv.access),
    usage: conv.usage && typeof conv.usage === 'object' ? conv.usage : null,
    messages: (conv.messages || []).map((m) => ({
      ...m,
      parts: Array.isArray(m.parts) ? m.parts : [],
      files: Array.isArray(m.files) ? m.files : [],
      note: null,
      error: m.error ? { code: m.status === 'canceled' ? 'canceled' : 'run_failed', message: m.error } : null,
    })),
  };
}
window.addEventListener('hashchange', route);

// ---------- sending a turn ----------
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderMessages(); });
}

async function sendTurn(text, files) {
  const s = store.get();
  if (s.run) return;
  let conv = s.conversation;
  const model = selectedModel();
  try {
    if (!conv) {
      const agent_ref = s.draftAgentRef || null;
      const access = normalizeAccess(s.draftAccess);
      const created = await api.createConversation({ title: titleFromText(text), model: model || undefined, agent_ref, ...(isDeveloper() ? { access } : {}) });
      if (!('access' in created)) created.access = isDeveloper() ? access : 'basic';
      if (agent_ref && created.agent_ref !== agent_ref) {
        // the create did not take the ref: bind it before the first turn
        const patched = await api.patchConversation(created.id, { agent_ref });
        created.agent_ref = patched?.agent_ref ?? agent_ref;
        created.agent_name = patched?.agent_name || created.agent_name;
      }
      if (!created.agent_name) created.agent_name = agentNameOf(created.agent_ref || null, s.catalog, s.myAgents, '');
      conv = normalizeConversation({ ...created, messages: [] });
      store.set({ conversation: conv, currentId: conv.id, context: null, contextError: false, conversations: [created, ...store.get().conversations] });
      routeSeq++; // the hashchange below must not reload what we hold
      history.replaceState(null, '', `#/c/${encodeURIComponent(conv.id)}`);
      list.clear();
    }
  } catch (e) {
    notify(e.detail || 'The conversation could not be created.', 'error');
    composer.setText(text);
    return;
  }
  viewer.noteSend(); // a closed panel may open itself again for this turn's output
  const user = newUserMessage(text, files.map((f) => ({ name: f.name, media_type: f.type, size: f.size })));
  const assistant = newAssistantMessage();
  const controller = new AbortController();
  store.set({
    conversation: { ...conv, status: 'running', messages: [...conv.messages, user, assistant] },
    run: { conversationId: conv.id, assistantId: assistant.id, startedAt: Date.now(), controller },
  });
  composer.setRunning(true);
  list.resetStick();
  renderMessages();
  let sawDone = false;
  try {
    await api.sendMessage(conv.id, { text, files, model }, {
      signal: controller.signal,
      onEvent: (ev) => {
        if (ev.type === 'done') sawDone = true;
        store.set((st) => {
          if (st.conversation?.id !== conv.id) return null;
          const next = applyStreamEvent(st.conversation, user.id, assistant.id, ev);
          const patch = { conversation: next };
          if (ev.type === 'title') {
            patch.conversations = st.conversations.map((c) => (c.id === conv.id ? { ...c, title: next.title } : c));
          }
          return patch;
        });
        if (ev.type === 'done') {
          // decision 3: the turn's last generated file, else (advanced) its last long code block
          const st = store.get();
          if (st.conversation?.id === conv.id) {
            viewer.setConversation(st.conversation);
            viewer.autoOpen(st.conversation.messages.find((m) => m.id === assistant.id));
          }
        }
        scheduleRender();
      },
    });
  } catch (e) {
    if (e.status === 409) {
      notify('A run is already in progress in this conversation.', 'error');
      store.set((st) => ({ conversation: { ...st.conversation, messages: st.conversation.messages.filter((m) => m.id !== user.id && m.id !== assistant.id) } }));
      composer.setText(text);
    } else if (e.status === 401) {
      /* banner shown by the handler */
    } else if (e.name !== 'AbortError') {
      notify(e.detail || 'The message could not be sent.', 'error');
      store.set((st) => ({
        conversation: applyStreamEvent(st.conversation, user.id, assistant.id,
          { type: 'error', data: { code: 'run_failed', message: e.detail || 'The connection was lost.' } }),
      }));
    }
  } finally {
    store.set({ run: null });
    composer.setRunning(false);
    renderMessages();
    if (!sawDone && store.get().conversation?.id === conv.id) {
      // The connection ended before `done`: the server finishes the turn on
      // its own, so re-read what it stored.
      await reloadCurrent(conv.id);
    }
    if (store.get().conversation?.id === conv.id) loadContext(conv.id); // after each turn, one call
    refreshList();
  }
}

async function reloadCurrent(id) {
  try {
    const fresh = await api.getConversation(id);
    if (store.get().currentId !== id) return;
    const running = fresh.status === 'running';
    store.set({ conversation: normalizeConversation(fresh) });
    if (running) notify('The agents are still working; reload in a moment to see the answer.');
    renderMessages();
  } catch { /* keep what we have */ }
}

async function refreshList() {
  try {
    const items = await api.listConversations();
    store.set({ conversations: Array.isArray(items) ? items : [] });
  } catch { /* the sidebar keeps its last list */ }
}

// ---------- rendering ----------
function renderMessages() {
  const s = store.get();
  viewer.setConversation(s.view === 'agents' ? null : s.conversation);
  if (!s.conversation) return;
  if (!s.run && !(s.conversation.messages || []).length) {
    renderEmptyFor(s.conversation.agent_ref || null, s.conversation.agent_name || '');
    return;
  }
  list.render(s.conversation, { live: !!s.run, agentName: currentAgentName() || 'Agents', excludedIds: excludedFileIds(s.context) });
}

store.subscribe((s) => {
  sidebar.render({
    conversations: s.conversations, query: s.query, currentId: s.currentId,
    principal: s.me?.principal || '', view: s.view,
  });
  const title = s.view === 'agents' ? 'Agents & skills' : (s.conversation?.title || s.me?.app_name || 'SAP BTP Multi-Agent');
  if (titleEl.textContent !== title) {
    titleEl.textContent = title;
    document.title = s.conversation?.title ? `${s.conversation.title} · ${s.me?.app_name || 'SAP BTP Multi-Agent'}` : (s.me?.app_name || 'SAP BTP Multi-Agent');
  }
  reflectModel();
  reflectAccess();
  fillAgents();
  reflectUsage(s.conversation?.usage);
  if (s.view === 'agents') usageChip.hidden = true;
  reflectContext(s);
  if (s.conversation && !s.run) renderMessages();
});

// The conversation total: "12.4k tokens", from the detail's `usage` and the
// turn's usage part (applyStreamEvent adds it); hidden at zero.
function reflectUsage(usage) {
  const total = Number(usage?.total_tokens) || 0;
  if (!total) {
    usageChip.hidden = true;
    usageChip.textContent = '';
    usageChip.title = '';
    return;
  }
  const text = `${formatTokens(total)} tokens`;
  const turns = Number(usage.turns) || 0;
  const title = `${formatTokens(usage.input_tokens || 0)} in / ${formatTokens(usage.output_tokens || 0)} out · ${Number(usage.requests) || 0} requests over ${turns === 1 ? '1 turn' : `${turns} turns`}`;
  if (usageChip.textContent !== text) usageChip.textContent = text;
  if (usageChip.title !== title) { usageChip.title = title; usageChip.setAttribute('aria-label', `Conversation usage: ${text}, ${title}`); }
  usageChip.hidden = false;
}

// ---------- sign-in links (same-origin /oauth/login only), as the classic chat ----------
function signinUrl(a) {
  let u;
  try { u = new URL(a.href, location.href); } catch { return null; }
  if (u.origin !== location.origin || u.pathname !== '/oauth/login') return null;
  return u.href;
}
const openSignin = (href) => window.open(href, 'mcp_auth', 'width=520,height=720');
document.addEventListener('click', (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest && e.target.closest('a[href]');
  if (!a) return;
  const href = signinUrl(a);
  if (!href) return;
  e.preventDefault();
  if (!openSignin(href)) window.location.href = href;
}, true);
const autoTried = new Set();
new MutationObserver(() => {
  for (const a of messagesEl.querySelectorAll('a[href*="/oauth/login"]')) {
    const href = signinUrl(a);
    if (!href || autoTried.has(href)) continue;
    autoTried.add(href);
    openSignin(href);
  }
}).observe(messagesEl, { childList: true, subtree: true });

// ---------- boot ----------
async function boot() {
  try {
    const me = await api.getMe();
    // the switch's value: the last choice of this principal, else the server's default; basic for everyone else
    const draftAccess = me?.developer === true ? rememberedAccess(me.principal, normalizeAccess(me.access_default)) : 'basic';
    store.set({ me, draftAccess });
    fillModels(me.models);
    reflectModel();
    reflectAccess();
  } catch (e) {
    if (e.status !== 401) notify('The chat service did not answer. Reload to try again.', 'error');
    fillModels([]);
  }
  await Promise.all([refreshList(), loadAgentLists(store.get().draftAccess)]);
  await route();
}
boot();
