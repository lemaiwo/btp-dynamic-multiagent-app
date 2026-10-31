// The Agents & skills page (#/agents): tabs Agents / Skills, each with
// "Mine" (edit, duplicate, delete, export selection) and "Admin · read-only"
// (lock badge, name + description; an agent row offers "Chat with this
// agent"). Toolbar: New, Import (file -> conflict rule dialog -> result
// summary), Export selected / Export all. For a developer (addendum d) the
// catalogue is loaded with access=advanced, so the advanced admin agents are
// listed too, and every advanced agent (admin or personal) carries the
// Developer badge. Everything of the server is inserted as text nodes.

import * as api from '../api.js';
import { exportQuery, formatImportResult, LIMITS, TEXTS, isAdvanced } from '../myagents.js';
import { lock, developerBadge } from './agentEditor.js';

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = text;
  return n;
}
function button(label, className, onClick) {
  const b = el('button', className, label);
  b.type = 'button';
  b.addEventListener('click', onClick);
  return b;
}

/**
 * createAgentsPage(root, {editor, notify, onChat(ref, name, agent), onChanged(data), getModels, isDeveloper})
 * -> {load(), show(), hide(), data()}
 * `isDeveloper()` decides the catalogue's access and what the editor offers.
 */
export function createAgentsPage(root, { editor, notify, onChat, onChanged, getModels, isDeveloper }) {
  const data = { catalog: null, agents: [], skills: [], loaded: false, error: null, catalogAccess: 'basic' };
  const developer = () => isDeveloper?.() === true;
  let tab = 'agents';
  const selected = { agents: new Set(), skills: new Set() };

  // ---- static frame ----
  const page = el('div', 'agents-page');
  const head = el('div', 'page-head');
  const back = el('a', 'back-link', '← Back to chat');
  back.href = '#/';
  const title = el('h2', 'page-title', 'Agents & skills');
  const intro = el('p', 'page-intro', 'Your own agents and skills, for you alone. Share them with another team as a bundle; the admin’s catalogue is listed read-only.');
  head.append(back, title, intro);

  const bar = el('div', 'page-bar');
  const tabs = el('div', 'tabs');
  tabs.setAttribute('role', 'tablist');
  const tabBtns = {};
  for (const [key, label] of [['agents', 'Agents'], ['skills', 'Skills']]) {
    const b = button(label, 'tab', () => setTab(key));
    b.setAttribute('role', 'tab');
    b.id = `tab-${key}`;
    b.setAttribute('aria-controls', `tabpanel-${key}`);
    tabBtns[key] = b;
    tabs.appendChild(b);
  }
  tabs.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    setTab(tab === 'agents' ? 'skills' : 'agents');
    tabBtns[tab].focus();
  });
  const actions = el('div', 'page-actions');
  const newBtn = button('New agent', 'btn primary', () => openNew());
  const importBtn = button('Import…', 'btn', () => fileInput.click());
  const exportSel = button('Export selected', 'btn', () => doExport(false));
  const exportAll = button('Export all', 'btn', () => doExport(true));
  actions.append(newBtn, importBtn, exportSel, exportAll);
  bar.append(tabs, actions);

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'application/json,.json';
  fileInput.hidden = true;
  fileInput.setAttribute('aria-label', 'Bundle file');
  fileInput.addEventListener('change', () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (f) askImport(f);
  });

  const panel = el('div', 'tabpanel');
  panel.setAttribute('role', 'tabpanel');
  page.append(head, bar, fileInput, panel);
  root.appendChild(page);

  // ---- dialogs ----
  const confirm = document.createElement('dialog');
  confirm.className = 'confirm';
  const cTitle = el('h3');
  const cText = el('p');
  const cRow = el('div', 'confirm-actions');
  const cCancel = button('Cancel', 'btn', () => confirm.close());
  const cOk = button('Delete', 'btn danger', () => { const fn = pendingDelete; pendingDelete = null; confirm.close(); if (fn) fn(); });
  cRow.append(cCancel, cOk);
  confirm.append(cTitle, cText, cRow);
  let pendingDelete = null;

  const importDlg = document.createElement('dialog');
  importDlg.className = 'confirm import';
  const iTitle = el('h3', null, 'Import a bundle');
  const iFile = el('p', 'import-file');
  const iRules = el('fieldset', 'rules');
  iRules.appendChild(el('legend', null, 'When a name already exists'));
  const RULES = [
    ['rename', 'Rename the imported one', 'Keeps yours; the new one gets a numbered name.'],
    ['replace', 'Replace mine', 'The imported one overwrites yours of the same name.'],
    ['skip', 'Skip it', 'Yours stays; the imported one is not written.'],
  ];
  for (const [value, label, sub] of RULES) {
    const lab = el('label', 'check');
    const r = document.createElement('input');
    r.type = 'radio';
    r.name = 'on_conflict';
    r.value = value;
    r.checked = value === 'rename';
    const t = el('span', 'check-text');
    t.append(el('span', 'check-name', label), el('span', 'check-desc', sub));
    lab.append(r, t);
    iRules.appendChild(lab);
  }
  const iRow = el('div', 'confirm-actions');
  const iCancel = button('Cancel', 'btn', () => importDlg.close());
  const iOk = button('Import', 'btn primary', () => runImport());
  iRow.append(iCancel, iOk);
  importDlg.append(iTitle, iFile, iRules, iRow);
  let importFile = null;

  const resultDlg = document.createElement('dialog');
  resultDlg.className = 'confirm result';
  resultDlg.setAttribute('aria-labelledby', 'import-result-title');
  const rTitle = el('h3', null, 'Import finished');
  rTitle.id = 'import-result-title';
  const rBody = el('div', 'result-body');
  const rRow = el('div', 'confirm-actions');
  const rOk = button('Close', 'btn primary', () => resultDlg.close());
  rRow.appendChild(rOk);
  resultDlg.append(rTitle, rBody, rRow);
  document.body.append(confirm, importDlg, resultDlg);

  // ---- data ----
  async function load() {
    data.error = null;
    const access = developer() ? 'advanced' : 'basic';
    try {
      const [catalog, agents, skills] = await Promise.all([api.getCatalog(developer() ? access : undefined), api.listMyAgents(), api.listMySkills()]);
      data.catalog = catalog && typeof catalog === 'object' ? catalog : { agents: [], skills: [], orchestrator: null };
      data.catalogAccess = access;
      data.agents = Array.isArray(agents) ? agents : [];
      data.skills = Array.isArray(skills) ? skills : [];
      data.loaded = true;
      for (const k of ['agents', 'skills']) {
        const ids = new Set(data[k].map((x) => x.id));
        for (const id of [...selected[k]]) if (!ids.has(id)) selected[k].delete(id);
      }
      onChanged?.(data);
    } catch (e) {
      if (e.status !== 401) data.error = 'The agents and skills could not be loaded.';
    }
    render();
  }

  function mineSkillsSeen() { return data.skills; }

  // ---- rendering ----
  function setTab(key) {
    tab = key;
    render();
  }

  function render() {
    for (const [key, b] of Object.entries(tabBtns)) {
      const on = key === tab;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle('on', on);
    }
    panel.id = `tabpanel-${tab}`;
    panel.setAttribute('aria-labelledby', `tab-${tab}`);
    newBtn.textContent = tab === 'agents' ? 'New agent' : 'New skill';
    const n = selected[tab].size;
    exportSel.textContent = n ? `Export selected (${n})` : 'Export selected';
    exportSel.disabled = !n;
    exportAll.disabled = !(data.agents.length || data.skills.length);
    panel.replaceChildren();
    if (data.error) {
      const p = el('p', 'page-error', data.error);
      p.setAttribute('role', 'alert');
      panel.append(p, button('Try again', 'btn', () => load()));
      return;
    }
    if (!data.loaded) { panel.appendChild(el('p', 'page-loading', 'Loading…')); return; }
    panel.append(mineSection(), adminSection());
  }

  function sectionHead(text, badge, count) {
    const h = el('div', 'section-head');
    const t = el('h3', null, text);
    if (badge) t.appendChild(lock());
    h.appendChild(t);
    h.appendChild(el('span', 'section-count', count));
    return h;
  }

  function mineSection() {
    const items = tab === 'agents' ? data.agents : data.skills;
    const max = tab === 'agents' ? LIMITS.agents : 100;
    const sec = el('section', 'list-section mine');
    sec.appendChild(sectionHead('Mine', false, `${items.length} of ${max}`));
    if (!items.length) {
      const empty = el('div', 'empty-list');
      empty.appendChild(el('p', 'empty-title', tab === 'agents' ? 'No agents of your own yet.' : 'No skills of your own yet.'));
      empty.appendChild(el('p', 'empty-sub', tab === 'agents'
        ? 'An agent is a name, a description and instructions; attach skills and let it consult the admin’s specialists.'
        : 'A skill is an instruction block you attach to one or more of your agents.'));
      empty.appendChild(button(tab === 'agents' ? 'Create your first agent' : 'Create your first skill', 'btn primary', () => openNew()));
      sec.appendChild(empty);
      return sec;
    }
    const ul = el('ul', 'rows');
    for (const it of items) ul.appendChild(tab === 'agents' ? agentRow(it) : skillRow(it));
    sec.appendChild(ul);
    return sec;
  }

  function selectBox(kind, it) {
    const lab = el('label', 'row-select');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = selected[kind].has(it.id);
    box.setAttribute('aria-label', `Select ${it.name} for export`);
    box.addEventListener('change', () => {
      if (box.checked) selected[kind].add(it.id); else selected[kind].delete(it.id);
      const n = selected[tab].size;
      exportSel.textContent = n ? `Export selected (${n})` : 'Export selected';
      exportSel.disabled = !n;
    });
    lab.appendChild(box);
    return lab;
  }

  function rowShell(kind, it, meta, actions) {
    const li = el('li', 'row');
    li.appendChild(selectBox(kind, it));
    const main = el('div', 'row-main');
    const name = el('span', 'row-name', it.name);
    if (kind === 'agents' && isAdvanced(it)) name.appendChild(developerBadge());
    main.appendChild(name);
    if (it.description) main.appendChild(el('span', 'row-desc', it.description));
    if (meta.length) {
      const m = el('span', 'row-meta');
      meta.forEach((t, i) => { if (i) m.appendChild(el('span', 'sep', '·')); m.appendChild(el('span', null, t)); });
      main.appendChild(m);
    }
    li.appendChild(main);
    const act = el('div', 'row-actions');
    act.append(...actions);
    li.appendChild(act);
    return li;
  }

  function modelLabel(id) {
    if (!id) return 'Default model';
    const m = (getModels?.() || []).find((x) => x.id === id);
    return m ? (m.label || m.id) : 'Model not available';
  }

  function agentRow(a) {
    const skills = Array.isArray(a.skills) ? a.skills.length : 0;
    const consults = Array.isArray(a.consults) ? a.consults.length : 0;
    const meta = [modelLabel(a.model), `${skills} ${skills === 1 ? 'skill' : 'skills'}`];
    if (consults) meta.push(`consults ${consults}`);
    if (a.deep === true) meta.push('deep tools');
    return rowShell('agents', a, meta, [
      button('Chat', 'btn small', () => onChat(a.ref || `mine:${a.id}`, a.name, a)),
      button('Edit', 'btn small ghost', () => openAgent(a, false)),
      button('Duplicate', 'btn small ghost', () => openAgent(a, true)),
      button('Delete', 'btn small ghost danger-text', () => askDelete('agent', a)),
    ]);
  }

  function skillRow(s) {
    const used = data.agents.filter((a) => (a.skills || []).some((x) => x.scope === 'mine' && x.name === s.name)).length;
    const meta = [`${(s.content || '').length.toLocaleString('en-US')} characters`];
    if (used) meta.push(`used by ${used} ${used === 1 ? 'agent' : 'agents'}`);
    return rowShell('skills', s, meta, [
      button('Edit', 'btn small ghost', () => openSkill(s, false)),
      button('Duplicate', 'btn small ghost', () => openSkill(s, true)),
      button('Delete', 'btn small ghost danger-text', () => askDelete('skill', s)),
    ]);
  }

  function adminSection() {
    // a non-developer never sees an advanced agent, whatever the catalogue answered
    const items = tab === 'agents' ? (data.catalog?.agents || []).filter((a) => developer() || !isAdvanced(a)) : (data.catalog?.skills || []);
    const sec = el('section', 'list-section admin');
    sec.appendChild(sectionHead('Admin · read-only', true, String(items.length)));
    if (!items.length) {
      sec.appendChild(el('p', 'empty-sub', tab === 'agents' ? 'No admin agent is open to chat right now.' : 'The admin has published no skills.'));
      return sec;
    }
    const ul = el('ul', 'rows');
    for (const it of items) {
      const li = el('li', 'row readonly');
      const main = el('div', 'row-main');
      const name = el('span', 'row-name', it.name);
      name.appendChild(lock());
      if (tab === 'agents' && isAdvanced(it)) name.appendChild(developerBadge());
      main.appendChild(name);
      if (it.description) main.appendChild(el('span', 'row-desc', it.description));
      li.appendChild(main);
      const act = el('div', 'row-actions');
      if (tab === 'agents') act.appendChild(button('Chat with this agent', 'btn small', () => onChat(it.ref || `admin:${it.name}`, it.name, it)));
      li.appendChild(act);
      ul.appendChild(li);
    }
    sec.appendChild(ul);
    return sec;
  }

  // ---- actions ----
  function openNew() { if (tab === 'agents') openAgent(null, false); else openSkill(null, false); }
  function openAgent(agent, duplicate) {
    const src = agent && duplicate ? { ...agent, name: copyNameOf(agent.name, data.agents) } : agent;
    editor.openAgent({ agent: src, duplicate, catalog: data.catalog, mine: { agents: data.agents, skills: mineSkillsSeen() }, models: getModels?.() || [], developer: developer() });
  }
  function openSkill(skill, duplicate) {
    const src = skill && duplicate ? { ...skill, name: copyNameOf(skill.name, data.skills) } : skill;
    editor.openSkill({ skill: src, duplicate, mine: { agents: data.agents, skills: data.skills } });
  }
  function copyNameOf(name, items) {
    const taken = new Set(items.map((x) => String(x.name).toLowerCase()));
    const base = String(name).replace(/\s+copy(?: \d+)?$/i, '');
    for (let i = 0; i < 100; i++) {
      const suffix = i ? ` copy ${i + 1}` : ' copy';
      const candidate = (base.slice(0, LIMITS.name - suffix.length) + suffix).trim();
      if (!taken.has(candidate.toLowerCase())) return candidate;
    }
    return base.slice(0, LIMITS.name);
  }

  function askDelete(kind, it) {
    cTitle.textContent = kind === 'agent' ? 'Delete this agent?' : 'Delete this skill?';
    cText.textContent = kind === 'agent'
      ? `“${it.name}” is removed. Conversations that used it continue with the orchestrator.`
      : `“${it.name}” is removed from your agents that use it.`;
    pendingDelete = async () => {
      try {
        if (kind === 'agent') await api.deleteMyAgent(it.id); else await api.deleteMySkill(it.id);
        notify(kind === 'agent' ? 'Agent deleted.' : 'Skill deleted.');
        await load();
      } catch (e) {
        if (e.status !== 401) notify(kind === 'agent' ? 'The agent could not be deleted.' : 'The skill could not be deleted.', 'error');
      }
    };
    confirm.showModal();
    cCancel.focus();
  }

  async function doExport(all) {
    const query = all ? '' : exportQuery([...selected.agents], [...selected.skills]);
    if (!all && !query) return;
    try {
      const { blob, filename } = await api.exportBundle(query);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      notify('Bundle downloaded.');
    } catch (e) {
      if (e.status !== 401) notify('The bundle could not be exported.', 'error');
    }
  }

  function askImport(file) {
    const isJson = /\.json$/i.test(file.name || '') || (file.type || '').includes('json');
    if (!isJson) { notify(TEXTS.bundle_type, 'error'); return; }
    if (file.size > LIMITS.bundleBytes) { notify(TEXTS.bundle_size, 'error'); return; }
    importFile = file;
    iFile.textContent = `${file.name} · ${file.size.toLocaleString('en-US')} bytes`;
    importDlg.querySelector('input[value="rename"]').checked = true;
    importDlg.showModal();
    iOk.focus();
  }

  async function runImport() {
    const file = importFile;
    if (!file) return;
    const rule = importDlg.querySelector('input[name="on_conflict"]:checked')?.value || 'rename';
    iOk.disabled = true;
    try {
      const result = await api.importBundle(file, rule);
      importDlg.close();
      showResult(formatImportResult(result));
      await load();
    } catch (e) {
      importDlg.close();
      if (e.status === 413) notify(TEXTS.bundle_size, 'error');
      else if (e.status === 422 || e.status === 415) notify('The file is not a bundle this app can read.', 'error');
      else if (e.status !== 401) notify('The bundle could not be imported.', 'error');
    } finally {
      iOk.disabled = false;
      importFile = null;
    }
  }

  function showResult(r) {
    rBody.replaceChildren();
    const block = (title, lines, cls) => {
      if (!lines.length) return;
      const d = el('div', `result-block ${cls || ''}`);
      d.appendChild(el('h4', null, title));
      const ul = el('ul');
      for (const line of lines) ul.appendChild(el('li', null, line));
      d.appendChild(ul);
      rBody.appendChild(d);
    };
    block('Written', r.summary, 'ok');
    block('Renamed', r.renamed);
    block('Dropped references', r.dropped, 'warn');
    block('Not imported', r.errors, 'err');
    resultDlg.showModal();
    rOk.focus();
  }

  return {
    load,
    show() { page.hidden = false; load(); },
    hide() { page.hidden = true; },
    data: () => data,
    setTab,
  };
}
