// The editor of a personal agent or skill: a <dialog> that slides in from
// the right (showModal traps focus and binds Escape). Name, description,
// instructions / content with a character counter, and for an agent the
// skills (Mine, Admin · read-only), the admin agents it may consult and the
// model; for a developer (addendum d) the "Deep agent tools" toggle and the
// advanced agents in "May consult", each with the Developer badge, which a
// non-developer never sees. Client checks mirror the limits with fixed
// texts; a server 422 marks the field, a 409 marks the name. Names and
// descriptions of the catalogue are inserted as text nodes only.

import * as api from '../api.js';
import { LIMITS, TEXTS, validateAgent, validateSkill, takenNames, fieldErrors, isAdvanced, advancedAgentNames } from '../myagents.js';

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = text;
  return n;
}

let fieldSeq = 0;

/** A labelled control with an error line; `control` is the input element. */
function field(label, control, { hint } = {}) {
  const id = `fld-${++fieldSeq}`;
  const wrap = el('div', 'field');
  const lab = el('label', 'field-label', label);
  lab.htmlFor = id;
  control.id = id;
  const err = el('p', 'field-error');
  err.id = `${id}-err`;
  err.hidden = true;
  wrap.append(lab, control);
  if (hint) wrap.appendChild(hint);
  wrap.appendChild(err);
  return {
    wrap, control, err,
    setError(text) {
      err.textContent = text || '';
      err.hidden = !text;
      wrap.classList.toggle('invalid', !!text);
      if (text) { control.setAttribute('aria-invalid', 'true'); control.setAttribute('aria-describedby', err.id); }
      else { control.removeAttribute('aria-invalid'); control.removeAttribute('aria-describedby'); }
    },
  };
}

/** A group of checkboxes (a fieldset) with an error line. */
function checkGroup(legend, items, checked, { emptyText, lockBadge = false } = {}) {
  const fs = el('fieldset', 'check-group');
  const lg = el('legend', null, legend);
  if (lockBadge) lg.appendChild(lock());
  fs.appendChild(lg);
  if (!items.length) {
    fs.appendChild(el('p', 'check-empty', emptyText || 'Nothing here yet.'));
  }
  const boxes = [];
  for (const it of items) {
    const row = el('label', 'check');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.value = it.value;
    box.checked = checked.has(it.value);
    const text = el('span', 'check-text');
    const nm = el('span', 'check-name', it.name);
    if (it.badge) { nm.appendChild(document.createTextNode(' ')); nm.appendChild(developerBadge()); }
    text.appendChild(nm);
    if (it.description) text.appendChild(el('span', 'check-desc', it.description));
    row.append(box, text);
    fs.appendChild(row);
    boxes.push(box);
  }
  const err = el('p', 'field-error');
  err.hidden = true;
  fs.appendChild(err);
  return {
    wrap: fs,
    values: () => boxes.filter((b) => b.checked).map((b) => b.value),
    setError(text) { err.textContent = text || ''; err.hidden = !text; fs.classList.toggle('invalid', !!text); },
  };
}

export function lock() {
  const b = el('span', 'lock', '');
  b.setAttribute('aria-label', 'read-only');
  b.title = 'Read-only';
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('width', '12');
  s.setAttribute('height', '12');
  s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', 'M7 11V8a5 5 0 0110 0v3M6 11h12v9H6z');
  s.appendChild(p);
  b.appendChild(s);
  return b;
}

/** The "Developer" badge of an advanced agent (a text node; the word is ours). */
export function developerBadge() {
  const b = el('span', 'badge dev', 'Developer');
  b.title = 'Developer agent: advanced access only';
  return b;
}

function counter(textarea, max) {
  const c = el('p', 'counter');
  c.setAttribute('aria-live', 'polite');
  const update = () => {
    const n = textarea.value.length;
    c.textContent = `${n.toLocaleString('en-US')} / ${max.toLocaleString('en-US')}`;
    c.classList.toggle('over', n > max || n === 0);
  };
  textarea.addEventListener('input', update);
  update();
  return { node: c, update };
}

