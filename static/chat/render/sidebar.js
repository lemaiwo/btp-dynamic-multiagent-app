// The sidebar: search, New chat, the grouped conversation list with a kebab
// menu (Rename inline, Delete behind a confirm dialog), and the signed-in
// principal at the bottom.

import { groupConversations, filterConversations } from '../util.js';

function svg(d, size = 18) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('width', size);
  s.setAttribute('height', size);
  s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', d);
  s.appendChild(p);
  return s;
}

export function createSidebar(root, handlers) {
  root.replaceChildren();
  const top = document.createElement('div');
  top.className = 'sb-top';
  const brand = document.createElement('div');
  brand.className = 'sb-brand';
  const logo = document.createElement('img');
  logo.src = '/static/sap.png';
  logo.alt = 'SAP';
  const brandText = document.createElement('span');
  brandText.textContent = 'BTP Multi-Agent';
  brand.append(logo, brandText);
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'icon-btn sb-close';
  closeBtn.setAttribute('aria-label', 'Close conversations');
  closeBtn.appendChild(svg('M6 6l12 12M18 6L6 18'));
  closeBtn.addEventListener('click', () => handlers.onClose?.());
  top.append(brand, closeBtn);

  const newBtn = document.createElement('button');
  newBtn.type = 'button';
  newBtn.className = 'sb-new';
  newBtn.appendChild(svg('M12 5v14M5 12h14'));
  newBtn.appendChild(document.createTextNode('New chat'));
  newBtn.addEventListener('click', () => handlers.onNew());

  const search = document.createElement('input');
  search.type = 'search';
  search.className = 'sb-search';
  search.placeholder = 'Search conversations';
  search.setAttribute('aria-label', 'Search conversations by title');
  search.addEventListener('input', () => handlers.onSearch(search.value));

  const list = document.createElement('nav');
  list.className = 'sb-list';
  list.setAttribute('aria-label', 'Conversations');

  const foot = document.createElement('div');
  foot.className = 'sb-foot';
  const agentsLink = document.createElement('a');
  agentsLink.className = 'sb-link';
  agentsLink.href = '#/agents';
  agentsLink.appendChild(svg('M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM12 12l8-4.5M12 12v9M12 12L4 7.5', 16));
  agentsLink.appendChild(document.createTextNode('Agents & skills'));
  agentsLink.addEventListener('click', () => handlers.onAgents?.());
  const who = document.createElement('span');
  who.className = 'sb-who';
  foot.append(agentsLink, who);

  root.append(top, newBtn, search, list, foot);

  // ---- kebab menu (one for the whole list) ----
  const menu = document.createElement('div');
  menu.className = 'menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  const miRename = document.createElement('button');
  miRename.type = 'button';
  miRename.setAttribute('role', 'menuitem');
  miRename.textContent = 'Rename';
  const miDelete = document.createElement('button');
  miDelete.type = 'button';
  miDelete.setAttribute('role', 'menuitem');
  miDelete.className = 'danger';
  miDelete.textContent = 'Delete';
  menu.append(miRename, miDelete);
  document.body.appendChild(menu);
  let menuFor = null; // {id, title, button}

  function closeMenu(refocus = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    if (menuFor?.button) {
      menuFor.button.setAttribute('aria-expanded', 'false');
      if (refocus) menuFor.button.focus();
    }
    menuFor = null;
  }
  function openMenu(button, conv) {
    menuFor = { id: conv.id, title: conv.title, button };
    const r = button.getBoundingClientRect();
    menu.hidden = false;
    menu.style.top = `${Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
    menu.style.left = `${Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)}px`;
    button.setAttribute('aria-expanded', 'true');
    miRename.focus();
  }
  menu.addEventListener('keydown', (e) => {
    const items = [miRename, miDelete];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === 'Tab') closeMenu();
  });
  document.addEventListener('pointerdown', (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== menuFor?.button) closeMenu();
  });
  window.addEventListener('resize', () => closeMenu());
  miRename.addEventListener('click', () => {
    const target = menuFor;
    closeMenu();
    if (target) startRename(target.id);
  });
  miDelete.addEventListener('click', () => {
    const target = menuFor;
    closeMenu();
    if (target) confirmDelete(target);
  });

  // ---- delete confirm ----
  const dialog = document.createElement('dialog');
  dialog.className = 'confirm';
  const dTitle = document.createElement('h3');
  dTitle.textContent = 'Delete this conversation?';
  const dText = document.createElement('p');
  const dRow = document.createElement('div');
  dRow.className = 'confirm-actions';
  const dCancel = document.createElement('button');
  dCancel.type = 'button';
  dCancel.className = 'btn';
  dCancel.textContent = 'Cancel';
  const dOk = document.createElement('button');
  dOk.type = 'button';
  dOk.className = 'btn danger';
  dOk.textContent = 'Delete';
  dRow.append(dCancel, dOk);
  dialog.append(dTitle, dText, dRow);
  document.body.appendChild(dialog);
  let deleting = null;
  dCancel.addEventListener('click', () => dialog.close());
  dOk.addEventListener('click', () => {
    const id = deleting?.id;
    dialog.close();
    if (id) handlers.onDelete(id);
  });
  function confirmDelete(target) {
    deleting = target;
    dText.textContent = `“${target.title}” and its files are removed. This cannot be undone.`;
    dialog.showModal();
    dCancel.focus();
  }

  // ---- rendering ----
  let renaming = null; // conversation id being renamed
  let lastSig = '';
  let current = { conversations: [], query: '', currentId: null, principal: '', view: 'chat' };

  function startRename(id) {
    renaming = id;
    lastSig = '';
    render(current);
    const input = list.querySelector('input.rename');
    if (input) { input.focus(); input.select(); }
  }

  function item(conv, active) {
    const li = document.createElement('li');
    li.className = `conv${active ? ' active' : ''}`;
    if (renaming === conv.id) {
      const input = document.createElement('input');
      input.className = 'rename';
      input.value = conv.title;
      input.maxLength = 120;
      input.setAttribute('aria-label', 'New title');
      let done = false;
      const finish = (save) => {
        if (done) return;
        done = true;
        renaming = null;
        const value = input.value.trim();
        if (save && value && value !== conv.title) handlers.onRename(conv.id, value);
        else { lastSig = ''; render(current); }
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      });
      input.addEventListener('blur', () => finish(true));
      li.appendChild(input);
      return li;
    }
    const a = document.createElement('a');
    a.className = 'conv-link';
    a.href = `#/c/${encodeURIComponent(conv.id)}`;
    const t = document.createElement('span');
    t.className = 'conv-title';
    t.textContent = conv.title || 'New chat';
    a.appendChild(t);
    if (conv.agent_ref) {
      // not the orchestrator: say who the conversation talks to
      const badge = document.createElement('span');
      badge.className = 'conv-badge';
      badge.textContent = conv.agent_name || 'agent';
      badge.title = `Talks to ${conv.agent_name || 'an agent'}`;
      a.appendChild(badge);
    }
    a.title = conv.title || '';
    if (active) a.setAttribute('aria-current', 'page');
    a.addEventListener('click', () => handlers.onSelect?.(conv.id));
    const kebab = document.createElement('button');
    kebab.type = 'button';
    kebab.className = 'icon-btn kebab';
    kebab.setAttribute('aria-label', `More actions for ${conv.title}`);
    kebab.setAttribute('aria-haspopup', 'menu');
    kebab.setAttribute('aria-expanded', 'false');
    kebab.appendChild(svg('M12 6.5a1 1 0 110-2 1 1 0 010 2zm0 6.5a1 1 0 110-2 1 1 0 010 2zm0 6.5a1 1 0 110-2 1 1 0 010 2z'));
    kebab.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!menu.hidden && menuFor?.id === conv.id) closeMenu();
      else { closeMenu(); openMenu(kebab, conv); }
    });
    if (conv.status === 'running') {
      const dot = document.createElement('span');
      dot.className = 'dot running small';
      dot.setAttribute('aria-label', 'running');
      li.appendChild(dot);
    }
    li.append(a, kebab);
    return li;
  }

  function render(state) {
    current = state;
    const sig = JSON.stringify([
      state.conversations.map((c) => [c.id, c.title, c.updated_at, c.status, c.agent_ref, c.agent_name]),
      state.query, state.currentId, state.principal, renaming, state.view,
    ]);
    if (sig === lastSig) return;
    lastSig = sig;
    who.textContent = state.principal || '';
    const onAgents = state.view === 'agents';
    agentsLink.classList.toggle('active', onAgents);
    if (onAgents) agentsLink.setAttribute('aria-current', 'page'); else agentsLink.removeAttribute('aria-current');
    if (search.value !== state.query) search.value = state.query;
    const groups = groupConversations(filterConversations(state.conversations, state.query));
    list.replaceChildren();
    if (!groups.length) {
      const p = document.createElement('p');
      p.className = 'sb-empty';
      p.textContent = state.query ? 'No conversation matches.' : 'No conversations yet.';
      list.appendChild(p);
      return;
    }
    for (const g of groups) {
      const h = document.createElement('h3');
      h.className = 'sb-group';
      h.textContent = g.label;
      const ul = document.createElement('ul');
      ul.className = 'sb-items';
      for (const c of g.items) ul.appendChild(item(c, c.id === state.currentId));
      list.append(h, ul);
    }
  }

  return { render, startRename, focusSearch: () => search.focus() };
}
