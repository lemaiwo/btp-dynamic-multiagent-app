// Personal agents and skills: the limits of addendum (c) as client-side
// checks with fixed texts, the export selection as a query string, the
// import result as lines for the summary dialog, and the header picker's
// option groups; addendum (d) adds the tier of an agent (basic | advanced),
// the "Developer" badge and the developer-only checks (deep agent tools,
// consulting an advanced agent). Pure: no DOM, no fetch (the jsdom tests
// import it as is).

export const TIERS = ['basic', 'advanced'];
export const DEVELOPER_BADGE = 'Developer';

/** 'basic' | 'advanced' from the server's `tier`; a personal agent with `deep` on is advanced too. */
export function tierOf(agent) {
  if (!agent || typeof agent !== 'object') return 'basic';
  if (agent.tier === 'advanced') return 'advanced';
  if (agent.deep === true) return 'advanced';
  return 'basic';
}
export function isAdvanced(agent) { return tierOf(agent) === 'advanced'; }

/** The option label of the picker: "name · Developer" for an advanced agent. */
export function optionLabel(agent) {
  const name = String(agent?.name ?? '');
  return isAdvanced(agent) ? `${name} · ${DEVELOPER_BADGE}` : name;
}

/** The names of the catalogue's advanced agents (for the consults check). */
export function advancedAgentNames(catalog) {
  return new Set((catalog?.agents || []).filter(isAdvanced).map((a) => String(a.name)));
}

export const LIMITS = {
  name: 64,
  description: 300,
  instructions: 20_000,
  content: 50_000,
  skills: 20,
  consults: 10,
  agents: 50,
  bundleBytes: 1024 * 1024,
};

export const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,63}$/;

/** Fixed texts; a value never appears in them. */
export const TEXTS = {
  name: 'Use 1 to 64 letters, digits, spaces, dots, hyphens, underscores or parentheses, starting with a letter or digit.',
  limit_agents: 'You have reached the limit of 50 agents.',
  limit_skills: 'You have reached the limit of 100 skills.',
  name_taken_agent: 'You already have an agent of this name.',
  name_taken_skill: 'You already have a skill of this name.',
  description: 'At most 300 characters, on one line.',
  instructions: 'Write 1 to 20,000 characters.',
  content: 'Write 1 to 50,000 characters.',
  skills: 'Pick at most 20 skills.',
  consults: 'Pick at most 10 agents.',
  consults_advanced: 'Consulting a developer agent needs the developer role.',
  deep: 'Deep agent tools need the developer role.',
  model: 'Pick a model from the list, or the default.',
  control: 'Control characters are not allowed (tab and line breaks are fine).',
  server: 'The server did not accept this value.',
  bundle_type: 'Pick a .json file.',
  bundle_size: 'The file is over 1 MB.',
};

// C0/C1 controls except tab, LF, CR; one line = none of those either
const CONTROL_RE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/;
const LINE_RE = /[\r\n\u2028\u2029]/;

function checkText(value, { min = 0, max, oneLine = false }) {
  const v = typeof value === 'string' ? value : '';
  if (v.length < min || v.length > max) return 'length';
  if (CONTROL_RE.test(v)) return 'control';
  if (oneLine && LINE_RE.test(v)) return 'length';
  return null;
}

/**
 * Check an agent form -> {field: text}. Empty when it may be sent.
 * `taken` is a lower-cased set of the user's other agent names;
 * `developer` false refuses `deep` and a consult in `advancedNames`
 * (the server answers 422 at the field for a non-developer).
 */
