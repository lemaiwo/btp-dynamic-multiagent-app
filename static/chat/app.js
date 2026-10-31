// Bootstrap: layout, router (#/c/<id>, #/agents), store wiring, streaming turns.

import * as api from './api.js';
import { createStore, initialState, newUserMessage, newAssistantMessage, applyStreamEvent } from './state.js';
import { createSidebar } from './render/sidebar.js';
import { createMessageList, renderEmpty } from './render/messages.js';
import { createComposer } from './composer.js';
import { createAgentsPage } from './render/agents.js';
import { createAgentEditor } from './render/agentEditor.js';
import { pickerGroups, agentNameOf } from './myagents.js';
import { initTheme, toggleTheme, onThemeChange } from './theme.js';
import { titleFromText, formatTokens } from './util.js';

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
        <label class="model-pick agent-pick"><span class="sr-only">Agent</span><select id="agent" aria-label="Agent"></select></label>
        <label class="model-pick"><span class="sr-only">Model</span><select id="model" aria-label="Model"></select></label>
        <button type="button" class="icon-btn theme" id="theme" aria-label="Switch to dark theme" title="Theme">
          <svg class="sun" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
          <svg class="moon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"/></svg>
        </button>
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
const logEl = $('.log');
const composerWrap = $('.composer-wrap');
const pageEl = $('#page');
const usageChip = $('#usage-chip');
const themeBtn = $('#theme');
const bannerEl = $('#banner');
const messagesEl = $('#messages');
const toastsEl = $('#toasts');

// ---------- toasts ----------
function notify(text, kind = 'info') {
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = text;
  toastsEl.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, 4500);
}

// ---------- theme ----------
function reflectTheme(theme) {
  themeBtn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  themeBtn.dataset.theme = theme;
}
reflectTheme(initTheme());
onThemeChange(reflectTheme);
themeBtn.addEventListener('click', () => toggleTheme());

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

// ---------- messages + composer ----------
const list = createMessageList(messagesEl);
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
    } catch (e) { notify(e.detail || 'The model could not be changed.', 'error'); }
  }
});

// ---------- agent picker ----------
let agentsSig = '';
function fillAgents() {
  const s = store.get();
  const conv = s.conversation;
  const current = conv ? (conv.agent_ref || null) : (s.draftAgentRef || null);
  const groups = pickerGroups(s.catalog, s.myAgents, current, conv?.agent_name || '');
  const sig = JSON.stringify([groups, current]);
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
  } catch (e) {
    if (e.status === 422) notify('That agent cannot be used in this conversation.', 'error');
    else if (e.status !== 401) notify(e.detail || 'The agent could not be changed.', 'error');
    fillAgents();
  }
});
async function loadAgentLists() {
  try {
    const [catalog, mine] = await Promise.all([api.getCatalog(), api.listMyAgents()]);
    store.set({ catalog: catalog && typeof catalog === 'object' ? catalog : null, myAgents: Array.isArray(mine) ? mine : [] });
  } catch { /* the picker keeps what it has */ }
}

// ---------- Agents & skills page ----------
const editor = createAgentEditor({
  notify,
  onSaved: () => agentsPage.load(),
});
const agentsPage = createAgentsPage(pageEl, {
  editor,
  notify,
  getModels: () => store.get().me?.models || [],
  onChanged: (d) => store.set({ catalog: d.catalog, myAgents: d.agents }),
  onChat: async (ref, name) => {
    try {
      const created = await api.createConversation({ title: 'New chat', agent_ref: ref, model: selectedModel() || undefined });
      store.set((st) => ({ draftAgentRef: ref, conversations: [{ ...created, agent_ref: created.agent_ref ?? ref, agent_name: created.agent_name || name }, ...st.conversations] }));
      goTo(created.id);
    } catch (e) {
      if (e.status !== 401) notify('The conversation could not be created.', 'error');
    }
  },
});
function showView(view) {
  const agents = view === 'agents';
  store.set({ view });
  logEl.hidden = agents;
  composerWrap.hidden = agents;
  pageEl.hidden = !agents;
  agentSel.parentElement.hidden = agents;
  modelSel.parentElement.hidden = agents;
  usageChip.hidden = agents || usageChip.hidden;
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
  const sig = `${ref || ''}|${name}`;
  if (messagesEl.classList.contains('empty-state') && emptySig === sig) return;
  emptySig = sig;
  renderEmpty(messagesEl, { appName: s.me?.app_name, onPrompt: (t) => composer.setText(t), agentName: name, agentDescription: desc });
}
function renderDraftEmpty() { renderEmptyFor(store.get().draftAgentRef || null); }
async function route() {
  if (isAgentsHash()) {
    routeSeq++;
    store.set({ currentId: null, conversation: null });
    showView('agents');
    return;
  }
  if (store.get().view !== 'chat') showView('chat');
  const id = parseHash();
  const s = store.get();
  if (id && s.conversation?.id === id) { store.set({ currentId: id }); return; }
  const seq = ++routeSeq;
  if (!id) {
    store.set({ currentId: null, conversation: null });
    list.clear();
    renderDraftEmpty();
    composer.focus();
    return;
  }
  store.set({ currentId: id, loading: true });
  try {
    const conv = await api.getConversation(id);
    if (seq !== routeSeq) return;
    list.clear();
    list.resetStick();
    store.set({ conversation: normalizeConversation(conv), loading: false, draftAgentRef: conv.agent_ref || null });
    renderMessages();
    list.scrollToBottom(true);
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
      const created = await api.createConversation({ title: titleFromText(text), model: model || undefined, agent_ref });
      if (agent_ref && created.agent_ref !== agent_ref) {
        // the create did not take the ref: bind it before the first turn
        const patched = await api.patchConversation(created.id, { agent_ref });
        created.agent_ref = patched?.agent_ref ?? agent_ref;
        created.agent_name = patched?.agent_name || created.agent_name;
      }
      if (!created.agent_name) created.agent_name = agentNameOf(created.agent_ref || null, s.catalog, s.myAgents, '');
      conv = normalizeConversation({ ...created, messages: [] });
      store.set({ conversation: conv, currentId: conv.id, conversations: [created, ...store.get().conversations] });
      routeSeq++; // the hashchange below must not reload what we hold
      history.replaceState(null, '', `#/c/${encodeURIComponent(conv.id)}`);
      list.clear();
    }
  } catch (e) {
    notify(e.detail || 'The conversation could not be created.', 'error');
    composer.setText(text);
    return;
  }
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
  if (!s.conversation) return;
  if (!s.run && !(s.conversation.messages || []).length) {
    renderEmptyFor(s.conversation.agent_ref || null, s.conversation.agent_name || '');
    return;
  }
  list.render(s.conversation, { live: !!s.run, agentName: currentAgentName() || 'Agents' });
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
  fillAgents();
  reflectUsage(s.conversation?.usage);
  if (s.view === 'agents') usageChip.hidden = true;
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
    store.set({ me });
    fillModels(me.models);
    reflectModel();
  } catch (e) {
    if (e.status !== 401) notify('The chat service did not answer. Reload to try again.', 'error');
    fillModels([]);
  }
  await Promise.all([refreshList(), loadAgentLists()]);
  await route();
}
boot();
