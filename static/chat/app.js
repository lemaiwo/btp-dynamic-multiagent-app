// Bootstrap: layout, router (#/c/<id>), store wiring, streaming turns.

import * as api from './api.js';
import { createStore, initialState, newUserMessage, newAssistantMessage, applyStreamEvent } from './state.js';
import { createSidebar } from './render/sidebar.js';
import { createMessageList, renderEmpty } from './render/messages.js';
import { createComposer } from './composer.js';
import { initTheme, toggleTheme, onThemeChange } from './theme.js';
import { titleFromText, formatTokens } from './util.js';

const store = createStore(initialState);
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

// ---------- router ----------
function parseHash() {
  const m = /^#\/c\/([^/?#]+)/.exec(location.hash || '');
  return m ? decodeURIComponent(m[1]) : null;
}
function goTo(id) {
  const target = id ? `#/c/${encodeURIComponent(id)}` : '#/';
  if (location.hash === target) route();
  else location.hash = target;
}
let routeSeq = 0;
async function route() {
  const id = parseHash();
  const s = store.get();
  if (id && s.conversation?.id === id) { store.set({ currentId: id }); return; }
  const seq = ++routeSeq;
  if (!id) {
    store.set({ currentId: null, conversation: null });
    list.clear();
    renderEmpty(messagesEl, { appName: s.me?.app_name, onPrompt: (t) => composer.setText(t) });
    composer.focus();
    return;
  }
  store.set({ currentId: id, loading: true });
  try {
    const conv = await api.getConversation(id);
    if (seq !== routeSeq) return;
    list.clear();
    list.resetStick();
    store.set({ conversation: normalizeConversation(conv), loading: false });
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
      const created = await api.createConversation({ title: titleFromText(text), model: model || undefined });
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
  list.render(s.conversation, { live: !!s.run });
}

store.subscribe((s) => {
  sidebar.render({
    conversations: s.conversations, query: s.query, currentId: s.currentId,
    principal: s.me?.principal || '',
  });
  const title = s.conversation?.title || s.me?.app_name || 'SAP BTP Multi-Agent';
  if (titleEl.textContent !== title) {
    titleEl.textContent = title;
    document.title = s.conversation?.title ? `${s.conversation.title} · ${s.me?.app_name || 'SAP BTP Multi-Agent'}` : (s.me?.app_name || 'SAP BTP Multi-Agent');
  }
  reflectModel();
  reflectUsage(s.conversation?.usage);
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
  await refreshList();
  await route();
}
boot();