export function validateAgent(form, { taken = new Set(), developer = true, advancedNames = new Set() } = {}) {
  const errors = {};
  const name = String(form.name ?? '');
  if (!NAME_RE.test(name) || CONTROL_RE.test(name)) errors.name = TEXTS.name;
  else if (taken.has(name.toLowerCase())) errors.name = TEXTS.name_taken_agent;
  const d = checkText(form.description ?? '', { max: LIMITS.description, oneLine: true });
  if (d) errors.description = d === 'control' ? TEXTS.control : TEXTS.description;
  const i = checkText(form.instructions ?? '', { min: 1, max: LIMITS.instructions });
  if (i) errors.instructions = i === 'control' ? TEXTS.control : TEXTS.instructions;
  const skills = Array.isArray(form.skills) ? form.skills : [];
  if (skills.length > LIMITS.skills) errors.skills = TEXTS.skills;
  const consults = Array.isArray(form.consults) ? form.consults : [];
  if (consults.length > LIMITS.consults) errors.consults = TEXTS.consults;
  else if (!developer && consults.some((c) => advancedNames.has(c))) errors.consults = TEXTS.consults_advanced;
  if (form.deep != null && typeof form.deep !== 'boolean') errors.deep = TEXTS.deep;
  else if (form.deep === true && !developer) errors.deep = TEXTS.deep;
  if (form.model != null && form.model !== '' && Array.isArray(form.models) && !form.models.includes(form.model)) errors.model = TEXTS.model;
  return errors;
}

/** Check a skill form -> {field: text}. */
export function validateSkill(form, { taken = new Set() } = {}) {
  const errors = {};
  const name = String(form.name ?? '');
  if (!NAME_RE.test(name) || CONTROL_RE.test(name)) errors.name = TEXTS.name;
  else if (taken.has(name.toLowerCase())) errors.name = TEXTS.name_taken_skill;
  const d = checkText(form.description ?? '', { max: LIMITS.description, oneLine: true });
  if (d) errors.description = d === 'control' ? TEXTS.control : TEXTS.description;
  const c = checkText(form.content ?? '', { min: 1, max: LIMITS.content });
  if (c) errors.content = c === 'control' ? TEXTS.control : TEXTS.content;
  return errors;
}

/** The names of `items` except `exceptId`, lower-cased, for the uniqueness check. */
export function takenNames(items, exceptId = null) {
  return new Set((items || []).filter((x) => x.id !== exceptId).map((x) => String(x.name || '').toLowerCase()));
}

/**
 * A server 422's `detail[]` -> {field: text}. The first path part after
 * `body` names the field (`skills.0.name` -> `skills`); the text is ours,
 * never the server's message or the value.
 */
export function fieldErrors(errors, fields) {
  const out = {};
  for (const e of errors || []) {
    const loc = (e.loc || []).filter((p) => p !== 'body');
    const field = loc[0];
    if (field && fields.includes(field) && !out[field]) out[field] = TEXTS[field] || TEXTS.server;
  }
  return out;
}

/**
 * The export selection as the query string of GET /my/export: ids joined by
 * commas per kind; nothing selected = '' (everything of the user).
 */