/**
 * createAgentEditor({notify, onSaved}) -> {openAgent(opts), openSkill(opts), close()}
 *   openAgent({agent, duplicate, catalog, mine, models, developer})   agent = row to edit (null = new),
 *     duplicate = prefill from a row but create; mine = {agents, skills};
 *     developer = the caller has the developer role (deep toggle, advanced consults)
 *   openSkill({skill, duplicate, mine})
 * `onSaved(kind, row, {created})` after a 2xx.
 */
export function createAgentEditor({ notify, onSaved }) {
  const dialog = document.createElement('dialog');
  dialog.className = 'panel';
  dialog.setAttribute('aria-labelledby', 'panel-title');
  document.body.appendChild(dialog);
  let busy = false;
  dialog.addEventListener('cancel', (e) => { if (busy) e.preventDefault(); });
  dialog.addEventListener('close', () => { dialog.replaceChildren(); });

  function frame(title, onSubmit, submitLabel) {
    dialog.replaceChildren();
    const form = el('form', 'panel-form');
    form.noValidate = true;
    const head = el('header', 'panel-head');
    const h = el('h2', null, title);
    h.id = 'panel-title';
    const close = el('button', 'icon-btn panel-close');
    close.type = 'button';
    close.setAttribute('aria-label', 'Close');
    close.textContent = '×';
    close.addEventListener('click', () => { if (!busy) dialog.close(); });
    head.append(h, close);
    const body = el('div', 'panel-body');
    const foot = el('footer', 'panel-foot');
    const formError = el('p', 'form-error');
    formError.setAttribute('role', 'alert');
    formError.hidden = true;
    const cancel = el('button', 'btn', 'Cancel');
    cancel.type = 'button';
    cancel.addEventListener('click', () => { if (!busy) dialog.close(); });
    const save = el('button', 'btn primary', submitLabel);
    save.type = 'submit';
    foot.append(formError, cancel, save);
    form.append(head, body, foot);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (busy) return;
      busy = true;
      save.disabled = true;
      formError.hidden = true;
      try { await onSubmit(); } finally { busy = false; save.disabled = false; }
    });
    dialog.appendChild(form);
    return { body, formError };
  }

  function applyServerError(e, fields, kind, nameField, formError) {
    if (e.status === 409) { nameField.setError(kind === 'agent' ? TEXTS.name_taken_agent : TEXTS.name_taken_skill); nameField.control.focus(); return; }
    if (e.status === 422) {
      const errs = fieldErrors(e.errors, Object.keys(fields));
      let first = null;
      for (const [k, f] of Object.entries(fields)) { f.setError(errs[k] || ''); if (errs[k] && !first) first = f; }
      if (first) { (first.control || first.wrap.querySelector('input')).focus(); return; }
      // a 422 with a string detail: the per-user limit (50 agents / 100 skills)
      formError.textContent = e.errors.length ? 'The server refused the entry. Check the fields and try again.' : (kind === 'agent' ? TEXTS.limit_agents : TEXTS.limit_skills);
      formError.hidden = false;
      return;
    }
    if (e.status === 401) return;
    formError.textContent = kind === 'agent' ? 'The agent could not be saved.' : 'The skill could not be saved.';
    formError.hidden = false;
  }

  function openAgent({ agent = null, duplicate = false, catalog, mine, models = [], developer = false }) {
    const editing = agent && !duplicate ? agent : null;
    const src = agent || {};
    const { body, formError } = frame(editing ? 'Edit agent' : 'New agent', submit, editing ? 'Save changes' : 'Create agent');

    const name = document.createElement('input');
    name.type = 'text';
    name.maxLength = LIMITS.name;
    name.autocomplete = 'off';
    name.value = src.name || '';
    const fName = field('Name', name);

    const desc = document.createElement('input');
    desc.type = 'text';
    desc.maxLength = LIMITS.description;
    desc.value = src.description || '';
    desc.placeholder = 'One line: what this agent is for';
    const fDesc = field('Description', desc);

    const instr = document.createElement('textarea');
    instr.className = 'mono';
    instr.rows = 12;
    instr.value = src.instructions || '';
    instr.placeholder = 'How the agent should work, what to check, how to answer.';
    const cnt = counter(instr, LIMITS.instructions);
    const fInstr = field('Instructions', instr, { hint: cnt.node });

    const pickedSkills = new Set((src.skills || []).map((s) => `${s.scope}:${s.name}`));
    const gMine = checkGroup('Skills · mine', (mine.skills || []).map((s) => ({ value: `mine:${s.name}`, name: s.name, description: s.description })), pickedSkills,
      { emptyText: 'You have no skills yet. Create one on the Skills tab.' });
    const gAdmin = checkGroup('Skills · admin', (catalog?.skills || []).map((s) => ({ value: `admin:${s.name}`, name: s.name, description: s.description })), pickedSkills,
      { emptyText: 'The admin has published no skills.', lockBadge: true });
    const skillsErr = { setError: (t) => { gMine.setError(t); } };

    const pickedConsults = new Set(src.consults || []);
    const consultable = (catalog?.agents || []).filter((a) => developer || !isAdvanced(a));
    const gConsult = checkGroup('May consult · admin agents', consultable.map((a) => ({ value: a.name, name: a.name, description: a.description, badge: isAdvanced(a) })), pickedConsults,
      { emptyText: 'No admin agent is open to chat right now.', lockBadge: true });

    // Deep agent tools (developers only): plan, scratchpad, sub-agents
    let deepBox = null;
    let deepErr = null;
    let deepSection = null;
    if (developer) {
      deepSection = el('div', 'panel-section');
      const h = el('h3', null, 'Developer tools');
      h.appendChild(document.createTextNode(' '));
      h.appendChild(developerBadge());
      deepSection.appendChild(h);
      deepSection.appendChild(el('p', 'section-sub', 'An agent with deep tools, or one that consults a developer agent, is a developer agent itself: it is offered in advanced conversations only.'));
      const row = el('label', 'check deep-toggle');
      deepBox = document.createElement('input');
      deepBox.type = 'checkbox';
      deepBox.checked = src.deep === true;
      deepBox.setAttribute('aria-describedby', 'deep-desc');
      const text = el('span', 'check-text');
      text.appendChild(el('span', 'check-name', 'Deep agent tools'));
      const d = el('span', 'check-desc', 'A plan (write_todos / read_todos), a scratchpad of files and sub-agents that share both, with the app’s default limits.');
      d.id = 'deep-desc';
      text.appendChild(d);
      row.append(deepBox, text);
      deepErr = el('p', 'field-error');
      deepErr.hidden = true;
      deepSection.append(row, deepErr);
    }
    const deepField = { setError: (t) => { if (!deepErr) return; deepErr.textContent = t || ''; deepErr.hidden = !t; }, control: deepBox };

    const model = document.createElement('select');
    const def = document.createElement('option');
    def.value = '';
    def.textContent = 'Default';
    model.appendChild(def);
    for (const m of models) {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = m.label || m.id;
      model.appendChild(o);
    }
    model.value = src.model && models.some((m) => m.id === src.model) ? src.model : '';
    const fModel = field('Model', model);

    const grid = el('div', 'panel-grid');
    grid.append(fName.wrap, fDesc.wrap, fInstr.wrap);
    const skillsBox = el('div', 'panel-section');
    skillsBox.appendChild(el('h3', null, 'Skills'));
    skillsBox.appendChild(el('p', 'section-sub', `Reusable instruction blocks, inlined into the agent's prompt. At most ${LIMITS.skills}.`));
    skillsBox.append(gMine.wrap, gAdmin.wrap);
    const consultBox = el('div', 'panel-section');
    consultBox.appendChild(el('h3', null, 'May consult'));
    consultBox.appendChild(el('p', 'section-sub', `Admin agents this agent can hand a question to. At most ${LIMITS.consults}.`));
    consultBox.appendChild(gConsult.wrap);
    body.append(grid, skillsBox, consultBox);
    if (deepSection) body.appendChild(deepSection);
    body.appendChild(fModel.wrap);

    const fields = { name: fName, description: fDesc, instructions: fInstr, skills: skillsErr, consults: gConsult, model: fModel };
    if (developer) fields.deep = deepField;
    const read = () => ({
      name: name.value,
      description: desc.value,
      instructions: instr.value,
      skills: [...gMine.values(), ...gAdmin.values()].map((v) => { const i = v.indexOf(':'); return { scope: v.slice(0, i), name: v.slice(i + 1) }; }),
      consults: gConsult.values(),
      model: model.value || null,
      // `deep` is sent by a developer only; the server's default is false
      ...(developer ? { deep: deepBox.checked === true } : {}),
    });

    async function submit() {
      const form = read();
      const errs = validateAgent({ ...form, models: models.map((m) => m.id) },
        { taken: takenNames(mine.agents, editing?.id), developer, advancedNames: advancedAgentNames(catalog) });
      let first = null;
      for (const [k, f] of Object.entries(fields)) { f.setError(errs[k] || ''); if (errs[k] && !first) first = f; }
      if (first) { (first.control || gMine.wrap.querySelector('input') || name).focus(); return; }
      try {
        const row = editing ? await api.updateMyAgent(editing.id, form) : await api.createMyAgent(form);
        dialog.close();
        notify(editing ? 'Agent saved.' : 'Agent created.');
        onSaved('agent', row, { created: !editing });
      } catch (e) {
        applyServerError(e, fields, 'agent', fName, formError);
      }
    }
    dialog.showModal();
    name.focus();
  }

  function openSkill({ skill = null, duplicate = false, mine }) {
    const editing = skill && !duplicate ? skill : null;
    const src = skill || {};
    const { body, formError } = frame(editing ? 'Edit skill' : 'New skill', submit, editing ? 'Save changes' : 'Create skill');
    const name = document.createElement('input');
    name.type = 'text';
    name.maxLength = LIMITS.name;
    name.autocomplete = 'off';
    name.value = src.name || '';
    const fName = field('Name', name);
    const desc = document.createElement('input');
    desc.type = 'text';
    desc.maxLength = LIMITS.description;
    desc.value = src.description || '';
    desc.placeholder = 'One line: when an agent should use it';
    const fDesc = field('Description', desc);
    const content = document.createElement('textarea');
    content.className = 'mono';
    content.rows = 18;
    content.value = src.content || '';
    content.placeholder = 'The instruction block an agent gets when this skill is attached.';
    const cnt = counter(content, LIMITS.content);
    const fContent = field('Content', content, { hint: cnt.node });
    const grid = el('div', 'panel-grid');
    grid.append(fName.wrap, fDesc.wrap, fContent.wrap);
    body.appendChild(grid);
    const fields = { name: fName, description: fDesc, content: fContent };

    async function submit() {
      const form = { name: name.value, description: desc.value, content: content.value };
      const errs = validateSkill(form, { taken: takenNames(mine.skills, editing?.id) });
      let first = null;
      for (const [k, f] of Object.entries(fields)) { f.setError(errs[k] || ''); if (errs[k] && !first) first = f; }
      if (first) { first.control.focus(); return; }
      try {
        const row = editing ? await api.updateMySkill(editing.id, form) : await api.createMySkill(form);
        dialog.close();
        notify(editing ? 'Skill saved.' : 'Skill created.');
        onSaved('skill', row, { created: !editing });
      } catch (e) {
        applyServerError(e, fields, 'skill', fName, formError);
      }
    }
    dialog.showModal();
    name.focus();
  }

  return { openAgent, openSkill, close: () => dialog.open && dialog.close(), isOpen: () => dialog.open };
}