export function exportQuery(agentIds = [], skillIds = []) {
  const parts = [];
  const a = [...new Set(agentIds.filter(Boolean))];
  const s = [...new Set(skillIds.filter(Boolean))];
  if (a.length) parts.push(`agents=${a.map(encodeURIComponent).join(',')}`);
  if (s.length) parts.push(`skills=${s.map(encodeURIComponent).join(',')}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

const RULE_TEXTS = {
  pattern: 'is not valid',
  too_long: 'is too long',
  too_short: 'is empty',
  too_many: 'has too many entries',
  duplicate: 'exists already',
  limit: 'would pass your limit',
  unknown_model: 'names a model that does not exist here',
  unknown_key: 'is a key this app does not know',
  control: 'holds control characters',
};

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * The answer of POST /my/import -> what the summary dialog shows:
 * {summary: [line], renamed: [line], dropped: [line], errors: [line], ok}.
 * Names in the lines are the user's own bundle item names (text nodes).
 */
export function formatImportResult(result) {
  const r = result || {};
  const kinds = [['agents', 'agent', 'agents'], ['skills', 'skill', 'skills']];
  const summary = [];
  const renamed = [];
  for (const [key, one, many] of kinds) {
    const k = r[key] || {};
    const created = Number(k.created) || 0;
    const replaced = Number(k.replaced) || 0;
    const skipped = Number(k.skipped) || 0;
    summary.push(`${plural(created, one, many)} created, ${replaced} replaced, ${skipped} skipped`);
    for (const x of Array.isArray(k.renamed) ? k.renamed : []) {
      if (x && typeof x === 'object') renamed.push(`${one} ${String(x.from ?? '')} → ${String(x.to ?? '')}`);
    }
  }
  const dropped = (Array.isArray(r.dropped) ? r.dropped : []).map((d) => {
    const item = String(d.item ?? '');
    if (d.field === 'deep') return `${item}: Deep agent tools (needs the developer role) were dropped`;
    const what = d.field === 'consults' ? 'consulted agent' : d.field === 'model' ? 'model' : 'skill';
    const name = d.name == null || d.name === '' ? '' : ` ${String(d.name)}`;
    return `${item}: ${what}${name} does not exist here and was dropped`;
  });
  // `rule` is the server's fixed text (never a value); a short code is worded here
  const errors = (Array.isArray(r.errors) ? r.errors : []).map((e) => {
    const raw = String(e.rule ?? '').trim();
    const rule = RULE_TEXTS[raw] || raw || 'was not accepted';
    return `${String(e.item ?? '')}: ${String(e.field ?? 'item')} \u2014 ${rule}; not imported`;
  });
  return { summary, renamed, dropped, errors, ok: errors.length === 0 };
}

/**
 * The header picker: [{label, options: [{value, label, disabled?}]}].
 * `value` is the agent_ref ('' for the orchestrator). A `current` ref that
 * is in no group (a deleted agent, or an advanced one in a basic
 * conversation) gets a disabled option so the select still shows the
 * conversation's state. `access` basic leaves every advanced agent out;
 * advanced lists them with the Developer badge in the label.
 */
export function pickerGroups(catalog, mine, current = null, currentName = '', { access = 'basic' } = {}) {
  const orch = catalog?.orchestrator || {};
  const offered = (list) => (list || []).filter((a) => access === 'advanced' || !isAdvanced(a));
  const groups = [
    { label: 'Orchestrator', options: [{ value: '', label: orch.name || 'Orchestrator' }] },
    { label: 'My agents', options: offered(mine).map((a) => ({ value: a.ref || `mine:${a.id}`, label: optionLabel(a) })) },
    { label: 'Admin agents · read-only', options: offered(catalog?.agents).map((a) => ({ value: a.ref || `admin:${a.name}`, label: optionLabel(a) })) },
  ];
  const ref = current || '';
  if (ref && !groups.some((g) => g.options.some((o) => o.value === ref))) {
    groups.push({ label: 'Unavailable', options: [{ value: ref, label: currentName || 'Deleted agent', disabled: true }] });
  }
  return groups.filter((g) => g.options.length);
}

/** Is `ref` offered by the picker for this access (not the Unavailable group)? '' / null = the orchestrator, always. */
export function refOffered(ref, catalog, mine, access = 'basic') {
  if (!ref) return true;
  return pickerGroups(catalog, mine, null, '', { access }).some((g) => g.options.some((o) => o.value === ref));
}

/** The agent row behind a ref (mine or admin), or null. */
export function agentOf(ref, catalog, mine) {
  if (!ref) return null;
  return (mine || []).find((a) => (a.ref || `mine:${a.id}`) === ref)
    || (catalog?.agents || []).find((x) => (x.ref || `admin:${x.name}`) === ref)
    || null;
}

/** The display name of a ref from the lists, or the stored name, or ''. */
export function agentNameOf(ref, catalog, mine, stored = '') {
  if (!ref) return catalog?.orchestrator?.name || 'Orchestrator';
  const m = (mine || []).find((a) => (a.ref || `mine:${a.id}`) === ref);
  if (m) return m.name;
  const a = (catalog?.agents || []).find((x) => (x.ref || `admin:${x.name}`) === ref);
  if (a) return a.name;
  return stored || '';
}
